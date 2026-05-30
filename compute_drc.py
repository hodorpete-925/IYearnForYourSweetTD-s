"""
compute_drc.py — Phase B: compute DRC for end-of-2025 rosters, looking forward
to the 2026 keeper-cost decision.

v4 changes:
- Consults `transaction_overrides` table to convert literal-waiver pickups
  into trades when the commissioner has manually flagged them.
- Corrected source DRC lookup: off-season trades query source's DRC for the
  YEAR PRIOR (since decrement hadn't been applied yet); mid-season trades
  query the trade year itself.
- find_most_recent_incoming now fetches transaction_id too.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "fantasy.db"
TARGET_SEASON = 2026

# Team lineage bridges: when a manager inherited a roster from a predecessor
# who left the league, search both the manager's own teams AND the predecessor's
# teams. This makes the backward-walk find original drafts/anchors for players
# carried over via the handoff.
#
# Handoff history in this league:
#   Vescuso left after 2023 -> Lewitus took over for 2024+
#   BRick left after 2024 -> Vescuso returned and took over for 2025+
HANDOFF_BRIDGES = {
    # Lewitus inherited Vescuso's 2023 roster (team 11)
    "WOCEV3POTYE7XLIJXO3CCNL56I": [11],
    # Vescuso (2025+) inherited BRick's full history (teams 10 in 2023, 16 in 2024)
    "NEIABZUWA773ZR2666V7TNMUNE": [10, 16],
}

def get_team_year(conn, team_season_id):
    row = conn.execute(
        "SELECT season FROM teams WHERE team_season_id = ?", (team_season_id,)
    ).fetchone()
    return row[0] if row else None


def get_manager_id(conn, team_season_id):
    row = conn.execute(
        "SELECT manager_id FROM teams WHERE team_season_id = ?", (team_season_id,)
    ).fetchone()
    return row[0] if row else None


def get_manager_team_ids(conn, manager_id):
    """All team_season_ids for a manager across all years, plus any
    additional team_season_ids inherited via handoff (see HANDOFF_BRIDGES)."""
    rows = conn.execute(
        "SELECT team_season_id FROM teams WHERE manager_id = ?", (manager_id,)
    ).fetchall()
    team_ids = [r[0] for r in rows]

    # If this manager inherited from a predecessor, extend the search set
    guid_row = conn.execute(
        "SELECT yahoo_guid FROM managers WHERE manager_id = ?", (manager_id,)
    ).fetchone()
    if guid_row and guid_row[0] in HANDOFF_BRIDGES:
        team_ids.extend(HANDOFF_BRIDGES[guid_row[0]])

    return team_ids

def find_most_recent_incoming(conn, player_id, team_id_set, before=None):
    """Returns (transaction_id, timestamp, source_type, counterparty_team_season_id) or None."""
    if not team_id_set:
        return None
    placeholders = ",".join("?" * len(team_id_set))
    sql = f"""
        SELECT tp.transaction_id, t.timestamp, tp.source_type, tp.counterparty_team_season_id
        FROM all_transactions t
        JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id
        WHERE tp.player_id = ?
          AND tp.team_season_id IN ({placeholders})
          AND tp.direction = 'incoming'
    """
    params = [player_id] + list(team_id_set)
    if before:
        sql += " AND t.timestamp < ?"
        params.append(before)
    sql += " ORDER BY t.timestamp DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def find_earliest_draft(conn, player_id, team_id_set=None):
    sql = "SELECT season, draft_round, team_season_id FROM draft_picks WHERE player_id = ?"
    params = [player_id]
    if team_id_set:
        placeholders = ",".join("?" * len(team_id_set))
        sql += f" AND team_season_id IN ({placeholders})"
        params.extend(team_id_set)
    sql += " ORDER BY season ASC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def get_keeper_status_override(conn, season, player_id, team_season_id):
    """Returns 1 / 0 if Pete has manually flagged this draft_picks row, else None."""
    row = conn.execute(
        "SELECT is_keeper FROM keeper_status_overrides "
        "WHERE season = ? AND player_id = ? AND team_season_id = ?",
        (season, player_id, team_season_id),
    ).fetchone()
    return row[0] if row else None


def find_anchor_draft(conn, player_id, team_id_set, max_season=None):
    """Find the most recent FRESH draft of this player by this manager.

    Walks draft_picks rows for the player+team set from newest to oldest. At
    each row, decides "is this a keeper or a fresh anchor?" via:
      1. keeper_status_overrides table (Pete's manual / Excel-driven truth)
      2. Inference fallback: was the same manager on last year's draft_picks
         for this player? If yes → keeper, walk back. If no → fresh anchor.

    Returns (season, draft_round, team_season_id) of the anchor, or None.
    """
    if not team_id_set:
        return None
    placeholders = ",".join("?" * len(team_id_set))
    sql = (
        f"SELECT season, draft_round, team_season_id "
        f"FROM draft_picks "
        f"WHERE player_id = ? AND team_season_id IN ({placeholders})"
    )
    params = [player_id] + list(team_id_set)
    if max_season is not None:
        sql += " AND season <= ?"
        params.append(max_season)
    sql += " ORDER BY season DESC"
    rows = list(conn.execute(sql, params))
    if not rows:
        return None

    for season, draft_round, ts_id in rows:
        override = get_keeper_status_override(conn, season, player_id, ts_id)
        if override is not None:
            if override == 0:
                # Pete says fresh — anchor here
                return (season, draft_round, ts_id)
            else:
                # Pete says kept — walk further back
                continue
        # No override: fall back to inference. Was the same manager on this
        # player's draft_picks last year too?
        prior = conn.execute(
            f"SELECT 1 FROM draft_picks "
            f"WHERE player_id = ? AND season = ? "
            f"  AND team_season_id IN ({placeholders}) LIMIT 1",
            [player_id, season - 1] + list(team_id_set),
        ).fetchone()
        if prior:
            # Inferred keeper, walk back
            continue
        # No prior year → must be fresh anchor
        return (season, draft_round, ts_id)

    # If we walked all the way back and every row was a keeper, the earliest
    # is the original anchor — but that's a logical paradox (a keeper has to
    # be kept FROM somewhere). Use the earliest row as the anchor anyway.
    return rows[-1]


def get_override(conn, transaction_id):
    """Returns (override_type, source_team_season_id) or None."""
    return conn.execute(
        "SELECT override_type, source_team_season_id FROM transaction_overrides WHERE transaction_id = ?",
        (transaction_id,)
    ).fetchone()


def is_mid_season_trade(timestamp_str):
    return int(timestamp_str[5:7]) >= 9


def compute_drc_for_year(starting_drc, anchor_year, query_year, is_mid_season=False):
    """Apply decrement rule from anchor to query year.
    For mid-season trades: freeze extends one year past anchor."""
    effective_anchor = anchor_year + 1 if is_mid_season else anchor_year
    if query_year <= effective_anchor:
        return starting_drc
    return max(starting_drc - (query_year - effective_anchor), 1)


def resolve_trade_source(source_type, counterparty_id, transaction_id, conn):
    """Given the raw source data, consult overrides and return the effective
    (source_type, counterparty_id, override_applied) tuple."""
    override = get_override(conn, transaction_id)
    if override:
        override_type, override_source = override
        if override_type == "trade_from" and override_source is not None:
            return "team", override_source, True
    return source_type, counterparty_id, False


def compute_drc(conn, player_id, team_season_id, depth=0):
    if depth > 5:
        return None

    manager_id = get_manager_id(conn, team_season_id)
    manager_team_ids = get_manager_team_ids(conn, manager_id)

    txn = find_most_recent_incoming(conn, player_id, manager_team_ids)
    if txn:
        transaction_id, timestamp, source_type, counterparty_id = txn
        source_type, counterparty_id, overridden = resolve_trade_source(
            source_type, counterparty_id, transaction_id, conn
        )
        anchor_year = int(timestamp[:4])
        override_tag = " [OVERRIDE]" if overridden else ""

        if source_type in ("waivers", "freeagents"):
            drc = compute_drc_for_year(16, anchor_year, TARGET_SEASON, is_mid_season=False)
            label = "waiver_pickup" if anchor_year == TARGET_SEASON else "kept"
            return drc, label, f"waiver from {source_type} in {anchor_year}{override_tag}"

        elif source_type == "team" and counterparty_id is not None:
            mid_season = is_mid_season_trade(timestamp)
            # Off-season: source's trade-time DRC is the year-prior value
            # Mid-season: source's trade-time DRC is the trade-year value
            source_query_year = anchor_year if mid_season else anchor_year - 1
            source = compute_drc_at_time(conn, player_id, counterparty_id, timestamp, source_query_year, depth + 1)
            if source is None:
                source_drc, source_note = 16, "lookback failed"
            else:
                source_drc, _, source_note = source

            drc = compute_drc_for_year(source_drc, anchor_year, TARGET_SEASON, is_mid_season=mid_season)
            label = "trade_acquired" if anchor_year == TARGET_SEASON else "kept"
            season_tag = "mid-season" if mid_season else "off-season"
            return drc, label, f"{season_tag} trade in {anchor_year} ({source_note}){override_tag}"

    # Fallback: most recent FRESH draft by this manager (override-aware)
    draft = find_anchor_draft(conn, player_id, team_id_set=manager_team_ids)
    if draft:
        season, draft_round, _ = draft
        drc = compute_drc_for_year(draft_round, season, TARGET_SEASON, is_mid_season=False)
        label = "drafted" if season == TARGET_SEASON else "kept"
        return drc, label, f"originally drafted round {draft_round} in {season}"

    return None


def compute_drc_at_time(conn, player_id, team_season_id, before_timestamp, query_year, depth):
    """Compute a team's DRC for a specific year, looking at events before before_timestamp.
    Also consults override table on recursive lookups."""
    if depth > 5:
        return None

    manager_id = get_manager_id(conn, team_season_id)
    manager_team_ids = get_manager_team_ids(conn, manager_id)

    txn = find_most_recent_incoming(conn, player_id, manager_team_ids, before=before_timestamp)
    if txn:
        transaction_id, timestamp, source_type, counterparty_id = txn
        source_type, counterparty_id, _ = resolve_trade_source(
            source_type, counterparty_id, transaction_id, conn
        )
        anchor_year = int(timestamp[:4])

        if source_type in ("waivers", "freeagents"):
            drc = compute_drc_for_year(16, anchor_year, query_year, is_mid_season=False)
            return drc, "waiver", f"waiver in {anchor_year}"
        elif source_type == "team" and counterparty_id is not None:
            mid_season = is_mid_season_trade(timestamp)
            source_query_year = anchor_year if mid_season else anchor_year - 1
            source = compute_drc_at_time(conn, player_id, counterparty_id, timestamp, source_query_year, depth + 1)
            if source is None:
                return 16, "fallback", "lookback failed"
            source_drc, _, source_note = source
            drc = compute_drc_for_year(source_drc, anchor_year, query_year, is_mid_season=mid_season)
            return drc, "trade", f"trade in {anchor_year} ({source_note})"

    draft = find_anchor_draft(conn, player_id, team_id_set=manager_team_ids,
                              max_season=query_year)
    if draft:
        season, draft_round, _ = draft
        drc = compute_drc_for_year(draft_round, season, query_year, is_mid_season=False)
        return drc, "draft", f"drafted round {draft_round} in {season}"

    return None


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    rosters = conn.execute("""
        SELECT fr.player_id, fr.team_season_id, p.player_name, m.full_name AS manager
        FROM final_rosters fr
        JOIN players p ON fr.player_id = p.player_id
        JOIN teams t ON fr.team_season_id = t.team_season_id
        JOIN managers m ON t.manager_id = m.manager_id
        WHERE fr.season = 2025
        ORDER BY m.full_name, p.player_name
    """).fetchall()

    print(f"Computing DRC FOR {TARGET_SEASON} (keeper cost going into {TARGET_SEASON})\n")
    print(f"Processing {len(rosters)} player-roster rows...\n")

    by_manager = {}
    failures = []
    for player_id, team_season_id, player_name, manager in rosters:
        result = compute_drc(conn, player_id, team_season_id)
        if result is None:
            failures.append((manager, player_name))
            continue
        drc, event, note = result
        by_manager.setdefault(manager, []).append((player_name, drc, event, note))

    for manager in sorted(by_manager.keys()):
        print(f"\n=== {manager} — DRC for {TARGET_SEASON} ===\n")
        for player_name, drc, event, note in sorted(by_manager[manager], key=lambda x: (x[1], x[0])):
            print(f"  DRC {drc:>2}  {player_name:<25}  [{event}]  // {note}")

    total = sum(len(v) for v in by_manager.values())
    print(f"\nTotal: {total} computed, {len(failures)} failures.")
    for manager, name in failures:
        print(f"  FAILED: {manager} — {name}")

    conn.close()


if __name__ == "__main__":
    main()