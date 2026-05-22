"""Quick database sanity check — print row counts for all tables."""
import sqlite3

conn = sqlite3.connect("fantasy.db")
tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()

print("Table row counts:\n")
for (name,) in tables:
    count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(f"  {name:25} {count}")

conn.close()