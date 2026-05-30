"""
trade_history.py - per-manager trade history with fantasy points outcomes.

For each manager, returns all trades they've made (both real Yahoo trades and
synthetic + override-tagged commish-pushed trades), enriched with per-year
fantasy points for each player involved.

Output shape per trade event:
    {
        'date': '2024-08-22',
        'date_display': 'Aug 22, 2024',
        'counterparty_name': 'Greg Pearson',
        'is_synthetic': False, 'is_override': False,
        'acquired': [{
            'player_id': 31002, 'name': 'CeeDee Lamb', 'position': 'WR', 'nfl_team': 'Dal',
            'points': {2023: {'full': 220.5, 'post_trade': None},
                       2024: {'full': 196.2, 'post_trade': 75.3},
                       2025: {'full': 110.4, 'post_trade': None}},
            'total_full': 527.1,
        }],
        'given_up': [ ... ],
        'subtotal_acquired': {2023: 220.5, 2024: 196.2, 2025: 110.4, 'total': 527.1},
        'subtotal_given_up': {...},
        'max_pts_per_year': {2023: 357.0, 2024: 448.8, 2025: 592.6},  # for bar scaling
    }
"""

import sqlite3
from datetime import datetime
from collections import defaultdict

YEARS = [2023, 2024, 2025]


def _format_date(date_str):
    """ISO YYYY-MM-DD -> 'Aug 22, 2024'."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %d, %Y")
        except Exception:
            return date_str


def _sum_points_while_on_team(conn, player_id, year, manager_team_ids):
    """Total fantasy points the player scored in `year` while on any of
    `manager_team_ids`. Used as a proxy for 'after the trade.'"""
    if not manager_team_ids:
        return None
    placeholders = ",".join("?" * len(manager_team_ids))
    result = conn.execute(
        f"SELECT SUM(fantasy_points) FROM player_weekly_stats "
        f"WHERE player_id = ? AND season = ? "
        f"  AND team_season_id IN ({placeholders})",
        (player_id, year, *manager_team_ids),
    ).fetchone()[0]
    return float(result) if result is not None else None


def _build_player_points(conn, player_id, trade_year, pts_by_year, manager_team_ids):
    """Build the year->points dict for one player on one side of one trade."""
    out = {}
    for year in YEARS:
        full = pts_by_year.get((player_id, year))
        post = None
        if year == trade_year:
            post = _sum_points_while_on_team(conn, player_id, year, manager_team_ids)
        elif year > trade_year:
            # After trade year - "post-trade" portion is just everything they scored
            # while on the manager's team across all later years
            post_year = _sum_points_while_on_team(conn, player_id, year, manager_team_ids)
            # Display only if different from full season
            if post_year is not None and full is not None and abs(post_year - full) > 0.1:
                post = post_year
        out[year] = {"full": float(full) if full is not None else None,
                     "post_trade": post}
    return out


def _enrich_player(conn, player_id, trade_year, pts_by_year, manager_team_ids):
    """Look up player metadata and per-year points."""
    row = conn.execute(
        "SELECT player_name, position, nfl_team FROM players WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if not row:
        name, pos, team = f"Player {player_id}", "—", "—"
    else:
        name, pos, team = row
    pts = _build_player_points(conn, player_id, trade_year, pts_by_year, manager_team_ids)
    total_full = sum(v["full"] for v in pts.values() if v["full"] is not None)
    return {
        "player_id": player_id,
        "name": name,
        "position": pos or "—",
        "nfl_team": team or "—",
        "points": pts,
        "total_full": round(total_full, 1),
    }


def _get_manager_name(conn, team_season_id):
    """Resolve team_season_id -> manager full_name."""
    row = conn.execute(
        "SELECT m.full_name FROM teams t JOIN managers m ON m.manager_id = t.manager_id "
        "WHERE t.team_season_id = ?",
        (team_season_id,),
    ).fetchone()
    return row[0] if row else "?"


def _compute_subtotals(players_list):
    """Sum full-season points per year across the given player list."""
    sub = {y: 0.0 for y in YEARS}
    for p in players_list:
        for y in YEARS:
            v = p["points"][y]["full"]
            if v is not None:
                sub[y] += v
    sub["total"] = round(sum(sub[y] for y in YEARS), 1)
    for y in YEARS:
        sub[y] = round(sub[y], 1)
    return sub


def _compute_max_pts_per_year(acquired, given_up):
    """For bar scaling: max points across all players (both sides) per year."""
    out = {}
    for y in YEARS:
        vals = []
        for p in acquired + given_up:
            v = p["points"][y]["full"]
            if v is not None:
                vals.append(v)
        out[y] = max(vals) if vals else 0.0
    return out


def _gather_real_and_synthetic_trades(conn, manager_team_ids_all):
    """Returns list of (trade_id, timestamp, acquired_player_rows, given_up_player_rows).

    Uses all_transactions + all_transaction_players (which already excludes
    vetoed trades and unions synthetic). Identifies which side of each trade
    the manager was on by checking team_season_id vs manager_team_ids_all."""
    placeholders = ",".join("?" * len(manager_team_ids_all))

    # Find every trade event where this manager was either side
    trade_ids = conn.execute(
        f"SELECT DISTINCT t.transaction_id, t.timestamp "
        f"FROM all_transactions t "
        f"JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        f"WHERE t.event_type = 'trade' "
        f"  AND (tp.team_season_id IN ({placeholders}) "
        f"       OR tp.counterparty_team_season_id IN ({placeholders})) "
        f"ORDER BY t.timestamp DESC",
        (*manager_team_ids_all, *manager_team_ids_all),
    ).fetchall()

    trades = []
    for tx_id, ts in trade_ids:
        # Acquired: player rows where THIS manager's team is the receiver
        acquired_rows = conn.execute(
            f"SELECT tp.player_id, tp.counterparty_team_season_id "
            f"FROM all_transaction_players tp "
            f"WHERE tp.transaction_id = ? "
            f"  AND tp.team_season_id IN ({placeholders})",
            (tx_id, *manager_team_ids_all),
        ).fetchall()

        # Given up: player rows where THIS manager's team is the counterparty (sender)
        given_rows = conn.execute(
            f"SELECT tp.player_id, tp.team_season_id "
            f"FROM all_transaction_players tp "
            f"WHERE tp.transaction_id = ? "
            f"  AND tp.counterparty_team_season_id IN ({placeholders})",
            (tx_id, *manager_team_ids_all),
        ).fetchall()
        trades.append({
            "trade_id": tx_id,
            "timestamp": ts,
            "acquired_rows": acquired_rows,
            "given_up_rows": given_rows,
        })
    return trades


def _gather_override_trades(conn, manager_team_ids_all):
    """Returns list of override-tagged commish-pushed trades. Groups overrides
    by (date, source_team, destination_team) pair to reconstruct multi-player
    trades that were executed via drop+add."""
    placeholders = ",".join("?" * len(manager_team_ids_all))

    rows = conn.execute(
        f"SELECT o.transaction_id, o.source_team_season_id, tp.team_season_id, "
        f"       tp.player_id, t.timestamp "
        f"FROM transaction_overrides o "
        f"JOIN transactions t ON t.transaction_id = o.transaction_id "
        f"JOIN transaction_players tp ON tp.transaction_id = o.transaction_id "
        f"WHERE o.override_type = 'trade_from' "
        f"  AND tp.direction = 'incoming' "
        f"  AND (tp.team_season_id IN ({placeholders}) "
        f"       OR o.source_team_season_id IN ({placeholders}))",
        (*manager_team_ids_all, *manager_team_ids_all),
    ).fetchall()

    # Group by (date_day, source_team, dest_team)
    groups = defaultdict(list)
    for tx_id, src_team, dst_team, pid, ts in rows:
        date_day = str(ts)[:10]
        groups[(date_day, src_team, dst_team)].append({
            "trade_id": tx_id,
            "timestamp": ts,
            "player_id": pid,
            "src_team": src_team,
            "dst_team": dst_team,
        })

    # Pair up groups: (date, A, B) + (date, B, A) = same logical trade
    seen = set()
    trades = []
    for key, group in groups.items():
        date_day, src, dst = key
        rev_key = (date_day, dst, src)
        if key in seen:
            continue
        seen.add(key)
        seen.add(rev_key)
        rev_group = groups.get(rev_key, [])

        # Determine which side is the manager's
        manager_is_dst = dst in manager_team_ids_all
        if manager_is_dst:
            acquired_rows = [(p["player_id"], p["src_team"]) for p in group]
            given_rows = [(p["player_id"], p["dst_team"]) for p in rev_group]
        else:
            acquired_rows = [(p["player_id"], p["src_team"]) for p in rev_group]
            given_rows = [(p["player_id"], p["dst_team"]) for p in group]

        trades.append({
            "trade_id": f"override-{group[0]['trade_id']}",
            "timestamp": group[0]["timestamp"],
            "is_override": True,
            "acquired_rows": acquired_rows,
            "given_up_rows": given_rows,
        })
    return trades


def _gather_picks_for_trade(conn, trade_id, manager_team_ids_all):
    """Returns (acquired_pick_rows, given_up_pick_rows) for picks moved in
    this trade where the manager was one side. Each row is shaped like a
    player entry so the renderer treats them uniformly."""
    if not manager_team_ids_all:
        return [], []
    # Synthetic transactions don't have picks in our schema. Skip those.
    if isinstance(trade_id, str) and trade_id.startswith("override-"):
        return [], []
    if isinstance(trade_id, int) and trade_id > 1000000:
        return [], []  # synthetic transaction_id range

    placeholders = ",".join("?" * len(manager_team_ids_all))
    acquired = []
    for r in conn.execute(
        f"SELECT draft_round FROM transaction_picks "
        f"WHERE transaction_id = ? "
        f"  AND destination_team_season_id IN ({placeholders})",
        (trade_id, *manager_team_ids_all),
    ):
        acquired.append(_pick_as_player_row(r[0]))
    given_up = []
    for r in conn.execute(
        f"SELECT draft_round FROM transaction_picks "
        f"WHERE transaction_id = ? "
        f"  AND source_team_season_id IN ({placeholders})",
        (trade_id, *manager_team_ids_all),
    ):
        given_up.append(_pick_as_player_row(r[0]))
    return acquired, given_up


def _pick_as_player_row(draft_round):
    """Shape a draft pick like a player row so render layer doesn't need to
    special-case it. Picks have no fantasy points."""
    return {
        "player_id": None,
        "name": f"Round {draft_round} draft pick",
        "position": "Pick",
        "nfl_team": "—",
        "points": {y: {"full": None, "post_trade": None} for y in YEARS},
        "total_full": 0.0,
        "is_pick": True,
    }


def _group_trades_by_day_and_counterparty(trades):
    """Combine trades that happened on the same day with the same counterparty
    into one logical trade event. Multi-player swaps that we stored as
    separate transactions (typical for synthetic trades) become one card."""
    grouped = {}
    for t in trades:
        key = (t["date"], t["counterparty_name"])
        if key in grouped:
            g = grouped[key]
            g["acquired"].extend(t["acquired"])
            g["given_up"].extend(t["given_up"])
            g["is_synthetic"] = g["is_synthetic"] or t["is_synthetic"]
            g["is_override"] = g["is_override"] or t["is_override"]
        else:
            grouped[key] = dict(t)
    out = []
    for g in grouped.values():
        g["subtotal_acquired"] = _compute_subtotals(g["acquired"])
        g["subtotal_given_up"] = _compute_subtotals(g["given_up"])
        g["max_pts_per_year"] = _compute_max_pts_per_year(g["acquired"], g["given_up"])
        out.append(g)
    out.sort(key=lambda t: t["date"], reverse=True)
    return out


def build_trade_history_for_manager(conn, manager_id, manager_team_ids_all, pts_by_year):
    """Returns a list of trade dicts for the dashboard, sorted most-recent first."""
    if not manager_team_ids_all:
        return []

    real = _gather_real_and_synthetic_trades(conn, manager_team_ids_all)
    overrides = _gather_override_trades(conn, manager_team_ids_all)

    all_trades = real + overrides
    all_trades.sort(key=lambda t: str(t["timestamp"]), reverse=True)

    out = []
    for tr in all_trades:
        ts = str(tr["timestamp"])
        date_iso = ts[:10]
        trade_year = int(date_iso[:4])

        counterparty_name = "?"
        if tr["acquired_rows"]:
            counterparty_name = _get_manager_name(conn, tr["acquired_rows"][0][1])
        elif tr["given_up_rows"]:
            counterparty_name = _get_manager_name(conn, tr["given_up_rows"][0][1])

        acquired = [
            _enrich_player(conn, pid, trade_year, pts_by_year, manager_team_ids_all)
            for pid, _src in tr["acquired_rows"]
        ]
        given_up = [
            _enrich_player(conn, pid, trade_year, pts_by_year, [])
            for pid, _src in tr["given_up_rows"]
        ]

        pick_acquired, pick_given = _gather_picks_for_trade(
            conn, tr["trade_id"], manager_team_ids_all
        )
        acquired.extend(pick_acquired)
        given_up.extend(pick_given)

        if not acquired and not given_up:
            continue

        out.append({
            "date": date_iso,
            "date_display": _format_date(date_iso),
            "counterparty_name": counterparty_name,
            "is_synthetic": bool(tr.get("is_synthetic")),
            "is_override": bool(tr.get("is_override")),
            "acquired": acquired,
            "given_up": given_up,
            "subtotal_acquired": _compute_subtotals(acquired),
            "subtotal_given_up": _compute_subtotals(given_up),
            "max_pts_per_year": _compute_max_pts_per_year(acquired, given_up),
        })
    return _group_trades_by_day_and_counterparty(out)
