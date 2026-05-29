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


# FantasyPros tags some rows with player status suffixes after the team/bye.
# Single letters: O Q D P R / Multi: IR SUS NA OUT QUE DTD
_STATUS_SUFFIX = r"O|Q|D|P|R|IR|SUS|NA|OUT|QUE|DTD"


def _parse_player_cell(text):
    """Parse the player-name cell. Returns (name, nfl_team, bye_week).

    Handles four shapes:
      "Joe Burrow CIN (7) O"   -> ("Joe Burrow", "CIN", 7)
      "Joe Burrow CIN (7)"     -> ("Joe Burrow", "CIN", 7)
      "Nick Chubb O"           -> ("Nick Chubb", None, None)
      "Amari Cooper"           -> ("Amari Cooper", None, None)
    """
    text = text.strip()

    # name + TEAM + (bye) + optional status
    m = re.match(
        rf"^(.*?)\s+([A-Z]{{2,4}})\s+\((\d+)\)(?:\s+(?:{_STATUS_SUFFIX}))?\s*$",
        text,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip(), int(m.group(3))

    # name + status (no team/bye) — only strip recognized status tokens
    m = re.match(rf"^(.+?)\s+(?:{_STATUS_SUFFIX})\s*$", text)
    if m:
        return m.group(1).strip(), None, None

    return text, None, None


def _is_adp_row(cells):
    """A cell list looks like an ADP row if cell[0] is an integer
    (overall rank) and one of the later cells is a decimal (ADP value)."""
    if len(cells) < 4:
        return False
    try:
        int(cells[0].get_text(strip=True))
    except ValueError:
        return False
    for c in cells[3:]:
        txt = c.get_text(strip=True).split()[0] if c.get_text(strip=True) else ""
        if "." in txt:
            try:
                v = float(txt)
                if 0 < v < 500:
                    return True
            except ValueError:
                pass
    return False


def _pick_best_table(soup):
    """Find the table with the most ADP-shaped rows. Resilient to
    FantasyPros table id/class changes across years."""
    best_table = None
    best_count = 0
    for t in soup.find_all("table"):
        count = sum(1 for tr in t.find_all("tr") if _is_adp_row(tr.find_all("td")))
        if count > best_count:
            best_count = count
            best_table = t
    return best_table, best_count


# Each row tuple is (overall_rank, name, pos_rank, nfl_team, bye_week, adp_value)
def parse_table(html):
    """Pull rows out of the FantasyPros ADP table."""
    soup = BeautifulSoup(html, "html.parser")
    table, count = _pick_best_table(soup)
    if table is None or count == 0:
        raise RuntimeError("No ADP-shaped table found on page")
    print(f"  selected table with {count} candidate rows")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not _is_adp_row(cells):
            continue

        # Cell 0: overall rank (e.g. "1")
        try:
            overall_rank = int(cells[0].get_text(strip=True))
        except ValueError:
            continue

        # Cell 1: player name + team + bye + optional status indicator
        # Examples:
        #   "Ja'Marr Chase CIN (10)"               - normal
        #   "Christian McCaffrey SF (14)"
        #   "Joe Burrow CIN (7) O"                 - O = Out
        #   "Justin Herbert LAC (5) Q"             - Q = Questionable
        #   "Nick Chubb O"                         - no team, has status
        #   "Amari Cooper"                         - just name
        player_cell_text = cells[1].get_text(" ", strip=True)
        name, nfl_team, bye_week = _parse_player_cell(player_cell_text)

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
            conn.execute(
                "INSERT INTO adp "
                "(season, source, player_name_raw, overall_rank, "
                " position_rank, nfl_team, bye_week, adp, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (season, SOURCE, r["name"], r["overall_rank"],
                 r["pos_rank"], r["nfl_team"], r["bye_week"],
                 r["adp"], fetched_at)
            )
            inserted += 1
        else:
            conn.execute(
                "UPDATE adp SET "
                "  overall_rank=?, position_rank=?, nfl_team=?, "
                "  bye_week=?, adp=?, fetched_at=? "
                "WHERE season=? AND source=? AND player_name_raw=?",
                (r["overall_rank"], r["pos_rank"], r["nfl_team"],
                 r["bye_week"], r["adp"], fetched_at,
                 season, SOURCE, r["name"])
            )
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
    p.add_argument("--clear", action="store_true",
                   help="Wipe existing adp rows for the target season(s) first")
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

    if args.clear:
        placeholders = ",".join("?" * len(seasons))
        n = conn.execute(
            f"DELETE FROM adp WHERE season IN ({placeholders})",
            seasons,
        ).rowcount
        conn.commit()
        print(f"Cleared {n} existing rows for seasons {seasons}")

    total_ins = total_upd = 0
    for i, season in enumerate(seasons):
        if i > 0:
            time.sleep(args.polite_delay)
        ins, upd = ingest_season(conn, season)
        total_ins += ins
        total_upd += upd

    conn.close()
    print(f"\nDone. Total inserted={total_ins}, updated={total_upd}")


if __name__ == "__main__":
    main()
