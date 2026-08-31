"""remove_keeper_row.py - delete one committed keeper_selections row.

Generic replacement for the one-off fix_remove_*_keeper.py scripts: when
a late-reported trade (or a keep reversal) means a committed keeper row
no longer belongs to that team, this removes it. If the player is kept
by an acquirer, add them to TRADED_IN_KEEPERS in add_keeper_selections.py
and re-run it - that script re-adds them under the right team.

Run:  python remove_keeper_row.py --player "Rome Odunze" --manager "Alex Schlosberg"
      ... --commit  to apply (dry-run without it)
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", required=True)
    ap.add_argument("--manager", required=True)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT p.player_name, m.full_name, ks.drc, ks.drc_dollars, ks.source
        FROM keeper_selections ks
        JOIN players p ON p.player_id = ks.player_id
        JOIN teams t ON t.team_season_id = ks.team_season_id
        JOIN managers m ON m.manager_id = t.manager_id
        WHERE ks.season = ? AND p.player_name = ? AND m.full_name = ?
    """, (args.season, args.player, args.manager)).fetchall()
    if not rows:
        print(f"Nothing to do - no {args.player!r} / {args.manager!r} "
              f"keeper row for {args.season}. (Names must match the DB "
              f"exactly - check players/managers spelling.)")
        return
    for r in rows:
        print(f"  would delete: {r[0]} ({r[1]}) DRC {r[2]} ${r[3]} [{r[4]}]")
    if not args.commit:
        print("DRY RUN. Re-run with --commit to apply.")
        return
    conn.execute("""
        DELETE FROM keeper_selections
        WHERE season = ?
          AND player_id = (SELECT player_id FROM players WHERE player_name = ?)
          AND team_season_id = (SELECT t.team_season_id FROM teams t
                                JOIN managers m ON m.manager_id = t.manager_id
                                WHERE m.full_name = ? AND t.season = ?)
    """, (args.season, args.player, args.manager, args.season))
    conn.commit()
    print(f"Deleted. Re-run add_keeper_selections.py --commit if an "
          f"acquirer keeps {args.player}.")


if __name__ == "__main__":
    main()
