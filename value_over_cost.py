"""
value_over_cost.py - production vs keeper cost for the DRC value board.

Reader: generate_dashboard.render_value_board. Pure computation, no HTML.

Definitions (2026-10-14, Pete + Claude, modeled on the "FPOR by draft slot"
boards that circulate on X, swapped from ADP to DRC):

  weeks           completed NFL weeks with league data (max week in
                  player_weekly_stats for the season; a week counts once any
                  team has points in it)
  PPG             a player's points per recorded week (weeks on ANY league
                  roster; byes count as 0, missing weeks are ignored)
  replacement     per position, the PPG of the Nth-best player by PPG among
                  players with >= MIN_WEEKS recorded weeks. N is the number
                  of starters the league fields at that position plus a bench
                  allowance (REPLACEMENT_RANK). "What a free agent gives you."
  FPOR            fantasy points over replacement, season to date:
                  sum over recorded weeks of (points - replacement PPG)
  expected FPOR   the league's own price curve: ordinary least squares of
                  FPOR on DRC across every rostered player who was drafted or
                  kept at a DRC (pickups excluded from the fit; they are
                  priced at DRC 16 by rule, not by the market)
  residual        FPOR - expected FPOR at the player's DRC
  z               residual / standard deviation of residuals, clipped to
                  [-Z_CLIP, +Z_CLIP]. This is the color.

So green = returning more than this league paid for that cost tier,
red = less, and the middle of the scale is the league's own line, not zero.
"""
import math
import sqlite3
from collections import defaultdict

SEASON = 2026
MIN_WEEKS = 2
Z_CLIP = 2.0

# 12 teams: QB1, RB2, WR3, TE1, W/R/T, Q/W/R/T, K, DEF (superflex: ~24 QBs start). Flex and superflex
# shares are folded into the base positions, then a bench allowance added.
REPLACEMENT_RANK = {"QB": 28, "RB": 32, "WR": 42, "TE": 15, "K": 13, "DEF": 13}


def _norm_pos(p):
    p = (p or "").upper()
    if p in ("D/ST", "DST", "DEF"):
        return "DEF"
    return p.split(",")[0]


def load_weekly(conn, season=SEASON):
    """player_id -> {week: points} for the season; plus the set of completed weeks."""
    by_player = defaultdict(dict)
    weeks_with_points = set()
    for pid, wk, pts in conn.execute(
            "SELECT player_id, week, fantasy_points FROM player_weekly_stats WHERE season=?",
            (season,)):
        pts = float(pts or 0.0)
        by_player[pid][wk] = pts
        if pts > 0:
            weeks_with_points.add(wk)
    completed = max(weeks_with_points) if weeks_with_points else 0
    # drop weeks past the last completed one (a current week pulled mid-way)
    for pid in by_player:
        by_player[pid] = {w: p for w, p in by_player[pid].items() if w <= completed}
    return by_player, completed


def replacement_levels(conn, by_player):
    """position -> replacement PPG."""
    pos_of = dict(conn.execute("SELECT player_id, position FROM players"))
    ppg_by_pos = defaultdict(list)
    for pid, weeks in by_player.items():
        if len(weeks) < MIN_WEEKS:
            continue
        ppg_by_pos[_norm_pos(pos_of.get(pid))].append(sum(weeks.values()) / len(weeks))
    out = {}
    for pos, rank in REPLACEMENT_RANK.items():
        vals = sorted(ppg_by_pos.get(pos, []), reverse=True)
        if not vals:
            out[pos] = 0.0
        else:
            out[pos] = vals[min(rank, len(vals)) - 1]
    return out


def _ols(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def compute(conn, by_manager, season=SEASON):
    """Returns (rows, meta). One row per rostered 2026 player:
       player_id, name, position, nfl_team, manager, manager_actual, drc, dollars,
       acq, weeks, pts, ppg, repl, fpor, expected, resid, z, row_key
    row_key is the DRC 1..16 for drafted/kept/traded players and 'pickup' for
    in-season waiver/FA adds (DRC 16 by rule, shown on their own row)."""
    by_player, completed = load_weekly(conn, season)
    repl = replacement_levels(conn, by_player)

    rows = []
    for mgr_name, d in by_manager.items():
        for e in d["roster_2026"]:
            pid = e["player_id"]
            pos = _norm_pos(e.get("position"))
            weeks = by_player.get(pid, {})
            pts = sum(weeks.values())
            n = len(weeks)
            r = repl.get(pos, 0.0)
            fpor = sum(p - r for p in weeks.values())
            acq = e.get("acq") or "drafted"
            rows.append({
                "player_id": pid, "name": e["name"], "position": pos,
                "nfl_team": e.get("nfl_team") or "", "manager": d["manager"],
                "manager_actual": d.get("manager_actual", mgr_name),
                "team_name": d.get("team_name") or "",
                "drc": int(e.get("drc") or 16), "dollars": e.get("drc_dollars"),
                "acq": acq, "weeks": n, "pts": round(pts, 1),
                "ppg": round(pts / n, 2) if n else None, "repl": round(r, 2),
                "fpor": round(fpor, 1),
                "row_key": "pickup" if acq in ("waiver", "fa") else int(e.get("drc") or 16),
            })

    # price curve on drafted/kept/traded players with data
    fit = [(x["drc"], x["fpor"]) for x in rows if x["row_key"] != "pickup" and x["weeks"] >= MIN_WEEKS]
    a, b = _ols([f[0] for f in fit], [f[1] for f in fit])
    resids = []
    for x in rows:
        x["expected"] = round(a + b * x["drc"], 1)
        x["resid"] = round(x["fpor"] - x["expected"], 1) if x["weeks"] else None
        if x["resid"] is not None and x["row_key"] != "pickup":
            resids.append(x["resid"])
    sd = (sum(v * v for v in resids) / len(resids)) ** 0.5 if resids else 1.0
    sd = sd or 1.0
    for x in rows:
        x["z"] = None if x["resid"] is None else round(max(-Z_CLIP, min(Z_CLIP, x["resid"] / sd)), 3)

    meta = {"season": season, "weeks": completed, "replacement": repl,
            "curve": {"intercept": round(a, 2), "slope": round(b, 2), "n": len(fit)},
            "resid_sd": round(sd, 2), "z_clip": Z_CLIP, "replacement_rank": REPLACEMENT_RANK}
    return rows, meta


if __name__ == "__main__":   # quick look
    import json, sys
    sys.argv = [sys.argv[0]]
    import generate_dashboard as g
    conn = sqlite3.connect(g.DB_PATH); conn.row_factory = sqlite3.Row
    bm = g.build_data()
    by_manager = bm[0] if isinstance(bm, tuple) else bm
    rows, meta = compute(conn, by_manager)
    print(json.dumps(meta, indent=1))
    rows.sort(key=lambda r: -(r["z"] or -9))
    for r in rows[:8]: print("TOP", r["drc"], r["name"], r["position"], r["manager"], r["fpor"], r["expected"], r["z"])
    for r in rows[-8:]: print("BOT", r["drc"], r["name"], r["position"], r["manager"], r["fpor"], r["expected"], r["z"])
