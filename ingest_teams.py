"""
ingest_teams.py — pull teams and managers from Yahoo for each season in the
seasons table, populate managers and teams tables.

Idempotent: safe to re-run. Updates nicknames and team names if they've changed.

SAFETY GUARD (added 2026-07-01 after a corruption incident): Yahoo sometimes
returns MASKED manager guids ('--hidden--') — a privacy/auth artifact. Because
managers are keyed by guid, a masked pull collapses every team onto one bogus
manager and repoints the whole teams table. So we now pull everything FIRST,
validate the guids, and refuse to write anything if the pull looks untrustworthy
(masked or duplicate guids). The teams/managers tables are left untouched on
abort. Retrying (re-auth / try again later) usually returns real guids.
"""

import sqlite3
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()
project_dir = Path(__file__).parent
DB_PATH = project_dir / "fantasy.db"

# Manager guids Yahoo returns when it hides identities, plus blanks.
MASKED_GUIDS = {None, "", "--hidden--"}


def decode(v):
    """yfpy returns some text fields as bytes; decode for storage."""
    return v.decode("utf-8") if isinstance(v, bytes) else v


def get_or_create_manager(conn, guid, nickname):
    """Insert manager on first sight; refresh nickname. Return manager_id."""
    conn.execute(
        "INSERT OR IGNORE INTO managers (yahoo_guid, nickname) VALUES (?, ?)",
        (guid, nickname),
    )
    conn.execute(
        "UPDATE managers SET nickname = ? WHERE yahoo_guid = ?",
        (nickname, guid),
    )
    row = conn.execute(
        "SELECT manager_id FROM managers WHERE yahoo_guid = ?", (guid,)
    ).fetchone()
    return row[0]


def upsert_team(conn, season, yahoo_team_id, team_name, manager_id):
    """Insert team-season row; update name/manager if changed."""
    conn.execute(
        """INSERT OR IGNORE INTO teams
           (season, yahoo_team_id, team_name, manager_id)
           VALUES (?, ?, ?, ?)""",
        (season, yahoo_team_id, team_name, manager_id),
    )
    conn.execute(
        """UPDATE teams
           SET team_name = ?, manager_id = ?
           WHERE season = ? AND yahoo_team_id = ?""",
        (team_name, manager_id, season, yahoo_team_id),
    )


def collect_season_teams(query, season):
    """Pull one season's teams from Yahoo into plain dicts. No DB writes."""
    rows = []
    for t in query.get_league_teams():
        m_wrapper = t.managers[0]
        m = m_wrapper.manager if hasattr(m_wrapper, "manager") else m_wrapper
        rows.append({
            "season": season,
            "yahoo_team_id": t.team_id,
            "team_name": decode(t.name),
            "guid": m.guid,
            "nickname": decode(m.nickname),
        })
    return rows


def validate(rows):
    """Return a list of problems; empty means the pull is safe to write.

    Guards against Yahoo masking manager identities, which would otherwise
    corrupt the teams -> managers mapping (the 2026-07-01 incident)."""
    problems = []

    masked = [r for r in rows if r["guid"] in MASKED_GUIDS]
    if masked:
        example = {r["season"] for r in masked}
        problems.append(
            f"{len(masked)} team(s) came back with a masked/blank manager guid "
            f"(e.g. '--hidden--') in season(s) {sorted(example)}."
        )

    # Within a season, every team must have a DISTINCT manager guid. Duplicates
    # mean Yahoo couldn't (or wouldn't) tell the managers apart.
    for season in sorted({r["season"] for r in rows}):
        guids = [r["guid"] for r in rows if r["season"] == season]
        dupes = [g for g, n in Counter(guids).items() if n > 1]
        if dupes:
            problems.append(
                f"Season {season}: {len(guids)} teams but duplicate manager guids "
                f"{dupes} — can't distinguish managers."
            )
    return problems


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    seasons = conn.execute(
        "SELECT season, nfl_game_id, yahoo_league_id FROM seasons ORDER BY season"
    ).fetchall()

    # --- Pass 1: pull every season from Yahoo (no DB writes yet) ---
    all_rows = []
    for season, nfl_game_id, yahoo_league_id in seasons:
        print(f"\n--- Pulling season {season} (game_id={nfl_game_id}, league_id={yahoo_league_id}) ---")
        query = YahooFantasySportsQuery(
            league_id=yahoo_league_id,
            game_code="nfl",
            game_id=nfl_game_id,
            env_file_location=project_dir,
            save_token_data_to_env_file=True,
        )
        rows = collect_season_teams(query, season)
        print(f"  Pulled {len(rows)} teams from Yahoo")
        all_rows.extend(rows)

    # --- Guard: refuse to write if Yahoo masked the manager identities ---
    problems = validate(all_rows)
    if problems:
        print("\n*** ABORTED — the Yahoo pull looks untrustworthy, so NO changes were made:")
        for p in problems:
            print("   - " + p)
        print("\nThe teams/managers tables were left exactly as they were. This usually")
        print("clears on a retry (re-auth, or try again shortly). If it persists, the")
        print("league may have hidden manager info on Yahoo's side.")
        conn.close()
        raise SystemExit(1)

    # --- Pass 2: pull validated, safe to write ---
    for r in all_rows:
        manager_id = get_or_create_manager(conn, r["guid"], r["nickname"])
        upsert_team(conn, r["season"], r["yahoo_team_id"], r["team_name"], manager_id)
    conn.commit()

    n_managers = conn.execute("SELECT COUNT(*) FROM managers").fetchone()[0]
    n_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    print(f"\n=== Summary ===")
    print(f"  managers table: {n_managers} rows")
    print(f"  teams table: {n_teams} rows")

    conn.close()


if __name__ == "__main__":
    main()
