"""add_transaction_overrides.py - declarative inserter for transaction_overrides.

WHAT THIS IS FOR
----------------
When a trade happens after the trade deadline or in the off-season, Yahoo has no
"trade" transaction type, so the commissioner processes it by hand: drop the
player from team A, add it to team B. Yahoo records that as a waiver/free-agent
move, not a trade. A `transaction_override` re-labels that ADD as a real trade.

The override goes on the ADD (incoming) transaction and points
`source_team_season_id` at the team that GAVE THE PLAYER UP. Both the DRC engine
(compute_drc.py) and the dashboard Trades tab (trade_history.py) honor these, so
once inserted the trade shows up on both teams' profiles automatically.

A multi-player trade needs one row per player (one per add transaction), and a
straight swap needs one row per side.

HOW TO USE
----------
Edit the OVERRIDES list below, then run:
    python add_transaction_overrides.py            # dry-run (shows the plan)
    python add_transaction_overrides.py --commit   # actually insert

It's idempotent: an override that already exists is skipped.
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"

# ============================================================================
# DECLARE OVERRIDES HERE
# Each entry: the ADD transaction to re-label, and the team that gave the
# player up (source_team_season_id).
# ============================================================================
OVERRIDES = [
    {
        "transaction_id": 1548,          # Josh Downs, added by Brian Malconian
        "source_team_season_id": 26,     # from George Mensing (2025)
        "note": "Josh Downs: George Mensing -> Brian Malconian, 9/2/25 "
                "(Downs-for-Flacco swap, commish-processed add/drop)",
    },
    {
        "transaction_id": 1547,          # Joe Flacco, added by George Mensing
        "source_team_season_id": 31,     # from Brian Malconian (2025)
        "note": "Joe Flacco: Brian Malconian -> George Mensing, 9/2/25 "
                "(Flacco-for-Downs swap, commish-processed add/drop)",
    },
]


def describe_add(conn, txn_id):
    """Return (timestamp, player_name, dest_team_id, dest_mgr, event_type) for
    the incoming side of a transaction, or None if there's no incoming row."""
    return conn.execute(
        "SELECT t.timestamp, p.player_name, tp.team_season_id, m.full_name, t.event_type "
        "FROM transactions t "
        "JOIN transaction_players tp ON tp.transaction_id = t.transaction_id "
        "  AND tp.direction = 'incoming' "
        "JOIN players p ON p.player_id = tp.player_id "
        "LEFT JOIN teams te ON te.team_season_id = tp.team_season_id "
        "LEFT JOIN managers m ON m.manager_id = te.manager_id "
        "WHERE t.transaction_id = ?",
        (txn_id,),
    ).fetchone()


def team_manager(conn, team_season_id):
    row = conn.execute(
        "SELECT m.full_name, t.season FROM teams t "
        "JOIN managers m ON m.manager_id = t.manager_id "
        "WHERE t.team_season_id = ?",
        (team_season_id,),
    ).fetchone()
    return row  # (full_name, season) or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Actually insert (default is a dry-run).")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    planned = []
    print(f"Checking {len(OVERRIDES)} override(s)...\n")
    for ov in OVERRIDES:
        txn_id = ov["transaction_id"]
        src_id = ov["source_team_season_id"]

        add = describe_add(conn, txn_id)
        if add is None:
            print(f"  SKIP txn {txn_id}: no incoming (add) row found — check the id.")
            continue
        ts, player, dest_team, dest_mgr, event_type = add

        src = team_manager(conn, src_id)
        if src is None:
            print(f"  SKIP txn {txn_id}: source_team_season_id {src_id} is not a real team.")
            continue
        src_mgr, src_season = src

        if dest_team == src_id:
            print(f"  SKIP txn {txn_id}: source team == destination team ({src_id}); that can't be right.")
            continue

        existing = conn.execute(
            "SELECT 1 FROM transaction_overrides WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        if existing:
            print(f"  skip (already overridden): txn {txn_id}  {player}")
            continue

        print(f"  txn {txn_id}  {ts[:10]}  {player}: "
              f"{src_mgr} -> {dest_mgr}  (trade_from team {src_id})")
        planned.append((txn_id, src_id, ov["note"]))

    print(f"\n=== Plan: {len(planned)} override(s) to insert ===")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    if not planned:
        print("\nNothing to insert. Done.")
        return

    for txn_id, src_id, note in planned:
        conn.execute(
            "INSERT INTO transaction_overrides "
            "(transaction_id, override_type, source_team_season_id, note) "
            "VALUES (?, 'trade_from', ?, ?)",
            (txn_id, src_id, note),
        )
    conn.commit()
    print(f"\nInserted {len(planned)} override(s). Done.")


if __name__ == "__main__":
    main()
