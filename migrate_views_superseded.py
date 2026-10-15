"""
migrate_views_superseded.py - rebuild the all_transactions / all_transaction_players
views so synthetic rows that Yahoo has superseded drop out of every consumer
(compute_drc, player_history, trade_history, recon scripts).

Background (2026-10-14): ingest_2026_yahoo.py flags post-draft synthetics with
superseded_by_yahoo_id once the same move exists as a real Yahoo transaction.
generate_dashboard filters on that flag directly, but the two UNION views did
not, so the 38 retired rows and their Yahoo twins were both visible.

Idempotent: safe to run again. One transaction; rolled back on any error.

    venv/Scripts/python.exe migrate_views_superseded.py           # dry run
    venv/Scripts/python.exe migrate_views_superseded.py --commit
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"

VIEWS = {
    "all_transactions": """
CREATE VIEW all_transactions AS
    SELECT transaction_id, timestamp, event_type, season, status, 0 AS is_synthetic
    FROM transactions
    WHERE status = 'successful'
    UNION ALL
    SELECT synth_id + 1000000 AS transaction_id, timestamp, event_type, season,
           'successful' AS status, 1 AS is_synthetic
    FROM synthetic_transactions
    WHERE superseded_by_yahoo_id IS NULL
""",
    "all_transaction_players": """
CREATE VIEW all_transaction_players AS
    SELECT tp.transaction_id, tp.player_id, tp.direction, tp.team_season_id,
           tp.source_type, tp.destination_type, tp.counterparty_team_season_id,
           0 AS is_synthetic
    FROM transaction_players tp
    JOIN transactions t ON t.transaction_id = tp.transaction_id
    WHERE t.status = 'successful'
    UNION ALL
    SELECT sp.synth_id + 1000000 AS transaction_id, sp.player_id, sp.direction,
           sp.team_season_id, sp.source_type, sp.destination_type,
           sp.counterparty_team_season_id, 1 AS is_synthetic
    FROM synthetic_transaction_players sp
    JOIN synthetic_transactions st ON st.synth_id = sp.synth_id
    WHERE st.superseded_by_yahoo_id IS NULL
""",
}


def counts(conn):
    return dict(conn.execute(
        "SELECT event_type, COUNT(*) FROM all_transactions WHERE season=2026 GROUP BY 1").fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(synthetic_transactions)")]
    if "superseded_by_yahoo_id" not in cols:
        print("synthetic_transactions.superseded_by_yahoo_id does not exist yet; "
              "run ingest_2026_yahoo.py --commit first.")
        sys.exit(1)
    print("2026 rows in all_transactions BEFORE:", counts(conn))
    conn.execute("BEGIN")
    try:
        for name, sql in VIEWS.items():
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(sql)
            print(f"  rebuilt view {name}")
        print("2026 rows in all_transactions AFTER: ", counts(conn))
        if args.commit:
            conn.commit(); print("COMMITTED.")
        else:
            conn.rollback(); print("DRY RUN: rolled back. Re-run with --commit.")
    except Exception as e:
        conn.rollback(); print(f"ROLLED BACK: {type(e).__name__}: {e}"); sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
