"""
draft_history.py - per-manager draft history.

For each manager, returns their draft picks across all seasons, enriched
with player metadata, keeper status, DRC if keeper, ADP for that year, and
a value/reach indicator vs ADP.

Output shape:
    {
        2023: [
            {
                "overall_pick": 7, "draft_round": 1, "pick_in_round": 7,
                "pick_label": "1.07",
                "player_id": ..., "player_name": "Christian McCaffrey",
                "position": "RB", "nfl_team": "SF",
                "is_keeper": True, "drc": 1, "slid": False,
                "adp": 6.2,
                "value_tag": "fair",  # 'steal' | 'fair' | 'reach' | 'major-reach'
            },
            ...
        ],
        2024: [...],
        2025: [...],
    }
"""

import sqlite3


def _pick_label(draft_round, pick_in_round):
    return f"{draft_round}.{pick_in_round:02d}"


def _value_tag(overall_pick, adp):
    """Compare draft slot to ADP. Returns one of:
        'steal' (drafted late vs ADP), 'fair', 'reach', 'major-reach'.

    Threshold: 12 picks = ~1 round in a 12-team league.
    """
    if adp is None:
        return ""
    delta = overall_pick - adp   # positive: drafted LATER than ADP (steal)
    if delta > 12:
        return "steal"
    if delta < -24:
        return "major-reach"
    if delta < -6:
        return "reach"
    return "fair"


def is_player_keeper_in_year(conn, player_id, manager_id, year, manager_team_ids_all):
    """Determine if a player was kept (vs newly drafted) in `year`.

    A player is a 'keeper' in year Y if they were on ANY roster at week 17
    of year Y-1. This covers both:
      - Players the manager kept directly (same team last year)
      - Players the manager acquired in the off-season via trade and kept

    A truly NEW pick is someone who wasn't owned at end of prior season."""
    prev_year = year - 1
    on_any_roster = conn.execute(
        "SELECT 1 FROM player_weekly_stats "
        "WHERE player_id = ? AND season = ? AND week = 17 "
        "  AND team_season_id IS NOT NULL LIMIT 1",
        (player_id, prev_year),
    ).fetchone()
    return on_any_roster is not None


def build_drc_trajectory(conn, player_id, owner_team_id, draft_year):
    """For a keeper pick in `draft_year`, build the DRC progression list
    from the player's origin (first appearance in our league) up to and
    including draft_year.

    Returns list of (year, label) tuples where label is:
        'R15'   - original draft round (year of first draft)
        'W'     - original waiver pickup
        '14'    - DRC value in a subsequent kept year

    Example for Kyren Williams kept twice after 2023 waiver:
        [(2023, 'W'), (2024, '15'), (2025, '14')]
    """
    import compute_drc as drc

    # Find origin year - earliest year the player appears in either
    # draft_picks or in any of this owner's transactions.
    earliest_draft = conn.execute(
        "SELECT MIN(season) FROM draft_picks WHERE player_id = ?",
        (player_id,),
    ).fetchone()[0]
    earliest_txn = conn.execute(
        "SELECT MIN(strftime('%Y', t.timestamp)) "
        "FROM all_transactions t "
        "JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        "WHERE tp.player_id = ? AND tp.direction = 'incoming'",
        (player_id,),
    ).fetchone()[0]
    origin_year = None
    if earliest_draft and earliest_txn:
        origin_year = min(int(earliest_draft), int(earliest_txn))
    elif earliest_draft:
        origin_year = int(earliest_draft)
    elif earliest_txn:
        origin_year = int(earliest_txn)
    if origin_year is None or origin_year > draft_year:
        return []

    # Walk the player's actual lineage year-by-year. The owner can change
    # mid-stream via trades, so we look up who actually held the player at
    # end of each year and compute DRC under THAT owner. Pete's view of a
    # player he traded for must still surface the original drafter's year-1
    # round to render meaningful R-prefix.
    import player_history as ph

    trajectory = []
    for y in range(origin_year, draft_year + 1):
        actual_owner = ph.get_owner_at_year_end(conn, player_id, y)
        if actual_owner is None:
            continue
        result = drc.compute_drc_at_time(
            conn, player_id, actual_owner,
            before_timestamp=f"{y}-12-31 23:59:59",
            query_year=y, depth=0,
        )
        if result is None:
            continue
        drc_val, _label, note = result
        # First-appearance labeling: distinguish original draft from waiver
        if y == origin_year:
            if "drafted" in (note or ""):
                # original draft round = drc_val at origin
                trajectory.append((y, f"R{drc_val}"))
            else:
                trajectory.append((y, "W"))
        else:
            trajectory.append((y, str(drc_val)))
    return trajectory


def detect_acquired_via_trade(conn, player_id, manager_team_ids_all,
                              before_date, after_date):
    """True if the most recent incoming acquisition for this player by this
    manager (across handoff bridges) **within the window [after_date, before_date]**
    was a TRADE (vs draft or waiver).

    The window matters: a player kept by this manager last year (with the
    original trade two years ago) is a keeper, not a 'traded for' acquisition.
    Only when there's a fresh trade in the past year do we light the pill.

    Trade-away-then-back is handled automatically — the re-acquisition trade
    falls inside the window and re-lights the pill.

    Checks three signals against the most-recent in-window event:
      1. all_transactions.event_type = 'trade' (real Yahoo trade)
      2. all_transaction_players.source_type = 'team' (synthetic trade view)
      3. transaction_overrides.override_type = 'trade_from' (commish-pushed
         drop+add that we tagged as a trade)
    """
    if not manager_team_ids_all:
        return False
    placeholders = ",".join("?" * len(manager_team_ids_all))
    row = conn.execute(
        f"SELECT t.transaction_id, t.event_type, tp.source_type "
        f"FROM all_transactions t "
        f"JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        f"WHERE tp.player_id = ? "
        f"  AND tp.team_season_id IN ({placeholders}) "
        f"  AND tp.direction = 'incoming' "
        f"  AND DATE(t.timestamp) >= ? "
        f"  AND DATE(t.timestamp) <= ? "
        f"ORDER BY t.timestamp DESC LIMIT 1",
        (player_id, *manager_team_ids_all, after_date, before_date),
    ).fetchone()
    if not row:
        return False
    tx_id, event_type, source_type = row
    if event_type == "trade" or source_type == "team":
        return True
    # Check overrides: this transaction may have been tagged as a commish-
    # pushed trade even though Yahoo recorded it as a drop+add.
    override_row = conn.execute(
        "SELECT 1 FROM transaction_overrides "
        "WHERE transaction_id = ? AND override_type = 'trade_from' LIMIT 1",
        (tx_id,),
    ).fetchone()
    return override_row is not None


def build_transaction_log_for_player(conn, player_id, before_date):
    """Build a chronological list of every event for this player up to
    `before_date`. Each entry is a dict with date, kind, description.

    Includes: draft picks, all transactions (trades, drops, adds, overrides,
    synthetic trades via the unioned views)."""
    events = []

    # Draft pick origins. Yahoo records every kept player in draft_picks (with
    # the slide-rule round). So the FIRST draft_picks row for a player is the
    # original draft event; subsequent rows are keeper allocations (the manager
    # paying the DRC cost to keep them, slotted into a round via the slide rule).
    draft_rows = list(conn.execute(
        "SELECT dp.season, dp.draft_round, m.full_name "
        "FROM draft_picks dp "
        "LEFT JOIN teams t ON t.team_season_id = dp.team_season_id "
        "LEFT JOIN managers m ON m.manager_id = t.manager_id "
        "WHERE dp.player_id = ? AND dp.season <= ? "
        "ORDER BY dp.season",
        (player_id, int(before_date[:4])),
    ))
    for i, r in enumerate(draft_rows):
        season, dround, mgr = r
        if i == 0:
            desc = f"Drafted R{dround} by {mgr or '?'}"
            kind = "draft"
        else:
            desc = f"Kept by {mgr or '?'} (slot R{dround})"
            kind = "kept"
        events.append({
            "date": f"{season}-08-25",
            "kind": kind,
            "desc": desc,
        })

    # Transactions (real + synthetic via union views)
    for r in conn.execute(
        "SELECT t.timestamp, t.event_type, tp.source_type, tp.destination_type, "
        "       md.full_name AS dest_mgr, ms.full_name AS src_mgr "
        "FROM all_transactions t "
        "JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        "LEFT JOIN teams td ON td.team_season_id = tp.team_season_id "
        "LEFT JOIN managers md ON md.manager_id = td.manager_id "
        "LEFT JOIN teams ts ON ts.team_season_id = tp.counterparty_team_season_id "
        "LEFT JOIN managers ms ON ms.manager_id = ts.manager_id "
        "WHERE tp.player_id = ? "
        "  AND tp.direction = 'incoming' "
        "  AND DATE(t.timestamp) <= ? "
        "ORDER BY t.timestamp",
        (player_id, before_date),
    ):
        ts, evt, src_type, dst_type, dest_mgr, src_mgr = r
        if evt == "trade" or src_type == "team":
            desc = f"Traded {src_mgr or '?'} → {dest_mgr or '?'}"
        elif src_type == "waivers":
            desc = f"Waiver claim by {dest_mgr or '?'}"
        elif src_type == "freeagents":
            desc = f"Free agent add by {dest_mgr or '?'}"
        else:
            desc = f"{evt} by {dest_mgr or '?'}"
        events.append({
            "date": str(ts)[:10],
            "kind": evt,
            "desc": desc,
        })

    events.sort(key=lambda e: e["date"])
    return events


def compute_keeper_drc_at_draft(conn, player_id, owner_team_id, year):
    """For a keeper in `year`, compute their DRC tier going into that draft.
    Uses compute_drc_at_time with timestamp = Aug 1 of `year` (pre-draft).
    Returns int or None."""
    import compute_drc as drc
    if owner_team_id is None:
        return None
    result = drc.compute_drc_at_time(
        conn, player_id, owner_team_id,
        before_timestamp=f"{year}-08-01 00:00:00",
        query_year=year, depth=0,
    )
    if result is None:
        return None
    drc_int, _label, _note = result
    return drc_int


def get_manager_team_ids_by_year(conn, manager_id):
    """Return {year: team_season_id} for this manager."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT season, team_season_id FROM teams WHERE manager_id = ?",
            (manager_id,),
        )
    }


def build_draft_history_for_manager(conn, manager_id, adp_by_year):
    """For each season, return the picks this manager made (enriched)."""
    import compute_drc as drc
    team_by_year = get_manager_team_ids_by_year(conn, manager_id)
    manager_team_ids_all = drc.get_manager_team_ids(conn, manager_id)
    out = {}
    for year, team_season_id in sorted(team_by_year.items()):
        rows = conn.execute(
            "SELECT dp.overall_pick, dp.draft_round, dp.pick_in_round, "
            "       dp.player_id, p.player_name, p.position, p.nfl_team "
            "FROM draft_picks dp "
            "JOIN players p ON p.player_id = dp.player_id "
            "WHERE dp.team_season_id = ? AND dp.season = ? "
            "ORDER BY dp.overall_pick",
            (team_season_id, year),
        ).fetchall()

        picks = []
        for r in rows:
            overall, dround, pir, pid, name, pos, team = r
            is_keeper = is_player_keeper_in_year(
                conn, pid, manager_id, year, manager_team_ids_all
            )
            drc_val = None
            slid = False
            trajectory = []
            if is_keeper:
                drc_val = compute_keeper_drc_at_draft(conn, pid, team_season_id, year)
                if drc_val is not None and dround > drc_val:
                    slid = True
                trajectory = build_drc_trajectory(conn, pid, team_season_id, year)

            # Window: between last year's draft (~9/01 of year-1) and this
            # year's draft (~9/01 of year). The "Traded for" pill should only
            # light up if the player was acquired in the past year — a player
            # traded for two seasons ago and kept since is just a keeper now.
            after_date = f"{year - 1}-09-01"
            before_date = f"{year}-09-01"
            acquired_via_trade = detect_acquired_via_trade(
                conn, pid, manager_team_ids_all, before_date, after_date
            ) if is_keeper else False
            txn_log = build_transaction_log_for_player(conn, pid, before_date)

            adp = adp_by_year.get((pid, year))
            value_tag = _value_tag(overall, adp)

            picks.append({
                "overall_pick": overall,
                "draft_round": dround,
                "pick_in_round": pir,
                "pick_label": _pick_label(dround, pir),
                "player_id": pid,
                "player_name": name,
                "position": pos,
                "nfl_team": team,
                "is_keeper": is_keeper,
                "acquired_via_trade": acquired_via_trade,
                "drc": drc_val,
                "slid": slid,
                "trajectory": trajectory,
                "txn_log": txn_log,
                "adp": adp,
                "value_tag": value_tag,
            })
        out[year] = picks
    return out
