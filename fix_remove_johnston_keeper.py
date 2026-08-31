"""fix_remove_johnston_keeper.py - move Quentin Johnston out of Greg's keepers.

Johnston was in Greg's committed 2026 keeper set, but the missed 8/29
trade (reported 2026-08-31) sent him to Scott for Scott's own R7 (7.05).
This deletes Greg's keeper_selections row; add_keeper_selections.py
re-adds him under Scott (trade addendum, frozen DRC 15 / $10).

Run:  python fix_remove_johnston_keeper.py            # dry-run
      python fix_remove_johnston_keeper.py --commit   # delete the row
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
        SELECT p.player_name, m.full_name, ks.drc, ks.drc_dollars
        FROM keeper_selections ks
        JOIN players p ON p.player_id = ks.player_id
        JOIN teams t ON t.team_season_id = ks.team_season_id
        JOIN managers m ON m.manager_id = t.manager_id
        WHERE ks.season = 2026 AND p.player_name = 'Quentin Johnston'
          AND m.full_name = 'Greg Pearson'
    """).fetchall()
    if not rows:
        print("Nothing to do - no Johnston/Greg keeper row found.")
        return
    for r in rows:
        print(f"  would delete: {r[0]} ({r[1]}) DRC {r[2]} ${r[3]}")
    if not args.commit:
        print("DRY RUN. Re-run with --commit to apply.")
        return
    conn.execute("""
        DELETE FROM keeper_selections
        WHERE season = 2026
          AND player_id = (SELECT player_id FROM players
                           WHERE player_name = 'Quentin Johnston')
          AND team_season_id = (SELECT t.team_season_id FROM teams t
                                JOIN managers m ON m.manager_id = t.manager_id
                                WHERE m.full_name = 'Greg Pearson'
                                  AND t.season = 2026)
    """)
    conn.commit()
    print("Deleted. Greg's committed total drops by $10; "
          "add_keeper_selections.py --commit re-adds Johnston under Scott.")


if __name__ == "__main__":
    main()
