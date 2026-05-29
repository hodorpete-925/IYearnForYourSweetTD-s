"""Quick diagnostic: confirm player_weekly_stats table exists + show DB tables."""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).parent / "fantasy.db")
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
)]
print(f"Tables ({len(tables)}):")
for t in tables:
    print(f"  - {t}")

print()
has_pws = "player_weekly_stats" in tables
print(f"player_weekly_stats present: {has_pws}")
if has_pws:
    n = conn.execute("SELECT COUNT(*) FROM player_weekly_stats").fetchone()[0]
    print(f"  rows currently: {n}")
conn.close()
