"""
value_board_sections.py - two Manager Tools pages added 2026-10-14 once the
Yahoo API came back:

  render_value_board(rows, meta)       "DRC value board": production vs cost
                                        heatmap (see value_over_cost.py)
  render_transaction_log(conn, by_manager, scores_doc)
                                        "2026 transaction log": every Yahoo
                                        move since the draft, newest first,
                                        with a per-manager FAAB summary

Both return a <section class="team-section" ... hidden> block plus their own
CSS/JS (VB_CSS, VB_JS) that generate_dashboard splices into the page.
"""
import html
import json
from collections import defaultdict

ROW_LABELS = {k: f"DRC {k}" for k in range(1, 17)}
ROW_LABELS["pickup"] = "Pickups"
ROW_SUB = {"pickup": "waiver / FA (DRC 16 by rule)"}


def _esc(s):
    return html.escape(str(s if s is not None else ""))


# --------------------------------------------------------------------------
# DRC value board
# --------------------------------------------------------------------------

def render_value_board(rows, meta, season=2026):
    by_row = defaultdict(list)
    for r in rows:
        by_row[r["row_key"]].append(r)
    managers = sorted({r["manager"] for r in rows})
    positions = ["QB", "RB", "WR", "TE", "K", "DEF"]

    def card(r):
        z = r["z"]
        if z is None:
            cls, style = "vb-card vb-nodata", ""
        else:
            a = 0.12 + 0.55 * abs(z) / meta["z_clip"]
            rgb = "28,122,74" if z >= 0 else "180,35,24"
            cls, style = "vb-card", f' style="background:rgba({rgb},{a:.2f})"'
        fpor = "n/a" if r["weeks"] == 0 else f'{r["fpor"]:+.1f}'
        cost = f'${r["dollars"]}' if r.get("dollars") is not None else ""
        acq = {"kept": "kept", "drafted": "drafted", "trade": "via trade",
               "waiver": "waiver", "fa": "free agent"}.get(r["acq"], r["acq"])
        tip = (f'{r["name"]} ({r["position"]}, {r["nfl_team"]}) | {r["manager"]} | '
               f'DRC {r["drc"]} {cost} {acq}')
        return (f'<div class="{cls}"{style} data-mgr="{_esc(r["manager"])}" data-pos="{r["position"]}" '
                f'data-z="{"" if z is None else z}" data-tip="{_esc(tip)}" '
                f'data-pts="{r["pts"]}" data-ppg="{r["ppg"] if r["ppg"] is not None else ""}" '
                f'data-wk="{r["weeks"]}" data-repl="{r["repl"]}" data-fpor="{r["fpor"]}" '
                f'data-exp="{r["expected"]}" data-res="{r["resid"] if r["resid"] is not None else ""}">'
                f'<div class="vb-name">{_esc(r["name"])}</div>'
                f'<div class="vb-sub">{r["position"]} &middot; {_esc(r["nfl_team"])}</div>'
                f'<div class="vb-mgr">{_esc(r["manager"])}</div>'
                f'<div class="vb-fpor">{fpor}</div>'
                f'</div>')

    row_html = []
    for key in list(range(1, 17)) + ["pickup"]:
        cards = sorted(by_row.get(key, []),
                       key=lambda r: (r["z"] is None, -(r["z"] if r["z"] is not None else 0), -r["fpor"]))
        if not cards:
            continue
        sub = ROW_SUB.get(key, f'{len(cards)} player{"s" if len(cards) != 1 else ""}')
        row_html.append(
            f'<div class="vb-row" data-row="{key}">'
            f'<div class="vb-rowhead"><div class="vb-rowlabel">{ROW_LABELS[key]}</div>'
            f'<div class="vb-rowsub">{sub}</div></div>'
            f'<div class="vb-cards">{"".join(card(r) for r in cards)}</div></div>')

    repl = meta["replacement"]
    repl_txt = ", ".join(f'{p} {repl.get(p, 0):.1f}' for p in positions)
    curve = meta["curve"]
    mgr_opts = "".join(f'<option value="{_esc(m)}">{_esc(m)}</option>' for m in managers)
    pos_chips = "".join(f'<button type="button" class="vb-chip is-on" data-pos="{p}">{p}</button>' for p in positions)

    return f"""
    <section class="team-section" id="value-board" hidden>
      <header class="section-header">
        <h1 class="section-title">DRC value board</h1>
        <p class="section-sub">Every rostered player, one row per keeper cost, colored by what he has returned against what this league paid for that tier. Through week {meta["weeks"]}. Green is beating the price, red is trailing it. The middle of the scale is the league's own price line, not zero.</p>
      </header>
      <div class="vb-app">
        <div class="vb-filters">
          <label class="vb-filter">Manager
            <select id="vb-mgr"><option value="">All managers</option>{mgr_opts}</select>
          </label>
          <div class="vb-filter vb-chips" id="vb-pos">{pos_chips}</div>
          <label class="vb-filter vb-check"><input type="checkbox" id="vb-hide-nodata"> Hide players with no games yet</label>
        </div>
        <div class="vb-legend">
          <span class="vb-legend-lo">Trailing the price line</span>
          <span class="vb-legend-bar"></span>
          <span class="vb-legend-hi">Beating it</span>
          <span class="vb-legend-nd"><span class="vb-legend-swatch"></span> no games yet</span>
        </div>
        <div class="vb-grid" id="vb-grid">{"".join(row_html)}</div>
        <div class="vb-tip" id="vb-tip" hidden></div>
        <p class="kb-hint">How it is scored. <b>FPOR</b> is fantasy points over replacement, season to date: each recorded week's points minus a replacement-level player's points per game at the position (replacement this week: {repl_txt}). The <b>price line</b> is a straight fit of FPOR against DRC across the {curve["n"]} drafted and kept players with games ({curve["intercept"]:+.1f} at DRC 0, {curve["slope"]:+.2f} per DRC step). A card's number is FPOR; its color is how far FPOR sits from the line in standard deviations, clipped at &plusmn;{meta["z_clip"]:.0f}. Pickups are priced at DRC 16 by rule and shown on their own row. Byes count as zero. Hover a card for the arithmetic.</p>
      </div>
    </section>"""


# --------------------------------------------------------------------------
# 2026 transaction log
# --------------------------------------------------------------------------

def render_transaction_log(conn, by_manager, scores_doc, drafted_ts, season=2026):
    mgr_by_tsid, team_by_tsid, mgr_by_ytid = {}, {}, {}
    # Franchise handoffs (e.g. The Lady Boys: Lewitus -> Keenan) mean the
    # teams.manager_id is not always the id by_manager carries, so match on
    # the 2026 team name first and fall back to manager id.
    mgr_id_to_display = {d["manager_id"]: d["manager"] for d in by_manager.values()}
    name_to_display = {d.get("team_name"): d["manager"] for d in by_manager.values()}
    for tsid, mid, tname, ytid in conn.execute(
            "SELECT team_season_id, manager_id, team_name, yahoo_team_id FROM teams WHERE season=?", (season,)):
        mgr_by_tsid[tsid] = name_to_display.get(tname) or mgr_id_to_display.get(mid, tname)
        team_by_tsid[tsid] = tname
        mgr_by_ytid[ytid] = mgr_by_tsid[tsid]
    pname = {}
    for pid, n, pos in conn.execute("SELECT player_id, player_name, position FROM players"):
        pname[pid] = (n, pos)

    txns = conn.execute("""
        SELECT transaction_id, yahoo_transaction_id, timestamp, event_type, faab_bid
        FROM transactions WHERE season=? AND status='successful' AND timestamp > ?
        ORDER BY timestamp DESC, yahoo_transaction_id DESC""", (season, drafted_ts)).fetchall()
    legs = defaultdict(list)
    for tid, pid, d, tsid, st, dt, cp in conn.execute("""
        SELECT tp.transaction_id, tp.player_id, tp.direction, tp.team_season_id,
               tp.source_type, tp.destination_type, tp.counterparty_team_season_id
        FROM transaction_players tp JOIN transactions t ON t.transaction_id=tp.transaction_id
        WHERE t.season=? AND t.status='successful'""", (season,)):
        legs[tid].append((pid, d, tsid, st, dt, cp))
    picks = defaultdict(list)
    for tid, rd, s, dst in conn.execute("""
        SELECT tk.transaction_id, tk.draft_round, tk.source_team_season_id, tk.destination_team_season_id
        FROM transaction_picks tk JOIN transactions t ON t.transaction_id=tk.transaction_id
        WHERE t.season=?""", (season,)):
        picks[tid].append((rd, s, dst))

    summary = defaultdict(lambda: {"adds": 0, "drops": 0, "trades": 0, "faab": 0, "bids": 0})
    rows_html = []
    for t in txns:
        tid, yid, ts, et, faab = t
        L = legs.get(tid, [])
        if not L and et == "commish":
            continue
        date = str(ts)[:10]
        if et == "trade":
            sides = defaultdict(lambda: {"in": [], "out": []})
            for pid, d, tsid, st, dt, cp in L:
                n, pos = pname.get(pid, (str(pid), ""))
                sides[tsid]["in"].append(f"{n} ({pos})")
                if cp is not None:
                    sides[cp]["out"].append(f"{n} ({pos})")
            for rd, s, dst in picks.get(tid, []):
                sides[dst]["in"].append(f"R{rd} pick")
                sides[s]["out"].append(f"R{rd} pick")
            parts = []
            for tsid, io in sides.items():
                mgr = mgr_by_tsid.get(tsid, "?")
                summary[mgr]["trades"] += 1
                parts.append(f'<div class="tl-side"><b>{_esc(mgr)}</b> gets {_esc(", ".join(io["in"]) or "nothing")}</div>')
            mgrs = " &harr; ".join(_esc(mgr_by_tsid.get(k, "?")) for k in sides)
            rows_html.append(
                f'<tr class="tl-row" data-type="trade" data-mgr="{"|".join(_esc(mgr_by_tsid.get(k, "")) for k in sides)}">'
                f'<td class="tl-date">{date}</td><td><span class="tl-badge tl-trade">Trade</span></td>'
                f'<td class="tl-mgr">{mgrs}</td><td class="tl-detail">{"".join(parts)}</td><td class="tl-faab"></td></tr>')
            continue
        # add / drop / add-drop: one team
        tsid = next((tsid for pid, d, tsid, *_ in L if tsid is not None), None)
        mgr = mgr_by_tsid.get(tsid, "?")
        ins, outs, src = [], [], None
        for pid, d, tsid2, st, dt, cp in L:
            n, pos = pname.get(pid, (str(pid), ""))
            if d == "incoming":
                ins.append(f"{n} ({pos})"); src = st
            else:
                outs.append(f"{n} ({pos})")
        if ins:
            summary[mgr]["adds"] += 1
        if outs:
            summary[mgr]["drops"] += 1
        if faab:
            summary[mgr]["faab"] += int(faab); summary[mgr]["bids"] += 1
        kind = "add/drop" if ins and outs else ("add" if ins else "drop")
        badge = {"add": "tl-add", "drop": "tl-drop", "add/drop": "tl-adddrop"}[kind]
        label = {"add": "Add", "drop": "Drop", "add/drop": "Add / drop"}[kind]
        via = " via waivers" if src == "waivers" else (" free agent" if src == "freeagents" else "")
        detail = []
        if ins:
            detail.append(f'<span class="tl-in">+ {_esc(", ".join(ins))}</span><span class="tl-via">{via}</span>')
        if outs:
            detail.append(f'<span class="tl-out">&minus; {_esc(", ".join(outs))}</span>')
        faab_txt = f"${int(faab)}" if faab else ""
        rows_html.append(
            f'<tr class="tl-row" data-type="{kind}" data-mgr="{_esc(mgr)}">'
            f'<td class="tl-date">{date}</td><td><span class="tl-badge {badge}">{label}</span></td>'
            f'<td class="tl-mgr">{_esc(mgr)}</td><td class="tl-detail">{"<br>".join(detail)}</td>'
            f'<td class="tl-faab">{faab_txt}</td></tr>')

    faab_left = {}
    for s in (scores_doc or {}).get("standings", []) or []:
        m = mgr_by_ytid.get(s.get("tid"))
        if m:
            faab_left[m] = s.get("faab")
    managers = sorted({d["manager"] for d in by_manager.values()})
    sum_rows = []
    for m in sorted(managers, key=lambda m: (-(summary[m]["adds"] + summary[m]["drops"] + summary[m]["trades"]), m)):
        s = summary[m]
        left = faab_left.get(m)
        sum_rows.append(
            f'<tr><td>{_esc(m)}</td><td class="num">{s["adds"]}</td><td class="num">{s["drops"]}</td>'
            f'<td class="num">{s["trades"]}</td><td class="num">{s["bids"]}</td>'
            f'<td class="num">${s["faab"]}</td><td class="num">{"" if left is None else f"${left}"}</td></tr>')
    mgr_opts = "".join(f'<option value="{_esc(m)}">{_esc(m)}</option>' for m in managers)
    n_moves = len(rows_html)
    as_of = (scores_doc or {}).get("as_of", "")

    return f"""
    <section class="team-section" id="transaction-log" hidden>
      <header class="section-header">
        <h1 class="section-title">{season} transaction log</h1>
        <p class="section-sub">Every move on Yahoo since the draft, newest first: {n_moves} waiver claims, free-agent adds, drops and trades. FAAB balances as of {_esc(as_of)}.</p>
      </header>
      <div class="tl-app">
        <h2 class="tl-h2">By manager</h2>
        <table class="tl-summary">
          <thead><tr><th>Manager</th><th class="num">Adds</th><th class="num">Drops</th><th class="num">Trades</th><th class="num">Bids won</th><th class="num">FAAB spent</th><th class="num">FAAB left</th></tr></thead>
          <tbody>{"".join(sum_rows)}</tbody>
        </table>
        <h2 class="tl-h2">All moves</h2>
        <div class="vb-filters">
          <label class="vb-filter">Manager
            <select id="tl-mgr"><option value="">All managers</option>{mgr_opts}</select>
          </label>
          <div class="vb-filter vb-chips" id="tl-type">
            <button type="button" class="vb-chip is-on" data-type="add">Adds</button>
            <button type="button" class="vb-chip is-on" data-type="add/drop">Add / drop</button>
            <button type="button" class="vb-chip is-on" data-type="drop">Drops</button>
            <button type="button" class="vb-chip is-on" data-type="trade">Trades</button>
          </div>
        </div>
        <table class="tl-table">
          <thead><tr><th>Date</th><th>Type</th><th>Manager</th><th>Detail</th><th class="num">Bid</th></tr></thead>
          <tbody id="tl-body">{"".join(rows_html)}</tbody>
        </table>
        <p class="kb-hint">Source: Yahoo Fantasy Sports API, pulled on every refresh. The commissioner's draft-day Walker/Love swap is recorded as an amended draft, not as moves. Pre-draft trades live on the {season} off-season trades page.</p>
      </div>
    </section>"""


VB_CSS = """
/* --- DRC value board + transaction log (2026-10-14) --------------------- */
.vb-app, .tl-app { max-width: 1240px; }
.vb-filters { display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: center; margin: 4px 0 14px; }
.vb-filter { font-size: 12.5px; color: var(--gray-600); display: flex; align-items: center; gap: 8px; }
.vb-filter select { font: inherit; font-size: 13px; padding: 5px 8px; border: 1px solid var(--gray-200); border-radius: 6px; background: #fff; color: #000; }
.vb-chips { gap: 6px; }
.vb-chip { font: inherit; font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--gray-200); background: #fff; color: var(--gray-500); cursor: pointer; }
.vb-chip.is-on { background: var(--blue-800); border-color: var(--blue-800); color: #fff; }
.vb-check input { margin: 0; }
.vb-legend { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--gray-600); margin: 0 0 16px; flex-wrap: wrap; }
.vb-legend-bar { display: inline-block; width: 180px; height: 10px; border-radius: 5px;
  background: linear-gradient(90deg, rgba(180,35,24,.67), rgba(180,35,24,.12) 42%, #f0f0f2 50%, rgba(28,122,74,.12) 58%, rgba(28,122,74,.67)); }
.vb-legend-nd { margin-left: 14px; display: inline-flex; align-items: center; gap: 6px; }
.vb-legend-swatch { display: inline-block; width: 14px; height: 10px; border-radius: 3px; background: var(--gray-100); border: 1px dashed var(--gray-200); }
.vb-grid { display: flex; flex-direction: column; gap: 6px; }
.vb-row { display: grid; grid-template-columns: 92px 1fr; gap: 10px; align-items: start; padding: 6px 0; border-top: 1px solid var(--gray-100); }
.vb-rowhead { position: sticky; left: 0; }
.vb-rowlabel { font-size: 13px; font-weight: 700; color: var(--gray-700); letter-spacing: 0.01em; }
.vb-rowsub { font-size: 11px; color: var(--gray-500); line-height: 1.3; margin-top: 2px; }
.vb-cards { display: flex; flex-wrap: wrap; gap: 6px; }
.vb-card { width: 128px; min-height: 68px; padding: 7px 8px 6px; border-radius: 8px; background: var(--gray-100); color: #000; cursor: default; transition: transform .08s ease; border: 1px solid rgba(0,0,0,.04); }
.vb-card:hover { transform: translateY(-1px); box-shadow: 0 2px 8px rgba(0,0,0,.14); }
.vb-card.vb-nodata { background: var(--gray-50); border: 1px dashed var(--gray-200); color: var(--gray-600); }
.vb-card.is-dim { opacity: .18; }
.vb-card.is-hidden { display: none; }
.vb-name { font-size: 12.5px; font-weight: 600; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vb-sub { font-size: 10.5px; color: rgba(0,0,0,.62); margin-top: 1px; }
.vb-nodata .vb-sub, .vb-nodata .vb-mgr { color: var(--gray-500); }
.vb-mgr { font-size: 10.5px; color: rgba(0,0,0,.72); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vb-fpor { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 3px; letter-spacing: -0.01em; }
.vb-tip { position: fixed; z-index: 60; pointer-events: none; background: var(--gray-700); color: #fff; font-size: 12px; line-height: 1.45; padding: 9px 11px; border-radius: 8px; box-shadow: 0 6px 20px rgba(0,0,0,.25); max-width: 300px; }
.vb-tip b { color: #fff; }
.vb-tip .vb-tip-row { display: flex; justify-content: space-between; gap: 16px; }
.vb-tip .vb-tip-row span:last-child { font-variant-numeric: tabular-nums; }
.vb-tip .vb-tip-head { font-weight: 600; margin-bottom: 4px; }
.vb-tip .vb-tip-sub { color: rgba(255,255,255,.72); margin-bottom: 6px; }
@media (max-width: 720px) {
  .vb-row { grid-template-columns: 1fr; gap: 4px; }
  .vb-rowhead { display: flex; gap: 8px; align-items: baseline; }
  .vb-card { width: calc(50% - 3px); }
}
.tl-h2 { font-size: 15px; font-weight: 600; margin: 18px 0 8px; color: var(--gray-700); }
.tl-summary, .tl-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.tl-summary th, .tl-table th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--gray-500); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--gray-200); }
.tl-summary td, .tl-table td { padding: 7px 8px; border-bottom: 1px solid var(--gray-100); vertical-align: top; }
.tl-summary .num, .tl-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.tl-date { white-space: nowrap; color: var(--gray-600); font-variant-numeric: tabular-nums; }
.tl-mgr { white-space: nowrap; font-weight: 600; }
.tl-faab { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.tl-badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
.tl-add { background: rgba(28,122,74,.14); color: #1c7a4a; }
.tl-drop { background: rgba(180,35,24,.12); color: #b42318; }
.tl-adddrop { background: rgba(2,36,121,.10); color: var(--blue-800); }
.tl-trade { background: rgba(225,181,35,.22); color: #7a5d05; }
.tl-in { color: #1c7a4a; font-weight: 600; }
.tl-out { color: #b42318; }
.tl-via { color: var(--gray-500); font-size: 12px; }
.tl-side { line-height: 1.4; }
.tl-row.is-hidden { display: none; }
@media (max-width: 720px) {
  .tl-table th:nth-child(2), .tl-table td:nth-child(2) { display: none; }
  .tl-summary { font-size: 12px; }
}
"""

VB_JS = """
(function () {
  // ---- DRC value board: filters + hover arithmetic -----------------------
  const grid = document.getElementById('vb-grid');
  if (grid) {
    const sel = document.getElementById('vb-mgr');
    const chips = Array.from(document.querySelectorAll('#vb-pos .vb-chip'));
    const hideNd = document.getElementById('vb-hide-nodata');
    const tip = document.getElementById('vb-tip');
    function apply() {
      const mgr = sel.value;
      const onPos = new Set(chips.filter(c => c.classList.contains('is-on')).map(c => c.dataset.pos));
      grid.querySelectorAll('.vb-card').forEach(card => {
        const posOk = onPos.has(card.dataset.pos);
        const ndOk = !(hideNd.checked && card.dataset.z === '');
        card.classList.toggle('is-hidden', !(posOk && ndOk));
        card.classList.toggle('is-dim', !!mgr && card.dataset.mgr !== mgr);
      });
      grid.querySelectorAll('.vb-row').forEach(row => {
        const any = row.querySelector('.vb-card:not(.is-hidden)');
        row.style.display = any ? '' : 'none';
      });
    }
    sel.addEventListener('change', apply);
    hideNd.addEventListener('change', apply);
    chips.forEach(c => c.addEventListener('click', () => { c.classList.toggle('is-on'); apply(); }));
    function fmt(v, d) { if (v === '' || v === undefined || v === null) return 'n/a'; const n = Number(v); return (d === undefined ? n : n.toFixed(d)); }
    function signed(v, d) { if (v === '' || v === undefined) return 'n/a'; const n = Number(v); return (n >= 0 ? '+' : '') + n.toFixed(d); }
    grid.addEventListener('mousemove', e => {
      const card = e.target.closest('.vb-card');
      if (!card) { tip.hidden = true; return; }
      const d = card.dataset;
      tip.innerHTML = '<div class="vb-tip-head">' + d.tip.split(' | ')[0] + '</div>' +
        '<div class="vb-tip-sub">' + d.tip.split(' | ').slice(1).join(' &middot; ') + '</div>' +
        '<div class="vb-tip-row"><span>Games recorded</span><span>' + fmt(d.wk) + '</span></div>' +
        '<div class="vb-tip-row"><span>Points</span><span>' + fmt(d.pts, 1) + '</span></div>' +
        '<div class="vb-tip-row"><span>Per game</span><span>' + fmt(d.ppg, 2) + '</span></div>' +
        '<div class="vb-tip-row"><span>Replacement per game</span><span>' + fmt(d.repl, 2) + '</span></div>' +
        '<div class="vb-tip-row"><span><b>FPOR</b></span><span><b>' + signed(d.fpor, 1) + '</b></span></div>' +
        '<div class="vb-tip-row"><span>Price line at this DRC</span><span>' + signed(d.exp, 1) + '</span></div>' +
        '<div class="vb-tip-row"><span>Vs. line</span><span>' + signed(d.res, 1) + (d.z === '' ? '' : ' (' + signed(d.z, 2) + ' sd)') + '</span></div>';
      tip.hidden = false;
      const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
      let x = e.clientX + pad, y = e.clientY + pad;
      if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
      if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    });
    grid.addEventListener('mouseleave', () => { tip.hidden = true; });
  }
  // ---- transaction log: filters ------------------------------------------
  const body = document.getElementById('tl-body');
  if (body) {
    const sel = document.getElementById('tl-mgr');
    const chips = Array.from(document.querySelectorAll('#tl-type .vb-chip'));
    function apply() {
      const mgr = sel.value;
      const on = new Set(chips.filter(c => c.classList.contains('is-on')).map(c => c.dataset.type));
      body.querySelectorAll('.tl-row').forEach(r => {
        const mgrOk = !mgr || r.dataset.mgr.split('|').includes(mgr);
        r.classList.toggle('is-hidden', !(mgrOk && on.has(r.dataset.type)));
      });
    }
    sel.addEventListener('change', apply);
    chips.forEach(c => c.addEventListener('click', () => { c.classList.toggle('is-on'); apply(); }));
  }
})();
"""
