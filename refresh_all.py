"""refresh_all.py — the one-swoop run: data in, checks, publish.

Order, stopping at the first failure (nothing after a failed step runs):
  1. back up fantasy.db to Backups/ (timestamped; newest 30 kept)
  2. Yahoo pull (with --yahoo):
       2a. probe_yahoo.py            read-only; if it fails, the Yahoo steps
                                     are skipped and the run continues on
                                     local data (never a silent half-pull)
       2b. ingest_2026_yahoo.py --commit   2026 transactions -> fantasy.db
                                     (upsert; flags superseded synthetics)
       2c. pull_yahoo_week.py        scores.json + current_lineups.json
  3. commit FINAL panel trades       (add_synthetic_trades.py --commit;
                                      idempotent, pending trades are ignored)
  4. recon_ownership.py              (read-only check)
  5. refresh.py "<message>"          (regenerate, verify, commit, push)

Usage:
    python refresh_all.py "Commit message"
    python refresh_all.py --yahoo "Commit message"
    python refresh_all.py --no-publish          # steps 1-4 + regenerate only
    python refresh_all.py --unattended          # for Task Scheduler: never
                                                # prompts, logs to refresh_all.log

Every DB write here is one Pete already runs by hand; this only chains them.
Run it host-side only (never through a cloud/bridge session: see the 9/2
journal incident and the 10/14 venv truncation).

History: the fenced Yahoo-page add/drop step (add_page_transactions.py) was
retired 2026-10-14 when the API came back; ingest_2026_yahoo.py superseded
every row it had produced.
"""
import os
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
# Child processes print team names with emoji; with their stdout piped
# Python would fall back to cp1252 and die on them (10/14). Force UTF-8.
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

# Yahoo-sourced pulls, in order. Each one was dry-run reviewed before it was
# added here. ingest_drafts / ingest_final_rosters / ingest_teams stay OUT:
# they clobber hand-maintained rows (and ingest_teams refuses on masked guids).
YAHOO_STEPS = [
    ["ingest_2026_yahoo.py", "--commit"],   # transactions (2026 only, upsert)
    ["pull_yahoo_week.py"],                 # scores.json + current_lineups.json
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
    """Run a step, echoing its output line by line through OUR stdout so it
    lands in the control panel's capture (and the unattended log) instead
    of an inherited handle that may go nowhere under pythonw."""
    say(f"\n=== {step} ===")
    p = subprocess.Popen([sys.executable, "-u", *cmd], cwd=HERE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace",
                         bufsize=1, creationflags=FLAGS, env=CHILD_ENV)
    for line in p.stdout:
        say(line.rstrip("\n"))
    p.wait()
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
        rc = run(["probe_yahoo.py"], "2a. Yahoo API probe", fatal=False)
        if rc != 0:
            say("  Yahoo API probe failed. Skipping the Yahoo pull; "
                "continuing with local data.")
        else:
            for cmd in YAHOO_STEPS:
                run(cmd, f"2. Yahoo pull: {' '.join(cmd)}")
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
