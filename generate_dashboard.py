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

import json
import sqlite3
import html
from datetime import datetime
from pathlib import Path

import compute_drc as drc  # reuse Phase B walk
import player_history as hist  # per-year history helper
import draft_history as drafth  # per-manager draft picks
import trade_history as tradeh  # per-manager trade events with points outcomes

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

    # Pre-built lookup dicts for per-year history (ADP all years, points, pos rank)
    adp_by_year = hist.load_adp_by_year(conn)
    pts_by_year = hist.load_season_points_by_player(conn)
    pos_rank_by_year = hist.load_position_rank_by_player(conn)
    pos_rank_neighbors = hist.load_pos_rank_neighbors(conn)

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
                "manager_id": row["manager_id"],
                "team_name": team_names.get(row["manager_id"], "(no team)"),
                "players": [],
                "draft_history": drafth.build_draft_history_for_manager(
                    conn, row["manager_id"], adp_by_year
                ),
                "trade_history": tradeh.build_trade_history_for_manager(
                    conn, row["manager_id"],
                    drc.get_manager_team_ids(conn, row["manager_id"]),
                    pts_by_year,
                ),
            }
        history = hist.build_history_for_player(
            conn, row["player_id"], row["team_season_id"],
            adp_by_year, pts_by_year, pos_rank_by_year,
            player_position=row["position"],
        )
        by_manager[mgr]["players"].append({
            "player_id": row["player_id"],
            "name": row["player_name"],
            "position": row["position"] or "—",
            "nfl_team": row["nfl_team"] or "—",
            "drc": drc_int,
            "drc_dollars": drc_dollars,
            "adp_2026": adp_2026.get(row["player_id"]),
            "chain": chain,
            "history": history,
        })

    # Sort players within each team by DRC ascending (most expensive first), then name
    for data in by_manager.values():
        data["players"].sort(key=lambda p: (p["drc"], p["name"]))
        data["total_drc_dollars"] = sum(p["drc_dollars"] for p in data["players"])
        data["player_count"] = len(data["players"])
        data["expensive_count"] = sum(1 for p in data["players"] if p["drc"] <= 2)
        data["cheap_count"] = sum(1 for p in data["players"] if p["drc"] >= 10)

    # League-wide player search dataset: every player that's touched a roster,
    # a draft, or a transaction in our data. Each entry is enriched with a
    # full transaction log, a per-year DRC trajectory, a 2025 fantasy summary,
    # an ownership lineage, and weekly fantasy point sparklines.

    # Pre-load weekly fantasy points: {(pid, season): {week: pts}}
    weekly_pts = {}
    for r in conn.execute("""
        SELECT player_id, season, week, fantasy_points
        FROM player_weekly_stats
        WHERE fantasy_points IS NOT NULL
    """):
        pid_, season_, week_, pts_ = r
        weekly_pts.setdefault((pid_, season_), {})[week_] = pts_

    far_future = "2099-12-31"
    search_players = []
    seen_pids = set()
    rows = conn.execute("""
        SELECT DISTINCT p.player_id, p.player_name, p.position, p.nfl_team
        FROM players p
        WHERE p.player_id IN (
            SELECT player_id FROM final_rosters
            UNION SELECT player_id FROM draft_picks
            UNION SELECT player_id FROM transaction_players
        )
        ORDER BY p.player_name
    """).fetchall()
    for row in rows:
        pid = row["player_id"]
        if pid in seen_pids:
            continue
        seen_pids.add(pid)

        events = drafth.build_transaction_log_for_player(conn, pid, far_future)

        # Current owner heading into TARGET_SEASON
        owner_team = hist.get_owner_at_year_end(conn, pid, TARGET_SEASON - 1)
        owner_name = None
        if owner_team:
            owner_row = conn.execute(
                "SELECT m.full_name FROM teams t JOIN managers m "
                "ON m.manager_id = t.manager_id WHERE t.team_season_id = ?",
                (owner_team,),
            ).fetchone()
            if owner_row:
                owner_name = MANAGER_DISPLAY_NAMES.get(
                    owner_row["full_name"], owner_row["full_name"]
                )

        # Per-year DRC trajectory (2023..2026): reuse build_history_for_player
        # but pass the current team if we have one (otherwise use 0 sentinel).
        per_year_history = hist.build_history_for_player(
            conn, pid, owner_team or 0,
            adp_by_year, pts_by_year, pos_rank_by_year,
            player_position=row["position"],
        )
        per_year = []
        for y in (2023, 2024, 2025, 2026):
            h = per_year_history.get(y)
            if h is None:
                per_year.append({"year": y, "drc": None, "dollars": None,
                                 "owner": None, "pts": None, "pos_rank": None})
                continue
            yr_owner_id = h.get("owner_team_id")
            yr_owner_name = None
            if yr_owner_id:
                r = conn.execute(
                    "SELECT m.full_name FROM teams t JOIN managers m "
                    "ON m.manager_id = t.manager_id WHERE t.team_season_id = ?",
                    (yr_owner_id,),
                ).fetchone()
                if r:
                    yr_owner_name = MANAGER_DISPLAY_NAMES.get(r[0], r[0])
            drc_v = h.get("drc")
            per_year.append({
                "year": y,
                "drc": drc_v,
                "dollars": dollar.get(drc_v) if drc_v else None,
                "owner": yr_owner_name,
                "pts": h.get("pts"),
                "pos_rank": h.get("pos_rank"),
                "adp": h.get("adp"),
            })

        # Lineage: distinct ownership periods. Walk through events
        # chronologically and create one lineage node per (manager change)
        # OR original draft, capturing how they acquired the player.
        lineage = []
        last_manager = None
        for e in events:
            kind = e.get("kind", "")
            desc = e.get("desc", "")
            date = e.get("date", "")
            # Extract manager name from description (formats vary by kind)
            # Drafted R{n} by {mgr}, Kept by {mgr} (slot R{n}), Traded {src} -> {dst},
            # Waiver claim by {mgr}, Free agent add by {mgr}
            if kind == "draft":
                # "Drafted R{n} by {mgr}"
                mgr = desc.split(" by ")[-1] if " by " in desc else "?"
                if mgr != last_manager:
                    lineage.append({"date": date, "manager": mgr,
                                    "method": "Drafted",
                                    "detail": desc.split(" by ")[0]})
                    last_manager = mgr
            elif kind == "kept":
                continue  # not an ownership change
            elif kind == "trade":
                # "Traded {src} -> {dst}"
                if " → " in desc:
                    parts = desc.replace("Traded ", "").split(" → ")
                    mgr = parts[-1] if len(parts) == 2 else "?"
                else:
                    mgr = "?"
                if mgr != last_manager:
                    lineage.append({"date": date, "manager": mgr,
                                    "method": "Trade", "detail": desc})
                    last_manager = mgr
            else:
                mgr = desc.split(" by ")[-1] if " by " in desc else "?"
                if mgr != last_manager:
                    method = "Waiver" if "Waiver" in desc else (
                        "Free agent" if "Free agent" in desc else kind.title())
                    lineage.append({"date": date, "manager": mgr,
                                    "method": method, "detail": desc})
                    last_manager = mgr

        # 2025 fantasy finish for the hero card
        pts_2025 = pts_by_year.get((pid, 2025))
        rank_2025 = pos_rank_by_year.get((pid, 2025))
        adp_2026 = conn.execute(
            "SELECT adp FROM adp WHERE player_id = ? AND season = 2026 LIMIT 1",
            (pid,),
        ).fetchone()

        # Weekly fantasy points for the sparkline, per year
        weekly_by_year = {yr: weekly_pts.get((pid, yr), {}) for yr in (2023, 2024, 2025)}

        # Position rank neighbors per year: ranks {N-2, N-1, N, N+1, N+2}
        # within the same position group, plus self for highlighting.
        neighbors_by_year = {}
        pos_for_lookup = row["position"]
        for yr in (2023, 2024, 2025):
            r = pos_rank_by_year.get((pid, yr))
            if r is None or not pos_for_lookup:
                continue
            nbs = []
            for delta in (-2, -1, 0, 1, 2):
                target = r + delta
                if target < 1:
                    continue
                hit = pos_rank_neighbors.get((yr, pos_for_lookup, target))
                if hit:
                    n_pid, n_name, n_pts = hit
                    nbs.append({
                        "label": f"{pos_for_lookup}{target}",
                        "name": n_name,
                        "pts": n_pts,
                        "is_self": (delta == 0),
                    })
            if nbs:
                neighbors_by_year[yr] = nbs

        search_players.append({
            "player_id": pid,
            "name": row["player_name"],
            "position": row["position"] or "—",
            "nfl_team": row["nfl_team"] or "—",
            "current_owner": owner_name,
            "pts_2025": pts_2025,
            "pos_rank_2025": rank_2025,
            "adp_2026": adp_2026[0] if adp_2026 else None,
            "per_year": per_year,
            "lineage": lineage,
            "events": events,
            "weekly_by_year": weekly_by_year,
            "neighbors_by_year": neighbors_by_year,
        })

    conn.close()
    return by_manager, failures, search_players


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


def _fmt(x, decimals=1):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{decimals}f}"
    return str(x)


def _round_or_drc_label(yr_record):
    """For an expanded-year cell: show 'R4' if drafted that year, 'DRC 3'
    if kept, '—' if not in our league."""
    if not yr_record:
        return "—"
    if yr_record.get("draft_round") is not None:
        return f"R{yr_record['draft_round']}"
    return f"DRC {yr_record['drc']}"


def _format_pos_rank(rec):
    """Combine position code + rank: 'WR15', 'RB2', 'QB1', 'DEF10'.
    Returns em-dash if not available."""
    if not rec:
        return "—"
    rank = rec.get("pos_rank")
    pos = rec.get("position")
    if rank is None or pos is None:
        return "—"
    return f"{pos}{rank}"


def _pos_rank_tier(rec):
    """Classify pos rank into a color tier for visual styling.
    Advent palette: green for top tier, gold for mid, red for low."""
    if not rec:
        return ""
    rank = rec.get("pos_rank")
    pos = rec.get("position")
    if rank is None or pos is None:
        return ""
    # Position-aware cutoffs (12-team league lens).
    # Top tier = starter-quality, mid = bench-flex, low = bottom-of-roster.
    if pos in ("QB", "TE", "K", "DEF"):
        top_n, mid_n = 12, 24
    elif pos == "RB":
        top_n, mid_n = 24, 48
    elif pos == "WR":
        top_n, mid_n = 36, 60
    else:
        top_n, mid_n = 12, 24
    if rank <= top_n:
        return "tier-top"
    if rank <= mid_n:
        return "tier-mid-perf"
    return "tier-low"


def render_year_card(year, rec):
    """Render one year's data as a vertically-stacked card. Card is greyed
    if the player wasn't in our league that year (rec is None)."""
    empty = rec is None
    extra_class = " card-empty" if empty else ""

    round_drc = _round_or_drc_label(rec)
    pos_rank_str = _format_pos_rank(rec)
    pos_rank_tier = _pos_rank_tier(rec)
    pts_str = _fmt(rec.get("pts") if rec else None, 1)
    adp_str = _fmt(rec.get("adp") if rec else None, 1)

    return f"""
        <div class="year-card{extra_class}">
          <div class="year-label">{year}</div>
          <div class="year-metric">
            <span class="m-label">Cost</span>
            <span class="m-val">{round_drc}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">Pos rank</span>
            <span class="m-val pos-rank {pos_rank_tier}">{pos_rank_str}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">Pts</span>
            <span class="m-val">{pts_str}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">ADP</span>
            <span class="m-val">{adp_str}</span>
          </div>
        </div>"""


def render_history_subrow(player_id, history, colspan):
    """Render history as horizontal year-cards (descending: 2025, 2024, 2023).
    2026 isn't here - it's already in the main row."""
    cards = "".join(
        render_year_card(year, history.get(year))
        for year in (2025, 2024, 2023)
    )
    return f"""
        <tr class="history-row" id="hist-{player_id}" hidden>
          <td colspan="{colspan}" class="history-cell">
            <div class="history-cards">{cards}</div>
          </td>
        </tr>"""


def render_player_row(p):
    adp = p.get("adp_2026")
    adp_display = f"{adp:.1f}" if adp is not None else "—"
    value_tag = _adp_value_class(p["drc"], adp)
    value_pill = ""
    if value_tag:
        labels = {"steal": "Steal", "fair": "Fair", "overpriced": "Overpriced"}
        value_pill = f'<span class="pill value-{value_tag}">{labels[value_tag]}</span>'

    pid = p.get("player_id", id(p))
    main_row = f"""
        <tr>
          <td class="player-name">{html.escape(p['name'])}<span class="sub-line">{html.escape(p['position'])} &middot; {html.escape(p['nfl_team'])}</span></td>
          <td class="meta">{html.escape(p['position'])}</td>
          <td class="meta">{html.escape(p['nfl_team'])}</td>
          <td class="num"><span class="pill {drc_tier_class(p['drc'])}">{p['drc']}</span></td>
          <td class="num cost">${p['drc_dollars']}</td>
          <td class="num">{adp_display}</td>
          <td class="num">{value_pill}</td>
          <td class="expand-col">
            <button class="expand-btn" data-target="hist-{pid}" aria-label="Show prior years">›</button>
          </td>
        </tr>"""

    sub_row = render_history_subrow(pid, p.get("history", {}), colspan=8)
    return main_row + sub_row


def _fmt_pts(v):
    return f"{v:.1f}" if v is not None else "—"


def render_trade_points_cell(pts_entry, max_in_year):
    """One cell in the trade-outcome table: full season number + optional
    post-trade portion subtly, plus a mini bar scaled to max_in_year."""
    full = pts_entry["full"]
    post = pts_entry["post_trade"]

    full_str = _fmt_pts(full)
    post_str = f"({_fmt_pts(post)} after)" if post is not None else ""

    if max_in_year and full is not None and max_in_year > 0:
        pct = max(0, min(100, (full / max_in_year) * 100))
    else:
        pct = 0
    bar_html = (
        f'<div class="mini-bar-track"><div class="mini-bar-fill" style="width:{pct:.0f}%"></div></div>'
        if pct > 0 else ""
    )
    post_html = f'<span class="pts-post-trade">{post_str}</span>' if post_str else ""
    return f"""
        <div class="pts-cell">
          <span class="pts-full">{full_str}</span>
          {post_html}
          {bar_html}
        </div>"""


def render_trade_side_table(label, players_list, subtotal, max_per_year):
    if not players_list:
        return f"""
        <div class="trade-side">
          <div class="trade-side-label">{label}</div>
          <p class="empty-note">No players.</p>
        </div>"""
    rows = []
    for p in players_list:
        cells_2023 = render_trade_points_cell(p["points"][2023], max_per_year.get(2023, 0))
        cells_2024 = render_trade_points_cell(p["points"][2024], max_per_year.get(2024, 0))
        cells_2025 = render_trade_points_cell(p["points"][2025], max_per_year.get(2025, 0))
        rows.append(f"""
            <tr>
              <td class="player-name">{html.escape(p['name'])}</td>
              <td class="meta">{html.escape(p['position'])}</td>
              <td class="meta">{html.escape(p['nfl_team'])}</td>
              <td class="num">{cells_2023}</td>
              <td class="num">{cells_2024}</td>
              <td class="num">{cells_2025}</td>
            </tr>""")
    subtotal_row = f"""
        <tr class="subtotal-row">
          <td colspan="3">Subtotal</td>
          <td class="num">{_fmt_pts(subtotal[2023])}</td>
          <td class="num">{_fmt_pts(subtotal[2024])}</td>
          <td class="num">{_fmt_pts(subtotal[2025])}</td>
        </tr>"""
    return f"""
    <div class="trade-side">
      <div class="trade-side-label">{label}</div>
      <table class="trade-table">
        <colgroup>
          <col class="col-player">
          <col class="col-pos">
          <col class="col-nfl">
          <col class="col-year">
          <col class="col-year">
          <col class="col-year">
        </colgroup>
        <thead>
          <tr>
            <th>Player</th>
            <th>Pos</th>
            <th>NFL</th>
            <th class="num">2023</th>
            <th class="num">2024</th>
            <th class="num">2025</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}{subtotal_row}</tbody>
      </table>
    </div>"""


def render_trade_event(trade):
    max_per_year = trade["max_pts_per_year"]
    acquired_table = render_trade_side_table(
        "Acquired", trade["acquired"], trade["subtotal_acquired"], max_per_year
    )
    given_table = render_trade_side_table(
        "Gave up", trade["given_up"], trade["subtotal_given_up"], max_per_year
    )

    return f"""
    <div class="trade-event">
      <div class="trade-header">
        <span class="trade-date">{html.escape(trade['date_display'])}</span>
        <span class="trade-vs">vs {html.escape(trade['counterparty_name'])}</span>
      </div>
      {acquired_table}
      {given_table}
    </div>"""


def render_trades_tab(trade_history, slug):
    if not trade_history:
        return '<p class="empty-note">No trades recorded for this manager.</p>'
    return "".join(render_trade_event(t) for t in trade_history)


def _value_tag_label(tag):
    return {
        "steal":        "Steal",
        "fair":         "Fair",
        "reach":        "Reach",
        "major-reach":  "Major reach",
    }.get(tag, "")


def _format_trajectory(trajectory):
    """Render a trajectory list as 'R15 (2023) → 14 → 13' format."""
    if not trajectory:
        return "—"
    parts = []
    for i, (year, label) in enumerate(trajectory):
        if i == 0:
            parts.append(f"{label} ({year})")
        else:
            parts.append(label)
    return " &rarr; ".join(parts)


def render_draft_pick_row(pick, show_round_cell=False, rowspan=1):
    """Render one pick row. If show_round_cell, prepend the leftmost <td>
    for the round-banding column with the given rowspan."""
    pos = pick.get("position") or "—"
    is_keeper = pick.get("is_keeper", False)
    is_traded_for = pick.get("acquired_via_trade", False)
    if is_traded_for:
        type_label = '<span class="pill pill-traded-for">Traded for</span>'
    elif is_keeper:
        type_label = "K"
    else:
        type_label = "—"

    dround = pick["draft_round"]
    trajectory_cell = _format_trajectory(pick.get("trajectory") or [])

    round_cell = (
        f'<td class="round-cell" rowspan="{rowspan}">{dround}</td>'
        if show_round_cell else ""
    )

    adp = pick.get("adp")
    adp_display = f"{adp:.1f}" if adp is not None else "—"

    tag = pick.get("value_tag") or ""
    label = _value_tag_label(tag)
    value_pill = f'<span class="pill value-{tag}">{label}</span>' if tag else ""

    trade_class = " traded-for" if pick.get("acquired_via_trade") else ""
    txn_log = pick.get("txn_log") or []
    if txn_log:
        log_items = "".join(
            f'<div class="tooltip-event">'
            f'<span class="event-date">{html.escape(e["date"])}</span>'
            f'<span class="event-desc">{html.escape(e["desc"])}</span>'
            f'</div>'
            for e in txn_log
        )
        tooltip_html = (
            '<div class="player-tooltip">'
            f'<div class="tooltip-header">{html.escape(pick["player_name"])} '
            '&middot; transaction log</div>'
            f'{log_items}'
            '</div>'
        )
    else:
        tooltip_html = ""

    return f"""
        <tr class="round-{dround}{trade_class}">
          {round_cell}
          <td class="pick-label num">{pick['overall_pick']}</td>
          <td class="player-name">
            <span class="player-name-link">{html.escape(pick['player_name'])}</span>
            {tooltip_html}
          </td>
          <td class="meta">{html.escape(pos)}</td>
          <td class="meta type-code">{type_label}</td>
          <td class="trajectory-cell">{trajectory_cell}</td>
          <td class="num">{adp_display}</td>
          <td>{value_pill}</td>
        </tr>"""


def render_year_drafts(year, picks, is_default_open, slug):
    if not picks:
        body = '<p class="empty-note">No draft data for this year.</p>'
    else:
        # Group picks by round so we can rowspan the Round column
        from itertools import groupby
        by_round = {}
        for p in picks:
            by_round.setdefault(p["draft_round"], []).append(p)

        max_round = max(by_round.keys()) if by_round else 16
        # In keeper leagues this league does 16 rounds; cover at least that
        last_round = max(max_round, 16)

        rows_parts = []
        for round_num in range(1, last_round + 1):
            group_list = by_round.get(round_num, [])
            if not group_list:
                # Insert a placeholder row showing "no pick this round"
                rows_parts.append(f"""
        <tr class="round-empty round-{round_num}">
          <td class="round-cell" rowspan="1">{round_num}</td>
          <td colspan="7" class="meta">No pick — traded away or skipped</td>
        </tr>""")
                continue
            for i, p in enumerate(group_list):
                rows_parts.append(
                    render_draft_pick_row(
                        p,
                        show_round_cell=(i == 0),
                        rowspan=len(group_list),
                    )
                )
        rows = "".join(rows_parts)

        body = f"""
            <table class="draft-table">
              <thead>
                <tr>
                  <th>Round</th>
                  <th class="num">Actual Pick</th>
                  <th>Player</th>
                  <th>Pos</th>
                  <th>Type</th>
                  <th>DRC lineage</th>
                  <th class="num">Average Pick (ADP)</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>"""

    open_class = " open" if is_default_open else ""
    return f"""
        <div class="year-collapsible{open_class}" id="year-{slug}-{year}">
          <button class="year-collapsible-header" data-target="year-{slug}-{year}">
            <span class="year-title">{year}</span>
            <span class="year-meta">{len(picks)} picks</span>
            <span class="year-chev">&rsaquo;</span>
          </button>
          <div class="year-collapsible-body">
            {body}
          </div>
        </div>"""


def render_drafts_tab(draft_history, slug):
    """Render the Drafts tab content: collapsible year blocks 2025 / 2024 / 2023."""
    years_desc = sorted(draft_history.keys(), reverse=True)
    # Default the most recent year open
    blocks = "".join(
        render_year_drafts(y, draft_history.get(y, []), is_default_open=(i == 0), slug=slug)
        for i, y in enumerate(years_desc)
    )
    return blocks or '<p class="empty-note">No draft history found.</p>'


def render_team_section(data, slug):
    pcount = data["player_count"]
    expensive = data["expensive_count"]
    cheap = data["cheap_count"]
    total = data["total_drc_dollars"]
    rows = "".join(render_player_row(p) for p in data["players"])
    drafts_html = render_drafts_tab(data.get("draft_history", {}), slug)
    trades_html = render_trades_tab(data.get("trade_history", []), slug)

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

      <div class="tabs" data-tabgroup="{slug}">
        <button class="tab-btn active" data-tab="{slug}-roster">Roster</button>
        <button class="tab-btn" data-tab="{slug}-drafts">Drafts</button>
        <button class="tab-btn" data-tab="{slug}-trades">Trades</button>
      </div>

      <div class="tab-panel active" id="{slug}-roster">
        <table class="roster team-roster">
          <thead>
            <tr>
              <th>Player</th>
              <th>Pos</th>
              <th>NFL</th>
              <th class="num">DRC</th>
              <th class="num">Cost</th>
              <th class="num">2026 ADP</th>
              <th class="num">Value</th>
              <th class="expand-col"></th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
          <tr class="total">
            <td>Total committed</td>
            <td class="meta"></td>
            <td class="meta"></td>
            <td class="num"></td>
            <td class="num cost">${total:,}</td>
            <td class="num"></td>
            <td class="num"></td>
            <td class="expand-col"></td>
          </tr>
        </table>
      </div>

      <div class="tab-panel" id="{slug}-drafts" hidden>
        {drafts_html}
      </div>

      <div class="tab-panel" id="{slug}-trades" hidden>
        {trades_html}
      </div>
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
            <td class="player-name"><a href="#" data-target="team-{slug}">{html.escape(t['team_name'])}</a><span class="sub-line">{html.escape(t['manager'])}</span></td>
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
      <table class="roster standings">
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
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 100px;
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

/* --- Expandable player history ---------------------------------------- */
.expand-btn {
  background: none;
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  width: 18px;
  height: 18px;
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-600);
  cursor: pointer;
  margin-right: 8px;
  line-height: 1;
  padding: 0;
  display: inline-block;
  vertical-align: middle;
  transition: transform 0.15s ease;
}
.expand-btn:hover { color: var(--blue-600); border-color: var(--blue-400); }
.expand-btn.open  { transform: rotate(90deg); color: var(--blue-600); border-color: var(--blue-600); }

.expand-col { width: 28px; text-align: center; }

tr.history-row > td.history-cell {
  padding: 0;
  background: var(--gray-50);
  border-bottom: 1px solid var(--gray-200);
}
.history-cards {
  display: flex;
  gap: 14px;
  padding: 16px 20px 18px 20px;
  flex-wrap: nowrap;
  overflow-x: auto;
}
.year-card {
  flex: 1 1 0;
  min-width: 180px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: #fff;
  padding: 14px 16px;
}
.year-card.card-empty {
  background: #fafafa;
  border-color: #ececec;
  opacity: 0.65;
}
.year-card .year-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--blue-600);
  margin-bottom: 12px;
}
.year-card.card-empty .year-label { color: var(--gray-500); }
.year-card .year-metric {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 5px 0;
  font-size: 13px;
  border-top: 1px solid var(--gray-100);
}
.year-card .year-metric:first-of-type { border-top: none; padding-top: 0; }
.year-card .m-label {
  color: var(--gray-500);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.year-card .m-val {
  font-variant-numeric: tabular-nums;
  color: var(--gray-700);
  font-weight: 500;
}

/* --- Tabs (Roster / Drafts / etc.) ----------------------------------- */
.tabs {
  display: flex;
  gap: 24px;
  border-bottom: 1px solid var(--gray-200);
  margin: 40px 0 24px 0;
}
.tab-btn {
  background: none;
  border: none;
  padding: 10px 0;
  font-family: inherit;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: -0.005em;
  color: var(--gray-600);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tab-btn:hover { color: var(--blue-600); }
.tab-btn.active {
  color: var(--blue-800);
  font-weight: 600;
  border-bottom-color: var(--blue-600);
}
.tab-panel[hidden] { display: none; }

/* --- Drafts: collapsible per-year blocks ----------------------------- */
.year-collapsible {
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  margin-bottom: 14px;
  overflow: hidden;
}
.year-collapsible-header {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 18px;
  background: #fff;
  border: none;
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-700);
  cursor: pointer;
  text-align: left;
}
.year-collapsible-header:hover { background: var(--gray-50); }
.year-collapsible .year-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--blue-800);
  letter-spacing: 0.04em;
}
.year-collapsible .year-meta {
  color: var(--gray-500);
  font-size: 12px;
  font-weight: 500;
}
.year-collapsible .year-chev {
  margin-left: auto;
  color: var(--gray-500);
  font-size: 16px;
  transition: transform 0.15s ease;
}
.year-collapsible.open .year-chev { transform: rotate(90deg); color: var(--blue-600); }
.year-collapsible-body {
  display: none;
  padding: 6px 18px 16px;
  border-top: 1px solid var(--gray-100);
  background: var(--gray-50);
}
.year-collapsible.open .year-collapsible-body { display: block; }

.draft-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13.5px;
  font-variant-numeric: tabular-nums;
  background: #fff;
}
.draft-table th {
  font-size: 10.5px;
  color: var(--gray-500);
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 10px 10px;
  border-bottom: 1px solid var(--gray-200);
  text-align: center;
}
.draft-table td {
  padding: 10px;
  border-bottom: 1px solid var(--gray-200);
}
.draft-table td.num { text-align: right; }
.draft-table td.pick-label {
  color: var(--blue-800);
  font-weight: 600;
  width: 64px;
  text-align: center;
}
.draft-table td.player-name { font-weight: 500; color: #000; }
.draft-table td.meta { color: var(--gray-600); }
.draft-table td.type-code {
  text-align: center;
  font-weight: 600;
  color: var(--blue-800);
}
.draft-table td.trajectory-cell {
  color: var(--gray-700);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

/* Round-banding leftmost column - subtle Blue 200 with Blue 800 text */
.draft-table td.round-cell {
  background: #eaf4ff;
  color: var(--blue-800);
  font-weight: 600;
  font-size: 13px;
  text-align: center;
  width: 44px;
  border-bottom: 1px solid var(--gray-200);
  border-right: 2px solid var(--blue-200);
  vertical-align: middle;
  letter-spacing: 0.04em;
}
/* Empty-round placeholder row (no pick that round) */
.draft-table tr.round-empty td {
  color: var(--gray-500);
  font-style: italic;
  background: #fafafa;
}

/* Traded-for row highlight: subtle blue tint + left accent */
.draft-table tr.traded-for td:not(.round-cell) {
  background: #f4f9ff !important;
}
.draft-table tr.traded-for td.pick-label {
  border-left: 3px solid var(--blue-400);
  padding-left: 7px;
}

/* Player-name tooltip on hover */
.draft-table td.player-name {
  position: relative;
  overflow: visible;
}
.draft-table .player-name-link {
  cursor: help;
  border-bottom: 1px dashed var(--gray-300);
}
.draft-table .player-name-link:hover {
  border-bottom-color: var(--blue-600);
  color: var(--blue-800);
}
.draft-table .player-tooltip {
  display: none;
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  padding: 12px 14px 14px;
  min-width: 320px;
  max-width: 420px;
  z-index: 100;
  font-size: 12px;
  line-height: 1.5;
  text-align: left;
  font-weight: 400;
}
.draft-table td.player-name:hover .player-tooltip { display: block; }
.draft-table .tooltip-header {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--blue-800);
  padding-bottom: 8px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--gray-200);
}
.draft-table .tooltip-event {
  display: flex;
  gap: 10px;
  padding: 4px 0;
}
.draft-table .tooltip-event .event-date {
  color: var(--gray-500);
  flex: 0 0 80px;
  font-variant-numeric: tabular-nums;
}
.draft-table .tooltip-event .event-desc {
  color: var(--gray-700);
  flex: 1 1 auto;
}

/* Subtle alternating round background bands on data rows */
.draft-table tr.round-2 td:not(.round-cell),
.draft-table tr.round-4 td:not(.round-cell),
.draft-table tr.round-6 td:not(.round-cell),
.draft-table tr.round-8 td:not(.round-cell),
.draft-table tr.round-10 td:not(.round-cell),
.draft-table tr.round-12 td:not(.round-cell),
.draft-table tr.round-14 td:not(.round-cell),
.draft-table tr.round-16 td:not(.round-cell) {
  background: #fafbfc;
}

.empty-note { color: var(--gray-500); font-size: 13px; padding: 8px 0; }

/* Traded-for row highlight */
.draft-table tr.traded-for td:not(.round-cell) { background: #f4f9ff !important; }
.draft-table tr.traded-for td.pick-label {
  border-left: 3px solid var(--blue-400);
  padding-left: 7px;
}

/* Player tooltip */
.draft-table td.player-name { position: relative; overflow: visible; }
.draft-table .player-name-link {
  cursor: help;
  border-bottom: 1px dashed var(--gray-300);
}
.draft-table .player-name-link:hover {
  border-bottom-color: var(--blue-600);
  color: var(--blue-800);
}
.draft-table .player-tooltip {
  display: none;
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  padding: 12px 14px 14px;
  min-width: 320px;
  max-width: 420px;
  z-index: 100;
  font-size: 12px;
  line-height: 1.5;
  text-align: left;
  font-weight: 400;
}
.draft-table td.player-name:hover .player-tooltip { display: block; }
.draft-table .tooltip-header {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--blue-800);
  padding-bottom: 8px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--gray-200);
}
.draft-table .tooltip-event {
  display: flex;
  gap: 10px;
  padding: 4px 0;
}
.draft-table .tooltip-event .event-date {
  color: var(--gray-500);
  flex: 0 0 80px;
  font-variant-numeric: tabular-nums;
}
.draft-table .tooltip-event .event-desc {
  color: var(--gray-700);
  flex: 1 1 auto;
}

/* --- Trades tab -------------------------------------------------------- */
.trade-event {
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: #fff;
  padding: 16px 20px;
  margin-bottom: 18px;
}
.trade-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 12px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--gray-200);
}
.trade-date {
  font-size: 13px;
  font-weight: 600;
  color: var(--blue-800);
  letter-spacing: 0.03em;
}
.trade-vs { font-size: 13px; color: var(--gray-600); }
.trade-side { margin-bottom: 12px; }
.trade-side-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 6px;
}
.trade-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  table-layout: fixed;
}
.trade-table col.col-player { width: 38%; }
.trade-table col.col-pos { width: 8%; }
.trade-table col.col-nfl { width: 10%; }
.trade-table col.col-year { width: 14.66%; }
.trade-table th {
  font-size: 10.5px;
  color: var(--gray-500);
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 8px 10px;
  border-bottom: 1px solid var(--gray-200);
  text-align: center;
}
.trade-table th:first-child,
.trade-table th:nth-child(2),
.trade-table th:nth-child(3) { text-align: left; }
.trade-table th.num { text-align: right; }
.trade-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--gray-100);
  vertical-align: middle;
}
.trade-table td.player-name { font-weight: 500; }
.trade-table td.meta { color: var(--gray-600); font-size: 12.5px; }
.trade-table td.num { text-align: right; }
.trade-table td.cost { font-weight: 600; color: var(--blue-800); }
.trade-table tr.subtotal-row td {
  background: var(--gray-50);
  font-weight: 600;
  border-top: 1.5px solid var(--gray-200);
  border-bottom: none;
}

/* Points cell with mini-bar */
.pts-cell {
  display: inline-block;
  text-align: right;
  position: relative;
  min-width: 60px;
  padding-bottom: 12px;
}
.pts-cell .pts-full { font-weight: 500; color: var(--gray-700); }
.pts-cell .pts-post-trade {
  display: block;
  font-size: 10px;
  color: var(--gray-500);
  font-style: italic;
}
.mini-bar-track {
  position: absolute;
  bottom: 0;
  left: 0; right: 0;
  height: 7px;
  background: var(--gray-200);
  border-radius: 3px;
  overflow: hidden;
}
.mini-bar-fill {
  height: 100%;
  background: var(--blue-600);
  border-radius: 3px;
}

/* --- Player search section -------------------------------------------- */
.ps-input-wrap {
  margin-bottom: 24px;
  max-width: 560px;
}
.ps-input {
  width: 100%;
  box-sizing: border-box;
  padding: 12px 16px;
  font-family: inherit;
  font-size: 15px;
  border: 1.5px solid var(--gray-300);
  border-radius: 6px;
  background: #fff;
  color: var(--gray-800);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.ps-input:focus {
  outline: none;
  border-color: var(--blue-600);
  box-shadow: 0 0 0 3px rgba(0, 56, 255, 0.12);
}
.ps-input-meta {
  margin-top: 6px;
  font-size: 12px;
  color: var(--gray-500);
}
.ps-empty-state {
  padding: 28px 0;
  color: var(--gray-500);
  font-size: 14px;
  font-style: italic;
}
.ps-results { display: flex; flex-direction: column; gap: 12px; }
.player-card {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 14px 18px;
}
.player-card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--gray-100);
  margin-bottom: 10px;
}
.player-card-title { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.player-card-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--blue-800);
}
.player-card-meta {
  font-size: 12px;
  color: var(--gray-600);
  letter-spacing: 0.04em;
}
.ps-owner {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--gray-700);
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  padding: 3px 8px;
}
.ps-owner-none { color: var(--gray-500); font-style: italic; }
.player-card-events { display: flex; flex-direction: column; gap: 4px; }
.ps-event {
  display: flex;
  gap: 12px;
  font-size: 13px;
  padding: 3px 0;
}
.ps-event-date {
  flex: 0 0 88px;
  color: var(--gray-500);
  font-variant-numeric: tabular-nums;
}
.ps-event-desc { color: var(--gray-800); }
.ps-event-empty {
  font-size: 13px;
  color: var(--gray-500);
  font-style: italic;
}

/* ---- Three summary cards inside each player-card ---- */
.psum-row-cards {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr;
  gap: 12px;
  margin: 12px 0 18px 0;
}
.psum-card {
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 12px 14px;
}
.psum-card-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 8px;
}
.psum-card-hero {
  background: var(--blue-800);
  color: #fff;
  border-color: var(--blue-800);
}
.psum-card-hero .psum-card-label {
  color: rgba(255, 255, 255, 0.75);
}
.psum-big {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.psum-big-sub {
  font-size: 12.5px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.85);
  margin-top: 2px;
}
.psum-big-meta {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.65);
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}
/* ---- Two-column DRC | Performance row ---- */
.ps-two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin: 14px 0 18px 0;
}
.ps-side {
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 14px;
}
.ps-side-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 12px;
}
.ps-hero {
  border-radius: 5px;
  padding: 14px 16px;
  margin-bottom: 12px;
  color: #fff;
}
.ps-hero-drc { background: var(--blue-800); border-left: 3px solid #E1B523; }
.ps-hero-adp { background: var(--blue-600); }
.ps-hero-big {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1.05;
  font-variant-numeric: tabular-nums;
}
.ps-hero-sub {
  font-size: 11.5px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.85);
  margin-top: 4px;
}
.ps-side-tiles {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
}
.ps-side-tile {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-left: 3px solid var(--gray-400);
  border-radius: 4px;
  padding: 10px 8px;
  text-align: left;
}
.ps-side-val {
  font-size: 16px;
  font-weight: 700;
  color: var(--blue-800);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.ps-side-sub {
  font-size: 10.5px;
  color: var(--gray-700);
  font-weight: 500;
  margin-top: 3px;
}
.ps-side-yr {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  color: var(--gray-500);
  margin-top: 6px;
  text-transform: uppercase;
}

/* Weekly fantasy points: three side-by-side weekly bar charts */
.ps-charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 16px;
}
.ps-chart-col {
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 10px;
}
.ps-chart-wrap {
  background: var(--gray-50);
  border-radius: 3px;
  padding: 4px 2px;
}
.ps-chart-svg {
  display: block;
  width: 100%;
  height: 70px;
}
.ps-chart-labels {
  margin-top: 10px;
  text-align: center;
}
.ps-chart-year {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
}
.ps-chart-stats {
  margin-top: 5px;
  font-size: 12.5px;
  display: flex;
  justify-content: center;
  align-items: baseline;
  gap: 6px;
}
.ps-chart-rank {
  font-weight: 700;
  color: var(--blue-800);
  font-variant-numeric: tabular-nums;
}
.ps-chart-sep { color: var(--gray-400); }
.ps-chart-pts {
  font-weight: 600;
  color: var(--gray-800);
  font-variant-numeric: tabular-nums;
}
.ps-chart-adp {
  font-weight: 500;
  color: var(--gray-700);
  font-variant-numeric: tabular-nums;
}

/* Position-rank neighbor table under each year's chart.
   table-layout: fixed lets the name column absorb all leftover width —
   without it, max-width tricks collapse the name to ~0px on mobile. */
.ps-nb-table {
  width: 100%;
  margin-top: 12px;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.ps-nb-table td {
  padding: 4px 6px;
  border-bottom: 1px solid var(--gray-100);
  vertical-align: middle;
}
.ps-nb-table tr:last-child td { border-bottom: none; }
.ps-nb-rank {
  width: 40px;
  font-weight: 600;
  color: var(--gray-600);
  letter-spacing: 0.02em;
}
.ps-nb-name {
  color: var(--gray-800);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ps-nb-pts {
  text-align: right;
  width: 44px;
  color: var(--gray-700);
}
.ps-nb-table tr.ps-nb-self td {
  background: rgba(0, 56, 255, 0.07);
  color: var(--blue-800);
  font-weight: 700;
}
.ps-nb-table tr.ps-nb-self .ps-nb-pts { color: var(--blue-800); }
.ps-nb-empty {
  margin-top: 10px;
  font-size: 11px;
  color: var(--gray-500);
  font-style: italic;
  text-align: center;
}
.ps-spark-empty {
  font-size: 12px;
  color: var(--gray-500);
  font-style: italic;
  padding: 8px 0 12px 0;
}
.psum-stat-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.psum-stat-val {
  font-size: 20px;
  font-weight: 700;
  color: var(--blue-800);
  font-variant-numeric: tabular-nums;
}
.psum-stat-key {
  font-size: 11px;
  color: var(--gray-500);
  margin-top: 2px;
  letter-spacing: 0.04em;
}

/* ---- Section subdividers within player-card ---- */
.ps-section { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--gray-100); }
.ps-section-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 10px;
}

/* ---- KPI strip: 2023-2026 DRC + current ADP ---- */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
}
.kpi-tile {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 12px 14px;
  text-align: left;
}
.kpi-tile-historical {
  border-left: 3px solid var(--gray-400);
}
.kpi-tile-current {
  background: rgba(225, 181, 35, 0.06);
  border-color: rgba(225, 181, 35, 0.30);
  border-left: 3px solid #E1B523;
}
.kpi-tile-current .kpi-big { color: #8C6E10; }
.kpi-tile-adp {
  background: var(--gray-50);
  border-left: 3px solid var(--blue-400);
}
.kpi-big {
  font-size: 22px;
  font-weight: 700;
  color: var(--blue-800);
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
.kpi-sub {
  font-size: 11.5px;
  color: var(--gray-700);
  font-weight: 500;
  margin-top: 4px;
}
.kpi-tag {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-top: 6px;
}

/* ---- Ownership lineage timeline ---- */
.lineage-flow {
  display: flex;
  align-items: stretch;
  gap: 8px;
  flex-wrap: wrap;
}
.lineage-node {
  flex: 1 1 180px;
  min-width: 180px;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-left: 3px solid var(--gray-400);
  border-radius: 4px;
  padding: 10px 12px;
}
.lineage-drafted { border-left-color: var(--blue-600); }
.lineage-trade { border-left-color: var(--gold-400, #E1B523); }
.lineage-waiver { border-left-color: var(--gray-500); }
.lineage-free-agent { border-left-color: var(--gray-500); }
.lineage-date {
  font-size: 11px;
  color: var(--gray-500);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}
.lineage-manager {
  font-size: 14px;
  font-weight: 600;
  color: var(--blue-800);
  margin-top: 2px;
}
.lineage-method {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-top: 6px;
}
.lineage-detail {
  font-size: 12px;
  color: var(--gray-700);
  margin-top: 2px;
}
.lineage-arrow {
  display: flex;
  align-items: center;
  color: var(--gray-400);
  font-size: 18px;
  padding: 0 2px;
}

/* --- Commissioner's Desk ----------------------------------------------- */
.desk-layout {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 28px;
  align-items: start;
}
.desk-rail {
  position: sticky;
  top: 24px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.desk-post-link {
  display: block;
  padding: 10px 14px;
  border-radius: 5px;
  text-decoration: none;
  color: var(--gray-800);
  border-left: 3px solid transparent;
  transition: background 0.12s, border-color 0.12s;
  cursor: pointer;
}
.desk-post-link:hover {
  background: var(--gray-50);
}
.desk-post-link.desk-active {
  background: var(--gray-50);
  border-left-color: var(--blue-600);
}
.desk-post-link-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--blue-800);
  line-height: 1.25;
}
.desk-post-link-date {
  font-size: 11px;
  color: var(--gray-500);
  margin-top: 3px;
  letter-spacing: 0.04em;
}
.desk-content { max-width: 780px; }
.desk-post-header {
  border-bottom: 1px solid var(--gray-200);
  padding-bottom: 18px;
  margin-bottom: 24px;
}
.desk-post-title {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.015em;
  color: var(--blue-800);
  margin: 0 0 6px 0;
  line-height: 1.15;
}
.desk-post-meta {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--gray-500);
}
.desk-post-summary {
  margin-top: 12px;
  font-size: 14px;
  color: var(--gray-700);
  line-height: 1.55;
  font-style: italic;
}
.desk-post-body p {
  font-size: 14.5px;
  line-height: 1.65;
  color: var(--gray-800);
  margin: 0 0 14px 0;
}
.desk-post-body strong { color: var(--blue-800); font-weight: 600; }
.desk-post-body em { color: var(--gray-700); }
.desk-post-body h1.desk-h1 {
  font-size: 22px;
  font-weight: 700;
  color: var(--blue-800);
  margin: 32px 0 12px 0;
  letter-spacing: -0.01em;
}
.desk-post-body h2.desk-h2 {
  font-size: 19px;
  font-weight: 700;
  color: var(--blue-800);
  margin: 30px 0 12px 0;
  letter-spacing: -0.005em;
}
/* Pete's *asterisk-wrapped* section headers render here. Strong visual break,
   no underline (clean look). */
.desk-post-body h3.desk-h3 {
  font-size: 18px;
  font-weight: 700;
  color: var(--blue-800);
  margin: 30px 0 12px 0;
  letter-spacing: -0.005em;
  text-transform: none;
}
.desk-post-body h4.desk-h4 {
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-700);
  margin: 20px 0 8px 0;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
/* Inline-bold sub-section heads like "*FAAB, FA, and Trade Moves: *". These
   sit between a team-finish header and the prose underneath. Tighter than h3. */
.desk-post-body h4.desk-subhead {
  font-size: 15px;
  font-weight: 700;
  color: var(--gray-800);
  margin: 20px 0 6px 0;
  letter-spacing: -0.003em;
  text-transform: none;
}
/* Team-finish header card: bold name + chips for each labelled stat.
   Used in season wrap-ups (one card per team, in finish order). */
.team-finish-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  margin: 40px 0 18px 0;
  padding: 14px 18px;
  background: var(--gray-50);
  border-left: 3px solid var(--blue-600);
  border-radius: 5px;
}
.team-finish-name {
  font-size: 20px;
  font-weight: 700;
  color: var(--blue-800);
  margin: 0;
  letter-spacing: -0.01em;
}
.team-finish-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.team-chip {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 11.5px;
  background: #fff;
  border: 1px solid var(--gray-200);
  padding: 4px 10px;
  border-radius: 3px;
}
.team-chip-k {
  font-weight: 600;
  color: var(--gray-500);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 10px;
}
.team-chip-v {
  font-weight: 700;
  color: var(--blue-800);
  font-variant-numeric: tabular-nums;
}
.desk-figure {
  margin: 22px 0;
  padding: 0;
}
.desk-img {
  display: block;
  max-width: 100%;
  height: auto;
  border: 1px solid var(--gray-200);
  border-radius: 5px;
}
.desk-figure figcaption {
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--gray-500);
  font-style: italic;
  text-align: center;
  letter-spacing: 0.02em;
}
.desk-img-missing {
  margin: 16px 0;
  padding: 12px 14px;
  background: var(--gray-50);
  border: 1px dashed var(--gray-300);
  border-radius: 5px;
  font-size: 12.5px;
  color: var(--gray-600);
  font-style: italic;
}
.desk-post-body ul.desk-list, .desk-post-body ol.desk-list {
  margin: 4px 0 16px 0;
  padding-left: 22px;
}
.desk-post-body ul.desk-list li, .desk-post-body ol.desk-list li {
  font-size: 14.5px;
  line-height: 1.55;
  color: var(--gray-800);
  margin-bottom: 6px;
}

/* --- League rules section ---------------------------------------------- */
.rules-grid {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 780px;
}
.rule-block {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 22px 26px;
}
.rule-block-new {
  border-left: 3px solid #E1B523;
  background: rgba(225, 181, 35, 0.04);
}
.rule-new-pill {
  display: inline-block;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #8C6E10;
  background: rgba(225, 181, 35, 0.18);
  padding: 4px 10px;
  border-radius: 3px;
  margin-bottom: 12px;
}
.rule-h2 {
  font-size: 18px;
  font-weight: 600;
  color: var(--blue-800);
  margin: 0 0 12px 0;
  letter-spacing: -0.01em;
}
.rule-h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-700);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin: 18px 0 8px 0;
}
.rule-block p {
  font-size: 14px;
  line-height: 1.55;
  color: var(--gray-800);
  margin: 0 0 12px 0;
}
.rule-block p:last-child { margin-bottom: 0; }
.rule-block strong { color: var(--blue-800); font-weight: 600; }
.rule-block em { color: var(--gray-700); }
.rules-list {
  margin: 8px 0 12px 0;
  padding-left: 22px;
}
.rules-list li {
  font-size: 14px;
  line-height: 1.5;
  color: var(--gray-800);
  margin-bottom: 4px;
}
.rules-note {
  font-size: 12.5px !important;
  color: var(--gray-600) !important;
  font-style: italic;
}
.rules-table {
  width: auto;
  margin: 8px 0 12px 0;
  border-collapse: collapse;
  font-size: 13.5px;
  font-variant-numeric: tabular-nums;
}
.rules-table th, .rules-table td {
  padding: 7px 18px 7px 0;
  border-bottom: 1px solid var(--gray-100);
  text-align: left;
}
.rules-table th {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gray-500);
}
.rules-table td.num, .rules-table th.num { text-align: right; }
.rules-table tbody tr:last-child td { border-bottom: none; }

/* Sub-callout: a newly-passed change inside an otherwise-established rule */
.rule-sub-callout {
  margin-top: 18px;
  padding: 16px 18px;
  background: rgba(225, 181, 35, 0.06);
  border: 1px solid rgba(225, 181, 35, 0.30);
  border-left: 3px solid #E1B523;
  border-radius: 5px;
}
.rule-sub-callout .rule-new-pill { margin-bottom: 10px; }
.rule-sub-callout p { font-size: 13.5px; }

/* Side-by-side old vs. new comparison table */
.rules-table-compare {
  width: 100%;
  margin: 12px 0 10px 0;
}
.rules-table-compare th, .rules-table-compare td {
  padding: 7px 10px;
}
.rules-th-new {
  color: #8C6E10 !important;
}
.rules-td-new {
  background: rgba(225, 181, 35, 0.10);
  font-weight: 600;
  color: #8C6E10;
}

/* --- Footnote ---------------------------------------------------------- */
.footnote {
  margin-top: 48px;
  padding-top: 18px;
  border-top: 1px solid var(--gray-200);
  font-size: 11.5px;
  color: var(--gray-500);
  font-style: italic;
}


/* Player search: autocomplete suggestions dropdown */
.ps-suggestions {
  max-width: 560px;
  margin-bottom: 18px;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
  max-height: 320px;
  overflow-y: auto;
}
.ps-suggestion {
  display: block;
  padding: 9px 14px;
  text-decoration: none;
  color: var(--gray-800);
  font-size: 14px;
  border-bottom: 1px solid var(--gray-100);
  cursor: pointer;
}
.ps-suggestion:last-child { border-bottom: none; }
.ps-suggestion:hover {
  background: var(--gray-50);
  color: var(--blue-800);
  font-weight: 500;
}


/* ---- Collapsible Teams in sidebar ---- */
.sidebar details.sidebar-teams { margin-top: 4px; }
.sidebar details.sidebar-teams > summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  list-style: none;
  padding: 14px 16px 8px 16px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: rgba(255, 255, 255, 0.55);
}
.sidebar details.sidebar-teams > summary::-webkit-details-marker { display: none; }
.sidebar details.sidebar-teams > summary::after {
  content: "+";
  margin-left: 8px;
  font-size: 16px;
  color: rgba(255, 255, 255, 0.55);
  transition: transform 0.18s ease;
}
.sidebar details.sidebar-teams[open] > summary::after { content: "\2212"; }
.sidebar-team-list { display: flex; flex-direction: column; }

/* ====================================================================== */
/* Stacked sub-line inside name cells (manager under team, pos/NFL under
   player). Hidden on desktop where those have their own columns; shown on
   mobile where the columns are hidden to keep rows to one clean line. */
.sub-line { display: none; }

/* ====================================================================== */
/* Trade analyzer                                                         */
/* ====================================================================== */
.ta-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin: 6px 0 20px;
}
.ta-side {
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 14px 16px;
}
.ta-label {
  display: block;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gray-600);
  margin-bottom: 6px;
}
.ta-team {
  width: 100%;
  font-family: inherit;
  font-size: 13.5px;
  padding: 8px 10px;
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  background: #fff;
  color: var(--gray-800);
  margin-bottom: 10px;
}
.ta-roster { max-height: 320px; overflow-y: auto; }
.ta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
}
.ta-row:hover { background: #fff; }
.ta-row input { margin: 0; flex: 0 0 auto; }
.ta-row .ta-nm {
  flex: 1 1 auto;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 500;
  color: var(--gray-800);
}
.ta-row .ta-meta { color: var(--gray-600); font-size: 11.5px; flex: 0 0 auto; }
.ta-row .ta-cost {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  font-weight: 600;
  color: var(--blue-800);
  width: 86px;
  text-align: right;
}
.ta-picks { margin-top: 12px; border-top: 1px solid var(--gray-200); padding-top: 10px; }
.ta-picks select, .ta-add-pick {
  font-family: inherit;
  font-size: 12.5px;
  padding: 5px 8px;
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  background: #fff;
  color: var(--gray-800);
}
.ta-add-pick { cursor: pointer; font-weight: 600; color: var(--blue-800); }
.ta-add-pick:hover { border-color: var(--blue-600); }
.ta-pick-chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.ta-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 999px;
  padding: 3px 6px 3px 11px;
  font-size: 12px;
  font-weight: 600;
  color: var(--blue-800);
}
.ta-chip button {
  border: none; background: none; cursor: pointer;
  color: var(--gray-500); font-size: 14px; line-height: 1; padding: 0 3px;
}
.ta-chip button:hover { color: var(--gray-800); }
.ta-results { margin-top: 4px; }
.ta-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.ta-recv {
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 14px 16px;
  background: #fff;
}
.ta-recv h3 { font-size: 14px; margin: 0 0 10px; color: var(--blue-800); }
table.ta-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  table-layout: fixed;
}
table.ta-table th {
  text-align: left;
  font-size: 9.5px;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--gray-600);
  padding: 4px 6px;
  border-bottom: 1px solid var(--gray-200);
}
table.ta-table td { padding: 6px; border-bottom: 1px solid var(--gray-100); }
table.ta-table th.num, table.ta-table td.num { text-align: right; }
table.ta-table td.ta-pname {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500;
}
table.ta-table td.ta-frozen { font-weight: 700; color: var(--blue-800); }
table.ta-table tr.ta-total td {
  border-top: 1.5px solid #000; border-bottom: none; font-weight: 700;
}
.ta-keepnote { font-size: 10.5px; color: var(--gray-500); display: block; }
.ta-bullets { margin: 14px 0 0; padding: 0 0 0 18px; font-size: 13px; color: var(--gray-800); }
.ta-bullets li { margin-bottom: 6px; line-height: 1.5; }
.ta-cap-up { color: var(--red-600, #982B09); font-weight: 600; }
.ta-cap-down { color: var(--green-600, #6B7D00); font-weight: 600; }
.ta-empty {
  font-size: 13px; color: var(--gray-500); font-style: italic;
  padding: 14px 0;
}
.ta-foot {
  font-size: 11.5px;
  color: var(--gray-500);
  margin-top: 22px;
  line-height: 1.6;
  border-top: 1px solid var(--gray-200);
  padding-top: 12px;
}

/* ====================================================================== */
/* Feedback widget: floating trigger + modal                              */
/* ====================================================================== */
.fb-trigger {
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 80;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: var(--blue-600);
  color: #fff;
  border: none;
  border-radius: 999px;
  padding: 10px 18px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.01em;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(2, 36, 121, 0.30);
  transition: background 0.15s ease, transform 0.15s ease;
}
.fb-trigger:hover { background: var(--blue-800); transform: translateY(-1px); }
.fb-icon { font-size: 14px; line-height: 1; }
.fb-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 110;
}
.fb-overlay[hidden], .fb-modal[hidden] { display: none; }
.fb-modal {
  position: fixed;
  bottom: 74px;
  right: 18px;
  z-index: 115;
  width: min(380px, calc(100vw - 36px));
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.22);
  padding: 18px 20px 16px 20px;
}
.fb-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
}
.fb-modal-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--blue-800);
  margin: 0;
}
.fb-close {
  background: none;
  border: none;
  font-size: 22px;
  line-height: 1;
  color: var(--gray-500);
  cursor: pointer;
  padding: 2px 4px;
}
.fb-close:hover { color: var(--gray-800); }
.fb-modal-sub {
  font-size: 12.5px;
  color: var(--gray-600);
  margin: 0 0 14px 0;
}
.fb-form label.fb-label {
  display: block;
  margin-bottom: 12px;
}
.fb-field-label {
  display: block;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gray-600);
  margin-bottom: 4px;
}
.fb-form input[type="text"],
.fb-form textarea {
  width: 100%;
  box-sizing: border-box;
  font-family: inherit;
  font-size: 13.5px;
  color: var(--gray-800);
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  padding: 8px 10px;
  resize: vertical;
}
.fb-form input[type="text"]:focus,
.fb-form textarea:focus {
  outline: none;
  border-color: var(--blue-600);
  box-shadow: 0 0 0 2px rgba(0, 56, 255, 0.12);
}
.fb-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 2px;
}
.fb-btn {
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  border-radius: 4px;
  padding: 8px 16px;
  cursor: pointer;
}
.fb-btn-primary {
  background: var(--blue-600);
  color: #fff;
  border: 1px solid var(--blue-600);
}
.fb-btn-primary:hover { background: var(--blue-800); border-color: var(--blue-800); }
.fb-btn-secondary {
  background: #fff;
  color: var(--gray-700);
  border: 1px solid var(--gray-200);
}
.fb-btn-secondary:hover { border-color: var(--gray-500); }
.fb-modal-foot {
  font-size: 11px;
  color: var(--gray-500);
  font-style: italic;
  margin: 12px 0 0 0;
}

/* ====================================================================== */
/* Mobile responsive layer (<= 720px) */
/* ====================================================================== */
.menu-toggle, .sidebar-tab { display: none; }
.sidebar-backdrop { display: none; }

@media (max-width: 720px) {
  .layout {
    grid-template-columns: 1fr;
    display: block;
  }
  /* Always-visible right-edge tab that re-opens the menu. */
  .sidebar-tab {
    display: flex;
    position: fixed;
    top: 0;
    left: 0;
    width: 30px;
    height: 100vh;
    background: var(--blue-800);
    color: #fff;
    z-index: 95;
    cursor: pointer;
    align-items: center;
    justify-content: center;
    border: none;
    padding: 0;
    font-family: inherit;
  }
  .sidebar-tab span {
    writing-mode: vertical-rl;
    text-orientation: mixed;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    transform: rotate(180deg);
  }
  /* Old top hamburger hidden — replaced by the right tab. */
  .menu-toggle { display: none; }
  /* Sidebar slides in from the RIGHT, not the left. */
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    right: auto;
    width: min(280px, 82vw);
    z-index: 100;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    overflow-y: auto;
    overflow-x: hidden;
    box-shadow: 2px 0 12px rgba(0, 0, 0, 0.2);
  }
  body.sidebar-open .sidebar { transform: translateX(0); }
  .sidebar-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    z-index: 90;
  }
  body.sidebar-open .sidebar-backdrop { display: block; }
  .content {
    padding: 14px 14px 14px 42px;
    margin-left: 0 !important;
  }
  .section-header { padding: 14px 0 18px 0; }
  .section-title { font-size: 22px; line-height: 1.15; }
  .section-sub { font-size: 13px; }
  .ps-two-col { grid-template-columns: 1fr; }
  .ps-charts-row { grid-template-columns: 1fr; }
  .kpi-strip { grid-template-columns: 1fr 1fr; }
  .psum-row-cards { grid-template-columns: 1fr; }
  .desk-layout { grid-template-columns: 1fr; }
  .desk-rail {
    position: static;
    flex-direction: row;
    overflow-x: auto;
    padding-bottom: 8px;
    margin-bottom: 14px;
    border-bottom: 1px solid var(--gray-200);
    gap: 6px;
  }
  .desk-post-link {
    flex: 0 0 auto;
    border-left: none;
    border-bottom: 3px solid transparent;
    padding: 8px 12px;
  }
  .desk-post-link.desk-active {
    border-left: none;
    border-bottom-color: var(--blue-600);
  }
  .desk-content { max-width: 100%; }
  .desk-post-title { font-size: 20px; }
  .trade-table, .draft-table, .player-table { font-size: 11.5px; }
  .rules-grid { max-width: 100%; }
  .rule-block { padding: 16px 18px; }
  .rule-h2 { font-size: 16px; }
  .ps-side-tiles { grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
  .ps-side-tile { padding: 8px 6px; }
  .ps-side-val { font-size: 14px; }
  .ps-hero-big { font-size: 22px; }
  .lineage-flow { flex-direction: column; }
  .lineage-arrow { transform: rotate(90deg); padding: 4px 0; }
  .lineage-node { flex: 1 1 auto; min-width: 0; }
  .ps-nb-table { font-size: 10.5px; }
  .ps-nb-rank { width: 32px; }
  .ps-nb-pts { width: 36px; }
  /* Player search input: make sure border + focus glow stay inside the
     content padding (was getting cut off slightly on the right edge). */
  .ps-input-wrap { max-width: 100%; padding-right: 2px; }
  .ps-input:focus { box-shadow: 0 0 0 2px rgba(0, 56, 255, 0.12); }

  /* Roster/summary tables: shrink padding + font, then hide secondary
     columns PER TABLE TYPE and surface that data as a stacked sub-line
     under the name instead — every row stays one clean line. */
  table.roster { font-size: 12px; }
  table.roster th, table.roster td { padding: 7px 6px; }
  table.roster th { font-size: 9.5px; letter-spacing: 0.08em; }
  /* Standings: hide Manager, Players, Premium → keep #, Team, Total cap.
     Manager shows as a sub-line under the team name. */
  table.standings th:nth-child(3), table.standings td:nth-child(3),
  table.standings th:nth-child(4), table.standings td:nth-child(4),
  table.standings th:nth-child(5), table.standings td:nth-child(5) { display: none; }
  /* Team rosters: hide Pos, NFL, ADP → KEEP DRC, Cost, Value (the data
     that matters on a keeper dashboard). Pos · NFL shows as a sub-line. */
  table.team-roster th:nth-child(2), table.team-roster td:nth-child(2),
  table.team-roster th:nth-child(3), table.team-roster td:nth-child(3),
  table.team-roster th:nth-child(6), table.team-roster td:nth-child(6) { display: none; }
  .sub-line {
    display: block;
    font-size: 10.5px;
    font-weight: 400;
    color: var(--gray-600);
    margin-top: 2px;
    letter-spacing: 0.01em;
  }
  td.player-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 46vw; }
  /* Small 3-col tables don't need to be horizontal scroll regions. */
  .content table.ps-nb-table { display: table; }
  /* Current-owner chip: never wrap mid-name. */
  .ps-owner { white-space: nowrap; font-size: 10.5px; padding: 3px 6px; }
  /* Trade analyzer: stack the two sides; results tables shrink. */
  .ta-grid, .ta-cols { grid-template-columns: 1fr; gap: 12px; }
  .ta-roster { max-height: 250px; }
  .ta-row { padding: 7px 6px; }
  table.ta-table { font-size: 11px; }
  table.ta-table td, table.ta-table th { padding: 5px 4px; }
  /* Feedback widget: full-width bottom sheet feel on small screens. */
  .fb-trigger { bottom: 14px; right: 14px; padding: 9px 15px; }
  .fb-modal {
    right: 10px;
    left: auto;
    bottom: 64px;
    width: min(380px, calc(100vw - 52px));
    max-height: 72vh;
    overflow-y: auto;
  }

  /* KPI cards: stack 2-up, keep consistent visual height. */
  .kpis { grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 24px; }
  .kpi { padding: 14px; min-height: 86px; }
  .kpi .k { font-size: 9.5px; }
  .kpi .v { font-size: 22px; margin-top: 8px; }
  /* Keep wide tables independently scrollable instead of overflowing the page.
     This lets the page itself stay at viewport width and preserves pinch-to-zoom. */
  .team-section, .tab-panel, section.team-section { max-width: 100%; }
  /* Make every main-content table its own horizontal scroll region. */
  .content table {
    display: block;
    width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  /* Don't strip overflow off the body/content — that breaks iOS pinch zoom. */
}

@media (max-width: 480px) {
  body { font-size: 13px; }
  .content { padding: 10px 10px 10px 42px; }
  .kpi-strip { grid-template-columns: 1fr 1fr; }
  .section-title { font-size: 20px; }
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

  links.forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      show(link.dataset.target);
    });
  });

  document.querySelectorAll('a[data-target]').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      show(a.dataset.target);
    });
  });

  show('summary');

  document.querySelectorAll('.expand-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const targetId = btn.dataset.target;
      const row = document.getElementById(targetId);
      if (!row) return;
      const opening = row.hasAttribute('hidden');
      if (opening) {
        row.removeAttribute('hidden');
        btn.classList.add('open');
      } else {
        row.setAttribute('hidden', '');
        btn.classList.remove('open');
      }
    });
  });

  document.querySelectorAll('.tabs').forEach(tabsEl => {
    const buttons = tabsEl.querySelectorAll('.tab-btn');
    buttons.forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const targetPanelId = btn.dataset.tab;
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const parentSection = tabsEl.closest('.team-section');
        if (!parentSection) return;
        parentSection.querySelectorAll('.tab-panel').forEach(panel => {
          if (panel.id === targetPanelId) {
            panel.removeAttribute('hidden');
            panel.classList.add('active');
          } else {
            panel.setAttribute('hidden', '');
            panel.classList.remove('active');
          }
        });
      });
    });
  });

  document.querySelectorAll('.year-collapsible-header').forEach(header => {
    header.addEventListener('click', (e) => {
      e.preventDefault();
      const block = header.closest('.year-collapsible');
      if (!block) return;
      block.classList.toggle('open');
    });
  });

  // Commissioner's Desk: switch between posts in the right pane.
  // Uses data-desk-target (not data-target) to avoid being captured by the
  // top-level a[data-target] handler that swaps team-sections.
  // Intentionally does NOT scroll — the user stays at their current scroll
  // position so the section header remains in view.
  document.querySelectorAll('.desk-post-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const target = link.dataset.deskTarget;
      document.querySelectorAll('.desk-post-link').forEach(l => l.classList.toggle('desk-active', l === link));
      document.querySelectorAll('.desk-post').forEach(p => {
        if (p.id === target) p.removeAttribute('hidden');
        else p.setAttribute('hidden', '');
      });
    });
  });

  // Player-search filter
  const psInput = document.getElementById('player-search-input');
  if (psInput) {
    const cards = document.querySelectorAll('#ps-results .player-card');
    const emptyState = document.getElementById('ps-empty');
    const noResults = document.getElementById('ps-no-results');
    const suggestions = document.getElementById('ps-suggestions');
    function hideAllCards() { cards.forEach(c => c.hidden = true); }
    function selectPlayerByCard(card) {
      hideAllCards();
      card.hidden = false;
      suggestions.hidden = true;
      psInput.value = card.dataset.displayName || '';
      if (emptyState) emptyState.hidden = true;
      if (noResults) noResults.hidden = true;
      card.scrollIntoView({ behavior: 'instant', block: 'start' });
    }
    psInput.addEventListener('input', () => {
      const q = psInput.value.trim().toLowerCase();
      hideAllCards();
      if (q.length < 2) {
        suggestions.hidden = true;
        suggestions.innerHTML = '';
        if (emptyState) emptyState.hidden = false;
        if (noResults) noResults.hidden = true;
        return;
      }
      if (emptyState) emptyState.hidden = true;
      // Build matches list (cap at 25 to keep dropdown manageable)
      const matches = [];
      for (const c of cards) {
        if (c.dataset.name.includes(q)) {
          matches.push(c);
          if (matches.length >= 25) break;
        }
      }
      suggestions.innerHTML = '';
      if (matches.length === 0) {
        suggestions.hidden = true;
        if (noResults) noResults.hidden = false;
        return;
      }
      if (noResults) noResults.hidden = true;
      for (const card of matches) {
        const item = document.createElement('a');
        item.className = 'ps-suggestion';
        item.href = '#';
        item.textContent = card.dataset.displayName || card.dataset.name;
        item.addEventListener('click', (e) => {
          e.preventDefault();
          selectPlayerByCard(card);
        });
        suggestions.appendChild(item);
      }
      suggestions.hidden = false;
    });
    // Enter key picks the top suggestion
    psInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const first = suggestions.querySelector('.ps-suggestion');
        if (first) first.click();
      } else if (e.key === 'Escape') {
        suggestions.hidden = true;
        psInput.blur();
      }
    });
    // Reset state if the user navigates to the page
    const psLink = document.querySelector('.nav-link[data-target="player-search"]');
    if (psLink) {
      psLink.addEventListener('click', () => {
        psInput.value = "";
        cards.forEach(c => c.hidden = true);
        if (emptyState) emptyState.hidden = false;
        if (noResults) noResults.hidden = true;
        setTimeout(() => psInput.focus(), 100);
      });
    }
  }

  // Mobile sidebar toggle
  const menuToggle = document.querySelector('.menu-toggle');
  const sidebarTab = document.querySelector('.sidebar-tab');
  if (sidebarTab) {
    sidebarTab.addEventListener('click', () => {
      document.body.classList.toggle('sidebar-open');
    });
  }
  const sidebar = document.querySelector('.sidebar');
  const backdrop = document.querySelector('.sidebar-backdrop');
  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
  }
  if (menuToggle && sidebar) {
    menuToggle.addEventListener('click', () => {
      document.body.classList.toggle('sidebar-open');
    });
    if (backdrop) backdrop.addEventListener('click', closeSidebar);
    sidebar.querySelectorAll('.nav-link').forEach(link => {
      link.addEventListener('click', closeSidebar);
    });
  }

  // Feedback widget: open/close modal + mailto: submit handler
  const fbTrigger = document.getElementById('fb-trigger');
  const fbOverlay = document.getElementById('fb-overlay');
  const fbModal = document.getElementById('fb-modal');
  const fbClose = document.getElementById('fb-close');
  const fbCancel = document.getElementById('fb-cancel');
  const fbForm = document.getElementById('fb-form');
  function fbOpen() {
    if (fbOverlay) fbOverlay.hidden = false;
    if (fbModal) fbModal.hidden = false;
    const nameInput = document.getElementById('fb-name');
    if (nameInput) setTimeout(() => nameInput.focus(), 50);
  }
  function fbCloseModal() {
    if (fbOverlay) fbOverlay.hidden = true;
    if (fbModal) fbModal.hidden = true;
  }
  if (fbTrigger) fbTrigger.addEventListener('click', fbOpen);
  if (fbClose) fbClose.addEventListener('click', fbCloseModal);
  if (fbCancel) fbCancel.addEventListener('click', fbCloseModal);
  if (fbOverlay) fbOverlay.addEventListener('click', fbCloseModal);
  if (fbForm) {
    fbForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const name = (document.getElementById('fb-name').value || '').trim();
      const message = (document.getElementById('fb-message').value || '').trim();
      if (!name || !message) return;
      const subject = encodeURIComponent('IYearn dashboard feedback from ' + name);
      const body = encodeURIComponent('From: ' + name + '\n\n' + message + '\n\n---\nSent via the IYearn dashboard feedback widget.');
      window.location.href = 'mailto:hodorpete@gmail.com?subject=' + subject + '&body=' + body;
      fbCloseModal();
    });
  }
})();

/* ---- Trade analyzer ------------------------------------------------- */
(function() {
  const D = window.TRADE_DATA;
  const root = document.getElementById('trade-analyzer');
  if (!D || !root) return;

  // Canonical DRC -> dollar table (mirror of drc_dollar_lookup / league rules).
  const DOLLARS = {1:200, 2:100, 3:80, 4:60, 5:50, 6:30, 7:30, 8:30, 9:30};
  const $$ = d => DOLLARS[d] || 10;
  const clampDrc = d => Math.max(1, Math.min(16, d));
  const Y0 = D.season;                      // freeze year (2026)
  const YEARS = [Y0, Y0 + 1, Y0 + 2];
  const esc = s => String(s == null ? '—' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const money = n => '$' + n.toLocaleString();
  const teamBy = {}; D.teams.forEach(t => teamBy[t.slug] = t);
  const playersBy = {}; D.players.forEach(p => {
    (playersBy[p.m] = playersBy[p.m] || []).push(p);
  });

  // Trade-time DRC anchor: the player's 2025 DRC; acquirer is frozen there
  // for Y0, then the decrement resumes. Fall back to the keep-path 2026
  // value for the rare player with no 2025 cost on record.
  function anchor(p) { return p.d5 != null ? p.d5 : p.d6; }
  function costRow(p) {
    const a = clampDrc(anchor(p));
    return YEARS.map((y, i) => { const d = clampDrc(a - i); return {y, d, c: $$(d)}; });
  }

  const sides = {};
  root.querySelectorAll('.ta-side').forEach(el => {
    const side = el.dataset.side;
    sides[side] = {el, sel: el.querySelector('.ta-team'),
                   roster: el.querySelector('.ta-roster'),
                   chips: el.querySelector('.ta-pick-chips'),
                   picked: new Set(), picks: []};
    const sel = sides[side].sel;
    D.teams.forEach(t => {
      const o = document.createElement('option');
      o.value = t.slug; o.textContent = t.team + ' — ' + t.mgr;
      sel.appendChild(o);
    });
    const yearSel = el.querySelector('.ta-pick-year');
    [Y0, Y0 + 1].forEach(y => {
      const o = document.createElement('option'); o.value = y; o.textContent = y;
      yearSel.appendChild(o);
    });
    const roundSel = el.querySelector('.ta-pick-round');
    for (let r = 1; r <= 16; r++) {
      const o = document.createElement('option'); o.value = r; o.textContent = 'Round ' + r;
      roundSel.appendChild(o);
    }
    sel.addEventListener('change', () => {
      sides[side].picked.clear();
      sides[side].picks = [];
      renderRoster(side);
      renderChips(side);
      compute();
    });
    el.querySelector('.ta-add-pick').addEventListener('click', () => {
      if (!sel.value) return;
      sides[side].picks.push({y: +yearSel.value, r: +roundSel.value});
      renderChips(side);
      compute();
    });
  });

  function renderRoster(side) {
    const s = sides[side];
    if (!s.sel.value) { s.roster.innerHTML = ''; return; }
    const list = (playersBy[s.sel.value] || []);
    s.roster.innerHTML = list.map(p =>
      '<label class="ta-row"><input type="checkbox" data-pid="' + p.i + '">' +
      '<span class="ta-nm">' + esc(p.n) + '</span>' +
      '<span class="ta-meta">' + esc(p.p) + ' · ' + esc(p.t) + '</span>' +
      '<span class="ta-cost">DRC ' + esc(p.d6) + ' · ' + money(p.c6) + '</span></label>'
    ).join('');
    s.roster.querySelectorAll('input').forEach(cb => {
      cb.addEventListener('change', () => {
        const pid = +cb.dataset.pid;
        cb.checked ? s.picked.add(pid) : s.picked.delete(pid);
        compute();
      });
    });
  }

  function renderChips(side) {
    const s = sides[side];
    s.chips.innerHTML = s.picks.map((pk, idx) =>
      '<span class="ta-chip">' + pk.y + ' R' + pk.r +
      '<button type="button" data-idx="' + idx + '" aria-label="Remove">&times;</button></span>'
    ).join('');
    s.chips.querySelectorAll('button').forEach(b => {
      b.addEventListener('click', () => {
        s.picks.splice(+b.dataset.idx, 1);
        renderChips(side);
        compute();
      });
    });
  }

  function pl(n, w) { return n + ' ' + (n === 1 ? w : w + 's'); }

  function compute() {
    const out = document.getElementById('ta-results');
    const A = sides.a, B = sides.b;
    if (!A.sel.value || !B.sel.value) { out.hidden = true; return; }
    if (A.sel.value === B.sel.value) {
      out.hidden = false;
      out.innerHTML = '<div class="ta-empty">Pick two different teams.</div>';
      return;
    }
    const get = pid => D.players.find(p => p.i === pid);
    const aSends = [...A.picked].map(get), bSends = [...B.picked].map(get);
    if (!aSends.length && !bSends.length && !A.picks.length && !B.picks.length) {
      out.hidden = false;
      out.innerHTML = '<div class="ta-empty">Check at least one player (or add a pick) on either side.</div>';
      return;
    }

    const tA = teamBy[A.sel.value], tB = teamBy[B.sel.value];
    // Receiving columns: A receives what B sends, and vice versa.
    out.hidden = false;
    out.innerHTML =
      '<div class="ta-cols">' +
      recvPanel(tA, bSends, B.picks, tB) +
      recvPanel(tB, aSends, A.picks, tA) +
      '</div>' +
      '<div class="ta-cols" style="margin-top:18px">' +
      bulletPanel(tA, bSends, aSends, B.picks, A.picks) +
      bulletPanel(tB, aSends, bSends, A.picks, B.picks) +
      '</div>';
  }

  function recvPanel(team, playersIn, picksIn, fromTeam) {
    let rows = '', totals = [0, 0, 0], pts = 0;
    playersIn.forEach(p => {
      const tr = costRow(p);
      tr.forEach((c, i) => totals[i] += c.c);
      if (p.pts) pts += p.pts;
      const keepNote = (p.d5 != null && tr[0].d !== p.d6)
        ? '<span class="ta-keepnote">keep-path was DRC ' + p.d6 + ' (' + money(p.c6) + ')</span>' : '';
      rows += '<tr><td class="ta-pname">' + esc(p.n) +
        '<span class="ta-keepnote">' + esc(p.p) + ' · ' + esc(p.t) +
        (p.pr ? ' · ' + esc(p.p) + esc(p.pr) + ' in 2025' : '') +
        (p.adp ? ' · ADP ' + esc(p.adp) : '') + '</span></td>' +
        '<td class="num">' + (p.pts != null ? p.pts.toFixed(1) : '—') + '</td>' +
        '<td class="num ta-frozen">DRC ' + tr[0].d + '<br>' + money(tr[0].c) + keepNote + '</td>' +
        '<td class="num">DRC ' + tr[1].d + '<br>' + money(tr[1].c) + '</td>' +
        '<td class="num">DRC ' + tr[2].d + '<br>' + money(tr[2].c) + '</td></tr>';
    });
    picksIn.forEach(pk => {
      rows += '<tr><td class="ta-pname">' + pk.y + ' Round ' + pk.r + ' pick' +
        '<span class="ta-keepnote">from ' + esc(fromTeam.team) + ' · face value only</span></td>' +
        '<td class="num">—</td><td class="num">—</td><td class="num">—</td><td class="num">—</td></tr>';
    });
    if (playersIn.length) {
      rows += '<tr class="ta-total"><td>Keeper cost if all kept</td><td class="num">' +
        (pts ? pts.toFixed(1) : '—') + '</td>' +
        totals.map(t => '<td class="num">' + money(t) + '</td>').join('') + '</tr>';
    }
    return '<div class="ta-recv"><h3>' + esc(team.team) + ' receives</h3>' +
      '<table class="ta-table"><colgroup><col><col style="width:14%"><col style="width:18%"><col style="width:16%"><col style="width:16%"></colgroup>' +
      '<thead><tr><th>Asset</th><th class="num">2025 pts</th>' +
      '<th class="num">' + YEARS[0] + ' (frozen)</th><th class="num">' + YEARS[1] + '</th>' +
      '<th class="num">' + YEARS[2] + '</th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="5" class="ta-empty">Nothing yet</td></tr>') +
      '</tbody></table></div>';
  }

  function bulletPanel(team, playersIn, playersOut, picksIn, picksOut) {
    const inCost = playersIn.reduce((s, p) => s + costRow(p)[0].c, 0);
    const outCost = playersOut.reduce((s, p) => s + p.c6, 0);
    const newCap = team.cap - outCost + inCost;
    const delta = newCap - team.cap;
    const inPts = playersIn.reduce((s, p) => s + (p.pts || 0), 0);
    const outPts = playersOut.reduce((s, p) => s + (p.pts || 0), 0);
    const commit = playersIn.reduce((s, p) => s + costRow(p).reduce((a, c) => a + c.c, 0), 0);
    const items = [];
    const sign = delta >= 0 ? '+' : '−';
    items.push(YEARS[0] + ' cap: ' + money(team.cap) + ' → ' + money(newCap) +
      ' (<span class="' + (delta > 0 ? 'ta-cap-up' : 'ta-cap-down') + '">' +
      sign + '$' + Math.abs(delta).toLocaleString() + '</span>)');
    items.push('Roster count: ' + (playersOut.length || playersIn.length
      ? pl(playersIn.length, 'player') + ' in, ' + pl(playersOut.length, 'player') + ' out'
      : 'unchanged') + ' (max 18 slots in ' + YEARS[0] + ')');
    items.push('2025 production: receives ' + inPts.toFixed(1) + ' pts, sends ' + outPts.toFixed(1) + ' pts');
    if (playersIn.length) {
      items.push('Three-year keeper commitment on players received (' + YEARS[0] + '–' +
        YEARS[2] + ', if all kept): ' + money(commit));
    }
    if (picksIn.length || picksOut.length) {
      const fmt = arr => arr.map(pk => pk.y + ' R' + pk.r).join(', ');
      if (picksIn.length) items.push('Receives picks: ' + fmt(picksIn) + ' (face value only)');
      if (picksOut.length) items.push('Sends picks: ' + fmt(picksOut));
    }
    return '<div class="ta-recv"><h3>' + esc(team.team) + ' — the facts</h3>' +
      '<ul class="ta-bullets">' + items.map(i => '<li>' + i + '</li>').join('') + '</ul></div>';
  }

  // Reset roster lists when navigating to the tab (cheap re-render).
  const taLink = document.querySelector('.nav-link[data-target="trade-analyzer"]');
  if (taLink) taLink.addEventListener('click', () => {
    ['a', 'b'].forEach(s => { if (sides[s].sel.value) renderRoster(s); });
  });
})();
"""


COMMS_DIR = Path(__file__).parent / "comms"


def _embed_image_b64(rel_path):
    """Read an image relative to COMMS_DIR and return a base64 data URI.
    Returns empty string if file not found so the parser can render a
    fallback. Keeps the dashboard self-contained as one HTML file."""
    import base64 as _b64
    img_path = COMMS_DIR / rel_path
    if not img_path.exists():
        return ""
    suffix = img_path.suffix.lower().lstrip(".")
    mime = "jpeg" if suffix == "jpg" else suffix
    data = img_path.read_bytes()
    return f"data:image/{mime};base64,{_b64.b64encode(data).decode('ascii')}"


def _md_format_inline(text):
    """Inline markdown: escape HTML then apply **bold** / *italic*."""
    import re as _re
    text = html.escape(text)
    text = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = _re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


def _md_to_html(text):
    """Tiny markdown→HTML for Pete's email-style posts. Handles paragraphs,
    multi-line bulleted/numbered lists, asterisk-wrapped headers (with
    internal ** tolerated), team-finish header cards (Name | Key: Val | ...),
    inline-bold sub-section heads (*Heading: *content), images, and inline
    bold/italic."""
    import re as _re
    lines = text.split("\n")
    out = []
    i = 0

    def _is_break_line(ln):
        s = ln.strip()
        if not s:
            return True
        if ln.lstrip().startswith(("- ", "* ", "• ")):
            return True
        if _re.match(r"^\s*\d+\.\s+", ln):
            return True
        if s.startswith("*") and s.endswith("*"):
            return True
        if _re.match(r"^#{1,4}\s+", s):
            return True
        return False

    while i < len(lines):
        start_i = i
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue

        # Standalone image line: ![alt](path)
        img_m = _re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if img_m:
            alt = html.escape(img_m.group(1))
            src = _embed_image_b64(img_m.group(2))
            if src:
                out.append(
                    f'<figure class="desk-figure">'
                    f'<img src="{src}" alt="{alt}" class="desk-img" />'
                    + (f'<figcaption>{alt}</figcaption>' if alt else "")
                    + '</figure>'
                )
            else:
                out.append(
                    f'<div class="desk-img-missing">Missing image: {html.escape(img_m.group(2))}</div>'
                )
            i += 1
            continue

        # Bulleted list with multi-line continuation support
        if line.lstrip().startswith(("- ", "* ", "• ")):
            items = []
            current = None
            while i < len(lines):
                ln = lines[i]
                if not ln.strip():
                    break
                if ln.lstrip().startswith(("- ", "* ", "• ")):
                    if current is not None:
                        items.append(" ".join(current))
                    current = [_re.sub(r"^[\-*•]\s+", "", ln.strip())]
                    i += 1
                elif current is not None and ln.startswith(" "):
                    current.append(ln.strip())
                    i += 1
                else:
                    break
            if current is not None:
                items.append(" ".join(current))
            out.append(
                '<ul class="desk-list">'
                + "".join(f"<li>{_md_format_inline(it)}</li>" for it in items)
                + '</ul>'
            )
            continue

        # Numbered list with multi-line continuation support
        if _re.match(r"^\s*\d+\.\s+", line):
            items = []
            current = None
            while i < len(lines):
                ln = lines[i]
                if not ln.strip():
                    break
                if _re.match(r"^\s*\d+\.\s+", ln):
                    if current is not None:
                        items.append(" ".join(current))
                    current = [_re.sub(r"^\s*\d+\.\s+", "", ln.strip())]
                    i += 1
                elif current is not None and ln.startswith(" "):
                    current.append(ln.strip())
                    i += 1
                else:
                    break
            if current is not None:
                items.append(" ".join(current))
            out.append(
                '<ol class="desk-list">'
                + "".join(f"<li>{_md_format_inline(it)}</li>" for it in items)
                + '</ol>'
            )
            continue

        # Team-finish header card: *Name | Key: Val | Key: Val*
        # Used in Pete's season wrap-ups for each team's section break.
        stripped = line.strip()
        if (
            stripped.startswith("*")
            and stripped.endswith("*")
            and "|" in stripped
        ):
            inner = stripped[1:-1].replace("**", "").strip()
            parts = [p.strip() for p in inner.split("|") if p.strip()]
            if len(parts) >= 2:
                name = parts[0]
                chips = []
                for p in parts[1:]:
                    if ":" in p:
                        k, v = p.split(":", 1)
                        chips.append(
                            '<span class="team-chip">'
                            f'<span class="team-chip-k">{html.escape(k.strip())}</span>'
                            f'<span class="team-chip-v">{html.escape(v.strip())}</span>'
                            '</span>'
                        )
                    else:
                        chips.append(
                            '<span class="team-chip">'
                            f'<span class="team-chip-v">{html.escape(p)}</span>'
                            '</span>'
                        )
                out.append(
                    '<div class="team-finish-header">'
                    f'<h3 class="team-finish-name">{html.escape(name)}</h3>'
                    f'<div class="team-finish-chips">{"".join(chips)}</div>'
                    '</div>'
                )
                i += 1
                continue

        # Plain *fully-wrapped header* (tolerate internal ** noise)
        if stripped.startswith("*") and stripped.endswith("*"):
            inner = stripped[1:-1].replace("**", "")
            # Only treat as a clean header if there are no stray asterisks left
            # AND it doesn't look like the "inline-head + content" pattern below
            if "*" not in inner and not _re.search(r":\s*$", inner) and ":" not in inner[:60]:
                out.append(f'<h3 class="desk-h3">{html.escape(inner.strip())}</h3>')
                i += 1
                continue

        # Sub-section "inline-bold heading" pattern: *Heading: *content...
        # Common in Pete's wrap-ups: a bolded label followed by inline prose.
        m = _re.match(r"^\s*\*([^*]+?):\s*\*(.*)$", line)
        if m:
            heading = m.group(1).strip()
            rest = m.group(2).strip()
            out.append(f'<h4 class="desk-subhead">{html.escape(heading)}</h4>')
            para = [rest] if rest else []
            i += 1
            while i < len(lines) and lines[i].strip() and not _is_break_line(lines[i]):
                para.append(lines[i].strip())
                i += 1
            if para:
                out.append(f"<p>{_md_format_inline(' '.join(para))}</p>")
            continue

        # Standard markdown headers (# / ## / ### / ####)
        m = _re.match(r"^(#{1,4})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            out.append(f'<h{level} class="desk-h{level}">{_md_format_inline(m.group(2))}</h{level}>')
            i += 1
            continue

        # Default: paragraph — gather contiguous non-break lines
        para = []
        while i < len(lines) and lines[i].strip() and not _is_break_line(lines[i]):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{_md_format_inline(' '.join(para))}</p>")

        # Safety: never get stuck on a single line. If no branch above advanced
        # `i`, consume the line as a plain paragraph so the loop always
        # terminates. Prevents pathological inputs from hanging the build.
        if i == start_i:
            out.append(f"<p>{_md_format_inline(line.strip())}</p>")
            i += 1
    return "\n".join(out)


def _parse_frontmatter(raw):
    """Parse simple YAML-like frontmatter delimited by --- ... ---.
    Returns (metadata_dict, body_text)."""
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.split("\n")
    if lines[0].strip() != "---":
        return {}, raw
    meta = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        ln = lines[i]
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()
        i += 1
    body = "\n".join(lines[i + 1:]).strip()
    return meta, body


def load_comms_posts():
    """Read all *.md files from comms/, parse frontmatter + body, return list
    sorted most-recent first."""
    if not COMMS_DIR.exists():
        return []
    posts = []
    for md_path in COMMS_DIR.glob("*.md"):
        raw = md_path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        posts.append({
            "slug": meta.get("slug") or md_path.stem,
            "title": meta.get("title") or md_path.stem,
            "date": meta.get("date") or "1970-01-01",
            "summary": meta.get("summary") or "",
            "body_html": _md_to_html(body),
        })
    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts


def render_commissioners_desk_section(posts):
    """The 'Commissioner's Desk' section: left rail of posts + selected
    post content on the right. JS swaps the visible post on click."""
    if not posts:
        return ""
    rail_links = []
    post_panels = []
    for i, p in enumerate(posts):
        try:
            date_dt = datetime.strptime(p["date"], "%Y-%m-%d")
            date_display = date_dt.strftime("%b %-d, %Y")
        except (ValueError, TypeError):
            try:
                date_dt = datetime.strptime(p["date"], "%Y-%m-%d")
                date_display = date_dt.strftime("%b %d, %Y")
            except Exception:
                date_display = p["date"]
        active_cls = " desk-active" if i == 0 else ""
        rail_links.append(
            f'<a class="desk-post-link{active_cls}" data-desk-target="post-{p["slug"]}">'
            f'<div class="desk-post-link-title">{html.escape(p["title"])}</div>'
            f'<div class="desk-post-link-date">{date_display}</div>'
            '</a>'
        )
        hidden_attr = "" if i == 0 else " hidden"
        post_panels.append(
            f'<article id="post-{p["slug"]}" class="desk-post"{hidden_attr}>'
            '<header class="desk-post-header">'
            f'<h2 class="desk-post-title">{html.escape(p["title"])}</h2>'
            f'<div class="desk-post-meta">{date_display}</div>'
            f'<p class="desk-post-summary">{html.escape(p["summary"])}</p>'
            '</header>'
            f'<div class="desk-post-body">{p["body_html"]}</div>'
            '</article>'
        )
    return f"""
    <section class="team-section" id="commissioners-desk" hidden>
      <header class="section-header">
        <h1 class="section-title">Commissioner's Desk</h1>
        <p class="section-sub">League dispatches, previews, and post-mortems. {len(posts)} entries on file.</p>
      </header>
      <div class="desk-layout">
        <aside class="desk-rail">{"".join(rail_links)}</aside>
        <div class="desk-content">{"".join(post_panels)}</div>
      </div>
    </section>"""


def render_about_section():
    """Welcome/about page. Brief tour of what the dashboard is, how it's
    organized, and how to give feedback. Image placeholders below each
    section description are for Pete to drop screenshots into later."""
    return """
    <section class="team-section" id="about" hidden>
      <header class="section-header">
        <h1 class="section-title">About this dashboard</h1>
        <p class="section-sub">A live ledger of <em>I Yearn For Your Sweet TD's</em> &mdash; keeper costs, draft history, trades, and league communications. Built for the 2026 season and ongoing.</p>
      </header>

      <div class="about-grid">

        <section class="about-block">
          <h2 class="about-h2">What is this?</h2>
          <p>This site replaces the manual Excel sheet Pete has been keeping since 2023 with an automatically-refreshed view of the league. The goal is to surface every manager's keeper cost, draft picks, and trade history in one place &mdash; with the math worked out and the rules linked &mdash; so we can spend less time arguing about numbers and more time arguing about everything else.</p>
        </section>

        <section class="about-block">
          <h2 class="about-h2">How to navigate</h2>
          <p>The sidebar on the left has two groups:</p>
          <ul class="about-list">
            <li><strong>League view</strong> &mdash; everything that's leaguewide. Summary, player search, commissioner's writeups, and the rules.</li>
            <li><strong>Teams</strong> &mdash; click any team to see its 2026 keepers, draft history, and trades. Tap the "+" to expand the team list.</li>
          </ul>
          <p>On mobile, the sidebar lives behind a small "MENU" tab on the left edge of the screen &mdash; tap it any time to open the menu, tap outside it to close.</p>
        </section>

        <section class="about-block">
          <h2 class="about-h2">Sections you'll find</h2>

          <h3 class="about-h3">Summary &amp; standings</h3>
          <p>The opening view. Total league cap committed, average keeper spend per team, premium-tier keepers leaguewide, and a ranked table of who's spent what for 2026.</p>

          <h3 class="about-h3">Player search</h3>
          <p>Type any player's name and a dropdown of matches appears. Click one (or hit Enter) to open that player's full profile: DRC cost over time, season-by-season fantasy production, weekly bar charts, ownership lineage, and where they rank against the players above and below them at their position.</p>

          <h3 class="about-h3">Trade analyzer</h3>
          <p>Pick two teams, check the players (and draft picks) going each way, and the tool lays out what's actually exchanged: 2025 production, market value, and &mdash; the part Yahoo can't show you &mdash; what each player costs to keep in 2026 and the out-years under the trade-freeze rule. It states facts and totals only; it will never tell you whether to do the trade.</p>

          <h3 class="about-h3">Commissioner's Desk</h3>
          <p>Pete's writeups &mdash; draft grades, season recaps, weekly previews, draft-day announcements. The left rail is the index; the most recent entry opens by default.</p>

          <h3 class="about-h3">League rules</h3>
          <p>The DRC cost table, decrement rules, trade-freeze logic, the slide rule (including the new pick chasm constraint), draft order, the FAAB washing rule, and the amended 2026-27 lottery weights.</p>

          <h3 class="about-h3">Per-team pages</h3>
          <p>Each team has three tabs: <strong>Roster</strong> (every player on the 2025 end-of-season roster with their 2026 keeper cost), <strong>Drafts</strong> (every draft pick this manager has made, year by year, with the trajectory of each keeper), and <strong>Trades</strong> (every trade event with weekly fantasy points on both sides so we can see who really won).</p>
        </section>

        <section class="about-block">
          <h2 class="about-h2">If something looks wrong</h2>
          <p>Click the <strong>Feedback</strong> button in the bottom-right corner. Tell Pete what you saw, what you expected, and which page you were on. Include your name so he can follow up. The DRC math is auditable but the historical transaction record is patchy in places &mdash; managers spotting their own discrepancies is the fastest way to fix them.</p>
        </section>

        <section class="about-block">
          <h2 class="about-h2">What's next</h2>
          <p>Between now and the June 26 Summit, expect to see:</p>
          <ul class="about-list">
            <li>2026 draft pick allocations once the 12th manager is confirmed</li>
            <li>A keeper roster simulator (Brian's tool, integrated)</li>
            <li>More writeups in the Commissioner's Desk as the season approaches</li>
            <li>Whatever else managers ask for via the feedback widget</li>
          </ul>
        </section>

      </div>
    </section>"""


def render_feedback_widget():
    """Floating bottom-right button + modal for collecting manager feedback.
    Submit composes a prefilled mailto: link with Name and Message in the body.
    Static-friendly (no backend needed). Can be swapped for a form service
    later by changing FEEDBACK_ACTION below."""
    return """
    <button class="fb-trigger" id="fb-trigger" type="button" aria-label="Open feedback form">
      <span class="fb-icon">&#9993;</span>
      <span class="fb-label">Feedback</span>
    </button>
    <div class="fb-overlay" id="fb-overlay" hidden></div>
    <div class="fb-modal" id="fb-modal" role="dialog" aria-labelledby="fb-title" hidden>
      <header class="fb-modal-header">
        <h2 id="fb-title" class="fb-modal-title">Send feedback to Pete</h2>
        <button class="fb-close" id="fb-close" type="button" aria-label="Close">&times;</button>
      </header>
      <p class="fb-modal-sub">Spotted something wrong? Have a question, suggestion, or rant? Drop it in.</p>
      <form id="fb-form" class="fb-form">
        <label class="fb-label">
          <span class="fb-field-label">Your name</span>
          <input type="text" name="name" id="fb-name" required placeholder="Who's asking?" autocomplete="name">
        </label>
        <label class="fb-label">
          <span class="fb-field-label">Message</span>
          <textarea name="message" id="fb-message" rows="6" required placeholder="What's on your mind?"></textarea>
        </label>
        <div class="fb-actions">
          <button type="button" class="fb-btn fb-btn-secondary" id="fb-cancel">Cancel</button>
          <button type="submit" class="fb-btn fb-btn-primary">Send</button>
        </div>
        <p class="fb-modal-foot">Submitting will open your email app with the message pre-filled. Hit send there to deliver it.</p>
      </form>
    </div>"""


def render_rules_section():
    """League rules page. Static content, organized into clearly bounded
    sections, styled to match the rest of the dashboard."""
    return """
    <section class="team-section" id="league-rules" hidden>
      <header class="section-header">
        <h1 class="section-title">League rules</h1>
        <p class="section-sub">The framework that governs <em>I Yearn For Your Sweet TD's</em>. Adopted rules below; recently passed motions are flagged.</p>
      </header>

      <div class="rules-grid">

        <section class="rule-block">
          <h2 class="rule-h2">League framework</h2>
          <p>Twelve teams. Each manager pays a <strong>$100 annual buy-in</strong> to renew their league seat, which also locks in one keeper slot. Every additional keeper has its own dollar cost on top of the buy-in (see Draft Round Cost below). There is <strong>no cap</strong> on the number of keepers a team may roster.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Draft Round Cost (DRC) &mdash; the keeper economy</h2>
          <p>DRC is a per-player integer between 1 and 16 that represents the keeper cost of a player. A player's DRC starts equal to the round they were originally drafted in (a R5 pick has DRC = 5). <strong>Lower DRC = more expensive to keep.</strong></p>
          <h3 class="rule-h3">Dollar cost by DRC tier</h3>
          <table class="rules-table">
            <thead><tr><th>DRC</th><th>Round equivalent</th><th class="num">Dollar cost</th></tr></thead>
            <tbody>
              <tr><td>1</td><td>Round 1</td><td class="num">$200</td></tr>
              <tr><td>2</td><td>Round 2</td><td class="num">$100</td></tr>
              <tr><td>3</td><td>Round 3</td><td class="num">$80</td></tr>
              <tr><td>4</td><td>Round 4</td><td class="num">$60</td></tr>
              <tr><td>5</td><td>Round 5</td><td class="num">$50</td></tr>
              <tr><td>6&ndash;9</td><td>Rounds 6&ndash;9</td><td class="num">$30</td></tr>
              <tr><td>10&ndash;16</td><td>Rounds 10&ndash;16</td><td class="num">$10</td></tr>
            </tbody>
          </table>
          <p class="rules-note">All keeper dollars accumulate in the league pot and are paid out at season end.</p>
          <h3 class="rule-h3">How DRC moves year over year</h3>
          <p>Each year a player is kept by the same drafting team <em>with no transactions in between</em>, their DRC decrements by one tier (becomes more expensive by one round). Decrement compounds annually until it hits the floor at DRC 1. There is no cap on how many years a player can be kept.</p>
          <p>For example, a R6 draft pick (DRC 6, $30) kept by the same manager untouched: DRC 5 ($50) the next year, DRC 4 ($60) the year after, and so on toward DRC 1.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Trades</h2>
          <h3 class="rule-h3">Trade review</h3>
          <p>All trades undergo a <strong>48-hour review window</strong>. During the window, other teams may counter the original agreement with a better offer. A successful counter must include at least one of the players involved in the original trade. The 48-hour timer starts at the acceptance of the original trade; successful counters do <em>not</em> reset it.</p>
          <h3 class="rule-h3">DRC freeze on trade</h3>
          <p>When a player is traded, their DRC is <strong>frozen for one season</strong> at the value they carried at the moment of the trade. After that freeze season, the normal year-over-year decrement resumes.</p>
          <ul class="rules-list">
            <li><strong>Off-season trade</strong> &mdash; the freeze applies to the upcoming season. The receiving manager pays the frozen DRC for one year, then decrement begins.</li>
            <li><strong>Mid-season trade</strong> &mdash; the freeze extends one year past the trade year. Decrement begins the year after that.</li>
          </ul>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Waivers and free agents</h2>
          <p>Players added off waivers or free agency anchor at <strong>DRC 16 ($10)</strong> &mdash; the cheapest tier. They behave like fresh draft picks from that anchor point: kept untouched, they decrement annually toward DRC 1.</p>
          <p>If a player was originally drafted, then dropped, then re-acquired off waivers, the waiver pickup is the new anchor. The original draft round is discarded.</p>
          <p class="rules-note">This applies to drops that happen during the live regular-season transaction window. Off-season "drops" are typically commissioner mechanics and do not trigger the reset.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">The slide rule</h2>
          <p>Only one keeper can occupy any given round of the draft. When multiple keepers share the same DRC tier on one roster (e.g., two DRC 1 keepers), the "loser" slides into the next available round's slot. <strong>The slide is purely a mechanical placement</strong> &mdash; the player's actual DRC and dollar cost do not change.</p>
          <p>The slide works in both directions at the boundaries: DRC 1 keepers in conflict slide <em>down</em> into Round 2, 3, etc. DRC 16 keepers in conflict slide <em>up</em> into Round 15, 14, etc., because there is no Round 17 to slide into.</p>
        </section>

        <section class="rule-block rule-block-new">
          <div class="rule-new-pill">Newly passed</div>
          <h2 class="rule-h2">Pick chasm rule</h2>
          <p>The slide rule has a hard limit: a keeper can only slide into a draft slot that the manager <em>still owns</em>. If a manager has traded away the round their keeper would need to slide to, they have created a chasm the player cannot span &mdash; and <strong>that player becomes ineligible to keep</strong>.</p>
          <p>Example: a manager has two DRC 1 keepers, both wanting the Round 1 slot. Normally the slide rule would push the second keeper into the Round 2 slot. But if that manager has traded away their Round 2 pick, there is no slot for the second keeper to occupy. The second keeper becomes un-keepable and must be released back to the draft pool.</p>
          <p class="rules-note">This is a strategic constraint at keeper-designation time, not a runtime cost computation. Trade pick activity should be planned with the keeper roster in mind.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Keeper selection</h2>
          <p>Before the draft each year, every manager designates which players from their end-of-season roster they want to keep. Designated keepers occupy their assigned draft slot (per the slide rule above). Players not designated are released back to the draft pool.</p>
          <p>If a manager has two keepers with the same DRC tier and only one round slot is available, they must choose: drop one to the draft pool, or trade one in the off-season before the keeper deadline. If a higher round slot is available, a traded-in player can be moved to it &mdash; but the player's underlying DRC and dollar cost remain at their original value.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Draft order</h2>
          <h3 class="rule-h3">Playoff teams (picks 7&ndash;12, reverse order)</h3>
          <ul class="rules-list">
            <li>Pick 12: champion</li>
            <li>Pick 11: runner-up</li>
            <li>Pick 10: semifinal loser, higher season points</li>
            <li>Pick 9: semifinal loser, lower season points</li>
            <li>Pick 8: quarterfinal loser, higher season points</li>
            <li>Pick 7: quarterfinal loser, lower season points</li>
          </ul>
          <h3 class="rule-h3">Non-playoff teams (picks 1&ndash;6, weighted lottery)</h3>
          <p>The bottom six teams enter a weighted lottery for the first overall pick. The remaining order is filled out from that result.</p>
          <div class="rule-sub-callout">
            <div class="rule-new-pill">Newly passed &mdash; effective 2026-2027</div>
            <p>At the 2025 Beach Summit, the lottery weights were inverted. Where the original system rewarded the worst finishers with the best odds, the amended system rewards the team that <em>just missed</em> the playoffs &mdash; reducing tanking incentive.</p>
            <table class="rules-table rules-table-compare">
              <thead>
                <tr><th>Regular-season finish</th><th class="num">Original (2025-2026)</th><th class="num rules-th-new">Amended (2026-2027)</th></tr>
              </thead>
              <tbody>
                <tr><td>7th place</td><td class="num">10%</td><td class="num rules-td-new">50%</td></tr>
                <tr><td>8th place</td><td class="num">10%</td><td class="num rules-td-new">15%</td></tr>
                <tr><td>9th place</td><td class="num">15%</td><td class="num rules-td-new">12.5%</td></tr>
                <tr><td>10th place</td><td class="num">15%</td><td class="num rules-td-new">10%</td></tr>
                <tr><td>11th place</td><td class="num">25%</td><td class="num rules-td-new">7.5%</td></tr>
                <tr><td>12th place</td><td class="num">25%</td><td class="num rules-td-new">5%</td></tr>
              </tbody>
            </table>
            <p class="rules-note">Odds shown are for the first overall pick only. The remaining lottery slots fill out based on the same weights with the winning team removed each round.</p>
          </div>
        </section>

        <section class="rule-block rule-block-new">
          <div class="rule-new-pill">Newly passed &mdash; 7-3 vote</div>
          <h2 class="rule-h2">FAAB washing rule</h2>
          <p>A manager <strong>cannot drop a player and immediately reclaim them for FAAB</strong> on the next waiver wire cycle. If you drop a player, they must clear waivers first and become a free agent before you can pick them back up.</p>
          <p class="rules-note">This rule applies specifically to managers with the most FAAB in the league. Commissioner discretion determines whether a given transaction qualifies as FAAB washing or a legitimate roster move.</p>
        </section>

      </div>
    </section>"""


def render_player_search_section(search_players):
    """League-wide player search view. Renders every player as a card; the
    cards are hidden by default and JS reveals matches as the user types
    (>= 2 chars). Each card has hero header, three summary cards, ownership
    lineage timeline, and a chronological transaction log."""
    cards = []
    for p in search_players:
        events_recent = p["events"][-3:] if p["events"] else []
        events_html = "".join(
            f'<div class="ps-event">'
            f'<span class="ps-event-date">{html.escape(e["date"])}</span>'
            f'<span class="ps-event-desc">{html.escape(e["desc"])}</span>'
            f'</div>'
            for e in events_recent
        ) or '<div class="ps-event-empty">No events recorded.</div>'

        owner_chip = (
            f'<span class="ps-owner">Currently: {html.escape(p["current_owner"])}</span>'
            if p["current_owner"] else
            '<span class="ps-owner ps-owner-none">No current owner</span>'
        )

        SPARK_YEARS = (2023, 2024, 2025)
        WEEKS_PER_YEAR = 17
        all_weekly = []
        for yr in SPARK_YEARS:
            all_weekly.extend(p["weekly_by_year"].get(yr, {}).values())
        max_pts = max(all_weekly) if all_weekly else 0
        if max_pts <= 0:
            max_pts = 1.0

        def _render_year_bars(weekly, max_val):
            W, H = 170, 70
            PAD_TOP, PAD_BOTTOM = 4, 4
            slot_w = W / WEEKS_PER_YEAR
            bar_w = slot_w * 0.62
            chart_h = H - PAD_TOP - PAD_BOTTOM
            if not weekly:
                return (
                    f'<svg class="ps-chart-svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
                    f'<text x="{W/2:.0f}" y="{H/2 + 3:.0f}" text-anchor="middle" '
                    'fill="#9ca3af" font-size="9" font-family="Inter">No data</text>'
                    '</svg>'
                )
            elements = []
            for wk in range(1, WEEKS_PER_YEAR + 1):
                pts = weekly.get(wk)
                bar_x = (wk - 1) * slot_w + (slot_w - bar_w) / 2
                if pts is None or pts <= 0:
                    elements.append(
                        f'<rect x="{bar_x:.1f}" y="{H - PAD_BOTTOM - 1:.1f}" '
                        f'width="{bar_w:.1f}" height="1" rx="0.5" fill="var(--gray-200)" />'
                    )
                    continue
                bh = (pts / max_val) * chart_h
                bar_y = H - PAD_BOTTOM - bh
                elements.append(
                    f'<rect x="{bar_x:.1f}" y="{bar_y:.1f}" '
                    f'width="{bar_w:.1f}" height="{bh:.1f}" rx="1" fill="var(--blue-600)" />'
                )
            return (
                f'<svg class="ps-chart-svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
                f'{"".join(elements)}'
                '</svg>'
            )

        year_cols = []
        has_any_weekly = bool(all_weekly)
        for yr in SPARK_YEARS:
            weekly = p["weekly_by_year"].get(yr, {})
            yr_data = next((py for py in p["per_year"] if py["year"] == yr), {})
            rank = yr_data.get("pos_rank")
            rank_str = f"{p['position']}{rank}" if rank and p["position"] != "—" else "—"
            adp_yr = yr_data.get("adp")
            adp_str = f"ADP {adp_yr:.1f}" if adp_yr is not None else "—"
            pts_yr = yr_data.get("pts")
            pts_str = f"{pts_yr:.1f} pts" if pts_yr is not None else "—"

            nbs = p.get("neighbors_by_year", {}).get(yr, [])
            if nbs:
                nb_rows = "".join(
                    '<tr class="' + ('ps-nb-self' if n["is_self"] else '') + '">'
                    f'<td class="ps-nb-rank">{html.escape(n["label"])}</td>'
                    f'<td class="ps-nb-name">{html.escape(n["name"])}</td>'
                    f'<td class="ps-nb-pts">{n["pts"]:.1f}</td>'
                    '</tr>'
                    for n in nbs
                )
                nb_table = f'<table class="ps-nb-table">{nb_rows}</table>'
            else:
                nb_table = '<div class="ps-nb-empty">No rank context for this year</div>'

            year_cols.append(
                '<div class="ps-chart-col">'
                f'<div class="ps-chart-wrap">{_render_year_bars(weekly, max_pts)}</div>'
                '<div class="ps-chart-labels">'
                f'<div class="ps-chart-year">{yr}</div>'
                '<div class="ps-chart-stats">'
                f'<span class="ps-chart-rank">{rank_str}</span>'
                '<span class="ps-chart-sep">&middot;</span>'
                f'<span class="ps-chart-pts">{pts_str}</span>'
                '<span class="ps-chart-sep">&middot;</span>'
                f'<span class="ps-chart-adp">{adp_str}</span>'
                '</div>'
                '</div>'
                f'{nb_table}'
                '</div>'
            )
        if has_any_weekly:
            card_trajectory = f'<div class="ps-charts-row">{"".join(year_cols)}</div>'
        else:
            card_trajectory = '<div class="ps-spark-empty">No weekly data ingested for this player.</div>'

        cur = next((y for y in p["per_year"] if y["year"] == 2026), None)
        if cur and cur["drc"] is not None:
            drc_hero_big = f"${cur['dollars']}"
            drc_hero_sub = f"DRC {cur['drc']} &middot; 2026 keeper cost"
        else:
            drc_hero_big = "—"
            drc_hero_sub = "Not owned in 2026"
        drc_tiles = []
        for yr in (2023, 2024, 2025):
            y = next((py for py in p["per_year"] if py["year"] == yr), None)
            if y and y["drc"] is not None:
                val = f"${y['dollars']}"
                sub = f"DRC {y['drc']}"
            else:
                val = "—"
                sub = "Not owned"
            drc_tiles.append(
                '<div class="ps-side-tile">'
                f'<div class="ps-side-val">{val}</div>'
                f'<div class="ps-side-sub">{sub}</div>'
                f'<div class="ps-side-yr">{yr}</div>'
                '</div>'
            )
        section_drc = (
            '<div class="ps-side">'
            '<div class="ps-side-label">DRC</div>'
            '<div class="ps-hero ps-hero-drc">'
            f'<div class="ps-hero-big">{drc_hero_big}</div>'
            f'<div class="ps-hero-sub">{drc_hero_sub}</div>'
            '</div>'
            f'<div class="ps-side-tiles">{"".join(drc_tiles)}</div>'
            '</div>'
        )

        adp_2026 = p.get("adp_2026")
        if adp_2026 is not None:
            perf_hero_big = f"{adp_2026:.1f}"
            perf_hero_sub = "2026 ADP &middot; Average draft position"
        else:
            perf_hero_big = "—"
            perf_hero_sub = "No 2026 ADP data"
        perf_tiles = []
        for yr in (2023, 2024, 2025):
            y = next((py for py in p["per_year"] if py["year"] == yr), None)
            rank = y.get("pos_rank") if y else None
            if rank is not None and p["position"] != "—":
                val = f"{p['position']}{rank}"
            else:
                val = "—"
            adp_yr = y.get("adp") if y else None
            sub = f"ADP {adp_yr:.1f}" if adp_yr is not None else "No ADP"
            perf_tiles.append(
                '<div class="ps-side-tile">'
                f'<div class="ps-side-val">{val}</div>'
                f'<div class="ps-side-sub">{sub}</div>'
                f'<div class="ps-side-yr">{yr}</div>'
                '</div>'
            )
        section_perf = (
            '<div class="ps-side">'
            '<div class="ps-side-label">Performance &amp; market</div>'
            '<div class="ps-hero ps-hero-adp">'
            f'<div class="ps-hero-big">{perf_hero_big}</div>'
            f'<div class="ps-hero-sub">{perf_hero_sub}</div>'
            '</div>'
            f'<div class="ps-side-tiles">{"".join(perf_tiles)}</div>'
            '</div>'
        )

        lineage_recent = p["lineage"][-3:] if p["lineage"] else []
        lineage_nodes = []
        for i, node in enumerate(lineage_recent):
            if i > 0:
                lineage_nodes.append('<div class="lineage-arrow">&rarr;</div>')
            method_class = node["method"].lower().replace(" ", "-")
            lineage_nodes.append(
                f'<div class="lineage-node lineage-{method_class}">'
                f'<div class="lineage-date">{html.escape(node["date"])}</div>'
                f'<div class="lineage-manager">{html.escape(node["manager"])}</div>'
                f'<div class="lineage-method">{html.escape(node["method"])}</div>'
                f'<div class="lineage-detail">{html.escape(node["detail"])}</div>'
                '</div>'
            )
        lineage_html = (
            '<div class="lineage-flow">' + "".join(lineage_nodes) + '</div>'
            if lineage_nodes
            else '<div class="ps-event-empty">No lineage recorded.</div>'
        )

        norm = p["name"].lower()
        cards.append(f"""
        <div class="player-card" data-name="{html.escape(norm)}" data-display-name="{html.escape(p['name'])}" hidden>
          <div class="player-card-header">
            <div class="player-card-title">
              <span class="player-card-name">{html.escape(p['name'])}</span>
              <span class="player-card-meta">{html.escape(p['position'])} &middot; {html.escape(p['nfl_team'])}</span>
            </div>
            {owner_chip}
          </div>

          <div class="ps-two-col">
            {section_drc}
            {section_perf}
          </div>

          <div class="ps-section">
            <div class="ps-section-label">Weekly fantasy points</div>
            {card_trajectory}
          </div>

          <div class="ps-section">
            <div class="ps-section-label">Ownership lineage</div>
            {lineage_html}
          </div>

          <div class="ps-section">
            <div class="ps-section-label">Recent activity</div>
            <div class="player-card-events">{events_html}</div>
          </div>
        </div>""")
    cards_html = "".join(cards)
    return f"""
    <section class="team-section" id="player-search" hidden>
      <header class="section-header">
        <h1 class="section-title">Player search</h1>
        <p class="section-sub">Type a player's name to see their full transaction history across the league.</p>
      </header>
      <div class="ps-input-wrap">
        <input type="search" id="player-search-input" class="ps-input"
               placeholder="Search any player..." autocomplete="off" spellcheck="false">
        <div class="ps-input-meta">Showing players who have appeared on any roster, draft, or transaction.</div>
      </div>
      <div id="ps-suggestions" class="ps-suggestions" hidden></div>
      <div id="ps-empty" class="ps-empty-state">Type at least 2 characters to search.</div>
      <div id="ps-no-results" class="ps-empty-state" hidden>No players match that search.</div>
      <div id="ps-results" class="ps-results">{cards_html}</div>
    </section>"""


def build_sidebar(by_manager):
    teams = sorted(by_manager.values(), key=lambda d: d["team_name"].lower())
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
      <a class="nav-link" data-target="about">About this dashboard</a>
      <a class="nav-link" data-target="summary">Summary &amp; standings</a>
      <a class="nav-link" data-target="player-search">Player search</a>
      <a class="nav-link" data-target="trade-analyzer">Trade analyzer</a>
      <a class="nav-link" data-target="commissioners-desk">Commissioner's Desk</a>
      <a class="nav-link" data-target="league-rules">League rules</a>

      <details class="sidebar-teams">
        <summary>Teams</summary>
        <div class="sidebar-team-list">{items}</div>
      </details>
    </aside>"""


def render_trade_analyzer(by_manager):
    """Trade analyzer tab: pick two teams, check players/picks moving each
    way, see production exchanged and keeper-cost trajectories under the
    trade-freeze rule. Facts and totals only — never a verdict.

    Cost model (league rules, confirmed via Lamar/Higgins worked examples):
      - Acquirer inherits the player's trade-time DRC (their most recent
        season's DRC, i.e. 2025), FROZEN for the first season after the
        trade (2026 for an off-season trade now).
      - Decrement-by-1 resumes the following year; DRC floors at 1.
      - The current owner's keep path has no freeze: their 2026 DRC is the
        already-decremented value the dashboard computes.
    """
    teams = []
    players = []
    for name, data in sorted(by_manager.items()):
        slug = slugify(data["manager_actual"])
        teams.append({
            "slug": slug,
            "team": data["team_name"],
            "mgr": data["manager"],
            "cap": data["total_drc_dollars"],
        })
        for p in data["players"]:
            h25 = (p.get("history") or {}).get(2025) or {}
            pts = h25.get("pts")
            pr = h25.get("pos_rank")
            players.append({
                "i": p["player_id"],
                "n": p["name"],
                "p": p["position"],
                "t": p["nfl_team"],
                "m": slug,
                "d6": p["drc"],                 # 2026 DRC on current owner's keep path
                "c6": p["drc_dollars"],         # 2026 $ on current owner's keep path
                "d5": h25.get("drc"),           # trade-time DRC anchor (2025)
                "pts": round(pts, 1) if isinstance(pts, (int, float)) else None,
                "pr": pr,
                "adp": p.get("adp_2026"),
            })

    data_json = json.dumps({"teams": teams, "players": players,
                            "season": TARGET_SEASON}, separators=(",", ":"))

    return f"""
    <section class="team-section" id="trade-analyzer" hidden>
      <header class="section-header">
        <h1 class="section-title">Trade analyzer</h1>
        <p class="section-sub">Pick two teams and check what's moving each way. The tool totals the production exchanged and lays out each player's keeper cost for {TARGET_SEASON} and the out-years under the trade-freeze rule. Numbers, not advice &mdash; the call is yours.</p>
      </header>

      <div class="ta-grid">
        <div class="ta-side" data-side="a">
          <label class="ta-label">Team A</label>
          <select class="ta-team"><option value="">Select team&hellip;</option></select>
          <div class="ta-roster"></div>
          <div class="ta-picks">
            <span class="ta-label">Add a draft pick</span>
            <select class="ta-pick-year"></select>
            <select class="ta-pick-round"></select>
            <button type="button" class="ta-add-pick">Add</button>
            <div class="ta-pick-chips"></div>
          </div>
        </div>
        <div class="ta-side" data-side="b">
          <label class="ta-label">Team B</label>
          <select class="ta-team"><option value="">Select team&hellip;</option></select>
          <div class="ta-roster"></div>
          <div class="ta-picks">
            <span class="ta-label">Add a draft pick</span>
            <select class="ta-pick-year"></select>
            <select class="ta-pick-round"></select>
            <button type="button" class="ta-add-pick">Add</button>
            <div class="ta-pick-chips"></div>
          </div>
        </div>
      </div>

      <div class="ta-results" id="ta-results" hidden></div>

      <p class="ta-foot">Cost projections assume the trade completes before the {TARGET_SEASON} draft: the acquiring team inherits each player's trade-time DRC, frozen for {TARGET_SEASON}, with the normal decrement resuming the year after. Draft picks are listed at face value only &mdash; slide and pick-chasm effects are not modeled. Off-season trades are executed by the commissioner (Yahoo limitation), so loop Pete in to finalize anything you agree on.</p>
    </section>
    <script>window.TRADE_DATA = {data_json};</script>"""


def render_html(by_manager, search_players, comms_posts, generated_at):
    sidebar = build_sidebar(by_manager)
    summary = render_summary_section(by_manager, generated_at)
    player_search = render_player_search_section(search_players)
    trade_analyzer = render_trade_analyzer(by_manager)
    desk = render_commissioners_desk_section(comms_posts)
    rules = render_rules_section()
    about = render_about_section()
    feedback = render_feedback_widget()
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
<button class="menu-toggle" aria-label="Open menu" type="button"><span class="menu-icon"><span></span></span> Menu</button>
<button class="sidebar-tab" aria-label="Open menu" type="button"><span>Menu</span></button>
<div class="sidebar-backdrop"></div>
{sidebar}
<main class="content">
{about}
{summary}
{player_search}
{trade_analyzer}
{desk}
{rules}
{team_sections}
{feedback}
</main>
</div>
<script>{JS}</script>
</body>
</html>"""


def main():
    by_manager, failures, search_players = build_data()
    comms_posts = load_comms_posts()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_out = render_html(by_manager, search_players, comms_posts, generated_at)
    OUT_PATH.write_text(html_out, encoding="utf-8")

    print(f"Wrote {OUT_PATH}")
    total_players = sum(len(d["players"]) for d in by_manager.values())
    print(f"  {len(by_manager)} managers, {total_players} players, {len(failures)} failures")
    for mgr, name in failures:
        print(f"  FAILED: {mgr} - {name}")


if __name__ == "__main__":
    main()
