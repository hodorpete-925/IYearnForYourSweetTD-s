"""Dump everything in synthetic_transactions for 2024-08-25 so we can
eyeball whether the cluster represents a real trade or a false synthetic."""
import sqlite3
from pathlib import Path

conn = sqlite3.connect("fantasy.db")
conn.row_factory = sqlite3.Row

print("=== All synthetic_transactions on 2024-08-25 ===\n")
synths = list(conn.execute("""
    SELECT st.synth_id, st.timestamp, st.event_type, st.season
    FROM synthetic_transactions st
    WHERE DATE(st.timestamp) = '2024-08-25'
    ORDER BY st.synth_id
"""))
print(f"Count: {len(synths)}")
for s in synths:
    print(f"\n  synth_id={s['synth_id']}  ts={s['timestamp']}  evt={s['event_type']}")
    movements = conn.execute("""
        SELECT stp.player_id, p.player_name, stp.direction,
               md.full_name AS dest_mgr, ms.full_name AS src_mgr
        FROM synthetic_transaction_players stp
        JOIN players p ON p.player_id = stp.player_id
        LEFT JOIN teams td ON td.team_season_id = stp.team_season_id
        LEFT JOIN managers md ON md.manager_id = td.manager_id
        LEFT JOIN teams ts ON ts.team_season_id = stp.counterparty_team_season_id
        LEFT JOIN managers ms ON ms.manager_id = ts.manager_id
        WHERE stp.synth_id = ?
        ORDER BY stp.direction, stp.player_id
    """, (s["synth_id"],)).fetchall()
    for m in movements:
        print(f"     {m['direction']:<8}  {m['player_name']:<25}  {m['src_mgr']} -> {m['dest_mgr']}")

print("\n=== Excel-tracked trades on 2024-08-25 or within a week ===\n")
print("(You can also check your Excel directly for this date range.)")

print("\n=== Synthetic trades in the broader Aug 2024 window (for context) ===\n")
broader = list(conn.execute("""
    SELECT DATE(timestamp) AS d, COUNT(DISTINCT synth_id) AS n_trades
    FROM synthetic_transactions
    WHERE DATE(timestamp) BETWEEN '2024-08-01' AND '2024-08-31'
    GROUP BY DATE(timestamp)
    ORDER BY d
"""))
for r in broader:
    print(f"  {r['d']}: {r['n_trades']} synthetic trade events")
