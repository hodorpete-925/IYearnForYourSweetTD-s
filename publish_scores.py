"""publish_scores.py — push ONLY scores.json (the game-day fast path).

The live page re-fetches scores.json on load, so the hourly game-window job
never has to rebuild or commit the 9 MB dashboard. Does nothing if
scores.json is unchanged. Host-side only.

    python publish_scores.py            # publish scores.json as it is on disk
    python publish_scores.py --pull     # run the Yahoo pull first (needs the API)
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
FLAGS = 0x08000000 if sys.platform == "win32" else 0
PULL = [sys.executable, "pull_yahoo_week.py"]   # to build once the API answers


def git(*a):
    r = subprocess.run(["git", *a], cwd=HERE, capture_output=True, text=True,
                       creationflags=FLAGS)
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    log = open(HERE / "publish_scores.log", "a", encoding="utf-8")
    def say(s):
        log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {s}\n"); log.flush(); print(s)
    if "--pull" in sys.argv:
        if not (HERE / "pull_yahoo_week.py").exists():
            say("pull_yahoo_week.py not built yet (Yahoo API still locked); publishing file as-is")
        elif subprocess.run(PULL, cwd=HERE, creationflags=FLAGS).returncode != 0:
            say("Yahoo pull failed; nothing published"); sys.exit(1)
    if not (HERE / "scores.json").exists():
        say("no scores.json; nothing to do"); return
    git("add", "scores.json")
    rc, _ = git("diff", "--cached", "--quiet", "--", "scores.json")
    if rc == 0:
        say("scores.json unchanged; nothing to publish"); return
    rc, out = git("commit", "-m", f"Scores {datetime.now():%Y-%m-%d %H:%M}", "--", "scores.json")
    if rc != 0:
        say("commit failed: " + out); sys.exit(1)
    rc, out = git("push", "origin", "main")
    say("pushed scores.json" if rc == 0 else "push failed: " + out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
