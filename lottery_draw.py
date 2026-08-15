"""lottery_draw.py — run the 2026 draft lottery, once, with a full audit trail.

Rules (published on the dashboard League Rules page + 2026 Beach Summit agenda):
- The six non-playoff teams (2025 final standings 7th-12th) draw for the 1.01
  on the AMENDED (inverted) weights, effective 2026-27:
      7th 50% / 8th 15% / 9th 12.5% / 10th 10% / 11th 7.5% / 12th 5%
- Odds are for the first overall pick only; remaining lottery slots fill from
  the same weights with each winner removed (renormalized each round).
- Picks 7-12 belong to the playoff teams in reverse finish order (champ = 12).

Method: secrets.SystemRandom (OS entropy, not seedable) — one draw, no
re-rolls. Every round logs the remaining pool, each seat's weight and
cumulative band, and the drawn value, so anyone can re-walk the math.

Usage:  python lottery_draw.py            # refuses to run if a result exists
        python lottery_draw.py --force    # explicit overwrite (don't)
"""
import json
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
RESULT = HERE / "lottery_result.json"
LOG = HERE / "lottery_draw_log.json"

# Seats keyed by 2025 FINAL standings (the published agenda assignment).
# manager_id -> (final_rank, weight_pct)
LOTTERY_SEATS = [
    (7, 50.0), (8, 15.0), (9, 12.5), (10, 10.0), (11, 7.5), (12, 5.0)
]
PLAYOFF_PICKS = {1: 12, 2: 11, 3: 10, 4: 9, 5: 8, 6: 7}  # final rank -> pick


def load_seats():
    """Resolve each 2025-final-standings rank to the manager + their CURRENT
    2026 team name (so renames and the new 8th-seat manager show correctly)."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # NOTE: join 2026 teams by MANAGER, not yahoo_team_id — Yahoo reshuffles
    # team ids on league renewal, so id-based joins mislabel teams.
    rows = con.execute("""
        SELECT s.rank, m.manager_id, m.full_name,
               COALESCE(t26.team_name, t25.team_name) AS team_name,
               (t26.team_season_id IS NULL) AS missing_2026
        FROM team_standings s
        JOIN teams t25 ON t25.team_season_id = s.team_season_id
        JOIN managers m ON m.manager_id = t25.manager_id
        LEFT JOIN teams t26 ON t26.season = 2026
             AND t26.manager_id = t25.manager_id
        WHERE s.season = 2025
        ORDER BY s.rank""").fetchall()
    con.close()
    return {r["rank"]: dict(r) for r in rows}


def main():
    if RESULT.exists() and "--force" not in sys.argv:
        sys.exit(f"{RESULT.name} already exists — the draw has been run. "
                 "One draw, no re-rolls. (--force only if you really mean it.)")

    seats = load_seats()
    # Optional CLI override for the 8th seat (Jon's replacement may not be in
    # the DB while the Yahoo API is down):  --seat8 "Name" [--seat8-team "Team"]
    if "--seat8" in sys.argv:
        i = sys.argv.index("--seat8")
        seats[8]["full_name"] = sys.argv[i + 1]
        if "--seat8-team" in sys.argv:
            j = sys.argv.index("--seat8-team")
            seats[8]["team_name"] = sys.argv[j + 1]
    pool = []
    for rank, weight in LOTTERY_SEATS:
        s = seats[rank]
        pool.append({
            "final_rank": rank,
            "manager": s["full_name"],
            "team": s["team_name"],
            "weight": weight,
        })

    rng = secrets.SystemRandom()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rounds, results = [], []

    for pick in range(1, 7):
        total = sum(p["weight"] for p in pool)
        draw = rng.random() * total          # uniform in [0, total)
        cum, winner_idx, bands = 0.0, None, []
        for i, p in enumerate(pool):
            lo, hi = cum, cum + p["weight"]
            bands.append({"manager": p["manager"], "weight": p["weight"],
                          "band": [round(lo, 4), round(hi, 4)]})
            if winner_idx is None and lo <= draw < hi:
                winner_idx = i
            cum = hi
        winner = pool.pop(winner_idx)
        rounds.append({
            "pick": pick, "total_weight": round(total, 4),
            "drawn_value": draw, "bands": bands,
            "winner": winner["manager"],
        })
        results.append({**winner, "pick": pick,
                        "odds_at_draw": round(100 * winner["weight"] / total, 2)})
        print(f"Pick {pick}: {winner['manager']}  "
              f"({winner['team']}) — {winner['weight']}% base odds, "
              f"{results[-1]['odds_at_draw']}% at time of draw")

    playoff = []
    for rank in sorted(PLAYOFF_PICKS):
        s = seats[rank]
        playoff.append({"pick": PLAYOFF_PICKS[rank], "final_rank": rank,
                        "manager": s["full_name"], "team": s["team_name"]})
    playoff.sort(key=lambda p: p["pick"])

    RESULT.write_text(json.dumps({
        "drawn_at_utc": ts,
        "method": "secrets.SystemRandom, sequential weighted draw, "
                  "winner removed each round (published amended weights)",
        "lottery": results, "playoff": playoff,
    }, indent=2))
    LOG.write_text(json.dumps({"drawn_at_utc": ts, "rounds": rounds}, indent=2))
    print(f"\nWrote {RESULT.name} + {LOG.name}")

    missing = [s for s in seats.values() if s["missing_2026"]]
    for s in missing:
        print(f"NOTE: no 2026 team row for {s['full_name']} "
              f"(showing 2025 name '{s['team_name']}')")


if __name__ == "__main__":
    main()
