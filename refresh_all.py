"""refresh_all.py — the one-swoop run: data in, checks, publish.

Order, stopping at the first failure (nothing after a failed step runs):
  1. back up fantasy.db to Backups/ (timestamped; newest 30 kept)
  2. Yahoo pull            (only with --yahoo; skipped cleanly while the
                            API is unauthorized — probe_yahoo.py decides)
  3. commit FINAL panel trades   (add_synthetic_trades.py --commit;
                                  idempotent, pending trades are ignored)
  4. recon_ownership.py          (read-only check)
  5. refresh.py "<message>"      (regenerate, verify, commit, push)

Usage:
    python refresh_all.py "Commit message"
    python refresh_all.py --yahoo "Commit message"
    python refresh_all.py --no-publish          # steps 1-4 + regenerate only
    python refresh_all.py --unattended          # for Task Scheduler: never
                                                # prompts, logs to refresh_all.log

Every DB write here is one Pete already runs by hand; this only chains them.
Run it host-side only (never through a cloud/bridge session: see the 9/2
journal incident).
"""
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
BACKUPS = HERE / "Backups"
KEEP_BACKUPS = 30
FLAGS = 0x08000000 if sys.platform == "win32" else 0

# Yahoo-sourced pulls, in order. Deliberately EMPTY of the historical ingest
# scripts until the synthetic-vs-Yahoo reconciliation matcher exists:
# ingest_transactions would double-record the 2026 synthetic trades, and
# ingest_drafts / ingest_final_rosters clobber hand-maintained rows.
# Add a script here only after it has been dry-run reviewed.
YAHOO_STEPS = [
    # ["pull_yahoo_week.py"],   # scores.json + current_lineups.json (to build once the API answers)
]

args = [a for a in sys.argv[1:]]
WITH_YAHOO = "--yahoo" in args
NO_PUBLISH = "--no-publish" in args
UNATTENDED = "--unattended" in args
msg = next((a for a in args if not a.startswith("--")), None) or \
    f"Refresh ({datetime.now():%Y-%m-%d %H:%M})"


def say(s=""):
    print(s, flush=True)


def run(cmd, step, fatal=True):
    say(f"\n=== {step} ===")
    p = subprocess.run([sys.executable, "-u", *cmd], cwd=HERE,
                       creationflags=FLAGS)
    if p.returncode != 0 and fatal:
        say(f"\n*** STOPPED at: {step} (exit {p.returncode}). Nothing after this ran.")
        sys.exit(1)
    return p.returncode


def backup_db():
    say("=== 1. backing up fantasy.db ===")
    if (HERE / "fantasy.db-journal").exists():
        say("*** fantasy.db-journal exists: the DB has an unfinished write. "
            "Open it once host-side to roll back, then rerun.")
        sys.exit(1)
    BACKUPS.mkdir(exist_ok=True)
    dest = BACKUPS / f"fantasy_{datetime.now():%Y%m%d_%H%M%S}.db"
    shutil.copy2(DB, dest)
    say(f"  saved {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    old = sorted(BACKUPS.glob("fantasy_*.db"))[:-KEEP_BACKUPS]
    for f in old:
        f.unlink()
    if old:
        say(f"  pruned {len(old)} old backup(s)")


def main():
    if UNATTENDED:
        log = open(HERE / "refresh_all.log", "a", encoding="utf-8")
        log.write(f"\n##### {datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(args)}\n")
        log.flush()
        sys.stdout = sys.stderr = log

    backup_db()

    if WITH_YAHOO:
        rc = run(["probe_yahoo.py"], "2. Yahoo API probe", fatal=False)
        if rc != 0:
            say("  Yahoo API still not authorized. Skipping the Yahoo pull; "
                "continuing with local data.")
        elif not YAHOO_STEPS:
            say("  Probe passed, but no Yahoo pull scripts are enabled yet "
                "(see YAHOO_STEPS).")
        else:
            for cmd in YAHOO_STEPS:
                run(cmd, f"2. Yahoo pull: {cmd[0]}")
    else:
        say("\n=== 2. Yahoo pull skipped (no --yahoo) ===")

    run(["add_synthetic_trades.py", "--commit"], "3. committing FINAL panel trades")
    run(["recon_ownership.py"], "4. ownership recon")

    if NO_PUBLISH:
        run(["generate_dashboard.py"], "5. regenerate only (--no-publish)")
    else:
        run(["refresh.py", msg], "5. regenerate + publish")
    say("\n=== refresh_all finished OK ===")


if __name__ == "__main__":
    main()
