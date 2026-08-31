"""fix_remove_jacobs_keeper.py - remove Josh Jacobs from Tom's 2026 keepers.

Jacobs (traded to Tom at the 8/29 deadline) was entered as a keeper under
the every-traded-in-player-is-kept assumption; Pete corrected 2026-08-31:
Tom is NOT keeping him ($200 DRC 1). He goes to the draft pool. The
synthetic trade itself stays - only the keeper_selections row goes.

Run:  python fix_remove_jacobs_keeper.py            # dry-run
      python fix_remove_jacobs_keeper.py --commit   # delete the row
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT ks.rowid, p.player_name, m.full_name, ks.drc, ks.drc_dollars
        FROM keeper_selections ks
        JOIN players p ON p.player_id = ks.player_id
        JOIN teams t ON t.team_season_id = ks.team_season_id
        JOIN managers m ON m.manager_id = t.manager_id
        WHERE ks.season = 2026 AND p.player_name = 'Josh Jacobs'
          AND m.full_name = 'Tom Watson'
    """).fetchall()
    if not rows:
        print("Nothing to do - no Jacobs/Tom keeper row found.")
        return
    for r in rows:
        print(f"  would delete: {r[1]} ({r[2]}) DRC {r[3]} ${r[4]}")
    if not args.commit:
        print("DRY RUN. Re-run with --commit to apply.")
        return
    conn.execute("""
        DELETE FROM keeper_selections
        WHERE season = 2026 AND player_id =
              (SELECT player_id FROM players WHERE player_name = 'Josh Jacobs')
          AND team_season_id =
              (SELECT t.team_season_id FROM teams t
               JOIN managers m ON m.manager_id = t.manager_id
               WHERE m.full_name = 'Tom Watson' AND t.season = 2026)
    """)
    conn.commit()
    print("Deleted. Tom's committed total drops by $200.")


if __name__ == "__main__":
    main()
