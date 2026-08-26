"""add_synthetic_trades.py - declarative inserter for synthetic trades.

Edit the TRADES list at the top to declare what to insert. Each trade is a
dict: date, season, side_a, side_b — each side is
(manager_full_name, [list_of_player_names_received]) — plus optional
picks_a / picks_b: lists of {"round": N, "original": "Manager Full Name"}
that the side RECEIVES (original = whose draft slot the pick started as;
for a manager's own pick that's just their own name).

The script:
  - Resolves manager names to team_season_ids for the trade's season
  - Resolves player names to player_ids (fuzzy fallback for typos)
  - Generates fresh synth_ids
  - Inserts one synthetic_transactions row per player movement (matches the
    existing batching pattern in synthetic_transactions)
  - PICKS (added 2026-08-19, post-API-outage): creates the
    synthetic_transaction_picks table on first run (mirrors
    transaction_picks, keyed to synth_id) and inserts one row per pick
    movement. generate_dashboard.py reads season-2026 synthetic pick moves
    into the 2026 pick boards.
  - Is idempotent: skips player/pick movements already present as synthetics

Run:  python add_synthetic_trades.py             # dry-run
      python add_synthetic_trades.py --commit    # actually insert
"""
import argparse
import difflib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"

# ============================================================================
# DECLARE TRADES HERE
# ============================================================================
# FOOT-GUN DEFUSED 2026-08-15: the four HISTORICAL trades that used to sit in
# this list (Feb-2025 Schlosberg/Watson, Achane/Gibbs, Kincaid+Dowdle/JSN, and
# the James Conner keeper-slot move) were NEVER inserted through this script.
# Their effects already live in the DB through the older synthetic rows /
# override layers, so the idempotence check does NOT catch them and running
# --commit with them listed would DOUBLE-ENTER 8 player movements and corrupt
# DRC history (verified via dry-run 2026-08-15). They are preserved in git
# history (pre-2026-08-19 version of this file) for the record. Only add NEW,
# not-yet-represented trades here.
TRADES = [
    {
        # Jeanty <-> Taylor, summer 2026 off-season swap. Reported by Pete
        # 2026-08-15 (Yahoo API dead, no automated record); actual trade
        # date corrected to 2026-08-01 per Pete 2026-08-22 (DB re-dated by
        # fix_jeanty_taylor_trade_date.py). Both freeze at their 2025
        # DRC 2 ($100) for 2026, decrement resumes 2027.
        # Already in the DB (synth_ids 21-22); idempotence skips it.
        "date": "2026-08-01",
        "season": 2026,
        "side_a": ("Brian Malconian", ["Ashton Jeanty"]),
        "side_b": ("Aric Tao", ["Jonathan Taylor"]),
        "note": "Off-season 2026 swap: Malconian gets Jeanty, Tao gets Taylor "
                "(player-for-player, no picks). Entered manually post-API; "
                "date approximate (report date).",
    },
    {
        # McLaurin + Scott's own R4  <->  Tuten + Pete's acquired R16.
        # Reported by Pete 2026-08-19 as FINALIZED.
        # DRC consequences (freeze rule): McLaurin to Pete frozen at his
        # 2025 DRC 5 ($50) for 2026; Tuten to Scott frozen at his 2025
        # DRC 16 ($10) for 2026. Decrements resume 2027.
        # PICK IDENTITY: the R16 Pete sends is the pick ORIGINALLY Dan
        # Vescuso's (16.01) — Pete's own 16.02 already belongs to Tom.
        # The R4 Scott sends is his own slot (4.05).
        # TODO(Pete): date below is the REPORT date; correct if the
        #   agreement date differs (any pre-draft 2026 date is equivalent).
        "date": "2026-08-19",
        "season": 2026,
        "side_a": ("Pete Hodor", ["Terry McLaurin"]),
        "picks_a": [{"round": 4, "original": "Scott Montgomery"}],
        "side_b": ("Scott Montgomery", ["Bhayshul Tuten"]),
        "picks_b": [{"round": 16, "original": "Dan Vescuso"}],
        "note": "Off-season 2026: Pete gets McLaurin + Scott's R4 (4.05); "
                "Scott gets Tuten + the R16 Pete had acquired from Dan "
                "(16.01). Entered manually post-API; date = report date.",
    },
    {
        # Corum <-> Marks. Reported by Pete 2026-08-22 as FINAL (happened
        # 8/20; the 48-hour counter window has passed).
        # DRC (freeze rule): both players' 2025 anchor is an in-season
        # waiver pickup (Corum re-added by Greg 12/3/25; Marks added by
        # Brian 9/19/25) -> 2025 DRC 16. Trade-time DRC 16 ($10) frozen
        # for 2026 on both sides; decrement resumes 2027.
        "date": "2026-08-20",
        "season": 2026,
        "side_a": ("Brian Malconian", ["Blake Corum"]),
        "side_b": ("Greg Pearson", ["Woody Marks"]),
        "note": "Off-season 2026 swap: Malconian gets Corum, Pearson gets "
                "Marks (player-for-player, no picks). Entered manually "
                "post-API; date = trade date per Pete.",
    },
    {
        # Waddle <-> Olave. Reported by Pete 2026-08-22 as FINAL (happened
        # 8/20; the 48-hour counter window has passed).
        # DRC (freeze rule): both 2025 rows are fresh ownership-changing
        # drafts (Waddle: George 2025 R5, prior owner Pete; Olave: Dan
        # 2025 R6, prior owner Pete). Waddle trade-time DRC 5 ($50)
        # frozen for Dan in 2026; Olave trade-time DRC 6 ($30) frozen
        # for George in 2026. Decrements resume 2027.
        "date": "2026-08-20",
        "season": 2026,
        "side_a": ("Dan Vescuso", ["Jaylen Waddle"]),
        "side_b": ("George Mensing", ["Chris Olave"]),
        "note": "Off-season 2026 swap: Vescuso gets Waddle, Mensing gets "
                "Olave (player-for-player, no picks). Entered manually "
                "post-API; date = trade date per Pete.",
    },
    {
        # McMillan + Brian's own R12  <->  the R4 Vescuso acquired from
        # George. Reported by Pete 2026-08-24.
        # DRC (freeze rule): Tetairoa McMillan's 2025 anchor is Brian's
        # 2025 R4 draft pick -> trade-time DRC 4 ($60) frozen for
        # Vescuso in 2026; decrement resumes 2027. (Keep-path for Brian
        # would have been DRC 3 / $80 — the freeze supersedes it.)
        # PICK IDENTITY: Brian sends his own R12 (12.12). Vescuso sends
        # the R4 originally George Mensing's (4.03) — Vescuso's own 4.01
        # stays put. Both teams stay at 16 picks.
        "date": "2026-08-22",
        "season": 2026,
        "side_a": ("Dan Vescuso", ["Tetairoa McMillan"]),
        "picks_a": [{"round": 12, "original": "Brian Malconian"}],
        "side_b": ("Brian Malconian", []),
        "picks_b": [{"round": 4, "original": "George Mensing"}],
        "note": "Off-season 2026: Vescuso gets Tet McMillan + Brian's own "
                "R12 (12.12); Malconian gets the R4 Vescuso had acquired "
                "from George (4.03). Entered manually post-API; date = "
                "trade date per Pete (8/22).",
    },
]


def normalize(s):
    if not s:
        return ""
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def resolve_player(conn, name, cache):
    if name in cache:
        return cache[name]
    rows = list(conn.execute(
        "SELECT player_id, player_name FROM players"))
    norm_target = normalize(name)
    # Exact normalized match first
    for pid, pname in rows:
        if normalize(pname) == norm_target:
            cache[name] = pid
            return pid
    # Fuzzy fallback
    pool = {pname: pid for pid, pname in rows}
    best = difflib.get_close_matches(name, pool.keys(), n=1, cutoff=0.75)
    if best:
        cache[name] = pool[best[0]]
        print(f"  fuzzy: {name!r} -> {best[0]!r}")
        return pool[best[0]]
    print(f"  WARN: player not found: {name!r}")
    return None


def resolve_team(conn, mgr_name, season, cache):
    key = (mgr_name, season)
    if key in cache:
        return cache[key]
    row = conn.execute("""
        SELECT t.team_season_id
        FROM teams t JOIN managers m ON m.manager_id = t.manager_id
        WHERE m.full_name = ? AND t.season = ?
    """, (mgr_name, season)).fetchone()
    if not row:
        print(f"  WARN: no team for {mgr_name!r} in {season}")
        return None
    cache[key] = row[0]
    return row[0]


def trade_already_exists(conn, date, team_dest, pid):
    """True if a synthetic trade already has this exact player movement."""
    row = conn.execute("""
        SELECT 1 FROM synthetic_transactions st
        JOIN synthetic_transaction_players stp ON stp.synth_id = st.synth_id
        WHERE DATE(st.timestamp) = ?
          AND stp.team_season_id = ?
          AND stp.player_id = ?
          AND stp.direction = 'incoming'
        LIMIT 1
    """, (date, team_dest, pid)).fetchone()
    return row is not None


def pick_already_exists(conn, date, dest_team, rnd, orig_team):
    row = conn.execute("""
        SELECT 1 FROM synthetic_transactions st
        JOIN synthetic_transaction_picks sp ON sp.synth_id = st.synth_id
        WHERE DATE(st.timestamp) = ?
          AND sp.destination_team_season_id = ?
          AND sp.draft_round = ?
          AND sp.original_team_season_id = ?
        LIMIT 1
    """, (date, dest_team, rnd, orig_team)).fetchone()
    return row is not None


def ensure_pick_table(conn):
    """Synthetic mirror of transaction_picks, keyed to synth_id. Safe to
    call every run."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synthetic_transaction_picks (
            synth_id                    INTEGER NOT NULL,
            draft_round                 INTEGER NOT NULL,
            source_team_season_id       INTEGER NOT NULL,
            destination_team_season_id  INTEGER NOT NULL,
            original_team_season_id     INTEGER NOT NULL,
            PRIMARY KEY (synth_id, draft_round, source_team_season_id),
            FOREIGN KEY (synth_id) REFERENCES synthetic_transactions(synth_id),
            FOREIGN KEY (source_team_season_id)      REFERENCES teams(team_season_id),
            FOREIGN KEY (destination_team_season_id) REFERENCES teams(team_season_id),
            FOREIGN KEY (original_team_season_id)    REFERENCES teams(team_season_id)
        )
    """)


def next_synth_id(conn):
    row = conn.execute("SELECT COALESCE(MAX(synth_id), 0) + 1 FROM synthetic_transactions").fetchone()
    return row[0]


def insert_movement(conn, synth_id, date, season, pid, dest_team, src_team):
    """Insert ONE synthetic trade movement (matches the per-player batching
    used by the existing synthetic_transactions data: each player gets its
    own synth_id with timestamp at the trade date)."""
    ts = f"{date} 00:00:00"
    conn.execute(
        "INSERT INTO synthetic_transactions (synth_id, timestamp, event_type, season) "
        "VALUES (?, ?, 'trade', ?)",
        (synth_id, ts, season),
    )
    # The destination team's incoming row
    conn.execute(
        "INSERT INTO synthetic_transaction_players "
        "(synth_id, player_id, direction, team_season_id, source_type, "
        " destination_type, counterparty_team_season_id) "
        "VALUES (?, ?, 'incoming', ?, 'team', 'team', ?)",
        (synth_id, pid, dest_team, src_team),
    )
    # The source team's outgoing row
    conn.execute(
        "INSERT INTO synthetic_transaction_players "
        "(synth_id, player_id, direction, team_season_id, source_type, "
        " destination_type, counterparty_team_season_id) "
        "VALUES (?, ?, 'outgoing', ?, 'team', 'team', ?)",
        (synth_id, pid, src_team, dest_team),
    )


def insert_pick_movement(conn, synth_id, date, season, rnd, src_team, dest_team, orig_team):
    """One pick movement = its own synth row + one synthetic pick row
    (same shape as transaction_picks)."""
    ts = f"{date} 00:00:00"
    conn.execute(
        "INSERT INTO synthetic_transactions (synth_id, timestamp, event_type, season) "
        "VALUES (?, ?, 'trade', ?)",
        (synth_id, ts, season),
    )
    conn.execute(
        "INSERT INTO synthetic_transaction_picks "
        "(synth_id, draft_round, source_team_season_id, "
        " destination_team_season_id, original_team_season_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (synth_id, rnd, src_team, dest_team, orig_team),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Actually insert (default is dry-run).")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")
    ensure_pick_table(conn)
    player_cache = {}
    team_cache = {}
    planned = []        # player movements
    planned_picks = []  # pick movements

    print(f"Loaded {len(TRADES)} trade(s) from TRADES list.\n")
    for trade in TRADES:
        print(f"--- {trade['date']} {trade['side_a'][0]} <-> {trade['side_b'][0]} ---")
        print(f"  ({trade['note']})")
        date = trade["date"]
        season = trade["season"]
        team_a = resolve_team(conn, trade["side_a"][0], season, team_cache)
        team_b = resolve_team(conn, trade["side_b"][0], season, team_cache)
        if not team_a or not team_b:
            print("  SKIPPED (team resolution failed)")
            continue
        # side_a gets the players from side_a's "received" list; same for side_b
        for player_name in trade["side_a"][1]:
            pid = resolve_player(conn, player_name, player_cache)
            if not pid:
                continue
            if trade_already_exists(conn, date, team_a, pid):
                print(f"  skip (already in DB): {player_name} -> {trade['side_a'][0]}")
                continue
            planned.append((date, season, pid, player_name, team_a, team_b,
                            trade["side_a"][0]))
        for player_name in trade["side_b"][1]:
            pid = resolve_player(conn, player_name, player_cache)
            if not pid:
                continue
            if trade_already_exists(conn, date, team_b, pid):
                print(f"  skip (already in DB): {player_name} -> {trade['side_b'][0]}")
                continue
            planned.append((date, season, pid, player_name, team_b, team_a,
                            trade["side_b"][0]))
        # Picks: received by side_a come FROM side_b, and vice versa
        for spec, dest_team, src_team, dest_mgr in (
                *[(p, team_a, team_b, trade["side_a"][0]) for p in trade.get("picks_a", [])],
                *[(p, team_b, team_a, trade["side_b"][0]) for p in trade.get("picks_b", [])]):
            orig_team = resolve_team(conn, spec["original"], season, team_cache)
            if not orig_team:
                print(f"  SKIPPED pick R{spec['round']} (original owner unresolved)")
                continue
            if pick_already_exists(conn, date, dest_team, spec["round"], orig_team):
                print(f"  skip (already in DB): R{spec['round']} pick -> {dest_mgr}")
                continue
            planned_picks.append((date, season, spec["round"], src_team,
                                  dest_team, orig_team, dest_mgr, spec["original"]))

    print(f"\n=== Plan: {len(planned)} player movement(s), {len(planned_picks)} pick movement(s) ===")
    for date, season, pid, pname, dest_team, src_team, dest_mgr in planned:
        print(f"  {date}  {pname:<30} -> {dest_mgr}")
    for date, season, rnd, src_team, dest_team, orig_team, dest_mgr, orig_mgr in planned_picks:
        print(f"  {date}  R{rnd} pick (orig {orig_mgr}){'':<8} -> {dest_mgr}")

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    if not planned and not planned_picks:
        print("\nNothing to insert. Done.")
        return

    print(f"\nInserting {len(planned)} player + {len(planned_picks)} pick movement(s)...")
    for date, season, pid, pname, dest_team, src_team, dest_mgr in planned:
        sid = next_synth_id(conn)
        insert_movement(conn, sid, date, season, pid, dest_team, src_team)
    for date, season, rnd, src_team, dest_team, orig_team, dest_mgr, orig_mgr in planned_picks:
        sid = next_synth_id(conn)
        insert_pick_movement(conn, sid, date, season, rnd, src_team, dest_team, orig_team)
    conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
