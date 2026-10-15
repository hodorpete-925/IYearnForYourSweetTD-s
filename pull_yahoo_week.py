"""
pull_yahoo_week.py - the in-season Yahoo pull: this week's matchups, the
standings, and every team's lineup slots. Writes two sidecars the dashboard
already reads (shapes unchanged from the 9/17 Chrome captures):

    scores.json           -> League home matchups block + standings (+ the
                             live page re-fetches it on load)
    current_lineups.json  -> team-page lineup slots, Trade planner boards,
                             roster drift check

READ-ONLY against the DB (never opens fantasy.db). Side effects: the two
JSON files, and yfpy refreshing the token block in .env. Both files are
written atomically (temp file, then replace) only after every Yahoo call
has succeeded, so a failed pull leaves the previous good files in place.

Run host-side from the repo folder:
    venv/Scripts/python.exe pull_yahoo_week.py            # current week
    venv/Scripts/python.exe pull_yahoo_week.py --week 5   # a specific week
Called by refresh_all.py (--yahoo) and publish_scores.py (--pull).
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from yfpy.query import YahooFantasySportsQuery

HERE = Path(__file__).parent
SCORES = HERE / "scores.json"
LINEUPS = HERE / "current_lineups.json"
GAME_ID, LEAGUE_ID, SEASON = 470, "4416", 2026

MATCHUP_STATUS = {"preevent": "upcoming", "midevent": "in_progress", "postevent": "final"}


# ---------- small helpers ---------------------------------------------------

def dec(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def unwrap(obj, key):
    """yfpy sometimes hands back {'team': Team} / {'player': Player} wrappers."""
    if isinstance(obj, dict) and key in obj:
        return obj[key]
    return getattr(obj, key, obj)


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def team_id(t):
    tid = getattr(t, "team_id", None)
    if tid is None and getattr(t, "team_key", None):
        tid = str(dec(t.team_key)).split(".")[-1]
    return int(tid)


def record(ts):
    ot = getattr(ts, "outcome_totals", None)
    w, l, t = (getattr(ot, "wins", 0), getattr(ot, "losses", 0), getattr(ot, "ties", 0)) if ot else (0, 0, 0)
    return f"{int(w or 0)}-{int(l or 0)}-{int(t or 0)}"


def streak(ts):
    s = getattr(ts, "streak", None)
    if not s:
        return None
    kind = str(dec(getattr(s, "type", "") or "")).lower()
    letter = {"win": "W", "loss": "L", "tie": "T"}.get(kind, kind[:1].upper())
    return f"{letter}-{int(getattr(s, 'value', 0) or 0)}"


def now_et():
    # Scheduler runs on Pete's PC, which is on Eastern time.
    import time
    return datetime.now().strftime("%Y-%m-%d %H:%M ") + ("EDT" if time.localtime().tm_isdst > 0 else "EST")


def atomic_write(path, doc):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------- pulls ------------------------------------------------------------

def pull_scoreboard(q, week, standings_by_tid):
    sb = q.get_league_scoreboard_by_week(week)
    out, statuses = [], set()
    for m in (getattr(sb, "matchups", None) or []):
        m = unwrap(m, "matchup")
        teams = [unwrap(t, "team") for t in (getattr(m, "teams", None) or [])]
        if len(teams) != 2:
            print(f"  WARNING: matchup with {len(teams)} teams skipped")
            continue
        sides = []
        for t in teams:
            tid = team_id(t)
            ts = getattr(t, "team_standings", None)
            srow = standings_by_tid.get(tid, {})
            sides.append({
                "tid": tid,
                "team": dec(t.name),
                "proj": round(num(getattr(getattr(t, "team_projected_points", None), "total", 0)), 2),
                "pts": round(num(getattr(getattr(t, "team_points", None), "total", 0)), 2),
                # the scoreboard's team objects carry no standings; use the standings pull
                "rec": (record(ts) if ts else None) or srow.get("rec"),
                "rank": (int(getattr(ts, "rank", 0) or 0) if ts else None) or srow.get("rank"),
                "win_prob": round(num(getattr(t, "win_probability", 0)), 3) or None,
            })
        statuses.add(str(dec(getattr(m, "status", "") or "")))
        out.append({"a": sides[0], "b": sides[1],
                    "status": MATCHUP_STATUS.get(str(dec(getattr(m, "status", "") or "")), "unknown")})
    # week status = the least-finished matchup. Yahoo flips a week to
    # "midevent" on Tuesday, before any game; treat an all-zero board as upcoming.
    any_points = any(side["pts"] for m in out for side in (m["a"], m["b"]))
    if "midevent" in statuses and any_points:
        wk_status = "in_progress"
    elif statuses and statuses <= {"postevent"}:
        wk_status = "final"
    else:
        wk_status = "upcoming"
    return out, wk_status


def pull_standings(q):
    st = q.get_league_standings()
    out = []
    for t in (getattr(st, "teams", None) or []):
        t = unwrap(t, "team")
        ts = getattr(t, "team_standings", None)
        out.append({
            "rank": int(getattr(ts, "rank", 0) or 0) if ts else None,
            "tid": team_id(t),
            "team": dec(t.name),
            "rec": record(ts) if ts else None,
            "pf": round(num(getattr(ts, "points_for", 0)), 2) if ts else None,
            "pa": round(num(getattr(ts, "points_against", 0)), 2) if ts else None,
            "streak": streak(ts) if ts else None,
            "faab": int(num(getattr(t, "faab_balance", 0))),
            "waiver": int(num(getattr(t, "waiver_priority", 0))),
            "moves": int(num(getattr(t, "number_of_moves", 0))),
        })
    out.sort(key=lambda r: (r["rank"] or 99))
    return out


def pull_lineups(q, week, team_names):
    teams = {}
    for tid in sorted(team_names):
        roster = q.get_team_roster_player_info_by_week(tid, week)
        # yfpy returns a plain list of Player here (a Roster object elsewhere)
        plist = roster if isinstance(roster, list) else (getattr(roster, "players", None) or [])
        players = []
        for p in plist:
            p = unwrap(p, "player")
            sp = getattr(p, "selected_position", None)
            if isinstance(sp, list):
                sp = sp[0] if sp else None
            slot = dec(getattr(sp, "position", None)) if sp is not None else None
            pos = dec(getattr(p, "primary_position", None) or getattr(p, "display_position", ""))
            name = dec(getattr(getattr(p, "name", None), "full", None) or getattr(p, "full_name", ""))
            row = {"slot": slot, "id": int(p.player_id)}
            if pos == "DEF":
                # Yahoo DEF ids (1000xx) match the players table; the name is
                # kept for the loader's name fallback and for human eyes.
                row["name"] = name
            players.append(row)
        teams[str(tid)] = {"team": team_names[tid], "players": players}
        print(f"  team {tid:>2} {team_names[tid]!r:<36} {len(players)} players")
    return teams


# ---------- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=None, help="NFL week (default: league current_week)")
    args = ap.parse_args()

    q = YahooFantasySportsQuery(league_id=LEAGUE_ID, game_code="nfl", game_id=GAME_ID,
                                env_file_location=HERE, save_token_data_to_env_file=True)

    meta = q.get_league_metadata()
    week = args.week or int(getattr(meta, "current_week", 0) or 0)
    if not week:
        print("could not determine current week"); sys.exit(1)
    print(f"league {dec(meta.name)!r} season {getattr(meta, 'season', '?')} week {week}")

    print("standings ...")
    standings = pull_standings(q)
    print(f"  {len(standings)} teams")
    team_names = {r["tid"]: r["team"] for r in standings}
    standings_by_tid = {r["tid"]: r for r in standings}

    print("scoreboard ...")
    matchups, wk_status = pull_scoreboard(q, week, standings_by_tid)
    print(f"  {len(matchups)} matchups, week status {wk_status}")
    for m in matchups:
        print(f"    {m['a']['team'][:26]:<26} {m['a']['pts']:>6} / {m['a']['proj']:<7} vs  "
              f"{m['b']['team'][:26]:<26} {m['b']['pts']:>6} / {m['b']['proj']}")

    print("lineups ...")
    lineups = pull_lineups(q, week, team_names)

    stamp = now_et()
    atomic_write(SCORES, {
        "as_of": stamp,
        "week": week,
        "status": wk_status,
        "source": "Yahoo Fantasy Sports API (pull_yahoo_week.py)",
        "matchups": matchups,
        "standings": standings,
        "standings_as_of": stamp,
    })
    atomic_write(LINEUPS, {
        "as_of": stamp,
        "week": week,
        "source": "Yahoo Fantasy Sports API (pull_yahoo_week.py)",
        "teams": lineups,
    })
    print(f"wrote {SCORES.name} and {LINEUPS.name} ({stamp}). Nothing was written to fantasy.db.")


if __name__ == "__main__":
    main()
