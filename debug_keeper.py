"""Debug: figure out the actual Python structure of is_keeper."""
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent

query = YahooFantasySportsQuery(
    league_id="48079",
    game_code="nfl",
    game_id=461,
    env_file_location=project_dir,
    save_token_data_to_env_file=True,
)

roster = query.get_team_roster_by_week(1, 17)
print("=== is_keeper structure for first 3 players ===\n")

for p_wrapper in roster.players[:3]:
    p = p_wrapper.player if hasattr(p_wrapper, 'player') else p_wrapper
    name = p.name.full if hasattr(p.name, 'full') else p.name
    print(f"--- {name} ---")
    print(f"  hasattr 'is_keeper': {hasattr(p, 'is_keeper')}")

    keeper = getattr(p, 'is_keeper', None)
    print(f"  keeper repr: {keeper!r}")
    print(f"  keeper type: {type(keeper).__name__}")

    if keeper is not None:
        if hasattr(keeper, '__dict__'):
            print(f"  __dict__: {keeper.__dict__}")
        if hasattr(keeper, 'kept'):
            print(f"  keeper.kept: {keeper.kept!r} (type={type(keeper.kept).__name__})")
    print()