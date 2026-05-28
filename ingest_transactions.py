"""
ingest_transactions.py — pull league transactions from Yahoo for each season
in the seasons table. Populates:
- transactions: one row per non-commish transaction
- transaction_players: one row per player movement
- transaction_picks: one row per draft-pick movement (trades only)
- players: opportunistically, as we encounter player_ids

Filters out commish events (administrative no-ops, no roster impact).
Idempotent: safe to re-run.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"


def decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def ensure_player(conn, player_id, name, position, nfl_team):
    """Insert player on first sight; refresh name/position/team to latest."""
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, player_name, position, nfl_team) VALUES (?, ?, ?, ?)",
        (player_id, name, position, nfl_team),
    )
    conn.execute(
        "UPDATE players SET player_name=?, position=?, nfl_team=? WHERE player_id=?",
        (name, position, nfl_team, player_id),
    )


def lookup_team_season_id(conn, team_key, season):
    """Convert a Yahoo team_key like '461.l.48079.t.5' to our team_season_id PK."""
    if not team_key:
        return None
    yahoo_team_id = int(team_key.split('.')[-1])
    row = conn.execute(
        "SELECT team_season_id FROM teams WHERE season=? AND yahoo_team_id=?",
        (season, yahoo_team_id),
    ).fetchone()
    return row[0] if row else None


def ingest_transaction(conn, t, season):
    """Insert one transaction + its players + its picks (commish events typically
    have neither, but we ingest them as bare rows for the audit trail)."""
    timestamp_iso = datetime.fromtimestamp(int(t.timestamp)).isoformat(sep=' ')
    conn.execute(
        """INSERT OR IGNORE INTO transactions
           (yahoo_transaction_id, season, timestamp, event_type, status)
           VALUES (?, ?, ?, ?, ?)""",
        (t.transaction_id, season, timestamp_iso, t.type, t.status),
    )

    row = conn.execute(
        "SELECT transaction_id FROM transactions WHERE season=? AND yahoo_transaction_id=?",
        (season, t.transaction_id),
    ).fetchone()
    transaction_id = row[0]

    # Walk players. Commish events typically have none — defensive iteration handles that.
    for p_wrapper in (getattr(t, 'players', None) or []):
        p = p_wrapper.player if hasattr(p_wrapper, 'player') else p_wrapper
        td = p.transaction_data

        player_id = p.player_id
        full_name = p.name.full if hasattr(p.name, 'full') else p.name
        ensure_player(
            conn,
            player_id,
            decode(full_name),
            decode(getattr(p, 'display_position', None)),
            decode(getattr(p, 'editorial_team_abbr', None)),
        )

        td_type = td.type
        source_type = td.source_type
        destination_type = td.destination_type

        if td_type == 'add':
            direction = 'incoming'
            team_season_id = lookup_team_season_id(conn, getattr(td, 'destination_team_key', None), season)
            counterparty = None
        elif td_type == 'drop':
            direction = 'outgoing'
            team_season_id = lookup_team_season_id(conn, getattr(td, 'source_team_key', None), season)
            counterparty = None
        elif td_type == 'trade':
            direction = 'incoming'
            team_season_id = lookup_team_season_id(conn, getattr(td, 'destination_team_key', None), season)
            counterparty = lookup_team_season_id(conn, getattr(td, 'source_team_key', None), season)
        else:
            print(f"  WARNING: unknown td.type={td_type!r} on txn {t.transaction_id} player {player_id}, skipping")
            continue

        conn.execute(
            """INSERT OR IGNORE INTO transaction_players
               (transaction_id, player_id, direction, team_season_id,
                source_type, destination_type, counterparty_team_season_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (transaction_id, player_id, direction, team_season_id,
             source_type, destination_type, counterparty),
        )

    # Walk picks (only present on some trade transactions)
    for pick_wrapper in (getattr(t, 'picks', None) or []):
        pick = pick_wrapper.pick if hasattr(pick_wrapper, 'pick') else pick_wrapper
        conn.execute(
            """INSERT OR IGNORE INTO transaction_picks
               (transaction_id, draft_round,
                source_team_season_id, destination_team_season_id, original_team_season_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                transaction_id,
                pick.round,
                lookup_team_season_id(conn, pick.source_team_key, season),
                lookup_team_season_id(conn, pick.destination_team_key, season),
                lookup_team_season_id(conn, pick.original_team_key, season),
            ),
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

        # An offseason / pre-draft year has no transactions yet — yfpy raises
        # YahooFantasySportsDataNotFound rather than returning an empty list.
        try:
            transactions = query.get_league_transactions()
        except Exception as e:
            print(f"  No transactions available: {type(e).__name__}")
            continue

        print(f"  Pulled {len(transactions)} transactions from Yahoo")

        ingested = errors = 0
        for t in transactions:
            try:
                ingest_transaction(conn, t, season)
                ingested += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR on txn {getattr(t, 'transaction_id', '?')}: {type(e).__name__}: {e}")

        conn.commit()
        print(f"  Ingested {ingested}, {errors} errors")

    # Summary
    print(f"\n=== Final table counts ===")
    for table in ('transactions', 'transaction_players', 'transaction_picks', 'players'):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<25} {n}")

    conn.close()


if __name__ == "__main__":
    main()