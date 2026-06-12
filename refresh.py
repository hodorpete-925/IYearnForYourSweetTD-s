"""refresh.py — one command from "data changed" to "live site updated."

Chains the publish flow with a checkpoint at every step, so the failure
modes we've hit by hand (regen silently skipped, push never ran, stale
dashboard shipped) can't happen quietly:

  1. regenerate dashboard.html (and require the generator to report success)
  2. verify the file on disk is actually the fresh build (footer timestamp)
  3. git add dashboard.html + index.html
  4. skip committing if only the footer timestamp changed (no real changes)
  5. commit and push
  6. confirm GitHub's main now matches local (the push really landed)

Usage:
    python refresh.py                       # default commit message
    python refresh.py "Post-trade update"   # custom commit message
    python refresh.py --force               # publish even if only the
                                            # timestamp changed / failures > 0

This script does NOT touch fantasy.db. Data work (ingests, matching,
patches) stays manual and deliberate — run this after the data is right.
"""
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
FORCE = "--force" in sys.argv
msg_args = [a for a in sys.argv[1:] if a != "--force"]
COMMIT_MSG = msg_args[0] if msg_args else f"Refresh dashboard ({datetime.now():%Y-%m-%d %H:%M})"
LIVE_URL = "https://hodorpete-925.github.io/IYearnForYourSweetTD-s/"


def die(step, detail=""):
    print(f"\n*** STOPPED at: {step}")
    if detail:
        print(detail)
    print("Nothing after this step was run. Fix the issue (or ask Claude) and rerun.")
    sys.exit(1)


def run(cmd, step):
    """Run a command, echo its output, die on non-zero exit."""
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if out.strip():
        print(out.strip())
    if r.returncode != 0:
        die(step, f"(command: {' '.join(cmd)})")
    return out


print("Step 1/6 — regenerating dashboard.html ...")
out = run([sys.executable, "generate_dashboard.py"], "regenerating the dashboard")
if "Wrote" not in out:
    die("regenerating the dashboard", "Generator finished but never said 'Wrote ...'")
m = re.search(r"(\d+) failures", out)
if m and int(m.group(1)) > 0 and not FORCE:
    die("regenerating the dashboard",
        f"Generator reported {m.group(1)} failures. Investigate, or rerun with --force.")

print("\nStep 2/6 — verifying the build on disk is fresh ...")
html = (HERE / "dashboard.html").read_text(encoding="utf-8", errors="replace")
fm = re.search(r"Generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", html)
if not fm:
    die("verifying the build", "No 'Generated <timestamp>' footer found in dashboard.html")
built = datetime.strptime(fm.group(1), "%Y-%m-%d %H:%M")
if datetime.now() - built > timedelta(minutes=10):
    die("verifying the build",
        f"dashboard.html says it was generated {fm.group(1)} — that's stale, "
        "so the regeneration didn't actually update the file.")
print(f"  build timestamp OK: {fm.group(1)}")

print("\nStep 3/6 — staging files ...")
run(["git", "add", "dashboard.html", "index.html"], "staging files")

print("\nStep 4/6 — checking for real changes ...")
numstat = run(["git", "diff", "--cached", "--numstat"], "checking staged changes")
lines = [l for l in numstat.strip().splitlines() if l.strip()]
only_timestamp = (
    len(lines) == 1
    and lines[0].split()[2] == "dashboard.html"
    and lines[0].split()[0] == "1"
    and lines[0].split()[1] == "1"
)
if not lines:
    print("  Nothing staged — dashboard identical to last commit. Nothing to publish.")
    sys.exit(0)
if only_timestamp and not FORCE:
    print("  Only the footer timestamp changed — no real content difference.")
    print("  Skipping commit. (Use --force if you want to publish anyway.)")
    run(["git", "restore", "--staged", "dashboard.html"], "unstaging")
    run(["git", "checkout", "--", "dashboard.html"], "restoring file")
    sys.exit(0)
print(f"  staged: {', '.join(l.split()[2] for l in lines)}")

print(f"\nStep 5/6 — committing and pushing ('{COMMIT_MSG}') ...")
run(["git", "commit", "-m", COMMIT_MSG], "committing")
run(["git", "push", "origin", "main"], "pushing to GitHub")

print("\nStep 6/6 — confirming the push landed on GitHub ...")
local = run(["git", "rev-parse", "HEAD"], "reading local commit").strip()
remote = run(["git", "ls-remote", "origin", "refs/heads/main"], "reading GitHub").split()[0]
if local != remote:
    die("confirming the push",
        f"Local is {local[:7]} but GitHub shows {remote[:7]}. The push didn't land.")

print(f"""
  Push confirmed: {local[:7]} is live on GitHub.

DONE. GitHub Pages rebuilds in ~1 minute:
  {LIVE_URL}
""")
