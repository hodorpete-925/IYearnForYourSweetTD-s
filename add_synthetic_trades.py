"""add_synthetic_trades.py - declarative inserter for synthetic trades.

Edit the TRADES list at the top to declare what to insert. Each trade is a
4-tuple shape: (date, season, side_a, side_b) where each side is
(manager_full_name, [list_of_player_names_received]).

The script:
  - Resolves manager names to team_season_ids for the trade's season
  - Resolves player names to player_ids (fuzzy fallback for typos)
  - Generates fresh synth_ids
  - Inserts one synthetic_transactions row per player movement (matches the
    existing batching pattern in synthetic_transactions)
  - Is idempotent: skips a trade if all its (date, player, manager) tuples
    already exist as synthetics

Run:  python add_synthetic_trades.py             # dry-run
      python add_synthetic_trades.py --commit    # actually insert
"""
import argparse
import difflib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"

# ============================================================================
# DECLARE TRADES HERE
# ============================================================================
TRADES = [
    {
        "date": "2025-02-03",
        "season": 2025,
        "side_a": ("Alex Schlosberg", []),
        "side_b": ("Tom Watson", ["Bryce Young", "Derrick Henry", "Jordan Addison"]),
        "note": "Off-season Schlosberg -> Tom Watson, Feb 2025 (verify counterparty)",
    },
    {
        "date": "2025-02-14",
        "season": 2025,
        "side_a": ("Brian Malconian", ["De'Von Achane"]),
        "side_b": ("Alex Schlosberg", ["Jahmyr Gibbs"]),
        "note": "Brian-Schlosberg swap, Feb 2025 (picks also moved; not modeled here)",
    },
    {
        "date": "2025-02-27",
        "season": 2025,
        "side_a": ("Greg Pearson", ["Dalton Kincaid", "Rico Dowdle"]),
        "side_b": ("Brian Malconian", ["Jaxon Smith-Njigba"]),
        "note": "Greg-Brian swap, Feb 2025",
    },
]


def normalize(s):
    if not s:
        return ""
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def resolve_player(conn, name, cache):
    if name in cache:
        return cache[name]
    rows = list(conn.execute(
        "SELECT player_id, player_name FROM players"))
    norm_target = normalize(name)
    # Exact normalized match first
    for pid, pname in rows:
        if normalize(pname) == norm_target:
            cache[name] = pid
            return pid
    # Fuzzy fallback
    pool = {pname: pid for pid, pname in rows}
    best = difflib.get_close_matches(name, pool.keys(), n=1, cutoff=0.75)
    if best:
        cache[name] = pool[best[0]]
        print(f"  fuzzy: {name!r} -> {best[0]!r}")
        return pool[best[0]]
    print(f"  WARN: player not found: {name!r}")
    return None


def resolve_team(conn, mgr_name, season, cache):
    key = (mgr_name, season)
    if key in cache:
        return cache[key]
    row = conn.execute("""
        SELECT t.team_season_id
        FROM teams t JOIN managers m ON m.manager_id = t.manager_id
        WHERE m.full_name = ? AND t.season = ?
    """, (mgr_name, season)).fetchone()
    if not row:
        print(f"  WARN: no team for {mgr_name!r} in {season}")
        return None
    cache[key] = row[0]
    return row[0]


def trade_already_exists(conn, date, team_dest, pid):
    """True if a synthetic trade already has this exact player movement."""
    row = conn.execute("""
        SELECT 1 FROM synthetic_transactions st
        JOIN synthetic_transaction_players stp ON stp.synth_id = st.synth_id
        WHERE DATE(st.timestamp) = ?
          AND stp.team_season_id = ?
          AND stp.player_id = ?
          AND stp.direction = 'incoming'
        LIMIT 1
    """, (date, team_dest, pid)).fetchone()
    return row is not None


def next_synth_id(conn):
    row = conn.execute("SELECT COALESCE(MAX(synth_id), 0) + 1 FROM synthetic_transactions").fetchone()
    return row[0]


def insert_movement(conn, synth_id, date, season, pid, dest_team, src_team):
    """Insert ONE synthetic trade movement (matches the per-player batching
    used by the existing synthetic_transactions data: each player gets its
    own synth_id with timestamp at the trade date)."""
    ts = f"{date} 00:00:00"
    conn.execute(
        "INSERT INTO synthetic_transactions (synth_id, timestamp, event_type, season) "
        "VALUES (?, ?, 'trade', ?)",
        (synth_id, ts, season),
    )
    # The destination team's incoming row
    conn.execute(
        "INSERT INTO synthetic_transaction_players "
        "(synth_id, player_id, direction, team_season_id, source_type, "
        " destination_type, counterparty_team_season_id) "
        "VALUES (?, ?, 'incoming', ?, 'team', 'team', ?)",
        (synth_id, pid, dest_team, src_team),
    )
    # The source team's outgoing row
    conn.execute(
        "INSERT INTO synthetic_transaction_players "
        "(synth_id, player_id, direction, team_season_id, source_type, "
        " destination_type, counterparty_team_season_id) "
        "VALUES (?, ?, 'outgoing', ?, 'team', 'team', ?)",
        (synth_id, pid, src_team, dest_team),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Actually insert (default is dry-run).")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")
    player_cache = {}
    team_cache = {}
    planned = []

    print(f"Loaded {len(TRADES)} trade(s) from TRADES list.\n")
    for trade in TRADES:
        print(f"--- {trade['date']} {trade['side_a'][0]} <-> {trade['side_b'][0]} ---")
        print(f"  ({trade['note']})")
        date = trade["date"]
        season = trade["season"]
        team_a = resolve_team(conn, trade["side_a"][0], season, team_cache)
        team_b = resolve_team(conn, trade["side_b"][0], season, team_cache)
        if not team_a or not team_b:
            print("  SKIPPED (team resolution failed)")
            continue
        # side_a gets the players from side_a's "received" list; same for side_b
        for player_name in trade["side_a"][1]:
            pid = resolve_player(conn, player_name, player_cache)
            if not pid:
                continue
            if trade_already_exists(conn, date, team_a, pid):
                print(f"  skip (already in DB): {player_name} -> {trade['side_a'][0]}")
                continue
            planned.append((date, season, pid, player_name, team_a, team_b,
                            trade["side_a"][0]))
        for player_name in trade["side_b"][1]:
            pid = resolve_player(conn, player_name, player_cache)
            if not pid:
                continue
            if trade_already_exists(conn, date, team_b, pid):
                print(f"  skip (already in DB): {player_name} -> {trade['side_b'][0]}")
                continue
            planned.append((date, season, pid, player_name, team_b, team_a,
                            trade["side_b"][0]))

    print(f"\n=== Plan: {len(planned)} player-movement inserts ===")
    for date, season, pid, pname, dest_team, src_team, dest_mgr in planned:
        print(f"  {date}  {pname:<30} -> {dest_mgr}")

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    if not planned:
        print("\nNothing to insert. Done.")
        return

    print(f"\nInserting {len(planned)} movements...")
    for date, season, pid, pname, dest_team, src_team, dest_mgr in planned:
        sid = next_synth_id(conn)
        insert_movement(conn, sid, date, season, pid, dest_team, src_team)
    conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
