"""add_bold_predictions.py - the 2026/27 Bold Predictions ledger, into the DB.

Pete's feature (2026-09-02): each manager submits bold predictions for the
season; Pete rates each on a 1-3 boldness scale; at the season-end summit
the league reviews them and awards the boldest CORRECT take. The dashboard
renders these under Commentary & League Info and tracks who hasn't
submitted yet.

Edit PREDICTIONS below to add/change entries (text edits and boldness
ratings both sync on re-run - the script updates rows that differ).
Boldness: 1-3 per Pete, None until he rates it. Outcome: set at season
end via SQL or a future script ('right' / 'wrong' / 'push').

NOTE: Tom's #1 was reworded for the public site ("fleeces" - original
group-chat phrasing on file with Pete); flagged to Pete 2026-09-02.

Run:  python add_bold_predictions.py             # dry-run
      python add_bold_predictions.py --commit    # create table + sync
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"
SEASON = 2026

# manager full_name -> list of (pred_no, prediction, boldness 1-3 or None)
PREDICTIONS = {
    "Pete Hodor": [
        (1, "Jets end the regular season above a .500 record", None),
        (2, "Bill K. makes the playoffs", None),
        (3, "Brian M. does not get the 1 or 2 seed in the playoffs", None),
    ],
    "Tom Watson": [
        (1, "Brian M. fleeces someone in a trade this season", None),
        (2, "Dan V. quits the league after this season", None),
        (3, "A trade gets countered at the trade deadline but misses "
            "the official cut-off", None),
    ],
    "Brian Malconian": [
        (1, "Pete H. drops a keeper by week 5 to spend on a waiver "
            "wire bid", None),
        (2, "Bill K. quits", None),
        (3, "Aric T. posts 5 times on the league chat on something "
            "that is not related to a trade", None),
    ],
    "George Mensing": [
        (1, "Buffalo Bills win the Super Bowl", None),
        (2, "A manager will drop from the league this year", None),
    ],
    "Alex Schlosberg": [
        (1, "Someone will spend more than $60 FAAB on a single player "
            "in free agency", None),
    ],
    "Paul Lewis": [
        (1, "Bhayshul Tuten will not live up to his hype and will "
            "underperform his ADP", None),
        (2, "First starting QB waiver wire claim will go for above "
            "$90 FAAB", None),
    ],
}

DDL = """
CREATE TABLE IF NOT EXISTS bold_predictions (
    season      INTEGER NOT NULL,
    manager_id  INTEGER NOT NULL,
    pred_no     INTEGER NOT NULL,
    prediction  TEXT    NOT NULL,
    boldness    INTEGER CHECK (boldness BETWEEN 1 AND 3),
    outcome     TEXT,
    PRIMARY KEY (season, manager_id, pred_no),
    FOREIGN KEY (manager_id) REFERENCES managers(manager_id)
)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    # Dry-run is STRICTLY read-only (the DB lives on Pete's machine and
    # all writes are his to run): open read-only unless --commit, and
    # only create the table inside --commit.
    if args.commit:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(DDL)
    else:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    table_exists = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'bold_predictions'"
    ).fetchone())

    plan = []
    for mgr, preds in PREDICTIONS.items():
        row = conn.execute(
            "SELECT manager_id FROM managers WHERE full_name = ?",
            (mgr,)).fetchone()
        if row is None:
            print(f"ERROR: unknown manager {mgr!r}")
            return
        mid = row[0]
        for no, text, bold in preds:
            cur = None
            if table_exists:
                cur = conn.execute("""
                    SELECT prediction, boldness FROM bold_predictions
                    WHERE season = ? AND manager_id = ? AND pred_no = ?
                """, (SEASON, mid, no)).fetchone()
            if cur is None:
                plan.append(("INSERT", mgr, mid, no, text, bold))
            elif cur[0] != text or cur[1] != bold:
                plan.append(("UPDATE", mgr, mid, no, text, bold))

    print(f"=== Plan: {len(plan)} change(s) ===")
    for op, mgr, _mid, no, text, bold in plan:
        b = f" [boldness {bold}]" if bold else ""
        print(f"  {op}  {mgr} #{no}: {text}{b}")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    for op, _mgr, mid, no, text, bold in plan:
        conn.execute("""
            INSERT INTO bold_predictions
                (season, manager_id, pred_no, prediction, boldness)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season, manager_id, pred_no)
            DO UPDATE SET prediction = excluded.prediction,
                          boldness = excluded.boldness
        """, (SEASON, mid, no, text, bold))
    conn.commit()
    print(f"Done. {len(plan)} row(s) applied.")


if __name__ == "__main__":
    main()
