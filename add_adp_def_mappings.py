"""Map ADP-source defense names to our DEF players ("Broncos").

Sources spell defenses differently — FFC says "Denver Defense", FantasyPros
says "Denver Broncos" — but our players table (from Yahoo) uses nicknames.
Team codes exist on both sides, so the mapping is mechanical: any unmatched
adp row whose position_rank starts with DEF joins on UPPER(nfl_team),
with source-specific code aliases (e.g. FantasyPros JAC -> Yahoo Jax).

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

CODE_ALIASES = {"JAC": "JAX", "WSH": "WAS", "LA": "LAR"}

proposals, unresolved = [], []
rows = conn.execute(
    "SELECT DISTINCT player_name_raw, nfl_team FROM adp "
    "WHERE player_id IS NULL AND ("
    "  player_name_raw LIKE '% Defense' OR position_rank LIKE 'DEF%')"
).fetchall()
for r in rows:
    raw, code = r["player_name_raw"], (r["nfl_team"] or "").upper()
    code = CODE_ALIASES.get(code, code)
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
