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
  - Refresh & publish  -> refresh.py   (asks for a commit message + confirm)
  - Regenerate only    -> generate_dashboard.py
  - Recon ownership    -> recon_ownership.py
  - Match ADP names    -> match_adp_players.py
  - DEF mappings (dry) -> add_adp_def_mappings.py
  - 2026 CSV (dry)     -> ingest_adp_2026_csv.py

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
LIVE_URL = "https://hodorpete-925.github.io/IYearnForYourSweetTD-s/"

ACTIONS = {
    "refresh":  {"label": "Refresh & publish", "cmd": ["refresh.py"], "takes_msg": True},
    "generate": {"label": "Regenerate only", "cmd": ["generate_dashboard.py"]},
    "recon":    {"label": "Recon ownership", "cmd": ["recon_ownership.py"]},
    "match":    {"label": "Match ADP names", "cmd": ["match_adp_players.py"]},
    "defmap":   {"label": "DEF mappings (dry run)", "cmd": ["add_adp_def_mappings.py"]},
    "adp2026":  {"label": "2026 ADP CSV (dry run)", "cmd": ["ingest_adp_2026_csv.py"]},
}


# ---------- helpers ----------------------------------------------------------

def _git(*args, timeout=10):
    try:
        r = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                           text=True, timeout=timeout)
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


def build_status():
    s = {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # Dashboard build
    dash = HERE / "dashboard.html"
    built = None
    if dash.exists():
        m = re.search(r"Generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
                      dash.read_text(encoding="utf-8", errors="replace"))
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
    s["runs"] = _load_runs()[:12]
    s["live_url"] = LIVE_URL
    return s


# ---------- HTTP --------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IYearn control panel</title>
<style>
:root { --b800:#022479; --b600:#0038FF; --g600:#606C71; --g200:#E5E5DD;
        --g100:#f0f0ee; --g50:#fafaf8; --red:#982B09; --green:#6B7D00; }
* { box-sizing: border-box; }
body { margin:0; font-family:'Inter','Segoe UI',-apple-system,sans-serif;
       color:#111; background:#fff; }
.wrap { max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 22px; color: var(--b800); margin: 0 0 2px; }
.sub { color: var(--g600); font-size: 13px; margin-bottom: 22px; }
.sub a { color: var(--b600); text-decoration: none; }
h2 { font-size: 12px; letter-spacing: .1em; text-transform: uppercase;
     color: var(--g600); margin: 26px 0 10px; font-weight: 600; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 12px; }
.card { background: var(--g50); border: 1px solid var(--g200); border-radius: 5px; padding: 14px 16px; }
.card .k { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
           color: var(--g600); font-weight: 600; margin-bottom: 6px; }
.card .v { font-size: 17px; font-weight: 700; color: var(--b800);
           font-variant-numeric: tabular-nums; }
.card .m { font-size: 12px; color: var(--g600); margin-top: 4px; line-height: 1.5; }
.ok { color: var(--green); } .bad { color: var(--red); }
table { width: 100%; border-collapse: collapse; font-size: 13px;
        font-variant-numeric: tabular-nums; }
th { text-align: left; font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
     color: var(--g600); padding: 6px 8px; border-bottom: 1px solid var(--g200); }
td { padding: 7px 8px; border-bottom: 1px solid var(--g100); }
.btns { display: flex; flex-wrap: wrap; gap: 8px; }
button { font-family: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
         border-radius: 4px; padding: 9px 15px; border: 1px solid var(--g200);
         background: #fff; color: var(--b800); }
button:hover { border-color: var(--b600); }
button.primary { background: var(--b600); border-color: var(--b600); color: #fff; }
button.primary:hover { background: var(--b800); }
button:disabled { opacity: .45; cursor: wait; }
#console { background: #0d1326; color: #d7e0ff; border-radius: 5px; padding: 14px;
           font: 12px/1.55 Consolas, monospace; white-space: pre-wrap;
           min-height: 60px; max-height: 380px; overflow: auto; display: none; }
.log .when { color: var(--g600); white-space: nowrap; }
.pill { display: inline-block; border-radius: 999px; padding: 1px 9px; font-size: 11px;
        font-weight: 600; }
.pill.ok { background: #eef4e2; color: var(--green); }
.pill.bad { background: #fbe9e2; color: var(--red); }
</style></head><body><div class="wrap">
<h1>I Yearn For Your Sweet TD&rsquo;s &mdash; control panel</h1>
<div class="sub">Local ops &amp; status &middot; <a href="" id="live" target="_blank">open the live site &#8599;</a> &middot; <span id="now"></span></div>

<h2>Status</h2>
<div class="cards" id="cards"></div>

<h2>ADP benchmark (2-QB)</h2>
<table id="adp"><thead><tr><th>Season</th><th>Source</th><th>Pulled</th>
<th>Rows</th><th>Matched</th><th>Unmatched</th></tr></thead><tbody></tbody></table>

<h2>Actions</h2>
<div class="btns" id="btns"></div>
<p style="font-size:12px;color:var(--g600);margin:8px 0 10px">
Anything with <code>--apply</code>, DB patches, or git surgery stays in the terminal on purpose.</p>
<div id="console"></div>

<h2>Activity log (from this panel)</h2>
<table class="log" id="runs"><thead><tr><th>When</th><th>Action</th><th></th><th>Summary</th></tr></thead><tbody></tbody></table>

<p style="margin-top:34px;font-size:12px;color:var(--g600)">
<a href="#" id="stop" style="color:var(--g600)">Stop the panel server</a>
&mdash; relaunch any time with the Start Control Panel shortcut.</p>
</div>
<script>
const ACTIONS = __ACTIONS__;
const esc = s => String(s ?? '—').replace(/&/g,'&amp;').replace(/</g,'&lt;');

function card(k, v, m, cls) {
  return '<div class="card"><div class="k">'+esc(k)+'</div><div class="v '+(cls||'')+'">'
         + esc(v) + '</div><div class="m">' + (m||'') + '</div></div>';
}

async function refreshStatus() {
  const r = await fetch('/api/status'); const s = await r.json();
  document.getElementById('live').href = s.live_url;
  document.getElementById('now').textContent = 'checked ' + s.now;
  let c = '';
  c += card('Dashboard build', s.dashboard.built || 'not found',
            (s.dashboard.size_mb||'?') + ' MB on disk');
  const g = s.git;
  c += card('GitHub sync',
            g.in_sync === null ? 'unknown' : (g.in_sync ? 'in sync' : 'OUT OF SYNC'),
            esc(g.last_commit) + (g.dirty_files ? '<br>' + g.dirty_files + ' uncommitted file(s)' : ''),
            g.in_sync === false ? 'bad' : (g.in_sync ? 'ok' : ''));
  const d = s.db;
  c += card('Database', d.error ? 'error' : (d.size_mb + ' MB'),
            d.error ? esc(d.error) : ('modified ' + esc(d.mtime) + '<br>' + d.transactions
            + ' transactions &middot; latest event ' + esc(d.latest_event)
            + '<br>' + d.players + ' players &middot; ' + d.rostered_2025 + ' rostered (2025)'),
            d.error ? 'bad' : '');
  c += card('2026 ADP file', s.files.adp_2026_csv || 'missing',
            'FantasyPros superflex CSV (last saved)');
  c += card('Last recon report', s.files.recon_report || 'never',
            'recon_ownership_report.csv');
  c += card('Last unmatched review', s.files.adp_unmatched || 'never',
            'adp_unmatched.csv (regenerated by Match ADP)');
  document.getElementById('cards').innerHTML = c;

  document.querySelector('#adp tbody').innerHTML = s.adp.map(a =>
    '<tr><td>'+a.season+'</td><td>'+esc(a.source)+'</td><td>'+esc(a.fetched)+'</td><td>'
    + a.total+'</td><td>'+a.matched+'</td><td'+(a.unmatched>30?' class="bad"':'')+'>'
    + a.unmatched+'</td></tr>').join('');

  document.querySelector('#runs tbody').innerHTML = (s.runs||[]).map(r =>
    '<tr><td class="when">'+esc(r.when)+'</td><td>'+esc(r.action)+'</td><td>'
    + '<span class="pill '+(r.ok?'ok':'bad')+'">'+(r.ok?'OK':'FAILED')+'</span></td><td>'
    + esc(r.summary)+'</td></tr>').join('') || '<tr><td colspan="4">No panel runs yet.</td></tr>';
}

function buildButtons() {
  document.getElementById('btns').innerHTML = Object.entries(ACTIONS).map(([id, a]) =>
    '<button '+(id==='refresh'?'class="primary" ':'')+'data-id="'+id+'">'+esc(a.label)+'</button>'
  ).join('');
  document.querySelectorAll('#btns button').forEach(b => b.onclick = () => runAction(b));
}

async function runAction(btn) {
  const id = btn.dataset.id; let body = {action: id};
  if (ACTIONS[id].takes_msg) {
    const msg = prompt('Commit message for this publish:', 'Dashboard refresh');
    if (msg === null) return;
    if (!confirm('This will regenerate AND push to the live site. Go?')) return;
    body.message = msg;
  }
  const all = document.querySelectorAll('#btns button');
  all.forEach(b => b.disabled = true);
  const out = document.getElementById('console');
  out.style.display = 'block';
  out.textContent = '… running ' + ACTIONS[id].label + ' …';
  try {
    const r = await fetch('/api/run', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    out.textContent = j.output + '\\n\\n' + (j.ok ? '=== done (exit 0) ===' : '=== FAILED (exit '+j.rc+') ===');
  } catch (e) { out.textContent = 'Request failed: ' + e; }
  all.forEach(b => b.disabled = false);
  refreshStatus();
}

document.getElementById('stop').onclick = async (e) => {
  e.preventDefault();
  if (!confirm('Stop the panel server? The page will go dead until you relaunch it.')) return;
  try { await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'__shutdown__'})}); } catch(_) {}
  document.body.innerHTML = '<div style="font-family:Inter,sans-serif;padding:60px 40px;color:#606C71">'
    + 'Panel server stopped. Double-click <b>Start Control Panel</b> to bring it back.</div>';
};

buildButtons(); refreshStatus(); setInterval(refreshStatus, 30000);
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
                {k: {"label": v["label"], "takes_msg": v.get("takes_msg", False)}
                 for k, v in ACTIONS.items()})).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._json(build_status())
        else:
            self.send_error(404)

    def do_POST(self):
        if self._deny_remote():
            return
        if urlparse(self.path).path != "/api/run":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"ok": False, "rc": -1, "output": "Bad request"}, 400)
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
            r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=600)
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
