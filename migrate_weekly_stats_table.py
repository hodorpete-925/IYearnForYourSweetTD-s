"""Create the player_weekly_stats table. Idempotent: safe to re-run."""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).parent / "fantasy.db")
conn.executescript("""
CREATE TABLE IF NOT EXISTS player_weekly_stats (
    season              INTEGER NOT NULL,
    week                INTEGER NOT NULL,
    player_id           INTEGER NOT NULL,
    team_season_id      INTEGER,
    fantasy_points      REAL,
    fetched_at          DATETIME NOT NULL,
    PRIMARY KEY (season, week, player_id),
    FOREIGN KEY (player_id)      REFERENCES players(player_id),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id)
);

CREATE INDEX IF NOT EXISTS idx_pws_player_season
    ON player_weekly_stats(player_id, season);
""")
conn.commit()
print("Schema applied.")
exists = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='player_weekly_stats'"
).fetchone()
print(f"player_weekly_stats exists: {bool(exists)}")
conn.close()
