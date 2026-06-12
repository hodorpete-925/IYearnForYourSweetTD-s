"""Load 2026 superflex (2-QB) ADP from a FantasyPros CSV export.

Why a CSV instead of the API/scraper: FFC's 2-QB feed has no 2026 data yet
(June), and FantasyPros' superflex page already does. Pete downloads the
export from FantasyPros -> NFL -> ADP -> Superflex (2QB) Overall and saves
it as adp_2026_2qb_fantasypros.csv in this folder; this script loads it.

Format notes (as of the June 2026 export):
  - Columns: Position, Overall, Player, "Team (Bye)", Avg Pick, High, Low, ...
  - "Avg Pick" is in round.pick format with an unstated league size, so it's
    ambiguous. We store the ADP value as the *Overall* rank instead —
    unambiguous, monotonic, and exactly what the DRC-vs-ADP value tags need.
  - The Team (Bye) cell contains a non-UTF8 spacer character; we read with
    cp1252 fallback and parse it with a regex.

Sanity gate: refuses to load unless the top 5 contains at least 3 QBs —
that's the signature of a real 2-QB/superflex board. (A 1-QB export would
quietly reintroduce the exact distortion this project just removed.)

Default is a dry run. Use --apply to replace ALL season-2026 adp rows.
After applying:  python match_adp_players.py  -> review adp_unmatched.csv
"""
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
CSV_PATH = HERE / "adp_2026_2qb_fantasypros.csv"
DB_PATH = HERE / "fantasy.db"
SEASON = 2026
SOURCE = "fantasypros_superflex"
APPLY = "--apply" in sys.argv

POS_MAP = {"DST": "DEF", "PK": "K"}


def read_rows():
    raw = CSV_PATH.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    rows = []
    for rec in csv.DictReader(text.splitlines()):
        player = (rec.get("Player") or "").strip()
        overall = (rec.get("Overall") or "").strip()
        if not player or not overall.isdigit():
            continue
        pos_rank_raw = (rec.get("Position") or "").strip()      # e.g. "QB1"
        m = re.match(r"([A-Za-z]+)(\d*)", pos_rank_raw)
        pos = POS_MAP.get(m.group(1).upper(), m.group(1).upper()) if m else None
        pos_rank = f"{pos}{m.group(2)}" if (m and m.group(2)) else pos

        team_bye = (rec.get("Team (Bye)") or "")
        tm = re.search(r"\b([A-Z]{2,3})\b", team_bye)
        bm = re.search(r"\((\d+)\)", team_bye)
        rows.append({
            "name": player,
            "overall_rank": int(overall),
            "pos": pos,
            "pos_rank": pos_rank,
            "nfl_team": tm.group(1) if tm else None,
            "bye_week": int(bm.group(1)) if bm else None,
            "adp": float(overall),   # Overall rank as ADP (see format notes)
        })
    return sorted(rows, key=lambda r: r["overall_rank"])


def main():
    if not CSV_PATH.exists():
        sys.exit(f"Missing {CSV_PATH.name} — download the FantasyPros "
                 "Superflex (2QB) Overall ADP export and save it under that name.")
    rows = read_rows()
    if len(rows) < 150:
        sys.exit(f"Only parsed {len(rows)} rows — that's suspiciously few. "
                 "Check the export; not loading.")
    top5 = rows[:5]
    qb_in_top5 = sum(1 for r in top5 if r["pos"] == "QB")
    print(f"Parsed {len(rows)} rows. Top 5: "
          + ", ".join(f"{r['name']} ({r['pos_rank']})" for r in top5))
    if qb_in_top5 < 3:
        sys.exit(f"Only {qb_in_top5} QBs in the top 5 — this does NOT look "
                 "like a 2-QB/superflex board. Wrong export? Not loading.")
    print(f"2-QB sanity check passed ({qb_in_top5} QBs in top 5).")

    if not APPLY:
        print("\nDry run only. To replace all season-2026 ADP rows, run:")
        print("  python ingest_adp_2026_csv.py --apply")
        return

    conn = sqlite3.connect(DB_PATH)
    deleted = conn.execute("DELETE FROM adp WHERE season=?", (SEASON,)).rowcount
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT INTO adp (season, source, player_name_raw, overall_rank, "
        " position_rank, nfl_team, bye_week, adp, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(SEASON, SOURCE, r["name"], r["overall_rank"], r["pos_rank"],
          r["nfl_team"], r["bye_week"], r["adp"], fetched_at) for r in rows],
    )
    conn.commit()
    print(f"\nReplaced {deleted} old season-{SEASON} rows with {len(rows)} "
          f"superflex rows (source='{SOURCE}').")
    print("Next: python match_adp_players.py   (then review adp_unmatched.csv)")


if __name__ == "__main__":
    main()
