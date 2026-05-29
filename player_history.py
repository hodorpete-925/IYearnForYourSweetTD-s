"""
player_history.py - per-year DRC + ADP + (eventually) stats for a player.

For each player on a manager's 2025 final roster, build a per-year history
record for years 2023-2026:

    {
        2023: {"drc": 4, "draft_round": 4, "owner_team_id": 11, "adp": 39.3, "pts": None, "pos_rank": None},
        2024: {"drc": 4, "draft_round": None, "owner_team_id": 24, "adp": 51.0, ...},
        2025: {"drc": 3, ...},
        2026: {"drc": 2, ...},
    }

Year keys may map to None when the player wasn't yet in our league that year.

Reuses compute_drc_at_time from compute_drc.py.
"""

import sqlite3

import compute_drc as drc


def get_owner_at_year_end(conn, player_id, year):
    """Return team_season_id that owned this player at end of `year`, or None.

    Looks at all incoming transactions for this player through Dec 31 of
    `year`. The most recent incoming = current owner at year-end. Falls back
    to draft_picks if no transaction history yet."""
    row = conn.execute("""
        SELECT tp.team_season_id
        FROM all_transactions t
        JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id
        WHERE tp.player_id = ?
          AND tp.direction = 'incoming'
          AND DATE(t.timestamp) <= ?
        ORDER BY t.timestamp DESC
        LIMIT 1
    """, (player_id, f"{year}-12-31")).fetchone()
    if row:
        return row[0]

    # Fallback: drafted that year but no transaction (original draft pick).
    row = conn.execute("""
        SELECT team_season_id FROM draft_picks
        WHERE player_id = ? AND season <= ?
        ORDER BY season DESC LIMIT 1
    """, (player_id, year)).fetchone()
    return row[0] if row else None


def get_draft_round_in_year(conn, player_id, year):
    """If the player was originally drafted (is_keeper=0) in `year`, return
    the round. Otherwise None.  Note: is_keeper is unreliable in our data;
    we use season-of-first-draft as the truth signal."""
    row = conn.execute("""
        SELECT draft_round, is_keeper FROM draft_picks
        WHERE player_id = ? AND season = ?
        ORDER BY overall_pick ASC LIMIT 1
    """, (player_id, year)).fetchone()
    if not row:
        return None
    draft_round, is_keeper = row
    # Find earliest draft year - if this year is the earliest, it's the original draft
    earliest = conn.execute(
        "SELECT MIN(season) FROM draft_picks WHERE player_id = ?", (player_id,)
    ).fetchone()[0]
    return draft_round if year == earliest else None


def compute_drc_for_year(conn, player_id, owner_team_id, year):
    """Compute DRC for this player+year via compute_drc_at_time. Uses
    Dec 31 of the year as the as-of timestamp so all events from that year
    are included.

    Returns (drc, label, chain_note) or None if the player has no chain."""
    if owner_team_id is None:
        return None
    return drc.compute_drc_at_time(
        conn,
        player_id,
        owner_team_id,
        before_timestamp=f"{year + 1}-01-01 00:00:00",  # everything up to year-end
        query_year=year,
        depth=0,
    )


def build_history_for_player(conn, player_id, current_team_season_id,
                             adp_by_year, pts_by_year, pos_rank_by_year,
                             player_position=None):
    """Return {2023: {...}, 2024: {...}, 2025: {...}, 2026: {...}}.

    For years before the player was acquired in our league, returns None
    for that year key.

    adp_by_year, pts_by_year, pos_rank_by_year are pre-built dicts:
        adp_by_year = {(player_id, year): adp_value}
        pts_by_year = {(player_id, year): season_total_pts}
        pos_rank_by_year = {(player_id, year): rank_int}

    player_position is the position code (e.g. 'WR') used for display.
    """
    history = {}
    for year in (2023, 2024, 2025, 2026):
        # Determine ownership for this year. For 2026, the player is on
        # current_team_season_id (the 2025 final-roster team_season_id rolls
        # forward to 2026 by default).
        if year == 2026:
            owner = current_team_season_id
        else:
            owner = get_owner_at_year_end(conn, player_id, year)

        if owner is None:
            history[year] = None
            continue

        drc_result = compute_drc_for_year(conn, player_id, owner, year)
        if drc_result is None:
            history[year] = None
            continue
        drc_int, _label, chain_note = drc_result

        # If the player was originally drafted IN this year, surface the round
        # for display (so we can show "R4" rather than DRC for the draft year).
        draft_round_this_year = get_draft_round_in_year(conn, player_id, year)

        history[year] = {
            "drc": drc_int,
            "draft_round": draft_round_this_year,
            "owner_team_id": owner,
            "adp": adp_by_year.get((player_id, year)),
            "pts": pts_by_year.get((player_id, year)),
            "pos_rank": pos_rank_by_year.get((player_id, year)),
            "position": player_position,
            "chain_note": chain_note,
        }
    return history


def load_adp_by_year(conn):
    """Pre-load ADP for all (player_id, year) pairs."""
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT player_id, season, adp FROM adp "
            "WHERE player_id IS NOT NULL"
        )
    }


def load_season_points_by_player(conn):
    """Pre-load total fantasy points per (player_id, season).
    Returns empty dict if player_weekly_stats is empty (backfill not done yet)."""
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT player_id, season, SUM(fantasy_points) "
            "FROM player_weekly_stats WHERE fantasy_points IS NOT NULL "
            "GROUP BY player_id, season"
        )
    }


def load_position_rank_by_player(conn):
    """Pre-load position rank per (player_id, season). Ranks are
    computed within (season, position) ordered by season total points
    descending. Returns empty dict if stats aren't ingested yet."""
    rows = list(conn.execute("""
        SELECT pws.season, p.position, pws.player_id,
               SUM(pws.fantasy_points) AS total_pts
        FROM player_weekly_stats pws
        JOIN players p ON p.player_id = pws.player_id
        WHERE pws.fantasy_points IS NOT NULL
        GROUP BY pws.season, p.position, pws.player_id
        ORDER BY pws.season, p.position, total_pts DESC
    """))
    out = {}
    current_key = (None, None)
    rank = 0
    for season, position, player_id, total in rows:
        key = (season, position)
        i