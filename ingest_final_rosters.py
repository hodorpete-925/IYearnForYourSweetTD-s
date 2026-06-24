"""
ingest_final_rosters.py — pull end-of-2025 rosters for all 12 teams (week 17)
and store as the snapshot anchor for Phase B's backward-walk DRC computation.

Idempotent: safe to re-run.
"""

import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

# Final week of the 2025 fantasy season (confirmed via inspect_roster.py)
FINAL_WEEK_2025 = 17
SEASON = 2025


def decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def ensure_player(conn, player_id, name, position, nfl_team):
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, player_name, position, nfl_team) VALUES (?, ?, ?, ?)",
        (player_id, name, position, nfl_team),
    )
    conn.execute(
        "UPDATE players SET player_name=?, position=?, nfl_team=? WHERE player_id=?",
        (name, position, nfl_team, player_id),
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    row = conn.execute(
        "SELECT nfl_game_id, yahoo_league_id FROM seasons WHERE season = ?",
        (SEASON,),
    ).fetchone()
    nfl_game_id, yahoo_league_id = row

    teams = conn.execute(
        """SELECT t.team_season_id, t.yahoo_team_id, t.team_name
           FROM teams t WHERE t.season = ? ORDER BY t.yahoo_team_id""",
        (SEASON,),
    ).fetchall()
    print(f"Found {len(teams)} teams for {SEASON}.\n")

    query = YahooFantasySportsQuery(
        league_id=yahoo_league_id,
        game_code="nfl",
        game_id=nfl_game_id,
        env_file_location=project_dir,
        save_token_data_to_env_file=True,
    )

    total_rows = 0
    for team_season_id, yahoo_team_id, team_name in teams:
        print(f"--- {team_name} (team_id={yahoo_team_id}) ---")
        try:
            roster = query.get_team_roster_by_week(yahoo_team_id, FINAL_WEEK_2025)
        except Exception as e:
            print(f"  failed: {type(e).__name__}: {e}")
            continue

        players = getattr(roster, "players", None) or []
        print(f"  Found {len(players)} players")

        for p_wrapper in players:
            p = p_wrapper.player if hasattr(p_wrapper, "player") else p_wrapper

            player_id = p.player_id
            full_name = p.name.full if hasattr(p.name, "full") else p.name
            ensure_player(
                conn,
                player_id,
                decode(full_name),
                decode(getattr(p, "display_position", None)),
                decode(getattr(p, "editorial_team_abbr", None)),
            )

            sel = getattr(p, "selected_position", None)
            selected_position = decode(getattr(sel, "position", None)) if sel else None

# is_keeper is a dict, not an object — use dict access. cost is
            # meaningless data we ignore (Pete only entered it in year 1).
            keeper = getattr(p, "is_keeper", None) or {}
            is_keeper_yahoo = 1 if keeper.get("kept", False) else 0

            conn.execute(
                """INSERT OR IGNORE INTO final_rosters
                   (season, team_season_id, player_id, selected_position,
                    is_keeper_yahoo)
                   VALUES (?, ?, ?, ?, ?)""",
                (SEASON, team_season_id, player_id, selected_position,
                 is_keeper_yahoo),
            )
            # Refresh on subsequent runs (selected_position and keeper status
            # might have changed since last ingest).
            conn.execute(
                """UPDATE final_rosters
                   SET selected_position = ?, is_keeper_yahoo = ?
                   WHERE season = ? AND team_season_id = ? AND player_id = ?""",
                (selected_position, is_keeper_yahoo,
                 SEASON, team_season_id, player_id),
            )
            total_rows += 1

    conn.commit()

    print(f"\n=== Summary ===")
    print(f"  Inserted/processed {total_rows} player-roster rows")
    n = conn.execute("SELECT COUNT(*) FROM final_rosters WHERE season = ?", (SEASON,)).fetchone()[0]
    print(f"  final_rosters table: {n} rows for season {SEASON}")

    conn.close()


if __name__ == "__main__":
    main()