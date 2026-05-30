"""
ingest_player_weekly_stats.py - pull weekly fantasy points for every player
on every team for every week of a fantasy season. Points come back already
computed using the league's scoring rules (PPR/half/etc.) - we consume them
directly.

Strategy: for each season, for each team, for each week 1-17, fetch the
team's roster with stats. Yahoo returns player_points.total per player for
that week. Upserts to player_weekly_stats.

Usage:
    python ingest_player_weekly_stats.py 2024        # one season
    python ingest_player_weekly_stats.py all         # 2023, 2024, 2025
    python ingest_player_weekly_stats.py 2024 --weeks 1-5
                                                     # specific week range
    python ingest_player_weekly_stats.py 2024 --teams 1,3,7
                                                     # specific yahoo_team_ids

Volume: ~612 API calls per full backfill (12 teams * 17 weeks * 3 seasons).
With the default 1.0s polite delay, expect roughly 10-12 minutes per season.

Idempotent: re-runs upsert in place. Safe to interrupt and resume.
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

DEFAULT_WEEKS = list(range(1, 18))   # weeks 1-17, regular + playoffs


def decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def ensure_player(conn, player_id, name, position, nfl_team):
    """Insert player on first sight; refresh fields on every call."""
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, player_name, position, nfl_team) "
        "VALUES (?, ?, ?, ?)",
        (player_id, name, position, nfl_team),
    )
    conn.execute(
        "UPDATE players SET player_name=?, position=?, nfl_team=? WHERE player_id=?",
        (name, position, nfl_team, player_id),
    )


def extract_points(player_obj):
    """Yahoo exposes per-week points as player.player_points.total.
    Return as float or None if missing/non-numeric."""
    pp = getattr(player_obj, "player_points", None)
    if pp is None:
        return None
    total = getattr(pp, "total", None)
    if total is None:
        return None
    try:
        return float(total)
    except (TypeError, ValueError):
        return None


def upsert_weekly_row(conn, season, week, player_id, team_season_id, points, fetched_at):
    conn.execute(
        "INSERT INTO player_weekly_stats "
        "(season, week, player_id, team_season_id, fantasy_points, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(season, week, player_id) DO UPDATE SET "
        "  team_season_id = excluded.team_season_id, "
        "  fantasy_points = excluded.fantasy_points, "
        "  fetched_at     = excluded.fetched_at",
        (season, week, player_id, team_season_id, points, fetched_at),
    )


def ingest_team_week(query, conn, season, team_season_id, yahoo_team_id, week, fetched_at):
    """Fetch one team's roster with weekly stats and upsert into the table.
    Returns the number of player-rows touched."""
    try:
        result = query.get_team_roster_player_stats_by_week(yahoo_team_id, week)
    except Exception as e:
        print(f"    week {week:>2}: FAILED {type(e).__name__}: {e}")
        return 0

    # yfpy 17: this method returns the player list directly (NOT wrapped in
    # a roster object with a .players attribute). Other roster methods do
    # wrap it; this one doesn't.
    if isinstance(result, list):
        players = result
    else:
        players = getattr(result, "players", None) or []
    rows = 0
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

        points = extract_points(p)
        upsert_weekly_row(
            conn, season, week, player_id, team_season_id, points, fetched_at
        )
        rows += 1
    return rows


def ingest_season(conn, season, weeks, team_filter, polite_delay):
    row = conn.execute(
        "SELECT nfl_game_id, yahoo_league_id FROM seasons WHERE season = ?",
        (season,),
    ).fetchone()
    if not row:
        print(f"!! no seasons row for {season}")
        return 0
    nfl_game_id, yahoo_league_id = row

    sql = "SELECT team_season_id, yahoo_team_id, team_name FROM teams WHERE season = ?"
    params = [season]
    if team_filter:
        placeholders = ",".join("?" * len(team_filter))
        sql += f" AND yahoo_team_id IN ({placeholders})"
        params.extend(team_filter)
    sql += " ORDER BY yahoo_team_id"
    teams = conn.execute(sql, params).fetchall()
    print(f"\n=== Season {season}: {len(teams)} teams x {len(weeks)} weeks "
          f"= {len(teams)*len(weeks)} API calls ===")

    query = YahooFantasySportsQuery(
        league_id=yahoo_league_id,
        game_code="nfl",
        game_id=nfl_game_id,
        env_file_location=project_dir,
        save_token_data_to_env_file=True,
    )

    total_rows = 0
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for ts_id, yahoo_team_id, team_name in teams:
        print(f"\n  {team_name} (yahoo_team_id={yahoo_team_id})")
        team_rows = 0
        for w_idx, week in enumerate(weeks):
            n = ingest_team_week(query, conn, season, ts_id, yahoo_team_id, week, fetched_at)
            team_rows += n
            if n:
                print(f"    week {week:>2}: {n} players")
            conn.commit()  # commit after each team-week so a crash doesn't lose work
            if polite_delay and (w_idx + 1) < len(weeks):
                time.sleep(polite_delay)
        print(f"    -> {team_rows} player-week rows")
        total_rows += team_rows

    return total_rows


def parse_weeks(spec):
    """'1-5' -> [1,2,3,4,5], '1,3,7' -> [1,3,7], '' -> all weeks."""
    if not spec:
        return DEFAULT_WEEKS
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return sorted(set(out))


def parse_teams(spec):
    if not spec:
        return None
    return [int(x.strip()) for x in spec.split(",") if x.strip()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("season", help="Season (2023|2024|2025) or 'all'")
    p.add_argument("--weeks", default="",
                   help="Week filter, e.g. '1-5' or '1,3,7' (default: 1-17)")
    p.add_argument("--teams", default="",
                   help="yahoo_team_id filter, e.g. '1,3,7' (default: all)")
    p.add_argument("--polite-delay", type=float, default=1.0,
                   help="Seconds between API calls (default 1.0)")
    args = p.parse_args()

    weeks = parse_weeks(args.weeks)
    team_filter = parse_teams(args.teams)

    if args.season == "all":
        seasons = [2023, 2024, 2025]
    else:
        try:
            seasons = [int(args.season)]
        except ValueError:
            sys.exit(f"Invalid season: {args.season}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    grand_total = 0
    for season in seasons:
        n = ingest_season(conn, season, weeks, team_filter, args.polite_delay)
        grand_total += n

    conn.close()
    print(f"\n=== Done. Total player-week rows touched: {grand_total} ===")


if __name__ == "__main__":
    main()
