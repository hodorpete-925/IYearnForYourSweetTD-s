"""Quick: do we have 2026 teams in the DB, and how do they map to managers?"""
import sqlite3
conn = sqlite3.connect("fantasy.db")
conn.row_factory = sqlite3.Row

print("=== seasons in DB ===")
for r in conn.execute("SELECT DISTINCT season FROM teams ORDER BY season"):
    print(f"  {r['season']}")

print("\n=== 2026 teams (if any) ===")
rows = list(conn.execute("""
    SELECT t.team_season_id, t.yahoo_team_id, t.team_name,
           m.manager_id, m.full_name AS manager
    FROM teams t JOIN managers m ON m.manager_id = t.manager_id
    WHERE t.season = 2026
    ORDER BY t.yahoo_team_id
"""))
if not rows:
    print("  No 2026 teams ingested.")
else:
    for r in rows:
        print(f"  team_season_id={r['team_season_id']:>3}  yahoo_id={r['yahoo_team_id']:>2}  "
              f"{r['team_name']:<30} -> {r['manager']}")

print("\n=== 2025 teams (for reference / mapping inference) ===")
for r in conn.execute("""
    SELECT t.team_season_id, t.yahoo_team_id, t.team_name, m.full_name AS manager
    FROM teams t JOIN managers m ON m.manager_id = t.manager_id
    WHERE t.season = 2025
    ORDER BY t.yahoo_team_id
"""):
    print(f"  yahoo_id={r['yahoo_team_id']:>2}  {r['team_name']:<32} -> {r['manager']}")
