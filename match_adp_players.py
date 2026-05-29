"""
match_adp_players.py - link rows in the `adp` table to player_ids in the
`players` table via three-pass matching:

  1. Manual override - check adp_name_mapping(raw_name -> player_id)
  2. Exact match    - player_name in players table
  3. Fuzzy match    - normalized name (lowercase, strip suffixes & punctuation),
                       verified against position and NFL team to avoid false
                       collisions

Unmatched rows are written to adp_unmatched.csv for human review. You can
add explicit overrides by inserting into adp_name_mapping and rerunning.

Idempotent: rows that already have a player_id are skipped unless --rematch is
passed.

Usage:
    python match_adp_players.py
    python match_adp_players.py --season 2026   # match just one season
    python match_adp_players.py --rematch       # clear existing matches first
"""

import argparse
import csv
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "fantasy.db"
UNMATCHED_CSV = Path(__file__).parent / "adp_unmatched.csv"

# Yahoo and FantasyPros sometimes use different NFL team abbreviations.
# Map FantasyPros -> Yahoo style. Add to this list when you find mismatches.
TEAM_ALIAS = {
    "JAC": "Jax",
    "JAX": "Jax",
    "WAS": "Was",
    "WSH": "Was",
    "LAR": "LAR",
    "LV":  "LV",
    "NO":  "NO",
    "SF":  "SF",
    "TB":  "TB",
    "GB":  "GB",
    "NE":  "NE",
    "KC":  "KC",
    "NYG": "NYG",
    "NYJ": "NYJ",
    "LAC": "LAC",
}


def normalize(name):
    """Lowercase, strip suffix (Jr/Sr/II/III/IV/V), strip punctuation, collapse whitespace."""
    if not name:
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", "", s)
    s = re.sub(r"[\.,'’\-]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_team(team):
    if not team:
        return ""
    t = team.upper().strip()
    return TEAM_ALIAS.get(t, t).upper()


def normalize_position(pos):
    """Strip position rank suffix - 'WR1' -> 'WR', 'RB14' -> 'RB'."""
    if not pos:
        return ""
    m = re.match(r"^([A-Za-z]+)", str(pos))
    return m.group(1).upper() if m else str(pos).upper()


def build_player_index(conn):
    """Build dicts for fast matching."""
    exact = {}           # player_name (raw) -> player_id
    by_norm_name = {}    # normalized_name -> [(player_id, position, nfl_team), ...]
    for row in conn.execute(
        "SELECT player_id, player_name, position, nfl_team FROM players"
    ):
        pid, name, pos, team = row
        exact[name] = pid
        nn = normalize(name)
        by_norm_name.setdefault(nn, []).append((pid, normalize_position(pos), normalize_team(team)))
    return exact, by_norm_name


def find_match(raw_name, raw_pos, raw_team, exact_idx, fuzzy_idx, overrides):
    """Return (player_id, match_reason) or (None, reason_unmatched)."""
    if raw_name in overrides:
        return overrides[raw_name], "override"
    if raw_name in exact_idx:
        return exact_idx[raw_name], "exact"

    nn = normalize(raw_name)
    candidates = fuzzy_idx.get(nn, [])
    if not candidates:
        return None, "no_norm_match"
    if len(candidates) == 1:
        return candidates[0][0], "fuzzy_unique"

    # Multiple candidates - disambiguate by position + team
    np = normalize_position(raw_pos)
    nt = normalize_team(raw_team)
    qualified = [c for c in candidates if c[1] == np and c[2] == nt]
    if len(qualified) == 1:
        return qualified[0][0], "fuzzy_pos_team"
    if len(qualified) > 1:
        return None, "fuzzy_ambiguous"

    # Loosen to just position match
    qualified = [c for c in candidates if c[1] == np]
    if len(qualified) == 1:
        return qualified[0][0], "fuzzy_pos_only"

    return None, "fuzzy_no_disambig"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--season", type=int, default=None,
                   help="Match only this season; default = all")
    p.add_argument("--rematch", action="store_true",
                   help="Clear existing player_id matches before re-running")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.rematch:
        sql = "UPDATE adp SET player_id = NULL"
        params = []
        if args.season:
            sql += " WHERE season = ?"
            params.append(args.season)
        n = conn.execute(sql, params).rowcount
        conn.commit()
        print(f"Cleared player_id on {n} rows")

    exact_idx, fuzzy_idx = build_player_index(conn)
    overrides = {r["raw_name"]: r["player_id"]
                 for r in conn.execute("SELECT raw_name, player_id FROM adp_name_mapping")}

    sql = """
        SELECT season, source, player_name_raw, position_rank, nfl_team
        FROM adp
        WHERE player_id IS NULL
    """
    params = []
    if args.season:
        sql += " AND season = ?"
        params.append(args.season)
    sql += " ORDER BY season, overall_rank"
    rows = conn.execute(sql, params).fetchall()
    print(f"Matching {len(rows)} unmatched rows")

    counts = {"override": 0, "exact": 0, "fuzzy_unique": 0,
              "fuzzy_pos_team": 0, "fuzzy_pos_only": 0}
    unmatched = []

    for r in rows:
        pid, reason = find_match(
            r["player_name_raw"], r["position_rank"], r["nfl_team"],
            exact_idx, fuzzy_idx, overrides
        )
        if pid is None:
            unmatched.append({
                "season": r["season"], "name": r["player_name_raw"],
                "position": r["position_rank"], "nfl_team": r["nfl_team"],
                "reason": reason,
            })
            continue
        conn.execute(
            "UPDATE adp SET player_id = ? WHERE season=? AND source=? AND player_name_raw=?",
            (pid, r["season"], r["source"], r["player_name_raw"])
        )
        counts[reason] = counts.get(reason, 0) + 1
    conn.commit()

    print("\nMatch results:")
    for k, v in counts.items():
        print(f"  {k:18s} {v}")
    print(f"  unmatched          {len(unmatched)}")

    if unmatched:
        with UNMATCHED_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["season", "name", "position", "nfl_team", "reason"])
            w.writeheader()
            w.writerows(unmatched)
        print(f"\nWrote {UNMATCHED_CSV} for review.")
        print("To resolve, find player_id for each row and INSERT INTO adp_name_mapping,")
        print("then rerun: python match_adp_players.py")

    conn.close()


if __name__ == "__main__":
    main()
