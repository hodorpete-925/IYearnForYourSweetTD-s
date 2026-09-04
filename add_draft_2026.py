"""add_draft_2026.py - the 2026 draft results, into the DB.

The Yahoo API is dead (Aug 2026), so ingest_drafts.py can't run. This
script loads the same thing from draft_results_2026.json (captured from
the league's Yahoo draft-results page on 2026-09-04, with the Sep 3
commissioner amendment applied: Fear the Peel was not allowed to draft
Kenneth Walker III at 1.01, so Fear the Peel holds Jeremiyah Love and
TheDarkKnight06 holds Walker - recorded as the amended DRAFT, not as
transactions, so both anchor at round 1 for DRC).

What it writes (all under --commit only):
  1. players            - INSERT OR IGNORE the rookies not yet in the
                          table (Yahoo player ids from the results page);
                          refresh nfl_team / position for drafted players
                          whose Yahoo listing moved (display only).
  2. draft_picks        - 192 rows for season 2026, is_keeper set from the
                          Yahoo keeper icon (which matched keeper_selections
                          exactly at capture - re-validated here, hard stop
                          on any drift).
  3. keeper_status_overrides - one row per 2026 pick (1 = kept, 0 = fresh)
                          so compute_drc / draft_history classify 2026 rows
                          from the record instead of the same-manager-last-
                          year inference (which fails for traded-in keepers
                          and for Bill Keenan's handoff roster).
  4. teams              - 2026 team-name renames seen on draft day
                          (JUST THE TUA US -> ROOTIN' TUTEN' COWBOY, etc.).

Read-only unless --commit (opens file:...?mode=ro, no DDL, nothing
touched). Idempotent: existing draft_picks rows are compared, not
duplicated; a mismatch is an ERROR that blocks commit.

Run:  python add_draft_2026.py             # dry-run + full plan
      python add_draft_2026.py --commit    # apply
Prereq: add_keeper_selections.py --commit (keeper_selections is the
cross-check for the 126 keeper flags).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
SRC = HERE / "draft_results_2026.json"
SEASON = 2026
OVERRIDE_SOURCE = "draft_results_2026"


def load_source():
    doc = json.loads(SRC.read_text(encoding="utf-8"))
    picks = doc["picks"]
    if len(picks) != 192:
        sys.exit(f"ERROR: expected 192 picks, file has {len(picks)}")
    overalls = sorted(p["overall"] for p in picks)
    if overalls != list(range(1, 193)):
        sys.exit("ERROR: overall picks are not exactly 1..192")
    return doc, picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    doc, picks = load_source()

    if args.commit:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys = ON;")
    else:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    errors = []

    # ---- 1. teams: resolve yahoo_team_id -> team_season_id, spot renames --
    team_rows = {r["yahoo_team_id"]: r for r in conn.execute(
        "SELECT team_season_id, yahoo_team_id, team_name FROM teams WHERE season = ?",
        (SEASON,))}
    if len(team_rows) != 12:
        errors.append(f"teams: expected 12 season-{SEASON} rows, found {len(team_rows)}")
    renames = []
    tsid_by_yid = {}
    for name, yid in doc["_meta"]["teams"].items():
        row = team_rows.get(yid)
        if row is None:
            errors.append(f"teams: no season-{SEASON} row for yahoo_team_id {yid} ({name})")
            continue
        tsid_by_yid[yid] = row["team_season_id"]
        if row["team_name"] != name:
            renames.append((yid, row["team_name"], name))

    # ---- 2. players: new ids, and drifted team/pos labels -----------------
    new_players, label_updates = [], []
    for p in picks:
        row = conn.execute(
            "SELECT player_name, position, nfl_team FROM players WHERE player_id = ?",
            (p["player_id"],)).fetchone()
        if row is None:
            new_players.append(p)
        else:
            if (row["nfl_team"] or "") != p["nfl"] or (row["position"] or "") != p["pos"]:
                label_updates.append((p, row["position"], row["nfl_team"]))

    # ---- 3. keeper flags vs keeper_selections ------------------------------
    ks = {(r["team_season_id"], r["player_id"]) for r in conn.execute(
        "SELECT team_season_id, player_id FROM keeper_selections WHERE season = ?",
        (SEASON,))}
    flagged = {(tsid_by_yid.get(p["yahoo_team_id"]), p["player_id"])
               for p in picks if p["keeper"]}
    if flagged != ks:
        only_draft = flagged - ks
        only_ks = ks - flagged
        errors.append(f"keeper flags drift: {len(only_draft)} flagged on the draft "
                      f"but not in keeper_selections {sorted(only_draft)[:5]}; "
                      f"{len(only_ks)} in keeper_selections but not flagged {sorted(only_ks)[:5]}")

    # ---- 4. draft_picks: existing rows must match, new rows planned --------
    existing = {r["overall_pick"]: r for r in conn.execute(
        "SELECT overall_pick, draft_round, pick_in_round, team_season_id, player_id, is_keeper "
        "FROM draft_picks WHERE season = ?", (SEASON,))}
    to_insert, matched = [], 0
    for p in picks:
        tsid = tsid_by_yid.get(p["yahoo_team_id"])
        want = (p["round"], p["slot"], tsid, p["player_id"], 1 if p["keeper"] else 0)
        have = existing.get(p["overall"])
        if have is None:
            to_insert.append((p, want))
        else:
            got = (have["draft_round"], have["pick_in_round"], have["team_season_id"],
                   have["player_id"], have["is_keeper"])
            if got != want:
                errors.append(f"draft_picks {SEASON} #{p['overall']} exists with {got}, "
                              f"file says {want} ({p['player']})")
            else:
                matched += 1

    # ---- 5. keeper_status_overrides plan -----------------------------------
    ov_existing = {(r["player_id"], r["team_season_id"]): r["is_keeper"] for r in conn.execute(
        "SELECT player_id, team_season_id, is_keeper FROM keeper_status_overrides WHERE season = ?",
        (SEASON,))}
    ov_insert = []
    for p in picks:
        tsid = tsid_by_yid.get(p["yahoo_team_id"])
        flag = 1 if p["keeper"] else 0
        have = ov_existing.get((p["player_id"], tsid))
        if have is None:
            ov_insert.append((p, tsid, flag))
        elif have != flag:
            errors.append(f"keeper_status_overrides {SEASON} {p['player']} exists as "
                          f"{have}, draft says {flag}")

    # ---- report -----------------------------------------------------------
    per_team = {}
    for p in picks:
        per_team[p["team"]] = per_team.get(p["team"], 0) + 1
    bad_counts = {t: n for t, n in per_team.items() if n != 16}
    if bad_counts:
        errors.append(f"pick counts per team not 16: {bad_counts}")

    print(f"Source: {SRC.name} - {len(picks)} picks, "
          f"{sum(1 for p in picks if p['keeper'])} keepers, "
          f"{sum(1 for p in picks if not p['keeper'])} live picks")
    for a in doc["_meta"].get("amendments", []):
        print(f"  amendment {a['date']}: {a['note']}")
    print(f"\nTeam renames ({len(renames)}):")
    for yid, old, new in renames:
        print(f"  yahoo_team_id {yid}: {old!r} -> {new!r}")
    print(f"\nNew players ({len(new_players)}):")
    for p in new_players:
        print(f"  {p['player_id']:>6}  {p['player']} ({p['pos']} - {p['nfl']})  pick {p['round']}.{p['slot']:02d}")
    print(f"\nPlayer label refreshes ({len(label_updates)}):")
    for p, pos, nfl in label_updates:
        print(f"  {p['player']}: {pos}/{nfl} -> {p['pos']}/{p['nfl']}")
    print(f"\ndraft_picks {SEASON}: {matched} already present and matching, {len(to_insert)} to insert")
    print(f"keeper_status_overrides {SEASON}: {len(ov_existing)} present, {len(ov_insert)} to insert "
          f"({sum(1 for _, _, f in ov_insert if f)} kept / {sum(1 for _, _, f in ov_insert if not f)} fresh)")

    if errors:
        print("\nERRORS (nothing written):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    # ---- commit -------------------------------------------------------------
    with conn:
        for yid, old, new in renames:
            conn.execute("UPDATE teams SET team_name = ? WHERE season = ? AND yahoo_team_id = ?",
                         (new, SEASON, yid))
        for p in new_players:
            conn.execute("INSERT OR IGNORE INTO players (player_id, player_name, position, nfl_team) "
                         "VALUES (?, ?, ?, ?)", (p["player_id"], p["player"], p["pos"], p["nfl"]))
        for p, _, _ in label_updates:
            conn.execute("UPDATE players SET position = ?, nfl_team = ? WHERE player_id = ?",
                         (p["pos"], p["nfl"], p["player_id"]))
        for p, want in to_insert:
            rnd, slot, tsid, pid, flag = want
            conn.execute("INSERT INTO draft_picks (season, overall_pick, draft_round, pick_in_round, "
                         "team_season_id, player_id, is_keeper) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (SEASON, p["overall"], rnd, slot, tsid, pid, flag))
        for p, tsid, flag in ov_insert:
            conn.execute("INSERT INTO keeper_status_overrides (season, player_id, team_season_id, "
                         "is_keeper, source, note) VALUES (?, ?, ?, ?, ?, ?)",
                         (SEASON, p["player_id"], tsid, flag, OVERRIDE_SOURCE,
                          f"{SEASON} draft pick {p['round']}.{p['slot']:02d}"
                          + (" (keeper)" if flag else " (live pick)")))
    n = conn.execute("SELECT COUNT(*) FROM draft_picks WHERE season = ?", (SEASON,)).fetchone()[0]
    k = conn.execute("SELECT COUNT(*) FROM keeper_status_overrides WHERE season = ?", (SEASON,)).fetchone()[0]
    print(f"\nCOMMITTED: draft_picks {SEASON} = {n} rows, keeper_status_overrides {SEASON} = {k} rows, "
          f"{len(new_players)} players added, {len(renames)} teams renamed.")
    print("Next: python generate_dashboard.py (or refresh.py) to rebuild the site.")


if __name__ == "__main__":
    main()
