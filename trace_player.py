"""trace_player.py - dump a player's full year-by-year history to debug a
suspicious DRC progression. Run from the project root:

    python trace_player.py                       # defaults to James Conner
    python trace_player.py "Davante Adams"       # trace any player by name
    python trace_player.py Lamar                 # partial match works
"""

import sqlite3
import sys

# Ensure we can import compute_drc / player_history from the same dir
sys.path.insert(0, ".")
import compute_drc as drc
import player_history as ph

DB = "fantasy.db"
DEFAULT_PLAYER = "James Conner"
PLAYER_NAME_LIKE = f"%{sys.argv[1]}%" if len(sys.argv) > 1 else f"%{DEFAULT_PLAYER}%"
YEARS = (2023, 2024, 2025, 2026)


def hr(char="=", width=70):
    print(char * width)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT player_id, player_name, position, nfl_team FROM players "
        "WHERE player_name LIKE ?",
        (PLAYER_NAME_LIKE,),
    ).fetchone()
    if not row:
        print(f"No player matching {PLAYER_NAME_LIKE}")
        return
    pid = row["player_id"]
    print(f"Player: {row['player_name']} ({row['position']}, {row['nfl_team']}) "
          f"id={pid}\n")

    # ----- 1. Every draft_picks row for this player ------------------------
    hr()
    print("DRAFT_PICKS rows (origin = first row, rest = keeper allocations):")
    hr()
    for r in conn.execute("""
        SELECT dp.season, dp.draft_round, dp.pick_in_round, dp.overall_pick,
               dp.is_keeper, m.full_name AS owner
        FROM draft_picks dp
        LEFT JOIN teams t ON t.team_season_id = dp.team_season_id
        LEFT JOIN managers m ON m.manager_id = t.manager_id
        WHERE dp.player_id = ?
        ORDER BY dp.season
    """, (pid,)):
        print(f"  {r['season']}: R{r['draft_round']}.{r['pick_in_round']:02d} "
              f"(overall {r['overall_pick']:>3})  owner={r['owner']}  "
              f"is_keeper_yahoo={r['is_keeper']}")

    # ----- 2. Every transaction touching this player -----------------------
    hr()
    print("TRANSACTIONS (incoming events, real + synthetic):")
    hr()
    for r in conn.execute("""
        SELECT t.timestamp, t.event_type, t.is_synthetic,
               tp.source_type, tp.destination_type,
               md.full_name AS dest_mgr, ms.full_name AS src_mgr
        FROM all_transactions t
        JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id
        LEFT JOIN teams td ON td.team_season_id = tp.team_season_id
        LEFT JOIN managers md ON md.manager_id = td.manager_id
        LEFT JOIN teams ts ON ts.team_season_id = tp.counterparty_team_season_id
        LEFT JOIN managers ms ON ms.manager_id = ts.manager_id
        WHERE tp.player_id = ? AND tp.direction = 'incoming'
        ORDER BY t.timestamp
    """, (pid,)):
        synth = " [SYNTH]" if r["is_synthetic"] else ""
        print(f"  {r['timestamp']}  {r['event_type']}{synth}: "
              f"{r['src_mgr'] or '—'} -> {r['dest_mgr'] or '—'}  "
              f"(src_type={r['source_type']}, dst_type={r['destination_type']})")

    # Overrides
    hr()
    print("TRANSACTION_OVERRIDES touching this player:")
    hr()
    for r in conn.execute("""
        SELECT o.transaction_id, o.override_type, o.note,
               t.timestamp,
               ms.full_name AS override_src_mgr
        FROM transaction_overrides o
        JOIN transactions t ON t.transaction_id = o.transaction_id
        JOIN transaction_players tp ON tp.transaction_id = o.transaction_id
        LEFT JOIN teams ts ON ts.team_season_id = o.source_team_season_id
        LEFT JOIN managers ms ON ms.manager_id = ts.manager_id
        WHERE tp.player_id = ?
    """, (pid,)):
        print(f"  txn {r['transaction_id']} ({r['timestamp']}): "
              f"{r['override_type']} from {r['override_src_mgr']} "
              f"-- {r['note'] or ''}")

    # ----- 3. Year-by-year DRC resolution ---------------------------------
    hr()
    print("DRC ENGINE RESOLUTION YEAR-BY-YEAR:")
    hr()
    for year in YEARS:
        print(f"\n--- {year} ---")
        if year == 2026:
            # 2026 owner = end-of-2025 owner (rolls forward)
            owner = ph.get_owner_at_year_end(conn, pid, 2025)
        else:
            owner = ph.get_owner_at_year_end(conn, pid, year)
        if owner is None:
            print("  no owner this year")
            continue
        mgr = conn.execute("""
            SELECT m.full_name FROM teams t
            JOIN managers m ON m.manager_id = t.manager_id
            WHERE t.team_season_id = ?
        """, (owner,)).fetchone()
        print(f"  owner team_season_id={owner} ({mgr[0] if mgr else '?'})")

        result = drc.compute_drc_at_time(
            conn, pid, owner,
            before_timestamp=f"{year + 1}-01-01 00:00:00",
            query_year=year, depth=0,
        )
        if result is None:
            print("  compute_drc_at_time returned None")
        else:
            drc_val, label, note = result
            dollars = conn.execute(
                "SELECT drc_dollars FROM drc_dollar_lookup WHERE drc = ?",
                (drc_val,),
            ).fetchone()
            dollar_str = f"${dollars[0]}" if dollars else "?"
            print(f"  DRC = {drc_val} ({dollar_str})   label = {label}")
            print(f"  chain_note = {note}")


if __name__ == "__main__":
    main()
