"""undo_scott_tom_pick_trade.py - reverse the mis-entered Scott/Tom pick trade.

The 8/29 entry "Scott's 7.11 -> Tom, Tom's own R6 -> Scott" was WRONG
(Pete, 2026-08-31). This deletes those two synthetic pick movements,
matched by CONTENT (round + source + destination + original owner), not
by synth_id. After --commit: Tom holds his own R6 again, Scott holds the
7.11 again, both back where they were. The corrected trade gets entered
fresh in add_synthetic_trades.py once Pete confirms the real terms.

Run:  python undo_scott_tom_pick_trade.py            # dry-run
      python undo_scott_tom_pick_trade.py --commit   # delete both rows
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"


def tsid(conn, mgr):
    return conn.execute(
        "SELECT t.team_season_id FROM teams t "
        "JOIN managers m ON m.manager_id = t.manager_id "
        "WHERE m.full_name = ? AND t.season = 2026", (mgr,)).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")
    scott, tom, paul = (tsid(conn, "Scott Montgomery"),
                        tsid(conn, "Tom Watson"), tsid(conn, "Paul Lewis"))
    # (round, source, destination, original) for the two wrong movements
    targets = [(7, scott, tom, paul),   # 7.11 (orig Paul) Scott -> Tom
               (6, tom, scott, tom)]    # Tom's own R6 Tom -> Scott
    hits = []
    for rnd, src, dst, orig in targets:
        row = conn.execute("""
            SELECT sp.synth_id FROM synthetic_transaction_picks sp
            JOIN synthetic_transactions st ON st.synth_id = sp.synth_id
            WHERE sp.draft_round = ? AND sp.source_team_season_id = ?
              AND sp.destination_team_season_id = ?
              AND sp.original_team_season_id = ?
              AND DATE(st.timestamp) = '2026-08-29'
        """, (rnd, src, dst, orig)).fetchall()
        if len(row) != 1:
            print(f"ABORT: expected exactly 1 match for R{rnd} movement, "
                  f"found {len(row)} - investigate before deleting.")
            return
        hits.append((row[0][0], rnd))
        print(f"  would delete: synth {row[0][0]} (R{rnd} movement)")
    if not args.commit:
        print("DRY RUN. Re-run with --commit to apply.")
        return
    for sid, _ in hits:
        conn.execute("DELETE FROM synthetic_transaction_picks WHERE synth_id = ?", (sid,))
        conn.execute("DELETE FROM synthetic_transactions WHERE synth_id = ?", (sid,))
    conn.commit()
    print("Undone. Tom holds his own R6 again; Scott holds the 7.11. "
          "Enter the corrected trade once terms are confirmed, then regen.")


if __name__ == "__main__":
    main()
