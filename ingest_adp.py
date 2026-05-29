"""
ingest_adp.py - pull Average Draft Position data from FantasyPros for a given
season (or all seasons) and upsert into the adp table.

Usage:
    python ingest_adp.py 2026          # one season
    python ingest_adp.py all           # 2023, 2024, 2025, 2026

For historical (2023, 2024, 2025) the data is final - run once and forget.
For 2026 the data updates daily up to the draft; run weekly via the scheduled
task or on demand.

The script:
1. Fetches the FantasyPros overall ADP page (HTML).
2. Parses the table for: overall rank, name, position rank, NFL team, bye,
   ADP value.
3. Upserts to fantasy.db -> adp table keyed on (season, source, name).
4. Leaves player_id matching to a separate step (match_adp_players.py).
"""

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent / "fantasy.db"
SOURCE = "fantasypros"

# Map season -> URL. Current year omits the year param.
URLS = {
    2023: "https://www.fantasypros.com/nfl/adp/overall.php?year=2023",
    2024: "https://www.fantasypros.com/nfl/adp/overall.php?year=2024",
    2025: "https://www.fantasypros.com/nfl/adp/overall.php?year=2025",
    2026: "https://www.fantasypros.com/nfl/adp/overall.php",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html",
}


def fetch_page(url):
    """GET the FantasyPros page, return its HTML text."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


# Compact parser: each row tuple is
# (overall_rank, name, pos_rank, nfl_team, bye_week, adp_value)
def parse_table(html):
    """Pull rows out of the FantasyPros ADP table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="data") or soup.find("table")
    if table is None:
        raise RuntimeError("No table found on page - did FantasyPros change layout?")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue  # header row or filler

        # Cell 0: overall rank (e.g. "1")
        try:
            overall_rank = int(cells[0].get_text(strip=True))
        except ValueError:
            continue

        # Cell 1: player name + team + bye
        # Examples:
        #   "Ja'Marr Chase CIN (10)"
        #   "Christian McCaffrey SF (14)"
        player_cell_text = cells[1].get_text(" ", strip=True)
        # Pull team abbreviation and bye week off the end
        m = re.match(r"^(.*?)\s+([A-Z]{2,4})\s+\((\d+)\)\s*$", player_cell_text)
        if m:
            name = m.group(1).strip()
            nfl_team = m.group(2).strip()
            bye_week = int(m.group(3))
        else:
            # Defenses sometimes show "Kansas City Chiefs KC (10)" or similar.
            name = player_cell_text
            nfl_team = None
            bye_week = None

        # Cell 2: position rank ("WR1", "RB12")
        pos_rank = cells[2].get_text(strip=True)

        # ADP value is the "AVG" column. Source-rank columns (Sleeper, RTSports,
        # NFFC, etc.) are integers; AVG is always written with a decimal point
        # (e.g. "1.0", "10.5"). So we find the cell containing a literal "." -
        # that's the ADP value. Some years have multiple source columns before
        # AVG, so we don't hard-code an index.
        adp_value = None
        for c in cells[3:]:
            txt = c.get_text(strip=True).split()[0] if c.get_text(strip=True) else ""
            if "." not in txt:
                continue
            try:
                v = float(txt)
                if 0 < v < 500:
                    adp_value = v
                    # Don't break - take the LAST decimal cell, in case
                    # FantasyPros adds a new "real-time" column with decimals
            except ValueError:
                continue
        if adp_value is None:
            continue

        rows.append({
            "overall_rank": overall_rank,
            "name": name,
            "pos_rank": pos_rank or None,
            "nfl_team": nfl_team,
            "bye_week": bye_week,
            "adp": adp_value,
        })
    return rows


def upsert_rows(conn, season, rows, fetched_at):
    """Upsert ADP rows. Existing rows update; new rows insert."""
    inserted = updated = 0
    for r in rows:
        existing = conn.execute(
            "SELECT adp FROM adp WHERE season=? AND source=? AND player_name_raw=?",
            (season, SOURCE, r["name"])
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO adp
                  (season, source, player_name_raw, overall_rank,
                   position_rank, nfl_team, bye_week, adp, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (season, SOURCE, r["name"], r["overall_rank"],
                  r["pos_rank"], r["nfl_team"], r["bye_week"],
                  r["adp"], fetched_at))
            inserted += 1
        else:
            conn.execute("""
                UPDATE adp
                   SET overall_rank=?, position_rank=?, nfl_team=?,
                       bye_week=?, adp=?, fetched_at=?
                 WHERE season=? AND source=? AND player_name_raw=?
            """, (r["overall_rank"], r["pos_rank"], r["nfl_team"],
                  r["bye_week"], r["adp"], fetched_at,
                  season, SOURCE, r["name"]))
            updated += 1
    return inserted, updated


def ingest_season(conn, season):
    url = URLS[season]
    print(f"\nSeason {season}: {url}")
    html = fetch_page(url)
    rows = parse_table(html)
    print(f"  parsed {len(rows)} rows")
    if not rows:
        print(f"  WARNING: no rows extracted. Page layout may have changed.")
        return 0, 0
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ins, upd = upsert_rows(conn, season, rows, fetched_at)
    conn.commit()
    print(f"  inserted={ins}  updated={upd}")
    return ins, upd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("season", help="Season year (2023|2024|2025|2026) or 'all'")
    p.add_argument("--polite-delay", type=float, default=2.0,
                   help="Seconds between requests when ingesting multiple seasons")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.season == "all":
        seasons = sorted(URLS.keys())
    else:
        try:
            seasons = [int(args.season)]
        except ValueError:
            sys.exit(f"Invalid season: {args.season}")
        if seasons[0] not in URLS:
            sys.exit(f"No URL configured for season {seasons[0]}")

 