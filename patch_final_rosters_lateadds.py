"""Patch final_rosters: add post-draft pickups that Yahoo's week-17 snapshot missed.

League ruling (Pete, 2026-06-11): pickups made after the final scoring week
still count as rostered players heading into the next keeper window.
Yahoo's final-roster snapshot freezes at the last scoring week, so e.g.
Brian's Dec 31 Malik Willis add is missing from final_rosters — and therefore
from his team page and cap math.

Selection rule (derived, not hardcoded, so this works in future seasons):
  - player's most recent transaction in SEASON is INCOMING (added, never
    dropped afterward)
  - that add happened after Sept 1 (i.e., post-draft, so it can't be a
    pre-draft add that Yahoo silently released at the draft reset)
  - player has no final_rosters row for SEASON

Default is a DRY RUN that prints what would be inserted.
Run with --apply to actually insert (selected_position='BN', is_keeper_yahoo=0).
Idempotent: re-running skips rows that already exist.
"""
import sqlite3
import sys
from pathlib import Path

SEASON = 2025
HERE = Path(__file__).parent
APPLY = "--apply" in sys.argv

conn = sqlite3.connect(HERE / "fantasy.db")
conn.row_factory = sqlite3.Row

candidates = conn.execute("""
    WITH last_event AS (
        SELECT tp.player_id,
               tp.team_season_id,
               tp.direction,
               t.timestamp,
               ROW_NUMBER() OVER (PARTITION BY tp.player_id
                                  ORDER BY t.timestamp DESC) AS rn
        FROM all_transactions t
        JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id
        WHERE t.season = :season
    )
    SELECT le.player_id, le.team_season_id, le.timestamp,
           p.player_name, p.position, tm.team_name, m.full_name AS manager
    FROM last_event le
    JOIN players p  ON p.player_id = le.player_id
    JOIN teams tm   ON tm.team_season_id = le.team_season_id
    JOIN managers m ON m.manager_id = tm.manager_id
    WHERE le.rn = 1
      AND le.direction = 'incoming'
      AND le.timestamp >= :postdraft
      AND le.player_id NOT IN (
            SELECT player_id FROM final_rosters WHERE season = :season)
    ORDER BY m.full_name, p.player_name
""", {"season": SEASON, "postdraft": f"{SEASON}-09-01 00:00:00"}).fetchall()

if not candidates:
    print(f"Nothing to patch — final_rosters {SEASON} already covers all post-draft adds.")
    sys.exit(0)

print(f"{'WILL INSERT' if APPLY else 'DRY RUN — would insert'} {len(candidates)} row(s) into final_rosters ({SEASON}):\n")
for c in candidates:
    print(f"  {c['manager']:<18} {c['player_name']:<24} {c['position'] or '?':<3} "
          f"added {str(c['timestamp'])[:10]}  ({c['team_name']})")

if APPLY:
    conn.executemany(
        "INSERT INTO final_rosters (season, team_season_id, player_id, "
        "selected_position, is_keeper_yahoo) VALUES (?, ?, ?, 'BN', 0)",
        [(SEASON, c["team_season_id"], c["player_id"]) for c in candidates],
    )
    conn.commit()
    print(f"\nInserted {len(candidates)} row(s). Re-run recon_ownership.py to confirm, "
          "then regenerate the dashboard.")
else:
    print("\nReview the list. If it matches the recon report's A1 bucket "
          "(minus Drew Lock), run:  python patch_final_rosters_lateadds.py --apply")
