"""apply_excel_keeper_overrides.py - read Pete's "Latest Players on Rosters"
sheet and populate keeper_status_overrides from the K flags.

The Excel file is the source of truth for 2024 and 2025 keeper status. For
every draft_picks row in those seasons, we look up the player+manager in the
Excel sheet. If the K flag is set, is_keeper=1. If not (and we have a row),
is_keeper=0. Anything we can't match gets logged for review.

Usage:
    python apply_excel_keeper_overrides.py --dry-run    # report only, no DB writes
    python apply_excel_keeper_overrides.py --commit     # actually insert

The dry-run summary always runs first; --commit is required to write.
"""

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict

import openpyxl

DB = "fantasy.db"
EXCEL_FILE = "I Year For Your Sweet TD's Transaction + Draft Tracking.xlsx"
SHEET_NAME = "Latest Players on Rosters"

# Column indices (0-based) in the sheet
COL_PLAYER = 0
COL_MANAGER = 3
COL_2024_K = 9
COL_2025_K = 15

UNMATCHED_CSV = "unmatched_keeper_rows.csv"


def normalize_player(name):
    """Normalize a player name for matching.
    Strips suffixes, punctuation, case, and collapses whitespace."""
    if not name:
        return ""
    s = str(name).lower().strip()
    # Strip trailing suffixes
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?$", "", s).strip()
    # Strip punctuation
    s = re.sub(r"[^\w\s]", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_player_index(conn):
    """Return {normalized_name: [player_id, ...]} for fuzzy lookup."""
    idx = defaultdict(list)
    for pid, name in conn.execute("SELECT player_id, player_name FROM players"):
        idx[normalize_player(name)].append(pid)
    return idx


def build_team_index(conn):
    """Return {(manager_full_name, season): team_season_id}."""
    out = {}
    for season, ts_id, full_name in conn.execute("""
        SELECT t.season, t.team_season_id, m.full_name
        FROM teams t JOIN managers m ON m.manager_id = t.manager_id
    """):
        out[(full_name, season)] = ts_id
    return out


def build_draft_picks_index(conn):
    """{(season, player_id, team_season_id): draft_round} for the years we care about."""
    out = {}
    for season, player_id, team_season_id, draft_round in conn.execute("""
        SELECT season, player_id, team_season_id, draft_round
        FROM draft_picks WHERE season IN (2024, 2025)
    """):
        out[(season, player_id, team_season_id)] = draft_round
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Actually insert overrides (default is dry-run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit dry-run flag; alias for default behavior.")
    args = parser.parse_args()

    if args.commit and args.dry_run:
        print("Specify either --commit OR --dry-run, not both.")
        sys.exit(1)
    do_commit = args.commit

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    # Sanity check: table must exist (run migrate_keeper_overrides.py first)
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='keeper_status_overrides'"
    ).fetchone()
    if not table_check:
        print("ERROR: keeper_status_overrides table does not exist. "
              "Run `python migrate_keeper_overrides.py` first.")
        sys.exit(1)

    # ----- Load DB indexes ------------------------------------------------
    player_idx = build_player_index(conn)
    team_idx = build_team_index(conn)
    draft_picks = build_draft_picks_index(conn)
    print(f"Loaded {sum(len(v) for v in player_idx.values())} players, "
          f"{len(team_idx)} team-season rows, "
          f"{len(draft_picks)} draft_picks for 2024/2025\n")

    # ----- Read Excel -----------------------------------------------------
    wb = openpyxl.load_workbook(EXCEL_FILE, data_only=True, read_only=True)
    ws = wb[SHEET_NAME]

    # ----- Plan inserts ---------------------------------------------------
    # First pass: walk Excel, record what player+manager+year says.
    # Build a map: (season, player_id, team_season_id) -> ('K', source_row) for keeper claims.
    keeper_claims = {}
    unmatched = []
    skipped_na = 0
    skipped_no_player = 0
    skipped_no_team = 0
    multi_match = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        player_name = row[COL_PLAYER]
        manager_name = row[COL_MANAGER]
        if not player_name:
            continue
        if not manager_name or manager_name == "#N/A":
            skipped_na += 1
            continue

        norm = normalize_player(player_name)
        candidates = player_idx.get(norm, [])
        if not candidates:
            unmatched.append((row_idx, player_name, manager_name, "player not found in DB"))
            skipped_no_player += 1
            continue
        if len(candidates) > 1:
            multi_match.append((row_idx, player_name, candidates))
            # Use the first; flag for review
        player_id = candidates[0]

        for season, k_col in ((2024, COL_2024_K), (2025, COL_2025_K)):
            ts_id = team_idx.get((manager_name, season))
            if ts_id is None:
                unmatched.append((row_idx, player_name, manager_name,
                                  f"no team for ({manager_name}, {season})"))
                skipped_no_team += 1
                continue

            is_keeper = 1 if row[k_col] == "K" else 0

            # Only insert overrides for rows that have a corresponding draft_picks row.
            # Otherwise we'd be claiming keeper-ness for a player who isn't even
            # in this year's draft slot — meaningless.
            if (season, player_id, ts_id) not in draft_picks:
                # Edge case: K=1 in Excel but no draft_picks. Worth flagging.
                if is_keeper:
                    unmatched.append((row_idx, player_name, manager_name,
                                      f"K=1 for {season} but no draft_picks row"))
                continue

            keeper_claims[(season, player_id, ts_id)] = (is_keeper, row_idx, player_name, manager_name)

    # ----- Summary --------------------------------------------------------
    by_season = defaultdict(lambda: Counter())
    for (season, _, _), (is_keeper, *_) in keeper_claims.items():
        by_season[season][is_keeper] += 1

    print("=" * 70)
    print("EXCEL → keeper_status_overrides ingest summary")
    print("=" * 70)
    print(f"Rows skipped: manager=#N/A     : {skipped_na}")
    print(f"Rows skipped: player not in DB : {skipped_no_player}")
    print(f"Rows skipped: no team mapping  : {skipped_no_team}")
    print(f"Multi-match warnings (using first hit): {len(multi_match)}")
    print(f"Unmatched rows logged          : {len(unmatched)}")
    print()
    print("Override rows planned per season:")
    for season in (2024, 2025):
        keep = by_season[season][1]
        fresh = by_season[season][0]
        print(f"  {season}: is_keeper=1: {keep:>3}   is_keeper=0: {fresh:>3}   total: {keep + fresh}")

    # Show first 10 of each direction per season as preview
    print()
    print("Preview — first 5 K=1 overrides (2024):")
    n = 0
    for (s, pid, ts), (k, row_idx, player_name, mgr) in keeper_claims.items():
        if s == 2024 and k == 1 and n < 5:
            n += 1
            print(f"  {player_name} ({mgr})")
    n = 0
    print("Preview — first 5 K=1 overrides (2025):")
    for (s, pid, ts), (k, row_idx, player_name, mgr) in keeper_claims.items():
        if s == 2025 and k == 1 and n < 5:
            n += 1
            print(f"  {player_name} ({mgr})")

    # Multi-match warnings detail
    if multi_match:
        print()
        print(f"Multi-match warnings (first 10 of {len(multi_match)}):")
        for row_idx, name, cands in multi_match[:10]:
            print(f"  Row {row_idx}: {name} → {len(cands)} candidates, using {cands[0]}")

    # Log unmatched to CSV
    if unmatched:
        with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["excel_row", "player_name", "manager_name", "reason"])
            for r in unmatched:
                w.writerow(r)
        print(f"\nUnmatched rows logged to {UNMATCHED_CSV} ({len(unmatched)} rows)")

    # ----- Commit ---------------------------------------------------------
    if not do_commit:
        print("\nDRY RUN — no rows inserted. Re-run with --commit to apply.")
        return

    print(f"\nCOMMITTING {len(keeper_claims)} override rows...")
    inserted = 0
    for (season, pid, ts), (is_keeper, _, player_name, mgr) in keeper_claims.items():
        note = f"Excel K flag {season}: {player_name} on {mgr}'s team"
        conn.execute("""
            INSERT OR REPLACE INTO keeper_status_overrides
              (season, player_id, team_season_id, is_keeper, source, note)
            VALUES (?, ?, ?, ?, 'excel_tracking_sheet', ?)
        """, (season, pid, ts, is_keeper, note))
        inserted += 1
    conn.commit()
    print(f"Inserted/replaced {inserted} rows in keeper_status_overrides.")


if __name__ == "__main__":
    main()
