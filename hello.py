"""
hello.py — Phase A: pull (team_id → manager) mapping for 2025 and inspect
the structure of a commish transaction.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent

NFL_GAME_IDS = {2023: 423, 2024: 449, 2025: 461, 2026: 470}
SEASON_TO_LEAGUE_ID = {
    2023: "1135115", 2024: "17476", 2025: "48079", 2026: "4416",
}
MY_TEAM_ID = 1
TARGET_SEASON = 2025

query = YahooFantasySportsQuery(
    league_id=SEASON_TO_LEAGUE_ID[TARGET_SEASON],
    game_code="nfl",
    game_id=NFL_GAME_IDS[TARGET_SEASON],
    env_file_location=project_dir,
    save_token_data_to_env_file=True,
)

# ---------- Teams + managers ----------
print(f"=== Teams in {TARGET_SEASON} ===\n")
teams = query.get_league_teams()
print(f"(yfpy returned {len(teams)} teams)\n")

def decode(v):
    """yfpy returns some text fields as bytes — decode for readability."""
    return v.decode("utf-8") if isinstance(v, bytes) else v

for t in teams:
    team_id = getattr(t, "team_id", "?")
    team_name = decode(getattr(t, "name", "?"))

    manager_nick = "?"
    if getattr(t, "managers", None):
        m_wrapper = t.managers[0]
        m = m_wrapper.manager if hasattr(m_wrapper, "manager") else m_wrapper
        manager_nick = decode(getattr(m, "nickname", "?"))

    print(f"  team_id={team_id:<4}  manager={manager_nick!r:<25}  team_name={team_name!r}")

print("\n=== First team object in full (so we can see all available fields) ===")
print(teams[0])

# ---------- Commish transaction ----------
print("\n=== Pulling transactions to find a commish event ===\n")
transactions = query.get_league_transactions()
commish = [t for t in transactions if t.type == "commish"]
print(f"Found {len(commish)} commish transactions.\n")

if commish:
    print("=== First commish transaction in full ===")
    print(commish[0])
else:
    print("(none — surprising given we counted 15 earlier)")