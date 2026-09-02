"""setup_2026_franchise_updates.py - one-time 2026 franchise maintenance.

Two changes, reported by Pete 2026-08-30 from the Yahoo keeper screens:

  1. RENAME: Tom Watson's 2026 team "The Prince of Darkness"
     -> "Alice in First Down Chains".
  2. BILL KEENAN: new manager replacing Jon Lewitus on The Lady Boys
     roster. The Yahoo API is dead, so his real guid is unknowable for
     now; we insert a placeholder guid (MANUAL-BILL-KEENAN) and a 2026
     teams row (yahoo_team_id 12 - the 2026 rows created so far used
     1-11). When the API comes back, ingest_teams will NOT collide with
     this row (it aborts on masked guids and keys by guid); reconcile
     the placeholder guid to his real one at that point.
     Display-level handoff already exists in generate_dashboard.py
     (CURRENT_HANDOFFS maps Jon Lewitus -> Bill Keenan); DRC keep-path
     still anchors on the 2025 Lewitus roster, which is correct.

Run:  python setup_2026_franchise_updates.py            # dry-run
      python setup_2026_franchise_updates.py --commit   # apply
Idempotent: safe to re-run; each step skips if already applied.
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"

NEW_TOM_NAME = "Alice in First Down Chains"
NEW_BRIAN_NAME = "Malco In The High Castle"   # spotted on Yahoo 2026-08-31
KEENAN_GUID = "MANUAL-BILL-KEENAN"   # placeholder until the API returns
KEENAN_NICK = "Bill"
KEENAN_FULL = "Bill Keenan"
LADYBOYS_NAME = "The Lady Boys"
LADYBOYS_YAHOO_TEAM_ID = 12          # 2026 rows so far use 1-11


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    actions = []

    # --- 1. Tom rename -------------------------------------------------
    row = conn.execute("""
        SELECT t.team_season_id, t.team_name FROM teams t
        JOIN managers m ON m.manager_id = t.manager_id
        WHERE m.full_name = 'Tom Watson' AND t.season = 2026
    """).fetchone()
    if row is None:
        print("ERROR: no 2026 team found for Tom Watson")
        return
    if row[1] == NEW_TOM_NAME:
        print(f"skip (already renamed): Tom Watson 2026 = {row[1]!r}")
    else:
        actions.append(("rename",
                        f"Tom Watson 2026 team: {row[1]!r} -> {NEW_TOM_NAME!r}",
                        ("UPDATE teams SET team_name = ? WHERE team_season_id = ?",
                         (NEW_TOM_NAME, row[0]))))

    # --- 1b. Brian rename (2026-08-31, from Yahoo draft-results page) ----
    row_b = conn.execute("""
        SELECT t.team_season_id, t.team_name FROM teams t
        JOIN managers m ON m.manager_id = t.manager_id
        WHERE m.full_name = 'Brian Malconian' AND t.season = 2026
    """).fetchone()
    if row_b is None:
        print("ERROR: no 2026 team found for Brian Malconian")
        return
    if row_b[1] == NEW_BRIAN_NAME:
        print(f"skip (already renamed): Brian Malconian 2026 = {row_b[1]!r}")
    else:
        actions.append(("rename",
                        f"Brian Malconian 2026 team: {row_b[1]!r} -> {NEW_BRIAN_NAME!r}",
                        ("UPDATE teams SET team_name = ? WHERE team_season_id = ?",
                         (NEW_BRIAN_NAME, row_b[0]))))

    # --- 2. Bill Keenan manager row -------------------------------------
    mgr = conn.execute("SELECT manager_id FROM managers WHERE yahoo_guid = ?",
                       (KEENAN_GUID,)).fetchone()
    if mgr:
        keenan_id = mgr[0]
        print(f"skip (manager exists): Bill Keenan = manager_id {keenan_id}")
    else:
        keenan_id = None
        actions.append(("manager",
                        f"INSERT manager {KEENAN_FULL!r} (guid {KEENAN_GUID}, "
                        f"placeholder until API returns)",
                        ("INSERT INTO managers (yahoo_guid, nickname, full_name) "
                         "VALUES (?, ?, ?)",
                         (KEENAN_GUID, KEENAN_NICK, KEENAN_FULL))))

    # --- 3. Lady Boys 2026 team row -------------------------------------
    team = conn.execute("""
        SELECT team_season_id FROM teams
        WHERE season = 2026 AND yahoo_team_id = ?
    """, (LADYBOYS_YAHOO_TEAM_ID,)).fetchone()
    if team:
        print(f"skip (team exists): 2026 yahoo_team_id {LADYBOYS_YAHOO_TEAM_ID}"
              f" = team_season_id {team[0]}")
    else:
        actions.append(("team",
                        f"INSERT 2026 team {LADYBOYS_NAME!r} "
                        f"(yahoo_team_id {LADYBOYS_YAHOO_TEAM_ID}, manager Bill Keenan)",
                        None))  # SQL built after manager insert (needs id)

    print(f"\n=== Plan: {len(actions)} action(s) ===")
    for _, desc, _sql in actions:
        print("  -", desc)

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    for kind, desc, sql in actions:
        if kind == "manager":
            cur = conn.execute(*sql)
            keenan_id = cur.lastrowid
        elif kind == "team":
            if keenan_id is None:
                keenan_id = conn.execute(
                    "SELECT manager_id FROM managers WHERE yahoo_guid = ?",
                    (KEENAN_GUID,)).fetchone()[0]
            conn.execute(
                "INSERT INTO teams (season, yahoo_team_id, team_name, manager_id) "
                "VALUES (2026, ?, ?, ?)",
                (LADYBOYS_YAHOO_TEAM_ID, LADYBOYS_NAME, keenan_id))
        else:
            conn.execute(*sql)
        print("  applied:", desc)
    conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
