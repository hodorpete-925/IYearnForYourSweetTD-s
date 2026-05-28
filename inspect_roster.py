"""
inspect_roster.py — peek at end-of-2025 roster.

The 2025 fantasy season ran ~17 weeks. We query a late-season week to get
the final roster state. This is the authoritative anchor for Phase B's
backward-walk DRC computation.
"""

from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent

query = YahooFantasySportsQuery(
    league_id="48079",
    game_code="nfl",
    game_id=461,  # 2025
    env_file_location=project_dir,
    save_token_data_to_env_file=True,
)

# Try late-season weeks in descending order until we find one with players.
# 17 is most common for fantasy regular season + playoffs combined.
for week in (18, 17, 16, 15, 14):
    print(f"\n--- Pete's team roster, 2025 week {week} ---")
    try:
        roster = query.get_team_roster_by_week(1, week)
        if getattr(roster, "players", None):
            n = len(roster.players)
            print(f"Found {n} players in week {week}\n")
            print(roster)
            break
        else:
            print(f"(empty roster for week {week} — trying earlier)")
    except Exception as e:
        print(f"  failed: {type(e).__name__}: {e}")