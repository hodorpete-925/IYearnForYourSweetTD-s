"""
ingest_2026_yahoo.py - load the 2026 season from Yahoo into fantasy.db and
retire the synthetic rows Yahoo now covers. Pete's ruling 2026-10-14: Yahoo is
the source of truth from the draft onward; pre-draft synthetics (the August
group-chat trades) stay.

What it does, in one SQLite transaction (rolled back on any error):
  1. Pulls every 2026 transaction for league 4416 via yfpy and inserts it with
     ingest_transactions.ingest_transaction() (upsert on season + yahoo id, so
     it is safe to re-run; new waiver moves land on each run).
  2. SKIPS the four 9/3 21:47 rows (Yahoo ids 20-23). They are the
     commissioner's Walker/Love swap, already recorded as an amended draft in
     draft_results_2026.json so both anchor at round 1 for DRC. Ingesting them
     would make both players look like waiver pickups (DRC 16).
  3. Adds synthetic_transactions.superseded_by_yahoo_id (if missing) and sets
     it on every 2026 synthetic whose movements Yahoo also recorded (exact
     move-set match). Flag, not delete: reversible, and the dashboard query
     ignores flagged rows. Matching rule: adds/drops compare both legs; trades
     compare incoming legs + picks, because Yahoo stores a trade's incoming
     legs only. So a trade entered on the control panel AND recorded by Yahoo
     collapses to the Yahoo copy on the next run.
  4. Carries the 2026 team renames in TEAM_RENAMES.

Dry run (default) prints the plan. --commit writes.
    venv/Scripts/python.exe ingest_2026_yahoo.py
    venv/Scripts/python.exe ingest_2026_yahoo.py --commit
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from yfpy.query import YahooFantasySportsQuery
from ingest_transactions import ingest_transaction

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
SEASON, GAME_ID, LEAGUE_ID = 2026, 470, "4416"

# Yahoo transaction ids NOT to ingest, with the reason. See step 2 above.
SKIP_YAHOO_TXN_IDS = {20: "Walker drop  (commish swap, amended draft)",
                      21: "Love drop    (commish swap, amended draft)",
                      22: "Love add     (commish swap, amended draft)",
                      23: "Walker add   (commish swap, amended draft)"}

# 2026 team renames seen on Yahoo 2026-10-14 (yahoo_team_id -> name).
# pull_yahoo_week.py carries current names into scores.json on every run;
# this list only keeps the teams table in step.
TEAM_RENAMES = {1: "Nightman Cometh For UR Blanket",
                7: "Mean Mug-ging",
                10: "The Winston Knight Rises"}


def synthetic_moves(conn):
    """synth_id -> frozenset of ('in'|'out', player_id, tsid) | ('k', round, dest_tsid).
    Trades keep incoming legs only (Yahoo's shape); adds/drops keep both legs,
    so a pure drop still matches its Yahoo twin."""
    etype = dict(conn.execute("SELECT synth_id, event_type FROM synthetic_transactions WHERE season=?", (SEASON,)))
    out = {sid: set() for sid in etype}
    for sid, pid, tsid, d in conn.execute(
            "SELECT sp.synth_id, sp.player_id, sp.team_season_id, sp.direction FROM synthetic_transaction_players sp "
            "JOIN synthetic_transactions st ON st.synth_id=sp.synth_id "
            "WHERE st.season=? AND sp.team_season_id IS NOT NULL", (SEASON,)):
        if d != "incoming" and etype.get(sid) == "trade":
            continue
        out[sid].add(("in" if d == "incoming" else "out", pid, tsid))
    for sid, rd, dst in conn.execute(
            "SELECT sp.synth_id, sp.draft_round, sp.destination_team_season_id FROM synthetic_transaction_picks sp "
            "JOIN synthetic_transactions st ON st.synth_id=sp.synth_id WHERE st.season=?", (SEASON,)):
        out[sid].add(("k", rd, dst))
    return {k: frozenset(v) for k, v in out.items() if v}


def yahoo_moves(conn):
    """transaction_id -> frozenset of the same shape, from the real tables."""
    out = {}
    for tid, pid, tsid, d in conn.execute(
            "SELECT tp.transaction_id, tp.player_id, tp.team_season_id, tp.direction FROM transaction_players tp "
            "JOIN transactions t ON t.transaction_id=tp.transaction_id "
            "WHERE t.season=? AND tp.team_season_id IS NOT NULL", (SEASON,)):
        out.setdefault(tid, set()).add(("in" if d == "incoming" else "out", pid, tsid))
    for tid, rd, dst in conn.execute(
            "SELECT tk.transaction_id, tk.draft_round, tk.destination_team_season_id FROM transaction_picks tk "
            "JOIN transactions t ON t.transaction_id=tk.transaction_id WHERE t.season=?", (SEASON,)):
        out.setdefault(tid, set()).add(("k", rd, dst))
    return {k: frozenset(v) for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write changes (default is dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")
    before = conn.execute("SELECT COUNT(*) FROM transactions WHERE season=?", (SEASON,)).fetchone()[0]
    print(f"transactions already in DB for {SEASON}: {before}")

    # --- 1. pull from Yahoo ---------------------------------------------
    q = YahooFantasySportsQuery(league_id=LEAGUE_ID, game_code="nfl", game_id=GAME_ID,
                                env_file_location=HERE, save_token_data_to_env_file=True)
    txns = q.get_league_transactions()
    print(f"pulled {len(txns)} transactions from Yahoo")

    conn.execute("BEGIN")
    try:
        # --- 2. ingest, skipping the commish swap ---------------------------
        ingested = skipped = errors = 0
        for t in txns:
            tid = int(t.transaction_id)
            if tid in SKIP_YAHOO_TXN_IDS:
                skipped += 1
                continue
            try:
                ingest_transaction(conn, t, SEASON)
                ingested += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR on yahoo txn {tid}: {type(e).__name__}: {e}")
        if errors:
            raise RuntimeError(f"{errors} ingest errors; rolling back")
        after = conn.execute("SELECT COUNT(*) FROM transactions WHERE season=?", (SEASON,)).fetchone()[0]
        print(f"upserted {ingested} (skipped {skipped} commish-swap rows); "
              f"{SEASON} rows in DB {before} -> {after} (+{after - before} new)")

        # --- 3. flag superseded synthetics ----------------------------------
        cols = [r[1] for r in conn.execute("PRAGMA table_info(synthetic_transactions)")]
        if "superseded_by_yahoo_id" not in cols:
            conn.execute("ALTER TABLE synthetic_transactions ADD COLUMN superseded_by_yahoo_id INTEGER")
            print("added column synthetic_transactions.superseded_by_yahoo_id")
        smoves, ymoves = synthetic_moves(conn), yahoo_moves(conn)
        yid_of = dict(conn.execute("SELECT transaction_id, yahoo_transaction_id FROM transactions WHERE season=?", (SEASON,)))
        already = dict(conn.execute("SELECT synth_id, superseded_by_yahoo_id FROM synthetic_transactions WHERE season=?", (SEASON,)))
        flagged_new = 0
        for sid, sm in sorted(smoves.items()):
            hit = next((tid for tid, ym in ymoves.items() if ym == sm), None)
            if hit is None:
                continue
            if already.get(sid) == yid_of[hit]:
                continue
            ts, et = conn.execute("SELECT timestamp, event_type FROM synthetic_transactions WHERE synth_id=?", (sid,)).fetchone()
            conn.execute("UPDATE synthetic_transactions SET superseded_by_yahoo_id=? WHERE synth_id=?", (yid_of[hit], sid))
            flagged_new += 1
            print(f"  synth {sid:>3} {ts[:10]} {et:9} superseded by yahoo {yid_of[hit]}")
        total_flagged = conn.execute("SELECT COUNT(*) FROM synthetic_transactions WHERE season=? AND superseded_by_yahoo_id IS NOT NULL", (SEASON,)).fetchone()[0]
        remaining = conn.execute("SELECT COUNT(*) FROM synthetic_transactions WHERE season=? AND superseded_by_yahoo_id IS NULL", (SEASON,)).fetchone()[0]
        print(f"superseded synthetics: {total_flagged} ({flagged_new} newly flagged); {remaining} remain live")

        # --- 4. team renames ---------------------------------------------------
        for ytid, name in TEAM_RENAMES.items():
            old = conn.execute("SELECT team_name FROM teams WHERE season=? AND yahoo_team_id=?", (SEASON, ytid)).fetchone()
            if old and old[0] != name:
                conn.execute("UPDATE teams SET team_name=? WHERE season=? AND yahoo_team_id=?", (name, SEASON, ytid))
                print(f"  team {ytid}: {old[0]!r} -> {name!r}")

        if args.commit:
            conn.commit()
            print("COMMITTED.")
        else:
            conn.rollback()
            print("DRY RUN: rolled back, nothing written. Re-run with --commit.")
    except Exception as e:
        conn.rollback()
        print(f"\nROLLED BACK: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
