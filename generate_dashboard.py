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

    # Manager → team_name (current 2026 name, falling back to 2025)
    team_names = {r["manager_id"]: r["team_name"] for r in conn.execute(
        "SELECT manager_id, team_name FROM teams WHERE season = 2025")}
    team_names.update({r["manager_id"]: r["team_name"] for r in conn.execute(
        "SELECT manager_id, team_name FROM teams WHERE season = 2026")})

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

    # ---- Apply current-season (2026) off-season trades ------------------
    # The 2025-final-roster walk above predates any 2026 trades. Move traded
    # players to their new manager and freeze them at their trade-time DRC
    # (their 2025 DRC) per the trade-freeze rule. Source: season-2026
    # synthetic trades (manual entry while the Yahoo API is dead). In-season
    # 2026 trade handling is designed separately (live-season tracking).
    moves = conn.execute("""
        SELECT sp.player_id, m_dst.full_name AS dst, m_src.full_name AS src
        FROM synthetic_transactions st
        JOIN synthetic_transaction_players sp ON sp.synth_id = st.synth_id
        JOIN teams t_dst ON t_dst.team_season_id = sp.team_season_id
        JOIN managers m_dst ON m_dst.manager_id = t_dst.manager_id
        JOIN teams t_src ON t_src.team_season_id = sp.counterparty_team_season_id
        JOIN managers m_src ON m_src.manager_id = t_src.manager_id
        WHERE st.season = 2026 AND st.event_type = 'trade'
          AND sp.direction = 'incoming'
        ORDER BY st.timestamp
    """).fetchall()
    for mv in moves:
        src_d, dst_d = by_manager.get(mv["src"]), by_manager.get(mv["dst"])
        if not src_d or not dst_d:
            continue
        p = next((x for x in src_d["players"]
                  if x["player_id"] == mv["player_id"]), None)
        if p is None:
            continue
        src_d["players"].remove(p)
        anchor = ((p.get("history") or {}).get(2025) or {}).get("drc") or p["drc"]
        frozen = max(1, min(16, int(anchor)))
        p["drc"] = frozen
        p["drc_dollars"] = dollar.get(frozen, 10)
        p["via_trade_2026"] = True
        dst_d["players"].append(p)

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


def _event_date_display(iso_date, is_trade, display=None):
    """Date-slot label. Off-season trades (month < Sept) read 'OFF-SEASON
    TRADE' — their exact date is fuzzy (synthetic / commish-pushed). Hardcoded
    uppercase because the date slots have no CSS text-transform (only the method
    label + section headers do), and we want it to match that caps styling.
    `display` overrides the shown text otherwise (e.g. a pre-formatted date)."""
    shown = display if display is not None else iso_date
    try:
        if is_trade and int(str(iso_date)[5:7]) < 9:
            return "OFF-SEASON TRADE"
    except (ValueError, IndexError, TypeError):
        pass
    return shown


def _trade_meta(p):
    meta = html.escape(p.get("position") or "")
    nfl = p.get("nfl_team")
    if not p.get("is_pick") and nfl and nfl not in ("—", ""):
        meta = f'{meta} &middot; {html.escape(nfl)}'
    return meta


def _trade_player_line(p, acquired):
    sign = "+" if acquired else "&minus;"
    cls = "since-pos" if acquired else "since-neg"
    if p.get("since") is None:
        since_html = '<span class="pl-since-na">&mdash;</span>'
    else:
        since_html = f'<span class="trade-pl-since {cls}">{sign}{p["since"]:.1f}</span>'
    return (
        f'<div class="trade-pl">'
        f'<div class="trade-pl-name"><span class="pl-name">{html.escape(p["name"])}</span> '
        f'<span class="pl-meta">{_trade_meta(p)}</span></div>'
        f'{since_html}'
        f'</div>'
    )


def _trade_pick_chips(players, acquired):
    sign = "+" if acquired else "&minus;"
    cls = "pick-got" if acquired else "pick-gave"
    chips = [
        f'<span class="pick-chip {cls}">{sign} '
        f'{html.escape(p["name"].replace(" draft pick", "").replace("Round ", "R"))}</span>'
        for p in players if p.get("is_pick")
    ]
    return f'<div class="trade-pick-chips">{"".join(chips)}</div>' if chips else ""


def _render_trade_history(trade):
    def yrs(p, y):
        if p.get("is_pick"):
            return "&mdash;"
        v = p["points"][y]["full"]
        return _fmt_pts(v) if v is not None else "&mdash;"

    def hrow(p, acquired):
        tag = "Acquired" if acquired else "Traded away"
        tagcls = "htag-got" if acquired else "htag-gave"
        if p.get("since") is None:
            since_html = "&mdash;"
        else:
            sign = "+" if acquired else "&minus;"
            scls = "since-pos" if acquired else "since-neg"
            since_html = f'<span class="{scls}">{sign}{p["since"]:.1f}</span>'
        return (
            f'<tr><td class="h-player"><span class="htag {tagcls}">{tag}</span> '
            f'<span class="pl-name">{html.escape(p["name"])}</span> '
            f'<span class="pl-meta">{_trade_meta(p)}</span></td>'
            f'<td class="num">{yrs(p, 2023)}</td><td class="num">{yrs(p, 2024)}</td>'
            f'<td class="num">{yrs(p, 2025)}</td>'
            f'<td class="num h-since">{since_html}</td></tr>'
        )

    rows = "".join(hrow(p, True) for p in trade["acquired"])
    rows += "".join(hrow(p, False) for p in trade["given_up"])
    return f"""
      <details class="trade-history">
        <summary>Full three-year history</summary>
        <table class="trade-hist-table">
          <thead><tr><th>Player</th><th class="num">2023</th><th class="num">2024</th><th class="num">2025</th><th class="num">Since</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </details>"""


def render_trade_event(trade):
    v = trade["verdict"]
    got_players = [p for p in trade["acquired"] if not p.get("is_pick")]
    gave_players = [p for p in trade["given_up"] if not p.get("is_pick")]
    got_lines = "".join(_trade_player_line(p, True) for p in got_players) or '<div class="trade-pl-empty">&mdash;</div>'
    gave_lines = "".join(_trade_player_line(p, False) for p in gave_players) or '<div class="trade-pl-empty">&mdash;</div>'
    date_lbl = _event_date_display(trade["date"], True, trade.get("date_display"))
    return f"""
    <div class="trade-card">
      <div class="trade-card-head">
        <div class="trade-card-when"><span class="trade-date">{html.escape(date_lbl)}</span> <span class="trade-vs">vs {html.escape(trade['counterparty_name'])}</span></div>
        <span class="verdict-chip verdict-{v['kind']}">{html.escape(v['label'])}</span>
      </div>
      <div class="trade-cols">
        <div class="trade-col">
          <div class="trade-col-label label-got">Acquired</div>
          {got_lines}
          {_trade_pick_chips(trade['acquired'], True)}
        </div>
        <div class="trade-col trade-col-gave">
          <div class="trade-col-label label-gave">Traded away</div>
          {gave_lines}
          {_trade_pick_chips(trade['given_up'], False)}
        </div>
      </div>
      {_render_trade_history(trade)}
    </div>"""


def render_trades_tab(trade_history, slug):
    if not trade_history:
        return '<p class="empty-note">No trades on record for this manager.</p>'
    scored = [t for t in trade_history if t["verdict"]["kind"] in ("won", "lost", "neutral")]
    net_total = round(sum(t["net"] for t in scored), 1)
    wins = sum(1 for t in trade_history if t["verdict"]["kind"] == "won")
    losses = sum(1 for t in trade_history if t["verdict"]["kind"] == "lost")
    evens = sum(1 for t in trade_history if t["verdict"]["kind"] == "neutral")
    net_cls = "since-pos" if net_total > 0.5 else "since-neg" if net_total < -0.5 else ""
    net_str = f'{"+" if net_total >= 0 else "&minus;"}{abs(net_total):.1f}'
    strip = f"""
    <div class="trade-summary">
      <div class="ts-cell"><div class="ts-label">Trades made</div><div class="ts-val">{len(trade_history)}</div></div>
      <div class="ts-cell"><div class="ts-label">Net points since trades</div><div class="ts-val {net_cls}">{net_str}</div></div>
      <div class="ts-cell"><div class="ts-label">Record (W&ndash;L&ndash;Even)</div><div class="ts-val">{wins}&ndash;{losses}&ndash;{evens}</div></div>
    </div>
    <p class="trade-note">&ldquo;Who won&rdquo; is judged on <b>points each player scored after the trade date</b> &mdash; not whole-season totals. Open a trade for the full history.</p>"""
    return strip + "".join(render_trade_event(t) for t in trade_history)


def _value_tag_label(tag):
    return {
        "steal":        "Steal",
        "fair":         "Fair",
        "reach":        "Reach",
        "major-reach":  "Major reach",
    }.get(tag, "")


def _traj_tier(d):
    if d <= 2:
        return 1
    if d <= 5:
        return 2
    if d <= 9:
        return 3
    return 4


def _format_trajectory(trajectory):
    """Render the DRC trajectory as a chip row: a solid draft-round chip
    then tier-colored DRC chips, each with a tiny year label (Design handoff)."""
    if not trajectory:
        return '<span class="traj-none">&mdash;</span>'
    chips = []
    for i, (year, label) in enumerate(trajectory):
        yr = f"&rsquo;{str(year)[-2:]}"
        if i == 0:
            digits0 = "".join(ch for ch in str(label) if ch.isdigit())
            display = "R" + digits0 if digits0 else str(label)
            chip_cls = "traj-chip traj-draft"
        else:
            digits = "".join(ch for ch in str(label) if ch.isdigit())
            d = int(digits) if digits else 10
            display = str(label)
            chip_cls = f"traj-chip traj-tier{_traj_tier(d)}"
        chips.append(
            f'<span class="traj-col">'
            f'<span class="{chip_cls}">{html.escape(display)}</span>'
            f'<span class="traj-yr">{yr}</span>'
            f'</span>'
        )
    return f'<span class="traj-chips">{"".join(chips)}</span>'


def render_draft_pick_row(pick, year, slug):
    """One flat pick row (Rd | Pick | Player[chevron+name+pos+type] | DRC
    trajectory | ADP | Value), plus a tap-to-expand transaction-log row when
    the pick has history (Design handoff)."""
    pos = pick.get("position") or ""
    dround = pick["draft_round"]
    is_keeper = pick.get("is_keeper", False)
    is_traded_for = pick.get("acquired_via_trade", False)

    if is_traded_for:
        type_tag = '<span class="draft-tag tag-traded">Traded for</span>'
    elif is_keeper:
        type_tag = '<span class="draft-tag tag-kept">Kept</span>'
    else:
        type_tag = ""

    trajectory_cell = _format_trajectory(pick.get("trajectory") or [])
    adp = pick.get("adp")
    adp_display = f"{adp:.1f}" if adp is not None else "—"

    tag = pick.get("value_tag") or ""
    label = _value_tag_label(tag)
    value_pill = f'<span class="pill value-{tag}">{label}</span>' if tag else ""

    pos_html = f'<span class="draft-pos">{html.escape(pos)}</span>' if pos else ""
    trade_class = " traded-for" if is_traded_for else ""
    txn_log = pick.get("txn_log") or []
    row_key = f"{slug}-{year}-{pick['overall_pick']}"

    if txn_log:
        chevron = '<span class="draft-log-toggle" aria-hidden="true">&rsaquo;</span>'
        row_class = f"round-{dround}{trade_class} has-log"
        row_data = f' data-log-row="dlog-{row_key}"'
        log_items = "".join(
            f'<div class="dlog-event">'
            f'<span class="event-date">{html.escape(_event_date_display(e["date"], e.get("kind") == "trade"))}</span>'
            f'<span class="event-desc">{html.escape(e["desc"])}</span>'
            f'</div>'
            for e in txn_log
        )
        detail_row = (
            f'<tr class="draft-log-detail" id="dlog-{row_key}" hidden>'
            f'<td colspan="6"><div class="dlog-wrap">'
            f'<div class="dlog-header">{html.escape(pick["player_name"])} '
            '&middot; transaction log</div>'
            f'{log_items}'
            '</div></td></tr>'
        )
    else:
        chevron = '<span class="draft-log-spacer"></span>'
        row_class = f"round-{dround}{trade_class}"
        row_data = ""
        detail_row = ""

    return f"""
        <tr class="{row_class}"{row_data}>
          <td class="round-cell">R{dround}</td>
          <td class="pick-label num">{pick['overall_pick']}</td>
          <td class="player-name">
            <div class="draft-player">
              {chevron}
              <span class="player-name-link">{html.escape(pick['player_name'])}</span>
              {pos_html}
              {type_tag}
            </div>
          </td>
          <td class="trajectory-cell">{trajectory_cell}</td>
          <td class="num">{adp_display}</td>
          <td>{value_pill}</td>
        </tr>{detail_row}"""


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
          <td class="round-cell">R{round_num}</td>
          <td colspan="5" class="meta">No pick — traded away or skipped</td>
        </tr>""")
                continue
            for p in group_list:
                rows_parts.append(render_draft_pick_row(p, year, slug))
        rows = "".join(rows_parts)

        body = f"""
            <table class="draft-table">
              <thead>
                <tr>
                  <th>Rd</th>
                  <th class="num">Pick</th>
                  <th>Player</th>
                  <th>DRC trajectory</th>
                  <th class="num">ADP</th>
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
      <div class="eyebrow">{TARGET_SEASON} keeper window</div>
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

/* --- Navigation chrome (Design handoff) --- */
.brand-home {
  display: block; width: 100%; text-align: left; border: none; background: none;
  color: inherit; cursor: pointer; padding: 8px 10px; border-radius: 8px; font: inherit;
  margin-bottom: 6px;
}
.brand-home:hover { background: rgba(255, 255, 255, 0.06); }
.brand-home-eyebrow {
  display: flex; align-items: center; gap: 6px; font-size: 10px; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--blue-200); font-weight: 600;
}
.brand-home-sub { display: block; font-size: 11px; color: var(--blue-200); margin-top: 3px; }
.crumb-bar {
  position: sticky; top: 0; z-index: 30; background: #fff;
  border-bottom: 1px solid var(--gray-200); padding: 10px 40px;
  display: flex; align-items: center; gap: 12px; font-size: 12.5px;
}
.crumb-back {
  border: 1px solid #d8e0f5; background: #f4f7ff; color: var(--blue-600); border-radius: 8px;
  padding: 6px 12px; font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
}
.crumb-back:hover { background: #e9f0ff; }
.crumb-sep { color: #c4c4c8; }
.crumb-current {
  color: var(--gray-700); font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.crumb-bar.at-home .crumb-sep,
.crumb-bar.at-home .crumb-current { display: none; }
.back-to-top {
  position: fixed; right: 22px; bottom: 22px; z-index: 40; width: 46px; height: 46px;
  border-radius: 50%; border: none; background: var(--blue-800); color: #fff; cursor: pointer;
  box-shadow: 0 6px 20px rgba(2, 36, 121, 0.34); font-size: 19px; line-height: 1;
  display: none; align-items: center; justify-content: center;
}
.back-to-top.visible { display: flex; }

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

.pill.value-major-steal { background: #cdedd9; color: #0e5730; }
.pill.value-steal       { background: #e6f6ee; color: #1c7a4a; }
.pill.value-fair        { background: #f0f0f2; color: #606C71; }
.pill.value-reach       { background: #fdecea; color: #b42318; }
.pill.value-major-reach { background: #fbd9d3; color: #8f1c11; }
.pill.value-overpriced  { background: #fff0e6; color: #b04a00; }

/* --- DRC trajectory chips (Design handoff) --- */
.traj-chips { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.traj-col { display: inline-flex; flex-direction: column; align-items: center; gap: 1px; }
.traj-chip {
  min-width: 20px; text-align: center; border-radius: 4px; padding: 1px 6px;
  font-weight: 700; font-size: 11.5px; font-variant-numeric: tabular-nums;
}
.traj-yr { font-size: 8.5px; color: #b8b8bc; font-weight: 600; }
.traj-draft { background: #022479; color: #fff; }
.traj-tier1 { background: #e7ecfa; color: #022479; }
.traj-tier2 { background: #eef4e2; color: #5b6b16; }
.traj-tier3 { background: #f0f0f2; color: #606C71; }
.traj-tier4 { background: #fbf3e0; color: #8a6a12; }
.traj-none { color: #c8c8cc; }

/* --- Draft player cell + tap-to-expand log (Design handoff) --- */
.draft-player { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.draft-pos { color: #a0a0a6; font-size: 11px; }
.draft-tag {
  font-size: 9.5px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase;
  padding: 1px 6px; border-radius: 4px; white-space: nowrap;
}
.draft-tag.tag-kept { background: #e7ecfa; color: #022479; }
.draft-tag.tag-traded { background: #fbf3e0; color: #8a6a12; }
.draft-log-toggle {
  color: #b0b0b4; font-size: 15px; font-weight: 700; line-height: 1;
  transition: transform 0.15s; display: inline-flex; align-items: center;
  justify-content: center; width: 13px;
}
.draft-log-toggle.open { transform: rotate(90deg); color: var(--blue-600); }
.draft-log-spacer { display: inline-block; width: 13px; }
.draft-table tr.has-log { cursor: pointer; }
.draft-table tr.has-log:hover td { background: #fafbfe; }
.draft-log-detail > td { padding: 0; background: #fafbfe; }
.dlog-wrap { padding: 10px 16px 12px 40px; }
.dlog-header {
  font-size: 10px; letter-spacing: 0.07em; text-transform: uppercase; color: #8e8e93;
  font-weight: 700; margin-bottom: 6px;
}
.dlog-event { display: flex; gap: 12px; padding: 2px 0; font-size: 12px; }
.dlog-event .event-date {
  color: #909096; white-space: nowrap; min-width: 118px; font-variant-numeric: tabular-nums;
}
.dlog-event .event-desc { color: #2a2a2e; }

/* --- Trades tab: summary strip + verdict cards (Design handoff) --- */
.trade-summary {
  display: flex; flex-wrap: wrap; align-items: stretch; margin: 8px 0;
  border: 1px solid var(--gray-200); border-radius: 10px; overflow: hidden; background: #fcfcfd;
}
.ts-cell { padding: 12px 18px; flex: 1; min-width: 140px; }
.ts-cell + .ts-cell { border-left: 1px solid var(--gray-200); }
.ts-label { font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: #8e8e93; font-weight: 600; }
.ts-val { font-size: 22px; font-weight: 700; color: var(--blue-800); font-variant-numeric: tabular-nums; margin-top: 2px; }
.trade-note { font-size: 11.5px; color: #8e8e93; margin: 0 0 18px; }
.trade-note b { color: #2a2a2e; }
.trade-card { border: 1px solid var(--gray-200); border-radius: 11px; overflow: hidden; margin-bottom: 12px; background: #fff; }
.trade-card-head {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 11px 15px; border-bottom: 1px solid #f2f2f4;
}
.trade-card-when { min-width: 0; }
.trade-card .trade-date { font-weight: 700; color: var(--blue-800); font-size: 14px; }
.trade-card .trade-vs { color: #8e8e93; font-size: 13px; }
.verdict-chip {
  display: inline-block; white-space: nowrap; font-size: 12px; font-weight: 700;
  letter-spacing: 0.02em; padding: 4px 11px; border-radius: 20px;
}
.verdict-won { background: #e6f6ee; color: #1c7a4a; }
.verdict-lost { background: #fdecea; color: #b42318; }
.verdict-neutral, .verdict-picks { background: #f0f0f2; color: #606C71; }
.verdict-pending { background: #eef2fb; color: #4a5578; }
.trade-cols { display: grid; grid-template-columns: 1fr 1fr; }
.trade-col { padding: 12px 15px; }
.trade-col-gave { border-left: 1px solid #f2f2f4; }
.trade-col-label { font-size: 10px; letter-spacing: 0.07em; text-transform: uppercase; font-weight: 700; margin-bottom: 7px; }
.label-got { color: #1c7a4a; }
.label-gave { color: #b42318; }
.trade-pl { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; padding: 3px 0; }
.trade-pl-name { min-width: 0; }
.pl-name { font-weight: 600; color: #2a2a2e; }
.pl-meta { color: #8e8e93; font-size: 11.5px; }
.trade-pl-since { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pl-since-na { color: #b0b0b4; }
.since-pos { color: #1c7a4a; }
.since-neg { color: #b42318; }
.trade-pl-empty { color: #c0c0c4; font-size: 13px; padding: 3px 0; }
.trade-pick-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.pick-chip { display: inline-block; border-radius: 5px; padding: 2px 8px; font-size: 11.5px; font-weight: 600; }
.pick-got { background: #e7ecfa; color: var(--blue-800); }
.pick-gave { background: #f0f0f2; color: #606C71; }
.trade-history { border-top: 1px solid #f2f2f4; }
.trade-history > summary {
  padding: 8px 15px; font-size: 11.5px; color: #8e8e93; cursor: pointer; font-weight: 600; list-style: none;
}
.trade-history > summary::-webkit-details-marker { display: none; }
.trade-history[open] > summary { color: var(--blue-600); }
.trade-hist-table { width: calc(100% - 30px); border-collapse: collapse; font-size: 12.5px; font-variant-numeric: tabular-nums; margin: 2px 15px 14px; }
.trade-hist-table th {
  text-align: left; font-size: 9.5px; letter-spacing: 0.06em; text-transform: uppercase;
  color: #b0b0b4; font-weight: 600; padding: 5px 8px;
}
.trade-hist-table th.num, .trade-hist-table td.num { text-align: right; }
.trade-hist-table td { padding: 4px 8px; border-top: 1px solid #f5f5f5; color: #606C71; }
.trade-hist-table td.h-player { color: #2a2a2e; }
.trade-hist-table td.h-since { font-weight: 600; }
.htag {
  display: inline-block; white-space: nowrap; text-transform: uppercase; font-size: 9px;
  font-weight: 700; letter-spacing: 0.04em; padding: 1px 5px; border-radius: 3px;
}
.htag-got { background: #e6f6ee; color: #1c7a4a; }
.htag-gave { background: #fdecea; color: #b42318; }
@media (max-width: 560px) {
  .trade-cols { grid-template-columns: 1fr; }
  .trade-col-gave { border-left: none; border-top: 1px solid #f2f2f4; }
}

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
.ta-board { margin-top: 12px; border-top: 1px solid var(--gray-200); padding-top: 10px; }
.ta-board summary {
  cursor: pointer; font-size: 12px; font-weight: 600; color: var(--blue-800);
  letter-spacing: 0.02em;
}
.ta-board summary:hover { color: var(--blue-600); }
table.ta-slots {
  width: 100%; border-collapse: collapse; margin-top: 8px;
  font-size: 11.5px; font-variant-numeric: tabular-nums; table-layout: fixed;
}
table.ta-slots td { padding: 4px 6px; border-bottom: 1px solid var(--gray-100); vertical-align: top; }
table.ta-slots td.ta-rd { width: 34px; font-weight: 700; color: var(--gray-600); }
table.ta-slots tr.ta-gone td { background: #faf4f2; }
table.ta-slots tr.ta-gone td.ta-rd { color: var(--red-600, #982B09); text-decoration: line-through; }
.ta-slot-player { font-weight: 600; color: var(--gray-800); }
.ta-slot-note { color: var(--gray-500); font-size: 10.5px; }
.ta-slot-up { color: #AA5200; font-weight: 600; }
.ta-orig-chip {
  display: inline-block; background: #eef3ff; color: var(--blue-800);
  border-radius: 3px; font-size: 10px; font-weight: 600; padding: 0 5px; margin-left: 4px;
}
.ta-legend {
  font-size: 10.5px; color: var(--gray-500); margin: 8px 0 6px; line-height: 1.5;
}
.ta-leg-acq { color: var(--blue-800); background: #eef3ff; border-radius: 3px; padding: 0 4px; }
.ta-leg-gone { color: var(--red-600, #982B09); }
.ta-slotrow {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 3px 0; border-bottom: 1px solid var(--gray-100);
}
.ta-slotrow.ta-gone { background: #faf4f2; border-radius: 3px; }
.ta-slotrow.ta-gone .ta-rd { color: var(--red-600, #982B09); text-decoration: line-through; }
.ta-slotrow .ta-rd {
  flex: 0 0 30px; font-size: 11px; font-weight: 700; color: var(--gray-600);
  padding-top: 4px; font-variant-numeric: tabular-nums;
}
.ta-pills { display: flex; flex-wrap: wrap; gap: 4px; flex: 1 1 auto; }
.ta-pill {
  display: inline-flex; align-items: baseline; gap: 5px;
  border: 1px solid var(--gray-200); border-radius: 4px;
  background: #fff; padding: 3px 8px; font-size: 11.5px; font-weight: 600;
  color: var(--gray-800); line-height: 1.4;
}
.ta-pill em { font-style: normal; font-weight: 600; font-size: 10px; color: var(--blue-800); }
.ta-pill i { font-style: normal; font-size: 10px; color: var(--gray-500); }
.ta-pill.open { border-style: dashed; color: var(--gray-500); font-weight: 500; }
.ta-pill.acq { background: #eef3ff; border-color: #c9d8ff; }
.ta-pill .ta-gl { color: var(--gray-500); font-weight: 700; }
.ta-pill .ta-gl-up { color: #AA5200; }
.ta-goneto { font-size: 11px; color: var(--red-600, #982B09); padding-top: 4px; }
.ta-unkeep {
  margin-top: 8px; font-size: 11.5px; color: var(--red-600, #982B09);
  background: #fbe9e2; border-radius: 4px; padding: 7px 10px; line-height: 1.5;
}
.ta-warnings {
  margin: 16px 0 0; background: #fff8e8; border: 1px solid var(--gold-400);
  border-radius: 5px; padding: 12px 16px;
}
.ta-warnings h3 { font-size: 13px; margin: 0 0 8px; color: #674F00; }
.ta-warnings ul { margin: 0; padding-left: 18px; font-size: 12.5px; color: var(--gray-800); }
.ta-warnings li { margin-bottom: 5px; line-height: 1.5; }
.ta-warn-bad { color: var(--red-600, #982B09); font-weight: 600; }
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
/* ---- Trade summary strip: at-a-glance stats card per team.
   Facts-only (no verdict). Two-column grid on desktop, still
   two-column on mobile (cards get denser + smaller type). */
.ta-sum-strip {
  margin-top: 12px;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  padding: 12px 14px 14px;
}
.ta-sum-h {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 10px;
}
.ta-sum-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.ta-sum-card {
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  padding: 10px 12px;
  min-width: 0;
}
.ta-sum-mgr {
  font-weight: 700;
  font-size: 13.5px;
  color: var(--blue-800);
  margin-bottom: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ta-sum-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  padding: 4px 0;
  border-top: 1px solid var(--gray-100);
}
.ta-sum-row:first-of-type { border-top: none; }
.ta-sum-k {
  color: var(--gray-600);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-weight: 600;
  flex: 0 0 auto;
}
.ta-sum-v {
  color: var(--gray-800);
  font-variant-numeric: tabular-nums;
  text-align: right;
  min-width: 0;
}
/* Position-net-swing block. Compact facts-only line per team. */
.ta-pos-swing {
  margin-top: 10px;
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  padding: 10px 14px 12px;
}
.ta-pos-h {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 6px;
}
.ta-pos-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.ta-pos-mgr {
  display: block;
  font-weight: 600;
  color: var(--blue-800);
  font-size: 11.5px;
  margin-bottom: 3px;
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
  /* Trade summary strip: keep two-column layout even on mobile so both
     teams stay comparable; tighten padding, shrink type. */
  .ta-sum-strip { padding: 10px 10px 12px; }
  .ta-sum-grid { gap: 6px; }
  .ta-sum-card { padding: 8px 9px; }
  .ta-sum-mgr { font-size: 12px; margin-bottom: 6px; }
  .ta-sum-row { font-size: 11.5px; padding: 3px 0; gap: 4px; flex-wrap: wrap; }
  .ta-sum-k { font-size: 9.5px; }
  .ta-sum-v { font-size: 11.5px; text-align: right; }
  .ta-pos-swing { padding: 8px 10px 10px; }
  .ta-pos-grid { grid-template-columns: 1fr 1fr; gap: 8px; font-size: 11.5px; }
  .ta-pos-mgr { font-size: 10.5px; }
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

/* ---- Keeper board ---------------------------------------------------- */
.kb-top { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin:0 0 14px; }
.kb-top select { font: inherit; padding:7px 10px; border:1px solid #ccc; border-radius:8px; background:#fff; min-width:230px; }
.kb-cap { margin-left:auto; display:flex; gap:10px; align-items:center; }
.kb-cap-box { background:#022479; color:#fff; border-radius:10px; padding:8px 16px; text-align:left; }
.kb-cap-box .kb-cap-num { font-size:20px; font-weight:700; font-variant-numeric: tabular-nums; }
.kb-cap-box .kb-cap-lbl { font-size:11px; color:#77CEFF; letter-spacing:.04em; text-transform:uppercase; }
.kb-btn { font:inherit; font-weight:600; border:1px solid #ccc; background:#fff; border-radius:8px; padding:8px 14px; cursor:pointer; }
.kb-btn:hover { border-color:#0038FF; color:#0038FF; }
.kb-cols { display:grid; grid-template-columns: 340px 1fr; gap:18px; align-items:start; }
.kb-panel { background:#fff; border:1px solid #e3e3e6; border-radius:12px; overflow:hidden; }
.kb-panel-h { padding:10px 14px; background:#f7f8fa; border-bottom:1px solid #e9e9ec; font-weight:700; font-size:13px; }
.kb-panel-h .kb-sub { font-weight:400; color:#606C71; font-size:12px; }
.kb-roster { max-height: 640px; overflow-y:auto; }
.kb-card { display:flex; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #f0f0f2; cursor:pointer; user-select:none; }
.kb-card:last-child { border-bottom:none; }
.kb-card .kb-nm { font-weight:600; font-size:13.5px; flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kb-card .kb-meta { color:#606C71; font-size:11.5px; flex:none; font-variant-numeric: tabular-nums; }
.kb-card .kb-check { flex:none; width:18px; height:18px; border:2px solid #c5c9d2; border-radius:5px; display:inline-flex; align-items:center; justify-content:center; font-size:12px; color:#fff; }
.kb-card.kb-on .kb-check { background:#0038FF; border-color:#0038FF; }
.kb-card.kb-on { background:#f4f8ff; }
.kb-card.kb-picked { outline:2px solid #0038FF; outline-offset:-2px; background:#eaf1ff; }
.kb-card.kb-chasm-card { background:#fdecea; }
.kb-board .kb-row { display:flex; align-items:center; gap:10px; padding:7px 12px; border-bottom:1px solid #f0f0f2; min-height:38px; }
.kb-board .kb-rnum { flex:none; width:26px; font-weight:700; color:#606C71; font-size:12.5px; text-align:right; }
.kb-slot { flex:1 1 0; border:1.5px dashed #d6d9e0; border-radius:8px; min-height:30px; padding:3px 8px; display:flex; align-items:center; gap:8px; font-size:12.5px; background:#fbfbfc; }
.kb-slot.kb-acq { border-style:solid; border-color:#c9b45e; background:#fdfaf0; }
.kb-slot .kb-origin { color:#8a6a12; font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; flex:none; }
.kb-slot.kb-legal { border-color:#0038FF; background:#eef4ff; box-shadow:0 0 0 2px rgba(0,56,255,.12); cursor:pointer; }
.kb-slot.kb-illegal { opacity:.45; }
.kb-slot .kb-seated { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kb-slot .kb-flag { flex:none; font-size:10.5px; background:#fff6e0; color:#8a6a12; border-radius:10px; padding:1px 7px; }
.kb-slot .kb-x { flex:none; margin-left:auto; border:none; background:none; color:#98a0ad; cursor:pointer; font-size:13px; padding:0 2px; }
.kb-slot .kb-x:hover { color:#b42318; }
.kb-row.kb-gone { background:#fdf3f2; }
.kb-row.kb-gone .kb-goneto { color:#b42318; font-size:12px; }
.kb-chasm-strip { margin:10px 12px; border-top:1px dashed #f0cfc9; padding-top:10px; }
.kb-chasm-chip { display:inline-block; background:#fdecea; color:#b42318; font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px; margin:0 6px 6px 0; }
.kb-hint { color:#606C71; font-size:12px; margin:10px 0 0; }
.kb-print { display:none; }
@media (max-width: 900px) {
  .kb-cols { grid-template-columns: 1fr; }
  .kb-roster { max-height: 320px; }
}
/* Mobile keeper-row stacking: at narrow widths, restructure each seated
   row so the full player name lives on line 1 (no truncation), DRC/$ and
   any flag chips on line 2, and the ACQ chip (if present) on line 3.
   The close X is pinned to the top-right so it stays a clean tap target
   regardless of row height. Desktop layout is untouched. */
@media (max-width: 480px) {
  .kb-board .kb-row {
    align-items: flex-start;
    gap: 6px;
    padding: 7px 8px;
  }
  .kb-board .kb-rnum {
    padding-top: 4px;
  }
  .kb-slot {
    position: relative;
    flex-wrap: wrap;
    row-gap: 3px;
    column-gap: 6px;
    padding: 6px 40px 6px 8px;
    min-height: 34px;
  }
  .kb-slot .kb-seated {
    order: 1;
    flex: 1 1 100%;
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    line-height: 1.25;
    font-size: 13px;
  }
  .kb-slot .kb-meta {
    order: 2;
    flex: 0 0 auto;
    margin-left: 0;
    font-size: 11px;
  }
  .kb-slot .kb-flag {
    order: 2;
    font-size: 10px;
    padding: 1px 6px;
  }
  .kb-slot .kb-origin {
    order: 3;
    flex: 1 1 100%;
    font-size: 10px;
  }
  .kb-slot .kb-x {
    position: absolute;
    top: 3px;
    right: 4px;
    margin-left: 0;
    padding: 4px 8px;
    font-size: 16px;
    line-height: 1;
  }
  /* Feedback pill and back-to-top button both anchor to the bottom-right
     and collide on narrow viewports. Stack them: fb-trigger on the bottom,
     back-to-top above with a comfortable gap and 44x44 tap target. */
  .back-to-top {
    right: 14px;
    bottom: 72px;
    width: 44px;
    height: 44px;
  }
  .fb-trigger {
    bottom: 14px;
    right: 14px;
  }
}
@media print {
  body.kb-printing .sidebar, body.kb-printing .crumb-bar, body.kb-printing .menu-toggle,
  body.kb-printing .sidebar-tab, body.kb-printing .back-to-top, body.kb-printing .kb-app,
  body.kb-printing .section-header, body.kb-printing .kb-foot,
  body.kb-printing .fb-trigger, body.kb-printing .fb-modal { display:none !important; }
  body.kb-printing .kb-print th { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  /* Force the print layout to page width. The desktop layout uses a
     280px sidebar + 1fr content grid; hiding the sidebar with display:
     none leaves .content in a 280px auto-placed slot. Collapse the
     grid entirely for print so content flows at full page width. */
  body.kb-printing .layout {
    display: block !important;
    grid-template-columns: none !important;
  }
  body.kb-printing { margin: 0; }
  body.kb-printing .content {
    padding: 0 !important;
    max-width: 100% !important;
    width: 100% !important;
    margin: 0 !important;
  }
  body.kb-printing #keeper-board { max-width: 100%; width: 100%; padding: 0; margin: 0; }
  body.kb-printing .kb-print { display:block; width: 100%; }
  body.kb-printing #keeper-board { display:block !important; }
  body.kb-printing .kb-print table.kb-print-full { table-layout: fixed; }
}
.kb-print h2 { font-size:18px; margin:0 0 2px; }
.kb-print .kb-print-sub { color:#606C71; font-size:12px; margin:0 0 12px; }
.kb-print table { width:100%; border-collapse:collapse; font-size:12px; }
.kb-print th { text-align:left; padding:5px 8px; background:#022479; color:#fff; font-weight:700; font-size:10.5px; letter-spacing:0.04em; text-transform:uppercase; }
.kb-print td { padding:4px 8px; border-bottom:1px solid #ebebed; vertical-align:top; }
.kb-print .num { text-align:right; font-variant-numeric:tabular-nums; }
.kb-print .kb-print-flag { color:#8a6a12; font-size:10.5px; margin-right:6px; }
.kb-print .kb-print-chasm { color:#b42318; font-weight:700; }
/* Full-slot export: one row per pick, whether keeper, open, or traded. */
.kb-print .kb-print-full td.num:first-child { font-weight:700; color:#606C71; }
.kb-print .kb-print-name { font-weight:600; }
.kb-print .kb-print-notes { color:#606C71; font-size:10.5px; }
.kb-print .kb-print-note { color:#8e8e93; font-style:italic; font-size:11px; }
.kb-print tr.kb-print-open td { background:#f8faff; }
.kb-print tr.kb-print-open td.num:first-child { color:#0038FF; }
.kb-print tr.kb-print-gone td { background:#fdf3f2; }
.kb-print tr.kb-print-gone td.num:first-child { color:#b42318; text-decoration:line-through; }
.kb-print .kb-print-gone-note { color:#b42318; }
.kb-print .kb-print-chasm-h { font-size:13px; margin:16px 0 6px; color:#b42318; }
.kb-print .kb-print-legend { color:#606C71; font-size:11px; font-weight:400; }
.kb-print .kb-print-legend-item { white-space:nowrap; }
.kb-print .kb-print-legend-key { display:inline-block; padding:0 5px; font-weight:700; font-size:10px; border-radius:3px; margin-right:3px; background:#f0f0f2; color:#606C71; }
.kb-print .kb-print-legend-key.kb-print-legend-open { background:#e6efff; color:#0038FF; }
.kb-print .kb-print-legend-key.kb-print-legend-gone { background:#fdecea; color:#b42318; }
"""

JS = r"""
(function() {
  const links = document.querySelectorAll('.nav-link');
  const sections = document.querySelectorAll('.team-section');

  function updateCrumb(targetId) {
    const bar = document.querySelector('.crumb-bar');
    if (!bar) return;
    bar.classList.toggle('at-home', targetId === 'summary');
    const link = document.querySelector('.nav-link[data-target="' + targetId + '"]');
    let label = '';
    if (link) label = ((link.childNodes[0] && link.childNodes[0].textContent) || link.textContent || '').trim();
    const cur = bar.querySelector('.crumb-current');
    if (cur) cur.textContent = label;
  }

  function show(targetId) {
    sections.forEach(s => s.hidden = (s.id !== targetId));
    links.forEach(l => l.classList.toggle('active', l.dataset.target === targetId));
    updateCrumb(targetId);
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

  const brandHome = document.querySelector('.brand-home');
  if (brandHome) brandHome.addEventListener('click', (e) => { e.preventDefault(); show('summary'); });
  document.querySelectorAll('.crumb-back').forEach(b => b.addEventListener('click', (e) => { e.preventDefault(); show('summary'); }));
  const backTop = document.querySelector('.back-to-top');
  if (backTop) {
    window.addEventListener('scroll', () => { backTop.classList.toggle('visible', (window.scrollY || document.documentElement.scrollTop) > 260); }, {passive: true});
    backTop.addEventListener('click', () => { window.scrollTo({top: 0, behavior: 'smooth'}); });
  }

  document.querySelectorAll('.draft-table tr.has-log').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('a')) return;
      const id = row.dataset.logRow;
      const detail = id ? document.getElementById(id) : null;
      if (!detail) return;
      const chev = row.querySelector('.draft-log-toggle');
      if (detail.hasAttribute('hidden')) { detail.removeAttribute('hidden'); if (chev) chev.classList.add('open'); }
      else { detail.setAttribute('hidden', ''); if (chev) chev.classList.remove('open'); }
    });
  });

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

  /* ---- Keeper slot simulation (league slide rules) -------------------
     Each rostered player wants the round slot equal to their 2026 DRC.
     - Native round owned & free  -> take it.
     - Native owned but occupied  -> slide DOWN through CONSECUTIVE owned
       rounds; the first round you don't own is a wall (the chasm).
     - Native round not owned, or down-slide hit the wall -> may move UP
       into any free earlier owned pick.
     - Nowhere to land -> un-keepable under current picks.
     Same-DRC ties seat the higher 2025 scorer first (sim assumption). */
  function slotSim(slug, roster, picks) {
    // own = your original pick (slide rules apply to these).
    // Acquired picks are PROTECTED: never consumed automatically — the sim
    // touches them only when a player can't be seated on your own picks,
    // and labels that use as optional.
    const slots = picks.map(pk =>
      ({r: pk.r, o: pk.o, lp: pk.lp, own: pk.o === slug, taken: null}));
    const owned = {};
    slots.forEach(s => { (owned[s.r] = owned[s.r] || []).push(s); });
    const holdAt = r => (owned[r] || []).length > 0;
    const freeAt = (r, ownOnly) =>
      (owned[r] || []).find(s => !s.taken && (!ownOnly || s.own));
    function findSeat(d, ownOnly) {
      if (holdAt(d)) {
        let f = freeAt(d, ownOnly);
        if (f) return {f, how: 'native'};
        for (let r = d + 1; r <= 16; r++) {  // slide down: consecutive held rounds
          if (!holdAt(r)) break;             // a round with no pick at all = the wall
          f = freeAt(r, ownOnly);
          if (f) return {f, how: 'slid'};
        }
      }
      for (let r = d - 1; r >= 1; r--) {     // up-slot escape
        const f = freeAt(r, ownOnly);
        if (f) return {f, how: 'up'};
      }
      return null;
    }
    const order = roster.slice().sort((x, y) =>
      (x.eff - y.eff) || ((y.pts || 0) - (x.pts || 0)));
    const placed = [], unkeepable = [];
    order.forEach(p => {
      const d = clampDrc(p.eff);
      const seat = findSeat(d, true) || findSeat(d, false);
      if (seat) {
        seat.f.taken = p;
        placed.push({p, r: seat.f.r, how: seat.how, viaAcq: !seat.f.own});
      } else unkeepable.push(p);
    });
    return {slots, placed, unkeepable};
  }

  function effRoster(slug) {
    return (playersBy[slug] || []).map(p =>
      ({i: p.i, n: p.n, pos: p.p, eff: p.d6, pts: p.pts, c: p.c6}));
  }
  const tierOf = d => d <= 2 ? 1 : d <= 5 ? 2 : d <= 9 ? 3 : 4;
  const first = m => String(m || '').split(' ')[0];
  const dotColor = t => ({1:'#022479',2:'#5b6b16',3:'#a6a6ac',4:'#c79a2e'})[t] || '#a6a6ac';
  function drcChipStyle(t) {
    const m = {1:['#e7ecfa','#022479'],2:['#eef4e2','#5b6b16'],3:['#f0f0f2','#606C71'],4:['#fbf3e0','#8a6a12']};
    const c = m[t] || m[3];
    return 'display:inline-block;min-width:22px;text-align:center;border-radius:4px;padding:1px 6px;font-weight:700;font-size:11.5px;flex:none;background:' + c[0] + ';color:' + c[1];
  }

  function boardModel(slug, picks, sim, picksLost) {
    const lostBy = {}; (picksLost || []).forEach(l => (lostBy[l.r] = lostBy[l.r] || []).push(l));
    const rows = [];
    for (let r = 1; r <= 16; r++) {
      const here = sim.slots.filter(s => s.r === r);
      const slots = here.map(sl => {
        const seat = sl.taken ? sim.placed.find(x => x.p === sl.taken) : null;
        return {own: sl.own, acqFrom: sl.own ? null : ((teamBy[sl.o] || {}).mgr || sl.o),
          seat: seat ? {i: seat.p.i, name: seat.p.n, drc: seat.p.eff, how: seat.how, viaAcq: seat.viaAcq, pos: seat.p.pos} : null};
      });
      rows.push({r, owned: here.length > 0, slots, goneTo: (lostBy[r] || []).map(l => (teamBy[l.to] || {}).mgr || l.to)});
    }
    return rows;
  }

  function sideResult(T, O, sendPlayers, recvPlayers, picksSent, picksRecv) {
    const team = teamBy[T];
    const sendIds = new Set(sendPlayers.map(p => p.i));
    const inCost = recvPlayers.reduce((s, p) => s + costRow(p)[0].c, 0);
    const outCost = sendPlayers.reduce((s, p) => s + p.c6, 0);
    const capAfter = team.cap - outCost + inCost;
    const ptsIn = recvPlayers.reduce((s, p) => s + (p.pts || 0), 0);
    const ptsOut = sendPlayers.reduce((s, p) => s + (p.pts || 0), 0);
    // Per-year future commit from incoming players only (out-year exposure this trade creates).
    // YEARS = [Y0, Y0+1, Y0+2]; index 0 is "this year", 1 and 2 are the two out-years.
    const commitByYear = YEARS.map((y, i) =>
      ({y, c: recvPlayers.reduce((s, p) => s + costRow(p)[i].c, 0)}));
    const commit3yr = commitByYear.reduce((s, e) => s + e.c, 0);
    const postRosterArr = effRoster(T).filter(p => !sendIds.has(p.i))
      .concat(recvPlayers.map(p => ({i: p.i, n: p.n, pos: p.p, eff: clampDrc(anchor(p)), pts: p.pts, c: costRow(p)[0].c, incoming: true})));
    const postPicks = (D.picks[T] || []).slice();
    picksSent.filter(pk => pk.y === Y0).forEach(pk => {
      let idx = postPicks.findIndex(c => c.r === pk.r && c.o !== T); if (idx < 0) idx = postPicks.findIndex(c => c.r === pk.r);
      if (idx >= 0) postPicks.splice(idx, 1);
    });
    picksRecv.filter(pk => pk.y === Y0).forEach(pk => postPicks.push({r: pk.r, o: 'acquired'}));
    const postSim = slotSim(T, postRosterArr, postPicks);
    const seatBy = {}; postSim.placed.forEach(a => seatBy[a.p.i] = a);
    const after = postRosterArr.slice().sort((a, b) => (a.eff - b.eff) || ((b.pts || 0) - (a.pts || 0))).map(p => {
      const seat = seatBy[p.i];
      return {p, pos: p.pos, eff: p.eff, incoming: !!p.incoming, chasm: !seat};
    });
    return {
      team, sends: sendPlayers, receives: recvPlayers, picksSent, picksRecv,
      capBefore: team.cap, capAfter, delta: capAfter - team.cap,
      ptsSwing: ptsIn - ptsOut, commit3yr, commitByYear, after,
      boardPost: boardModel(T, postPicks, postSim, (D.picks_lost[T] || []).concat(picksSent.filter(pk => pk.y === Y0).map(pk => ({r: pk.r, to: O})))),
    };
  }

  function computeTrade(cfg) {
    const {L, R, sel, picksL, picksR} = cfg;
    if (!L || !R || L === R) return {valid: false};
    const selIds = new Set(Object.keys(sel).filter(k => sel[k]).map(Number));
    const get = id => D.players.find(p => p.i === id);
    const lSends = [...selIds].map(get).filter(p => p && p.m === L);
    const rSends = [...selIds].map(get).filter(p => p && p.m === R);
    return {valid: true, empty: !(lSends.length || rSends.length || picksL.length || picksR.length),
      L: sideResult(L, R, lSends, rSends, picksL, picksR),
      R: sideResult(R, L, rSends, lSends, picksR, picksL)};
  }

  function teamOptions(cur) {
    return '<option value=""' + (cur ? '' : ' selected') + '>Select team…</option>' +
      D.teams.map(t => '<option value="' + t.slug + '"' + (t.slug === cur ? ' selected' : '') + '>' +
        esc(t.team) + ' — ' + esc(t.mgr) + '</option>').join('');
  }

  function boardChipHTML(sl, recvIds) {
    if (!sl.seat) {
      const acq = !sl.own;
      const st2 = 'display:flex;align-items:center;padding:5px 10px;border:1px dashed ' + (acq ? '#bfe3cf' : '#e0e0e3') +
        ';border-radius:7px;font-size:11.5px;color:' + (acq ? '#1c7a4a' : '#b8b8bc') + ';background:' + (acq ? '#f2fbf5' : '#fff') + ';';
      return '<div style="' + st2 + '">' + (acq ? ('acquired pick' + (sl.acqFrom ? ' · from ' + esc(sl.acqFrom) : '')) : 'open') + '</div>';
    }
    const s = sl.seat, incoming = recvIds.has(s.i), tier = tierOf(s.drc);
    const mark = s.how === 'slid' ? '↓' : s.how === 'up' ? '↑' : '';
    const chip = 'display:flex;align-items:center;gap:7px;padding:6px 10px;border-radius:7px;cursor:pointer;font-size:12.5px;border:1px solid ' +
      (incoming ? '#bfe3cf' : '#e5e5e8') + ';background:' + (incoming ? '#e6f6ee' : '#fff') + ';';
    const tag = incoming ? '<span style="color:#1c7a4a;font-weight:700;font-size:9.5px;letter-spacing:.04em;flex:none;">IN</span>'
      : (s.viaAcq ? '<span style="color:#8e8e93;font-weight:600;font-size:9.5px;flex:none;">via acq</span>' : '');
    return '<div data-toggle="' + s.i + '" style="' + chip + '">' +
      '<span style="width:7px;height:7px;border-radius:50%;flex:none;background:' + dotColor(tier) + ';"></span>' +
      '<span style="font-weight:600;color:#2a2a2e;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(s.name) + '</span>' +
      '<span style="color:#a0a0a6;font-size:11px;flex:none;">' + esc(s.pos) + '</span>' +
      '<span style="color:#0038FF;font-weight:700;flex:none;">' + mark + '</span>' +
      '<span style="flex:1;"></span>' + tag +
      '<span style="' + drcChipStyle(tier) + '">' + s.drc + '</span>' +
      '<span style="font-weight:700;color:#022479;font-size:12px;flex:none;font-variant-numeric:tabular-nums;">' + money($$(s.drc)) + '</span>' +
      '</div>';
  }

  function boardHTML(sideVM, side, active) {
    const recvIds = new Set(sideVM.receives.map(p => p.i));
    const chasmN = sideVM.after.filter(a => a.chasm).length;
    const d = sideVM.delta;
    const capStr = active ? ('cap ' + money(sideVM.capBefore) + ' → ' + money(sideVM.capAfter)) : ('cap ' + money(sideVM.capBefore));
    const deltaStr = active ? (d > 0 ? '▲ +$' + Math.abs(d).toLocaleString() : d < 0 ? '▼ −$' + Math.abs(d).toLocaleString() : '±0') : '';
    const deltaStyle = d > 0 ? 'color:#b42318;font-weight:700;' : d < 0 ? 'color:#1c7a4a;font-weight:700;' : 'color:#606C71;';
    let rows = '';
    sideVM.boardPost.forEach(row => {
      let slots = row.slots.map(sl => boardChipHTML(sl, recvIds)).join('');
      row.goneTo.forEach(m => { slots += '<div style="display:flex;align-items:center;padding:5px 10px;border:1px dashed #e3c4be;border-radius:7px;font-size:11.5px;color:#b06a60;background:#fdf4f2;">— traded to ' + esc(m) + '</div>'; });
      if (!row.owned && row.goneTo.length === 0 && row.slots.length === 0)
        slots += '<div style="display:flex;align-items:center;padding:5px 10px;border:1px dashed #e0e0e3;border-radius:7px;font-size:11.5px;color:#b8b8bc;">no pick</div>';
      rows += '<div style="display:flex;align-items:flex-start;gap:8px;padding:2px 0;">' +
        '<div style="width:30px;flex:none;text-align:center;font-size:11px;font-weight:700;color:#909096;font-variant-numeric:tabular-nums;padding-top:6px;">R' + row.r + '</div>' +
        '<div style="flex:1;display:flex;flex-direction:column;gap:4px;min-width:0;">' + slots + '</div></div>';
    });
    let chasm = '';
    const chItems = sideVM.after.filter(a => a.chasm);
    if (chItems.length) {
      chasm = '<div style="margin-top:10px;border-top:1px dashed #f0cfc9;padding-top:10px;">' +
        '<div style="font-size:10.5px;letter-spacing:0.06em;text-transform:uppercase;color:#b42318;font-weight:700;margin-bottom:6px;">Can&#39;t keep — no round to slot into</div>' +
        '<div style="display:flex;flex-direction:column;gap:4px;">' +
        chItems.map(a => '<div data-toggle="' + a.p.i + '" style="display:flex;align-items:center;gap:7px;padding:5px 10px;border:1px solid #f3c7c0;background:#fdecea;border-radius:7px;cursor:pointer;font-size:12.5px;">' +
          '<span style="color:#b42318;font-weight:800;flex:none;">⚠</span>' +
          '<span style="font-weight:600;color:#7a1a12;">' + esc(a.p.n) + '</span>' +
          '<span style="color:#b06a60;font-size:11px;">' + esc(a.pos) + '</span><span style="flex:1;"></span>' +
          (recvIds.has(a.p.i) ? '<span style="color:#b42318;font-weight:700;font-size:9.5px;letter-spacing:.04em;">JUST ACQUIRED</span>' : '') +
          '<span style="' + drcChipStyle(tierOf(a.eff)) + '">' + a.eff + '</span></div>').join('') +
        '</div></div>';
    }
    const picksArr = side === 'L' ? st.picksL : st.picksR;
    const dyv = side === 'L' ? st.dyL : st.dyR, drv = side === 'L' ? st.drL : st.drR;
    const yearOpts = [Y0, Y0 + 1].map(y => '<option value="' + y + '"' + (y === dyv ? ' selected' : '') + '>' + y + '</option>').join('');
    const roundOpts = Array.from({length: 16}, (_, i) => '<option value="' + (i + 1) + '"' + ((i + 1) === drv ? ' selected' : '') + '>R' + (i + 1) + '</option>').join('');
    const pickChips = picksArr.map((pk, i) => '<span style="display:inline-flex;align-items:center;gap:4px;background:#e6f6ee;color:#1c7a4a;border-radius:5px;padding:3px 6px;font-size:11.5px;font-weight:600;">' +
      pk.y + ' R' + pk.r + '<button type="button" data-act="rm' + side + '" data-idx="' + i + '" style="border:none;background:none;color:#1c7a4a;cursor:pointer;font-size:13px;line-height:1;padding:0;">×</button></span>').join('');
    const footer = '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:9px 14px;border-top:1px solid #ebebed;background:#fcfcfd;">' +
      '<span style="font-size:10.5px;letter-spacing:0.06em;text-transform:uppercase;color:#8e8e93;font-weight:600;">Add pick</span>' +
      '<select data-role="dy' + side + '" style="border:1px solid #d8d8dc;border-radius:6px;padding:4px 6px;font:inherit;font-size:12px;">' + yearOpts + '</select>' +
      '<select data-role="dr' + side + '" style="border:1px solid #d8d8dc;border-radius:6px;padding:4px 6px;font:inherit;font-size:12px;">' + roundOpts + '</select>' +
      '<button type="button" data-act="add' + side + '" style="border:1px solid #022479;background:#fff;color:#022479;border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;">Add</button>' + pickChips + '</div>';
    const chasmBadge = chasmN > 0 ? '<div style="margin-top:6px;"><span style="display:inline-block;background:#fdecea;color:#b42318;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;">' +
      chasmN + (chasmN === 1 ? ' keeper can&#39;t slot' : ' keepers can&#39;t slot') + '</span></div>' : '';
    return '<div style="border:1px solid #ebebed;border-radius:12px;overflow:hidden;background:#fff;">' +
      '<div style="padding:12px 15px;background:#fcfcfd;border-bottom:1px solid #ebebed;">' +
      '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">' +
      '<span style="font-weight:700;font-size:15px;color:#022479;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(sideVM.team.mgr) + '</span>' +
      '<span style="font-size:12px;color:#606C71;font-variant-numeric:tabular-nums;white-space:nowrap;">' + capStr + ' <span style="' + deltaStyle + '">' + deltaStr + '</span></span></div>' +
      chasmBadge + '</div>' +
      '<div style="padding:10px 12px;">' + rows + chasm + '</div>' + footer + '</div>';
  }

  function trayHTML(vm) {
    if (!vm.valid || vm.empty) return '';
    const L = vm.L, R = vm.R;
    const items = (sideVM) => {
      let a = sideVM.sends.map(p => '<button type="button" data-toggle="' + p.i + '" style="display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.25);color:#fff;border-radius:6px;padding:4px 8px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;">' + esc(p.n) + ' <span style="color:#77CEFF;font-size:13px;">×</span></button>');
      sideVM.picksSent.forEach(pk => a.push('<span style="display:inline-flex;align-items:center;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.25);color:#fff;border-radius:6px;padding:4px 8px;font-size:12px;font-weight:600;white-space:nowrap;">' + pk.y + ' R' + pk.r + '</span>'));
      return a.length ? a.join('') : '<span style="color:#9fbdff;font-size:12.5px;">nothing yet</span>';
    };
    const sw = n => (n >= 0 ? '+' : '−') + Math.abs(n).toFixed(1) + ' pts';
    const swSt = n => n >= 0 ? 'color:#a7f3c4' : 'color:#ffc4bb';
    return '<div style="background:#022479;border-radius:12px;padding:14px 16px;color:#fff;">' +
      '<div style="display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center;">' +
      '<div><div style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:#9fbdff;font-weight:600;margin-bottom:6px;">' + esc(first(L.team.mgr)) + ' sends</div><div style="display:flex;flex-wrap:wrap;gap:6px;">' + items(L) + '</div></div>' +
      '<div style="display:flex;align-items:center;justify-content:center;color:#77CEFF;font-size:20px;font-weight:700;">⇄</div>' +
      '<div><div style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:#9fbdff;font-weight:600;margin-bottom:6px;">' + esc(first(R.team.mgr)) + ' sends</div><div style="display:flex;flex-wrap:wrap;gap:6px;">' + items(R) + '</div></div></div>' +
      '<div style="margin-top:10px;padding-top:9px;border-top:1px solid rgba(255,255,255,0.15);font-size:12.5px;color:#cfe0ff;">2025 production swing: <b style="' + swSt(L.ptsSwing) + '">' + esc(first(L.team.mgr)) + ' ' + sw(L.ptsSwing) + '</b> &middot; <b style="' + swSt(R.ptsSwing) + '">' + esc(first(R.team.mgr)) + ' ' + sw(R.ptsSwing) + '</b></div></div>';
  }

  /* Compact per-team stat card — a mobile-friendly stats panel that
     surfaces numbers our engine already computes but scatters across the
     tray footer and board headers. Facts-only, no verdict.
     Four rows per side:
       - 2025 production swing (pts in − pts out)
       - Cap Y0 (this year): before → after, with delta
       - Future commit from INCOMING players only, broken out per year
         (Y0+1 and Y0+2) — surfaces the multi-year exposure this trade
         creates.
       - Slot risk (chasm count) */
  function summaryCardHTML(sideVM, active) {
    const pts = sideVM.ptsSwing;
    const ptsStr = active ? ((pts > 0 ? '▲ +' : pts < 0 ? '▼ −' : '±') + Math.abs(pts).toFixed(1)) : '—';
    const ptsStyle = pts > 0 ? 'color:#1c7a4a;font-weight:700;' : pts < 0 ? 'color:#b42318;font-weight:700;' : 'color:#606C71;';
    const d = sideVM.delta;
    const capNowStr = active ? (money(sideVM.capBefore) + ' → ' + money(sideVM.capAfter)) : money(sideVM.capBefore);
    const capDeltaStr = active ? (d > 0 ? ' (▲ +$' + Math.abs(d).toLocaleString() + ')' : d < 0 ? ' (▼ −$' + Math.abs(d).toLocaleString() + ')' : ' (±0)') : '';
    const capDeltaStyle = d > 0 ? 'color:#b42318;' : d < 0 ? 'color:#1c7a4a;' : 'color:#606C71;';
    // Multi-year commit on INCOMING players (index 1 = Y0+1, index 2 = Y0+2).
    const cbY = sideVM.commitByYear || [];
    let futureStr;
    if (!active || (!cbY[1] || !cbY[1].c) && (!cbY[2] || !cbY[2].c)) {
      futureStr = '—';
    } else {
      futureStr = (cbY[1].y) + ' ' + money(cbY[1].c) + ' &middot; ' + (cbY[2].y) + ' ' + money(cbY[2].c);
    }
    const chasmN = sideVM.after.filter(a => a.chasm).length;
    const chasmStr = chasmN > 0 ? (chasmN + (chasmN === 1 ? ' keeper' : ' keepers') + ' can&rsquo;t slot') : 'clean';
    const chasmStyle = chasmN > 0 ? 'color:#b42318;font-weight:700;' : 'color:#1c7a4a;';
    return '<div class="ta-sum-card">' +
      '<div class="ta-sum-mgr">' + esc(sideVM.team.mgr) + '</div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">2025 pts</span><span class="ta-sum-v" style="' + ptsStyle + '">' + ptsStr + '</span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Cap ' + Y0 + '</span><span class="ta-sum-v">' + capNowStr +
        '<span style="' + capDeltaStyle + 'font-weight:700;">' + capDeltaStr + '</span></span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Future commit</span><span class="ta-sum-v">' + futureStr + '</span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Slot risk</span><span class="ta-sum-v" style="' + chasmStyle + '">' + chasmStr + '</span></div>' +
      '</div>';
  }
  function summaryStripHTML(vm, active) {
    return '<div class="ta-sum-strip">' +
      '<div class="ta-sum-h">Trade summary</div>' +
      '<div class="ta-sum-grid">' +
      summaryCardHTML(vm.L, active) +
      summaryCardHTML(vm.R, active) +
      '</div></div>';
  }

  /* Position-count net swing. Uses `pos` already on every player; adds
     one facts-only line per side showing what shape the trade actually
     is (gains X at RB, loses Y at WR, etc.). Skipped when the trade is
     picks-only or empty. */
  function positionSwingHTML(vm, active) {
    if (!active) return '';
    const line = sideVM => {
      const net = {};
      sideVM.receives.forEach(p => { net[p.p] = (net[p.p] || 0) + 1; });
      sideVM.sends.forEach(p => { net[p.p] = (net[p.p] || 0) - 1; });
      const gains = Object.keys(net).filter(k => net[k] > 0)
        .map(k => k + ' +' + net[k]);
      const losses = Object.keys(net).filter(k => net[k] < 0)
        .map(k => k + ' ' + net[k]);
      const parts = [];
      if (gains.length) parts.push('<span style="color:#1c7a4a;font-weight:600;">' + gains.join('  ') + '</span>');
      if (losses.length) parts.push('<span style="color:#b42318;font-weight:600;">' + losses.join('  ') + '</span>');
      if (!parts.length) return '<span style="color:#8e8e93;">picks only</span>';
      return parts.join('  &middot;  ');
    };
    const noPlayers = !vm.L.receives.length && !vm.L.sends.length &&
                       !vm.R.receives.length && !vm.R.sends.length;
    if (noPlayers) return '';
    return '<div class="ta-pos-swing">' +
      '<div class="ta-pos-h">Position swing</div>' +
      '<div class="ta-pos-grid">' +
      '<div><span class="ta-pos-mgr">' + esc(first(vm.L.team.mgr)) + '</span>' + line(vm.L) + '</div>' +
      '<div><span class="ta-pos-mgr">' + esc(first(vm.R.team.mgr)) + '</span>' + line(vm.R) + '</div>' +
      '</div></div>';
  }

  function render() {
    let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;">' +
      '<div><label style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:#8e8e93;font-weight:600;display:block;margin-bottom:4px;">Team A</label>' +
      '<select data-role="teamL" style="width:100%;border:1px solid #d8d8dc;border-radius:8px;padding:8px 10px;font:inherit;font-size:14px;font-weight:600;color:#022479;">' + teamOptions(st.teamL) + '</select></div>' +
      '<div><label style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:#8e8e93;font-weight:600;display:block;margin-bottom:4px;">Team B</label>' +
      '<select data-role="teamR" style="width:100%;border:1px solid #d8d8dc;border-radius:8px;padding:8px 10px;font:inherit;font-size:14px;font-weight:600;color:#022479;">' + teamOptions(st.teamR) + '</select></div></div>';
    if (!st.teamL || !st.teamR) {
      app.innerHTML = html + '<div style="border:1px dashed #d8d8dc;border-radius:10px;padding:28px;text-align:center;color:#8e8e93;font-size:13px;">Pick a team on each side to compare their keeper boards and build a trade.</div>';
      return;
    }
    if (st.teamL === st.teamR) {
      app.innerHTML = html + '<div style="border:1px dashed #d8d8dc;border-radius:10px;padding:28px;text-align:center;color:#8e8e93;font-size:13px;">Pick two different teams.</div>';
      return;
    }
    const vm = computeTrade({L: st.teamL, R: st.teamR, sel: st.sel, picksL: st.picksL, picksR: st.picksR});
    const active = !vm.empty;
    html += trayHTML(vm);
    // Trade-summary strip + position swing (both facts-only, no verdict).
    // The summary strip carries the scan-both-teams-at-a-glance work; no
    // mobile tab toggle needed.
    html += summaryStripHTML(vm, active);
    html += positionSwingHTML(vm, active);
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;align-items:start;margin-top:14px;">' +
      boardHTML(vm.L, 'L', active) + boardHTML(vm.R, 'R', active) + '</div>';
    html += '<p style="font-size:11.5px;color:#8e8e93;margin:14px 2px 0;">Click a keeper on either board to move them across. <b style="color:#2a2a2e;">Dot</b> = keeper tier · <span style="color:#0038FF;font-weight:600;">↓/↑</span> = slid to a nearby round · <span style="color:#1c7a4a;font-weight:600;">green</span> = just acquired · a round you don&#39;t own is a wall, and anyone who can&#39;t reach a round drops to <span style="color:#b42318;font-weight:600;">can&#39;t keep</span>.</p>';
    app.innerHTML = html;
  }

  const st = {
    teamL: '', teamR: '',
    sel: {}, picksL: [], picksR: [],
    dyL: Y0, drL: 1, dyR: Y0, drR: 1,
  };
  const app = root.querySelector('.ta-app');

  app.addEventListener('click', (e) => {
    const tog = e.target.closest('[data-toggle]');
    if (tog) { const pid = +tog.dataset.toggle; if (st.sel[pid]) delete st.sel[pid]; else st.sel[pid] = true; render(); return; }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const a = act.dataset.act;
    if (a === 'addL') st.picksL.push({y: st.dyL, r: st.drL});
    else if (a === 'addR') st.picksR.push({y: st.dyR, r: st.drR});
    else if (a === 'rmL') st.picksL.splice(+act.dataset.idx, 1);
    else if (a === 'rmR') st.picksR.splice(+act.dataset.idx, 1);
    render();
  });

  app.addEventListener('change', (e) => {
    const role = e.target.dataset.role;
    if (!role) return;
    if (role === 'teamL' || role === 'teamR') {
      const old = role === 'teamL' ? st.teamL : st.teamR;
      Object.keys(st.sel).forEach(id => { const p = D.players.find(x => x.i === +id); if (p && p.m === old) delete st.sel[id]; });
      if (role === 'teamL') { st.teamL = e.target.value; st.picksL = []; }
      else { st.teamR = e.target.value; st.picksR = []; }
      render();
    } else if (role === 'dyL') st.dyL = +e.target.value;
    else if (role === 'drL') st.drL = +e.target.value;
    else if (role === 'dyR') st.dyR = +e.target.value;
    else if (role === 'drR') st.drR = +e.target.value;
  });

  render();
})();

/* ---- Keeper board (designation sandbox) ------------------------------ */
(function() {
  const D = window.TRADE_DATA;
  const root = document.getElementById('keeper-board');
  if (!D || !root) return;

  const DOLLARS = {1:200, 2:100, 3:80, 4:60, 5:50, 6:30, 7:30, 8:30, 9:30};
  const $$ = d => DOLLARS[d] || 10;
  const clampDrc = d => Math.max(1, Math.min(16, d));
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const money = n => '$' + n.toLocaleString();
  const teamBy = {}; D.teams.forEach(t => teamBy[t.slug] = t);
  const playersBy = {}; D.players.forEach(p => { (playersBy[p.m] = playersBy[p.m] || []).push(p); });

  const st = { team: '', keep: {}, manual: {}, pick: null };

  function mkSlots() {
    return (D.picks[st.team] || []).map((pk, i) =>
      ({ id: i, r: pk.r, o: pk.o, lp: pk.lp, own: pk.o === st.team, taken: null, manual: false }));
  }
  function roster() {
    return (playersBy[st.team] || []).slice().sort((a, b) => (a.d6 - b.d6) || ((b.pts || 0) - (a.pts || 0)));
  }
  function keepers() { return roster().filter(p => st.keep[p.i]); }

  /* Rounds this player may legally occupy, per slide rules:
     native DRC round; slide-down through CONSECUTIVE held rounds below it;
     any held round above it (moving up is always allowed). */
  function legalRoundsFor(d, slots) {
    const held = {}; slots.forEach(s => held[s.r] = 1);
    const ok = {};
    for (let r = d - 1; r >= 1; r--) if (held[r]) ok[r] = 1;
    if (held[d]) {
      ok[d] = 1;
      for (let r = d + 1; r <= 16; r++) { if (!held[r]) break; ok[r] = 1; }
    }
    return ok;
  }

  /* Sim honoring manual placements, then auto-seating the rest
     (own picks first, acquired only as fallback; DRC order, pts tiebreak). */
  function kbSim(excludePid) {
    const slots = mkSlots();
    const placed = {}, unkeepable = [];
    const ks = keepers().filter(p => p.i !== excludePid);
    const badManual = [];
    ks.forEach(p => {
      const sid = st.manual[p.i];
      if (sid == null) return;
      const sl = slots.find(s => s.id === sid);
      const ok = sl && !sl.taken && legalRoundsFor(clampDrc(p.d6), slots)[sl.r];
      if (ok) { sl.taken = p; sl.manual = true; placed[p.i] = { slot: sl, manual: true }; }
      else badManual.push(p.i);
    });
    badManual.forEach(pid => delete st.manual[pid]);
    const auto = ks.filter(p => !placed[p.i])
      .sort((a, b) => (a.d6 - b.d6) || ((b.pts || 0) - (a.pts || 0)));
    const held = {}; slots.forEach(s => { (held[s.r] = held[s.r] || []).push(s); });
    const freeAt = (r, ownOnly) => (held[r] || []).find(s => !s.taken && (!ownOnly || s.own));
    function findSeat(d, ownOnly) {
      if ((held[d] || []).length) {
        let f = freeAt(d, ownOnly);
        if (f) return f;
        for (let r = d + 1; r <= 16; r++) {
          if (!(held[r] || []).length) break;
          f = freeAt(r, ownOnly);
          if (f) return f;
        }
      }
      for (let r = d - 1; r >= 1; r--) { const f = freeAt(r, ownOnly); if (f) return f; }
      return null;
    }
    auto.forEach(p => {
      const d = clampDrc(p.d6);
      const seat = findSeat(d, true) || findSeat(d, false);
      if (seat) { seat.taken = p; placed[p.i] = { slot: seat, manual: false }; }
      else unkeepable.push(p);
    });
    return { slots, placed, unkeepable };
  }

  function capTotal() { return keepers().reduce((s, p) => s + (p.c6 || 0), 0); }

  function lostRows() {
    const lostBy = {};
    (D.picks_lost[st.team] || []).forEach(l => (lostBy[l.r] = lostBy[l.r] || []).push(l));
    return lostBy;
  }

  function render() {
    const app = root.querySelector('.kb-app');
    const opts = ['<option value="">Choose your team&hellip;</option>'].concat(
      D.teams.slice().sort((a, b) => a.team.toLowerCase() < b.team.toLowerCase() ? -1 : 1)
        .map(t => '<option value="' + t.slug + '"' + (t.slug === st.team ? ' selected' : '') + '>' +
                  esc(t.team) + ' (' + esc(t.mgr) + ')</option>'));
    let top = '<div class="kb-top"><select data-role="kbteam">' + opts.join('') + '</select>';
    if (st.team) {
      const ks = keepers();
      top += '<button class="kb-btn" data-kbact="reset" type="button">Reset board</button>' +
             '<button class="kb-btn" data-kbact="print" type="button">Print / save PDF</button>' +
             '<div class="kb-cap"><div class="kb-cap-box"><div class="kb-cap-num">' + money(capTotal()) +
             '</div><div class="kb-cap-lbl">' + ks.length + (ks.length === 1 ? ' keeper' : ' keepers') +
             ' committed</div></div></div>';
    }
    top += '</div>';
    if (!st.team) {
      app.innerHTML = top + '<p class="kb-hint">Pick your team to start building your keeper board. ' +
        'This is a sandbox: nothing is saved or submitted, fiddle freely.</p>';
      root.querySelector('.kb-print').innerHTML = '';
      return;
    }

    const sim = kbSim(st.pick != null ? st.pick : undefined);
    let legal = null;
    if (st.pick != null) {
      const p = D.players.find(x => x.i === st.pick);
      if (p) legal = legalRoundsFor(clampDrc(p.d6), sim.slots);
    }

    const cards = roster().map(p => {
      const on = !!st.keep[p.i];
      const isPick = st.pick === p.i;
      const chasm = sim.unkeepable.some(u => u.i === p.i) && on;
      const meta = 'DRC ' + p.d6 + ' &middot; ' + money(p.c6) +
                   (p.pts != null ? ' &middot; ' + p.pts + ' pts' : '');
      return '<div class="kb-card' + (on ? ' kb-on' : '') + (isPick ? ' kb-picked' : '') +
        (chasm ? ' kb-chasm-card' : '') + '" data-pid="' + p.i + '"' +
        (on ? ' draggable="true"' : '') + '>' +
        '<span class="kb-check" data-role="check">' + (on ? '&#10003;' : '') + '</span>' +
        '<span class="kb-nm">' + esc(p.n) + ' <span style="color:#606C71;font-weight:400;">' +
        esc(p.p || '') + '</span></span>' +
        '<span class="kb-meta">' + meta + '</span></div>';
    }).join('');

    const lostBy = lostRows();
    let rows = '';
    for (let r = 1; r <= 16; r++) {
      const here = sim.slots.filter(s => s.r === r);
      const gone = (lostBy[r] || []).map(l => esc((teamBy[l.to] || {}).mgr || l.to));
      let cells = here.map(sl => {
        const cls = ['kb-slot'];
        if (!sl.own) cls.push('kb-acq');
        if (legal) cls.push(legal[sl.r] && !sl.taken ? 'kb-legal' : 'kb-illegal');
        let inner = '';
        if (!sl.own) inner += '<span class="kb-origin">acq &middot; ' +
          esc(((teamBy[sl.o] || {}).mgr || '').split(' ')[0]) + '</span>';
        if (sl.taken) {
          const p = sl.taken;
          const flags = [];
          if (sl.r < clampDrc(p.d6)) flags.push('<span class="kb-flag" title="Seated earlier than DRC requires; round ' + clampDrc(p.d6) + ' freed">earlier than needed</span>');
          if (!sl.own) flags.push('<span class="kb-flag">via acquired pick</span>');
          if (sl.manual) flags.push('<span class="kb-flag" style="background:#e9f0ff;color:#022479;">placed by you</span>');
          inner += '<span class="kb-seated">' + esc(p.n) + '</span>' +
            '<span class="kb-meta">DRC ' + p.d6 + ' &middot; ' + money(p.c6) + '</span>' +
            flags.join('') +
            '<button class="kb-x" data-unseat="' + p.i + '" title="Remove from keepers" type="button">&#10005;</button>';
        } else if (!legal) {
          inner += '<span style="color:#b6bcc7;">open</span>';
        }
        return '<div class="' + cls.join(' ') + '" data-slot="' + sl.id + '">' + inner + '</div>';
      }).join('');
      if (!here.length) {
        cells = '<div class="kb-slot kb-illegal" style="border-style:none;background:none;">' +
          '<span class="kb-goneto">pick traded away' + (gone.length ? ' to ' + gone.join(', ') : '') + '</span></div>';
      }
      rows += '<div class="kb-row' + (!here.length ? ' kb-gone' : '') + '"><span class="kb-rnum">' + r + '</span>' + cells + '</div>';
    }
    let chasmStrip = '';
    if (sim.unkeepable.length) {
      chasmStrip = '<div class="kb-chasm-strip"><strong style="color:#b42318;font-size:12px;">Can&#39;t slot (chasm):</strong> ' +
        sim.unkeepable.map(p => '<span class="kb-chasm-chip">' + esc(p.n) + ' (DRC ' + p.d6 + ')</span>').join('') +
        '<div class="kb-hint">No owned round can seat them under the slide rules with this board. Free a round or trade for a pick.</div></div>';
    }

    // Preserve the roster list's scroll position across the innerHTML
    // rebuild — otherwise clicking a player at the bottom of the list
    // jumps the user back to the top on every render.
    const prevRoster = app.querySelector('.kb-roster');
    const prevScroll = prevRoster ? prevRoster.scrollTop : 0;

    app.innerHTML = top +
      '<div class="kb-cols">' +
      '<div class="kb-panel"><div class="kb-panel-h">Roster <span class="kb-sub">tap to keep &middot; tap a kept player to place them</span></div>' +
      '<div class="kb-roster">' + cards + '</div></div>' +
      '<div class="kb-panel"><div class="kb-panel-h">2026 draft board <span class="kb-sub">' +
      (st.pick != null ? 'lit slots are legal for the picked-up player, tap one to place' : 'your picks, with keepers seated by the slide rules') +
      '</span></div><div class="kb-board">' + rows + '</div>' + chasmStrip + '</div></div>' +
      '<p class="kb-hint">Costs come from DRC, not from the round a keeper sits in. Moving a player up spends a better pick than needed; the board flags it and shows the freed round, the call is yours. Acquired picks are protected: the auto-seat never uses them while one of your own picks works.</p>';

    if (prevScroll) {
      const newRoster = app.querySelector('.kb-roster');
      if (newRoster) {
        newRoster.scrollTop = prevScroll;
        requestAnimationFrame(() => { newRoster.scrollTop = prevScroll; });
      }
    }

    renderPrint(sim);
  }

  function renderPrint(sim) {
    /* Full 16-round board for printing / PDF export. One row per slot:
       - Keeper seated  -> player, DRC, $, notes (slid/acquired/manual)
       - Owned open     -> "(open — you draft here)"
       - Traded away    -> "(traded to <mgr>)"
       Chasm players (kept but no seat) get their own follow-up block.
       Replaces the old two-table layout that mirrored the same data
       twice; the right column ("What Yahoo shows") was intended to
       diverge from the tool's slide/up-slide seating but never did. */
    const t = teamBy[st.team] || {};
    const lostBy = {};
    (D.picks_lost[st.team] || []).forEach(l => (lostBy[l.r] = lostBy[l.r] || []).push(l));

    const rowsHtml = [];
    for (let r = 1; r <= 16; r++) {
      const here = sim.slots.filter(sl => sl.r === r);
      const gone = lostBy[r] || [];
      if (here.length === 0 && gone.length === 0) {
        // Round Pete owns nothing at (traded pre-history or never held)
        rowsHtml.push(
          '<tr class="kb-print-gone"><td class="num">' + r + '</td>' +
          '<td colspan="4" class="kb-print-note">no pick this round</td></tr>');
        continue;
      }
      here.forEach(sl => {
        if (sl.taken) {
          const p = sl.taken;
          const notes = [];
          if (sl.r < clampDrc(p.d6)) notes.push('<span class="kb-print-flag">slid up from R' + clampDrc(p.d6) + '</span>');
          if (!sl.own) notes.push('<span class="kb-print-flag">via acquired pick' +
            ((teamBy[sl.o] || {}).mgr ? ' (' + esc((teamBy[sl.o] || {}).mgr.split(' ')[0]) + ')' : '') + '</span>');
          if (sl.manual) notes.push('<span class="kb-print-flag" style="color:#022479;">placed by you</span>');
          rowsHtml.push(
            '<tr><td class="num">' + r + '</td>' +
            '<td class="kb-print-name">' + esc(p.n) + '</td>' +
            '<td class="num">' + p.d6 + '</td>' +
            '<td class="num">' + money(p.c6) + '</td>' +
            '<td class="kb-print-notes">' + notes.join(' ') + '</td></tr>');
        } else if (sl.own) {
          rowsHtml.push(
            '<tr class="kb-print-open"><td class="num">' + r + '</td>' +
            '<td colspan="4" class="kb-print-note">open &mdash; you draft here</td></tr>');
        } else {
          rowsHtml.push(
            '<tr class="kb-print-open"><td class="num">' + r + '</td>' +
            '<td colspan="4" class="kb-print-note">open &mdash; acquired pick, you draft here' +
            ((teamBy[sl.o] || {}).mgr ? ' (from ' + esc((teamBy[sl.o] || {}).mgr.split(' ')[0]) + ')' : '') + '</td></tr>');
        }
      });
      gone.forEach(l => {
        const to = (teamBy[l.to] || {}).mgr || l.to;
        rowsHtml.push(
          '<tr class="kb-print-gone"><td class="num">' + r + '</td>' +
          '<td colspan="4" class="kb-print-note kb-print-gone-note">traded to ' + esc(to) + '</td></tr>');
      });
    }

    let chasmBlock = '';
    if (sim.unkeepable.length) {
      chasmBlock = '<h3 class="kb-print-chasm-h">Can&rsquo;t slot (chasm) &mdash; keeper designation blocked</h3>' +
        '<table><thead><tr><th>Player</th><th>Pos</th><th>DRC</th><th>$</th><th>Reason</th></tr></thead><tbody>' +
        sim.unkeepable.map(p =>
          '<tr><td class="kb-print-chasm">' + esc(p.n) + '</td>' +
          '<td>' + esc(p.p || '') + '</td>' +
          '<td class="num">' + p.d6 + '</td>' +
          '<td class="num">' + money(p.c6) + '</td>' +
          '<td class="kb-print-chasm">no round available under the slide rules</td></tr>').join('') +
        '</tbody></table>';
    }

    root.querySelector('.kb-print').innerHTML =
      '<h2>' + esc(t.team || '') + ' &middot; 2026 keeper board</h2>' +
      '<p class="kb-print-sub">' + esc(t.mgr || '') + ' &middot; ' + keepers().length +
      ' keepers &middot; ' + money(capTotal()) + ' committed &middot; ' +
      '<span class="kb-print-legend"><span class="kb-print-legend-item"><span class="kb-print-legend-key">R#</span> keeper slot</span> &middot; ' +
      '<span class="kb-print-legend-item"><span class="kb-print-legend-key kb-print-legend-open">R#</span> open draft slot</span> &middot; ' +
      '<span class="kb-print-legend-item"><span class="kb-print-legend-key kb-print-legend-gone">R#</span> traded away</span></span></p>' +
      '<table class="kb-print-full"><thead><tr><th style="width:44px;">Rd</th><th>Slot</th><th style="width:56px;">DRC</th><th style="width:60px;">$</th><th style="width:38%;">Notes</th></tr></thead><tbody>' +
      rowsHtml.join('') + '</tbody></table>' +
      chasmBlock;
  }

  const app = root.querySelector('.kb-app');

  app.addEventListener('change', e => {
    if (e.target.dataset.role === 'kbteam') {
      st.team = e.target.value; st.keep = {}; st.manual = {}; st.pick = null; render();
    }
  });

  app.addEventListener('click', e => {
    const unseat = e.target.closest('[data-unseat]');
    if (unseat) { const pid = +unseat.dataset.unseat; delete st.keep[pid]; delete st.manual[pid]; if (st.pick === pid) st.pick = null; render(); return; }
    const act = e.target.closest('[data-kbact]');
    if (act) {
      if (act.dataset.kbact === 'reset') { st.keep = {}; st.manual = {}; st.pick = null; render(); }
      else if (act.dataset.kbact === 'print') {
        document.body.classList.add('kb-printing');
        const done = () => { document.body.classList.remove('kb-printing'); window.removeEventListener('afterprint', done); };
        window.addEventListener('afterprint', done);
        window.print();
      }
      return;
    }
    const slot = e.target.closest('.kb-slot.kb-legal');
    if (slot && st.pick != null) {
      st.manual[st.pick] = +slot.dataset.slot; st.pick = null; render(); return;
    }
    const card = e.target.closest('.kb-card');
    if (card) {
      const pid = +card.dataset.pid;
      const onCheck = !!e.target.closest('[data-role="check"]');
      if (!st.keep[pid]) { st.keep[pid] = true; st.pick = null; }
      else if (onCheck) { delete st.keep[pid]; delete st.manual[pid]; if (st.pick === pid) st.pick = null; }
      else st.pick = (st.pick === pid ? null : pid);
      render(); return;
    }
    if (st.pick != null) { st.pick = null; render(); }
  });

  app.addEventListener('dragstart', e => {
    const card = e.target.closest('.kb-card');
    if (!card || !st.keep[+card.dataset.pid]) { e.preventDefault(); return; }
    st.pick = +card.dataset.pid;
    e.dataTransfer.setData('text/plain', card.dataset.pid);
    setTimeout(render, 0);
  });
  app.addEventListener('dragover', e => {
    if (e.target.closest('.kb-slot.kb-legal')) e.preventDefault();
  });
  app.addEventListener('drop', e => {
    const slot = e.target.closest('.kb-slot.kb-legal');
    if (slot && st.pick != null) { e.preventDefault(); st.manual[st.pick] = +slot.dataset.slot; }
    st.pick = null; render();
  });
  app.addEventListener('dragend', () => { if (st.pick != null) { st.pick = null; render(); } });

  render();
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

        # Raw HTML block (single line): emit verbatim (tables, KPI cards)
        if line.lstrip().startswith("<"):
            out.append(line)
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
            <li><strong>IYFYSTD Resources</strong> &mdash; everything that's leaguewide. Summary, player search, commissioner's writeups, and the rules.</li>
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


def render_rules_history_section():
    """Rules History page: every rule ever floated, reconciled from the league
    group chat (proposal -> debate -> outcome) against DB/workbook canon.
    Plain string (not f-string) so the scoped CSS braces stay literal; all
    selectors are namespaced under #rules-history so nothing leaks to the
    rest of the dashboard."""
    return """
    <section class="team-section" id="rules-history" hidden>
      <header class="section-header">
        <h1 class="section-title">Rules history</h1>
        <p class="section-sub">Every rule that's ever been floated, traced through the league group chat &mdash; who proposed it, who fought it, and whether it actually passed. The DB and Pete's workbook are canon; this reconciles the debate record against them.</p>
      </header>
      <style>
        #rules-history .rh-canon{background:#f3f6fc;border-left:3px solid #022479;padding:11px 14px;border-radius:0 6px 6px 0;font-size:13.5px;color:#33415c;margin:4px 0 8px;}
        #rules-history .rh-h2{font-size:17px;font-weight:700;color:#022479;margin:30px 0 3px;padding-bottom:6px;border-bottom:2px solid #022479;}
        #rules-history .rh-note{color:#606c71;font-size:13px;margin:0 0 12px;}
        #rules-history .rh-chip{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;white-space:nowrap;}
        #rules-history .rh-live{background:#e9f3c9;color:#3f4a00;border:1px solid #cfe0a0;}
        #rules-history .rh-fail{background:#fbe3da;color:#8a2708;border:1px solid #f0c4b3;}
        #rules-history .rh-dock{background:#eaf1fb;color:#022479;border:1px solid #cfe0f4;}
        #rules-history .rh-conf{background:#fdf0cf;color:#6b5200;border:1px solid #ecd79a;}
        #rules-history table.rh-ov{width:100%;border-collapse:collapse;margin:6px 0 4px;font-size:13.5px;}
        #rules-history table.rh-ov th{background:#022479;color:#fff;font-weight:600;text-align:left;padding:8px 11px;}
        #rules-history table.rh-ov td{padding:8px 11px;border-bottom:1px solid #ebebed;vertical-align:top;}
        #rules-history table.rh-ov tr:nth-child(even) td{background:#f7f8fa;}
        #rules-history table.rh-ov td.rh-r{white-space:nowrap;color:#606c71;}
        #rules-history .rh-rule{padding:14px 0 4px;border-bottom:1px solid #ebebed;}
        #rules-history .rh-rule:last-child{border-bottom:none;}
        #rules-history .rh-rhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:3px;}
        #rules-history .rh-rname{font-size:15px;font-weight:700;color:#022479;}
        #rules-history .rh-sum{margin:2px 0 8px;font-size:13.5px;}
        #rules-history ul.rh-rec{margin:0 0 8px;padding:0;list-style:none;border-left:2px solid #e1e6ef;}
        #rules-history ul.rh-rec li{padding:3px 0 3px 12px;font-size:12.5px;color:#3a3f4a;}
        #rules-history .rh-date{display:inline-block;min-width:90px;color:#0038ff;font-weight:700;font-variant-numeric:tabular-nums;}
        #rules-history .rh-who{font-weight:600;color:#222;}
        #rules-history .rh-verdict{font-size:13px;background:#f7f8fa;border-radius:6px;padding:8px 12px;}
        #rules-history .rh-verdict strong{color:#022479;}
        #rules-history .rh-flag{color:#8a2708;font-weight:700;}
        #rules-history .rh-foot{margin-top:26px;font-size:12px;color:#606c71;border-top:1px solid #ebebed;padding-top:13px;}
      </style>

      <div class="rh-canon"><strong>What's canon:</strong> the league database and Pete's tracking workbook are the source of truth for what's in effect. The historical rules docs are <em>not</em> &mdash; they mix adopted rules with proposals that never passed. Quotes below are from the league thread, 2023&ndash;2026.</div>

      <h2 class="rh-h2">At a glance</h2>
      <p class="rh-note">Status of every rule in this ledger.</p>
      <table class="rh-ov">
        <thead><tr><th>Rule</th><th>Status</th><th class="rh-r">Last action</th></tr></thead>
        <tbody>
          <tr><td>Fill the open 12th seat ("The Lady Boys")</td><td><span class="rh-chip rh-dock">On docket</span></td><td class="rh-r">Open since '25</td></tr>
          <tr><td>1.01 free-keeper loophole &mdash; ban</td><td><span class="rh-chip rh-fail">Failed twice</span></td><td class="rh-r">Re-vote Fri</td></tr>
          <tr><td>Keepers to the back of the draft (Tom)</td><td><span class="rh-chip rh-fail">Voted down '24</span></td><td class="rh-r">Re-vote Fri</td></tr>
          <tr><td>Counter-offer rule (must include an original player)</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Review Fri</td></tr>
          <tr><td>Last-place +300 parlay</td><td><span class="rh-chip rh-conf">Unconfirmed</span></td><td class="rh-r">Confirm Fri</td></tr>
          <tr><td>Paul's pick-trading tweak</td><td><span class="rh-chip rh-dock">On docket</span></td><td class="rh-r">Clarify Fri</td></tr>
          <tr><td>Slide-down + pick chasm ("Cannobie Lake")</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Chasm new '26</td></tr>
          <tr><td>6th-seed points wildcard</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Passed '24</td></tr>
          <tr><td>10% top-points skim</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Passed '24</td></tr>
          <tr><td>No drop-and-immediately-re-add</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Passed '25</td></tr>
          <tr><td>Salary cap ($500&ndash;600)</td><td><span class="rh-chip rh-fail">Never adopted</span></td><td class="rh-r">Voted down '24</td></tr>
          <tr><td>Straight-up losers bracket draft order</td><td><span class="rh-chip rh-fail">Never adopted</span></td><td class="rh-r">Voted down</td></tr>
          <tr><td>"George $200 Rule"</td><td><span class="rh-chip rh-fail">Never a rule</span></td><td class="rh-r">&mdash;</td></tr>
          <tr><td>Half-cost first year after a trade</td><td><span class="rh-chip rh-fail">Never adopted</span></td><td class="rh-r">'23 proposal</td></tr>
          <tr><td>Player-for-pick &rarr; DRC one round lower</td><td><span class="rh-chip rh-fail">Never adopted</span></td><td class="rh-r">'23 proposal</td></tr>
        </tbody>
      </table>

      <h2 class="rh-h2">On the 2026 docket</h2>
      <p class="rh-note">The six items up for a decision Friday &mdash; with the history behind each.</p>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">1 &middot; Fill the open 12th seat</span> <span class="rh-chip rh-dock">On docket</span></div>
        <p class="rh-sum">"The Lady Boys" seat is open (Manager TBD). The 12th chair has been contested since day one.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-09-04</span><span class="rh-who">Pete:</span> "Who do we want as our 12th and final manager? A. Jon  B. Other Dan."</li>
          <li><span class="rh-date">2023-09-04</span><span class="rh-who">Paul:</span> "Jon didn't get six votes&hellip; other Dan gets right of first refusal."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> a personnel vote, not a rules mechanic. The seat has churned managers nearly every season; Friday's job is simply to fill it.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">2 &middot; 1.01 free-keeper loophole &mdash; ban</span> <span class="rh-chip rh-fail">Failed twice</span></div>
        <p class="rh-sum">Stop the manager holding 1.01 from dropping a keeper during selection and re-grabbing him at 1.01 for the cheap original cost instead of paying $200. Paul has flagged this since the founding year.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-11-14</span><span class="rh-who">Paul:</span> "The only way to keep the keeper-forfeiture rule and fix the loophole is to preclude keeping of first-round picks."</li>
          <li><span class="rh-date">2024-07-22</span><span class="rh-who">Proposal:</span> "Manager at 1.1 must either keep their player for $200 or can't redraft him at 1.1." &mdash; Brian, George &amp; Tom for; Scott, Alex &amp; Brad skeptical.</li>
          <li><span class="rh-date">2024-07-24</span><span class="rh-who">Pete:</span> "A stalemate means no rule is passed." <span class="rh-who">Tom:</span> "6 votes is not a majority."</li>
          <li><span class="rh-date">2025-06-27</span><span class="rh-who">Paul:</span> "Efforts to pass [a] rule to preclude [the] loophole fail[ed]."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> stalemated in 2024, failed again in 2025. Note the related general rule &mdash; no drop-and-immediately-re-add &mdash; <em>did</em> pass (below); this specific 1.01 ban did not. Re-vote Friday.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">3 &middot; Keepers to the back of the draft (Tom)</span> <span class="rh-chip rh-fail">Voted down '24</span></div>
        <p class="rh-sum">Tom's simplification: keepers fill the back rounds; a pick's round only sets the dollar cost. Pitched as a replacement for the slide/chasm machinery.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024-06-29</span><span class="rh-who">Tom:</span> "Your keepers fill in the back of your roster, not the front&hellip; draft round should only matter for the monetary cost."</li>
          <li><span class="rh-date">2024-07-13</span><span class="rh-who">Paul:</span> "Tom's proposal got voted down at Hodor's in person&hellip; My thing got voted down. Tom's thing got voted down."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> voted down at the 2024 summit. Back on the docket Friday &mdash; and if it passes, it retires the slide/chasm rules below.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">4 &middot; Counter-offer rule</span> <span class="rh-chip rh-live">In effect</span></div>
        <p class="rh-sum">When a trade is posted, the league has 48 hours to counter &mdash; and a valid counter must include one of the two original players. Already enforced; Friday is keep-or-adjust.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-09-05</span><span class="rh-who">Tom:</span> "Once a trade is accepted it is presented to the league and we have 48 hours to offer counteroffers."</li>
          <li><span class="rh-date">2023-09-12</span><span class="rh-who">Pete:</span> "There should be a [counter] that includes one of the two original players."</li>
          <li><span class="rh-date">2024-11-06</span><span class="rh-who">Pete:</span> "Denied." &mdash; rejecting an invalid counter under the rule.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> in effect and enforced. The Friday question is whether to keep the "must include an original player" requirement as-is or loosen it.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">5 &middot; Last-place +300 parlay</span> <span class="rh-chip rh-conf">Unconfirmed</span></div>
        <p class="rh-sum">The last-place finisher must place a $100 bet at +300 odds or better; if it hits, the winnings fund the summer summit.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-09-08</span><span class="rh-who">Alex:</span> "Should there be a punishment for coming in last place?"</li>
          <li><span class="rh-date">2024-25</span><span class="rh-who">Summit motion:</span> "lowest-points team obligated to place a $100 bet at +300 odds or greater&hellip; if [it] wins, the pot goes to summer summit."</li>
          <li><span class="rh-date">2025-11-18</span><span class="rh-who">Pete:</span> "Gents. Vote for this to be last-place punishment? Courtesy of Schlosberg."</li>
        </ul>
        <p class="rh-verdict"><strong class="rh-flag">Flagged:</strong> the chat shows it was <em>put to a vote</em> in Nov 2025, but no message records a clean pass &mdash; which is why it's on the agenda to confirm. Settle it Friday before treating it as a rule.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">6 &middot; Paul's pick-trading tweak</span> <span class="rh-chip rh-dock">On docket</span></div>
        <p class="rh-sum">Paul's view: you shouldn't be able to "loophole the slide" by trading away picks &mdash; the slide shouldn't skip a round just because you no longer hold that pick.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024-07-15</span><span class="rh-who">Paul:</span> "This is why you shouldn't be able to trade picks in this league."</li>
          <li><span class="rh-date">2024-11-08</span><span class="rh-who">Paul:</span> "I like the slide&hellip; but I think the slide needs to not apply when you're trading picks&hellip; the pick trade is trumping the slide. That's what shouldn't be [allowed]."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> long-debated, never formally resolved. Paul clarifies the exact proposal Friday, then it goes to a vote.</p>
      </div>

      <h2 class="rh-h2">Already on the books</h2>
      <p class="rh-note">Confirmed in effect &mdash; not up for a vote (with the receipts that prove they passed).</p>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Slide-down + pick chasm ("Cannobie Lake")</span> <span class="rh-chip rh-live">In effect</span></div>
        <p class="rh-sum">Same-cost keepers can't share a round, so extras slide <em>down</em> to the next round you still hold. You can't slide across a round whose pick you traded away &mdash; that gap is the "chasm."</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024-07-24</span><span class="rh-who">Paul:</span> "The agreement was that the draft-capital cost bumps down to the next available [round] that you have not kept."</li>
          <li><span class="rh-date">2024-07-24</span><span class="rh-who">Pete:</span> "You could have 4 first-round-cost players and they'd slide to 1, 2, 3 and 4."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> slide-<em>down</em> is settled and in effect. The chasm refinement (no sliding across a traded-away pick) is the piece new for the 2026&ndash;27 cycle.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">6th-seed points wildcard</span> <span class="rh-chip rh-live">In effect</span></div>
        <p class="rh-sum">Seeds 1&ndash;5 are straight standings; the 6th playoff spot goes to the most-points team among places 6&ndash;12, regardless of record.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024 (summit)</span><span class="rh-who">Minute:</span> "Vote on 6th-place wildcard passes&hellip; 1&ndash;5 is straight standings, 6th is most points of 6&ndash;12. This new rule is in."</li>
          <li><span class="rh-date">2025-09-30</span><span class="rh-who">Paul:</span> "I am very glad that wildcard rule passed."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> passed in 2024, in effect. (Worth a quick verbal reaffirmation Friday &mdash; a couple of managers needed it re-explained mid-2025.)</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">10% top-points skim</span> <span class="rh-chip rh-live">In effect</span></div>
        <p class="rh-sum">The regular-season top scorer takes 10% off the top of the pot, separate from placing money.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024-07-24</span><span class="rh-who">Pete:</span> "The 10% to the top scorer did pass."</li>
          <li><span class="rh-date">2024-25</span><span class="rh-who">Payout sheet:</span> "Top Scorer: $563.70 @ 10% of total pot." &mdash; already paid out.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> passed and already applied to a real payout. Settled.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">No drop-and-immediately-re-add</span> <span class="rh-chip rh-live">In effect</span></div>
        <p class="rh-sum">You can't drop a player and immediately re-add him via FAAB in the next waiver cycle to dodge his keeper cost &mdash; he returns at his original draft value.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2025-08-20</span><span class="rh-who">Brian:</span> "If you drop a guy and pick him back up that same waiver period, he should go back to your original draft value."</li>
          <li><span class="rh-date">2025-08</span><span class="rh-who">Proposal:</span> "Ban anyone from dropping a player and immediately picking them back up for FAAB in the subsequent waiver cycle."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> adopted for 2025. This is the general version of the fix; the narrower 1.01-specific ban (docket item 2) is the part still unsettled.</p>
      </div>

      <h2 class="rh-h2">Proposed but never adopted</h2>
      <p class="rh-note">The graveyard &mdash; floated, sometimes hotly, but never passed. Don't let these get cited as rules.</p>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">"George $200 Rule"</span> <span class="rh-chip rh-fail">Never a rule</span></div>
        <p class="rh-sum">The <em>name</em> for the 1.01 loophole maneuver itself (drop a DRC-1 keeper, re-pick him at 1.01 for $0 instead of paying $200). Discussed at summit; never sanctioned.</p>
        <p class="rh-verdict"><strong>Status:</strong> never adopted. Per canon, it is not in the algorithm or the books.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Salary cap ($500&ndash;600)</span> <span class="rh-chip rh-fail">Voted down</span></div>
        <ul class="rh-rec"><li><span class="rh-date">2024-06-29</span><span class="rh-who">Paul:</span> "I thought we voted down the cap."</li></ul>
        <p class="rh-verdict"><strong>Status:</strong> proposed alongside Tom's back-of-draft idea; voted down at the 2024 summit.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Straight-up losers-bracket draft order</span> <span class="rh-chip rh-fail">Voted down</span></div>
        <p class="rh-sum">Paul's pitch to set the first six picks straight from the losers' bracket finish, so the back half of the season has stakes.</p>
        <ul class="rh-rec"><li><span class="rh-date">2024 (summit)</span><span class="rh-who">Minute:</span> "Vote on doing straight-up losers bracket, first six picks in order &mdash; fails." <span class="rh-who">Paul:</span> "My losers-bracket proposal was vetoed last year."</li></ul>
        <p class="rh-verdict"><strong>Status:</strong> never adopted; draft order is set by lottery (Friday's live draw).</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Half-cost first year after a trade</span> <span class="rh-chip rh-fail">Never adopted</span></div>
        <p class="rh-verdict"><strong>Status:</strong> a 2023-doc proposal. Never passed &mdash; canon keeps the current rule: a traded player's DRC freezes for one season at full dollar value, then decrements normally.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Player-traded-for-a-pick &rarr; DRC one round lower</span> <span class="rh-chip rh-fail">Never adopted</span></div>
        <p class="rh-verdict"><strong>Status:</strong> a 2023-doc proposal. Never passed &mdash; a player acquired via trade simply inherits his existing DRC chain.</p>
      </div>

      <p class="rh-foot"><strong>Sources &amp; method.</strong> Outcomes reconciled from ~8,700 league-thread messages (2023&ndash;2026) against the league database and Pete's tracking workbook. Direct quotes are trimmed for length; "summit minute" lines are notes pasted into the thread after in-person votes. One canon correction: the DRC-5 keeper price is <strong>$50</strong> (the old 2023 doc's $40 is superseded). Anything <span class="rh-flag">Flagged</span> is genuinely unsettled in the record &mdash; confirm in person, don't infer.</p>

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
        events_recent = p["events"][-5:] if p["events"] else []
        events_html = "".join(
            f'<div class="ps-event">'
            f'<span class="ps-event-date">{html.escape(_event_date_display(e["date"], e.get("kind") == "trade"))}</span>'
            f'<span class="ps-event-desc">{html.escape(e["desc"])}</span>'
            f'</div>'
            for e in events_recent
        ) or '<div class="ps-event-empty">No events recorded.</div>'

        owner_chip = (
            f'<span class="ps-owner">Currently: {html.escape(p["current_owner"])}</span>'
            if p["current_owner"] else
            '<span class="ps-owner ps-owner-none">No current owner</span>'
        )

        SPARK_YEARS = (2025, 2024, 2023)  # Design: newest-first (weekly columns + neighbors)
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
        for yr in (2025, 2024, 2023):  # Design: newest-first
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
        for yr in (2025, 2024, 2023):  # Design: newest-first
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

        lineage_recent = p["lineage"][-5:] if p["lineage"] else []
        lineage_nodes = []
        for i, node in enumerate(lineage_recent):
            if i > 0:
                lineage_nodes.append('<div class="lineage-arrow">&rarr;</div>')
            method_class = node["method"].lower().replace(" ", "-")
            lineage_nodes.append(
                f'<div class="lineage-node lineage-{method_class}">'
                f'<div class="lineage-date">{html.escape(_event_date_display(node["date"], node["method"] == "Trade"))}</div>'
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
      <button class="brand-home" data-target="summary" type="button">
        <span class="brand-home-eyebrow">&#8962; League home</span>
        <span class="brand-title">{html.escape(LEAGUE_NAME)}</span>
        <span class="brand-home-sub">12-team keeper league &middot; 2026</span>
      </button>

      <h3>IYFYSTD Resources</h3>
      <details class="sidebar-teams">
        <summary>Commentary &amp; League Info</summary>
        <div class="sidebar-team-list">
          <a class="nav-link" data-target="commissioners-desk">Commissioner's Desk</a>
          <a class="nav-link" data-target="league-rules">League rules</a>
          <a class="nav-link" data-target="rules-history">Rules History</a>
          <a class="nav-link" data-target="about">About this dashboard</a>
        </div>
      </details>
      <details class="sidebar-teams">
        <summary>League Standings and Records</summary>
        <div class="sidebar-team-list">
          <a class="nav-link" data-target="summary">Summary &amp; standings</a>
        </div>
      </details>
      <details class="sidebar-teams">
        <summary>Manager Tools</summary>
        <div class="sidebar-team-list">
          <a class="nav-link" data-target="player-search">Player search</a>
          <a class="nav-link" data-target="trade-analyzer">Trade analyzer</a>
          <a class="nav-link" data-target="keeper-board">Keeper board</a>
        </div>
      </details>

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

    # --- 2026 pick inventory per team -----------------------------------
    # Every team starts with rounds 1-16 of its own draft slot; picks traded
    # during the 2025 season (= 2026-draft picks under the league's
    # next-draft convention) move between teams. "Round 17" rows are the
    # league's last-pick notation — treated as a round-16 pick.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    slug_by_tsid = {}
    for r in conn.execute(
            "SELECT t.team_season_id, m.full_name FROM teams t "
            "JOIN managers m ON m.manager_id = t.manager_id WHERE t.season = 2025"):
        slug_by_tsid[r["team_season_id"]] = slugify(r["full_name"])
    held = {t["slug"]: [{"r": r, "o": t["slug"]} for r in range(1, 17)]
            for t in teams}
    lost = {t["slug"]: [] for t in teams}
    for mv in conn.execute(
            "SELECT tp.draft_round rnd, tp.source_team_season_id s, "
            " tp.destination_team_season_id d, tp.original_team_season_id o "
            "FROM transaction_picks tp "
            "JOIN all_transactions t ON t.transaction_id = tp.transaction_id "
            "WHERE t.season = 2025 ORDER BY t.timestamp"):
        rnd = min(mv["rnd"], 16)
        last_pick = mv["rnd"] > 16
        src = slug_by_tsid.get(mv["s"])
        dst = slug_by_tsid.get(mv["d"])
        orig = slug_by_tsid.get(mv["o"]) or src
        if src in held:
            pool = held[src]
            hit = next((p for p in pool if p["r"] == rnd and p["o"] == orig),
                       next((p for p in pool if p["r"] == rnd), None))
            if hit:
                pool.remove(hit)
                if hit["o"] == src:
                    lost[src].append({"r": rnd, "to": dst})
        if dst in held:
            held[dst].append({"r": rnd, "o": orig, **({"lp": 1} if last_pick else {})})
    conn.close()
    for slug in held:
        held[slug].sort(key=lambda p: (p["r"], p["o"]))

    data_json = json.dumps({"teams": teams, "players": players,
                            "picks": held, "picks_lost": lost,
                            "season": TARGET_SEASON}, separators=(",", ":"))

    return f"""
    <section class="team-section" id="trade-analyzer" hidden>
      <header class="section-header">
        <h1 class="section-title">Trade analyzer</h1>
        <p class="section-sub">Pick two teams and check what's moving each way. The tool totals the production exchanged and lays out each player's keeper cost for {TARGET_SEASON} and the out-years under the trade-freeze rule. Numbers, not advice &mdash; the call is yours.</p>
      </header>

      <div class="ta-app"></div>

      <p class="ta-foot">Cost projections assume the trade completes before the {TARGET_SEASON} draft: the acquiring team inherits each player's trade-time DRC, frozen for {TARGET_SEASON}, with the normal decrement resuming the year after. The keeper-slotting boards place every rostered player at their DRC round under the league's slide rules &mdash; collisions slide down only through consecutive rounds you own (a missing round is a wall), and a player whose own round is gone can move UP into a free earlier pick but never down past the gap. Acquired picks are protected from the slide: the sim never spends them while one of your own picks can seat the player, and labels any optional use. Where two keepers share a DRC, the sim seats the higher 2025 scorer first; in real life that ordering is the manager's call, so treat slot assignments as one valid arrangement, not the only one. Off-season trades are executed by the commissioner (Yahoo limitation), so loop Pete in to finalize anything you agree on.</p>
    </section>
    <script>window.TRADE_DATA = {data_json};</script>"""


def render_keeper_board():
    """Keeper designation board: a per-team sandbox for the August keeper
    deadline. Toggle keepers on, watch the slide/chasm engine seat them on
    your actual 2026 pick inventory, drag (or tap) to override seating,
    track the running dollar commitment, and print a side-by-side sheet to
    check against Yahoo's keeper screen. Reads the same TRADE_DATA embed as
    the trade analyzer; client-side only, no saved state."""
    return f"""
    <section class="team-section" id="keeper-board" hidden>
      <header class="section-header">
        <h1 class="section-title">Keeper board</h1>
        <p class="section-sub">Build your {TARGET_SEASON} keeper slate against your real pick inventory. The board seats every keeper at their DRC round under the slide rules, flags anyone who can&rsquo;t legally slot (the chasm), and keeps a running total of what you&rsquo;re committing in keeper dollars. Legality and cost are the two things Yahoo&rsquo;s keeper screen won&rsquo;t show you. Nothing here saves or submits; it&rsquo;s a scratchpad.</p>
      </header>
      <div class="kb-app"></div>
      <div class="kb-print"></div>
      <p class="kb-foot">Seating defaults follow the sim&rsquo;s rule of thumb: at the same DRC, the higher 2025 scorer takes the earlier pick. The seating order is still yours to set. Pick up any kept player and move them wherever the rules allow. Print / save PDF gives you a sheet to hold next to Yahoo&rsquo;s keeper page when you enter your real designations.</p>
    </section>"""


def render_html(by_manager, search_players, comms_posts, generated_at):
    sidebar = build_sidebar(by_manager)
    summary = render_summary_section(by_manager, generated_at)
    player_search = render_player_search_section(search_players)
    trade_analyzer = render_trade_analyzer(by_manager)
    keeper_board = render_keeper_board()
    desk = render_commissioners_desk_section(comms_posts)
    rules = render_rules_section()
    rules_history = render_rules_history_section()
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
<title>{html.escape(LEAGUE_NAME)}</title>
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
<div class="crumb-bar"><button class="crumb-back" data-target="summary" type="button">&larr; League home</button><span class="crumb-sep">&rsaquo;</span><span class="crumb-current"></span></div>
{about}
{summary}
{player_search}
{trade_analyzer}
{keeper_board}
{desk}
{rules}
{rules_history}
{team_sections}
{feedback}
</main>
<button class="back-to-top" aria-label="Back to top" type="button">&uarr;</button>
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
