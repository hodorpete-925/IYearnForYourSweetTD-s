"""control_panel.py — local control panel for the IYearn dashboard project.

Run:  python control_panel.py
Then your browser opens http://127.0.0.1:8765 — a status page with buttons
for the safe, routine operations. Ctrl+C in the terminal stops it.

What it shows:
  - dashboard build freshness (footer timestamp) and git/GitHub sync state
  - ADP freshness per season: source, fetched date, matched/unmatched counts
  - database vitals (size, last modified, transaction count/latest event)
  - when the 2026 ADP CSV was last saved, when recon last ran
  - an activity log of everything run from this panel (runs.json)

What the buttons run (allowlist; nothing else is executable from the page):
  - Update everything  -> refresh_all.py  (backup DB, commit FINAL panel
                          trades, recon, regenerate, publish; one confirm)
  - ...with Yahoo pull -> refresh_all.py --yahoo (same, Yahoo data first;
                          skips the pull cleanly while the API is locked)
  - Trades: dry run    -> add_synthetic_trades.py          (read-only plan)
  - Trades: commit     -> add_synthetic_trades.py --commit (confirm)
  - Refresh & publish  -> refresh.py   (asks for a commit message + confirm)
  - Regenerate only    -> generate_dashboard.py
  - Recon ownership    -> recon_ownership.py
  - Match ADP names    -> match_adp_players.py
  - DEF mappings (dry) -> add_adp_def_mappings.py
  - 2026 CSV (dry)     -> ingest_adp_2026_csv.py

Trade entry (2026-09-17): the form writes trades_pending.json only. The DB
is touched solely by add_synthetic_trades.py --commit, which reads FINAL
trades from that file; a trade still in its 48-hour counter window stays
pending and is ignored. Corrections to committed trades stay in the terminal.

Deliberately NOT on the page: anything with --apply, DB patches, git surgery.
Those stay in the terminal, on purpose.

Server binds 127.0.0.1 only — nothing on your network can reach it.
Standard library only; no installs needed.
"""
import json
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
PORT = 8765
RUNS_LOG = HERE / "runs.json"
# Under pythonw (no console), child processes like git would otherwise each
# flash up their own console window. This flag suppresses that.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
LIVE_URL = "https://hodorpete-925.github.io/IYearnForYourSweetTD-s/"

PENDING_FILE = HERE / "trades_pending.json"
TRADE_SEASON = 2026

ACTIONS = {
    "swoop":    {"label": "Update everything & publish", "cmd": ["refresh_all.py"], "takes_msg": True,
                 "confirm": "Back up the DB, commit FINAL trades, run recon, regenerate AND push to the live site. Go?"},
    "swoop_y":  {"label": "Update everything with Yahoo pull", "cmd": ["refresh_all.py", "--yahoo"], "takes_msg": True,
                 "confirm": "Same as Update everything, pulling Yahoo data first (skipped if the API is still locked). Go?"},
    "trades_dry": {"label": "Dry run", "cmd": ["add_synthetic_trades.py"], "group": "trades",
                   "desc": "Read-only. Lists every movement a commit would write, and which entries are ignored as pending."},
    "trades_commit": {"label": "Commit final trades to the database", "cmd": ["add_synthetic_trades.py", "--commit"],
                      "group": "trades", "danger": True,
                      "desc": "Writes trades marked final into fantasy.db. No backup here: Update everything backs up first.",
                      "confirm": "Write every FINAL pending trade into fantasy.db? Run the dry run first if you have not."},
    "refresh":  {"label": "Rebuild and publish", "cmd": ["refresh.py"], "takes_msg": True, "group": "publish",
                 "desc": "Regenerates the dashboard from the database as it stands and pushes it live. No database writes.",
                 "confirm": "This will regenerate AND push to the live site. Go?"},
    "generate": {"label": "Rebuild only", "cmd": ["generate_dashboard.py"], "group": "publish",
                 "desc": "Regenerates dashboard.html locally so you can look before publishing."},
    "recon":    {"label": "Ownership recon", "cmd": ["recon_ownership.py"], "group": "publish",
                 "desc": "Read-only check that team pages and player search agree on who owns whom."},
    "match":    {"label": "Match ADP names", "cmd": ["match_adp_players.py"], "group": "data",
                 "desc": "Links ADP rows to players. Run after any ADP ingest or cards show a dash."},
    "defmap":   {"label": "DEF mappings (dry run)", "cmd": ["add_adp_def_mappings.py"], "group": "data",
                 "desc": "Shows the defense name mappings it would add."},
    "adp2026":  {"label": "2026 ADP CSV (dry run)", "cmd": ["ingest_adp_2026_csv.py"], "group": "data",
                 "desc": "Parses the FantasyPros CSV and reports what it would load."},
}


# ---------- helpers ----------------------------------------------------------

def _git(*args, timeout=10):
    try:
        r = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                           text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _mtime(path):
    p = HERE / path
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _load_runs():
    try:
        return json.loads(RUNS_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []


def _log_run(action, rc, output):
    runs = _load_runs()
    tail = [l for l in output.strip().splitlines() if l.strip()][-3:]
    runs.insert(0, {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": ACTIONS.get(action, {}).get("label", action),
        "ok": rc == 0,
        "summary": " | ".join(tail)[:300],
    })
    RUNS_LOG.write_text(json.dumps(runs[:50], indent=1), encoding="utf-8")


def _load_pending():
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8")).get("trades", [])
    except Exception:
        return []


def _save_pending(trades):
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"trades": trades}, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(PENDING_FILE)


def trade_options():
    """Managers (DB full_name: what add_synthetic_trades resolves on) and
    each one's current roster, read from the last dashboard build's
    TRADE_DATA so the names are exactly the ones the DB knows."""
    managers, players = [], []
    try:
        conn = sqlite3.connect(f"file:{HERE / 'fantasy.db'}?mode=ro", uri=True)
        managers = [{"name": r[0], "team": r[1]} for r in conn.execute(
            "SELECT m.full_name, t.team_name FROM teams t JOIN managers m "
            "ON m.manager_id = t.manager_id WHERE t.season = ? ORDER BY t.team_name",
            (TRADE_SEASON,))]
        conn.close()
    except Exception as e:
        return {"error": str(e), "managers": [], "players": [], "pending": _load_pending()}
    try:
        html_ = (HERE / "dashboard.html").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"window\.TRADE_DATA\s*=\s*(\{.*?\});?\s*</script>", html_, re.S)
        td = json.loads(m.group(1))
        team_by_slug = {t["slug"]: t["team"] for t in td["teams"]}
        players = sorted(({"n": p["n"], "pos": p["p"], "team": team_by_slug.get(p["m"], "")}
                          for p in td["players"]), key=lambda p: p["n"])
    except Exception:
        players = []   # form still works with free-typed names
    return {"managers": managers, "players": players, "pending": _load_pending()}


def _clean_trade(req):
    """Validate a trade posted by the form. Returns (trade, error)."""
    date = str(req.get("date", "")).strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None, "Date must be YYYY-MM-DD."
    a, b = str(req.get("mgr_a", "")).strip(), str(req.get("mgr_b", "")).strip()
    if not a or not b or a == b:
        return None, "Pick two different managers."

    def names(v):
        return [x.strip() for x in (v or []) if str(x).strip()][:12]

    def picks(v):
        out = []
        for p in (v or [])[:8]:
            try:
                rnd, fs = int(p["round"]), int(p["for_season"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (1 <= rnd <= 16) or not (TRADE_SEASON <= fs <= TRADE_SEASON + 3):
                return None
            out.append({"round": rnd, "original": str(p.get("original", "")).strip(),
                        "for_season": fs})
        return out
    pa, pb = picks(req.get("picks_a")), picks(req.get("picks_b"))
    if pa is None or pb is None:
        return None, "Bad pick (round 1-16, draft year, original owner)."
    if any(not p["original"] for p in pa + pb):
        return None, "Every pick needs its original owner."
    ga, gb = names(req.get("gets_a")), names(req.get("gets_b"))
    if not (ga or gb or pa or pb):
        return None, "Nothing is moving in this trade."
    return {
        "date": date, "season": TRADE_SEASON, "final": bool(req.get("final")),
        "side_a": [a, ga], "side_b": [b, gb], "picks_a": pa, "picks_b": pb,
        "note": str(req.get("note", "")).strip()[:300] or "Entered via control panel.",
        "entered": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, None


def build_status():
    s = {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # Dashboard build
    dash = HERE / "dashboard.html"
    built = None
    if dash.exists():
        m = re.search(r"Dashboard rebuilt.{0,160}?(\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
                      dash.read_text(encoding="utf-8", errors="replace"), re.S)
        built = m.group(1) if m else None
    s["dashboard"] = {"built": built, "file_mtime": _mtime("dashboard.html"),
                      "size_mb": round(dash.stat().st_size / 1e6, 1) if dash.exists() else None}

    # Git
    _, head, _ = _git("rev-parse", "--short", "HEAD")
    _, last, _ = _git("log", "-1", "--pretty=%s (%cd)", "--date=format:%Y-%m-%d %H:%M")
    _, dirty, _ = _git("status", "--porcelain")
    rc, remote, err = _git("ls-remote", "origin", "refs/heads/main", timeout=8)
    _, full_head, _ = _git("rev-parse", "HEAD")
    in_sync = None
    if rc == 0 and remote:
        in_sync = remote.split()[0] == full_head
    s["git"] = {"head": head, "last_commit": last,
                "dirty_files": len([l for l in dirty.splitlines() if l.strip()]),
                "in_sync": in_sync,
                "remote_error": None if rc == 0 else (err or "unreachable")}

    # Database + ADP
    s["adp"], s["db"] = [], {}
    try:
        conn = sqlite3.connect(f"file:{HERE / 'fantasy.db'}?mode=ro", uri=True)
        for season, source, fetched, total, matched in conn.execute(
                "SELECT season, source, MAX(fetched_at), COUNT(*), "
                " SUM(CASE WHEN player_id IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM adp GROUP BY season ORDER BY season"):
            s["adp"].append({"season": season, "source": source,
                             "fetched": (fetched or "")[:16],
                             "total": total, "matched": matched,
                             "unmatched": total - matched})
        txn = conn.execute("SELECT COUNT(*), MAX(timestamp) FROM transactions").fetchone()
        s["db"] = {
            "mtime": _mtime("fantasy.db"),
            "size_mb": round((HERE / "fantasy.db").stat().st_size / 1e6, 1),
            "transactions": txn[0], "latest_event": (txn[1] or "")[:16],
            "players": conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
            "rostered_2025": conn.execute(
                "SELECT COUNT(*) FROM final_rosters WHERE season=2025").fetchone()[0],
        }
        conn.close()
    except Exception as e:
        s["db"]["error"] = str(e)

    # Files of interest
    s["files"] = {
        "adp_2026_csv": _mtime("adp_2026_2qb_fantasypros.csv"),
        "recon_report": _mtime("recon_ownership_report.csv"),
        "adp_unmatched": _mtime("adp_unmatched.csv"),
    }
    _b = sorted((HERE / "Backups").glob("fantasy_*.db")) if (HERE / "Backups").exists() else []
    s["files"]["last_backup"] = (datetime.fromtimestamp(_b[-1].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                                 if _b else None)
    _p = _load_pending()
    s["trades"] = {"final_n": sum(1 for t in _p if t.get("final")),
                   "pending_n": sum(1 for t in _p if not t.get("final"))}
    s["yahoo"] = {}
    for key, name in (("scores", "scores.json"), ("lineups", "current_lineups.json")):
        try:
            doc = json.loads((HERE / name).read_text(encoding="utf-8"))
            s["yahoo"][key + "_as_of"] = doc.get("as_of")
            s["yahoo"]["source"] = doc.get("source") or s["yahoo"].get("source")
        except Exception:
            pass
    s["runs"] = _load_runs()[:25]
    s["live_url"] = LIVE_URL
    return s


# ---------- HTTP --------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IYearn control panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root { --b800:#022479; --b600:#0038FF; --b400:#269AFF; --g600:#606C71; --g400:#999893;
        --g200:#E5E5DD; --line:#ebebed; --bg:#f7f7f5; --red:#982B09; --redbg:#fdeee8;
        --green:#566500; --greenbg:#f1f6dc; --amber:#8a6a00; --amberbg:#fdf3d1; }
* { box-sizing: border-box; }
body { margin:0; font-family:'Inter','Segoe UI',-apple-system,sans-serif; font-size:14px;
       line-height:1.5; color:#111; background:var(--bg); }
header { background:var(--b800); color:#fff; }
.hin, .tabs-in, main { max-width:1040px; margin:0 auto; padding-left:24px; padding-right:24px; }
.hin { display:flex; align-items:center; justify-content:space-between; gap:16px; padding-top:20px; padding-bottom:18px; flex-wrap:wrap; }
header h1 { font-size:20px; font-weight:600; margin:0; letter-spacing:-.01em; }
header .sub { font-size:12.5px; color:#b9c4e6; margin-top:2px; }
header a.live { color:#fff; text-decoration:none; font-size:13px; font-weight:600; border:1px solid rgba(255,255,255,.35);
                border-radius:6px; padding:7px 13px; white-space:nowrap; }
header a.live:hover { background:rgba(255,255,255,.12); }
.tabs { background:#fff; border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
.tabs-in { display:flex; gap:4px; overflow-x:auto; }
.tab { border:none; background:none; font:inherit; font-size:13.5px; font-weight:600; color:var(--g600);
       padding:14px 14px 12px; border-bottom:2px solid transparent; border-radius:0; cursor:pointer; white-space:nowrap; }
.tab:hover { color:var(--b800); }
.tab.on { color:var(--b800); border-bottom-color:var(--b600); }
.tab .n { display:inline-block; min-width:18px; margin-left:6px; padding:0 6px; border-radius:9px; font-size:11px;
          background:var(--amberbg); color:var(--amber); text-align:center; }
main { padding-top:26px; padding-bottom:140px; }
.pane { display:none; } .pane.on { display:block; }
h2 { font-size:16px; font-weight:600; color:#111; margin:0 0 4px; }
.lede { color:var(--g600); font-size:13px; margin:0 0 16px; max-width:720px; }
.panel { background:#fff; border:1px solid var(--line); border-radius:10px; padding:20px 22px; margin-bottom:18px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(225px,1fr)); gap:12px; margin-bottom:18px; }
.card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:15px 17px; border-left:3px solid var(--g200); }
.card.good { border-left-color:#B5D208; } .card.warn { border-left-color:#E1B523; } .card.err { border-left-color:#FA6526; }
.card .k { font-size:12px; color:var(--g600); font-weight:600; margin-bottom:4px; }
.card .v { font-size:18px; font-weight:700; color:var(--b800); font-variant-numeric:tabular-nums; line-height:1.25; }
.card .m { font-size:12px; color:var(--g600); margin-top:5px; }
.hero { display:flex; gap:22px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
.hero ol { margin:8px 0 0; padding-left:18px; color:var(--g600); font-size:13px; }
.hero .cta { display:flex; flex-direction:column; gap:8px; min-width:250px; }
table { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums; }
th { text-align:left; font-size:12px; color:var(--g600); font-weight:700; padding:8px 10px; border-bottom:1px solid var(--g200); }
td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:none; }
td.when { color:var(--g600); white-space:nowrap; }
td.num, th.num { text-align:right; }
button { font-family:inherit; font-size:13px; font-weight:600; cursor:pointer; border-radius:6px; padding:9px 15px;
         border:1px solid #d4d6da; background:#fff; color:var(--b800); }
button:hover { border-color:var(--b600); }
button.primary { background:var(--b600); border-color:var(--b600); color:#fff; padding:12px 18px; font-size:14px; }
button.primary:hover { background:var(--b800); border-color:var(--b800); }
button.danger { color:var(--red); }
button.mini { padding:5px 10px; font-size:12px; }
button:disabled { opacity:.45; cursor:wait; }
.arow { display:flex; align-items:center; gap:16px; padding:13px 0; border-bottom:1px solid var(--line); }
.arow:last-child { border-bottom:none; padding-bottom:0; } .arow:first-child { padding-top:0; }
.arow .t { flex:1; min-width:0; } .arow .t b { display:block; font-size:13.5px; }
.arow .t span { font-size:12.5px; color:var(--g600); }
.arow button { min-width:120px; }
.pill { display:inline-block; border-radius:999px; padding:2px 10px; font-size:11px; font-weight:700; }
.pill.ok { background:var(--greenbg); color:var(--green); } .pill.bad { background:var(--redbg); color:var(--red); }
.pill.wait { background:var(--amberbg); color:var(--amber); }
.bad-t { color:var(--red); }
label.f { display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--g600); font-weight:600; }
label.chk { display:flex; align-items:center; gap:8px; font-size:13px; color:#111; font-weight:500; }
input, select, textarea { font:13px 'Inter',sans-serif; padding:8px 10px; border:1px solid #d4d6da; border-radius:6px; background:#fff; color:#111; }
input:focus, select:focus, textarea:focus { outline:2px solid #cfdcff; border-color:var(--b600); }
textarea { resize:vertical; min-height:74px; }
.frow { display:flex; gap:16px; align-items:flex-end; flex-wrap:wrap; margin-bottom:16px; }
.sides { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }
.side { min-width:0; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:14px; display:flex; flex-direction:column; gap:10px; }
.side h3 { margin:0; font-size:13px; font-weight:700; color:var(--b800); }
.pickrow { display:flex; gap:6px; align-items:center; } .pickrow select { padding:6px 8px; min-width:0; } .side select, .side textarea { width:100%; } .pickrow select { width:auto; }
.picks { display:flex; flex-direction:column; gap:6px; }
.finder { display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; margin-bottom:16px; }
.foot { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; border-top:1px solid var(--line); padding-top:16px; }
.note { font-size:12.5px; color:var(--g600); margin:12px 0 0; }
#drawer { position:fixed; left:0; right:0; bottom:0; background:#0d1326; color:#d7e0ff; display:none; z-index:10;
          box-shadow:0 -6px 24px rgba(2,36,121,.25); }
#drawer .dh { display:flex; justify-content:space-between; align-items:center; padding:9px 24px; font-size:12.5px;
              font-weight:600; border-bottom:1px solid #222a47; max-width:1040px; margin:0 auto; }
#drawer .dh button { background:none; border:1px solid #38426b; color:#d7e0ff; padding:3px 10px; font-size:12px; }
#console { font:12px/1.55 Consolas,monospace; white-space:pre-wrap; max-height:300px; overflow:auto;
           padding:12px 24px 16px; max-width:1040px; margin:0 auto; }
.stop { margin-top:28px; font-size:12px; color:var(--g400); } .stop a { color:var(--g600); }
@media (max-width:720px) { .sides { grid-template-columns:1fr; } .arow { flex-wrap:wrap; } }
</style></head><body>
<header><div class="hin">
  <div><h1>I Yearn For Your Sweet TD&rsquo;s control panel</h1>
  <div class="sub">Runs on this PC only &middot; <span id="now">checking&hellip;</span></div></div>
  <a class="live" href="" id="live" target="_blank">Open the live site &#8599;</a>
</div></header>
<nav class="tabs"><div class="tabs-in">
  <button class="tab on" data-tab="overview">Overview</button>
  <button class="tab" data-tab="trades">Trades<span class="n" id="tn" hidden></span></button>
  <button class="tab" data-tab="publish">Publish &amp; checks</button>
  <button class="tab" data-tab="data">Data health</button>
  <button class="tab" data-tab="activity">Activity</button>
</div></nav>
<main>

<section class="pane on" id="p-overview">
  <div class="cards" id="cards"></div>
  <div class="panel hero">
    <div><h2>Update everything</h2>
      <p class="lede" style="margin:0">One run, stopping at the first failure:</p>
      <ol><li>Back up fantasy.db</li><li>Pull Yahoo data (optional)</li><li>Commit trades marked final</li>
      <li>Run the ownership recon</li><li>Rebuild the dashboard and push it live</li></ol></div>
    <div class="cta"><button class="primary" data-id="swoop">Update everything &amp; publish</button>
      <button data-id="swoop_y">Same, with Yahoo pull first</button></div>
  </div>
  <div class="panel"><h2>Last runs</h2><table id="runs3"><tbody></tbody></table></div>
</section>

<section class="pane" id="p-trades">
  <div class="panel">
    <h2>Enter a trade</h2>
    <p class="lede">Saved trades wait in a pending list. Only trades marked final are ever written to the database, so anything still inside the 48-hour counter window is safe to enter early.</p>
    <div class="frow">
      <label class="f">Trade date <input type="date" id="t_date"></label>
      <label class="chk"><input type="checkbox" id="t_final"> Final: past the counter window and not vetoed</label>
    </div>
    <div class="finder">
      <label class="f" style="flex:1;min-width:240px">Find a player <input id="t_find" list="plist" placeholder="Start typing a name">
      <datalist id="plist"></datalist></label>
      <button type="button" data-give="a">Add to team A receives</button>
      <button type="button" data-give="b">Add to team B receives</button>
    </div>
    <div class="sides">
      <div class="side"><h3>Team A</h3><select id="t_a"></select>
        <label class="f">Receives these players (one per line)<textarea id="t_ga"></textarea></label>
        <div class="picks" id="t_pa"></div><button type="button" class="mini" data-addpick="a" style="align-self:flex-start">+ Add a pick team A receives</button></div>
      <div class="side"><h3>Team B</h3><select id="t_b"></select>
        <label class="f">Receives these players (one per line)<textarea id="t_gb"></textarea></label>
        <div class="picks" id="t_pb"></div><button type="button" class="mini" data-addpick="b" style="align-self:flex-start">+ Add a pick team B receives</button></div>
    </div>
    <label class="f" style="margin-bottom:16px">Note (optional) <input id="t_note" maxlength="300" placeholder="Context for the trade log"></label>
    <div class="foot"><span id="t_err" class="bad-t" style="font-size:13px"></span>
      <button type="button" class="primary" id="t_save" style="margin-left:auto">Save to pending list</button></div>
  </div>
  <div class="panel">
    <h2>Pending list</h2>
    <p class="lede">Entries stay listed after they are committed. Later runs skip anything already in the database.</p>
    <table id="pending"><thead><tr><th>Date</th><th>Trade</th><th>Status</th><th></th></tr></thead><tbody></tbody></table>
  </div>
  <div class="panel" id="acts-trades"></div>
</section>

<section class="pane" id="p-publish">
  <div class="panel"><h2>Publish and checks</h2>
    <p class="lede">Rebuild or publish without touching the database. Patches, corrections and git surgery stay in the terminal on purpose.</p>
    <div id="acts-publish"></div></div>
</section>

<section class="pane" id="p-data">
  <div class="cards" id="cards-data"></div>
  <div class="panel"><h2>ADP benchmark (2-QB)</h2>
    <table id="adp"><thead><tr><th>Season</th><th>Source</th><th>Pulled</th>
    <th class="num">Rows</th><th class="num">Matched</th><th class="num">Unmatched</th></tr></thead><tbody></tbody></table></div>
  <div class="panel" id="acts-data"></div>
</section>

<section class="pane" id="p-activity">
  <div class="panel"><h2>Activity</h2><p class="lede">Everything run from this panel, newest first.</p>
  <table id="runs"><thead><tr><th>When</th><th>Action</th><th></th><th>Summary</th></tr></thead><tbody></tbody></table></div>
</section>

<p class="stop"><a href="#" id="stop">Stop the panel server</a>. Relaunch any time with the Start Control Panel shortcut.</p>
</main>
<div id="drawer"><div class="dh"><span id="dtitle">Output</span><button id="dclose">Hide</button></div><div id="console"></div></div>

<script>
const ACTIONS = __ACTIONS__;
const esc = s => String(s ?? '—').replace(/&/g,'&amp;').replace(/</g,'&lt;');
const $ = id => document.getElementById(id);

/* ---- tabs ---- */
function showTab(t) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.id === 'p-' + t));
  try { history.replaceState(null, '', '#' + t); } catch (_) {}
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => showTab(b.dataset.tab));
if (location.hash && $('p-' + location.hash.slice(1))) showTab(location.hash.slice(1));

function card(k, v, m, tone) {
  return '<div class="card '+(tone||'')+'"><div class="k">'+esc(k)+'</div><div class="v">'
         + esc(v) + '</div><div class="m">' + (m||'') + '</div></div>';
}
function runRows(runs) {
  return runs.map(r => '<tr><td class="when">'+esc(r.when)+'</td><td>'+esc(r.action)+'</td><td>'
    + '<span class="pill '+(r.ok?'ok':'bad')+'">'+(r.ok?'OK':'Failed')+'</span></td><td>'
    + esc(r.summary)+'</td></tr>').join('') || '<tr><td colspan="4">Nothing has been run from the panel yet.</td></tr>';
}

async function refreshStatus() {
  const s = await (await fetch('/api/status')).json();
  $('live').href = s.live_url;
  $('now').textContent = 'status checked ' + s.now;
  const g = s.git, d = s.db, y = s.yahoo || {}, t = s.trades || {};
  let c = '';
  c += card('Live site', g.in_sync === null ? 'Unknown' : (g.in_sync ? 'In sync with GitHub' : 'Not published yet'),
            esc(g.last_commit) + (g.dirty_files ? '<br>' + g.dirty_files + ' uncommitted file(s)' : ''),
            g.in_sync === false ? 'warn' : (g.in_sync ? 'good' : ''));
  c += card('Dashboard build', s.dashboard.built || 'Not found', (s.dashboard.size_mb||'?') + ' MB on disk',
            s.dashboard.built ? 'good' : 'err');
  c += card('Trades waiting', (t.final_n||0) + ' final · ' + (t.pending_n||0) + ' pending',
            'Final trades are committed by the next update run', (t.final_n||0) ? 'warn' : '');
  c += card('Yahoo data', y.scores_as_of || 'No scores file', 'Lineups: ' + esc(y.lineups_as_of || 'none')
            + '<br>' + esc(y.source || ''), y.scores_as_of ? '' : 'warn');
  $('cards').innerHTML = c;

  let cd = '';
  cd += card('Database', d.error ? 'Error' : (d.size_mb + ' MB'),
             d.error ? esc(d.error) : ('Modified ' + esc(d.mtime) + '<br>' + d.transactions
             + ' transactions, latest ' + esc(d.latest_event) + '<br>' + d.players + ' players'), d.error ? 'err' : '');
  cd += card('Latest backup', s.files.last_backup || 'None yet', 'Backups folder, newest 30 kept', s.files.last_backup ? '' : 'warn');
  cd += card('2026 ADP file', s.files.adp_2026_csv || 'Missing', 'FantasyPros superflex CSV');
  cd += card('Last recon report', s.files.recon_report || 'Never', 'recon_ownership_report.csv');
  cd += card('Last unmatched review', s.files.adp_unmatched || 'Never', 'adp_unmatched.csv');
  $('cards-data').innerHTML = cd;

  document.querySelector('#adp tbody').innerHTML = s.adp.map(a =>
    '<tr><td>'+a.season+'</td><td>'+esc(a.source)+'</td><td>'+esc(a.fetched)+'</td><td class="num">'
    + a.total+'</td><td class="num">'+a.matched+'</td><td class="num'+(a.unmatched>30?' bad-t':'')+'">'
    + a.unmatched+'</td></tr>').join('');
  document.querySelector('#runs tbody').innerHTML = runRows(s.runs || []);
  document.querySelector('#runs3 tbody').innerHTML = runRows((s.runs || []).slice(0, 3));
}

/* ---- trade entry ---- */
let TOPT = {managers: [], players: [], pending: []};
function mgrOptions() {
  return TOPT.managers.map(m => '<option value="'+esc(m.name)+'">'+esc(m.team)+' ('+esc(m.name)+')</option>').join('');
}
function addPickRow(side) {
  const d = document.createElement('div'); d.className = 'pickrow';
  const yr = new Date().getFullYear();
  d.innerHTML = '<select class="p_fs">'+[yr+1, yr+2].map(y => '<option>'+y+'</option>').join('')+'</select>'
    + '<select class="p_r">'+Array.from({length:16}, (_, i) => '<option value="'+(i+1)+'">Round '+(i+1)+'</option>').join('')+'</select>'
    + '<select class="p_o" style="flex:1;min-width:0"><option value="">Original owner</option>'+mgrOptions()+'</select>'
    + '<button type="button" class="mini" title="Remove pick">×</button>';
  d.querySelector('button').onclick = () => d.remove();
  $('t_p'+side).appendChild(d);
}
function readPicks(side) {
  return [...document.querySelectorAll('#t_p'+side+' .pickrow')].map(r => ({
    for_season: +r.querySelector('.p_fs').value, round: +r.querySelector('.p_r').value,
    original: r.querySelector('.p_o').value}));
}
function renderPending() {
  const side = (s, picks) => [...s[1], ...(picks||[]).map(p => p.for_season+' R'+p.round+' (orig '+p.original+')')].join(', ') || 'nothing';
  document.querySelector('#pending tbody').innerHTML = TOPT.pending.map((t, i) =>
    '<tr><td class="when">'+esc(t.date)+'</td><td><b>'+esc(t.side_a[0])+'</b> gets '+esc(side(t.side_a, t.picks_a))
    + '<br><b>'+esc(t.side_b[0])+'</b> gets '+esc(side(t.side_b, t.picks_b))
    + (t.note ? '<br><span style="color:var(--g600);font-size:12px">'+esc(t.note)+'</span>' : '') + '</td><td>'
    + '<span class="pill '+(t.final?'ok':'wait')+'">'+(t.final?'Final':'Pending')+'</span></td><td style="white-space:nowrap;text-align:right">'
    + '<button class="mini" data-tog="'+i+'">'+(t.final?'Mark pending':'Mark final')+'</button> '
    + '<button class="mini danger" data-rm="'+i+'">Remove</button></td></tr>').join('')
    || '<tr><td colspan="4">No trades entered from the panel yet.</td></tr>';
  document.querySelectorAll('[data-tog]').forEach(b => b.onclick = () => tradeOp({op:'toggle_final', index:+b.dataset.tog}));
  document.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => {
    if (confirm('Remove this entry from the pending list? This does not undo a trade already committed to the database.'))
      tradeOp({op:'remove', index:+b.dataset.rm}); });
  const n = TOPT.pending.length; $('tn').hidden = !n; $('tn').textContent = n;
}
async function tradeOp(body) {
  const r = await fetch('/api/trade', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  $('t_err').textContent = j.ok ? '' : (j.error || 'Failed');
  if (j.ok) { TOPT.pending = j.pending; renderPending(); refreshStatus(); }
  return j.ok;
}
let tradesWired = false;
async function initTrades() {
  TOPT = await (await fetch('/api/trades')).json();
  const keepA = $('t_a').value, keepB = $('t_b').value;
  $('t_a').innerHTML = $('t_b').innerHTML = '<option value="">Choose a team</option>' + mgrOptions();
  $('t_a').value = keepA; $('t_b').value = keepB;
  $('plist').innerHTML = TOPT.players.map(p => '<option value="'+esc(p.n)+'">'+esc(p.pos+' · '+p.team)+'</option>').join('');
  renderPending();
  if (tradesWired) return; tradesWired = true;
  $('t_date').value = new Date().toLocaleDateString('en-CA');
  document.querySelectorAll('[data-addpick]').forEach(b => b.onclick = () => addPickRow(b.dataset.addpick));
  document.querySelectorAll('[data-give]').forEach(b => b.onclick = () => {
    const f = $('t_find'); if (!f.value.trim()) return;
    const ta = $('t_g'+b.dataset.give);
    ta.value = (ta.value.trim() ? ta.value.trim()+'\\n' : '') + f.value.trim(); f.value = ''; f.focus(); });
  $('t_save').onclick = async () => {
    const lines = id => $(id).value.split('\\n').map(x => x.trim()).filter(Boolean);
    const ok = await tradeOp({op:'add', date: $('t_date').value, final: $('t_final').checked,
      mgr_a: $('t_a').value, mgr_b: $('t_b').value,
      gets_a: lines('t_ga'), gets_b: lines('t_gb'), picks_a: readPicks('a'), picks_b: readPicks('b'),
      note: $('t_note').value});
    if (ok) { ['t_ga','t_gb','t_note'].forEach(id => $(id).value = '');
      $('t_pa').innerHTML = $('t_pb').innerHTML = ''; $('t_final').checked = false; }
  };
}

/* ---- actions ---- */
function buildButtons() {
  const groups = {trades: ['Commit trades', 'Run the dry run first. It shows exactly what would be written.'],
                  publish: null, data: ['ADP maintenance', 'Dry runs and name matching. None of these publish.']};
  Object.keys(groups).forEach(gk => {
    const rows = Object.entries(ACTIONS).filter(([, a]) => a.group === gk).map(([id, a]) =>
      '<div class="arow"><div class="t"><b>'+esc(a.label)+'</b><span>'+esc(a.desc||'')+'</span></div>'
      + '<button data-id="'+id+'"'+(a.danger?' class="danger"':'')+'>Run</button></div>').join('');
    const head = groups[gk] ? '<h2>'+groups[gk][0]+'</h2><p class="lede">'+groups[gk][1]+'</p>' : '';
    $('acts-'+gk).innerHTML = head + rows;
  });
  document.querySelectorAll('button[data-id]').forEach(b => b.onclick = () => runAction(b.dataset.id));
}
async function runAction(id) {
  let body = {action: id};
  if (ACTIONS[id].takes_msg) {
    const msg = prompt('Commit message for this publish:', 'Dashboard refresh');
    if (msg === null) return;
    body.message = msg;
  }
  if (ACTIONS[id].confirm && !confirm(ACTIONS[id].confirm)) return;
  const all = document.querySelectorAll('button[data-id]');
  all.forEach(b => b.disabled = true);
  $('drawer').style.display = 'block';
  $('dtitle').textContent = 'Running: ' + ACTIONS[id].label + ' …';
  $('console').textContent = 'Working. The dashboard build alone takes a few seconds.';
  try {
    const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    $('dtitle').textContent = ACTIONS[id].label + (j.ok ? ': done' : ': FAILED (exit ' + j.rc + ')');
    $('console').textContent = j.output;
    $('console').scrollTop = $('console').scrollHeight;
  } catch (e) { $('console').textContent = 'Request failed: ' + e; }
  all.forEach(b => b.disabled = false);
  refreshStatus(); initTrades();
}
$('dclose').onclick = () => $('drawer').style.display = 'none';

$('stop').onclick = async (e) => {
  e.preventDefault();
  if (!confirm('Stop the panel server? The page will go dead until you relaunch it.')) return;
  try { await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'__shutdown__'})}); } catch(_) {}
  document.body.innerHTML = '<div style="font-family:Inter,sans-serif;padding:60px 40px;color:#606C71">'
    + 'Panel server stopped. Double-click <b>Start Control Panel</b> to bring it back.</div>';
};

buildButtons(); refreshStatus(); initTrades(); setInterval(refreshStatus, 30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _deny_remote(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_error(403)
            return True
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._deny_remote():
            return
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.replace("__ACTIONS__", json.dumps(
                {k: {kk: v.get(kk) for kk in ("label", "takes_msg", "confirm", "group", "desc", "danger")}
                 for k, v in ACTIONS.items()})).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._json(build_status())
        elif path == "/api/trades":
            self._json(trade_options())
        else:
            self.send_error(404)

    def do_POST(self):
        if self._deny_remote():
            return
        route = urlparse(self.path).path
        if route not in ("/api/run", "/api/trade"):
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"ok": False, "rc": -1, "output": "Bad request"}, 400)
        if route == "/api/trade":
            trades = _load_pending()
            op = req.get("op")
            if op == "add":
                t, err = _clean_trade(req)
                if err:
                    return self._json({"ok": False, "error": err}, 400)
                trades.append(t)
            elif op in ("remove", "toggle_final"):
                try:
                    i = int(req.get("index"))
                    assert 0 <= i < len(trades)
                except Exception:
                    return self._json({"ok": False, "error": "Bad index"}, 400)
                if op == "remove":
                    trades.pop(i)
                else:
                    trades[i]["final"] = not trades[i].get("final")
            else:
                return self._json({"ok": False, "error": "Unknown op"}, 400)
            _save_pending(trades)
            return self._json({"ok": True, "pending": trades})
        action = req.get("action")
        if action == "__shutdown__":
            self._json({"ok": True, "rc": 0, "output": "Server stopping."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if action not in ACTIONS:
            return self._json({"ok": False, "rc": -1, "output": "Unknown action"}, 400)
        cmd = [sys.executable] + ACTIONS[action]["cmd"]
        if ACTIONS[action].get("takes_msg") and req.get("message"):
            cmd.append(str(req["message"])[:120])
        try:
            r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=600,
                               creationflags=CREATE_NO_WINDOW)
            output = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
            rc = r.returncode
        except subprocess.TimeoutExpired:
            output, rc = "Timed out after 10 minutes.", -1
        _log_run(action, rc, output)
        self._json({"ok": rc == 0, "rc": rc, "output": output})

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running (port busy) — just open the existing panel.
        webbrowser.open(url)
        return
    print(f"Control panel running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
