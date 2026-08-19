"""patch_final_rosters_latedrops.py - remove final_rosters rows for players who
were dropped AFTER the week-17 snapshot.

final_rosters is Yahoo's week-17 snapshot. A player streamed in during the final
scoring week and then dropped in the post-season (e.g. Dec 31) stays in the
snapshot, but is NOT on the roster going into the next keeper window. The
transaction walk (player_history.get_owner_at_year_end) already knows they're
gone; this patch aligns final_rosters with it. It's the mirror image of
patch_final_rosters_lateadds.py, which ADDS post-snapshot pickups.

SAFETY GATE: a final_rosters row is removed only when BOTH are true:
  - get_owner_at_year_end(player, 2025) disagrees with the roster's team, AND
  - the player's LAST 2025 transaction is an outgoing drop (they explicitly left).
Anything where the walk disagrees but the last event is not a clean drop is
SKIPPED and printed for manual review, never auto-removed.

    python patch_final_rosters_latedrops.py            # dry-run (shows the plan)
    python patch_final_rosters_latedrops.py --commit   # actually remove
"""
import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import player_history as ph  # noqa: E402  (the dashboard's own owner logic)

SEASON = 2025


def last_2025_direction(conn, player_id):
    row = conn.execute(
        "SELECT tp.direction FROM all_transactions t "
        "JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        "WHERE tp.player_id = ? AND t.season = ? "
        "ORDER BY t.timestamp DESC, tp.direction LIMIT 1",
        (player_id, SEASON),
    ).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually remove (default is a dry-run).")
    args = ap.parse_args()

    conn = sqlite3.connect(HERE / "fantasy.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    teams = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT t.team_season_id, m.full_name FROM teams t "
            "JOIN managers m ON m.manager_id = t.manager_id WHERE t.season = ?",
            (SEASON,),
        )
    }

    rows = conn.execute(
        "SELECT fr.player_id, fr.team_season_id, p.player_name, p.position "
        "FROM final_rosters fr JOIN players p ON p.player_id = fr.player_id "
        "WHERE fr.season = ?",
        (SEASON,),
    ).fetchall()

    to_remove = []
    for pid, team, name, pos in rows:
        owner = ph.get_owner_at_year_end(conn, pid, SEASON)
        if owner == team:
            continue  # snapshot agrees with the walk -> keep
        direction = last_2025_direction(conn, pid)
        walk = "free agent" if owner is None else teams.get(owner, str(owner))
        if direction == "outgoing":
            to_remove.append((pid, team))
            print(f"  REMOVE  {teams.get(team, team):<17}{name:<24}{pos or '?':<4}"
                  f"dropped post-snapshot; now {walk}")
        else:
            print(f"  SKIP    {teams.get(team, team):<17}{name:<24}{pos or '?':<4}"
                  f"walk disagrees ({walk}) but last event not a drop -- review")

    print(f"\n=== Plan: remove {len(to_remove)} final_rosters row(s) ===")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    if not to_remove:
        print("\nNothing to remove. Done.")
        return

    for pid, team in to_remove:
        conn.execute(
            "DELETE FROM final_rosters WHERE season = ? AND player_id = ? AND team_season_id = ?",
            (SEASON, pid, team),
        )
    conn.commit()
    print(f"\nRemoved {len(to_remove)} row(s). Done.")


if __name__ == "__main__":
    main()
