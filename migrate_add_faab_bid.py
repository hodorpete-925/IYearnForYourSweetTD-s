"""
migrate_add_faab_bid.py — add transactions.faab_bid (INTEGER, nullable).

Why: the trivia (and FAAB analysis generally) needs the FAAB bid amount per
waiver claim, which the original ingest didn't capture. This adds the column;
then re-running ingest_transactions.py backfills the values from Yahoo.

Idempotent: checks PRAGMA before ALTER, so it's safe to run more than once.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "fantasy.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    if "faab_bid" in cols:
        print("faab_bid already present on transactions — nothing to do.")
    else:
        conn.execute("ALTER TABLE transactions ADD COLUMN faab_bid INTEGER")
        conn.commit()
        print("Added transactions.faab_bid (INTEGER, nullable).")
        print("Next: re-run `python ingest_transactions.py` to backfill FAAB from Yahoo.")
    conn.close()


if __name__ == "__main__":
    main()
