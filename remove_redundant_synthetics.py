"""remove_redundant_synthetics.py - delete synthetic trades that duplicate a
transaction_override on a real Yahoo transaction.

BACKGROUND
----------
The 2025 off-season trades got entered twice:
  (a) as SYNTHETIC trades dated Feb 2025 (when the deals were agreed verbally),
  (b) as OVERRIDES on the real Aug-14 2025 drop+adds that Yahoo recorded when
      the commissioner processed the deals at the draft.
The overrides sit on real transactions AND are what drives DRC, so the Feb
synthetics are the redundant copies -- this removes them. (Each trade then
shows on its Aug processing date instead of the Feb handshake date.)

SAFETY GATE
-----------
A synthetic is deleted only if EVERY player it moves also has a 2025
`trade_from` override -- i.e., the trade is definitely preserved elsewhere.
The 2024 keeper-slot synthetics and the Conner / Marvin Harrison synthetics are
outside the Feb-2025 window and are never touched.

USAGE
-----
    python remove_redundant_synthetics.py            # dry-run (shows the plan)
    python remove_redundant_synthetics.py --commit   # actually delete
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"
WINDOW = "2025-02-%"   # the redundant Feb-2025 synthetic batch


def player_has_2025_override(conn, player_id):
    """Return the covering override transaction_id, or None."""
    row = conn.execute(
        "SELECT o.transaction_id FROM transaction_overrides o "
        "JOIN transactions t ON t.transaction_id = o.transaction_id "
        "JOIN transaction_players tp ON tp.transaction_id = o.transaction_id "
        "  AND tp.direction = 'incoming' "
        "WHERE tp.player_id = ? AND o.override_type = 'trade_from' "
        "  AND t.season = 2025 LIMIT 1",
        (player_id,),
    ).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually delete (default is a dry-run).")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    candidates = conn.execute(
        "SELECT synth_id, timestamp FROM synthetic_transactions "
        "WHERE timestamp LIKE ? ORDER BY synth_id",
        (WINDOW,),
    ).fetchall()

    print(f"Found {len(candidates)} synthetic trade(s) in the {WINDOW} window.\n")
    to_delete = []
    for sid, ts in candidates:
        players = conn.execute(
            "SELECT DISTINCT stp.player_id, p.player_name "
            "FROM synthetic_transaction_players stp "
            "JOIN players p ON p.player_id = stp.player_id "
            "WHERE stp.synth_id = ?",
            (sid,),
        ).fetchall()
        notes, all_covered = [], True
        for pid, name in players:
            ov = player_has_2025_override(conn, pid)
            if ov:
                notes.append(f"{name} [override txn{ov}]")
            else:
                all_covered = False
                notes.append(f"{name} [NO override]")
        if all_covered:
            to_delete.append(sid)
            print(f"  synth {sid} ({ts[:10]}) -> DELETE   {'; '.join(notes)}")
        else:
            print(f"  synth {sid} ({ts[:10]}) -> KEEP (not fully covered)   {'; '.join(notes)}")

    print(f"\n=== Plan: delete {len(to_delete)} synthetic trade(s) ===")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    if not to_delete:
        print("\nNothing to delete. Done.")
        return

    ph = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM synthetic_transaction_players WHERE synth_id IN ({ph})", to_delete)
    conn.execute(f"DELETE FROM synthetic_transactions WHERE synth_id IN ({ph})", to_delete)
    conn.commit()
    print(f"\nDeleted {len(to_delete)} synthetic trade(s). Done.")


if __name__ == "__main__":
    main()
