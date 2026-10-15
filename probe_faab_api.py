"""
probe_faab_api.py - READ-ONLY. Does the Yahoo Fantasy API expose LOSING FAAB
offers anywhere? Dumps raw JSON for:
  1. the most recent waiver add that has a faab_bid (expect only the winner's bid)
  2. league transactions with types=waiver (expect pending claims only)
  3. league transactions with types=waiver for the signed-in team
Nothing is written anywhere except the token block yfpy refreshes in .env.

    venv/Scripts/python.exe probe_faab_api.py > faab_api_probe.txt
"""
import json
from pathlib import Path

from yfpy.query import YahooFantasySportsQuery

HERE = Path(__file__).parent
GAME_ID, LEAGUE_ID = 470, "4416"
LEAGUE_KEY = f"{GAME_ID}.l.{LEAGUE_ID}"
BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


def raw(q, url):
    """Return Yahoo's raw JSON for a URL (bypasses yfpy's model parsing)."""
    try:
        return q.query(url, [], data_type_class=None)
    except TypeError:
        return q.query(url, [])


def main():
    q = YahooFantasySportsQuery(league_id=LEAGUE_ID, game_code="nfl", game_id=GAME_ID,
                                env_file_location=HERE, save_token_data_to_env_file=True)

    print("=== 1. most recent waiver add with a faab_bid (raw extracted data) ===")
    txns = q.get_league_transactions()
    hit = None
    for t in txns:
        if getattr(t, "faab_bid", None) not in (None, "") and str(getattr(t, "type", "")) in ("add", "add/drop"):
            hit = t
            break
    if hit is None:
        print("no waiver add with faab_bid found")
    else:
        print(json.dumps(getattr(hit, "_extracted_data", {}), indent=1, default=str)[:6000])

    print("\n=== 2. league transactions;types=waiver (raw) ===")
    try:
        r = raw(q, f"{BASE}/league/{LEAGUE_KEY}/transactions;types=waiver")
        print(json.dumps(r, indent=1, default=str)[:6000])
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")

    print("\n=== 3. league transactions;types=waiver;team_key=<mine> (raw) ===")
    try:
        me = q.get_team_info(1)  # team 1 = Pete
        tk = getattr(me, "team_key", f"{LEAGUE_KEY}.t.1")
        r = raw(q, f"{BASE}/league/{LEAGUE_KEY}/transactions;types=waiver;team_key={tk}")
        print(json.dumps(r, indent=1, default=str)[:6000])
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")

    print("\n=== 4. transactions;types=add;count=3 (raw, to see every field Yahoo sends) ===")
    try:
        r = raw(q, f"{BASE}/league/{LEAGUE_KEY}/transactions;types=add;count=3")
        print(json.dumps(r, indent=1, default=str)[:8000])
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
