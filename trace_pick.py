"""trace_pick.py - trace the 2nd round pick Pete sent to George in the Darnold
trade to whatever player George ended up drafting with it.

Adjust the constants at the top if any of the name searches don't match.
Run from the project root:

    python trace_pick.py
"""

import sqlite3

DB = "fantasy.db"

# Adjust these patterns if needed - they're SQL LIKE patterns
PETE_NAME_LIKE = "%Pete%"
GEORGE_NAME_LIKE = "%George%"
TARGET_PLAYER_LIKE = "%Darnold%"
PICK_ROUND = 2  # the round of the pick we're tracing


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ----- 1. Resolve manager IDs ------------------------------------------
    pete = conn.execute(
        "SELECT manager_id, full_name FROM managers WHERE full_name LIKE ?",
        (PETE_NAME_LIKE,),
    ).fetchone()
    george = conn.execute(
        "SELECT manager_id, full_name FROM managers WHERE full_name LIKE ?",
        (GEORGE_NAME_LIKE,),
    ).fetchone()
    if not pete or not george:
        print("Couldn't resolve one of the managers. Adjust *_NAME_LIKE.")
        return
    print(f"Pete:   {pete['full_name']} (manager_id={pete['manager_id']})")
    print(f"George: {george['full_name']} (manager_id={george['manager_id']})")

    # ----- 2. Find the target player ---------------------------------------
    target = conn.execute(
        "SELECT player_id, player_name FROM players WHERE player_name LIKE ?",
        (TARGET_PLAYER_LIKE,),
    ).fetchone()
    if not target:
        print(f"No player matching {TARGET_PLAYER_LIKE}")
        return
    print(f"\nTarget player: {target['player_name']} (id={target['player_id']})")

    # ----- 3. Find Pete↔George trades involving the target player ----------
    trades = list(conn.execute("""
        SELECT DISTINCT t.transaction_id, t.timestamp, t.event_type, t.status, t.season
        FROM transactions t
        JOIN transaction_players tp ON tp.transaction_id = t.transaction_id
        JOIN teams td ON td.team_season_id = tp.team_season_id
        JOIN teams ts ON ts.team_season_id = tp.counterparty_team_season_id
        WHERE tp.player_id = ?
          AND tp.direction = 'incoming'
          AND td.manager_id = ?     -- destination = Pete
          AND ts.manager_id = ?     -- source      = George
        ORDER BY t.timestamp
    """, (target["player_id"], pete["manager_id"], george["manager_id"])))

    if not trades:
        print("\nNo Pete<-George trades found for this player.")
        print("Could be a synthetic trade - checking synthetic_transactions...")
        trades = list(conn.execute("""
            SELECT DISTINCT st.synth_id AS transaction_id, st.timestamp, st.event_type,
                            'successful' AS status, st.season
            FROM synthetic_transactions st
            JOIN synthetic_transaction_players stp ON stp.synth_id = st.synth_id
            JOIN teams td ON td.team_season_id = stp.team_season_id
            JOIN teams ts ON ts.team_season_id = stp.counterparty_team_season_id
            WHERE stp.player_id = ?
              AND stp.direction = 'incoming'
              AND td.manager_id = ?
              AND ts.manager_id = ?
        """, (target["player_id"], pete["manager_id"], george["manager_id"])))
        if not trades:
            print("Nothing in synthetic either. Bailing.")
            return

    print(f"\nFound {len(trades)} candidate trade(s):")
    for tr in trades:
        print(f"  id={tr['transaction_id']}  ts={tr['timestamp']}  "
              f"type={tr['event_type']}  status={tr['status']}  season={tr['season']}")

    # Pick the most recent (or only) one
    trade = trades[-1]
    tx_id = trade["transaction_id"]
    print(f"\nUsing trade id={tx_id}, timestamp={trade['timestamp']}")

    # ----- 4. Find any picks attached to this trade ------------------------
    picks = list(conn.execute("""
        SELECT tp.draft_round, tp.source_team_season_id, tp.destination_team_season_id,
               tp.original_team_season_id,
               ts.season AS src_season, td.season AS dst_season,
               ms.full_name AS src_mgr, md.full_name AS dst_mgr
        FROM transaction_picks tp
        JOIN teams ts ON ts.team_season_id = tp.source_team_season_id
        JOIN teams td ON td.team_season_id = tp.destination_team_season_id
        JOIN managers ms ON ms.manager_id = ts.manager_id
        JOIN managers md ON md.manager_id = td.manager_id
        WHERE tp.transaction_id = ?
    """, (tx_id,)))
    print(f"\nPicks attached to this trade: {len(picks)}")
    for p in picks:
        print(f"  R{p['draft_round']}  {p['src_mgr']} ({p['src_season']}) -> "
              f"{p['dst_mgr']} ({p['dst_season']})")

    # ----- 5. Filter to the Pete -> George R2 pick we want -----------------
    r2 = [p for p in picks
          if p["draft_round"] == PICK_ROUND
          and p["src_mgr"] == pete["full_name"]
          and p["dst_mgr"] == george["full_name"]]
    if not r2:
        print(f"\nNo R{PICK_ROUND} Pete->George pick on this trade.")
        print("Might be recorded against a different transaction, or not "
              "captured in transaction_picks. Check the raw Yahoo trade details.")
        return
    pick = r2[0]
    # Heuristic for which draft year the pick is for. transaction_picks doesn't
    # carry for_season yet (task #23). Best guess:
    #   - timestamp before Aug = same-year draft
    #   - timestamp Aug or later = next-year draft
    month = int(str(trade["timestamp"])[5:7])
    base_year = int(str(trade["timestamp"])[:4])
    target_year = base_year if month < 8 else base_year + 1
    print(f"\nPete's R{PICK_ROUND} pick -> George.")
    print(f"Heuristic guess: pick is for the {target_year} draft "
          f"(trade was in month {month:02d} of {base_year}).")

    # ----- 6. Derive Pete's draft slot for the target year -----------------
    pete_team = conn.execute(
        "SELECT team_season_id FROM teams WHERE manager_id = ? AND season = ?",
        (pete["manager_id"], target_year),
    ).fetchone()
    if not pete_team:
        print(f"Pete had no team in {target_year}. Bailing.")
        return
    pete_team_id = pete_team["team_season_id"]

    pete_r1 = conn.execute("""
        SELECT overall_pick, pick_in_round, p.player_name
        FROM draft_picks dp
        JOIN players p ON p.player_id = dp.player_id
        WHERE dp.team_season_id = ? AND dp.season = ? AND dp.draft_round = 1
        ORDER BY dp.overall_pick ASC LIMIT 1
    """, (pete_team_id, target_year)).fetchone()
    if pete_r1:
        pete_slot = pete_r1["pick_in_round"]
        print(f"Pete's draft slot in {target_year}: "
              f"#{pete_slot} (R1 pick = {pete_r1['player_name']} at overall {pete_r1['overall_pick']})")
    else:
        print(f"\nPete had no R1 pick in {target_year} - probably traded it away too.")
        print("Will show ALL of George's R2 picks so you can identify by context.")
        pete_slot = None

    # ----- 7. Find George's R2 picks in target_year ------------------------
    george_team = conn.execute(
        "SELECT team_season_id FROM teams WHERE manager_id = ? AND season = ?",
        (george["manager_id"], target_year),
    ).fetchone()
    if not george_team:
        print(f"George had no team in {target_year}.")
        return
    george_team_id = george_team["team_season_id"]

    george_r2 = list(conn.execute("""
        SELECT dp.overall_pick, dp.pick_in_round, dp.is_keeper,
               p.player_name, p.position, p.nfl_team
        FROM draft_picks dp
        JOIN players p ON p.player_id = dp.player_id
        WHERE dp.team_season_id = ? AND dp.season = ?
              AND dp.draft_round = ?
        ORDER BY dp.overall_pick
    """, (george_team_id, target_year, PICK_ROUND)))

    print(f"\nGeorge's R{PICK_ROUND} picks in {target_year} (count={len(george_r2)}):")
    for pk in george_r2:
        marker = ""
        if pete_slot is not None and pk["pick_in_round"] == pete_slot:
            marker = "   <-- THIS is likely Pete's original pick"
        keeper_flag = " [KEEPER]" if pk["is_keeper"] else ""
        print(f"  overall {pk['overall_pick']:>3}  slot #{pk['pick_in_round']:>2}  "
              f"{pk['player_name']} ({pk['position']}, {pk['nfl_team']}){keeper_flag}{marker}")


if __name__ == "__main__":
    main()
