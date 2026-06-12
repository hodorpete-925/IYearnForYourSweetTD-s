"""
ingest_adp_2qb.py - pull 2-QB ADP from FantasyFootballCalculator's JSON API
and REPLACE the adp table rows for each ingested season.

Why: the league is a 2-QB league. The old FantasyPros benchmark was 1-QB
ADP, which buries QBs (Lamar ~pick 56 in 1-QB vs ~pick 2 in 2-QB) and made
every rostered QB look "Overpriced" on the dashboard.

Source: https://fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12&year=YYYY
  - Real 2-QB mock drafts, JSON, no scraping.
  - Each historical year serves the FINAL pre-season window (mocking stops
    at kickoff), i.e., the season-start ADP snapshot we want.
  - Quirk (June 2026): year=2025 errors on their side, but the "current"
    endpoint (no year param) still serves the 2025 final window. We use
    that, and VALIDATE the window dates before storing anything.
  - 2026 mocks haven't started yet. Run `python ingest_adp_2qb.py 2026`
    weekly from late July; it refuses to store data until the window
    dates actually say 2026.

Usage:
    python ingest_adp_2qb.py 2023            # one season
    python ingest_adp_2qb.py all             # 2023, 2024, 2025, 2026 (skips unavailable)
    python ingest_adp_2qb.py clear-2026      # explicitly wipe the stale 1-QB 2026 rows
                                             # (until real 2-QB 2026 data exists)

Per season the script: fetch -> validate window dates -> DELETE all adp rows
for that season (any source) -> insert fresh rows as source='ffc_2qb'.
Nothing is deleted unless validated replacement data is in hand.

After ingesting, rerun:  python match_adp_players.py
then check adp_unmatched.csv, then regenerate the dashboard.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "fantasy.db"
SOURCE = "ffc_2qb"
API = "https://fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12"
SEASONS = (2023, 2024, 2025, 2026)

HEADERS = {"User-Agent": "Mozilla/5.0 (fantasy keeper-league research)"}

# Yahoo uses K; FFC uses PK.
POS_MAP = {"PK": "K"}


def fetch_season(season):
    """Fetch one season. Returns (meta, players) or (None, reason)."""
    urls = [f"{API}&year={season}"]
    # Their year=2025 endpoint is currently broken; the no-year endpoint
    # still serves the latest completed window. Try it as a fallback for
    # any season whose explicit URL fails — date validation keeps us honest.
    urls.append(API)

    last_reason = "no response"
    for url in urls:
        try:
            j = requests.get(url, headers=HEADERS, timeout=30).json()
        except Exception as e:
            last_reason = f"request failed: {e}"
            continue
        if j.get("status") != "Success" or not j.get("players"):
            last_reason = f"API status={j.get('status')!r}"
            continue
        start = str(j["meta"].get("start_date", ""))
        if not start.startswith(str(season)):
            last_reason = (f"window starts {start!r}, not {season} — "
                           "data for this season isn't published yet")
            continue
        return j["meta"], j["players"]
    return None, last_reason


def rows_from_players(players):
    """Convert FFC player dicts -> adp-table row dicts."""
    players = sorted(players, key=lambda p: p["adp"])
    pos_counts = {}
    rows = []
    for i, p in enumerate(players, 1):
        pos = POS_MAP.get(p.get("position"), p.get("position"))
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        bye = p.get("bye") or None  # FFC uses 0 for free agents
        rows.append({
            "name": p["name"].strip(),
            "overall_rank": i,
            "pos_rank": f"{pos}{pos_counts[pos]}" if pos else None,
            "nfl_team": (p.get("team") or None),
            "bye_week": bye,
            "adp": float(p["adp"]),
        })
    return rows


def replace_season(conn, season, rows, fetched_at):
    """Delete ALL adp rows for the season (any source), insert fresh ones."""
    deleted = conn.execute(
        "DELETE FROM adp WHERE season=?", (season,)
    ).rowcount
    conn.executemany(
        "INSERT INTO adp (season, source, player_name_raw, overall_rank, "
        " position_rank, nfl_team, bye_week, adp, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(season, SOURCE, r["name"], r["overall_rank"], r["pos_rank"],
          r["nfl_team"], r["bye_week"], r["adp"], fetched_at) for r in rows],
    )
    conn.commit()
    return deleted


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python ingest_adp_2qb.py <season|all|clear-2026>")
    arg = sys.argv[1]

    conn = sqlite3.connect(DB_PATH)

    if arg == "clear-2026":
        n = conn.execute("DELETE FROM adp WHERE season=2026").rowcount
        conn.commit()
        print(f"Cleared {n} stale 2026 ADP rows (old 1-QB benchmark).")
        print("Dashboard will show no 2026 ADP/value tags until the 2-QB")
        print("ingest succeeds (rerun `python ingest_adp_2qb.py 2026` from late July).")
        return

    seasons = SEASONS if arg == "all" else (int(arg),)
    if any(s not in SEASONS for s in seasons):
        sys.exit(f"Season must be one of {SEASONS}")

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for season in seasons:
        print(f"\nSeason {season}:")
        meta, players = fetch_season(season)
        if meta is None:
            print(f"  SKIPPED — {players}")
            continue
        print(f"  window {meta['start_date']} .. {meta['end_date']}, "
              f"{meta['total_drafts']} drafts, {meta['type']}, "
              f"{meta['teams']} teams")
        rows = rows_from_players(players)
        deleted = replace_season(conn, season, rows, fetched_at)
        qbs = [r for r in rows if (r["pos_rank"] or "").startswith("QB")][:3]
        print(f"  replaced {deleted} old rows with {len(rows)} 2-QB rows")
        print(f"  top QBs: " + ", ".join(f"{r['name']} (adp {r['adp']})" for r in qbs))

    conn.close()
    print("\nNext: python match_adp_players.py   (then review adp_unmatched.csv)")


if __name__ == "__main__":
    main()
