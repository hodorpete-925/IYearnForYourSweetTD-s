"""repair_team_managers.py - restore teams.manager_id after an ingest_teams.py
run corrupted them.

WHAT HAPPENED (2026-07-01): re-running ingest_teams.py pulled manager data from
Yahoo with MASKED guids ('--hidden--'). ingest_teams keys managers by guid, so
every team collapsed onto ONE bogus manager row (null full_name) and every team's
manager_id was repointed to it. The dashboard then crashes in build_sidebar
(slugify(None)) because that manager has no name.

THE FIX: nothing else touched the teams/managers tables between the last backup
and the bad ingest, so the backup's (team_season_id -> manager_id) mapping is the
correct one. This restores each team's manager_id from the backup and deletes the
bogus manager. Current team NAMES are kept (so any legit rename survives); only
manager_id is restored.

    python repair_team_managers.py            # dry-run (shows the plan)
    python repair_team_managers.py --commit   # apply
"""
import argparse
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
DEFAULT_BACKUP = HERE / "Backups" / "fantasy.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Actually apply (default is a dry-run).")
    ap.add_argument("--backup", default=str(DEFAULT_BACKUP),
                    help="Backup DB holding the correct team->manager mapping.")
    args = ap.parse_args()

    cur = sqlite3.connect(DB)
    cur.execute("PRAGMA foreign_keys = ON;")
    bak = sqlite3.connect(f"file:{args.backup}?mode=ro", uri=True)

    bak_map = {r[0]: r[1] for r in bak.execute("SELECT team_season_id, manager_id FROM teams")}
    bak_mgr = {r[0]: r[1] for r in bak.execute("SELECT manager_id, full_name FROM managers")}

    fixes, missing = [], []
    for tsid, cur_mgr in cur.execute("SELECT team_season_id, manager_id FROM teams").fetchall():
        correct = bak_map.get(tsid)
        if correct is None:
            missing.append(tsid)
        elif cur_mgr != correct:
            fixes.append((tsid, cur_mgr, correct, bak_mgr.get(correct)))

    print(f"Backup: {args.backup}")
    print(f"{len(fixes)} team(s) to re-point; {len(missing)} not found in backup (left untouched).\n")
    for tsid, old, new, name in fixes:
        print(f"  team {tsid}: manager {old} -> {new} ({name})")
    if missing:
        print(f"\n  NOT IN BACKUP (review manually): {missing}")

    print(f"\n=== Plan: re-point {len(fixes)} teams, then drop any nameless manager with no teams ===")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    for tsid, old, new, name in fixes:
        cur.execute("UPDATE teams SET manager_id=? WHERE team_season_id=?", (new, tsid))
    orphan_bogus = [r[0] for r in cur.execute(
        "SELECT manager_id FROM managers WHERE full_name IS NULL "
        "AND manager_id NOT IN (SELECT DISTINCT manager_id FROM teams)")]
    for mid in orphan_bogus:
        cur.execute("DELETE FROM managers WHERE manager_id=?", (mid,))
    cur.commit()

    bad = cur.execute("SELECT COUNT(*) FROM teams t JOIN managers m ON m.manager_id=t.manager_id "
                      "WHERE m.full_name IS NULL").fetchone()[0]
    print(f"\nRepaired {len(fixes)} teams; removed bogus manager(s) {orphan_bogus}.")
    print(f"Teams still pointing at a nameless manager: {bad} (should be 0). Done.")


if __name__ == "__main__":
    main()
