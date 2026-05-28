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
    rows = conn.execute(
        "SELECT team_season_id FROM teams WHERE manager_id = ?", (manager_id,)
    ).fetchall()
    return [r[0] for r in rows]


def find_most_recent_incoming(conn, player_id, team_id_set, before=None):
    """Returns (transaction_id, timestamp, source_type, counterparty_team_season_id) or None."""
    if not team_id_set:
        return None
    placeholders = ",".join("?" * len(team_id_set))
    sql = f"""
        SELECT tp.transaction_id, t.timestamp, tp.source_type, tp.counterparty_team_season_id
        FROM transactions t
        JOIN transaction_players tp ON tp.transaction_id = t.transaction_id
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

    # Fallback: original draft by this manager
    draft = find_earliest_draft(conn, player_id, team_id_set=manager_team_ids)
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

    draft = find_earliest_draft(conn, player_id, team_id_set=manager_team_ids)
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

    print(f"=== Pete Hodor (Are Bonita Fish Big?) — DRC for {TARGET_SEASON} ===\n")
    for player_name, drc, event, note in sorted(by_manager.get("Pete Hodor", []),
                                                 key=lambda x: (x[1], x[0])):
        print(f"  DRC {drc:>2}  {player_name:<25}  [{event}]  // {note}")

    total = sum(len(v) for v in by_manager.values())
    print(f"\nTotal: {total} computed, {len(failures)} failures.")
    for manager, name in failures:
        print(f"  FAILED: {manager} — {name}")

    conn.close()


if __name__ == "__main__":
    main()