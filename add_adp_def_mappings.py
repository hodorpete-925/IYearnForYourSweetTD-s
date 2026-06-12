"""Map FFC-style defense names ("Denver Defense") to our DEF players ("Broncos").

The FFC 2-QB ADP feed names defenses by city; our players table (from Yahoo)
names them by nickname. Team codes exist on both sides, so the mapping is
mechanical: join on UPPER(nfl_team).

Dry run by default (prints proposals). Run with --apply to insert into
adp_name_mapping. Idempotent — existing mappings are left alone.

After applying:  python match_adp_players.py
"""
import sqlite3
import sys
from pathlib import Path

APPLY = "--apply" in sys.argv
conn = sqlite3.connect(Path(__file__).parent / "fantasy.db")
conn.row_factory = sqlite3.Row

defs = {  # UPPER(team code) -> (player_id, nickname)
    r["nfl_team"].upper(): (r["player_id"], r["player_name"])
    for r in conn.execute(
        "SELECT player_id, player_name, nfl_team FROM players WHERE position='DEF'")
}
existing = {r[0] for r in conn.execute("SELECT raw_name FROM adp_name_mapping")}

proposals, unresolved = [], []
rows = conn.execute(
    "SELECT DISTINCT player_name_raw, nfl_team FROM adp "
    "WHERE player_name_raw LIKE '% Defense'").fetchall()
for r in rows:
    raw, code = r["player_name_raw"], (r["nfl_team"] or "").upper()
    if raw in existing:
        continue
    if code in defs:
        proposals.append((raw, defs[code][0], defs[code][1], code))
    else:
        unresolved.append((raw, code))

print(f"{'INSERTING' if APPLY else 'DRY RUN — would insert'} {len(proposals)} defense mapping(s):")
for raw, pid, nick, code in sorted(proposals):
    print(f"  {raw:<28} -> {nick:<14} (player_id {pid}, {code})")
if unresolved:
    print("\nNo DEF player found for (left unmatched):")
    for raw, code in unresolved:
        print(f"  {raw}  [{code}]")

if APPLY and proposals:
    conn.executemany(
        "INSERT INTO adp_name_mapping (raw_name, player_id, note) VALUES (?, ?, ?)",
        [(raw, pid, f"FFC defense name -> {nick}") for raw, pid, nick, _ in proposals],
    )
    conn.commit()
    print(f"\nInserted {len(proposals)}. Now rerun: python match_adp_players.py")
elif not APPLY:
    print("\nLooks right? Run:  python add_adp_def_mappings.py --apply")
