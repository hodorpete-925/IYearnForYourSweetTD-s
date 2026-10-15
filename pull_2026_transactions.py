"""
pull_2026_transactions.py — READ-ONLY. Fetch every 2026 transaction for league
4416 from Yahoo and write it to yahoo_transactions_2026.json. Never opens
fantasy.db. The only side effect is yfpy refreshing the token block in .env.

Run host-side from the repo folder:
    venv\Scripts\python.exe pull_2026_transactions.py
"""
import json
from datetime import datetime
from pathlib import Path

from yfpy.query import YahooFantasySportsQuery

HERE = Path(__file__).parent
OUT = HERE / "yahoo_transactions_2026.json"
GAME_ID, LEAGUE_ID, SEASON = 470, "4416", 2026


def dec(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def team_id_from_key(key):
    # '470.l.4416.t.5' -> 5
    return int(str(dec(key)).split(".")[-1]) if key else None


def player_rows(t):
    rows = []
    for w in (getattr(t, "players", None) or []):
        p = w.player if hasattr(w, "player") else w
        td = getattr(p, "transaction_data", None)
        if isinstance(td, list):
            td = td[0] if td else None
        td = getattr(td, "transaction_data", td)
        rows.append({
            "player_id": int(getattr(p, "player_id", 0) or 0),
            "name": dec(getattr(getattr(p, "name", None), "full", None) or getattr(p, "full_name", "?")),
            "position": dec(getattr(p, "display_position", None) or getattr(p, "primary_position", "")),
            "nfl_team": dec(getattr(p, "editorial_team_abbr", "")),
            "type": dec(getattr(td, "type", None)),                 # add / drop / trade
            "source_type": dec(getattr(td, "source_type", None)),   # team / waivers / freeagents
            "source_team_id": team_id_from_key(getattr(td, "source_team_key", None)),
            "dest_type": dec(getattr(td, "destination_type", None)),
            "dest_team_id": team_id_from_key(getattr(td, "destination_team_key", None)),
        })
    return rows


def pick_rows(t):
    rows = []
    for w in (getattr(t, "picks", None) or []):
        pk = w.pick if hasattr(w, "pick") else w
        rows.append({
            "round": int(getattr(pk, "round", 0) or 0),
            "original_team_id": team_id_from_key(getattr(pk, "original_team_key", None)),
            "source_team_id": team_id_from_key(getattr(pk, "source_team_key", None)),
            "dest_team_id": team_id_from_key(getattr(pk, "destination_team_key", None)),
        })
    return rows


def main():
    q = YahooFantasySportsQuery(league_id=LEAGUE_ID, game_code="nfl", game_id=GAME_ID,
                                env_file_location=HERE, save_token_data_to_env_file=True)
    txns = q.get_league_transactions()
    out = []
    for t in txns:
        faab = getattr(t, "faab_bid", None)
        out.append({
            "yahoo_transaction_id": int(t.transaction_id),
            "type": dec(t.type),
            "status": dec(t.status),
            "timestamp": datetime.fromtimestamp(int(t.timestamp)).isoformat(sep=" "),
            "faab_bid": int(faab) if faab not in (None, "") else None,
            "trader_team_id": team_id_from_key(getattr(t, "trader_team_key", None)),
            "tradee_team_id": team_id_from_key(getattr(t, "tradee_team_key", None)),
            "players": player_rows(t),
            "picks": pick_rows(t),
        })
    out.sort(key=lambda r: r["timestamp"])
    OUT.write_text(json.dumps({"season": SEASON, "pulled_at": datetime.now().isoformat(sep=" "),
                               "count": len(out), "transactions": out}, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    by_type = {}
    for r in out:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    print(f"wrote {len(out)} transactions to {OUT.name}")
    print("by type:", by_type)
    print("date range:", out[0]["timestamp"] if out else None, "->", out[-1]["timestamp"] if out else None)
    print("Nothing was written to fantasy.db.")


if __name__ == "__main__":
    main()
