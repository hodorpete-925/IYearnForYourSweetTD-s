"""
create_database.py — initialize fantasy.db with the schema and seed data.

Idempotent-ish: refuses to run if fantasy.db already exists, so you don't
accidentally wipe a populated database. To rebuild, delete fantasy.db first.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "fantasy.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Static data — the single source of truth for season → game/league mappings.
# When 2027 rolls around, add a row here.
SEASONS = [
    # (season, nfl_game_id, yahoo_league_id, league_key)
    (2023, 423, "1135115", "423.l.1135115"),
    (2024, 449, "17476",   "449.l.17476"),
    (2025, 461, "48079",   "461.l.48079"),
    (2026, 470, "4416",    "470.l.4416"),
]


def main():
    if DB_PATH.exists():
        print(f"ERROR: {DB_PATH.name} already exists. Delete it first if you want a fresh build.")
        sys.exit(1)

    print(f"Creating {DB_PATH.name}...")
    conn = sqlite3.connect(DB_PATH)

    # Turn on foreign key enforcement (off by default in SQLite, irritatingly)
    conn.execute("PRAGMA foreign_keys = ON;")

    print(f"Running {SCHEMA_PATH.name}...")
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)

    print("Seeding seasons...")
    conn.executemany(
        "INSERT INTO seasons (season, nfl_game_id, yahoo_league_id, league_key) VALUES (?, ?, ?, ?)",
        SEASONS,
    )

    conn.commit()
    conn.close()

    print(f"\nDone. {DB_PATH.name} is ready at {DB_PATH}")
    print("Open it in DBeaver to verify tables exist.")


if __name__ == "__main__":
    main()