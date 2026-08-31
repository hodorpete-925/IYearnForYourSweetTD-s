"""add_keeper_selections.py - the 2026 keeper commitments, into the DB.

Creates a `keeper_selections` table (Pete's ruling 2026-08-30: keepers
live in the DATABASE, not a one-off JSON; when the Yahoo API returns it
may become an ingest target) and loads the finalized 2026 selections.

Source: Yahoo "Review keepers" screenshots from Pete, 2026-08-30, with
the two trades-supersede violations already corrected on Yahoo and
EXCLUDED here (Josh Jacobs was checked by Paul but belongs to Tom;
Chris Olave was checked by Dan V. but belongs to George).

TRADED-IN PLAYERS: Pete's ruling 2026-08-30 - EVERY player acquired
in a 2026 off-season trade is being kept (assume for now). They live
in TRADED_IN_KEEPERS below (source tagged trade_addendum) because
Yahoo could not record them; each also needs a manual draft-slot
assignment in Yahoo. Script is idempotent - re-runs skip existing rows.

DRC + dollars come from generate_dashboard.build_data() board truth
(the engine, with all 2026 synthetic-trade freezes applied), NOT from
Yahoo's salary column (a dead remnant - Pete 2026-08-30). Stored values
are a snapshot-at-commit of the engine's answer; the engine stays canon.

VALIDATION built in: a keeper who is not on that manager's 2026 board
(wrong team, traded away, name typo) is an ERROR and blocks commit.

Run:  python add_keeper_selections.py             # dry-run + cost preview
      python add_keeper_selections.py --commit    # create table + insert
Prereq: setup_2026_franchise_updates.py --commit (Bill Keenan's team row).
"""
import argparse
import difflib
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "fantasy.db"
SOURCE = "yahoo_keeper_submissions_2026-08"

# ============================================================================
# 2026 KEEPER SELECTIONS (manager -> players kept)
# Defenses use the players-table nickname ("Broncos", "Texans", ...).
# ============================================================================
KEEPERS = {
    "Pete Hodor": [
        "Lamar Jackson", "Cam Skattebo", "Kyle Williams", "Isaac TeSlaa",
        "Luther Burden III", "Emeka Egbuka", "Sam Darnold", "Colston Loveland",
        "Kyren Williams", "Ladd McConkey",
    ],
    "Paul Lewis": [
        # Josh Jacobs excluded - traded to Tom 8/29 (trades supersede keepers)
        "Dak Prescott", "Amon-Ra St. Brown", "Wan'Dale Robinson",
        "Bijan Robinson", "Jared Goff", "Parker Washington",
        "Chris Rodriguez Jr.", "Quinshon Judkins", "Tucker Kraft", "Broncos",
    ],
    "George Mensing": [
        "Josh Allen", "Mike Evans", "Javonte Williams", "Tyler Shough",
        "Brian Thomas Jr.", "David Montgomery", "Tyjae Spears", "Jason Myers",
        "Texans",
    ],
    "Dan Vescuso": [
        # Chris Olave excluded - traded to George 8/20 (trades supersede keepers)
        "D'Andre Swift", "Jordan Love", "Kyler Murray",
    ],
    "Aric Tao": [
        "Shedeur Sanders", "Malik Nabers", "Braelon Allen", "Bryce Young",
        "Romeo Doubs", "Tyrone Tracy Jr.", "Jameson Williams",
        "Justin Jefferson", "Deshaun Watson",
    ],
    "Scott Montgomery": [
        "Baker Mayfield", "Brock Bowers", "J.K. Dobbins", "Daniel Jones",
        "Jayden Daniels", "Jayden Reed", "DJ Moore", "Chase Brown",
    ],
    "Brian Malconian": [
        "Joe Burrow", "Malik Willis", "Tua Tagovailoa", "Chris Godwin Jr.",
        "Ja'Marr Chase", "Drake Maye", "James Cook III", "Trey McBride",
        "De'Von Achane", "George Pickens", "Jaxon Smith-Njigba",
    ],
    "Greg Pearson": [
        # Quentin Johnston removed - traded to Scott 8/29 (reported 8/31);
        # fix_remove_johnston_keeper.py deletes his committed row.
        "Matthew Stafford", "Dalton Kincaid",
        "Justin Herbert", "Chuba Hubbard", "Tyler Allgeier", "Brock Purdy",
        "Michael Wilson", "Rico Dowdle", "Rhamondre Stevenson",
        "Christian Watson", "Chase McLaughlin", "Seahawks",
    ],
    "Tom Watson": [
        "Jalen Hurts", "Patrick Mahomes", "Kayshon Boutte", "Jordan Mason",
        "Zach Charbonnet", "Kyle Monangai", "Puka Nacua", "George Kittle",
        "Omarion Hampton", "Derrick Henry", "Drake London",
    ],
    "Dan MacNulty": [
        "Caleb Williams", "Jaxson Dart", "Bucky Irving", "Alec Pierce",
        "Sam LaPorta", "Hunter Henry", "Tank Dell", "Sean Tucker",
        "Harrison Butker",
    ],
    "Alex Schlosberg": [
        "Jacoby Brissett", "Michael Pittman Jr.", "Jalen Coker",
        "Jahmyr Gibbs", "Jaylen Warren", "Harold Fannin Jr.",
        "Christian McCaffrey", "C.J. Stroud", "Aaron Rodgers",
        "Jalen McMillan", "Rome Odunze",
    ],
    "Bill Keenan": [
        "Trevor Lawrence", "Nico Collins", "Courtland Sutton",
        "Saquon Barkley", "Tyler Warren", "Kyle Pitts Sr.", "Bo Nix",
        "Rashee Rice",
    ],
}

# Board truth keys by the manager who RAN the 2025 roster; selections key
# by the CURRENT face. Bridge the 2026 handoff.
BOARD_KEY = {"Bill Keenan": "Jon Lewitus"}

# Players acquired via 2026 off-season trades - ALL kept (Pete 2026-08-30).
# Frozen trade-time DRC comes from board truth like everything else.
SOURCE_TRADE = "trade_addendum_2026-08-30"
TRADED_IN_KEEPERS = {
    "Pete Hodor": ["Terry McLaurin"],
    "Paul Lewis": ["TreVeyon Henderson"],
    # Josh Jacobs NOT kept by Tom (Pete 2026-08-31) — removed from the
    # addendum; fix_remove_jacobs_keeper.py deletes the committed row.
    "Tom Watson": ["A.J. Brown"],
    "George Mensing": ["Chris Olave"],
    "Dan Vescuso": ["Jaylen Waddle", "Tetairoa McMillan"],
    "Brian Malconian": ["Ashton Jeanty", "Blake Corum"],
    "Aric Tao": ["Jonathan Taylor"],
    "Scott Montgomery": ["Bhayshul Tuten", "Quentin Johnston"],
    "Greg Pearson": ["Woody Marks"],
    # 8/27 trade (reported 8/31): Lamb + Addison to Alex, Brown to Tom
    "Alex Schlosberg": ["CeeDee Lamb", "Jordan Addison"],
}

DDL = """
CREATE TABLE IF NOT EXISTS keeper_selections (
    season          INTEGER NOT NULL,
    team_season_id  INTEGER NOT NULL,
    player_id       INTEGER NOT NULL,
    drc             INTEGER NOT NULL,
    drc_dollars     INTEGER NOT NULL,
    source          TEXT,
    note            TEXT,
    PRIMARY KEY (season, team_season_id, player_id),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id),
    FOREIGN KEY (player_id)      REFERENCES players(player_id)
)
"""


def normalize(s):
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    print("Building board truth from the DRC engine (takes a minute)...")
    import generate_dashboard as gd
    by_manager, failures, _search, _trades = gd.build_data()
    if failures:
        print(f"  note: engine reported {len(failures)} DRC failure(s) "
              f"(pre-existing, not keeper-related)")

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    planned = []   # (mgr, team_season_id, player_id, name, pos, drc, dollars)
    errors = []

    all_names = {m: [(n, SOURCE) for n in KEEPERS[m]] for m in KEEPERS}
    for m, extra in TRADED_IN_KEEPERS.items():
        all_names.setdefault(m, []).extend((n, SOURCE_TRADE) for n in extra)

    for mgr, names in all_names.items():
        board_mgr = BOARD_KEY.get(mgr, mgr)
        board = by_manager.get(board_mgr)
        if board is None:
            errors.append(f"{mgr}: no board found (key {board_mgr!r})")
            continue
        trow = conn.execute("""
            SELECT t.team_season_id FROM teams t
            JOIN managers m ON m.manager_id = t.manager_id
            WHERE m.full_name = ? AND t.season = 2026
        """, (mgr,)).fetchone()
        if trow is None:
            errors.append(f"{mgr}: no 2026 team row - run "
                          f"setup_2026_franchise_updates.py --commit first")
            continue
        tsid = trow[0]
        pool = {normalize(p["name"]): p for p in board["players"]}
        for name, src in names:
            p = pool.get(normalize(name))
            if p is None:
                close = difflib.get_close_matches(
                    normalize(name), pool.keys(), n=1, cutoff=0.8)
                if close:
                    p = pool[close[0]]
                    print(f"  fuzzy: {mgr}: {name!r} -> {p['name']!r}")
                else:
                    errors.append(
                        f"{mgr}: {name!r} is NOT on this manager's 2026 "
                        f"board (traded away, wrong team, or typo)")
                    continue
            planned.append((mgr, tsid, p["player_id"], p["name"],
                            p["position"], p["drc"], p["drc_dollars"], src))

    print(f"\n=== 2026 keeper commitments"
          f" ({len(planned)} keeper(s) across {len(KEEPERS)} team(s)) ===")
    league_total = 0
    for mgr in all_names:
        rows = [x for x in planned if x[0] == mgr]
        total = sum(x[6] for x in rows)
        league_total += total
        print(f"\n  {mgr}  -  {len(rows)} keeper(s), ${total} to the pot")
        for _m, _t, _pid, nm, pos, drc, dol, src in sorted(rows, key=lambda x: (x[5], x[3])):
            tag = "  [trade]" if src == SOURCE_TRADE else ""
            print(f"      DRC {drc:>2}  ${dol:>3}   {nm} ({pos}){tag}")
    print(f"\n  LEAGUE TOTAL: ${league_total}")

    if errors:
        print(f"\n=== {len(errors)} ERROR(S) - fix before commit ===")
        for e in errors:
            print("  !", e)

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return
    if errors:
        print("\nREFUSING to commit with errors above.")
        return

    conn.execute(DDL)
    inserted = skipped = 0
    for mgr, tsid, pid, nm, _pos, drc, dol, src in planned:
        exists = conn.execute("""
            SELECT 1 FROM keeper_selections
            WHERE season = 2026 AND team_season_id = ? AND player_id = ?
        """, (tsid, pid)).fetchone()
        if exists:
            skipped += 1
            continue
        conn.execute("""
            INSERT INTO keeper_selections
                (season, team_season_id, player_id, drc, drc_dollars, source)
            VALUES (2026, ?, ?, ?, ?, ?)
        """, (tsid, pid, drc, dol, src))
        inserted += 1
    conn.commit()
    print(f"\nDone. Inserted {inserted}, skipped {skipped} already present.")


if __name__ == "__main__":
    main()
