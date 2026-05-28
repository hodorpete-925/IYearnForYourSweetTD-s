"""
ingest_teams.py — pull teams and managers from Yahoo for each season in the
seasons table, populate managers and teams tables.

Idempotent: safe to re-run. Updates nicknames and team names if they've changed.
"""

import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"


def decode(v):
    """yfpy returns some text fields as bytes; decode for storage."""
    return v.decode("utf-8") if isinstance(v, bytes) else v


def get_or_create_manager(conn, guid, nickname):
    """Insert manager on first sight; refresh nickname. Return manager_id."""
    conn.execute(
        "INSERT OR IGNORE INTO managers (yahoo_guid, nickname) VALUES (?, ?)",
        (guid, nickname),
    )
    conn.execute(
        "UPDATE managers SET nickname = ? WHERE yahoo_guid = ?",
        (nickname, guid),
    )
    row = conn.execute(
        "SELECT manager_id FROM managers WHERE yahoo_guid = ?", (guid,)
    ).fetchone()
    return row[0]


def upsert_team(conn, season, yahoo_team_id, team_name, manager_id):
    """Insert team-season row; update name/manager if changed."""
    conn.execute(
        """INSERT OR IGNORE INTO teams
           (season, yahoo_team_id, team_name, manager_id)
           VALUES (?, ?, ?, ?)""",
        (season, yahoo_team_id, team_name, manager_id),
    )
    conn.execute(
        """UPDATE teams
           SET team_name = ?, manager_id = ?
           WHERE season = ? AND yahoo_team_id = ?""",
        (team_name, manager_id, season, yahoo_team_id),
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    seasons = conn.execute(
        "SELECT season, nfl_game_id, yahoo_league_id FROM seasons ORDER BY season"
    ).fetchall()

    for season, nfl_game_id, yahoo_league_id in seasons:
        print(f"\n--- Season {season} (game_id={nfl_game_id}, league_id={yahoo_league_id}) ---")

        query = YahooFantasySportsQuery(
            league_id=yahoo_league_id,
            game_code="nfl",
            game_id=nfl_game_id,
            env_file_location=project_dir,
            save_token_data_to_env_file=True,
        )

        teams = query.get_league_teams()
        print(f"  Pulled {len(teams)} teams from Yahoo")

        for t in teams:
            yahoo_team_id = t.team_id
            team_name = decode(t.name)

            m_wrapper = t.managers[0]
            m = m_wrapper.manager if hasattr(m_wrapper, "manager") else m_wrapper
            guid = m.guid
            nickname = decode(m.nickname)

            manager_id = get_or_create_manager(conn, guid, nickname)
            upsert_team(conn, season, yahoo_team_id, team_name, manager_id)

    conn.commit()

    n_managers = conn.execute("SELECT COUNT(*) FROM managers").fetchone()[0]
    n_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    print(f"\n=== Summary ===")
    print(f"  managers table: {n_managers} rows")
    print(f"  teams table: {n_teams} rows")

    conn.close()


if __name__ == "__main__":
    main()