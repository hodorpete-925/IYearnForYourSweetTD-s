"""
generate_dashboard.py — Phase C: build dashboard.html from fantasy.db.

Output is a single self-contained HTML file with a sidebar of teams and a main
pane that swaps content on click. Default view is the league summary. Designed
to be opened in any browser or hosted via GitHub Pages.

Visual style: Pete's Advent Capital brand book (Inter font, blue 600 accents,
white background, left-align everything, sentence case headers).

Run:  python generate_dashboard.py
Out:  dashboard.html
"""

import sqlite3
import html
from datetime import datetime
from pathlib import Path

import compute_drc as drc  # reuse Phase B walk

DB_PATH = Path(__file__).parent / "fantasy.db"
OUT_PATH = Path(__file__).parent / "dashboard.html"
TARGET_SEASON = drc.TARGET_SEASON  # 2026
LEAGUE_NAME = "I Yearn For Your Sweet TD's"

# Manager-name overrides for display only (the underlying DB is unchanged).
# Useful when a manager has left the league and the seat hasn't been refilled.
MANAGER_DISPLAY_NAMES = {
    "Jon Lewitus": "TBD",
}


# ---------- Data assembly ----------------------------------------------------

def build_data():
    """Walk all 2025 final-rosters, compute DRC for each player, return a
    nested dict ready for the template."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    # DRC dollar lookup
    dollar = {r["drc"]: r["drc_dollars"] for r in conn.execute(
        "SELECT drc, drc_dollars FROM drc_dollar_lookup")}

    # Manager → team_name (2025)
    team_names = {r["manager_id"]: r["team_name"] for r in conn.execute(
        "SELECT manager_id, team_name FROM teams WHERE season = 2025")}

    # 2026 ADP per player_id (where available - some players have no ADP match)
    adp_2026 = {r["player_id"]: r["adp"] for r in conn.execute(
        "SELECT player_id, adp FROM adp WHERE season = 2026 AND player_id IS NOT NULL")}

    rosters = conn.execute("""
        SELECT fr.player_id, fr.team_season_id, fr.selected_position,
               p.player_name, p.position, p.nfl_team,
               m.manager_id, m.full_name AS manager
        FROM final_rosters fr
        JOIN players p   ON p.player_id = fr.player_id
        JOIN teams t     ON fr.team_season_id = t.team_season_id
        JOIN managers m  ON t.manager_id = m.manager_id
        WHERE fr.season = 2025
        ORDER BY m.full_name, p.player_name
    """).fetchall()

    by_manager = {}
    failures = []
    for row in rosters:
        result = drc.compute_drc(conn, row["player_id"], row["team_season_id"])
        if result is None:
            failures.append((row["manager"], row["player_name"]))
            continue
        drc_int, _label, chain = result
        drc_dollars = dollar.get(drc_int, 10)

        mgr = row["manager"]
        display = MANAGER_DISPLAY_NAMES.get(mgr, mgr)
        if mgr not in by_manager:
            by_manager[mgr] = {
                "manager": display,
                "manager_actual": mgr,  # keep original for slug stability
                "team_name": team_names.get(row["manager_id"], "(no team)"),
                "players": [],
            }
        by_manager[mgr]["players"].append({
            "name": row["player_name"],
            "position": row["position"] or "—",
            "nfl_team": row["nfl_team"] or "—",
            "drc": drc_int,
            "drc_dollars": drc_dollars,
            "adp_2026": adp_2026.get(row["player_id"]),
            "chain": chain,
        })

    # Sort players within each team by DRC ascending (most expensive first), then name
    for data in by_manager.values():
        data["players"].sort(key=lambda p: (p["drc"], p["name"]))
        data["total_drc_dollars"] = sum(p["drc_dollars"] for p in data["players"])
        data["player_count"] = len(data["players"])
        data["expensive_count"] = sum(1 for p in data["players"] if p["drc"] <= 2)
        data["cheap_count"] = sum(1 for p in data["players"] if p["drc"] >= 10)

    conn.close()
    return by_manager, failures


# ---------- HTML rendering ---------------------------------------------------

def drc_tier_class(drc_int):
    """Pill color class for DRC tier."""
    if drc_int <= 2:
        return "tier-premium"   # DRC 1-2: $100-$200
    if drc_int <= 5:
        return "tier-mid"       # DRC 3-5: $50-$80
    if drc_int <= 9:
        return "tier-value"     # DRC 6-9: $30
    return "tier-cheap"         # DRC 10-16: $10


def _adp_value_class(drc_int, adp):
    """Compare DRC (cost in rounds) to ADP (talent expressed in rounds).

    Pete's framework:
      - DRC is the round you're 'paying' to keep them. DRC 1 = round-1 cost ($200).
        DRC 15 = round-15 cost ($10). Lower DRC = more expensive.
      - ADP is the round they'd naturally go in a draft. ADP 1-12 = round 1,
        13-24 = round 2, etc. Lower ADP = better player.
      - Compare them on the same 'round' scale.

      'overpriced' -> ADP round is LATER than DRC round (paying premium cost
                       for a non-premium talent; you'd get them cheaper by
                       drafting fresh)
      'steal'      -> ADP round is EARLIER than DRC round (paying minimal cost
                       for premium talent; you'd never get them at this cost
                       in a draft)
      'fair'       -> within ~1.5 rounds either way

    NOTE: This is a 12-team-wide heuristic. Once the 2026 draft order is
    finalized, we'll refine to compare against each manager's actual pick
    slot (e.g. for the manager picking 7th, their round-1 pick is overall #7,
    so a DRC 1 keeper costs them their pick #7 specifically).
    """
    if adp is None:
        return ""
    adp_round = adp / 12.0          # ADP overall converted to round number
    delta = adp_round - drc_int     # positive = ADP later than DRC tier
    if delta > 1.5:
        return "overpriced"
    if delta < -1.5:
        return "steal"
    return "fair"


def render_player_row(p):
    adp = p.get("adp_2026")
    adp_display = f"{adp:.1f}" if adp is not None else "—"
    value_tag = _adp_value_class(p["drc"], adp)
    value_pill = ""
    if value_tag:
        labels = {"steal": "Steal", "fair": "Fair", "overpriced": "Overpriced"}
        value_pill = f'<span class="pill value-{value_tag}">{labels[value_tag]}</span>'
    return f"""
        <tr>
          <td class="player-name">{html.escape(p['name'])}</td>
          <td class="meta">{html.escape(p['position'])}</td>
          <td class="meta">{html.escape(p['nfl_team'])}</td>
          <td class="num"><span class="pill {drc_tier_class(p['drc'])}">{p['drc']}</span></td>
          <td class="num cost">${p['drc_dollars']}</td>
          <td class="num">{adp_display}</td>
          <td class="num">{value_pill}</td>
          <td class="chain">{html.escape(p['chain'])}</td>
        </tr>"""


def render_team_section(data, slug):
    pcount = data["player_count"]
    expensive = data["expensive_count"]
    cheap = data["cheap_count"]
    total = data["total_drc_dollars"]
    rows = "".join(render_player_row(p) for p in data["players"])

    return f"""
    <section class="team-section" id="team-{slug}" hidden>
      <div class="eyebrow">Manager</div>
      <h1 class="team-name">{html.escape(data['team_name'])}</h1>
      <p class="manager-name">{html.escape(data['manager'])}</p>

      <div class="kpis">
        <div class="kpi">
          <div class="k">Total 2026 keeper cost</div>
          <div class="v">${total:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Players on roster</div>
          <div class="v">{pcount}</div>
        </div>
        <div class="kpi">
          <div class="k">Premium keepers (DRC ≤ 2)</div>
          <div class="v">{expensive}</div>
        </div>
        <div class="kpi">
          <div class="k">Cheap keepers (DRC ≥ 10)</div>
          <div class="v">{cheap}</div>
        </div>
      </div>

      <h2>Roster</h2>
      <table class="roster">
        <thead>
          <tr>
            <th>Player</th>
            <th>Pos</th>
            <th>NFL</th>
            <th class="num">DRC</th>
            <th class="num">Cost</th>
            <th class="num">2026 ADP</th>
            <th class="num">Value</th>
            <th>Acquisition chain</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tr class="total">
          <td colspan="4">Total committed</td>
          <td class="num cost">${total:,}</td>
          <td colspan="3"></td>
        </tr>
      </table>
    </section>"""


def render_summary_section(by_manager, generated_at):
    teams = sorted(by_manager.values(), key=lambda d: -d["total_drc_dollars"])
    league_total = sum(d["total_drc_dollars"] for d in teams)
    avg = league_total // max(len(teams), 1)
    premium_total = sum(d["expensive_count"] for d in teams)

    rows = ""
    for idx, t in enumerate(teams, 1):
        slug = slugify(t["manager_actual"])
        rows += f"""
          <tr>
            <td class="rank">{idx}</td>
            <td class="player-name"><a href="#" data-target="team-{slug}">{html.escape(t['team_name'])}</a></td>
            <td class="meta">{html.escape(t['manager'])}</td>
            <td class="num">{t['player_count']}</td>
            <td class="num">{t['expensive_count']}</td>
            <td class="num cost">${t['total_drc_dollars']:,}</td>
          </tr>"""

    return f"""
    <section class="team-section" id="summary">
      <div class="eyebrow">League 4416 · {TARGET_SEASON} keeper window</div>
      <h1 class="team-name">League cap commitment</h1>
      <p class="manager-name">Dollars each team will spend to keep their {TARGET_SEASON} keepers.</p>

      <div class="kpis">
        <div class="kpi">
          <div class="k">Total league cap committed</div>
          <div class="v">${league_total:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Average team cap</div>
          <div class="v">${avg:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Premium keepers leaguewide</div>
          <div class="v">{premium_total}</div>
        </div>
        <div class="kpi">
          <div class="k">Teams</div>
          <div class="v">{len(teams)}</div>
        </div>
      </div>

      <h2>Teams ranked by {TARGET_SEASON} cap commitment</h2>
      <table class="roster">
        <thead>
          <tr>
            <th>#</th>
            <th>Team</th>
            <th>Manager</th>
            <th class="num">Players</th>
            <th class="num">Premium</th>
            <th class="num">Total cap</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>

      <p class="footnote">Generated {generated_at} · Source: fantasy.db · DRC algorithm: compute_drc.py</p>
    </section>"""


def slugify(name):
    return name.lower().replace(" ", "-").replace(".", "").replace("'", "")


CSS = r"""
:root {
  --blue-800: #022479;
  --blue-600: #0038FF;
  --blue-400: #269AFF;
  --blue-200: #77CEFF;
  --gold-400: #E1B523;
  --gray-700: #2a2a2e;
  --gray-600: #606C71;
  --gray-500: #8e8e93;
  --gray-200: #ebebed;
  --gray-100: #f5f5f5;
  --gray-50:  #fcfcfd;
  --off-white: #fafafb;
  color-scheme: light;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Helvetica Neue", Arial, sans-serif;
  background: #fff;
  color: #000;
  font-size: 14.5px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  min-height: 100vh;
}

/* --- Sidebar ----------------------------------------------------------- */
.sidebar {
  background: var(--blue-800);
  color: #fff;
  padding: 32px 24px 40px;
  position: sticky;
  top: 0;
  align-self: start;
  height: 100vh;
  overflow-y: auto;
}
.sidebar .brand {
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--blue-200);
  font-weight: 600;
  margin-bottom: 6px;
}
.sidebar .brand-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.25;
  margin-bottom: 4px;
  color: #fff;
}
.sidebar .brand-sub {
  font-size: 11.5px;
  color: var(--blue-200);
  margin-bottom: 36px;
}
.sidebar h3 {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--blue-200);
  margin: 24px 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.nav-link {
  display: block;
  padding: 9px 10px;
  color: rgba(255, 255, 255, 0.82);
  text-decoration: none;
  font-size: 13.5px;
  border-radius: 4px;
  margin-bottom: 1px;
  cursor: pointer;
}
.nav-link:hover { background: rgba(255, 255, 255, 0.08); color: #fff; }
.nav-link.active {
  background: var(--blue-600);
  color: #fff;
  font-weight: 500;
}
.nav-link .manager {
  display: block;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.55);
  margin-top: 1px;
}
.nav-link.active .manager { color: rgba(255, 255, 255, 0.75); }

/* --- Main content ------------------------------------------------------ */
.content {
  padding: 56px 64px 96px;
  max-width: 1100px;
}

.eyebrow {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 8px;
}
h1.team-name {
  font-size: 32px;
  font-weight: 600;
  letter-spacing: -0.015em;
  margin: 0;
  line-height: 1.15;
  color: #000;
}
.manager-name {
  font-size: 14.5px;
  color: var(--gray-600);
  margin: 10px 0 0;
}

h2 {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 56px 0 16px;
  color: #000;
}

/* --- KPI cards --------------------------------------------------------- */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-top: 36px;
}
.kpi {
  padding: 18px 20px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--gray-50);
}
.kpi .k {
  font-size: 10.5px;
  color: var(--gray-500);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-weight: 600;
  line-height: 1.3;
}
.kpi .v {
  font-size: 26px;
  font-weight: 600;
  margin-top: 10px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
  color: var(--blue-800);
}

/* --- Tables ------------------------------------------------------------ */
table.roster {
  width: 100%;
  border-collapse: collapse;
  margin-top: 8px;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
table.roster th {
  font-size: 10.5px;
  color: var(--gray-500);
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding: 12px 10px;
  border-bottom: 1.5px solid var(--gray-200);
  text-align: left;
}
table.roster th.num { text-align: right; }
table.roster td {
  padding: 12px 10px;
  border-bottom: 1px solid var(--gray-100);
  vertical-align: middle;
}
table.roster td.num { text-align: right; }
table.roster td.player-name { font-weight: 500; color: #000; }
table.roster td.player-name a {
  color: var(--blue-600);
  text-decoration: none;
}
table.roster td.player-name a:hover { text-decoration: underline; }
table.roster td.meta { color: var(--gray-600); font-size: 13px; }
table.roster td.chain { color: var(--gray-600); font-size: 12.5px; }
table.roster td.cost { font-weight: 500; }
table.roster td.rank { color: var(--gray-500); width: 32px; }

table.roster tr.total td {
  border-top: 1.5px solid #000;
  border-bottom: 1.5px solid #000;
  font-weight: 600;
  padding-top: 14px;
  padding-bottom: 14px;
}

/* --- Pills (DRC tier) -------------------------------------------------- */
.pill {
  display: inline-block;
  padding: 2px 11px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  min-width: 28px;
  text-align: center;
}
.pill.tier-premium { background: #0038FF; color: #fff; }
.pill.tier-mid     { background: var(--blue-200); color: var(--blue-800); }
.pill.tier-value   { background: #fff8e1; color: #8a6a1a; }
.pill.tier-cheap   { background: var(--gray-100); color: var(--gray-600); }

.pill.value-steal      { background: #eef7ee; color: #1d6b3a; }
.pill.value-fair       { background: var(--gray-100); color: var(--gray-600); }
.pill.value-overpriced { background: #fff0e6; color: #b04a00; }

/* --- Footnote ---------------------------------------------------------- */
.footnote {
  margin-top: 48px;
  padding-top: 18px;
  border-top: 1px solid var(--gray-200);
  font-size: 11.5px;
  color: var(--gray-500);
  font-style: italic;
}
"""

JS = r"""
(function() {
  const links = document.querySelectorAll('.nav-link');
  const sections = document.querySelectorAll('.team-section');

  function show(targetId) {
    sections.forEach(s => s.hidden = (s.id !== targetId));
    links.forEach(l => l.classList.toggle('active', l.dataset.target === targetId));
    window.scrollTo({top: 0, behavior: 'instant'});
  }

  // Sidebar nav
  links.forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      show(link.dataset.target);
    });
  });

  // Inline team links in summary table
  document.querySelectorAll('a[data-target]').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      show(a.dataset.target);
    });
  });

  // Default: summary
  show('summary');
})();
"""


def build_sidebar(by_manager):
    teams = sorted(by_manager.values(), key=lambda d: d["team_name"].lower())
    items = ''.join(
        f'<a class="nav-link" data-target="team-{slugify(t["manager_actual"])}">'
        f'{html.escape(t["team_name"])}'
        f'<span class="manager">{html.escape(t["manager"])}</span>'
        f'</a>'
        for t in teams
    )
    items = ''.join(
        f'<a class="nav-link" data-target="team-{slugify(t["manager_actual"])}">'
        f'{html.escape(t["team_name"])}'
        f'<span class="manager">{html.escape(t["manager"])}</span>'
        f'</a>'
        for t in teams
    )
    return f"""
    <aside class="sidebar">
      <div class="brand">League 4416</div>
      <div class="brand-title">{html.escape(LEAGUE_NAME)}</div>
      <div class="brand-sub">Keeper ledger - {TARGET_SEASON}</div>

      <h3>League view</h3>
      <a class="nav-link" data-target="summary">Summary &amp; standings</a>

      <h3>Teams</h3>
      {items}
    </aside>"""


def render_html(by_manager, generated_at):
    sidebar = build_sidebar(by_manager)
    summary = render_summary_section(by_manager, generated_at)
    team_sections = "\n".join(
        render_team_section(data, slugify(data["manager_actual"]))
        for name, data in sorted(by_manager.items())
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(LEAGUE_NAME)} - Keeper ledger - {TARGET_SEASON}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="layout">
{sidebar}
<main class="content">
{summary}
{team_sections}
</main>
</div>
<script>{JS}</script>
</body>
</html>"""


def main():
    by_manager, failures = build_data()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_out = render_html(by_manager, generated_at)
    OUT_PATH.write_text(html_out, encoding="utf-8")

    print(f"Wrote {OUT_PATH}")
    total_players = sum(len(d["players"]) for d in by_manager.values())
    print(f"  {len(by_manager)} managers, {total_players} players, {len(failures)} failures")
    for mgr, name in failures:
        print(f"  FAILED: {mgr} - {name}")


if __name__ == "__main__":
    main()
