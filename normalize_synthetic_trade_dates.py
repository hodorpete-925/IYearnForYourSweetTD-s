"""normalize_synthetic_trade_dates.py - pin every synthetic trade to June 30 of
its season.

Synthetic trades are commish-processed / verbally-agreed off-season deals with
no precise real date. When one happened to be dated on draft day (Aug 25) it
collided with that year's keeper draft event and sorted AFTER it -- making a
player look "kept" before he was traded for (the James Conner case). Pinning all
synthetics to {season}-06-30 places them cleanly in the off-season, before every
draft / keeper / regular-season event, so ordering is always trade -> keep.

The dashboard displays these as "Off-Season Trade" (no date shown), so the exact
day is cosmetic -- this is purely about sort order. DRC is unaffected: June is
off-season just like August, so the freeze/decrement math is identical (verify
with the DRC diff before committing if you like).

    python normalize_synthetic_trade_dates.py            # dry-run (shows the plan)
    python normalize_synthetic_trade_dates.py --commit   # apply
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually update (default is a dry-run).")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT st.synth_id, st.timestamp, st.season, "
        "       (SELECT p.player_name FROM synthetic_transaction_players stp "
        "        JOIN players p ON p.player_id = stp.player_id "
        "        WHERE stp.synth_id = st.synth_id AND stp.direction = 'incoming' LIMIT 1) "
        "FROM synthetic_transactions st ORDER BY st.synth_id"
    ).fetchall()

    changes = []
    for sid, ts, season, sample in rows:
        target = f"{season}-06-30 00:00:00"
        if str(ts) != target:
            changes.append((sid, ts, target, sample))

    print(f"{len(rows)} synthetic trade(s); {len(changes)} need re-dating to June 30:\n")
    for sid, old, new, sample in changes:
        print(f"  synth {sid:<3} {str(old)[:10]} -> {new[:10]}   ({sample or '?'})")

    print(f"\n=== Plan: update {len(changes)} timestamp(s) ===")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    if not changes:
        print("\nNothing to change. Done.")
        return

    for sid, old, new, sample in changes:
        conn.execute("UPDATE synthetic_transactions SET timestamp = ? WHERE synth_id = ?",
                     (new, sid))
    conn.commit()
    print(f"\nUpdated {len(changes)} timestamp(s). Done.")


if __name__ == "__main__":
    main()
