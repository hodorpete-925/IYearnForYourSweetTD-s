"""Diagnostic v2: get_team_roster_player_stats_by_week returns a list of
players directly. Dump the first player's structure so we can confirm the
points field shape before running the full ingestion."""

import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent

conn = sqlite3.connect(project_dir / "fantasy.db")
row = conn.execute(
    "SELECT nfl_game_id, yahoo_league_id FROM seasons WHERE season = 2024"
).fetchone()
nfl_game_id, yahoo_league_id = row
conn.close()

query = YahooFantasySportsQuery(
    league_id=yahoo_league_id,
    game_code="nfl",
    game_id=nfl_game_id,
    env_file_location=project_dir,
    save_token_data_to_env_file=True,
)

print("=== get_team_roster_player_stats_by_week(team_id=1, week=1) ===")
result = query.get_team_roster_player_stats_by_week(1, 1)
print(f"Type: {type(result).__name__}, length: {len(result) if hasattr(result, '__len__') else '?'}")

if not result:
    print("Empty list. Bailing.")
    raise SystemExit()

first = result[0]
inner = first.player if hasattr(first, "player") else first
print(f"\nFirst item: {type(first).__name__}")
print(f"  Inner player type: {type(inner).__name__}")
print(f"  Inner player attrs:")
for attr in sorted(dir(inner)):
    if attr.startswith("_"):
        continue
    try:
        v = getattr(inner, attr)
        if callable(v):
            continue
        s = repr(v)
        if len(s) > 140:
            s = s[:140] + "...(truncated)"
        print(f"    {attr:30s} = {s}")
    except Exception as e:
        print(f"    {attr:30s} = <error: {e}>")

# Specifically look at player_points
pp = getattr(inner, "player_points", None)
print(f"\nplayer_points type: {type(pp).__name__}")
if pp is not None:
    for a in sorted(dir(pp)):
        if a.startswith("_"):
            continue
        try:
            v = getattr(pp, a)
            if not callable(v):
                print(f"  {a:20s} = {v!r}")
        except Exception as e:
            print(f"  {a:20s} = <error: {e}>")

# Show the full player list briefly
print(f"\n=== Quick scan of all {len(result)} players (name + points) ===")
for item in result[:25]:
    p = item.player if hasattr(item, "player") else item
    name = p.name.full if hasattr(p.name, "full") else p.name
    if isinstance(name, bytes):
        name = name.decode("utf-8")
    pp = getattr(p, "player_points", None)
    total = getattr(pp, "total", None) if pp is not None else None
    print(f"  {name:30s} -> player_points.total = {total!r}")
