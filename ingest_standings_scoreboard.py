"""
ingest_standings_scoreboard.py — pull final standings + weekly matchup scores
from Yahoo for each season in the seasons table.

Creates (IF NOT EXISTS) and populates:
- team_standings : one row per (season, team) — final rank, playoff seed,
                   W/L/T, points for / against.
- matchups       : one row per (season, week, team) — points, opponent,
                   opponent points, playoff/consolation flags, win flag.
                   (Two rows per head-to-head matchup, one per team.)

Idempotent: upserts on the primary keys, so it's safe to re-run. Offseason /
pre-draft seasons that have no schedule yet are skipped gracefully (yfpy raises
rather than returning empty).

Run:  python ingest_standings_scoreboard.py
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
CREATE TABLE IF NOT EXISTS team_standings (
    season          INTEGER NOT NULL,
    team_season_id  INTEGER NOT NULL,
    rank            INTEGER,
    playoff_seed    INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    ties            INTEGER,
    points_for      REAL,
    points_against  REAL,
    fetched_at      DATETIME NOT NULL,
    PRIMARY KEY (season, team_season_id),
    FOREIGN KEY (season)         REFERENCES seasons(season),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    season                  INTEGER NOT NULL,
    week                    INTEGER NOT NULL,
    team_season_id          INTEGER NOT NULL,
    points                  REAL,
    opponent_team_season_id INTEGER,
    opponent_points         REAL,
    is_playoffs             INTEGER NOT NULL DEFAULT 0,
    is_consolation          INTEGER NOT NULL DEFAULT 0,
    is_winner               INTEGER,
    fetched_at              DATETIME NOT NULL,
    PRIMARY KEY (season, week, team_season_id),
    FOREIGN KEY (season)                  REFERENCES seasons(season),
    FOREIGN KEY (team_season_id)          REFERENCES teams(team_season_id),
    FOREIGN KEY (opponent_team_season_id) REFERENCES teams(team_season_id)
);
"""


def decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tsid(conn, season, team_key):
    """Yahoo team_key '461.l.48079.t.5' -> our team_season_id."""
    if not team_key:
        return None
    yahoo_team_id = int(str(team_key).split(".")[-1])
    row = conn.execute(
        "SELECT team_season_id FROM teams WHERE season=? AND yahoo_team_id=?",
        (season, yahoo_team_id),
    ).fetchone()
    return row[0] if row else None


def ingest_standings(conn, query, season, now):
    standings = query.get_league_standings()
    teams = getattr(standings, "teams", None) or []
    n = 0
    for t in teams:
        ts = getattr(t, "team_standings", None)
        ot = getattr(ts, "outcome_totals", None) if ts else None
        team_season_id = tsid(conn, season, getattr(t, "team_key", None))
        if team_season_id is None:
            print(f"    WARN: standings team_key {getattr(t,'team_key',None)} not mapped")
            continue
        conn.execute(
            """INSERT INTO team_standings
               (season, team_season_id, rank, playoff_seed, wins, losses, ties,
                points_for, points_against, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(season, team_season_id) DO UPDATE SET
                 rank=excluded.rank, playoff_seed=excluded.playoff_seed,
                 wins=excluded.wins, losses=excluded.losses, ties=excluded.ties,
                 points_for=excluded.points_for,
                 points_against=excluded.points_against,
                 fetched_at=excluded.fetched_at""",
            (season, team_season_id,
             _int(getattr(ts, "rank", None)),
             _int(getattr(ts, "playoff_seed", None)),
             _int(getattr(ot, "wins", None)),
             _int(getattr(ot, "losses", None)),
             _int(getattr(ot, "ties", None)),
             _float(getattr(ts, "points_for", None)),
             _float(getattr(ts, "points_against", None)),
             now),
        )
        n += 1
    return n


def ingest_week(conn, query, season, week, now):
    matchups = query.get_league_matchups_by_week(week)
    n = 0
    for m in (matchups or []):
        mteams = getattr(m, "teams", None) or []
        if len(mteams) < 2:
            continue
        is_playoffs = _int(getattr(m, "is_playoffs", 0)) or 0
        is_consolation = _int(getattr(m, "is_consolation", 0)) or 0
        winner_key = getattr(m, "winner_team_key", None)
        info = []
        for mt in mteams:
            tk = getattr(mt, "team_key", None)
            tp = getattr(mt, "team_points", None)
            pts = _float(getattr(tp, "total", None)) if tp else None
            info.append((tk, pts))
        for i, (tk, pts) in enumerate(info):
            opp_tk, opp_pts = info[1 - i]
            team_season_id = tsid(conn, season, tk)
            if team_season_id is None:
                continue
            is_winner = (1 if tk == winner_key else 0) if winner_key else None
            conn.execute(
                """INSERT INTO matchups
                   (season, week, team_season_id, points, opponent_team_season_id,
                    opponent_points, is_playoffs, is_consolation, is_winner, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(season, week, team_season_id) DO UPDATE SET
                     points=excluded.points,
                     opponent_team_season_id=excluded.opponent_team_season_id,
                     opponent_points=excluded.opponent_points,
                     is_playoffs=excluded.is_playoffs,
                     is_consolation=excluded.is_consolation,
                     is_winner=excluded.is_winner,
                     fetched_at=excluded.fetched_at""",
                (season, week, team_season_id, pts, tsid(conn, season, opp_tk),
                 opp_pts, is_playoffs, is_consolation, is_winner, now),
            )
            n += 1
    return n


def main():
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(DDL)

    seasons = conn.execute(
        "SELECT season, nfl_game_id, yahoo_league_id FROM seasons ORDER BY season"
    ).fetchall()

    for season, nfl_game_id, yahoo_league_id in seasons:
        print(f"\n--- Season {season} ---")
        query = YahooFantasySportsQuery(
            league_id=yahoo_league_id,
            game_code="nfl",
            game_id=nfl_game_id,
            env_file_location=project_dir,
            save_token_data_to_env_file=True,
        )

        try:
            ns = ingest_standings(conn, query, season, now)
            print(f"  standings: {ns} teams")
        except Exception as e:
            print(f"  standings unavailable: {type(e).__name__}: {e}")

        # Figure out how many weeks to pull; fall back to 17.
        end_week = 17
        try:
            info = query.get_league_info()
            ew = _int(getattr(info, "end_week", None))
            if ew:
                end_week = ew
        except Exception:
            pass

        total = 0
        for week in range(1, end_week + 1):
            try:
                total += ingest_week(conn, query, season, week, now)
            except Exception as e:
                print(f"  week {week} skipped: {type(e).__name__}")
        print(f"  matchups: {total} team-week rows (weeks 1-{end_week})")
        conn.commit()

    print("\n=== counts ===")
    for table in ("team_standings", "matchups"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<16} {n}")
    conn.close()


if __name__ == "__main__":
    main()
