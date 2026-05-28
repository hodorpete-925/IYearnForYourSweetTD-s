"""
inspect_vetoed_txn.py — diagnostic to see what status field yfpy exposes on
trades, so we can capture vetoed/successful in our schema.

Picks txn 425 (2023-09-14 Adams+Stafford vetoed Pearson<->Malconian) as the
test case and dumps every attribute available. Also prints a known-successful
trade for side-by-side comparison.

Run from project root: python inspect_vetoed_txn.py
"""

import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

TARGET_TXN = "425"
SEASON     = 2023


def dump_attrs(label, txn):
    print(f"\n--- {label} ---")
    print(f"transaction_id  : {getattr(txn, 'transaction_id', '<missing>')}")
    print(f"type            : {getattr(txn, 'type', '<missing>')}")
    print(f"status          : {getattr(txn, 'status', '<missing>')}")
    print(f"timestamp       : {getattr(txn, 'timestamp', '<missing>')}")
    print("All attributes:")
    for k, v in vars(txn).items():
        if k.startswith("_"):
            continue
        # truncate long lists/dicts for readability
        s = repr(v)
        if len(s) > 200:
            s = s[:200] + " ...(truncated)"
        print(f"  {k:30s} = {s}")


def main():
    # Pull season -> nfl_game_id + yahoo_league_id from DB (same pattern as ingest_transactions.py)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT nfl_game_id, yahoo_league_id FROM seasons WHERE season = ?",
        (SEASON,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"No seasons row for {SEASON}")
    nfl_game_id, yahoo_league_id = row
    conn.close()

    print(f"Fetching {SEASON} transactions (league {yahoo_league_id}, game {nfl_game_id})...")
    query = YahooFantasySportsQuery(
        league_id=yahoo_league_id,
        game_code="nfl",
        game_id=nfl_game_id,
        env_file_location=project_dir,
        save_token_data_to_env_file=True,
    )
    txns = query.get_league_transactions()
    print(f"Got {len(txns)} transactions\n")

    # Find target
    def tid(t):
        return str(getattr(t, "transaction_id", "")) or \
               str(getattr(t, "transaction_key", "")).split(".")[-1]

    target = next((t for t in txns if tid(t) == TARGET_TXN), None)
    if target is None:
        print(f"!! Could not find txn {TARGET_TXN} in {len(txns)} returned transactions")
        print("First 5 trades for reference:")
        trades = [t for t in txns if getattr(t, "type", "") == "trade"][:5]
        for t in trades:
            print(f"  txn {tid(t):>4s}  type={getattr(t,'type','?'):8s}  status={getattr(t,'status','?')}")
        return

    dump_attrs(f"TARGET vetoed: txn {TARGET_TXN}", target)

    # Find a known-successful trade for comparison
    success = next((t for t in txns
                    if getattr(t, "type", "") == "trade"
                    and tid(t) != TARGET_TXN), None)
    if success:
        dump_attrs(f"Reference trade: txn {tid(success)}", success)


if __name__ == "__main__":
    main()
