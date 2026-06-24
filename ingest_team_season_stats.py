"""
ingest_team_season_stats.py — pull each team's season-level Yahoo stats:
number_of_moves (Yahoo's official add/drop count), number_of_trades, and
faab_balance (remaining FAAB). This powers the 'most/fewest moves' trivia with
Yahoo's own numbers, and gives an authoritative FAAB-spent figure
(spent = $100 budget - remaining balance).

Creates (IF NOT EXISTS) and populates team_season_stats. Idempotent upsert.

Run (needs the venv — uses yfpy + dotenv):
    .\\venv\\Scripts\\python.exe ingest_team_season_stats.py
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

DDL = """
CREATE TABLE IF NOT EXISTS team_season_stats (
    season           INTEGER NOT NULL,
    team_season_id   INTEGER NOT NULL,
    number_of_moves  INTEGER,
    number_of_trades INTEGER,
    faab_balance     INTEGER,
    fetched_at       DATETIME NOT NULL,
    PRIMARY KEY (season, team_season_id),
    FOREIGN KEY (season)         REFERENCES seasons(season),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id)
);
"""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def tsid(conn, season, team_key):
    if not team_key:
        return None
    yid = int(str(team_key).split(".")[-1])
    row = conn.execute(
        "SELECT team_season_id FROM teams WHERE season=? AND yahoo_team_id=?",
        (season, yid),
    ).fetchone()
    return row[0] if row else None


def main():
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(DDL)

    seasons = conn.execute(
        "SELECT season, nfl_game_id, yahoo_league_id FROM seasons ORDER BY season"
    ).fetchall()

    for season, gid, lid in seasons:
        print(f"\n--- Season {season} ---")
        query = YahooFantasySportsQuery(
            league_id=lid, game_code="nfl", game_id=gid,
            env_file_location=project_dir, save_token_data_to_env_file=True,
        )
        try:
            teams = query.get_league_teams()
        except Exception as e:
            print(f"  teams unavailable: {type(e).__name__}")
            continue

        n = 0
        for t in (teams or []):
            ts_id = tsid(conn, season, getattr(t, "team_key", None))
            if ts_id is None:
                continue
            conn.execute(
                """INSERT INTO team_season_stats
                   (season, team_season_id, number_of_moves, number_of_trades,
                    faab_balance, fetched_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(season, team_season_id) DO UPDATE SET
                     number_of_moves=excluded.number_of_moves,
                     number_of_trades=excluded.number_of_trades,
                     faab_balance=excluded.faab_balance,
                     fetched_at=excluded.fetched_at""",
                (season, ts_id,
                 _int(getattr(t, "number_of_moves", None)),
                 _int(getattr(t, "number_of_trades", None)),
                 _int(getattr(t, "faab_balance", None)),
                 now),
            )
            n += 1
        conn.commit()
        print(f"  team stats: {n} teams")

    print("\n=== counts ===")
    print("  team_season_stats:", conn.execute("SELECT COUNT(*) FROM team_season_stats").fetchone()[0])
    print("\n  2025 moves (top 3, for your eyeball):")
    for full, mv, tr in conn.execute(
        """SELECT m.full_name, s.number_of_moves, s.number_of_trades
           FROM team_season_stats s
           JOIN teams te ON te.team_season_id=s.team_season_id
           JOIN managers m ON m.manager_id=te.manager_id
           WHERE s.season=2025 ORDER BY s.number_of_moves DESC LIMIT 3"""):
        print(f"    {full}: {mv} moves, {tr} trades")
    conn.close()


if __name__ == "__main__":
    main()
