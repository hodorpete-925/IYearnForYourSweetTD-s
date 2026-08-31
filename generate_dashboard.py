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

# Manager alias map: real full name -> "First L." for public rendering.
# Real names stay in the DB for Yahoo API correlation; this layer sanitizes
# every dashboard surface so no real last names ship to GitHub Pages.
# The JSON file is gitignored — if it's missing we log a warning and
# fall through to raw names (so we never silently ship the wrong thing).
def _load_manager_aliases():
    path = Path(__file__).parent / "manager_aliases.json"
    if not path.exists():
        print("  WARNING: manager_aliases.json missing — dashboard will render REAL manager names")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: manager_aliases.json unreadable ({e}) — falling back to real names")
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


MANAGER_ALIASES = _load_manager_aliases()
_ALIAS_MISSING_WARNED = set()


# 2026 franchise handoffs: the person who ran the roster through 2025 vs the
# person who owns it now. Used ONLY for current-season (2026) display slots —
# roster headers, current-owner chips, 2026 lineage nodes. Historical events
# keep the historical manager's name so the record reads true.
CURRENT_HANDOFFS = {"Jon Lewitus": "Bill Keenan"}


def current_face(name):
    """Map a franchise's historical manager to its current (2026) manager
    for display in current-season contexts, then alias."""
    return alias_name(CURRENT_HANDOFFS.get(name, name))


def alias_name(name):
    """Real manager name -> First+LastInitial alias. Unknown names fall
    through with a one-time warning per name so leaks are visible in the
    generator log instead of quietly shipping."""
    if not name:
        return name
    if name in MANAGER_ALIASES:
        return MANAGER_ALIASES[name]
    if name not in _ALIAS_MISSING_WARNED:
        _ALIAS_MISSING_WARNED.add(name)
        print(f"  WARNING: no alias for {name!r} — add it to manager_aliases.json (raw name leaked in this build)")
    return name


def sanitize_rendered_html(html_str):
    """Safety-net post-render pass. Sweeps the assembled HTML for any real
    manager name that slipped through (embedded comms bodies, third-party
    modules like trade_history, error text, anywhere) and rewrites it to
    the alias.

    Two passes:
      1. Full "First Last" match -> full alias. Safe universally.
      2. Bare surname -> full alias, but SKIPPED for surnames that
         collide with NFL player surnames (Montgomery, Watson, Lewis,
         Pearson, Keenan). Catches Pete's bare-surname prose in comms
         bodies (e.g. "Schlosberg has been quite active") without
         mangling player-search tables that list "David Montgomery" or
         "Deshaun Watson". Longest first so partial-substring surnames
         (unlikely here but future-proof) can't be nested-matched.
    """
    import re as _re
    if not MANAGER_ALIASES:
        return html_str
    NFL_COLLIDING_SURNAMES = {"Montgomery", "Watson", "Lewis", "Pearson", "Keenan"}
    # Pass 1 — full names
    for real, alias in sorted(MANAGER_ALIASES.items(), key=lambda kv: -len(kv[0])):
        html_str = _re.sub(r"\b" + _re.escape(real) + r"\b", alias, html_str)
    # Pass 2 — bare surnames, skipping NFL colliders
    for real, alias in sorted(MANAGER_ALIASES.items(), key=lambda kv: -len(kv[0].split()[-1] if " " in kv[0] else "")):
        parts = real.split()
        if len(parts) < 2:
            continue
        surname = parts[-1]
        if surname in NFL_COLLIDING_SURNAMES:
            continue
        html_str = _re.sub(r"\b" + _re.escape(surname) + r"\b", alias, html_str)
    return html_str


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
        display = current_face(mgr)
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
        SELECT sp.player_id, m_dst.full_name AS dst, m_src.full_name AS src,
               DATE(st.timestamp) AS d
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

    # Collect the same moves into per-trade groups for the "Off-season
    # trades" tab. One group per (date, pair of managers); players and
    # picks received by each side, with the 2026 cost both ways: the
    # keep-path DRC the old owner faced (no trade) and the frozen
    # trade-time DRC the acquirer inherits.
    offseason_groups = {}

    def _trade_group(date, mgr_x, mgr_y):
        key = (date, frozenset((mgr_x, mgr_y)))
        if key not in offseason_groups:
            offseason_groups[key] = {
                "date": date, "mgr_a": mgr_x, "mgr_b": mgr_y,
                "players_a": [], "players_b": [],
                "picks_a": [], "picks_b": [],
            }
        return offseason_groups[key]

    for mv in moves:
        src_d, dst_d = by_manager.get(mv["src"]), by_manager.get(mv["dst"])
        if not src_d or not dst_d:
            continue
        p = next((x for x in src_d["players"]
                  if x["player_id"] == mv["player_id"]), None)
        if p is None:
            continue
        src_d["players"].remove(p)
        keep_drc = p["drc"]                    # 2026 DRC had the old owner kept him
        keep_dollars = p["drc_dollars"]
        anchor = ((p.get("history") or {}).get(2025) or {}).get("drc") or p["drc"]
        frozen = max(1, min(16, int(anchor)))
        p["drc"] = frozen
        p["drc_dollars"] = dollar.get(frozen, 10)
        p["via_trade_2026"] = True
        dst_d["players"].append(p)
        g = _trade_group(mv["d"], mv["dst"], mv["src"])
        entry = {
            "name": p["name"], "position": p["position"],
            "nfl_team": p["nfl_team"],
            "keep_drc": keep_drc, "keep_dollars": keep_dollars,
            "frozen_drc": frozen, "frozen_dollars": p["drc_dollars"],
        }
        (g["players_a"] if mv["dst"] == g["mgr_a"] else g["players_b"]).append(entry)

    # Pick movements in the same trades (table exists once
    # add_synthetic_trades.py has migrated; absent = no pick moves yet).
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='synthetic_transaction_picks'").fetchone():
        pick_moves = conn.execute("""
            SELECT DATE(st.timestamp) AS d, sp.draft_round,
                   m_src.full_name AS src, m_dst.full_name AS dst,
                   m_orig.full_name AS orig
            FROM synthetic_transaction_picks sp
            JOIN synthetic_transactions st ON st.synth_id = sp.synth_id
            JOIN teams t_src ON t_src.team_season_id = sp.source_team_season_id
            JOIN managers m_src ON m_src.manager_id = t_src.manager_id
            JOIN teams t_dst ON t_dst.team_season_id = sp.destination_team_season_id
            JOIN managers m_dst ON m_dst.manager_id = t_dst.manager_id
            JOIN teams t_orig ON t_orig.team_season_id = sp.original_team_season_id
            JOIN managers m_orig ON m_orig.manager_id = t_orig.manager_id
            WHERE st.season = 2026 AND st.event_type = 'trade'
            ORDER BY st.timestamp
        """).fetchall()
        # Resolve each pick's number-in-round from the 2026 draft order
        # (linear draft: a round-N pick originally from the team in slot S
        # is pick N.S in every round). Lottery-file names can differ from
        # DB manager names, so fall back to the team-name lookup.
        pick_by_mgr, pick_by_team = _load_2026_draft_order()
        for pm in pick_moves:
            g = _trade_group(pm["d"], pm["dst"], pm["src"])
            orig_team_name = (by_manager.get(pm["orig"]) or {}).get("team_name")
            slot = pick_by_mgr.get(pm["orig"]) or pick_by_team.get(orig_team_name)
            entry = {"round": pm["draft_round"], "original": pm["orig"],
                     "slot": slot}
            (g["picks_a"] if pm["dst"] == g["mgr_a"] else g["picks_b"]).append(entry)

    offseason_trades = sorted(offseason_groups.values(), key=lambda g: g["date"])

    # ---- Stamp finalized 2026 keeper selections -------------------------
    # keeper_selections is the committed record (add_keeper_selections.py,
    # loaded from the Yahoo submissions + the traded-in addendum). Board
    # truth above stays canon for DRC math; a drift between the stored
    # snapshot and the engine is a data problem worth a loud warning.
    # Selections key by the CURRENT franchise face (e.g. Bill Keenan);
    # by_manager keys by whoever ran the 2025 roster — bridge via
    # CURRENT_HANDOFFS.
    handoff_back = {v: k for k, v in CURRENT_HANDOFFS.items()}
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='keeper_selections'").fetchone():
        for r in conn.execute("""
                SELECT ks.player_id, ks.drc, ks.drc_dollars, ks.source,
                       m.full_name AS mgr
                FROM keeper_selections ks
                JOIN teams t ON t.team_season_id = ks.team_season_id
                JOIN managers m ON m.manager_id = t.manager_id
                WHERE ks.season = 2026"""):
            board_mgr = handoff_back.get(r["mgr"], r["mgr"])
            data = by_manager.get(board_mgr)
            hit = None
            if data:
                hit = next((p for p in data["players"]
                            if p["player_id"] == r["player_id"]), None)
            if hit is None:
                print(f"WARNING: keeper_selections row not on board: "
                      f"{r['mgr']} player_id {r['player_id']}")
                continue
            if hit["drc"] != r["drc"] or hit["drc_dollars"] != r["drc_dollars"]:
                print(f"WARNING: keeper_selections drift for {hit['name']} "
                      f"({r['mgr']}): stored DRC {r['drc']}/${r['drc_dollars']}"
                      f" vs engine DRC {hit['drc']}/${hit['drc_dollars']}")
            hit["kept_2026"] = True
            hit["keeper_source"] = r["source"]

    # Sort players within each team by DRC ascending (most expensive first), then name
    for data in by_manager.values():
        data["players"].sort(key=lambda p: (p["drc"], p["name"]))
        data["total_drc_dollars"] = sum(p["drc_dollars"] for p in data["players"])
        data["player_count"] = len(data["players"])
        data["expensive_count"] = sum(1 for p in data["players"] if p["drc"] <= 2)
        data["cheap_count"] = sum(1 for p in data["players"] if p["drc"] >= 10)
        kept = [p for p in data["players"] if p.get("kept_2026")]
        data["keeper_count"] = len(kept)
        data["committed_total"] = sum(p["drc_dollars"] for p in kept)
        data["premium_kept"] = sum(1 for p in kept if p["drc"] <= 2)

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
                owner_name = current_face(owner_row["full_name"])

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
                    yr_owner_name = current_face(r[0]) if y >= 2026 else alias_name(r[0])
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

        # ---- Cost annotations for the lineage (make the DRC chain explicit) ----
        # Self-contained chain walk over the lineage nodes, mirroring the
        # league rules: fresh draft anchors DRC at the round; waiver resets to
        # 16; a trade conveys the trade-time DRC (pre-decrement for off-season
        # trades) and freezes it for the next season. Independent of per_year
        # history so dead cost cycles annotate correctly.
        import re as _re_lin
        _anchor_yr = None   # season whose DRC equals _anchor_drc
        _anchor_drc = None
        for _idx, _node in enumerate(lineage):
            try:
                _nyr = int((_node.get("date") or "")[:4])
            except (ValueError, TypeError):
                _nyr = None
            try:
                _nmo = int((_node.get("date") or "")[5:7])
            except (ValueError, TypeError):
                _nmo = None
            _node["year"] = _nyr
            _m = _node["method"]
            _set = None
            _tag = None
            if _m == "Drafted":
                _rm = _re_lin.search(r"R(\d+)", _node.get("detail", ""))
                _set = int(_rm.group(1)) if _rm else None
                if _set is not None and _nyr is not None:
                    _anchor_yr, _anchor_drc = _nyr, _set
                _tag = "new cost cycle" if _idx > 0 else "cost cycle starts"
            elif _m in ("Waiver", "Free agent"):
                _set = 16
                if _nyr is not None:
                    _anchor_yr, _anchor_drc = _nyr, 16
                _tag = "cost resets to DRC 16"
            elif _m == "Trade":
                if _anchor_drc is not None and _nyr is not None:
                    _mid = _nmo is not None and _nmo >= 9
                    _conv = max(_nyr if _mid else _nyr - 1, _anchor_yr)
                    _set = max(_anchor_drc - max(0, _conv - _anchor_yr), 1)
                    _anchor_yr = (_nyr + 1) if _mid else _nyr
                    _anchor_drc = _set
                _tag = "carries DRC, trades never reset cost"
            _node["drc_set"] = _set
            _node["dollars_set"] = dollar.get(_set) if _set else None
            _node["cost_tag"] = _tag
            _node["cycle_break"] = _idx > 0 and _m in ("Drafted", "Waiver", "Free agent")

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

    # ---- Overlay 2026 rosters onto the search corpus --------------------
    # search_players' owner comes from get_owner_at_year_end(2025), which by
    # construction predates 2026 off-season (synthetic) trades, and its 2026
    # keep cost comes from build_history_for_player, which doesn't know the
    # synthetic trade freeze. by_manager above already applied both (the
    # moves loop), so it is the source of truth heading into 2026. Overlay
    # it here so Player search ("Currently:") and Player comparison (o / k)
    # agree with the keeper boards for every rostered player.
    board_truth = {}
    for data in by_manager.values():
        for bp in data["players"]:
            board_truth[bp["player_id"]] = (
                data["manager"], bp["drc"], bp["drc_dollars"])
    for sp in search_players:
        hit = board_truth.get(sp["player_id"])
        if not hit:
            continue
        owner_disp, drc_now, dollars_now = hit
        sp["current_owner"] = owner_disp
        for y in sp["per_year"]:
            if y["year"] == TARGET_SEASON:
                y["drc"] = drc_now
                y["dollars"] = dollars_now
                y["owner"] = owner_disp

    conn.close()
    return by_manager, failures, search_players, offseason_trades


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


def render_year_card_2026(p):
    """The 2026 card: the commitment is now KNOWN (keeper_selections), so
    the year strip leads with it. Kept players show the committed DRC/$;
    everyone else is headed to the draft pool."""
    kept = p.get("kept_2026")
    status = "Kept" if kept else "Not kept"
    status_cls = " kept-yes" if kept else " kept-no"
    adp_str = _fmt(p.get("adp_2026"), 1)
    return f"""
        <div class="year-card year-card-2026">
          <div class="year-label">2026</div>
          <div class="year-metric">
            <span class="m-label">Cost</span>
            <span class="m-val">DRC {p['drc']}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">Status</span>
            <span class="m-val{status_cls}">{status}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">$</span>
            <span class="m-val">${p['drc_dollars']}</span>
          </div>
          <div class="year-metric">
            <span class="m-label">ADP</span>
            <span class="m-val">{adp_str}</span>
          </div>
        </div>"""


def render_history_subrow(player_id, history, colspan, player=None):
    """Render history as horizontal year-cards (descending). 2026 leads
    with the committed keeper status (Pete's request 2026-08-30), then
    2025, 2024, 2023."""
    cards = ("" if player is None else render_year_card_2026(player)) + "".join(
        render_year_card(year, history.get(year))
        for year in (2025, 2024, 2023)
    )
    return f"""
        <tr class="history-row" id="hist-{player_id}" hidden>
          <td colspan="{colspan}" class="history-cell">
            <div class="history-cards">{cards}</div>
          </td>
        </tr>"""


def render_player_row(p, slot_label=None):
    slot_cell = ("" if slot_label is None else
                 f'<td class="meta slot-col">{html.escape(slot_label)}</td>')
    pid = p.get("player_id", id(p))
    ncols = 5 + (1 if slot_label is not None else 0)
    main_row = f"""
        <tr>
          {slot_cell}
          <td class="player-name">{html.escape(p['name'])}<span class="sub-line">{html.escape(p['position'])} &middot; {html.escape(p['nfl_team'])}</span></td>
          <td class="meta">{html.escape(p['position'])}</td>
          <td class="num"><span class="pill {drc_tier_class(p['drc'])}">{p['drc']}</span></td>
          <td class="num cost">${p['drc_dollars']}</td>
          <td class="expand-col">
            <button class="expand-btn" data-target="hist-{pid}" aria-label="Show prior years">›</button>
          </td>
        </tr>"""

    sub_row = render_history_subrow(pid, p.get("history", {}), colspan=ncols,
                                    player=p)
    return main_row + sub_row


def _lineup_assign(players):
    """Mirror of the keeper board's lineupAssign(): best 2026 ADP fills
    each starting slot first (2025 pts as tiebreak), the rest sit on the
    bench. 2026 lineup: QB, RB, RB, WR, WR, WR, TE, W/R/T, Q/W/R/T, K,
    DEF + bench. Returns (starters, bench): starters is a list of
    (slot_label, player-or-None)."""
    def adp_val(p):
        a = p.get("adp_2026")
        return a if a is not None else 1e6
    def pts25(p):
        h = (p.get("history") or {}).get(2025) or {}
        v = h.get("pts")
        return v if isinstance(v, (int, float)) else 0.0
    pool = {"QB": [], "RB": [], "WR": [], "TE": [], "K": [], "DEF": []}
    other = []
    for p in sorted(players, key=lambda p: (adp_val(p), -pts25(p))):
        pool.get(p.get("position"), other).append(p)

    def take(pos):
        return pool[pos].pop(0) if pool[pos] else None

    def take_best(poss):
        best = None
        for pos in poss:
            if pool[pos] and (best is None or
                              (adp_val(pool[pos][0]), -pts25(pool[pos][0])) <
                              (adp_val(pool[best][0]), -pts25(pool[best][0]))):
                best = pos
        return pool[best].pop(0) if best else None

    starters = [
        ("QB", take("QB")),
        ("RB", take("RB")), ("RB", take("RB")),
        ("WR", take("WR")), ("WR", take("WR")), ("WR", take("WR")),
        ("TE", take("TE")),
        ("W/R/T", take_best(["WR", "RB", "TE"])),
        ("Q/W/R/T", take_best(["QB", "WR", "RB", "TE"])),
        ("K", take("K")),
        ("DEF", take("DEF")),
    ]
    bench = sorted(
        pool["QB"] + pool["RB"] + pool["WR"] + pool["TE"] + pool["K"]
        + pool["DEF"] + other,
        key=lambda p: (adp_val(p), -pts25(p)))
    return starters, bench


def render_empty_slot_row(slot_label):
    return f"""
        <tr class="slot-empty">
          <td class="meta slot-col">{html.escape(slot_label)}</td>
          <td class="player-name empty-slot" colspan="4">open &mdash; filled at the draft</td>
          <td class="expand-col"></td>
        </tr>"""


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
    """Date-slot label. PRE-2026 off-season trades (month < Sept) read
    'OFF-SEASON TRADE' — their exact date is fuzzy (synthetic /
    commish-pushed, mostly pinned to June 30). From 2026 on, synthetic
    trades are entered with their real transaction date (Pete's ruling
    2026-08-22), so the actual date is shown like any other event.
    Hardcoded uppercase because the date slots have no CSS text-transform
    (only the method label + section headers do), and we want it to match
    that caps styling. `display` overrides the shown text otherwise
    (e.g. a pre-formatted date)."""
    shown = display if display is not None else iso_date
    try:
        if (is_trade and int(str(iso_date)[5:7]) < 9
                and int(str(iso_date)[:4]) < 2026):
            return f"{str(iso_date)[:4]} OFF-SEASON TRADE"
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


def render_drafts_tab(draft_history, slug, none_open=False):
    """Render the Drafts tab content: collapsible year blocks 2025 / 2024 / 2023.
    none_open=True when a 2026 keeper block sits above these and takes the
    default-open spot."""
    years_desc = sorted(draft_history.keys(), reverse=True)
    # Default the most recent year open
    blocks = "".join(
        render_year_drafts(y, draft_history.get(y, []),
                           is_default_open=(i == 0 and not none_open), slug=slug)
        for i, y in enumerate(years_desc)
    )
    return blocks or '<p class="empty-note">No draft history found.</p>'


def render_2026_draft_block(slug, data, pick_data):
    """The team's 2026 draft, pre-populated by the keeper process: every
    pick the team holds, in round order, with the seated keeper on the
    pick that seating consumes and "open" on the picks the team will
    actually make on draft day. Mirrors the league-wide draft board's
    seating (same TRADE_DATA computation)."""
    held = sorted(pick_data["held"].get(slug, []),
                  key=lambda pk: (pk["r"], pick_data["draft_pos"].get(pk["o"], 99)))
    seat_by_pick = {(st["r"], st["o"]): st
                    for st in pick_data["seats"].get(slug, [])}
    chasm_pids = pick_data["chasm"].get(slug, [])
    mgr_by_slug = {t["slug"]: t["mgr"] for t in pick_data["teams"]}
    pby = {pl["player_id"]: pl for pl in data["players"]}
    draft_pos = pick_data["draft_pos"]

    by_round = {}
    for pk in held:
        by_round.setdefault(pk["r"], []).append(pk)

    rows_parts = []
    seated_n = 0
    for rnd in range(1, 17):
        picks = by_round.get(rnd)
        if not picks:
            rows_parts.append(f"""
        <tr class="round-empty round-{rnd}">
          <td class="round-cell">R{rnd}</td>
          <td colspan="3" class="meta">No pick &mdash; traded away</td>
        </tr>""")
            continue
        for pk in picks:
            slot = draft_pos.get(pk["o"])
            num = f"{rnd}.{slot:02d}" if slot else "&mdash;"
            acq_note = ""
            if pk["o"] != slug:
                first = html.escape((mgr_by_slug.get(pk["o"]) or "").split(" ")[0])
                acq_note = f' <span class="draft-tag tag-traded">via {first}</span>'
            st = seat_by_pick.get((pk["r"], pk["o"]))
            if st and st["pid"] in pby:
                pl = pby[st["pid"]]
                seated_n += 1
                up_note = (f' <span class="k26-up">slid up from R{pl["drc"]}</span>'
                           if st.get("up") else "")
                player_cell = (f'<span class="k26-name">{html.escape(pl["name"])}</span>'
                               f' <span class="draft-pos">{html.escape(pl["position"])}</span>'
                               f'<span class="draft-tag tag-kept">Kept</span>{up_note}{acq_note}')
                cost_cell = f'<span class="pill {drc_tier_class(pl["drc"])}">DRC {pl["drc"]}</span> ${pl["drc_dollars"]}'
            else:
                player_cell = f'<span class="k26-open">open &mdash; filled on draft day</span>{acq_note}'
                cost_cell = ""
            rows_parts.append(f"""
        <tr class="round-{rnd}">
          <td class="round-cell">R{rnd}</td>
          <td class="num">{num}</td>
          <td>{player_cell}</td>
          <td class="num">{cost_cell}</td>
        </tr>""")

    chasm_html = ""
    for pid in chasm_pids:
        pl = pby.get(pid)
        if pl:
            chasm_html += (f'<p class="k26-chasm">&#9888; {html.escape(pl["name"])} '
                           f'(DRC {pl["drc"]}) is kept but has NO legal pick to '
                           f'occupy &mdash; resolve with the commissioner before '
                           f'the draft.</p>')

    body = f"""
            <table class="draft-table">
              <thead>
                <tr>
                  <th>Rd</th>
                  <th class="num">Pick</th>
                  <th>Player</th>
                  <th class="num">Cost</th>
                </tr>
              </thead>
              <tbody>{rows_parts and "".join(rows_parts)}</tbody>
            </table>{chasm_html}"""

    return f"""
        <div class="year-collapsible open" id="year-{slug}-2026">
          <button class="year-collapsible-header" data-target="year-{slug}-2026">
            <span class="year-title">2026</span>
            <span class="year-meta">{seated_n} keepers seated &middot; {len(held)} picks held</span>
            <span class="year-chev">&rsaquo;</span>
          </button>
          <div class="year-collapsible-body">
            {body}
          </div>
        </div>"""


def render_team_section(data, slug, pick_data=None):
    total = data["committed_total"]
    kcount = data["keeper_count"]
    premium_kept = data["premium_kept"]
    if pick_data is not None:
        _slug_picks = pick_data["held"].get(slug, [])
        _seated = pick_data["seats"].get(slug, [])
        picks_to_make = len(_slug_picks) - len(_seated)
    else:
        picks_to_make = None

    # Roster tab (Pete's rulings 2026-08-30): keepers ONLY — a player who
    # wasn't kept is no longer on the roster. Laid out in Yahoo's lineup
    # structure: the best player fills each starting slot, kept overflow
    # rides the bench, everything else is an open slot for draft day.
    kept_players = [pl for pl in data["players"] if pl.get("kept_2026")]
    starters, bench = _lineup_assign(kept_players)
    starter_rows = "".join(
        render_player_row(pl, slot_label=lbl) if pl is not None
        else render_empty_slot_row(lbl)
        for lbl, pl in starters)
    bench_rows = ("".join(render_player_row(pl, slot_label="BN")
                          for pl in bench)
                  or render_empty_slot_row("BN"))
    rows = (f'<tr class="group-h"><td colspan="6">Starting lineup</td></tr>'
            f'{starter_rows}'
            f'<tr class="group-h"><td colspan="6">Bench</td></tr>'
            f'{bench_rows}')
    drafts_html = render_drafts_tab(data.get("draft_history", {}), slug,
                                    none_open=pick_data is not None)
    if pick_data is not None:
        drafts_html = render_2026_draft_block(slug, data, pick_data) + drafts_html
    trades_html = render_trades_tab(data.get("trade_history", []), slug)

    return f"""
    <section class="team-section" id="team-{slug}" hidden>
      <div class="eyebrow">Manager</div>
      <h1 class="team-name">{html.escape(data['team_name'])}</h1>
      <p class="manager-name">{html.escape(data['manager'])}</p>

      <div class="kpis">
        <div class="kpi">
          <div class="k">2026 committed keeper cost</div>
          <div class="v">${total:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Keepers locked in</div>
          <div class="v">{kcount}</div>
        </div>
        <div class="kpi">
          <div class="k">Premium keepers (DRC ≤ 2)</div>
          <div class="v">{premium_kept}</div>
        </div>
        <div class="kpi">
          <div class="k">Picks to make on draft day</div>
          <div class="v">{picks_to_make if picks_to_make is not None else "&mdash;"}</div>
        </div>
      </div>

      <div class="tabs" data-tabgroup="{slug}">
        <button class="tab-btn active" data-tab="{slug}-roster">Roster</button>
        <button class="tab-btn" data-tab="{slug}-drafts">Drafts</button>
        <button class="tab-btn" data-tab="{slug}-trades">Trades</button>
      </div>

      <div class="tab-panel active" id="{slug}-roster">
        <p class="roster-note">The {TARGET_SEASON} roster heading into the draft &mdash; keepers only, since everyone else is back in the draft pool. The best player fills each starting slot, kept overflow rides the bench, and open slots get filled on draft day.</p>
        <table class="roster team-roster">
          <thead>
            <tr>
              <th>Slot</th>
              <th>Player</th>
              <th>Pos</th>
              <th class="num">DRC</th>
              <th class="num">Cost</th>
              <th class="expand-col"></th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
          <tr class="total">
            <td class="meta"></td>
            <td>Total committed ({kcount} keepers)</td>
            <td class="meta"></td>
            <td class="num"></td>
            <td class="num cost">${total:,}</td>
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


def _render_updated_widget(generated_at, meta):
    """CSS-only click-to-expand "last updated" widget in the footer.
    Uses the hidden-checkbox + label pattern so it works with zero JS —
    good for print, good for any file previewer, good on any browser."""
    meta = meta or {}
    data_refreshed = meta.get("data_refreshed_at") or "unknown"
    deploy_time = meta.get("deploy_time") or "unknown"
    deploy_msg = meta.get("deploy_msg") or ""
    recent = meta.get("recent_commits") or []
    log_lines = "\n".join(
        f"  {ts}  {html.escape(msg)}" for ts, msg in recent
    ) or "  (no git history available)"
    deploy_line = (f'{deploy_time} EDT &mdash; {html.escape(deploy_msg)}'
                   if deploy_msg else f'{deploy_time} EDT')
    return f"""
      <div class="updated-widget">
        <input type="checkbox" id="updated-toggle" class="updated-toggle">
        <label for="updated-toggle" class="updated-trigger" aria-label="Show update details">
          Updated {generated_at} EDT
        </label>
        <div class="updated-details" role="region" aria-labelledby="updated-toggle">
          <div class="updated-row"><span class="updated-k">Last data refresh</span><span class="updated-v">{data_refreshed} EDT</span></div>
          <div class="updated-row"><span class="updated-k">Last deploy</span><span class="updated-v">{deploy_line}</span></div>
          <div class="updated-row"><span class="updated-k">Dashboard rebuilt</span><span class="updated-v">{generated_at} EDT (this page)</span></div>
          <div class="updated-log">
            <div class="updated-log-h">Recent activity</div>
            <pre class="updated-log-body">{log_lines}</pre>
          </div>
        </div>
      </div>"""


def render_summary_section(by_manager, generated_at, meta=None):
    # Keepers are FINAL (locked 2026-08; keeper_selections is the record),
    # so the league home ranks teams by what they actually committed.
    teams = sorted(by_manager.values(),
                   key=lambda d: (-d["committed_total"], d["team_name"]))
    league_total = sum(d["committed_total"] for d in teams)
    avg = league_total // max(len(teams), 1)
    premium_total = sum(d["premium_kept"] for d in teams)
    keeper_total = sum(d["keeper_count"] for d in teams)

    rows = ""
    for idx, t in enumerate(teams, 1):
        slug = manager_slug(t["manager_actual"])
        rows += f"""
          <tr>
            <td class="rank">{idx}</td>
            <td class="player-name"><a href="#" data-target="team-{slug}">{html.escape(t['team_name'])}</a><span class="sub-line">{html.escape(t['manager'])}</span></td>
            <td class="meta">{html.escape(t['manager'])}</td>
            <td class="num">{t['keeper_count']}</td>
            <td class="num">{t['premium_kept']}</td>
            <td class="num cost">${t['committed_total']:,}</td>
          </tr>"""

    return f"""
    <section class="team-section" id="summary">
      <div class="eyebrow">{TARGET_SEASON} keepers locked</div>
      <h1 class="team-name">League keeper commitments</h1>
      <p class="manager-name">Keepers are final. This is what each team locked in and what it owes the pot.</p>

      <div class="kpis">
        <div class="kpi">
          <div class="k">Total owed to the pot</div>
          <div class="v">${league_total:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Average per team</div>
          <div class="v">${avg:,}</div>
        </div>
        <div class="kpi">
          <div class="k">Keepers league-wide</div>
          <div class="v">{keeper_total}</div>
        </div>
        <div class="kpi">
          <div class="k">Premium keepers (DRC ≤ 2)</div>
          <div class="v">{premium_total}</div>
        </div>
      </div>

      <h2>Teams ranked by {TARGET_SEASON} keeper commitment</h2>
      <table class="roster standings">
        <thead>
          <tr>
            <th>#</th>
            <th>Team</th>
            <th>Manager</th>
            <th class="num">Keepers</th>
            <th class="num">Premium</th>
            <th class="num">Owed</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>

      <p class="footnote">Source: fantasy.db &middot; DRC algorithm: compute_drc.py</p>
      {_render_updated_widget(generated_at, meta)}
    </section>"""


def slugify(name):
    return name.lower().replace(" ", "-").replace(".", "").replace("'", "")


def manager_slug(name):
    """Slug for a manager, aliased first. Real last names leak into HTML
    attribute values (data-target, id) and JS payload keys otherwise —
    'team-pete-hodor' is a real-name leak even though nothing renders it
    visibly. Route every manager-derived slug through this helper so
    aliases propagate into IDs, hrefs, and JSON keys too."""
    return slugify(alias_name(name))


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
  /* Keep touch scrolling contained to the sidebar — without this, when the
     sidebar hits its top/bottom, the scroll gesture "chains" to the
     underlying page and the sidebar feels stuck. */
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}
/* Subtle draft-slot indicator prepended to each team name in the sidebar
   Teams list. Small, muted, tabular; a hint, not a competing element. */
.sidebar .draft-slot {
  display: inline-block;
  min-width: 18px;
  margin-right: 8px;
  color: rgba(255, 255, 255, 0.42);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.04em;
  text-align: right;
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
  padding: 14px 18px 14px 46px;
  font-family: inherit;
  font-size: 15.5px;
  border: 2px solid #c5c9d2;
  border-radius: 10px;
  background: #fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23606C71' stroke-width='2.4' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='16.5' y1='16.5' x2='21' y2='21'/%3E%3C/svg%3E") 14px center / 20px 20px no-repeat;
  color: var(--gray-800);
  box-shadow: 0 2px 8px rgba(2, 36, 121, 0.08);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.ps-input:hover { border-color: #9aa1ad; }
.ps-input::placeholder { color: #82889a; }
.ps-input:focus {
  outline: none;
  border-color: var(--blue-600);
  box-shadow: 0 0 0 3px rgba(0, 56, 255, 0.14), 0 2px 8px rgba(2, 36, 121, 0.08);
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
.lineage-cost {
  display: inline-block;
  margin-top: 8px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef4ff;
  color: var(--blue-800);
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.lineage-cost-now { background: #e8f6ec; color: #14532d; font-size: 13px; }
.lineage-now { border-left-color: #16a34a; background: #fbfffc; }
.lineage-tag {
  font-size: 10.5px;
  color: var(--gray-500);
  margin-top: 4px;
  letter-spacing: 0.02em;
}
.lineage-break {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 0 6px;
  max-width: 92px;
  text-align: center;
}
.lineage-break-x { color: #b91c1c; font-size: 14px; font-weight: 700; }
.lineage-break-txt {
  font-size: 9.5px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #b91c1c;
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

/* --- Last-updated widget: click-to-expand footer stamp -----------------
   CSS-only accordion driven by a hidden checkbox; no JS. Trigger sits
   inline with the source footnote as a subtle right-aligned text link. */
.updated-widget { margin: 4px 0 0; text-align: right; }
.updated-toggle {
  position: absolute; opacity: 0; pointer-events: none;
  width: 0; height: 0; margin: 0;
}
.updated-trigger {
  display: inline-flex; align-items: center; gap: 5px;
  cursor: pointer; user-select: none;
  font-size: 11.5px; color: var(--gray-500);
  padding: 4px 8px; border-radius: 4px;
  font-variant-numeric: tabular-nums;
}
.updated-trigger:hover { background: rgba(0, 0, 0, 0.04); color: var(--gray-700); }
.updated-trigger::after { content: "\25B8"; font-size: 9px; opacity: 0.6; }
.updated-toggle:checked + .updated-trigger::after { content: "\25BE"; }
.updated-toggle:focus-visible + .updated-trigger {
  outline: 2px solid var(--blue-600); outline-offset: 2px;
}
.updated-details {
  display: none;
  text-align: left;
  margin-top: 6px;
  background: #fbfbfd; border: 1px solid var(--gray-200); border-radius: 8px;
  padding: 10px 14px 12px; font-size: 12px; color: var(--gray-700);
  overscroll-behavior: contain;
}
.updated-toggle:checked ~ .updated-details { display: block; }
.updated-details .updated-row {
  display: flex; gap: 10px; align-items: baseline;
  padding: 3px 0;
}
.updated-details .updated-k {
  flex: 0 0 130px;
  font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase;
  font-weight: 600; color: var(--gray-500);
}
.updated-details .updated-v {
  flex: 1 1 auto; min-width: 0; word-break: break-word;
  font-variant-numeric: tabular-nums;
}
.updated-details .updated-log {
  margin-top: 10px;
  border-top: 1px solid var(--gray-100);
  padding-top: 8px;
}
.updated-details .updated-log-h {
  font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase;
  font-weight: 600; color: var(--gray-500); margin-bottom: 4px;
}
.updated-details .updated-log-body {
  margin: 0;
  font-family: "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace;
  font-size: 10.5px; color: var(--gray-600);
  white-space: pre-wrap; word-break: break-word; line-height: 1.5;
}
/* Mobile tightening — narrower key column, smaller monospace. */
@media (max-width: 480px) {
  .updated-details .updated-row { flex-wrap: wrap; gap: 2px; }
  .updated-details .updated-k { flex-basis: 100%; font-size: 9.5px; }
  .updated-details .updated-v { font-size: 11.5px; }
  .updated-details .updated-log-body { font-size: 10px; }
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
    /* Explicit dvh height (dynamic viewport height) instead of
       top:0 + bottom:0. Mobile Chrome's URL bar & tabs bar collapse and
       re-expand as the user scrolls; vh + bottom:0 both compute against
       the LARGEST viewport, so the sidebar's bottom is hidden under the
       chrome when the bar is expanded. dvh tracks the visible viewport,
       so the sidebar's floor sits above the chrome and the last team
       stays tappable. Fallback for browsers that don't support dvh:
       start with 100vh, then override with 100dvh. */
    height: 100vh;
    height: 100dvh;
    bottom: auto;
    right: auto;
    width: min(280px, 82vw);
    z-index: 100;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    overflow-y: auto;
    overflow-x: hidden;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
    /* Clear the iOS home-indicator gesture area + Chrome's bottom chrome
       so the last team in the Teams list isn't visually pinned to the
       edge (or worse, hidden under the chrome). */
    padding-bottom: calc(40px + env(safe-area-inset-bottom, 0px));
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
  /* Compact roster (2026-08-30): Slot, Player, Pos, DRC, Cost. On small
     screens hide only Pos (3) — the player sub-line carries pos/team. */
  table.team-roster th:nth-child(3), table.team-roster td:nth-child(3) { display: none; }
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

/* ---- Player comparison ----------------------------------------------- */
.pc-top { position:relative; max-width:560px; margin:0 0 16px; }
.pc-sugg { position:absolute; top:100%; left:0; right:0; z-index:40; background:#fff; border:1px solid #d8d8dc; border-radius:10px; margin-top:4px; box-shadow:0 8px 24px rgba(0,0,0,.12); overflow:hidden; }
.pc-sugg-item { display:flex; align-items:center; gap:8px; padding:8px 12px; cursor:pointer; font-size:13px; }
.pc-sugg-item:hover { background:#eef4ff; }
.pc-sugg-item .pc-sugg-meta { color:#8e8e93; font-size:11.5px; flex:none; }
.pc-sugg-item .pc-sugg-owner { margin-left:auto; color:#606C71; font-size:11px; text-align:right; }
.pc-cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; align-items:start; }
.pc-card { background:#fff; border:1px solid #e3e3e6; border-radius:12px; padding:14px 16px; }
.pc-head { display:flex; align-items:baseline; gap:8px; }
.pc-name { font-weight:700; font-size:15.5px; color:#022479; flex:1 1 auto; min-width:0; }
.pc-x { flex:none; border:none; background:none; color:#98a0ad; cursor:pointer; font-size:14px; padding:0 2px; }
.pc-x:hover { color:#b42318; }
.pc-meta { color:#606C71; font-size:12px; margin:2px 0 8px; }
.pc-owner { font-size:12px; color:#2a2a2e; background:#f7f8fa; border:1px solid #ececef; border-radius:8px; padding:5px 9px; margin:0 0 4px; }
.pc-owner b { color:#022479; }
.pc-season { border-top:1px solid #f0f0f2; padding-top:10px; margin-top:10px; }
.pc-yr { font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:#8e8e93; font-weight:700; }
.pc-dot { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:6px; vertical-align:baseline; }
.pc-pts { font-size:20px; font-weight:700; color:#022479; font-variant-numeric:tabular-nums; }
.pc-sub { color:#606C71; font-size:11.5px; margin:1px 0 6px; }
.pc-empty { color:#8e8e93; font-size:12.5px; padding:8px 0 2px; }
.pc-hintcard { border:1px dashed #d8d8dc; border-radius:10px; padding:26px; text-align:center; color:#8e8e93; font-size:13px; grid-column:1/-1; }
.pc-chart { width:100%; height:54px; display:block; }
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
.kb-slot { flex:1 1 0; border:1.5px dashed #d6d9e0; border-radius:8px; min-height:30px; padding:3px 8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:12.5px; background:#fbfbfc; }
.kb-slot.kb-acq { border-style:solid; border-color:#c9b45e; background:#fdfaf0; }
.kb-slot .kb-origin { color:#8a6a12; font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; flex:none; }
.kb-slot.kb-legal { border-color:#0038FF; background:#eef4ff; box-shadow:0 0 0 2px rgba(0,56,255,.12); cursor:pointer; }
.kb-slot.kb-illegal { opacity:.45; }
.kb-slot .kb-seated { font-weight:600; flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kb-slot .kb-meta { flex:none; font-variant-numeric:tabular-nums; }
.kb-slot .kb-slot-notes { flex:1 1 100%; order:9; color:#98a0ad; font-size:10.5px; line-height:1.3; padding:0 0 1px 2px; margin-top:-2px; }
.kb-slot .kb-flag { flex:none; font-size:10.5px; background:#fff6e0; color:#8a6a12; border-radius:10px; padding:1px 7px; }
.kb-slot .kb-x { flex:none; margin-left:auto; border:none; background:none; color:#98a0ad; cursor:pointer; font-size:13px; padding:0 2px; }
.kb-slot .kb-x:hover { color:#b42318; }
.kb-row.kb-gone { background:#fdf3f2; }
.kb-row.kb-gone .kb-goneto { color:#b42318; font-size:12px; }
.kb-chasm-strip { margin:10px 12px; border-top:1px dashed #f0cfc9; padding-top:10px; }
.kb-chasm-chip { display:inline-block; background:#fdecea; color:#b42318; font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px; margin:0 6px 6px 0; }
.kb-hint { color:#606C71; font-size:12px; margin:10px 0 0; }
/* Pick numbers (lottery order), stacked multi-pick rounds, drag states */
.kb-picknum { flex:none; font-size:10.5px; font-weight:700; color:#606C71; background:#f0f1f4; border-radius:6px; padding:1px 6px; font-variant-numeric:tabular-nums; }
.kb-slot.kb-acq .kb-picknum { background:#f5ecce; color:#8a6a12; }
.kb-picknum-gone { background:#f7dcd8; color:#b42318; }
.kb-row.kb-row-cont { border-top:none; padding-top:2px; }
.kb-rnum-cont { color:#c5c9d2; font-weight:600; }
.kb-gone-slot { border-style:none; background:none; }
.kb-slot[data-dragpid] { cursor:grab; user-select:none; }
.kb-slot[data-dragpid]:active { cursor:grabbing; }
.kb-slot.kb-drop-hot { background:#dbe7ff; border-style:solid; border-color:#0038FF; }
.kb-dragging .kb-roster { opacity:.75; }
.kb-card.kb-unplaced { background:#fff8e6; }
.kb-card.kb-unplaced .kb-check { background:#c9971c; border-color:#c9971c; }
/* Slot-first placement: open picks are tappable */
.kb-slot.kb-open { cursor:pointer; }
.kb-slot.kb-open:hover { border-color:#9aa1ad; background:#f4f6fa; }
.kb-open-hint { color:#b6bcc7; font-size:11.5px; }
.kb-pf-overlay { position:fixed; inset:0; background:rgba(15,18,25,.45); z-index:120; display:flex; align-items:center; justify-content:center; padding:16px; }
.kb-pf { background:#fff; border-radius:14px; max-width:440px; width:100%; padding:16px 18px; box-shadow:0 12px 40px rgba(0,0,0,.25); }
.kb-pf h3 { margin:0 0 10px; font-size:15px; }
.kb-pf-list { max-height:60vh; overflow-y:auto; display:flex; flex-direction:column; gap:6px; }
.kb-pf-item { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px; width:100%; text-align:left; font:inherit; border:1.5px solid #d6d9e0; background:#fbfbfc; border-radius:10px; padding:9px 12px; cursor:pointer; }
.kb-pf-item:hover { border-color:#0038FF; background:#eef4ff; }
.kb-pf-nm { font-weight:700; font-size:13.5px; flex:1 1 auto; min-width:0; }
.kb-pf-pos { color:#606C71; font-weight:400; font-size:12px; }
.kb-pf-meta { flex:none; color:#2a2a2e; font-size:12px; font-variant-numeric:tabular-nums; }
.kb-pf-tag { flex:1 1 100%; color:#8a919c; font-size:10.5px; }
.kb-pf-none { color:#606C71; font-size:12.5px; margin:4px 0 8px; }
.kb-pf-cancel { margin-top:10px; width:100%; }
/* Trade analyzer: pick pills are clickable (add to / remove from trade) */
.ta-pill { cursor:pointer; user-select:none; }
.ta-pill:hover { box-shadow:0 0 0 2px rgba(0,56,255,.15); }
.kb-wait-chip { display:inline-block; background:#fff6e0; color:#8a6a12; font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px; margin:0 6px 6px 0; }
/* ---- 2026 draft board (league-wide) ---------------------------------- */
.db26-top { margin: 0 0 14px; }
.db26-toggle { font-size: 13px; font-weight: 600; color: #2b2b2e; display: flex; align-items: baseline; gap: 8px; cursor: pointer; flex-wrap: wrap; }
.db26-toggle input { transform: translateY(1px); }
.db26-toggle-sub { font-weight: 400; color: #606C71; font-size: 12px; }
.db26-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; align-items: start; }
.db26-round { background: #fff; border: 1px solid #ebebed; border-radius: 8px; overflow: hidden; }
.db26-round-h { display: flex; justify-content: space-between; align-items: baseline; padding: 8px 12px; background: #f7f8fa; border-bottom: 1px solid #ebebed; font-weight: 700; font-size: 13px; color: #022479; }
.db26-cost { font-weight: 600; font-size: 11px; color: #606C71; }
.db26-pick { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; padding: 6px 12px; border-bottom: 1px solid #f2f2f4; }
.db26-pick:last-child { border-bottom: none; }
.db26-num { flex: 0 0 44px; font-variant-numeric: tabular-nums; font-weight: 700; font-size: 12px; color: #606C71; }
.db26-team { font-size: 13px; font-weight: 600; display: flex; flex-direction: column; min-width: 0; flex: 1; }
.db26-mgr { font-weight: 400; font-size: 11.5px; color: #606C71; }
.db26-acq { background: #fdfaf0; }
.db26-acq .db26-num { color: #8a6a12; }
.db26-stack { flex-basis: 100%; padding: 2px 0 4px 52px; display: flex; flex-direction: column; gap: 2px; }
.db26-kp { font-size: 12px; font-weight: 600; }
.db26-kp-sub { font-weight: 400; color: #606C71; font-size: 11px; }
.db26-none { font-size: 11px; color: #98a0ad; font-style: italic; }
.db26-used { background: #f2f8f4; }
.db26-seat { flex-basis: 100%; padding: 2px 0 2px 52px; font-size: 12.5px; font-weight: 700; color: #116b3f; }
.db26-seat .db26-kp-sub { font-weight: 400; }
.db26-await { background: #fffaf0; border: 1px solid #f0e3c0; border-radius: 8px; padding: 10px 14px; margin: 0 0 14px; display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12.5px; }
.db26-await-h { flex-basis: 100%; font-weight: 700; color: #8C6E10; font-size: 12px; letter-spacing: .02em; }
.db26-await-item.db26-chasm { color: #b42318; font-weight: 600; }
.slot-col { font-weight: 700; color: #022479; white-space: nowrap; width: 64px; }
tr.slot-empty td.empty-slot { color: #98a0ad; font-style: italic; font-weight: 400; }
tr.group-h td { background: #f7f8fa; font-weight: 700; font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: #606C71; padding: 6px 12px; border-top: 1px solid #ebebed; }
.pill.kept-pill { background: #e8f5ee; color: #116b3f; font-weight: 700; }
.year-card-2026 { border-color: #c9dfd2; }
.m-val.kept-yes { color: #116b3f; font-weight: 700; }
.m-val.kept-no { color: #98a0ad; }
.roster-note { font-size: 12.5px; color: #606C71; margin: 0 0 10px; }
.k26-name { font-weight: 600; }
.k26-open { color: #98a0ad; font-style: italic; }
.k26-up { font-size: 11px; color: #8C6E10; font-weight: 600; }
.k26-chasm { color: #b42318; font-weight: 600; font-size: 12.5px; padding: 8px 12px; }

/* Keeper board: slot-occupancy banner */
.kb-slotbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
.kb-slotbar-inpanel { margin: 10px 14px 2px; }
.kb-slotchip { font-size: 12px; font-weight: 600; color: #606C71; background: #f0f0f2; border-radius: 6px; padding: 4px 10px; white-space: nowrap; }
.kb-slotchip .kb-slotnum { font-variant-numeric: tabular-nums; font-weight: 700; }
.kb-slot-part { background: #e6efff; color: #022479; }
.kb-slot-full { background: #022479; color: #fff; }
.kb-slot-over { background: #fdecea; color: #b42318; }

/* Keeper board: lineup preview (Yahoo slot layout) */
.kb-lineup { margin-top: 16px; }
.kb-lu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 28px; padding: 10px 14px 14px; }
.kb-lu-colh { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: #606C71; padding: 6px 0 4px; border-bottom: 1px solid #ebebed; }
.kb-lu-row { display: flex; align-items: baseline; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f2f2f4; min-height: 30px; flex-wrap: wrap; }
.kb-lu-slot { flex: 0 0 62px; font-size: 11px; font-weight: 700; color: #022479; background: #e6efff; border-radius: 4px; text-align: center; padding: 2px 0; }
.kb-lu-slot-bn { background: #f0f0f2; color: #606C71; }
.kb-lu-nm { font-weight: 600; font-size: 13px; }
.kb-lu-sub { color: #606C71; font-weight: 400; font-size: 12px; }
.kb-lu-meta { margin-left: auto; color: #606C71; font-size: 11.5px; font-variant-numeric: tabular-nums; }
.kb-lu-open { color: #98a0ad; font-size: 12px; font-style: italic; }
.kb-lu-over { background: #fdf3f2; }
.kb-lu-over .kb-lu-slot { background: #fdecea; color: #b42318; }
@media (max-width: 760px) { .kb-lu-grid { grid-template-columns: 1fr; } }

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
.kb-print .kb-print-picknum { color:#98a0ad; font-size:11px; font-weight:400; }
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

/* --- Off-season trades tab --------------------------------------------- */
.ot-list { display: flex; flex-direction: column; gap: 20px; max-width: 880px; }
.ot-card {
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: #fff;
  overflow: hidden;
}
.ot-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--gray-200);
  background: var(--gray-50);
}
.ot-date {
  font-size: 11px;
  font-weight: 600;
  color: var(--gray-600);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  white-space: nowrap;
}
.ot-teams { font-size: 15px; font-weight: 600; }
.ot-swap { color: var(--blue-600); font-weight: 400; padding: 0 2px; }
.ot-sides { display: grid; grid-template-columns: 1fr 1fr; }
.ot-side { padding: 16px 20px 18px; }
.ot-side + .ot-side { border-left: 1px solid var(--gray-200); }
.ot-side-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--gray-600);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin-bottom: 10px;
}
.ot-player { padding: 10px 0 12px; }
.ot-player + .ot-player { border-top: 1px solid var(--gray-200); }
.ot-player-top { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.ot-player-name { font-weight: 600; font-size: 14.5px; }
.ot-player-meta { color: var(--gray-500); font-size: 12px; }
.ot-costline {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  margin-top: 8px;
  font-variant-numeric: tabular-nums;
}
.ot-cost-label {
  display: block;
  font-size: 10px;
  font-weight: 600;
  color: var(--gray-500);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin-bottom: 2px;
}
.ot-cost-was { color: var(--gray-600); font-size: 13px; }
.ot-cost-now .pill { font-size: 12px; }
.ot-arrow { color: var(--blue-400); font-size: 15px; padding-bottom: 2px; }
.ot-pick {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  margin: 10px 8px 0 0;
  padding: 5px 10px;
  border: 1px dashed var(--blue-200);
  border-radius: 6px;
  background: #f4faff;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--blue-800);
}
.ot-pick-orig { font-weight: 400; color: var(--gray-600); font-size: 11.5px; }
.ot-none { color: var(--gray-500); font-size: 13px; padding: 8px 0; }
@media (max-width: 720px) {
  .ot-sides { grid-template-columns: 1fr; }
  .ot-side + .ot-side { border-left: none; border-top: 1px solid var(--gray-200); }
  .ot-head { flex-direction: column; gap: 2px; }
}
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

  /* Pick numbering from the 2026 lottery order (linear draft). Hypothetical
     picks added in the trade builder carry the SENDER's slug, so they
     number as the sender's own slot — a stated simplification (the sender
     could in principle deal a pick they themselves acquired). */
  const DPOS = D.draft_pos || {};
  const pickNumTA = (r, o) => { const p = DPOS[o]; return p ? r + '.' + String(p).padStart(2, '0') : null; };

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

  /* DRC-grouped inventory board (Pete's ruling 2026-08-19): NO simulated
     seating. Each round row shows the picks held there (native + acquired,
     numbered) and every roster player whose post-trade DRC equals that
     round — stacked when several share it. Arrangement work (slides,
     up-moves) lives in the Keeper board tab; this view states facts. */
  /* Chasm-only count (Pete's ruling 2026-08-19): flag ONLY structural
     impossibility, never "roster bigger than pick pool." Per DRC group:
     reachable seats = every pick at an earlier round (up-moves are always
     legal) + the slide chain from the native round (consecutive HELD
     rounds; a missing round is the wall). Group size beyond that bound is
     chasm-blocked no matter which keepers the manager picks. The bound
     ignores competition between groups, so it only reports certainties. */
  function chasmCount(picks, rosterArr) {
    const heldBy = {}; picks.forEach(pk => { heldBy[pk.r] = (heldBy[pk.r] || 0) + 1; });
    const groups = {}; rosterArr.forEach(p => { const d = clampDrc(p.eff); groups[d] = (groups[d] || 0) + 1; });
    let blocked = 0;
    Object.keys(groups).forEach(k => {
      const d = +k, n = groups[d];
      let above = 0; for (let r = 1; r < d; r++) above += heldBy[r] || 0;
      let chain = 0;
      if (heldBy[d]) { let r = d; while (r <= 16 && heldBy[r]) { chain += heldBy[r]; r++; } }
      const cap = above + chain;
      if (n > cap) blocked += n - cap;
    });
    return blocked;
  }

  /* Exact "how many can be kept at all" via augmenting-path matching:
     players -> pick slots. A player of DRC d may use any pick at round
     q <= d (native or up-move) or, below native, a pick reachable through
     CONSECUTIVE held rounds from d (the slide chain). Roster minus this
     matching = the total that can't be kept, whatever the manager picks. */
  function fitCount(picks, rosterArr) {
    const seats = picks.map(pk => pk.r);
    const heldRounds = {}; seats.forEach(r => { heldRounds[r] = 1; });
    const legal = (d, q) => {
      if (q <= d) return true;
      for (let r = d; r <= q; r++) if (!heldRounds[r]) return false;
      return true;
    };
    const ds = rosterArr.map(p => clampDrc(p.eff));
    const seatOf = new Array(seats.length).fill(-1);
    function tryPlace(i, seen) {
      for (let s = 0; s < seats.length; s++) {
        if (seen[s] || !legal(ds[i], seats[s])) continue;
        seen[s] = true;
        if (seatOf[s] === -1 || tryPlace(seatOf[s], seen)) { seatOf[s] = i; return true; }
      }
      return false;
    }
    let fit = 0;
    for (let i = 0; i < ds.length; i++) if (tryPlace(i, new Array(seats.length).fill(false))) fit++;
    return fit;
  }

  function groupModel(slug, picks, rosterArr, picksLost) {
    const lostBy = {}; (picksLost || []).forEach(l => (lostBy[l.r] = lostBy[l.r] || []).push(l));
    const rows = [];
    for (let r = 1; r <= 16; r++) {
      const held = picks.filter(pk => pk.r === r).map(pk => ({
        own: pk.o === slug,
        r: r,
        o: pk.o,
        num: pickNumTA(r, pk.o),
        hypo: !!pk.hypo,
        hidx: pk.hidx,
        lp: !!pk.lp,
        acqFrom: pk.o === slug ? null : first((teamBy[pk.o] || {}).mgr || pk.o)
      })).sort((a, b) => ((a.num || 'zz') < (b.num || 'zz') ? -1 : 1));
      const players = rosterArr.filter(p => clampDrc(p.eff) === r)
        .sort((a, b) => (b.pts || 0) - (a.pts || 0));
      rows.push({r, held, players, goneTo: (lostBy[r] || []).map(l => ({
        to: first((teamBy[l.to] || {}).mgr || l.to),
        num: pickNumTA(r, l.o != null ? l.o : slug),
        tradeIdx: l.tradeIdx
      }))});
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
      // Exact pick when the send came from a pill click (pk.o known);
      // otherwise the old preference: spend an acquired copy first.
      let idx = pk.o != null ? postPicks.findIndex(c => c.r === pk.r && c.o === pk.o) : -1;
      if (idx < 0) idx = postPicks.findIndex(c => c.r === pk.r && c.o !== T);
      if (idx < 0) idx = postPicks.findIndex(c => c.r === pk.r);
      if (idx >= 0) postPicks.splice(idx, 1);
    });
    picksRecv.map((pk, i) => ({pk, i})).filter(x => x.pk.y === Y0)
      .forEach(x => postPicks.push({r: x.pk.r, o: x.pk.o != null ? x.pk.o : O, hypo: 1, hidx: x.i}));
    return {
      team, sends: sendPlayers, receives: recvPlayers, picksSent, picksRecv,
      capBefore: team.cap, capAfter, delta: capAfter - team.cap,
      ptsSwing: ptsIn - ptsOut, commit3yr, commitByYear,
      chasmN: chasmCount(postPicks, postRosterArr),
      rosterN: postRosterArr.length,
      cantKeepN: Math.max(0, postRosterArr.length - fitCount(postPicks, postRosterArr)),
      /* Board shows sent players IN PLACE, grayed with an OUT tag, so you
         see where the trade pulls from; math above already excludes them.
         OUT cards display the FROZEN trade-time DRC (2025 anchor), same as
         the IN card on the other board — in a trade the decrement pauses,
         so the number stays flat across both sides (Pete, 2026-08-19). */
      boardPost: groupModel(T, postPicks,
        postRosterArr.concat(sendPlayers.map(p => ({i: p.i, n: p.n, pos: p.p, eff: clampDrc(anchor(p)), pts: p.pts, outgoing: true}))),
        (D.picks_lost[T] || []).concat(picksSent.map((pk, i) => ({pk, i})).filter(x => x.pk.y === Y0)
          .map(x => ({r: x.pk.r, to: O, tradeIdx: x.i, o: x.pk.o})))),
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

  function taPickChip(h, side) {
    const num = h.num ? '<b style="font-variant-numeric:tabular-nums;">' + h.num + '</b>' : (h.lp ? '<b>last pick</b>' : '<b>R?</b>');
    if (h.hypo) {
      // Incoming via this trade: click removes it (mirrors the sender's send list)
      const other = side === 'L' ? 'R' : 'L';
      return '<span class="ta-pill" data-act="rm' + other + '" data-idx="' + h.hidx + '" title="Remove from trade" ' +
        'style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border:1px solid #bfe3cf;border-radius:6px;font-size:10.5px;color:#1c7a4a;background:#f2fbf5;font-weight:600;">' +
        num + (h.acqFrom ? ' from ' + esc(h.acqFrom) : ' acquired') + ' &middot; this trade &times;</span>';
    }
    if (h.own) {
      return '<span class="ta-pill" data-addpick="1" data-side="' + side + '" data-r="' + h.r + '" data-o="' + esc(h.o) + '" title="Add to trade" ' +
        'style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border:1px solid #e0e0e3;border-radius:6px;font-size:10.5px;color:#606C71;background:#f7f8fa;">' + num + ' your pick</span>';
    }
    return '<span class="ta-pill" data-addpick="1" data-side="' + side + '" data-r="' + h.r + '" data-o="' + esc(h.o) + '" title="Add to trade" ' +
      'style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border:1px dashed #bfe3cf;border-radius:6px;font-size:10.5px;color:#1c7a4a;background:#f2fbf5;font-weight:600;">' +
      num + (h.acqFrom ? ' from ' + esc(h.acqFrom) : ' acquired') + '</span>';
  }

  function taPlayerCard(p, recvIds) {
    const incoming = recvIds.has(p.i) && !p.outgoing, tier = tierOf(p.eff);
    const chip = 'display:flex;align-items:center;gap:7px;padding:6px 10px;border-radius:7px;cursor:pointer;font-size:12.5px;border:1px solid ' +
      (incoming ? '#bfe3cf' : '#e5e5e8') + ';background:' + (incoming ? '#e6f6ee' : (p.outgoing ? '#f4f4f6' : '#fff')) +
      (p.outgoing ? ';opacity:.55;' : ';');
    const tag = incoming ? '<span style="color:#1c7a4a;font-weight:700;font-size:9.5px;letter-spacing:.04em;flex:none;">IN</span>'
      : (p.outgoing ? '<span style="color:#b06a60;font-weight:700;font-size:9.5px;letter-spacing:.04em;flex:none;">OUT</span>' : '');
    return '<div data-toggle="' + p.i + '" style="' + chip + '">' +
      '<span style="width:7px;height:7px;border-radius:50%;flex:none;background:' + dotColor(tier) + ';"></span>' +
      '<span style="font-weight:600;color:#2a2a2e;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(p.n) + '</span>' +
      '<span style="color:#a0a0a6;font-size:11px;flex:none;">' + esc(p.pos) + '</span>' +
      '<span style="flex:1;"></span>' + tag +
      '<span style="' + drcChipStyle(tier) + '">' + p.eff + '</span>' +
      '<span style="font-weight:700;color:#022479;font-size:12px;flex:none;font-variant-numeric:tabular-nums;">' + money($$(p.eff)) + '</span>' +
      '</div>';
  }

  function boardHTML(sideVM, side, active) {
    const recvIds = new Set(sideVM.receives.map(p => p.i));
    const chasmN = sideVM.chasmN;
    const d = sideVM.delta;
    const capStr = active ? ('cap ' + money(sideVM.capBefore) + ' → ' + money(sideVM.capAfter)) : ('cap ' + money(sideVM.capBefore));
    const deltaStr = active ? (d > 0 ? '▲ +$' + Math.abs(d).toLocaleString() : d < 0 ? '▼ −$' + Math.abs(d).toLocaleString() : '±0') : '';
    const deltaStyle = d > 0 ? 'color:#b42318;font-weight:700;' : d < 0 ? 'color:#1c7a4a;font-weight:700;' : 'color:#606C71;';
    let rows = '';
    sideVM.boardPost.forEach(row => {
      let strip = row.held.map(h => taPickChip(h, side)).join('');
      row.goneTo.forEach(g => {
        const clickable = g.tradeIdx != null;
        strip += '<span' + (clickable ? ' class="ta-pill" data-act="rm' + side + '" data-idx="' + g.tradeIdx + '" title="Remove from trade"' : '') +
          ' style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border:1px dashed #e3c4be;border-radius:6px;font-size:10.5px;color:#b06a60;background:#fdf4f2;">' +
          (g.num ? '<b style="font-variant-numeric:tabular-nums;">' + g.num + '</b> ' : '') + 'traded to ' + esc(g.to) + (clickable ? ' &times;' : '') + '</span>';
      });
      if (!strip) strip = '<span style="display:inline-flex;align-items:center;padding:2px 8px;border:1px dashed #e0e0e3;border-radius:6px;font-size:10.5px;color:#b8b8bc;">no pick</span>';
      const cards = row.players.map(p => taPlayerCard(p, recvIds)).join('');
      rows += '<div style="display:flex;align-items:flex-start;gap:8px;padding:3px 0;border-bottom:1px solid #f4f4f6;">' +
        '<div style="width:30px;flex:none;text-align:center;font-size:11px;font-weight:700;color:#909096;font-variant-numeric:tabular-nums;padding-top:4px;">R' + row.r + '</div>' +
        '<div style="flex:1;display:flex;flex-direction:column;gap:4px;min-width:0;">' +
        '<div style="display:flex;flex-wrap:wrap;gap:4px;">' + strip + '</div>' + cards + '</div></div>';
    });
    const picksArr = side === 'L' ? st.picksL : st.picksR;
    const dyv = side === 'L' ? st.dyL : st.dyR, drv = side === 'L' ? st.drL : st.drR;
    const yearOpts = [Y0, Y0 + 1].map(y => '<option value="' + y + '"' + (y === dyv ? ' selected' : '') + '>' + y + '</option>').join('');
    const roundOpts = Array.from({length: 16}, (_, i) => '<option value="' + (i + 1) + '"' + ((i + 1) === drv ? ' selected' : '') + '>R' + (i + 1) + '</option>').join('');
    const pickChips = picksArr.map((pk, i) => '<span style="display:inline-flex;align-items:center;gap:4px;background:#e6f6ee;color:#1c7a4a;border-radius:5px;padding:3px 6px;font-size:11.5px;font-weight:600;">' +
      pk.y + ' ' + (pk.o != null && pickNumTA(pk.r, pk.o) ? pickNumTA(pk.r, pk.o) : 'R' + pk.r) +
      '<button type="button" data-act="rm' + side + '" data-idx="' + i + '" style="border:none;background:none;color:#1c7a4a;cursor:pointer;font-size:13px;line-height:1;padding:0;">×</button></span>').join('');
    const footer = '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:9px 14px;border-top:1px solid #ebebed;background:#fcfcfd;">' +
      '<span style="font-size:10.5px;letter-spacing:0.06em;text-transform:uppercase;color:#8e8e93;font-weight:600;">Add pick</span>' +
      '<select data-role="dy' + side + '" style="border:1px solid #d8d8dc;border-radius:6px;padding:4px 6px;font:inherit;font-size:12px;">' + yearOpts + '</select>' +
      '<select data-role="dr' + side + '" style="border:1px solid #d8d8dc;border-radius:6px;padding:4px 6px;font:inherit;font-size:12px;">' + roundOpts + '</select>' +
      '<button type="button" data-act="add' + side + '" style="border:1px solid #022479;background:#fff;color:#022479;border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;">Add</button>' + pickChips + '</div>';
    const chasmBadge = chasmN > 0 ? '<div style="margin-top:6px;"><span style="display:inline-block;background:#fdecea;color:#b42318;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;">' +
      'chasm: ' + chasmN + (chasmN === 1 ? ' keeper can&#39;t slot' : ' keepers can&#39;t slot') + '</span></div>' : '';
    return '<div style="border:1px solid #ebebed;border-radius:12px;overflow:hidden;background:#fff;">' +
      '<div style="padding:12px 15px;background:#fcfcfd;border-bottom:1px solid #ebebed;">' +
      '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">' +
      '<span style="font-weight:700;font-size:15px;color:#022479;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(sideVM.team.mgr) + '</span>' +
      '<span style="font-size:12px;color:#606C71;font-variant-numeric:tabular-nums;white-space:nowrap;">' + capStr + ' <span style="' + deltaStyle + '">' + deltaStr + '</span></span></div>' +
      chasmBadge + '</div>' +
      '<div style="padding:10px 12px;">' + rows + '</div>' + footer + '</div>';
  }

  function trayHTML(vm) {
    if (!vm.valid || vm.empty) return '';
    const L = vm.L, R = vm.R;
    const items = (sideVM) => {
      let a = sideVM.sends.map(p => '<button type="button" data-toggle="' + p.i + '" style="display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.25);color:#fff;border-radius:6px;padding:4px 8px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;">' + esc(p.n) + ' <span style="color:#77CEFF;font-size:13px;">×</span></button>');
      sideVM.picksSent.forEach(pk => a.push('<span style="display:inline-flex;align-items:center;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.25);color:#fff;border-radius:6px;padding:4px 8px;font-size:12px;font-weight:600;white-space:nowrap;">' + pk.y + ' ' + (pk.o != null && pickNumTA(pk.r, pk.o) ? pickNumTA(pk.r, pk.o) : 'R' + pk.r) + '</span>'));
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
    const chasmN = sideVM.chasmN;
    const chasmStr = chasmN > 0 ? (chasmN + (chasmN === 1 ? ' keeper' : ' keepers') + ' chasm-blocked') : 'clean';
    const chasmStyle = chasmN > 0 ? 'color:#b42318;font-weight:700;' : 'color:#1c7a4a;';
    const cantStr = sideVM.cantKeepN + ' of ' + sideVM.rosterN + ' can&rsquo;t be kept';
    return '<div class="ta-sum-card">' +
      '<div class="ta-sum-mgr">' + esc(sideVM.team.mgr) + '</div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">2025 pts</span><span class="ta-sum-v" style="' + ptsStyle + '">' + ptsStr + '</span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Cap ' + Y0 + '</span><span class="ta-sum-v">' + capNowStr +
        '<span style="' + capDeltaStyle + 'font-weight:700;">' + capDeltaStr + '</span></span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Future commit</span><span class="ta-sum-v">' + futureStr + '</span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Can&rsquo;t keep</span><span class="ta-sum-v" style="font-weight:700;color:#2a2a2e;">' + cantStr + '</span></div>' +
      '<div class="ta-sum-row"><span class="ta-sum-k">Chasm</span><span class="ta-sum-v" style="' + chasmStyle + '">' + chasmStr + '</span></div>' +
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
    html += '<p style="font-size:11.5px;color:#8e8e93;margin:14px 2px 0;">Click a player on either board to move them across. Each round shows the picks held there (numbered by lottery order) and every player whose DRC lands in it &mdash; a stack means more keepers than picks in that round. <b style="color:#2a2a2e;">Dot</b> = keeper tier · <span style="color:#1c7a4a;font-weight:600;">green</span> = acquired or incoming · <span style="color:#b06a60;font-weight:600;">red</span> = pick traded away. The <span style="color:#b42318;font-weight:600;">chasm</span> badge counts only keepers a broken slide chain makes impossible, not roster overflow.</p>';
    if (st.warn) {
      const w = st.warn.impact;
      const names = w.names.slice(0, 3).map(esc).join(', ') + (w.names.length > 3 ? ' and ' + (w.names.length - 3) + ' more' : '');
      let body = '';
      if (w.names.length && w.heldAfter < w.names.length) {
        body += '<p style="margin:0 0 10px;color:#606C71;font-size:12.5px;">Round ' + w.r + ' currently houses <b>' + names +
          '</b> (DRC ' + w.r + '). After this trade ' + esc(w.mgr) + ' would hold ' + w.heldAfter +
          (w.heldAfter === 1 ? ' pick' : ' picks') + ' there for ' + w.names.length +
          (w.names.length === 1 ? ' player' : ' players') + '.</p>';
      }
      if (w.chasmDelta > 0) {
        body += '<p style="margin:0 0 10px;color:#b42318;font-size:12.5px;font-weight:600;">It also breaks a slide chain: ' +
          w.chasmDelta + ' more ' + (w.chasmDelta === 1 ? 'keeper becomes' : 'keepers become') + ' impossible to slot.</p>';
      }
      if (w.cantDelta > 0) {
        body += '<p style="margin:0 0 10px;color:#606C71;font-size:12.5px;">Total that can&rsquo;t be kept rises by ' + w.cantDelta + '.</p>';
      }
      html += '<div style="position:fixed;inset:0;background:rgba(15,18,25,.45);z-index:120;display:flex;align-items:center;justify-content:center;padding:16px;">' +
        '<div class="ta-warn-card" style="background:#fff;border-radius:14px;max-width:440px;width:100%;padding:18px 20px;box-shadow:0 12px 40px rgba(0,0,0,.25);">' +
        '<h3 style="margin:0 0 8px;font-size:15px;">Send the R' + w.r + ' pick?</h3>' + body +
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px;">' +
        '<button type="button" data-act="warnno" style="font:inherit;font-size:12.5px;font-weight:600;border:1px solid #d6d9e0;background:#fff;border-radius:8px;padding:7px 14px;cursor:pointer;">Keep the pick</button>' +
        '<button type="button" data-act="warnok" style="font:inherit;font-size:12.5px;font-weight:700;border:1px solid #022479;background:#022479;color:#fff;border-radius:8px;padding:7px 14px;cursor:pointer;">Send it anyway</button>' +
        '</div></div></div>';
    }
    app.innerHTML = html;
  }

  const st = {
    teamL: '', teamR: '',
    sel: {}, picksL: [], picksR: [],
    dyL: Y0, drL: 1, dyR: Y0, drR: 1,
    warn: null,
  };
  const app = root.querySelector('.ta-app');

  function commitAdd(side, pend) {
    const entry = pend || (side === 'L' ? {y: st.dyL, r: st.drL} : {y: st.dyR, r: st.drR});
    if (side === 'L') st.picksL.push(entry); else st.picksR.push(entry);
  }

  /* "This pick currently houses a player" check (Pete's ruling 2026-08-19):
     adding a Y0 pick to the send side warns, before committing, when the
     round still houses players' native DRC or the removal breaks a slide
     chain (chasm count rises). Facts + an are-you-sure; never a block. */
  function sendImpact(side, pend) {
    const entry = pend || (side === 'L' ? {y: st.dyL, r: st.drL} : {y: st.dyR, r: st.drR});
    const y = entry.y, r = entry.r;
    if (y !== Y0 || !st.teamL || !st.teamR || st.teamL === st.teamR) return null;
    const cfgNow = {L: st.teamL, R: st.teamR, sel: st.sel, picksL: st.picksL.slice(), picksR: st.picksR.slice()};
    const cfgAfter = {L: st.teamL, R: st.teamR, sel: st.sel,
      picksL: side === 'L' ? st.picksL.concat([entry]) : st.picksL.slice(),
      picksR: side === 'R' ? st.picksR.concat([entry]) : st.picksR.slice()};
    const vmNow = computeTrade(cfgNow), vmAfter = computeTrade(cfgAfter);
    if (!vmNow.valid || !vmAfter.valid) return null;
    const now = side === 'L' ? vmNow.L : vmNow.R, aft = side === 'L' ? vmAfter.L : vmAfter.R;
    const rowNow = now.boardPost.find(x => x.r === r), rowAft = aft.boardPost.find(x => x.r === r);
    const groupNames = rowNow ? rowNow.players.filter(p => !p.outgoing).map(p => p.n) : [];
    const heldAfter = rowAft ? rowAft.held.length : 0;
    const housing = groupNames.length > 0 && heldAfter < groupNames.length;
    const chasmDelta = aft.chasmN - now.chasmN;
    const cantDelta = aft.cantKeepN - now.cantKeepN;
    if (!housing && chasmDelta <= 0) return null;
    return {r, names: groupNames, heldAfter, chasmDelta, cantDelta, mgr: first(now.team.mgr)};
  }

  app.addEventListener('click', (e) => {
    if (st.warn) {
      const w = e.target.closest('[data-act="warnok"], [data-act="warnno"]');
      if (w) {
        if (w.dataset.act === 'warnok') commitAdd(st.warn.side, st.warn.pend);
        st.warn = null; render(); return;
      }
      if (!e.target.closest('.ta-warn-card')) { st.warn = null; render(); }
      return;
    }
    const tog = e.target.closest('[data-toggle]');
    if (tog) { const pid = +tog.dataset.toggle; if (st.sel[pid]) delete st.sel[pid]; else st.sel[pid] = true; render(); return; }
    const pill = e.target.closest('[data-addpick]');
    if (pill) {
      const side = pill.dataset.side;
      const pend = {y: Y0, r: +pill.dataset.r, o: pill.dataset.o};
      const impact = sendImpact(side, pend);
      if (impact) { st.warn = {side, pend, impact}; render(); return; }
      commitAdd(side, pend); render(); return;
    }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const a = act.dataset.act;
    if (a === 'addL' || a === 'addR') {
      const side = a === 'addL' ? 'L' : 'R';
      const impact = sendImpact(side);
      if (impact) { st.warn = {side, impact}; render(); return; }
      commitAdd(side);
    }
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
  /* 2025 positional finish, e.g. "WR4" — pr comes from player_history's
     pos_rank (rank by total 2025 points within position). Null for
     rookies / players with no 2025 stats. Requested by Dan MacNulty:
     the printout should show position + prior-year finish. */
  const finish25 = p => (p && p.pr != null && p.p)
    ? String(p.p).replace(/&/g, '&amp;').replace(/</g, '&lt;') + p.pr : '&mdash;';
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const money = n => '$' + n.toLocaleString();
  const teamBy = {}; D.teams.forEach(t => teamBy[t.slug] = t);
  const playersBy = {}; D.players.forEach(p => { (playersBy[p.m] = playersBy[p.m] || []).push(p); });

  /* Pick numbering from the 2026 lottery order (linear draft: a round-N
     pick from the team drafting 2nd is N.02, wherever it lives now). */
  const DPOS = D.draft_pos || {};
  const pickNumOf = sl => {
    if (sl.lp) return null;                    // "last pick" notation, no fixed slot
    const p = DPOS[sl.o];
    return p ? sl.r + '.' + String(p).padStart(2, '0') : null;
  };
  const pickChip = sl => {
    const n = pickNumOf(sl);
    let out = n ? '<span class="kb-picknum">' + n + '</span>' : '';
    if (sl.lp) out += '<span class="kb-flag">last pick</span>';
    return out;
  };
  /* st.keep[pid] = keep-order sequence number (1, 2, 3, ...), not a bare
     boolean: auto-seating is FIRST-COME, so whoever was kept earlier holds
     his native pick and a later same-DRC keep goes to Awaiting placement
     instead of stealing the chair (Pete's ruling, 2026-08-19). */
  const st = { team: '', keep: {}, manual: {}, pick: null, seq: 0, pickFor: null };

  function mkSlots() {
    return (D.picks[st.team] || []).map((pk, i) =>
      ({ id: i, r: pk.r, o: pk.o, lp: pk.lp, own: pk.o === st.team, taken: null, manual: false }));
  }
  function roster() {
    return (playersBy[st.team] || []).slice().sort((a, b) => (a.d6 - b.d6) || ((b.pts || 0) - (a.pts || 0)));
  }
  function keepers() { return roster().filter(p => st.keep[p.i]); }

  /* SLOT-level legality for placing a player (league rules, Pete 2026-08-19):
     - Native DRC round or any earlier held round: any FREE slot, own or
       acquired, is a legal conscious choice.
     - BELOW native: only the ACTIVATED SLIDE DESTINATION — native must be
       held and full, every round walking down must be held and full, and
       the first held round with a free slot is the (only) legal landing.
       A held-but-unfilled gap means you'd land there instead; an unheld
       round is the chasm wall. Ownership doesn't matter at the
       destination (this is how an acquired 16th can catch a DRC-15
       overflow), but you can never voluntarily park below native without
       the collision chain above. */
  function legalSlotIds(d, slots) {
    const byR = {}; slots.forEach(s => { (byR[s.r] = byR[s.r] || []).push(s); });
    const ok = {};
    for (let r = d; r >= 1; r--) (byR[r] || []).forEach(s => { if (!s.taken) ok[s.id] = 1; });
    const native = byR[d] || [];
    if (native.length && !native.some(s => !s.taken)) {
      let r = d + 1;
      while (r <= 16) {
        const here = byR[r] || [];
        if (!here.length) break;                       // chasm wall
        const free = here.filter(s => !s.taken);
        if (free.length) { free.forEach(s => { ok[s.id] = 1; }); break; }
        r++;
      }
    }
    return ok;
  }

  /* Sim (Pete's conscious-placement model, 2026-08-19):
     - Auto-seat does ONE thing: a keeper's own native pick, if free.
     - Everything else is a conscious manual placement (drag or tap),
       validated against legalSlotIds.
     - Kept players with no seat split into "unplaced" (legal spots exist,
       manager must choose) and "chasm" (no legal spot at all).
     Three passes so a below-native slide placement can depend on the
     native-round collision that auto/native placements create. */
  function kbSim(excludePid) {
    const slots = mkSlots();
    const placed = {};
    const ks = keepers().filter(p => p.i !== excludePid);
    const byId = {}; slots.forEach(s => { byId[s.id] = s; });
    const badManual = [];
    // Pass 1: manual placements at native-or-earlier (always legal if free)
    ks.forEach(p => {
      const sid = st.manual[p.i];
      if (sid == null) return;
      const sl = byId[sid];
      if (sl && !sl.taken && sl.r <= clampDrc(p.d6)) {
        sl.taken = p; sl.manual = true; placed[p.i] = { slot: sl, manual: true };
      }
    });
    // Pass 2: auto-seat checkbox keeps in KEEP ORDER (first-come). Own
    // native pick if free; otherwise SLIDE down the consecutive-held chain
    // to the first free OWN pick — league physics, per Brian via Pete
    // 2026-08-19. Acquired picks stay protected: they keep the chain alive
    // as held rounds but are never auto-consumed. Chain wall (unheld
    // round) or no own seat -> Awaiting placement (up-moves and acquired
    // picks remain conscious choices).
    const byR2 = {}; slots.forEach(s => { (byR2[s.r] = byR2[s.r] || []).push(s); });
    ks.filter(p => !placed[p.i] && st.manual[p.i] == null)
      .sort((a, b) => (st.keep[a.i] || 0) - (st.keep[b.i] || 0))
      .forEach(p => {
        const d = clampDrc(p.d6);
        let seat = slots.find(s => s.r === d && s.own && !s.taken);
        if (!seat && (byR2[d] || []).length) {
          for (let r = d + 1; r <= 16; r++) {
            const here = byR2[r] || [];
            if (!here.length) break;                       // chasm wall
            const f = here.find(s => s.own && !s.taken);
            if (f) { seat = f; break; }
          }
        }
        if (seat) { seat.taken = p; placed[p.i] = { slot: seat, manual: false }; }
      });
    // Pass 3: below-native manuals — legal only as the activated slide destination
    ks.forEach(p => {
      const sid = st.manual[p.i];
      if (sid == null || placed[p.i]) return;
      const sl = byId[sid];
      if (sl && !sl.taken && legalSlotIds(clampDrc(p.d6), slots)[sl.id]) {
        sl.taken = p; sl.manual = true; placed[p.i] = { slot: sl, manual: true };
      } else badManual.push(p.i);
    });
    badManual.forEach(pid => delete st.manual[pid]);
    // A dropped manual usually means its collision vanished (e.g. the
    // native-round occupant was unkept) — the slide unwinds, so fall the
    // player back to auto seating: own native pick, else the own-pick
    // slide chain (same rule as pass 2).
    badManual.forEach(pid => {
      const p = ks.find(x => x.i === pid);
      if (!p || placed[p.i]) return;
      const d = clampDrc(p.d6);
      let seat = slots.find(s => s.r === d && s.own && !s.taken);
      if (!seat && (byR2[d] || []).length) {
        for (let r = d + 1; r <= 16; r++) {
          const here = byR2[r] || [];
          if (!here.length) break;
          const f = here.find(s => s.own && !s.taken);
          if (f) { seat = f; break; }
        }
      }
      if (seat) { seat.taken = p; placed[p.i] = { slot: seat, manual: false }; }
    });
    const unplaced = [], chasm = [];
    ks.filter(p => !placed[p.i]).forEach(p => {
      const ok = legalSlotIds(clampDrc(p.d6), slots);
      (Object.keys(ok).length ? unplaced : chasm).push(p);
    });
    return { slots, placed, unplaced, chasm };
  }

  function capTotal() { return keepers().reduce((s, p) => s + (p.c6 || 0), 0); }

  /* ---- Lineup preview: keepers arranged in Yahoo's slot layout ---------
     QB RB RB WR WR WR TE W/R/T Q/W/R/T K DEF + 5 BN + 2 IR (the 2026
     shape, bench trimmed to 5). Dedicated slots fill first by BEST 2026
     ADP (lowest number = drafted earliest; Pete's ruling 2026-08-26 —
     Lamar takes QB, Darnold flows to the superflex unless his ADP is
     better), then W/R/T, then the superflex, then bench. Players with no
     2026 ADP sort last within their position, tiebreak 2025 points. IR
     never fills from keepers — you can't keep a player INTO an IR slot.
     Roster construction only; seating legality lives on the board above. */
  function lineupAssign() {
    const adpVal = p => (p.adp != null ? p.adp : 1e6);
    const better = (a, b) => adpVal(a) - adpVal(b) || (b.pts || 0) - (a.pts || 0);
    const pool = { QB: [], RB: [], WR: [], TE: [], K: [], DEF: [] };
    const other = [];
    keepers().slice().sort(better)
      .forEach(p => { (pool[p.p] || other).push(p); });
    const take = pos => pool[pos].length ? pool[pos].shift() : null;
    const takeBest = poss => {
      let bestPos = null;
      poss.forEach(pos => {
        const c = pool[pos][0];
        if (c && (bestPos == null || better(c, pool[bestPos][0]) < 0)) bestPos = pos;
      });
      return bestPos ? pool[bestPos].shift() : null;
    };
    const starters = [
      { lbl: 'QB', p: take('QB') },
      { lbl: 'RB', p: take('RB') },
      { lbl: 'RB', p: take('RB') },
      { lbl: 'WR', p: take('WR') },
      { lbl: 'WR', p: take('WR') },
      { lbl: 'WR', p: take('WR') },
      { lbl: 'TE', p: take('TE') },
      { lbl: 'W/R/T', p: takeBest(['WR', 'RB', 'TE']) },
      { lbl: 'Q/W/R/T', p: takeBest(['QB', 'WR', 'RB', 'TE']) },
      { lbl: 'K', p: take('K') },
      { lbl: 'DEF', p: take('DEF') },
    ];
    const rest = [].concat(pool.QB, pool.RB, pool.WR, pool.TE, pool.K, pool.DEF, other)
      .sort(better);
    const bench = [];
    for (let i = 0; i < 5; i++) bench.push({ lbl: 'BN', p: rest[i] || null });
    return { starters, bench, overflow: rest.slice(5) };
  }

  function luRow(s, extraCls) {
    const p = s.p;
    let body;
    if (p) {
      body = '<span class="kb-lu-nm">' + esc(p.n) +
        ' <span class="kb-lu-sub">' + esc(p.p || '') + (p.t ? ' &middot; ' + esc(p.t) : '') + '</span></span>' +
        '<span class="kb-lu-meta">' + (p.pr != null ? finish25(p) + ' in 2025 &middot; ' : '') +
        'DRC ' + p.d6 + ' &middot; ' + money(p.c6) + '</span>';
    } else {
      body = '<span class="kb-lu-open">' + (s.lbl === 'IR' ? 'empty &mdash; keepers can&#39;t fill IR' : 'open &mdash; filled at the draft') + '</span>';
    }
    return '<div class="kb-lu-row' + (p ? '' : ' kb-lu-empty') + (extraCls || '') + '">' +
      '<span class="kb-lu-slot' + (s.lbl === 'BN' || s.lbl === 'IR' ? ' kb-lu-slot-bn' : '') + '">' + s.lbl + '</span>' + body + '</div>';
  }

  function lineupPrint() {
    if (!keepers().length) return '';
    const lu = lineupAssign();
    const row = s => {
      const p = s.p;
      return '<tr' + (p ? '' : ' class="kb-print-open"') + '><td class="num" style="font-weight:700;color:#606C71;">' + s.lbl + '</td>' +
        (p ? '<td class="kb-print-name">' + esc(p.n) + '</td><td>' + esc(p.p || '') + '</td><td>' + finish25(p) +
             '</td><td class="num">' + p.d6 + '</td><td class="num">' + money(p.c6) + '</td>'
           : '<td colspan="5" class="kb-print-note">' + (s.lbl === 'IR' ? 'empty' : 'open &mdash; filled at the draft') + '</td>') + '</tr>';
    };
    return '<h3 class="kb-print-chasm-h" style="color:#022479;">Lineup preview &mdash; keepers in Yahoo&#39;s slot layout</h3>' +
      '<table><thead><tr><th style="width:64px;">Slot</th><th>Player</th><th>Pos</th><th>2025 finish</th><th>DRC</th><th>$</th></tr></thead><tbody>' +
      lu.starters.map(row).join('') + lu.bench.map(row).join('') +
      lu.overflow.map(p => row({ lbl: 'BN*', p })).join('') +
      '<tr class="kb-print-open"><td class="num" style="font-weight:700;color:#606C71;">IR</td><td colspan="5" class="kb-print-note">empty</td></tr>'.repeat(2) +
      '</tbody></table>';
  }

  function lostRows() {
    const lostBy = {};
    (D.picks_lost[st.team] || []).forEach(l => (lostBy[l.r] = lostBy[l.r] || []).push(l));
    return lostBy;
  }

  /* Slot-first placement (Pete's mobile flow, 2026-08-19): tap an empty
     pick to list every player who may legally take it right now — natives
     first, then up-movers, slide-landers only when the chain is active.
     Selecting an unkept player keeps AND places; a seated player moves. */
  function eligibleFor(slotId) {
    const out = [];
    const cur = kbSim();
    roster().forEach(p => {
      const sim0 = kbSim(p.i);
      const target = sim0.slots.find(s => s.id === slotId);
      if (!target) return;
      const ok = legalSlotIds(clampDrc(p.d6), sim0.slots);
      if (!ok[slotId]) return;
      let status = 'not kept yet';
      if (st.keep[p.i]) {
        const seat = cur.placed[p.i];
        status = seat ? ('moves from ' + (pickNumOf(seat.slot) || 'R' + seat.slot.r)) : 'awaiting placement';
      }
      out.push({ p, d: clampDrc(p.d6), native: clampDrc(p.d6) === target.r, status });
    });
    out.sort((a, b) => (b.native - a.native) || (a.d - b.d) || ((b.p.pts || 0) - (a.p.pts || 0)));
    return out;
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
      if (p) legal = legalSlotIds(clampDrc(p.d6), sim.slots);
    }

    const cards = roster().map(p => {
      const on = !!st.keep[p.i];
      const isPick = st.pick === p.i;
      const chasm = on && sim.chasm.some(u => u.i === p.i);
      const waiting = on && sim.unplaced.some(u => u.i === p.i);
      const meta = 'DRC ' + p.d6 + ' &middot; ' + money(p.c6) +
                   (p.pts != null ? ' &middot; ' + p.pts + ' pts' : '') +
                   (waiting ? ' &middot; needs a slot' : '');
      return '<div class="kb-card' + (on ? ' kb-on' : '') + (isPick ? ' kb-picked' : '') +
        (chasm ? ' kb-chasm-card' : '') + (waiting ? ' kb-unplaced' : '') + '" data-pid="' + p.i + '" draggable="true">' +
        '<span class="kb-check" data-role="check">' + (on ? '&#10003;' : '') + '</span>' +
        '<span class="kb-nm">' + esc(p.n) + ' <span style="color:#606C71;font-weight:400;">' +
        esc(p.p || '') + '</span></span>' +
        '<span class="kb-meta">' + meta + '</span></div>';
    }).join('');

    const lostBy = lostRows();
    function slotCell(sl) {
      const cls = ['kb-slot'];
      if (!sl.own) cls.push('kb-acq');
      if (legal) cls.push(legal[sl.id] ? 'kb-legal' : 'kb-illegal');
      let inner = pickChip(sl);
      if (sl.taken) {
        /* Seated: player, DRC and $ own the row; every provenance note
           drops to one quiet text line below so nothing crowds the name. */
        const p = sl.taken;
        const notes = [];
        if (sl.manual) notes.push('placed by you');
        if (!sl.own) {
          const from = ((teamBy[sl.o] || {}).mgr || '').split(' ')[0];
          notes.push(from ? 'via ' + esc(from) + '&#39;s pick' : 'via acquired pick');
        }
        if (sl.r > clampDrc(p.d6)) notes.push('slid down from R' + clampDrc(p.d6));
        if (sl.r < clampDrc(p.d6)) notes.push('earlier than needed &middot; frees R' + clampDrc(p.d6));
        inner += '<span class="kb-seated">' + esc(p.n) + '</span>' +
          '<span class="kb-meta">DRC ' + p.d6 + ' &middot; ' + money(p.c6) + '</span>' +
          '<button class="kb-x" data-unseat="' + p.i + '" title="Remove from keepers" type="button">&#10005;</button>' +
          (notes.length ? '<span class="kb-slot-notes">' + notes.join(' &middot; ') + '</span>' : '');
        /* The WHOLE slot is the drag handle for a seated player — a full
           row grabs reliably where a clipped name span does not. */
        return '<div class="' + cls.join(' ') + '" data-slot="' + sl.id + '" draggable="true" data-dragpid="' + p.i +
          '" title="Drag to another lit slot to move">' + inner + '</div>';
      } else {
        cls.push('kb-open');
        if (!sl.own) inner += '<span class="kb-origin">acq &middot; ' +
          esc(((teamBy[sl.o] || {}).mgr || '').split(' ')[0]) + '</span>';
        if (!legal) inner += '<span class="kb-open-hint">open &middot; tap to fill</span>';
      }
      return '<div class="' + cls.join(' ') + '" data-slot="' + sl.id + '">' + inner + '</div>';
    }
    /* One row per pick. Multi-pick rounds stack: the first row carries the
       bold round number, continuation rows repeat it muted so the round is
       never ambiguous. Traded-away picks get their own red row, numbered. */
    let rows = '';
    for (let r = 1; r <= 16; r++) {
      const sub = [];
      sim.slots.filter(s => s.r === r).forEach(sl =>
        sub.push({ pos: sl.lp ? 98 : (DPOS[sl.o] || 97), gone: false, cell: slotCell(sl) }));
      (lostBy[r] || []).forEach(l => {
        const mine = { r: r, o: st.team };
        const n = pickNumOf(mine);
        sub.push({ pos: DPOS[st.team] || 97, gone: true,
          cell: '<div class="kb-slot kb-gone-slot">' +
            (n ? '<span class="kb-picknum kb-picknum-gone">' + n + '</span>' : '') +
            '<span class="kb-goneto">traded to ' + esc((teamBy[l.to] || {}).mgr || l.to) + '</span></div>' });
      });
      if (!sub.length) sub.push({ pos: 99, gone: true,
        cell: '<div class="kb-slot kb-gone-slot"><span class="kb-goneto">no pick this round</span></div>' });
      sub.sort((a, b) => a.pos - b.pos);
      sub.forEach((row, i) => {
        rows += '<div class="kb-row' + (row.gone ? ' kb-gone' : '') + (i > 0 ? ' kb-row-cont' : '') + '">' +
          '<span class="kb-rnum' + (i > 0 ? ' kb-rnum-cont' : '') + '">' + r + '</span>' + row.cell + '</div>';
      });
    }
    let chasmStrip = '';
    if (sim.unplaced.length) {
      chasmStrip += '<div class="kb-chasm-strip"><strong style="color:#8a6a12;font-size:12px;">Awaiting placement:</strong> ' +
        sim.unplaced.map(p => '<span class="kb-wait-chip">' + esc(p.n) + ' (DRC ' + p.d6 + ')</span>').join('') +
        '<div class="kb-hint">Their own native pick is taken. Tap or drag each one to a lit slot: their DRC round or better, or the slide landing below a full native round.</div></div>';
    }
    if (sim.chasm.length) {
      chasmStrip += '<div class="kb-chasm-strip"><strong style="color:#b42318;font-size:12px;">Can&#39;t slot (chasm):</strong> ' +
        sim.chasm.map(p => '<span class="kb-chasm-chip">' + esc(p.n) + ' (DRC ' + p.d6 + ')</span>').join('') +
        '<div class="kb-hint">No legal slot exists for them on this board. Free a round or trade for a pick.</div></div>';
    }

    // Preserve the roster list's scroll position across the innerHTML
    // rebuild — otherwise clicking a player at the bottom of the list
    // jumps the user back to the top on every render.
    const prevRoster = app.querySelector('.kb-roster');
    const prevScroll = prevRoster ? prevRoster.scrollTop : 0;

    /* Slot-occupancy banner: how many of each lineup slot the current
       keeper slate already fills (Pete's request 2026-08-26). Rendered
       twice — under the top controls and again atop the lineup preview
       panel (redundant on purpose, per Pete). */
    const lu = lineupAssign();
    const slotGroups = [];
    const addGroup = (label, slots) => slotGroups.push(
      { label: label, filled: slots.filter(s => s.p).length, total: slots.length });
    addGroup('QB', lu.starters.filter(s => s.lbl === 'QB'));
    addGroup('RB', lu.starters.filter(s => s.lbl === 'RB'));
    addGroup('WR', lu.starters.filter(s => s.lbl === 'WR'));
    addGroup('TE', lu.starters.filter(s => s.lbl === 'TE'));
    addGroup('FLEX', lu.starters.filter(s => s.lbl === 'W/R/T'));
    addGroup('Q/W/R/T', lu.starters.filter(s => s.lbl === 'Q/W/R/T'));
    addGroup('K', lu.starters.filter(s => s.lbl === 'K'));
    addGroup('DEF', lu.starters.filter(s => s.lbl === 'DEF'));
    addGroup('BN', lu.bench);
    const slotChips = slotGroups.map(g =>
      '<span class="kb-slotchip' + (g.filled >= g.total ? ' kb-slot-full' : (g.filled ? ' kb-slot-part' : '')) + '">' +
      '<span class="kb-slotnum">' + g.filled + '/' + g.total + '</span> ' + g.label + '</span>').join('') +
      (lu.overflow.length ? '<span class="kb-slotchip kb-slot-over">+' + lu.overflow.length + ' over the roster</span>' : '');
    const slotBanner = '<div class="kb-slotbar">' + slotChips + '</div>';

    /* Lineup preview panel — only once at least one keeper is checked. */
    let lineupHtml = '';
    if (keepers().length) {
      const over = lu.overflow.map(p => luRow({ lbl: 'BN', p: p }, ' kb-lu-over')).join('');
      lineupHtml = '<div class="kb-panel kb-lineup"><div class="kb-panel-h">Lineup preview <span class="kb-sub">' +
        'your keepers in Yahoo&#39;s ' + D.season + ' slot layout &middot; best 2026 ADP fills each slot first &middot; open slots get filled at the draft</span></div>' +
        '<div class="kb-slotbar kb-slotbar-inpanel">' + slotChips + '</div>' +
        '<div class="kb-lu-grid"><div class="kb-lu-col"><div class="kb-lu-colh">Starters</div>' +
        lu.starters.map(s => luRow(s)).join('') + '</div>' +
        '<div class="kb-lu-col"><div class="kb-lu-colh">Bench &amp; IR</div>' +
        lu.bench.map(s => luRow(s)).join('') + over +
        luRow({ lbl: 'IR', p: null }) + luRow({ lbl: 'IR', p: null }) + '</div></div>' +
        (lu.overflow.length ? '<div class="kb-hint" style="color:#b42318;padding:0 14px 12px;">You&#39;ve kept more players than the 16 roster spots outside IR &mdash; the red bench rows don&#39;t fit.</div>' : '') +
        '</div>';
    }

    let pfHtml = '';
    if (st.pickFor != null) {
      const sl = sim.slots.find(s => s.id === st.pickFor);
      if (sl && !sl.taken) {
        const elig = eligibleFor(st.pickFor);
        const slotLabel = (pickNumOf(sl) || 'R' + sl.r) +
          (!sl.own ? ' (acquired from ' + esc(((teamBy[sl.o] || {}).mgr || '').split(' ')[0]) + ')' : '');
        const items = elig.length ? elig.map(e =>
          '<button class="kb-pf-item" data-pfpick="' + e.p.i + '" type="button">' +
          '<span class="kb-pf-nm">' + esc(e.p.n) + ' <span class="kb-pf-pos">' + esc(e.p.p || '') + '</span></span>' +
          '<span class="kb-pf-meta">DRC ' + e.p.d6 + ' &middot; ' + money(e.p.c6) + '</span>' +
          '<span class="kb-pf-tag">' + (e.native ? 'native round &middot; ' : '') + e.status + '</span>' +
          '</button>').join('')
          : '<p class="kb-pf-none">No one can legally take this pick right now. Below-native landings need the slide chain: the native round and every round between must be full.</p>';
        pfHtml = '<div class="kb-pf-overlay"><div class="kb-pf">' +
          '<h3>Who takes ' + slotLabel + '?</h3>' +
          '<div class="kb-pf-list">' + items + '</div>' +
          '<button class="kb-btn kb-pf-cancel" data-pfclose="1" type="button">Cancel</button>' +
          '</div></div>';
      } else { st.pickFor = null; }
    }

    app.innerHTML = top + slotBanner +
      '<div class="kb-cols">' +
      '<div class="kb-panel"><div class="kb-panel-h">Roster <span class="kb-sub">tap to keep (seats at your open native pick) &middot; drag anywhere lit to place by hand</span></div>' +
      '<div class="kb-roster">' + cards + '</div></div>' +
      '<div class="kb-panel"><div class="kb-panel-h">2026 draft board <span class="kb-sub">' +
      (st.pick != null ? 'lit slots are legal for the picked-up player, tap one to place' : 'one row per pick &middot; tap an open pick to fill it') +
      '</span></div><div class="kb-board">' + rows + '</div>' + chasmStrip + '</div></div>' +
      lineupHtml +
      '<p class="kb-hint">Costs come from DRC, not from the round a keeper sits in. Checkbox keeps auto-seat only on your own open native pick; anything else is your conscious call. Tap any open pick to see who can legally take it, or drag players to lit slots. Legal by hand: the native round or any earlier held pick, plus the slide landing below a native round that&#39;s full (every round between must be full too). Acquired picks are never consumed automatically. Pick numbers (10.02 = round 10, 2nd overall slot) come from the published lottery order.</p>' +
      pfHtml;

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
          '<td colspan="6" class="kb-print-note">no pick this round</td></tr>');
        continue;
      }
      here.forEach(sl => {
        const num = pickNumOf(sl);
        const numTag = num ? ' <span class="kb-print-picknum">(' + num + ')</span>' : (sl.lp ? ' <span class="kb-print-picknum">(last pick)</span>' : '');
        if (sl.taken) {
          const p = sl.taken;
          const notes = [];
          if (sl.r < clampDrc(p.d6)) notes.push('<span class="kb-print-flag">slid up from R' + clampDrc(p.d6) + '</span>');
          if (!sl.own) notes.push('<span class="kb-print-flag">via acquired pick' +
            ((teamBy[sl.o] || {}).mgr ? ' (' + esc((teamBy[sl.o] || {}).mgr.split(' ')[0]) + ')' : '') + '</span>');
          if (sl.manual) notes.push('<span class="kb-print-flag" style="color:#022479;">placed by you</span>');
          rowsHtml.push(
            '<tr><td class="num">' + r + '</td>' +
            '<td class="kb-print-name">' + esc(p.n) + numTag + '</td>' +
            '<td>' + esc(p.p || '') + '</td>' +
            '<td>' + finish25(p) + '</td>' +
            '<td class="num">' + p.d6 + '</td>' +
            '<td class="num">' + money(p.c6) + '</td>' +
            '<td class="kb-print-notes">' + notes.join(' ') + '</td></tr>');
        } else if (sl.own) {
          rowsHtml.push(
            '<tr class="kb-print-open"><td class="num">' + r + '</td>' +
            '<td colspan="6" class="kb-print-note">open &mdash; you draft here' + numTag + '</td></tr>');
        } else {
          rowsHtml.push(
            '<tr class="kb-print-open"><td class="num">' + r + '</td>' +
            '<td colspan="6" class="kb-print-note">open &mdash; acquired pick, you draft here' +
            ((teamBy[sl.o] || {}).mgr ? ' (from ' + esc((teamBy[sl.o] || {}).mgr.split(' ')[0]) + ')' : '') + numTag + '</td></tr>');
        }
      });
      gone.forEach(l => {
        const to = (teamBy[l.to] || {}).mgr || l.to;
        const num = pickNumOf({ r: r, o: st.team });
        rowsHtml.push(
          '<tr class="kb-print-gone"><td class="num">' + r + '</td>' +
          '<td colspan="6" class="kb-print-note kb-print-gone-note">traded to ' + esc(to) +
          (num ? ' <span class="kb-print-picknum">(' + num + ')</span>' : '') + '</td></tr>');
      });
    }

    let chasmBlock = '';
    if (sim.unplaced.length) {
      chasmBlock += '<h3 class="kb-print-chasm-h" style="color:#8a6a12;">Kept, awaiting placement &mdash; seat before entering in Yahoo</h3>' +
        '<table><thead><tr><th>Player</th><th>Pos</th><th>2025 finish</th><th>DRC</th><th>$</th><th>Status</th></tr></thead><tbody>' +
        sim.unplaced.map(p =>
          '<tr><td>' + esc(p.n) + '</td>' +
          '<td>' + esc(p.p || '') + '</td>' +
          '<td>' + finish25(p) + '</td>' +
          '<td class="num">' + p.d6 + '</td>' +
          '<td class="num">' + money(p.c6) + '</td>' +
          '<td>native pick taken; place by hand on the board</td></tr>').join('') +
        '</tbody></table>';
    }
    if (sim.chasm.length) {
      chasmBlock += '<h3 class="kb-print-chasm-h">Can&rsquo;t slot (chasm) &mdash; keeper designation blocked</h3>' +
        '<table><thead><tr><th>Player</th><th>Pos</th><th>2025 finish</th><th>DRC</th><th>$</th><th>Reason</th></tr></thead><tbody>' +
        sim.chasm.map(p =>
          '<tr><td class="kb-print-chasm">' + esc(p.n) + '</td>' +
          '<td>' + esc(p.p || '') + '</td>' +
          '<td>' + finish25(p) + '</td>' +
          '<td class="num">' + p.d6 + '</td>' +
          '<td class="num">' + money(p.c6) + '</td>' +
          '<td class="kb-print-chasm">no legal slot under the slide rules</td></tr>').join('') +
        '</tbody></table>';
    }

    root.querySelector('.kb-print').innerHTML =
      '<h2>' + esc(t.team || '') + ' &middot; 2026 keeper board</h2>' +
      '<p class="kb-print-sub">' + esc(t.mgr || '') + ' &middot; ' + keepers().length +
      ' keepers &middot; ' + money(capTotal()) + ' committed &middot; ' +
      '<span class="kb-print-legend"><span class="kb-print-legend-item"><span class="kb-print-legend-key">R#</span> keeper slot</span> &middot; ' +
      '<span class="kb-print-legend-item"><span class="kb-print-legend-key kb-print-legend-open">R#</span> open draft slot</span> &middot; ' +
      '<span class="kb-print-legend-item"><span class="kb-print-legend-key kb-print-legend-gone">R#</span> traded away</span></span></p>' +
      '<table class="kb-print-full"><thead><tr><th style="width:44px;">Rd</th><th>Slot</th><th style="width:44px;">Pos</th><th style="width:74px;">2025 finish</th><th style="width:56px;">DRC</th><th style="width:60px;">$</th><th style="width:30%;">Notes</th></tr></thead><tbody>' +
      rowsHtml.join('') + '</tbody></table>' +
      lineupPrint() +
      chasmBlock;
  }

  const app = root.querySelector('.kb-app');

  app.addEventListener('change', e => {
    if (e.target.dataset.role === 'kbteam') {
      st.team = e.target.value; st.keep = {}; st.manual = {}; st.pick = null; st.seq = 0; st.pickFor = null; render();
    }
  });

  app.addEventListener('click', e => {
    if (st.pickFor != null) {
      const opt = e.target.closest('[data-pfpick]');
      if (opt) {
        const pid = +opt.dataset.pfpick;
        if (!st.keep[pid]) st.keep[pid] = ++st.seq;
        st.manual[pid] = st.pickFor;
        st.pickFor = null; render(); return;
      }
      if (e.target.closest('[data-pfclose]') || !e.target.closest('.kb-pf')) { st.pickFor = null; render(); }
      return;
    }
    const unseat = e.target.closest('[data-unseat]');
    if (unseat) { const pid = +unseat.dataset.unseat; delete st.keep[pid]; delete st.manual[pid]; if (st.pick === pid) st.pick = null; render(); return; }
    const act = e.target.closest('[data-kbact]');
    if (act) {
      if (act.dataset.kbact === 'reset') { st.keep = {}; st.manual = {}; st.pick = null; st.seq = 0; st.pickFor = null; render(); }
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
      if (!st.keep[pid]) { st.keep[pid] = ++st.seq; st.pick = null; }
      else if (onCheck) { delete st.keep[pid]; delete st.manual[pid]; if (st.pick === pid) st.pick = null; }
      else st.pick = (st.pick === pid ? null : pid);
      render(); return;
    }
    const openSlot = e.target.closest('.kb-slot.kb-open[data-slot]');
    if (openSlot && st.pick == null) { st.pickFor = +openSlot.dataset.slot; render(); return; }
    if (st.pick != null) { st.pick = null; render(); }
  });

  /* Real drag and drop. The old version re-rendered on dragstart, which
     destroyed the dragged node and killed the drag in every browser; this
     one lights the legal slots IN PLACE and only re-renders on drop.
     Draggable: any roster card (dragging an unkept player keeps AND
     places them in one motion) and any seated player on the board. */
  let dragPid = null;
  app.addEventListener('dragstart', e => {
    const seated = e.target.closest('[data-dragpid]');
    const card = e.target.closest('.kb-card');
    const pid = seated ? +seated.dataset.dragpid : (card ? +card.dataset.pid : null);
    if (pid == null) { e.preventDefault(); return; }
    dragPid = pid;
    e.dataTransfer.setData('text/plain', String(pid));
    e.dataTransfer.effectAllowed = 'move';
    const p = D.players.find(x => x.i === pid);
    const sim = kbSim(pid);
    const freeIds = p ? legalSlotIds(clampDrc(p.d6), sim.slots) : {};
    app.querySelectorAll('.kb-slot[data-slot]').forEach(el => {
      el.classList.remove('kb-legal', 'kb-illegal', 'kb-drop-hot');
      el.classList.add(freeIds[+el.dataset.slot] ? 'kb-legal' : 'kb-illegal');
    });
    app.classList.add('kb-dragging');
  });
  app.addEventListener('dragover', e => {
    const slot = e.target.closest('.kb-slot.kb-legal');
    if (slot) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (!slot.classList.contains('kb-drop-hot')) {
        app.querySelectorAll('.kb-slot.kb-drop-hot').forEach(el => el.classList.remove('kb-drop-hot'));
        slot.classList.add('kb-drop-hot');
      }
    }
  });
  app.addEventListener('dragleave', e => {
    const slot = e.target.closest('.kb-slot.kb-drop-hot');
    if (slot && !slot.contains(e.relatedTarget)) slot.classList.remove('kb-drop-hot');
  });
  app.addEventListener('drop', e => {
    const slot = e.target.closest('.kb-slot.kb-legal');
    if (slot && dragPid != null) {
      e.preventDefault();
      if (!st.keep[dragPid]) st.keep[dragPid] = ++st.seq;
      st.manual[dragPid] = +slot.dataset.slot;
    }
    dragPid = null; app.classList.remove('kb-dragging'); render();
  });
  app.addEventListener('dragend', () => {
    if (dragPid != null || app.classList.contains('kb-dragging')) {
      dragPid = null; app.classList.remove('kb-dragging'); render();
    }
  });

  render();
})();

/* ---- 2026 draft board (league-wide pick grid) ------------------------ */
(function() {
  const D = window.TRADE_DATA;
  const root = document.getElementById('draft-board');
  if (!D || !root) return;
  const app = root.querySelector('.db26-app');
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const money = n => '$' + n.toLocaleString();
  const DOLLARS = {1:200, 2:100, 3:80, 4:60, 5:50, 6:30, 7:30, 8:30, 9:30};
  const roundCost = r => DOLLARS[r] || 10;
  const clampDrc = d => Math.max(1, Math.min(16, d));
  const teamBy = {}; D.teams.forEach(t => teamBy[t.slug] = t);
  const playersBy = {}; D.players.forEach(p => { (playersBy[p.m] = playersBy[p.m] || []).push(p); });
  const DPOS = D.draft_pos || {};
  const firstName = s => (s || '').split(' ')[0];
  const pById = {}; D.players.forEach(p => pById[p.i] = p);
  const SEATS = D.seats || {}, AWAIT = D.awaiting || {}, CHASM = D.chasm || {};
  const seatKey = {};
  Object.keys(SEATS).forEach(h => SEATS[h].forEach(st =>
    seatKey[h + '|' + st.r + '|' + st.o] = st));

  /* League-wide pick map from the per-team inventories the analyzer and
     keeper board already embed: D.picks[holder] lists {r, o, lp}. A pick
     renders at its ORIGINAL owner's draft slot (that fixes its number,
     linear draft), naming whoever holds it now. */
  const byRound = {};
  Object.keys(D.picks).forEach(h => (D.picks[h] || []).forEach(pk => {
    (byRound[pk.r] = byRound[pk.r] || []).push({ h: h, o: pk.o, lp: pk.lp });
  }));

  let showKeep = false;

  /* The keeper lens is an INVENTORY view, same principle as the trade
     analyzer: players whose DRC natively lands in this round for the
     pick's holder. It never assumes who is actually kept. */
  function stackFor(holder, r) {
    return (playersBy[holder] || []).filter(p => clampDrc(p.d6) === r)
      .sort((a, b) => (b.pts || 0) - (a.pts || 0));
  }

  function render() {
    const cards = [];
    for (let r = 1; r <= 16; r++) {
      const picks = (byRound[r] || []).slice()
        .sort((a, b) => ((a.lp ? 98 : DPOS[a.o] || 97) - (b.lp ? 98 : DPOS[b.o] || 97)));
      const stacked = {};   // holder -> already stacked this round (a team
                            // with two picks here gets its players listed once)
      const rows = picks.map(pk => {
        const t = teamBy[pk.h] || {};
        const slot = pk.lp ? null : DPOS[pk.o];
        const num = slot ? r + '.' + String(slot).padStart(2, '0') : null;
        const acq = pk.o !== pk.h;
        let note = '';
        if (acq) note = 'via trade from ' + esc(firstName((teamBy[pk.o] || {}).mgr));
        if (pk.lp) note = (note ? note + ' &middot; ' : '') + 'last-pick convention';
        const seatSt = seatKey[pk.h + '|' + r + '|' + pk.o];
        let seatHtml = '';
        if (seatSt && pById[seatSt.pid]) {
          const sp = pById[seatSt.pid];
          seatHtml = '<span class="db26-seat">&#10004; ' + esc(sp.n) +
            ' <span class="db26-kp-sub">' + esc(sp.p || '') +
            ' &middot; DRC ' + sp.d6 + ' &middot; ' + money(sp.c6) +
            (seatSt.up ? ' &middot; slid up from R' + sp.d6 : '') +
            '</span></span>';
        }
        let stack = '';
        if (showKeep && !stacked[pk.h]) {
          stacked[pk.h] = 1;
          const ps = stackFor(pk.h, r);
          stack = '<div class="db26-stack">' + (ps.length
            ? ps.map(p => '<span class="db26-kp">' + esc(p.n) +
                ' <span class="db26-kp-sub">' + esc(p.p || '') +
                (p.pr != null && p.p ? ' &middot; ' + esc(p.p) + p.pr + ' in 2025' : '') + '</span></span>').join('')
            : '<span class="db26-none">no roster player at DRC ' + r + '</span>') + '</div>';
        }
        return '<div class="db26-pick' + (acq ? ' db26-acq' : '') + (seatHtml ? ' db26-used' : '') + '">' +
          '<span class="db26-num">' + (num || 'LP') + '</span>' +
          '<span class="db26-team">' + esc(t.team || pk.h) +
          '<span class="db26-mgr">' + esc(t.mgr || '') + (note ? ' &middot; ' + note : '') + '</span></span>' +
          seatHtml + stack + '</div>';
      }).join('');
      cards.push('<div class="db26-round"><div class="db26-round-h">Round ' + r +
        '<span class="db26-cost">keeper cost ' + money(roundCost(r)) + '</span></div>' + rows + '</div>');
    }
    const awaitBits = [];
    Object.keys(AWAIT).forEach(h => (AWAIT[h] || []).forEach(pid => {
      const p0 = pById[pid]; if (!p0) return;
      awaitBits.push('<span class="db26-await-item"><strong>' +
        esc(firstName((teamBy[h] || {}).mgr || h)) + ':</strong> ' + esc(p0.n) +
        ' (DRC ' + p0.d6 + ' &middot; ' + money(p0.c6) + ')</span>');
    }));
    Object.keys(CHASM).forEach(h => (CHASM[h] || []).forEach(pid => {
      const p0 = pById[pid]; if (!p0) return;
      awaitBits.push('<span class="db26-await-item db26-chasm"><strong>' +
        esc(firstName((teamBy[h] || {}).mgr || h)) + ':</strong> ' + esc(p0.n) +
        ' (DRC ' + p0.d6 + ' &mdash; no legal slot)</span>');
    }));
    const awaitHtml = awaitBits.length
      ? '<div class="db26-await"><div class="db26-await-h">Awaiting placement &mdash; keeper needs a manual slot call (traded-in keepers and acquired-pick landings are never seated automatically)</div>' + awaitBits.join('') + '</div>'
      : '';
    app.innerHTML =
      '<div class="db26-top"><label class="db26-toggle"><input type="checkbox"' + (showKeep ? ' checked' : '') +
      ' data-role="db26keep"> Show potential keepers<span class="db26-toggle-sub">each pick lists the holder&#39;s roster players whose DRC lands in that round &mdash; who could sit there, not who will</span></label></div>' +
      awaitHtml +
      '<div class="db26-grid">' + cards.join('') + '</div>';
  }

  app.addEventListener('change', e => {
    if (e.target.dataset.role === 'db26keep') { showKeep = e.target.checked; render(); }
  });
  render();
})();

/* ---- Player comparison ----------------------------------------------- */
(function() {
  const C = window.COMPARE_DATA;
  const root = document.getElementById('player-compare');
  if (!C || !root) return;
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const input = document.getElementById('pc-input');
  const sugg = document.getElementById('pc-sugg');
  const cols = document.getElementById('pc-cols');
  const byId = {}; C.forEach(p => { byId[p.i] = p; });
  let sel = [];

  function bars(w, maxV, color) {
    const W = 170, H = 54, PB = 3, slots = 17;
    const sw = W / slots, bw = sw * 0.62, ch = H - 3 - PB;
    let out = '';
    for (let k = 0; k < slots; k++) {
      const v = w ? w[k] : null;
      const x = k * sw + (sw - bw) / 2;
      if (v == null || v <= 0) {
        out += '<rect x="' + x.toFixed(1) + '" y="' + (H - PB - 1).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="1" rx="0.5" fill="var(--gray-200)"/>';
      } else {
        const bh = Math.max(1.5, (v / maxV) * ch);
        out += '<rect x="' + x.toFixed(1) + '" y="' + (H - PB - bh).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + bh.toFixed(1) + '" rx="1" fill="' + color + '"/>';
      }
    }
    return '<svg class="pc-chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' + out + '</svg>';
  }

  /* Empty seasons render at FULL block height (dash + stub bars) so all
     three cards line up row for row, rookie or not. */
  function seasonBlock(label, s, maxV, color) {
    const head = '<div class="pc-yr"><span class="pc-dot" style="background:' + color + ';"></span>' + label + '</div>';
    if (!s) {
      return '<div class="pc-season">' + head +
        '<div class="pc-pts" style="color:#c5c9d2;">&mdash;</div>' +
        '<div class="pc-sub">no recorded weeks</div>' +
        bars(null, maxV, color) + '</div>';
    }
    const w = (s.w || []).filter(v => v != null);
    const n = w.length;
    const tot = s.pts != null ? s.pts : w.reduce((a, b) => a + b, 0);
    const ppg = n ? (tot / n) : null;
    return '<div class="pc-season">' + head +
      '<div class="pc-pts">' + Number(tot).toFixed(1) + ' <span style="font-size:12px;color:#8e8e93;font-weight:600;">pts</span></div>' +
      '<div class="pc-sub">' + n + ' wks' + (ppg != null ? ' &middot; ' + ppg.toFixed(1) + '/wk' : '') + (s.rk ? ' &middot; pos rank ' + s.rk : '') + '</div>' +
      bars(s.w, maxV, color) + '</div>';
  }

  function render() {
    let maxV = 0;
    sel.forEach(id => {
      const p = byId[id];
      [p.y25, p.y24].forEach(s => { if (s && s.w) s.w.forEach(v => { if (v != null && v > maxV) maxV = v; }); });
    });
    if (maxV <= 0) maxV = 1;
    if (!sel.length) {
      cols.innerHTML = '<div class="pc-hintcard">Search a player above to start a comparison. Up to three side by side.</div>';
    } else {
      cols.innerHTML = sel.map(id => {
        const p = byId[id];
        const adp = p.a != null ? ' &middot; ADP ' + (p.a % 1 === 0 ? p.a : p.a.toFixed(1)) : '';
        const own = (p.o
          ? ('Currently: <b>' + esc(p.o) + '</b>' + (p.k ? ' &middot; DRC ' + p.k.d + ' &middot; $' + (p.k.c || 0).toLocaleString() + ' to keep' : ''))
          : 'No current owner') + adp;
        return '<div class="pc-card"><div class="pc-head"><span class="pc-name">' + esc(p.n) + '</span>' +
          '<button class="pc-x" data-pcrm="' + p.i + '" title="Remove" type="button">&#10005;</button></div>' +
          '<div class="pc-meta">' + esc(p.p || '') + ' &middot; ' + esc(p.t || '') + '</div>' +
          '<div class="pc-owner">' + own + '</div>' +
          seasonBlock('2025', p.y25, maxV, 'var(--blue-600)') +
          seasonBlock('2024', p.y24, maxV, '#022479') +
          '</div>';
      }).join('');
    }
    input.disabled = sel.length >= 3;
    input.placeholder = sel.length >= 3 ? 'Three players max — remove one to swap' : 'Type a player name to add (up to 3)…';
  }

  function hideSugg() { sugg.hidden = true; sugg.innerHTML = ''; }
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    if (q.length < 2) { hideSugg(); return; }
    const hits = C.filter(p => p.n.toLowerCase().indexOf(q) >= 0 && sel.indexOf(p.i) < 0).slice(0, 8);
    if (!hits.length) { hideSugg(); return; }
    sugg.innerHTML = hits.map(p => '<div class="pc-sugg-item" data-pcadd="' + p.i + '"><b>' + esc(p.n) + '</b>' +
      '<span class="pc-sugg-meta">' + esc(p.p || '') + ' &middot; ' + esc(p.t || '') + '</span>' +
      '<span class="pc-sugg-owner">' + (p.o ? esc(p.o) : 'free agent') + '</span></div>').join('');
    sugg.hidden = false;
  });
  document.addEventListener('click', e => {
    const add = e.target.closest('[data-pcadd]');
    if (add) { if (sel.length < 3) sel.push(+add.dataset.pcadd); input.value = ''; hideSugg(); render(); return; }
    const rm = e.target.closest('[data-pcrm]');
    if (rm) { sel = sel.filter(x => x !== +rm.dataset.pcrm); render(); return; }
    if (!e.target.closest('.pc-top')) hideSugg();
  });
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


def _ot_player_card(entry):
    """One traded-player card: name, pos/team, and the 2026 cost both
    ways — keep-path DRC (what the old owner would have paid, no trade)
    -> frozen trade-time DRC (what the acquirer pays in 2026)."""
    meta = f'{entry["position"]} &middot; {entry["nfl_team"]}'
    keep = f'DRC {entry["keep_drc"]} &middot; ${entry["keep_dollars"]}'
    frozen = f'DRC {entry["frozen_drc"]} &middot; ${entry["frozen_dollars"]}'
    tier = drc_tier_class(entry["frozen_drc"])
    return f"""
          <div class="ot-player">
            <div class="ot-player-top">
              <span class="ot-player-name">{html.escape(entry["name"])}</span>
              <span class="ot-player-meta">{meta}</span>
            </div>
            <div class="ot-costline">
              <span class="ot-cost-was"><span class="ot-cost-label">Keeper DRC</span>{keep}</span>
              <span class="ot-arrow" aria-hidden="true">&rarr;</span>
              <span class="ot-cost-now"><span class="ot-cost-label">Trade DRC</span><span class="pill {tier}">{frozen}</span></span>
            </div>
          </div>"""


def _ot_pick_chip(pick):
    """Pick chip in R.PP format (4.05 = round 4, pick 5; linear draft, so
    the pick-in-round equals the original owner's draft slot in every
    round). Falls back to 'R{n} pick' if the slot can't be resolved from
    lottery_result.json."""
    orig = alias_name(pick["original"])
    slot = pick.get("slot")
    label = (f'Pick {pick["round"]}.{slot:02d}' if slot
             else f'R{pick["round"]} pick')
    return (f'<div class="ot-pick">{label}'
            f'<span class="ot-pick-orig">orig. {html.escape(orig)}</span></div>')


def _ot_side(mgr_actual, players, picks):
    mgr = alias_name(mgr_actual)
    cards = "".join(_ot_player_card(p) for p in players)
    chips = "".join(_ot_pick_chip(pk) for pk in picks)
    empty = ('<div class="ot-none">No players</div>'
             if not players and not picks else "")
    return f"""
        <div class="ot-side">
          <div class="ot-side-label">{html.escape(mgr)} receives</div>
          {cards}{chips}{empty}
        </div>"""


def render_offseason_trades_section(trades, season=2026):
    """One '{season} off-season trades' tab: one card per trade, the
    players (and picks) moving each way, and each player's keeper cost
    with and without the trade under the freeze rule. Facts only,
    consistent with the trade analyzer's no-verdict stance.

    Sidebar has a permanent 'Off-season Trades' group — each new season
    gets its own section (call this once per season with that season's
    trades) plus a matching sidebar link."""
    cards = []
    for t in trades:
        try:
            date_disp = datetime.strptime(t["date"], "%Y-%m-%d") \
                .strftime("%b %d, %Y").replace(" 0", " ")
        except (ValueError, TypeError):
            date_disp = t["date"]
        mgr_a = alias_name(t["mgr_a"])
        mgr_b = alias_name(t["mgr_b"])
        cards.append(f"""
      <article class="ot-card">
        <header class="ot-head">
          <span class="ot-date">{date_disp}</span>
          <span class="ot-teams">{html.escape(mgr_a)} <span class="ot-swap">&harr;</span> {html.escape(mgr_b)}</span>
        </header>
        <div class="ot-sides">
          {_ot_side(t["mgr_a"], t["players_a"], t["picks_a"])}
          {_ot_side(t["mgr_b"], t["players_b"], t["picks_b"])}
        </div>
      </article>""")
    body = "".join(cards) if cards else \
        f'<div class="ot-none">No off-season trades recorded yet for {season}.</div>'
    count_note = f"{len(trades)} trade{'s' if len(trades) != 1 else ''} in the {season} off-season window."
    return f"""
    <section class="team-section" id="offseason-trades-{season}" hidden>
      <header class="section-header">
        <h1 class="section-title">{season} off-season trades</h1>
        <p class="section-sub">Every trade since the {season - 1} season ended. Each player shows the {season} cost on their old owner's keep path and the frozen trade-time cost the new owner inherits (decrements resume {season + 1}). {count_note}</p>
      </header>
      <div class="ot-list">{body}</div>
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

      <div class="rh-canon" style="background:#fff8e8;border-left-color:#c79a2e;color:#674f00;">
        <strong>Summit update &mdash; 2026-06-27:</strong> four votes settled at the Beach Summit. <strong>Passed:</strong> 1.01 free-keeper loophole ban, asset-for-asset trade counters (FAAB now countable), proxy voting banned (mail-in ballots allowed). <strong>Failed again:</strong> keepers-to-the-back-of-the-draft. <strong>Still pending:</strong> whether the annual lottery should happen <em>after</em> the keeper deadline. 12 votes present (proxies noted in each rule below).
      </div>

      <h2 class="rh-h2">Pending votes</h2>
      <p class="rh-note">Unsettled as of the 2026-06-27 summit &mdash; queued for the next league decision.</p>
      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">Lottery timing &mdash; before or after the keeper deadline?</span> <span class="rh-chip rh-dock">Pending</span></div>
        <p class="rh-sum">Should the annual draft lottery happen <em>after</em> the keeper deadline instead of before? Consequential for keeper strategy: knowing your draft slot changes which players are worth keeping at their DRC cost.</p>
        <p class="rh-verdict"><strong>Where it stands:</strong> raised at the 2026-06-27 summit, no verdict reached. Carried over.</p>
      </div>

      <h2 class="rh-h2">At a glance</h2>
      <p class="rh-note">Status of every rule in this ledger.</p>
      <table class="rh-ov">
        <thead><tr><th>Rule</th><th>Status</th><th class="rh-r">Last action</th></tr></thead>
        <tbody>
          <tr><td>Fill the open 12th seat ("The Lady Boys")</td><td><span class="rh-chip rh-live">Resolved</span></td><td class="rh-r">Bill K. joined 8/26</td></tr>
          <tr><td>Proxy voting banned; mail-in ballots allowed</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Passed 6/27/26</td></tr>
          <tr><td>1.01 free-keeper loophole &mdash; ban</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Passed 6/27/26</td></tr>
          <tr><td>Keepers to the back of the draft (Tom)</td><td><span class="rh-chip rh-fail">Failed again</span></td><td class="rh-r">Voted down 6/27/26</td></tr>
          <tr><td>Counter-offer rule &mdash; now asset-for-asset (players, picks, or FAAB)</td><td><span class="rh-chip rh-live">In effect</span></td><td class="rh-r">Updated 6/27/26</td></tr>
          <tr><td>Lottery after the keeper deadline?</td><td><span class="rh-chip rh-dock">Pending</span></td><td class="rh-r">Raised 6/27/26</td></tr>
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
        <p class="rh-sum">RESOLVED: Bill K. takes over "The Lady Boys" for 2026. The 12th chair had been contested since day one.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-09-04</span><span class="rh-who">Pete:</span> "Who do we want as our 12th and final manager? A. Jon  B. Other Dan."</li>
          <li><span class="rh-date">2023-09-04</span><span class="rh-who">Paul:</span> "Jon didn't get six votes&hellip; other Dan gets right of first refusal."</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> a personnel vote, not a rules mechanic. The seat has churned managers nearly every season; Friday's job is simply to fill it.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">2 &middot; 1.01 free-keeper loophole &mdash; ban</span> <span class="rh-chip rh-live">Passed 6/27/26</span></div>
        <p class="rh-sum">Stop the manager holding 1.01 from dropping a keeper during selection and re-grabbing him at 1.01 for the cheap original cost instead of paying $200. Paul has flagged this since the founding year.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-11-14</span><span class="rh-who">Paul:</span> "The only way to keep the keeper-forfeiture rule and fix the loophole is to preclude keeping of first-round picks."</li>
          <li><span class="rh-date">2024-07-22</span><span class="rh-who">Proposal:</span> "Manager at 1.1 must either keep their player for $200 or can't redraft him at 1.1." &mdash; Brian, George &amp; Tom for; Scott, Alex &amp; Brad skeptical.</li>
          <li><span class="rh-date">2024-07-24</span><span class="rh-who">Pete:</span> "A stalemate means no rule is passed." <span class="rh-who">Tom:</span> "6 votes is not a majority."</li>
          <li><span class="rh-date">2025-06-27</span><span class="rh-who">Paul:</span> "Efforts to pass [a] rule to preclude [the] loophole fail[ed]."</li>
          <li><span class="rh-date">2026-06-27</span><span class="rh-who">Summit minute:</span> <strong>Passes.</strong> The 1.01 free-keeper maneuver (drop a DRC-1 keeper mid-draft and re-select him at the 1.01 for the cheap original cost) is banned. Whoever holds the 1.01 must either keep the player at full $200 or cannot redraft him at 1.01.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> <strong>in effect as of 2026-06-27.</strong> Third attempt cleared. Along with the general no-drop-and-re-add rule (below), the 1.01 lane is now specifically closed. See the League Rules doc for the enforcement text.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">3 &middot; Keepers to the back of the draft (Tom)</span> <span class="rh-chip rh-fail">Failed again 6/27/26</span></div>
        <p class="rh-sum">Tom's simplification: keepers fill the back rounds; a pick's round only sets the dollar cost. Pitched as a replacement for the slide/chasm machinery.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2024-06-29</span><span class="rh-who">Tom:</span> "Your keepers fill in the back of your roster, not the front&hellip; draft round should only matter for the monetary cost."</li>
          <li><span class="rh-date">2024-07-13</span><span class="rh-who">Paul:</span> "Tom's proposal got voted down at Hodor's in person&hellip; My thing got voted down. Tom's thing got voted down."</li>
          <li><span class="rh-date">2026-06-27</span><span class="rh-who">Summit minute:</span> <strong>Does not pass.</strong> Slide + chasm rules stay as the mechanism.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> voted down again at the 2026-06-27 summit. The slide/chasm machinery remains the mechanism for keeper placement.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">4 &middot; Counter-offer rule &mdash; asset-for-asset</span> <span class="rh-chip rh-live">Updated 6/27/26</span></div>
        <p class="rh-sum">When a trade is posted, the league has 48 hours to counter. As of 6/27/26 a valid counter is <strong>asset-for-asset</strong> &mdash; assets include players, draft picks, <em>and</em> FAAB (new). The counter must include at least one of the original trade's assets, and if that shared asset is FAAB, the counter's FAAB must be at least the original amount.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2023-09-05</span><span class="rh-who">Tom:</span> "Once a trade is accepted it is presented to the league and we have 48 hours to offer counteroffers."</li>
          <li><span class="rh-date">2023-09-12</span><span class="rh-who">Pete:</span> "There should be a [counter] that includes one of the two original players."</li>
          <li><span class="rh-date">2024-11-06</span><span class="rh-who">Pete:</span> "Denied." &mdash; rejecting an invalid counter under the pre-2026 rule.</li>
          <li><span class="rh-date">2026-06-27</span><span class="rh-who">Summit minute:</span> <strong>Amended.</strong> "Player-for-player" broadened to "asset-for-asset." Example: a trade of MHJ for 50 FAAB can be countered where either MHJ <em>or</em> at least 50 FAAB is included in the counter.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> in effect with the 6/27/26 amendment. FAAB is now a first-class asset for both proposing and countering trades.</p>
      </div>

      <div class="rh-rule">
        <div class="rh-rhead"><span class="rh-rname">7 &middot; Proxy voting &mdash; banned; mail-in ballots &mdash; allowed</span> <span class="rh-chip rh-live">Passed 6/27/26</span></div>
        <p class="rh-sum">A manager can no longer vote by proxy through another manager present at the summit. Mail-in ballots (a manager's vote submitted in writing ahead of time) remain a legitimate substitute for absentees.</p>
        <ul class="rh-rec">
          <li><span class="rh-date">2026-06-27</span><span class="rh-who">Summit minute:</span> <strong>Passes.</strong> No proxy; mail-in allowed. Retroactively noted: 12 votes were present via proxy at this summit (Pete H. represented Bill K. + Alex S. + himself; George M. represented Dan V.; Tom W. represented Aric T.; Greg P., Dan M., Paul L., Scott M., Brian M. each 1). Future summits use mail-in for absentees.</li>
        </ul>
        <p class="rh-verdict"><strong>Where it stands:</strong> in effect from 2026-06-27 forward.</p>
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
        <p class="rh-sum">The <em>name</em> for the 1.01 loophole maneuver itself (drop a DRC-1 keeper, re-pick him at 1.01 for $0 instead of paying $200). Discussed at summit; never sanctioned as a legal move.</p>
        <p class="rh-verdict"><strong>Status:</strong> the maneuver was never legal, and as of 2026-06-27 it is explicitly <em>banned</em> (see docket item 2 above). The name persists as folklore.</p>
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
          <h3 class="rule-h3">Trade review &mdash; asset-for-asset counters <span class="rule-new-pill" style="display:inline-block;margin-left:8px;">Updated 6/27/26</span></h3>
          <p>All trades undergo a <strong>48-hour review window</strong>. During the window, other teams may counter the original agreement with a better offer. As of the 2026-06-27 summit, a valid counter is <strong>asset-for-asset</strong> &mdash; an "asset" is a player, a draft pick, <em>or</em> FAAB. The counter must include at least one of the original trade's assets; if the shared asset is FAAB, the counter's FAAB must be at least the original amount. Example: a trade of MHJ for 50 FAAB can be countered where either MHJ or at least 50 FAAB is included in the counter. The 48-hour timer starts at the acceptance of the original trade; successful counters do <em>not</em> reset it.</p>
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

        <section class="rule-block rule-block-new">
          <div class="rule-new-pill">Newly passed 6/27/26</div>
          <h2 class="rule-h2">1.01 free-keeper ban</h2>
          <p>The manager holding the <strong>1.01</strong> (first overall pick) may <em>not</em> drop a DRC-1 keeper during draft selection and then re-select that same player at 1.01 for the cheap original cost. The choice is binary: <strong>keep the player at full $200</strong>, or release them and pick a different player at 1.01.</p>
          <p>Closes the loophole informally known as the "George $200 Rule" &mdash; the workaround that would have let the 1.01 holder pay $0 for a DRC-1 player instead of the $200 that keeping normally costs. Complements the general no-drop-and-immediately-re-add rule below by removing the 1.01-specific edge case that survived it.</p>
          <p class="rules-note">Passed at the 2026-06-27 Beach Summit. Third attempt cleared; failed at the 2024 and 2025 summits.</p>
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

        <section class="rule-block rule-block-new">
          <div class="rule-new-pill">Newly passed 6/27/26</div>
          <h2 class="rule-h2">Voting &mdash; proxy banned, mail-in allowed</h2>
          <p><strong>Proxy voting is not allowed.</strong> A manager present at a league vote cannot cast a vote on another manager's behalf.</p>
          <p><strong>Mail-in ballots are allowed.</strong> A manager who cannot attend in person may submit their vote in writing ahead of the meeting. A mail-in ballot counts the same as an in-person vote for quorum and tally purposes.</p>
          <p class="rules-note">Passed at the 2026-06-27 Beach Summit. That summit itself was decided on proxies (12 votes present via proxy: Pete H. represented Bill K. + Alex S. + himself; George M. represented Dan V.; Tom W. represented Aric T.; Greg P., Dan M., Paul L., Scott M., Brian M. each cast one) &mdash; from the next league vote forward, absentees submit mail-in ballots.</p>
        </section>

        <section class="rule-block">
          <h2 class="rule-h2">Pending votes</h2>
          <p><strong>Lottery timing.</strong> Whether the annual draft lottery should happen <em>after</em> the keeper deadline (rather than before, as it does now) was raised at the 2026-06-27 summit and left unresolved. Consequential for keeper strategy since knowing your draft slot changes which players are worth keeping at their DRC cost. Carried to the next league vote.</p>
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

        lineage_all = p["lineage"] if p["lineage"] else []
        lineage_nodes = []
        for i, node in enumerate(lineage_all):
            if i > 0:
                if node.get("cycle_break"):
                    lineage_nodes.append(
                        '<div class="lineage-break">'
                        '<span class="lineage-break-x">&#10007;</span>'
                        '<span class="lineage-break-txt">not kept &middot; back to draft pool</span>'
                        '</div>'
                    )
                else:
                    lineage_nodes.append('<div class="lineage-arrow">&rarr;</div>')
            method_class = node["method"].lower().replace(" ", "-")
            cost_chip = ""
            if node.get("drc_set"):
                _d = node["drc_set"]
                _dol = node.get("dollars_set")
                cost_chip = (
                    f'<div class="lineage-cost">DRC {_d}'
                    + (f' &middot; ${_dol}' if _dol else '')
                    + '</div>'
                )
            tag_html = (
                f'<div class="lineage-tag">{html.escape(node["cost_tag"])}</div>'
                if node.get("cost_tag") else ""
            )
            lineage_nodes.append(
                f'<div class="lineage-node lineage-{method_class}">'
                f'<div class="lineage-date">{html.escape(_event_date_display(node["date"], node["method"] == "Trade"))}</div>'
                f'<div class="lineage-manager">{html.escape(node["manager"])}</div>'
                f'<div class="lineage-method">{html.escape(node["method"])}</div>'
                f'<div class="lineage-detail">{html.escape(node["detail"])}</div>'
                f'{cost_chip}{tag_html}'
                '</div>'
            )
        # Terminal node: what this chain costs for 2026
        cur_ln = next((y for y in p["per_year"] if y["year"] == 2026), None)
        if lineage_nodes and cur_ln and cur_ln.get("drc") is not None:
            lineage_nodes.append('<div class="lineage-arrow">&rarr;</div>')
            lineage_nodes.append(
                '<div class="lineage-node lineage-now">'
                '<div class="lineage-date">2026</div>'
                f'<div class="lineage-manager">{html.escape(cur_ln.get("owner") or "&#8212;")}</div>'
                '<div class="lineage-method">Keeper cost</div>'
                f'<div class="lineage-cost lineage-cost-now">DRC {cur_ln["drc"]} &middot; ${cur_ln["dollars"]}</div>'
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
            <div class="ps-section-label">Ownership &amp; cost lineage</div>
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


def _load_2026_draft_order():
    """Return two lookups from lottery_result.json — by manager name and
    by team name — so the sidebar can find each team's pick regardless of
    which identifier matches (a manager whose DB name differs from the
    lottery-file name still resolves via team). Falls back to empty dicts
    if the file is missing or malformed — team list then sorts by team
    name (prior behavior)."""
    path = Path(__file__).parent / "lottery_result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, {}
    by_mgr, by_team = {}, {}
    for entry in (data.get("lottery") or []) + (data.get("playoff") or []):
        pick = entry.get("pick")
        if not isinstance(pick, int):
            continue
        mgr = entry.get("manager")
        team = entry.get("team")
        if mgr:
            by_mgr[mgr] = pick
        if team:
            by_team[team] = pick
    return by_mgr, by_team


def build_sidebar(by_manager):
    # Sort by 2026 R1 draft pick (per lottery_result.json); anyone missing
    # from the lottery file falls to the end alphabetically. Team-name
    # match is the fallback so a manager whose DB name doesn't match the
    # lottery file (e.g. a mid-season seat handoff) still gets a pick.
    pick_by_mgr, pick_by_team = _load_2026_draft_order()
    def pick_for(t):
        return pick_by_mgr.get(t["manager_actual"]) or pick_by_team.get(t["team_name"])
    def sort_key(d):
        pick = pick_for(d)
        return (pick if pick is not None else 99, d["team_name"].lower())
    teams = sorted(by_manager.values(), key=sort_key)
    items = ''.join(
        (lambda pick: (
            f'<a class="nav-link" data-target="team-{manager_slug(t["manager_actual"])}">'
            + (f'<span class="draft-slot" aria-label="2026 draft pick {pick}">{pick}</span>'
               if pick is not None else '<span class="draft-slot" aria-hidden="true"></span>')
            + html.escape(t["team_name"])
            + f'<span class="manager">{html.escape(t["manager"])}</span>'
            + '</a>'
        ))(pick_for(t))
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
        <summary>Off-season Trades</summary>
        <div class="sidebar-team-list">
          <!-- One entry per year. Add the new season's link (and pass its
               trades to render_offseason_trades_section) each off-season. -->
          <a class="nav-link" data-target="offseason-trades-2026">2026 off-season trades</a>
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
          <a class="nav-link" data-target="player-compare">Player comparison</a>
          <a class="nav-link" data-target="trade-analyzer">Trade analyzer</a>
          <a class="nav-link" data-target="keeper-board">Keeper board</a>
          <a class="nav-link" data-target="draft-board">2026 draft board</a>
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
        slug = manager_slug(data["manager_actual"])
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
                **({"k": 1} if p.get("kept_2026") else {}),
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
            "JOIN managers m ON m.manager_id = t.manager_id "
            "WHERE t.season IN (2025, 2026)"):
        slug_by_tsid[r["team_season_id"]] = manager_slug(r["full_name"])
    held = {t["slug"]: [{"r": r, "o": t["slug"]} for r in range(1, 17)]
            for t in teams}
    lost = {t["slug"]: [] for t in teams}

    def _apply_pick_move(rnd_raw, s_id, d_id, o_id):
        rnd = min(rnd_raw, 16)
        last_pick = rnd_raw > 16
        src = slug_by_tsid.get(s_id)
        dst = slug_by_tsid.get(d_id)
        orig = slug_by_tsid.get(o_id) or src
        if src in held:
            pool = held[src]
            hit = next((p for p in pool if p["r"] == rnd and p["o"] == orig),
                       next((p for p in pool if p["r"] == rnd), None))
            if hit:
                pool.remove(hit)
                if hit["o"] == src:
                    lost[src].append({"r": rnd, "to": dst})
                elif orig in lost:
                    # Multi-hop pick: keep the original owner's "traded to"
                    # note pointing at the CURRENT holder, not the first hop
                    # (e.g. Dan's R16 went Dan -> Pete -> Scott; Dan's board
                    # should read "traded to Scott").
                    note = next((e for e in lost[orig]
                                 if e["r"] == rnd and e.get("to") == src),
                                next((e for e in lost[orig] if e["r"] == rnd),
                                     None))
                    if note:
                        note["to"] = dst
        if dst in held:
            held[dst].append({"r": rnd, "o": orig, **({"lp": 1} if last_pick else {})})

    # --- 2026 draft order (lottery result) -> per-team draft slot -------
    # The draft is LINEAR (verified against 2023-25 draft_picks: rounds run
    # in the same order, no snake), so a team's round-N pick is N.<slot>,
    # e.g. slot 2 in round 10 = 10.02. Keyed by manager slug from
    # lottery_result.json; unmatched entries warn and render without
    # numbers rather than guessing.
    draft_pos = {}
    lottery_path = Path(__file__).parent / "lottery_result.json"
    if lottery_path.exists():
        with open(lottery_path, encoding="utf-8") as f:
            _lot = json.load(f)
        _known = {t["slug"] for t in teams}
        _slug_by_team_name = {t["team"]: t["slug"] for t in teams}
        for entry in (_lot.get("lottery") or []) + (_lot.get("playoff") or []):
            s = manager_slug(entry.get("manager") or "")
            if s not in _known:
                s = _slug_by_team_name.get(entry.get("team") or "")
            if s in _known:
                draft_pos[s] = entry["pick"]
            else:
                print(f"WARNING: lottery entry unmatched to a team: "
                      f"{entry.get('manager')!r} / {entry.get('team')!r}")
        _missing = _known - set(draft_pos)
        if _missing:
            print(f"WARNING: no draft slot for {sorted(_missing)} — "
                  f"their picks render without numbers")
    else:
        print("WARNING: lottery_result.json not found — pick numbers omitted")

    # "My last pick" IOUs: Yahoo recorded these as Round 17 in a 16-round
    # draft (three real ones from the Nov 2025 deadline window — txns
    # 1226, 1206, 1201; three more R17 rows in raw `transactions` are
    # status='vetoed' and the all_transactions view already drops them).
    # League convention (Pete, 2026-08-24): the giver conveys their
    # actual final selection — the highest-round pick they still hold,
    # cascading up (no 16th left -> the 15th, and so on).
    last_pick_ious = []

    def _settle_last_picks():
        """Drain pending last-pick IOUs. ERA-ORDER RULING (Pete,
        2026-08-24): an IOU settles after every concrete trade of its own
        era but BEFORE the next era's trades apply — Alex's November
        claim on Dan's last pick resolves against Dan's end-of-2025
        holdings, so the 12.12 Dan acquired in Aug 2026 stays with Dan.
        (Within an era it still settles last, so e.g. Dan's 11/14 R13+R14
        trade is applied before his R17 IOU.) Highest round wins; within
        a round the latest draft slot is the literal last pick. The
        conveyed pick keeps its real identity, so it renders with its
        true number and a "via trade from" note downstream."""
        for src, dst in last_pick_ious:
            pool = held.get(src)
            if not pool:
                print(f"WARNING: last-pick settlement: {src!r} holds no "
                      f"picks to convey to {dst!r} — IOU dropped")
                continue
            hit = max(pool, key=lambda p: (p["r"], draft_pos.get(p["o"], 0)))
            pool.remove(hit)
            if dst in held:
                held[dst].append(hit)
            print(f"  last-pick IOU settled: {src} -> {dst} conveys "
                  f"R{hit['r']} (orig {hit['o']})")
        last_pick_ious.clear()

    # Real Yahoo pick trades: season-2025 transactions deal 2026-draft picks
    for mv in conn.execute(
            "SELECT tp.draft_round rnd, tp.source_team_season_id s, "
            " tp.destination_team_season_id d, tp.original_team_season_id o "
            "FROM transaction_picks tp "
            "JOIN all_transactions t ON t.transaction_id = tp.transaction_id "
            "WHERE t.season = 2025 ORDER BY t.timestamp"):
        if mv["rnd"] > 16:
            last_pick_ious.append((slug_by_tsid.get(mv["s"]),
                                   slug_by_tsid.get(mv["d"])))
        else:
            _apply_pick_move(mv["rnd"], mv["s"], mv["d"], mv["o"])

    _settle_last_picks()   # 2025-era IOUs resolve before 2026 trades apply

    # Synthetic pick trades (post-API-outage, commissioner-entered):
    # 2026 off-season trades deal 2026-draft picks. Table appears once
    # add_synthetic_trades.py has run its migration; absent = no moves yet.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='synthetic_transaction_picks'").fetchone():
        for mv in conn.execute(
                "SELECT sp.draft_round rnd, sp.source_team_season_id s, "
                " sp.destination_team_season_id d, sp.original_team_season_id o "
                "FROM synthetic_transaction_picks sp "
                "JOIN synthetic_transactions st ON st.synth_id = sp.synth_id "
                "WHERE st.season = 2026 ORDER BY st.timestamp"):
            if mv["rnd"] > 16:
                last_pick_ious.append((slug_by_tsid.get(mv["s"]),
                                       slug_by_tsid.get(mv["d"])))
            else:
                _apply_pick_move(mv["rnd"], mv["s"], mv["d"], mv["o"])
        _settle_last_picks()   # any synthetic-era IOU settles after its own era
    conn.close()


    total_picks = sum(len(v) for v in held.values())
    if total_picks != 16 * len(teams):
        print(f"WARNING: pick ledger out of balance — {total_picks} picks "
              f"across {len(teams)} teams (expected {16 * len(teams)}). "
              f"Check PICK_TXNS_IGNORED / last-pick settlements.")

    for slug in held:
        held[slug].sort(key=lambda p: (p["r"], draft_pos.get(p["o"], 99), p["o"]))

    # --- Seat the FINAL keepers onto each team's pick inventory ----------
    # Keepers are locked (keeper_selections), so the draft board can show
    # which picks the keeper process consumes. Auto-seat follows the
    # keeper-board physics: seat order is higher 2025 scorer first; a
    # keeper takes his own native-round pick if free, else slides DOWN the
    # consecutive-held chain (acquired picks keep the chain alive but are
    # NEVER auto-consumed) to the first free OWN pick. No auto up-moves.
    # Anyone unseated is "awaiting placement" (legal slots exist — the
    # manager/commish chooses, e.g. an acquired pick or an up-move) or a
    # chasm (no legal slot at all). Every traded-in keeper lands in
    # awaiting by construction (his native round belongs to his old team's
    # slot only if he holds a pick there).
    seats, awaiting, chasm = {}, {}, {}
    for _name, _data in by_manager.items():
        _slug = manager_slug(_data["manager_actual"])
        _picks = [dict(pk) for pk in held.get(_slug, [])]
        _rounds = {}
        for pk in _picks:
            pk["taken"] = False
            _rounds.setdefault(pk["r"], []).append(pk)

        def _pts25(pl):
            h = (pl.get("history") or {}).get(2025) or {}
            v = h.get("pts")
            return v if isinstance(v, (int, float)) else -1.0
        _keepers = sorted((pl for pl in _data["players"] if pl.get("kept_2026")),
                          key=_pts25, reverse=True)
        _seated, _await, _chasm = [], [], []
        for kp in _keepers:
            native = max(1, min(16, int(kp["drc"])))
            seat = None
            if native in _rounds:
                r = native
                while r <= 16 and r in _rounds:
                    own_free = [pk for pk in _rounds[r]
                                if pk["o"] == _slug and not pk["taken"]]
                    if own_free:
                        seat = own_free[0]
                        break
                    r += 1
            if seat is not None:
                seat["taken"] = True
                _seated.append({"r": seat["r"], "o": seat["o"],
                                "pid": kp["player_id"]})
                continue
            # Bottom-tier up-slide (league ruling 2026-05-29, auto-apply
            # ratified by Pete 2026-08-30): at the $10 tier (DRC >= 10)
            # there is no round 17 to slide into, so overflow slides UP —
            # the highest unused OWN round below native. Acquired picks
            # stay protected. Mid/premium tiers never auto up-slide;
            # those stay manual calls.
            if kp["drc"] >= 10:
                for r in range(native - 1, 0, -1):
                    own_free = [pk for pk in _rounds.get(r, [])
                                if pk["o"] == _slug and not pk["taken"]]
                    if own_free:
                        seat = own_free[0]
                        break
                if seat is not None:
                    seat["taken"] = True
                    _seated.append({"r": seat["r"], "o": seat["o"],
                                    "pid": kp["player_id"], "up": 1})
                    continue
            # No auto seat — is there ANY legal landing (free held pick at
            # native-or-earlier, or the activated slide destination)?
            legal = any(not pk["taken"]
                        for r in range(1, native + 1)
                        for pk in _rounds.get(r, []))
            if not legal and native in _rounds:
                r = native
                while r <= 16 and r in _rounds:
                    if any(not pk["taken"] for pk in _rounds[r]):
                        legal = True
                        break
                    r += 1
            (_await if legal else _chasm).append(kp["player_id"])
        if _seated:
            seats[_slug] = _seated
        if _await:
            awaiting[_slug] = _await
        if _chasm:
            chasm[_slug] = _chasm
    n_seated = sum(len(v) for v in seats.values())
    n_await = sum(len(v) for v in awaiting.values())
    n_chasm = sum(len(v) for v in chasm.values())
    print(f"  keeper seating: {n_seated} auto-seated, {n_await} awaiting "
          f"placement, {n_chasm} chasm")

    data_json = json.dumps({"teams": teams, "players": players,
                            "picks": held, "picks_lost": lost,
                            "draft_pos": draft_pos,
                            "seats": seats, "awaiting": awaiting,
                            "chasm": chasm,
                            "season": TARGET_SEASON}, separators=(",", ":"))

    # Shared with the team pages (2026 draft block in the Drafts tab).
    pick_data = {"held": held, "lost": lost, "draft_pos": draft_pos,
                 "seats": seats, "awaiting": awaiting, "chasm": chasm,
                 "teams": teams}

    section = f"""
    <section class="team-section" id="trade-analyzer" hidden>
      <header class="section-header">
        <h1 class="section-title">Trade analyzer</h1>
        <p class="section-sub">Pick two teams and check what's moving each way. The tool totals the production exchanged and lays out each player's keeper cost for {TARGET_SEASON} and the out-years under the trade-freeze rule. Numbers, not advice &mdash; the call is yours.</p>
      </header>

      <div class="ta-app"></div>

      <p class="ta-foot">Cost projections assume the trade completes before the {TARGET_SEASON} draft: the acquiring team inherits each player's trade-time DRC, frozen for {TARGET_SEASON}, with the normal decrement resuming the year after. The boards are an inventory view, not an arrangement: each round shows the picks you'd hold there (numbered by the lottery order; green means acquired) and every player whose DRC lands in that round &mdash; two players stacked under one round means more keepers than that round has picks, and sorting out who sits where is what the Keeper board tab is for. The chasm counter flags only STRUCTURAL impossibility: a DRC group bigger than the seats its slide chain and earlier picks can ever reach (a missing round is the wall). Having more players than picks overall is not flagged &mdash; nobody keeps a whole roster, and choosing who stays is the manager's call. Off-season trades are executed by the commissioner (Yahoo limitation), so loop Pete in to finalize anything you agree on.</p>
    </section>
    <script>window.TRADE_DATA = {data_json};</script>"""
    return section, pick_data


def render_player_compare(search_players):
    """Player comparison tab (Manager Tools): search like the player-search
    view, pin up to three players side by side. Shows the two most recent
    completed seasons of production (2025 + 2024) plus current ownership
    and the 2026 keep cost. Client-side; embeds a compact COMPARE_DATA
    JSON derived from the same corpus as player search."""
    rows = []
    for p in search_players:
        def season_pack(yr):
            py = next((y for y in p["per_year"] if y["year"] == yr), {}) or {}
            weekly = p["weekly_by_year"].get(yr, {})
            w = [round(weekly[wk], 1) if weekly.get(wk) is not None else None
                 for wk in range(1, 18)]
            has = any(v is not None for v in w) or py.get("pts") is not None
            if not has:
                return None
            return {"pts": py.get("pts"), "rk": py.get("pos_rank"), "w": w}
        cur = next((y for y in p["per_year"] if y["year"] == 2026), None) or {}
        entry = {
            "i": p["player_id"], "n": p["name"], "p": p["position"],
            "t": p["nfl_team"], "o": p["current_owner"] or None,
        }
        if cur.get("drc"):
            entry["k"] = {"d": cur["drc"], "c": cur.get("dollars")}
        if p.get("adp_2026") is not None:
            entry["a"] = round(p["adp_2026"], 1)
        y25, y24 = season_pack(2025), season_pack(2024)
        if y25: entry["y25"] = y25
        if y24: entry["y24"] = y24
        rows.append(entry)
    data_json = json.dumps(rows, separators=(",", ":"))
    return f"""
    <section class="team-section" id="player-compare" hidden>
      <header class="section-header">
        <h1 class="section-title">Player comparison</h1>
        <p class="section-sub">Line up to three players side by side: {TARGET_SEASON - 1} and {TARGET_SEASON - 2} production, current ownership, and the {TARGET_SEASON} keep cost. Bars share one scale across everyone compared, so height means the same thing in every column.</p>
      </header>
      <div class="pc-app">
        <div class="pc-top">
          <input type="search" id="pc-input" class="ps-input" placeholder="Type a player name to add (up to 3)&hellip;" autocomplete="off">
          <div class="pc-sugg" id="pc-sugg" hidden></div>
        </div>
        <div class="pc-cols" id="pc-cols"></div>
        <p class="kb-hint">Weekly bars cover weeks the player was on a league roster; position ranks are within players logged in league data. Rookies and never-rostered weeks show as gaps, not zeros. ADP is the {TARGET_SEASON} superflex/2-QB overall rank.</p>
      </div>
      <script>window.COMPARE_DATA = {data_json};</script>
    </section>"""


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
      <p class="kb-foot">Checkbox keeps auto-seat on your own open native pick and nothing else; every other placement is your conscious call. Seats are first-come: whoever you kept first holds his pick, and a later keep at the same DRC waits in Awaiting placement for you to place. Drag any player (from the roster or between board slots) and the legal landings light up: his DRC round, any earlier held pick, or the slide landing below a full native round (every round between must also be full &mdash; that&rsquo;s the slide rule doing the work, and the only path onto a below-native pick, acquired or not). Pick numbers like 10.02 read round 10, 2nd draft slot, straight from the published lottery order. Print / save PDF gives you a sheet to hold next to Yahoo&rsquo;s keeper page when you enter your real designations.</p>
    </section>"""


def render_draft_board():
    """League-wide 2026 draft board (Manager Tools): every pick of the
    linear draft in lottery order, round by round, naming the current
    holder of each pick with traded picks flagged. Optional keeper lens
    stacks each holder's roster players whose DRC lands in that round —
    an inventory view (who COULD sit there), never a prediction of who's
    kept. Reads the same TRADE_DATA embed as the analyzer and keeper
    board; client-side only."""
    return f"""
    <section class="team-section" id="draft-board" hidden>
      <header class="section-header">
        <h1 class="section-title">{TARGET_SEASON} draft board</h1>
        <p class="section-sub">Every pick in the {TARGET_SEASON} draft, laid out the way the draft will actually run: linear order from the published lottery, one card per round, traded picks sitting where they&rsquo;ll be made with a note on where they came from. Keepers are locked, so picks consumed by the keeper process show their keeper in green &mdash; auto-seated by the slide rules (own native pick, else the first free own pick down the held chain). The amber strip lists keepers still needing a manual slot call; check the final arrangement against Yahoo&rsquo;s keeper assignment screen before the draft.</p>
      </header>
      <div class="db26-app"></div>
      <p class="ta-foot">The draft is linear, no snake: a pick number like 3.07 reads round 3, 7th draft slot, and the slot follows the pick&rsquo;s ORIGINAL owner &mdash; a traded pick keeps its number and changes hands. Gold rows are picks that have moved. Each round&rsquo;s keeper cost is the DRC dollar figure a keeper seated in that round contributes to the pot. Trades of &ldquo;my last pick&rdquo; convey the giver&rsquo;s actual final selection &mdash; their highest remaining pick after all other trades settle &mdash; so those picks appear here under their real round and number.</p>
    </section>"""


def render_html(by_manager, search_players, comms_posts, generated_at, meta=None,
                offseason_trades=None):
    sidebar = build_sidebar(by_manager)
    offseason = render_offseason_trades_section(offseason_trades or [])
    summary = render_summary_section(by_manager, generated_at, meta)
    player_search = render_player_search_section(search_players)
    player_compare = render_player_compare(search_players)
    trade_analyzer, pick_data = render_trade_analyzer(by_manager)
    keeper_board = render_keeper_board()
    draft_board = render_draft_board()
    desk = render_commissioners_desk_section(comms_posts)
    rules = render_rules_section()
    rules_history = render_rules_history_section()
    about = render_about_section()
    feedback = render_feedback_widget()
    team_sections = "\n".join(
        render_team_section(data, manager_slug(data["manager_actual"]),
                            pick_data=pick_data)
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
{player_compare}
{trade_analyzer}
{keeper_board}
{draft_board}
{desk}
{offseason}
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


def _collect_update_meta():
    """Gather the three timestamps + recent-commit list surfaced in the
    footer's click-to-expand "last updated" widget. Every value is either
    a real datum or None; the widget template renders "unknown" for None.

    - generated_at: dashboard build time (this run), EDT
    - data_refreshed_at: fantasy.db mtime, EDT (last ingest wrote to it)
    - deploy_time / deploy_msg: HEAD commit's author date + subject
    - recent_commits: list of (date_str, subject) for the last 5 commits
    """
    import subprocess
    from datetime import datetime as _dt, timezone as _tz
    try:
        from zoneinfo import ZoneInfo
        edt = ZoneInfo("America/New_York")
    except Exception:
        edt = None  # very old python; fall back to naive local time

    def fmt(dt):
        if edt is not None and dt.tzinfo is not None:
            dt = dt.astimezone(edt)
        return dt.strftime("%Y-%m-%d %H:%M")

    meta = {"generated_at": fmt(_dt.now(_tz.utc) if edt else _dt.now()),
            "data_refreshed_at": None,
            "deploy_time": None, "deploy_msg": None,
            "recent_commits": []}

    # Data refresh: mtime of fantasy.db (updated by any ingest_*.py run).
    try:
        db_path = Path(__file__).parent / "fantasy.db"
        if db_path.exists():
            mtime = _dt.fromtimestamp(db_path.stat().st_mtime, tz=_tz.utc)
            meta["data_refreshed_at"] = fmt(mtime)
    except Exception:
        pass

    # Deploy info + recent activity from git — no-op if git unavailable
    # or repo missing (e.g. running from a zip export).
    def git(args):
        try:
            return subprocess.check_output(["git"] + args, cwd=Path(__file__).parent,
                                           stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return ""

    head = git(["log", "-1", "--format=%ci|%s"])
    if "|" in head:
        raw_time, msg = head.split("|", 1)
        try:
            # git %ci format: 2026-08-16 16:02:38 -0400
            dt = _dt.strptime(raw_time.strip(), "%Y-%m-%d %H:%M:%S %z")
            meta["deploy_time"] = fmt(dt)
            meta["deploy_msg"] = msg.strip()
        except ValueError:
            pass

    log = git(["log", "-5", "--format=%ai|%s"])
    for line in log.splitlines():
        if "|" not in line:
            continue
        raw_time, msg = line.split("|", 1)
        try:
            dt = _dt.strptime(raw_time.strip(), "%Y-%m-%d %H:%M:%S %z")
            meta["recent_commits"].append((fmt(dt), msg.strip()))
        except ValueError:
            continue

    return meta


def main():
    by_manager, failures, search_players, offseason_trades = build_data()
    comms_posts = load_comms_posts()
    meta = _collect_update_meta()
    generated_at = meta["generated_at"]
    html_out = render_html(by_manager, search_players, comms_posts,
                           generated_at, meta, offseason_trades)
    # Belt-and-suspenders privacy sweep. Structured renders route through
    # alias_name() already; this catches anything embedded from comms
    # bodies or third-party modules (trade_history, draft_history, etc.).
    html_out = sanitize_rendered_html(html_out)
    OUT_PATH.write_text(html_out, encoding="utf-8")

    print(f"Wrote {OUT_PATH}")
    total_players = sum(len(d["players"]) for d in by_manager.values())
    print(f"  {len(by_manager)} managers, {total_players} players, {len(failures)} failures")
    for mgr, name in failures:
        print(f"  FAILED: {mgr} - {name}")


if __name__ == "__main__":
    main()
