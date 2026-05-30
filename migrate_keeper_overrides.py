"""migrate_keeper_overrides.py - add the keeper_status_overrides table.

Safe to run multiple times: uses CREATE TABLE IF NOT EXISTS.

    python migrate_keeper_overrides.py
"""

import sqlite3

DB = "fantasy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS keeper_status_overrides (
    season           INTEGER NOT NULL,
    player_id        INTEGER NOT NULL,
    team_season_id   INTEGER NOT NULL,
    is_keeper        INTEGER NOT NULL CHECK (is_keeper IN (0, 1)),
    source           TEXT    NOT NULL DEFAULT 'manual',
    note             TEXT,
    PRIMARY KEY (season, player_id, team_season_id),
    FOREIGN KEY (season)         REFERENCES seasons(season),
    FOREIGN KEY (player_id)      REFERENCES players(player_id),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id)
);
"""

def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM keeper_status_overrides").fetchone()[0]
    print(f"keeper_status_overrides table ready. Existing rows: {n}")

if __name__ == "__main__":
    main()
