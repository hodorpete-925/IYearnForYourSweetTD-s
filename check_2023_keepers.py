"""check_2023_keepers.py - quick look at how Yahoo flagged keepers in 2023.

If 2023 was the league's inception year, we'd expect NO keepers — every player
drafted fresh. If 2023 had any is_keeper=1 rows, that means either the league
started before 2023 (and we're missing prior years) or Yahoo's flag is noisy.
"""

import sqlite3

conn = sqlite3.connect("fantasy.db")
conn.row_factory = sqlite3.Row

print("=== 2023 draft_picks is_keeper distribution ===")
for r in conn.execute("""
    SELECT is_keeper, COUNT(*) AS n
    FROM draft_picks WHERE season = 2023
    GROUP BY is_keeper
"""):
    print(f"  is_keeper = {r['is_keeper']}: {r['n']} rows")

print("\n=== Same view for 2024 and 2025 (for comparison) ===")
for season in (2024, 2025):
    print(f"\nSeason {season}:")
    for r in conn.execute("""
        SELECT is_keeper, COUNT(*) AS n
        FROM draft_picks WHERE season = ?
        GROUP BY is_keeper
    """, (season,)):
        print(f"  is_keeper = {r['is_keeper']}: {r['n']} rows")

print("\n=== If any 2023 keepers exist, show first 20 ===")
rows = list(conn.execute("""
    SELECT dp.draft_round, dp.overall_pick, p.player_name, p.position,
           m.full_name AS manager
    FROM draft_picks dp
    JOIN players p ON p.player_id = dp.player_id
    JOIN teams t ON t.team_season_id = dp.team_season_id
    JOIN managers m ON m.manager_id = t.manager_id
    WHERE dp.season = 2023 AND dp.is_keeper = 1
    ORDER BY dp.overall_pick
    LIMIT 20
"""))
if rows:
    for r in rows:
        print(f"  R{r['draft_round']:>2} (overall {r['overall_pick']:>3})  "
              f"{r['player_name']:<25} {r['position']:<4} {r['manager']}")
else:
    print("  (none — Yahoo flagged ZERO keepers in 2023)")
