"""add_page_transactions.py — FENCED stopgap for in-season adds/drops while
the Yahoo API is locked.

Reads yahoo_page_transactions.json (captured from the league's Transactions
page in Chrome) and writes each Yahoo transaction into the synthetic layer:

    synthetic_transactions        event_type add | drop | add/drop
                                  note = 'YAHOO_PAGE|<timestamp>|<yahoo_team_id>'
    synthetic_transaction_players incoming (waivers/freeagents -> team)
                                  outgoing (team -> waivers)

THE FENCE is the note prefix 'YAHOO_PAGE|'. Every row this script creates
carries it and nothing else does, so the whole layer can be lifted out in
one move when the real API ingest takes over:

    python add_page_transactions.py             # dry run (read-only)
    python add_page_transactions.py --commit    # insert new rows (idempotent)
    python add_page_transactions.py --remove    # delete ALL YAHOO_PAGE rows
    python add_page_transactions.py --list      # show what is in the DB now

Cut-over plan: --remove, then ingest_transactions.py for 2026, then rebuild.
Players missing from `players` are inserted (id/name/pos/team) - those rows
are NOT fenced on purpose: the API ingest would upsert the same ids.

DRC rules applied downstream (generate_dashboard): a waiver/FA add starts
the player at DRC 16 ($10); a drop ends the previous owner's cost tracking.
Trades and pick moves are NOT in this file - they go through the trade queue.

Host-side only (writes fantasy.db). Dry run opens the DB read-only.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
SRC = HERE / "yahoo_page_transactions.json"
FENCE = "YAHOO_PAGE|"
SEASON = 2026


def note_key(t):
    return f"{FENCE}{t['ts']}|{t['team_id']}"


def event_type(moves):
    kinds = {m["dir"] for m in moves}
    if kinds == {"add", "drop"}:
        return "add/drop"
    return "add" if kinds == {"add"} else "drop"


def resolve_player(conn, m, cache):
    """Yahoo player id when the page gave one; DEF rows come without an id,
    so match those by name (players.position = 'DEF')."""
    if m.get("player_id"):
        return m["player_id"], False
    key = ("DEF", m["name"])
    if key in cache:
        return cache[key], False
    row = conn.execute("SELECT player_id FROM players WHERE position='DEF' AND player_name=?",
                       (m["name"],)).fetchone()
    if not row:
        print(f"  WARN: DEF not found in players: {m['name']!r}")
        return None, False
    cache[key] = row[0]
    return row[0], False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--remove", action="store_true", help="delete every fenced row")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    rw = a.commit or a.remove
    conn = sqlite3.connect(DB) if rw else sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys = ON;")

    fenced = conn.execute("SELECT synth_id, timestamp, event_type, note FROM synthetic_transactions "
                          "WHERE note LIKE ? ORDER BY timestamp", (FENCE + "%",)).fetchall()
    if a.list:
        print(f"{len(fenced)} fenced transaction(s) in the DB:")
        for sid, ts, et, note in fenced:
            pl = conn.execute("SELECT direction, player_id FROM synthetic_transaction_players WHERE synth_id=?",
                              (sid,)).fetchall()
            print(f"  {sid:>5}  {ts}  {et:<9} {pl}")
        return

    if a.remove:
        ids = [r[0] for r in fenced]
        if not ids:
            print("Nothing fenced to remove.")
            return
        q = ",".join("?" * len(ids))
        n1 = conn.execute(f"DELETE FROM synthetic_transaction_players WHERE synth_id IN ({q})", ids).rowcount
        n2 = conn.execute(f"DELETE FROM synthetic_transactions WHERE synth_id IN ({q})", ids).rowcount
        conn.commit()
        print(f"Removed {n2} fenced transaction(s), {n1} player movement(s). "
              "Synthetic trades and everything else untouched.")
        return

    doc = json.loads(SRC.read_text(encoding="utf-8"))
    existing = {r[3] for r in fenced}
    tsid = {r[0]: r[1] for r in conn.execute(
        "SELECT yahoo_team_id, team_season_id FROM teams WHERE season=?", (SEASON,))}
    known = {r[0] for r in conn.execute("SELECT player_id FROM players")}
    cache = {}
    plan, new_players, skipped = [], {}, 0
    print(f"Loaded {len(doc['transactions'])} transaction(s) from {SRC.name} "
          f"(as of {doc.get('as_of')}); {len(existing)} already in the DB.\n")
    for t in doc["transactions"]:
        key = note_key(t)
        if key in existing:
            skipped += 1
            continue
        team = tsid.get(t["team_id"])
        if team is None:
            print(f"  SKIP {t['ts']}: unknown yahoo_team_id {t['team_id']}")
            continue
        rows = []
        for m in t["moves"]:
            pid, _ = resolve_player(conn, m, cache)
            if pid is None:
                continue
            if pid not in known and pid not in new_players:
                new_players[pid] = (m["name"], m["pos"], m["nfl"])
            if m["dir"] == "add":
                rows.append((pid, "incoming", team, m.get("source", "freeagents"), "team", m["name"], m.get("faab")))
            else:
                rows.append((pid, "outgoing", team, "team", "waivers", m["name"], None))
        if rows:
            plan.append((t, key, event_type(t["moves"]), rows))

    print(f"=== Plan: {len(plan)} new transaction(s), {skipped} already present, "
          f"{len(new_players)} player row(s) to add ===")
    for t, key, et, rows in plan:
        for pid, d, team, s, dst, name, faab in rows:
            tag = "+" if d == "incoming" else "-"
            bid = f" (${faab} FAAB)" if faab is not None else ""
            print(f"  {t['ts']}  team {t['team_id']:>2}  {tag} {name:<24} [{et}{bid}]")
    for pid, (name, pos, nfl) in new_players.items():
        print(f"  new player row: {pid} {name} {pos} {nfl}")

    if not a.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    for pid, (name, pos, nfl) in new_players.items():
        conn.execute("INSERT OR IGNORE INTO players (player_id, player_name, position, nfl_team) VALUES (?,?,?,?)",
                     (pid, name, pos, nfl))
    for t, key, et, rows in plan:
        cur = conn.execute("INSERT INTO synthetic_transactions (timestamp, event_type, season, note) VALUES (?,?,?,?)",
                           (t["ts"], et, SEASON, key))
        sid = cur.lastrowid
        for pid, d, team, s, dst, name, faab in rows:
            conn.execute("INSERT INTO synthetic_transaction_players (synth_id, player_id, direction, team_season_id, "
                         "source_type, destination_type, counterparty_team_season_id) VALUES (?,?,?,?,?,?,NULL)",
                         (sid, pid, d, team, s, dst))
    conn.commit()
    print(f"\nInserted {len(plan)} transaction(s). Fence: note LIKE '{FENCE}%'.")


if __name__ == "__main__":
    main()
