"""
ingest_drafts.py — pull draft results from Yahoo for each season and populate
the draft_picks table. Also opportunistically ensures players exist.

Idempotent: safe to re-run.
"""

import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

# Number of teams per season, needed to compute pick_in_round.
# 2026 is 11 (Lewitus replacement TBD); 2023-2025 were 12-team.
TEAM_COUNT_PER_SEASON = {2023: 12, 2024: 12, 2025: 12, 2026: 11}


def decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def lookup_team_season_id(conn, team_key, season):
    if not team_key:
        return None
    yahoo_team_id = int(team_key.split('.')[-1])
    row = conn.execute(
        "SELECT team_season_id FROM teams WHERE season=? AND yahoo_team_id=?",
        (season, yahoo_team_id),
    ).fetchone()
    return row[0] if row else None


def parse_player_id(player_key):
    """Convert player_key like '461.p.30123' → integer 30123."""
    return int(player_key.split('.')[-1])


def ensure_player(conn, player_id, fallback_name):
    """If the player isn't already in the table, insert with a fallback name.
    Doesn't overwrite existing data — keeps richer info from transaction ingestion."""
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, player_name) VALUES (?, ?)",
        (player_id, fallback_name),
    )


def ingest_pick(conn, d, season, team_count):
    """Insert one draft_pick row."""
    overall_pick = d.pick
    draft_round = d.round
    pick_in_round = ((overall_pick - 1) % team_count) + 1

    team_season_id = lookup_team_season_id(conn, d.team_key, season)
    player_id = parse_player_id(d.player_key)

    # Try a few likely yfpy paths for the player's name; fall back to placeholder.
    player_name = None
    if hasattr(d, 'player_name'):
        player_name = decode(d.player_name)
    fallback = player_name if player_name else f"Player {player_id}"
    ensure_player(conn, player_id, fallback)

    # is_keeper: presence/truthiness of cost or keeper_status varies by yfpy version
    is_keeper = 1 if getattr(d, 'cost', None) or getattr(d, 'keeper_status', None) else 0

    conn.execute(
        """INSERT OR IGNORE INTO draft_picks
           (season, overall_pick, draft_round, pick_in_round,
            team_season_id, player_id, is_keeper)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (season, overall_pick, draft_round, pick_in_round,
         team_season_id, player_id, is_keeper),
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

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
            draft_results = query.get_league_draft_results()
        except Exception as e:
            print(f"  No draft results: {type(e).__name__}")
            continue

        print(f"  Pulled {len(draft_results)} picks from Yahoo")

        team_count = TEAM_COUNT_PER_SEASON.get(season, 12)
        ingested = errors = 0

        for d in draft_results:
            try:
                if not d.player_key:
                    # Pre-draft empty slot (2026 has these — draft order is set,
                    # no players picked yet). Skip; nothing useful to store.
                    continue
                ingest_pick(conn, d, season, team_count)
                ingested += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR on pick {getattr(d, 'pick', '?')}: {type(e).__name__}: {e}")

        conn.commit()
        print(f"  Ingested {ingested}, {errors} errors")

    print(f"\n=== draft_picks count by season ===")
    rows = conn.execute(
        "SELECT season, COUNT(*) FROM draft_picks GROUP BY season ORDER BY season"
    ).fetchall()
    for row in rows:
        print(f"  {row[0]}: {row[1]} picks")

    conn.close()


if __name__ == "__main__":
    main()