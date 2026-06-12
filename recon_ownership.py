"""Reconcile player ownership: transaction walk vs final-roster snapshot.

The dashboard has two sources of truth that can disagree:
  - Team pages:  final_rosters WHERE season = 2025  (Yahoo's week-17 snapshot)
  - Player search "Currently:":  player_history.get_owner_at_year_end()
    (walks the full transaction log through Dec 31)

This script is READ-ONLY. It flags two kinds of mismatch:

  A1  Player's last 2025 event is an ADD (never dropped after) -> they were
      genuinely on that team at season end, but Yahoo's final-roster snapshot
      (taken at the last scoring week) missed them. These players are absent
      from the team page AND from cap/DRC math.

  A2  Player's last 2025 event is a DROP -> they're correctly off every
      roster page, but get_owner_at_year_end() only looks at INCOMING
      transactions, so the search card still says "Currently: <last adder>"
      when it should say free agent.

Run:  python recon_ownership.py
Output: console report + recon_ownership_report.csv next to this file.
"""
import csv
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import player_history as hist  # noqa: E402  (the dashboard's own owner logic)

SEASON = 2025

conn = sqlite3.connect(f"file:{HERE / 'fantasy.db'}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# 2025 team_season_id -> manager name
teams = {
    r["team_season_id"]: r["full_name"]
    for r in conn.execute(
        "SELECT t.team_season_id, m.full_name FROM teams t "
        "JOIN managers m ON m.manager_id = t.manager_id WHERE t.season = ?",
        (SEASON,),
    )
}

# Who is in the final-roster snapshot
on_roster = {
    r["player_id"]
    for r in conn.execute(
        "SELECT player_id FROM final_rosters WHERE season = ?", (SEASON,)
    )
}

# Same player universe the dashboard's search uses
player_rows = conn.execute(
    "SELECT DISTINCT p.player_id, p.player_name, p.position FROM players p "
    "WHERE p.player_id IN ("
    "  SELECT player_id FROM final_rosters "
    "  UNION SELECT player_id FROM draft_picks "
    "  UNION SELECT player_id FROM transaction_players)"
).fetchall()

a1, a2 = [], []
for row in player_rows:
    pid = row["player_id"]
    owner = hist.get_owner_at_year_end(conn, pid, SEASON)
    if owner is None or owner not in teams or pid in on_roster:
        continue  # consistent (or not a 2025 league team) -> skip

    last = conn.execute(
        "SELECT t.timestamp, tp.direction FROM all_transactions t "
        "JOIN all_transaction_players tp ON tp.transaction_id = t.transaction_id "
        "WHERE tp.player_id = ? AND t.season = ? "
        "ORDER BY t.timestamp DESC, tp.direction LIMIT 1",
        (pid, SEASON),
    ).fetchone()
    if last is None:
        continue

    rec = (teams[owner], row["player_name"], row["position"] or "?",
           str(last["timestamp"])[:10])
    (a1 if last["direction"] == "incoming" else a2).append(rec)

print(f"=== A1: owned at season end but MISSING from final_rosters ({len(a1)}) ===")
print("    (missing from team page AND from cap math)")
for x in sorted(a1):
    print("  %-17s %-24s %-3s last add: %s" % x)

print(f"\n=== A2: dropped, but search 'Currently:' still names an owner ({len(a2)}) ===")
for x in sorted(a2):
    print("  %-17s %-24s %-3s last drop: %s" % x)

out = HERE / "recon_ownership_report.csv"
with open(out, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["bucket", "search_owner", "player", "pos", "last_event_date"])
    for x in sorted(a1):
        w.writerow(["A1_missing_from_roster_page", *x])
    for x in sorted(a2):
        w.writerow(["A2_currently_label_wrong", *x])
print(f"\nWrote {out}")
