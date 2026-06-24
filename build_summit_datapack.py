"""
build_summit_datapack.py — assemble every data-driven figure for the 2026
Beach Summit deck (reviews the 2025 season) into summit_2026_datapack.md.
"""
import sqlite3
import math
import csv
from datetime import date, timedelta, datetime
from pathlib import Path
import compute_drc as drc

HERE = Path(__file__).parent
DB = HERE / "fantasy.db"
OUT = HERE / "summit_2026_datapack.md"
SCHED_CSV = HERE / "nfl_schedule_2025.csv"
REVIEW = 2025
TARGET = drc.TARGET_SEASON
FAAB_BUDGET = 100
N_TEAMS = 12

# NFL 2025 week-start map (Week 1 Thu = Sep 4, 2025; ~7 days/week) for mapping
# a trade date to "weeks after the trade" when scoring who won a trade.
_W1 = date(2025, 9, 4)
WEEK_START = {w: (_W1 + timedelta(days=7 * (w - 1))).isoformat() for w in range(1, 18)}
def trade_week(d):
    wk = 0
    for w in range(1, 18):
        if WEEK_START[w] <= d:
            wk = w
        else:
            break
    return wk

ALIAS = {"JAC": "JAX", "JAX": "JAX", "LA": "LAR", "LAR": "LAR", "STL": "LAR",
         "OAK": "LV", "LV": "LV", "SD": "LAC", "WSH": "WAS", "WAS": "WAS", "ARZ": "ARI"}
def nt(t):
    return ALIAS.get((t or "").upper(), (t or "").upper())
SCHED = {}
if SCHED_CSV.exists():
    for r in csv.DictReader(open(SCHED_CSV, encoding="utf-8")):
        try:
            SCHED[(int(r["week"]), nt(r["team"]))] = (nt(r["opponent"]), r["home_away"])
        except (ValueError, KeyError):
            pass

def dst(name, pos):
    return (name + " DST") if (pos or "").upper() in ("DEF", "DST") else name

def rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()

def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None

def md_table(headers, data):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in data:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)

def has_rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False

def manager_display(conn):
    d = {}
    for tsid, full, nick, team in rows(conn,
        """SELECT t.team_season_id, m.full_name, m.nickname, t.team_name
           FROM teams t JOIN managers m ON m.manager_id=t.manager_id WHERE t.season=?""", (REVIEW,)):
        d[tsid] = (full or nick, team)
    return d

def fmt_date(ts):
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%b %d '%y").replace(" 0", " ")
    except ValueError:
        return ts[:10]


def recap(conn):
    L = ["## 2025 Season Recap & Awards\n"]
    disp = manager_display(conn)
    standings = rows(conn, """
        SELECT ts.rank, ts.team_season_id, ts.wins, ts.losses, ts.ties, ts.points_for, ts.points_against
        FROM team_standings ts WHERE ts.season=? ORDER BY ts.rank""", (REVIEW,))
    if standings:
        maxpf = max((r[5] or 0) for r in standings)
        last_rank = max(r[0] for r in standings)
        def badges(rank, pf):
            b = ""
            if rank == 1: b += " 🥇"
            elif rank == 2: b += " 🥈"
            elif rank == 3: b += " 🥉"
            if rank == 7: b += " 🚀"
            if rank == last_rank: b += " 💩"
            if (pf or 0) == maxpf: b += " 👑"
            return b
        table = [(r[0], disp.get(r[1], ("?", "?"))[0] + badges(r[0], r[5]), disp.get(r[1], ("?", "?"))[1],
                  f"{r[2]}-{r[3]}-{r[4]}", round(r[5], 1) if r[5] else r[5],
                  round(r[6], 1) if r[6] else r[6]) for r in standings]
        L.append("**Final standings**\n")
        L.append(md_table(["Rank", "Manager", "Team", "W-L-T", "PF", "PA"], table) + "\n")
        L.append("_🥇🥈🥉 finish · 👑 most points · 🚀 won the losers bracket · 💩 last place_\n")

    hi = rows(conn, """SELECT mu.team_season_id, mu.week, mu.points, mu.opponent_team_season_id, mu.opponent_points
        FROM matchups mu WHERE mu.season=? AND mu.points IS NOT NULL ORDER BY mu.points DESC LIMIT 5""", (REVIEW,))
    if hi:
        L.append("**Highest single-week team scores**\n")
        L.append(md_table(["Manager", "Week", "Points", "Opponent", "Opp pts"],
                 [(disp.get(r[0], ("?",))[0], r[1], round(r[2], 1), disp.get(r[3], ("?",))[0],
                   round(r[4], 1) if r[4] is not None else "—") for r in hi]) + "\n")

    blow = rows(conn, """SELECT mu.team_season_id, mu.week, mu.points, mu.opponent_team_season_id, mu.opponent_points, (mu.points-mu.opponent_points) margin
        FROM matchups mu WHERE mu.season=? AND mu.is_winner=1 AND mu.opponent_points IS NOT NULL ORDER BY margin DESC LIMIT 5""", (REVIEW,))
    if blow:
        L.append("**Biggest blowouts (top 5)**\n")
        L.append(md_table(["Winner", "Week", "Loser", "Score", "Margin"],
                 [(disp.get(r[0], ("?",))[0], r[1], disp.get(r[3], ("?",))[0],
                   f"{round(r[2],1)}-{round(r[4],1)}", round(r[5], 1)) for r in blow]) + "\n")

    perf = rows(conn, """SELECT p.player_name, p.position, p.nfl_team, pws.week, pws.fantasy_points, pws.team_season_id
        FROM player_weekly_stats pws JOIN players p ON p.player_id=pws.player_id
        WHERE pws.season=? AND pws.fantasy_points IS NOT NULL ORDER BY pws.fantasy_points DESC LIMIT 10""", (REVIEW,))
    if perf:
        out = []
        for name, pos, team, wk, pts, owner_ts in perf:
            sc = SCHED.get((wk, nt(team)))
            nfl_opp = (sc[1] + " " + sc[0]) if sc else "—"
            owner = disp.get(owner_ts, ("FA/—",))[0] if owner_ts else "FA/—"
            mu = rows(conn, "SELECT opponent_team_season_id, points, opponent_points, is_winner FROM matchups WHERE season=? AND week=? AND team_season_id=?",
                      (REVIEW, wk, owner_ts)) if owner_ts else []
            if mu:
                opp_ts, team_pts, opp_pts, won = mu[0]
                res = "W" if won == 1 else ("L" if won == 0 else "—")
                score = f"{round(team_pts,1)}-{round(opp_pts,1)}" if team_pts is not None and opp_pts is not None else "—"
                opp_mgr = disp.get(opp_ts, ("—",))[0]
            else:
                res, score, opp_mgr = "—", "—", "—"
            out.append((dst(name, pos), nfl_opp, wk, round(pts, 1), owner, res, score, opp_mgr))
        L.append("**Best individual weeks of 2025**\n")
        L.append(md_table(["Player", "NFL opp", "Wk", "Pts", "Manager", "Result", "Score (for-vs)", "vs Manager"], out) + "\n")
    return "\n".join(L)


def points_since(conn, pids, after_week):
    if not pids:
        return 0.0
    ph = ",".join("?" * len(pids))
    v = one(conn, f"SELECT COALESCE(SUM(fantasy_points),0) FROM player_weekly_stats WHERE season=? AND week>? AND player_id IN ({ph})",
            [REVIEW, after_week] + list(pids))
    return v or 0.0


def trades(conn):
    disp = manager_display(conn)
    trade_list = []
    # Real trades: one transaction = one trade (both sides recorded as incoming rows)
    for tid, ts in rows(conn, "SELECT transaction_id, timestamp FROM transactions WHERE season=? AND event_type='trade' AND status='successful'", (REVIEW,)):
        sides = {}
        for team, pid, nm, pos in rows(conn, """SELECT tp.team_season_id, tp.player_id, p.player_name, p.position
                FROM transaction_players tp JOIN players p ON p.player_id=tp.player_id
                WHERE tp.transaction_id=? AND tp.direction='incoming'""", (tid,)):
            sides.setdefault(team, {"players": [], "picks": []})["players"].append((pid, dst(nm, pos)))
        for team, rd in rows(conn, "SELECT destination_team_season_id, draft_round FROM transaction_picks WHERE transaction_id=?", (tid,)):
            sides.setdefault(team, {"players": [], "picks": []})["picks"].append(rd)
        trade_list.append({"date": ts[:10], "sides": sides})
    # Synthetic trades: stored one synth_id per player movement -> regroup by (date, team-pair)
    groups = {}
    for ts, dest, cp, pid, nm, pos in rows(conn, """SELECT st.timestamp, stp.team_season_id, stp.counterparty_team_season_id, stp.player_id, p.player_name, p.position
            FROM synthetic_transactions st JOIN synthetic_transaction_players stp ON stp.synth_id=st.synth_id
            JOIN players p ON p.player_id=stp.player_id
            WHERE st.season=? AND st.event_type='trade' AND stp.direction='incoming'""", (REVIEW,)):
        if dest is None or cp is None:
            continue
        g = groups.setdefault((ts[:10], frozenset([dest, cp])), {})
        g.setdefault(dest, {"players": [], "picks": []})["players"].append((pid, dst(nm, pos)))
        g.setdefault(cp, {"players": [], "picks": []})
    for (date, pair), sides in groups.items():
        trade_list.append({"date": date, "sides": sides})
    trade_list.sort(key=lambda t: t["date"])
    out = []
    for t in trade_list:
        teams_in = list(t["sides"].keys())
        if len(teams_in) != 2:
            continue
        A, B = teams_in
        wk = trade_week(t["date"])
        def assets(sd):
            items = [nm for _, nm in sd["players"]] + [f"R{rd} pick" for rd in sd["picks"]]
            return ", ".join(items) if items else "—"
        aP = [pid for pid, _ in t["sides"][A]["players"]]
        bP = [pid for pid, _ in t["sides"][B]["players"]]
        ra = rb = ""
        if aP and bP:
            pa, pb = points_since(conn, aP, wk), points_since(conn, bP, wk)
            if round(pa, 1) > round(pb, 1): ra, rb = "✓", "✗"
            elif round(pb, 1) > round(pa, 1): ra, rb = "✗", "✓"
        out.append((fmt_date(t["date"]), disp.get(A, ("?",))[0], ra, assets(t["sides"][A]),
                    disp.get(B, ("?",))[0], rb, assets(t["sides"][B])))
    L = ["## 2025 Trades\n"]
    L.append(f"_{len(out)} trades (in-season + offseason). ✓/✗ = more fantasy points by acquired players since the trade (player-for-player only)._\n")
    L.append(md_table(["Date", "Manager", "W", "Acquired", "Counterparty", "W", "Acquired"], out) + "\n")
    return "\n".join(L)


def keepers(conn):
    disp = manager_display(conn)
    dollar = {d: dol for d, dol in rows(conn, "SELECT drc, drc_dollars FROM drc_dollar_lookup")}
    adp = {pid: rank for pid, rank in rows(conn, "SELECT player_id, overall_rank FROM adp WHERE season=? AND player_id IS NOT NULL", (TARGET,))}
    roster = rows(conn, """SELECT fr.player_id, fr.team_season_id, p.player_name, p.position
        FROM final_rosters fr JOIN players p ON p.player_id=fr.player_id WHERE fr.season=?""", (REVIEW,))
    by_mgr = {}
    value_rows = []
    for pid, tsid, pname, pos in roster:
        res = drc.compute_drc(conn, pid, tsid)
        if not res:
            continue
        drc_int, label, chain = res
        dollars = dollar.get(drc_int, 10)
        mgr = disp.get(tsid, ("?",))[0]
        nm = dst(pname, pos)
        by_mgr.setdefault(mgr, []).append((nm, drc_int, dollars))
        if pid in adp:
            adp_round = math.ceil(adp[pid] / N_TEAMS)
            value_rows.append((mgr, nm, drc_int, dollars, adp[pid], adp_round, adp_round - drc_int))
    L = ["## Keeper Menus — 2026 cost (per manager)\n"]
    for mgr, plist in sorted(by_mgr.items(), key=lambda kv: -sum(p[2] for p in kv[1])):
        plist.sort(key=lambda p: (p[1], p[0]))
        total = sum(p[2] for p in plist)
        L.append(f"### {mgr} — total 2026 keeper cost ${total:,}\n")
        L.append(md_table(["Player", "DRC", "$"], [(p[0], p[1], f"${p[2]}") for p in plist]) + "\n")
    L.append("## Keeper Value vs 2-QB ADP\n")
    L.append("_Δ = ADP round − keeper-cost (DRC) round. Negative = steal; positive = premium._\n")
    steals = sorted([v for v in value_rows if v[6] <= -2], key=lambda v: v[6])[:12]
    over = sorted([v for v in value_rows if v[6] >= 2], key=lambda v: -v[6])[:12]
    cols = ["Manager", "Player", "DRC", "$", "ADP", "ADP rd", "Δ"]
    L.append("**Biggest steals**\n")
    L.append(md_table(cols, [(v[0], v[1], v[2], f"${v[3]}", v[4], v[5], v[6]) for v in steals]) + "\n")
    L.append("**Most overpriced**\n")
    L.append(md_table(cols, [(v[0], v[1], v[2], f"${v[3]}", v[4], v[5], v[6]) for v in over]) + "\n")
    return "\n".join(L)


def lottery(conn):
    disp = manager_display(conn)
    teams = rows(conn, """SELECT DISTINCT mu.team_season_id, ts.rank
        FROM matchups mu JOIN team_standings ts ON ts.team_season_id=mu.team_season_id AND ts.season=mu.season
        WHERE mu.season=? AND mu.is_consolation=1 ORDER BY ts.rank""", (REVIEW,))
    if not teams:
        teams = rows(conn, "SELECT team_season_id, rank FROM team_standings WHERE season=? AND rank>=7 ORDER BY rank", (REVIEW,))
    L = ["## Draft Lottery — non-playoff teams (by 2025 final standing)\n"]
    L.append(md_table(["Final rank", "Manager", "Team"],
             [(r[1], disp.get(r[0], ("?", "?"))[0], disp.get(r[0], ("?", "?"))[1]) for r in teams]) + "\n")
    return "\n".join(L)


def pricing(conn):
    return "## Keeper Pricing Schedule (static)\n\n" + md_table(["Draft round", "Keeper $"],
        rows(conn, "SELECT drc, drc_dollars FROM drc_dollar_lookup ORDER BY drc")) + "\n"


def trivia(conn):
    L = ["## League Trivia (2025 season)\n"]
    L.append("**Q1 — Most-transacted players in 2025** (waiver/FA/draft/trade moves)\n")
    movers = rows(conn, """SELECT tp.player_id, p.player_name, p.position, COUNT(DISTINCT tp.transaction_id) n
        FROM transaction_players tp JOIN transactions t ON t.transaction_id=tp.transaction_id
        JOIN players p ON p.player_id=tp.player_id
        WHERE t.season=? AND t.status='successful'
        GROUP BY tp.player_id ORDER BY n DESC, p.player_name LIMIT 15""", (REVIEW,))
    q1 = []
    for pid, pname, pos, n in movers:
        most_by = one(conn, """SELECT m.full_name FROM transaction_players tp
            JOIN transactions t ON t.transaction_id=tp.transaction_id
            JOIN teams te ON te.team_season_id=tp.team_season_id JOIN managers m ON m.manager_id=te.manager_id
            WHERE t.season=? AND t.status='successful' AND tp.player_id=?
            GROUP BY m.manager_id ORDER BY COUNT(DISTINCT tp.transaction_id) DESC LIMIT 1""", (REVIEW, pid)) or "—"
        final_owner = one(conn, """SELECT m.full_name FROM final_rosters fr
            JOIN teams te ON te.team_season_id=fr.team_season_id JOIN managers m ON m.manager_id=te.manager_id
            WHERE fr.season=? AND fr.player_id=?""", (REVIEW, pid)) or "Free Agent"
        try:
            faab = one(conn, """SELECT COALESCE(SUM(t.faab_bid),0) FROM transactions t
                JOIN transaction_players tp ON tp.transaction_id=t.transaction_id AND tp.direction='incoming'
                WHERE t.season=? AND tp.player_id=? AND t.faab_bid IS NOT NULL""", (REVIEW, pid)) or 0
        except sqlite3.OperationalError:
            faab = 0
        q1.append((dst(pname, pos), n, most_by, final_owner, f"${faab}"))
    L.append(md_table(["Player", "Moves", "Most by", "Finished on", "Total FAAB"], q1) + "\n")

    use_official = has_rows(conn, "SELECT COUNT(*) FROM team_season_stats WHERE season=? AND number_of_moves IS NOT NULL", (REVIEW,))
    if use_official:
        move_sql = """SELECT m.full_name, s.number_of_moves, s.number_of_trades
            FROM team_season_stats s JOIN teams te ON te.team_season_id=s.team_season_id
            JOIN managers m ON m.manager_id=te.manager_id WHERE s.season=? ORDER BY s.number_of_moves {dir}, m.full_name"""
        note = " _(Yahoo's official add/drop count)_"
    else:
        move_sql = """SELECT m.full_name, COUNT(DISTINCT tp.transaction_id) n, NULL
            FROM transaction_players tp JOIN transactions t ON t.transaction_id=tp.transaction_id
            JOIN teams te ON te.team_season_id=tp.team_season_id JOIN managers m ON m.manager_id=te.manager_id
            WHERE t.season=? AND t.status='successful' GROUP BY m.manager_id ORDER BY n {dir}"""
        note = " _(transaction-based)_"
    L.append("**Q2 — Most moves by a manager in 2025**" + note + "\n")
    L.append(md_table(["Manager", "Moves", "Trades"], rows(conn, move_sql.format(dir="DESC") + " LIMIT 6", (REVIEW,))) + "\n")
    L.append("**Q3 — Fewest moves by a manager in 2025**\n")
    L.append(md_table(["Manager", "Moves", "Trades"], rows(conn, move_sql.format(dir="ASC") + " LIMIT 6", (REVIEW,))) + "\n")

    L.append("**Q4 — Highest single FAAB bids in 2025**\n")
    try:
        L.append(md_table(["Manager", "Player", "FAAB $", "Date"], rows(conn, """
            SELECT m.full_name, p.player_name, t.faab_bid, date(t.timestamp)
            FROM transactions t JOIN transaction_players tp ON tp.transaction_id=t.transaction_id AND tp.direction='incoming'
            JOIN players p ON p.player_id=tp.player_id JOIN teams te ON te.team_season_id=tp.team_season_id
            JOIN managers m ON m.manager_id=te.manager_id
            WHERE t.season=? AND t.faab_bid IS NOT NULL ORDER BY t.faab_bid DESC, date(t.timestamp) LIMIT 5""", (REVIEW,))) + "\n")
    except sqlite3.OperationalError as e:
        L.append(f"_(FAAB unavailable: {e})_\n")

    L.append("**Q5 — Managers who did NOT use all $%d FAAB in 2025**\n" % FAAB_BUDGET)
    use_bal = has_rows(conn, "SELECT COUNT(*) FROM team_season_stats WHERE season=? AND faab_balance IS NOT NULL", (REVIEW,))
    try:
        if use_bal:
            L.append("_(spent = $%d budget − Yahoo's remaining FAAB balance)_\n" % FAAB_BUDGET)
            data = rows(conn, """SELECT m.full_name, (? - s.faab_balance) spent
                FROM team_season_stats s JOIN teams te ON te.team_season_id=s.team_season_id
                JOIN managers m ON m.manager_id=te.manager_id WHERE s.season=? AND s.faab_balance > 0 ORDER BY spent""", (FAAB_BUDGET, REVIEW))
        else:
            data = rows(conn, """SELECT m.full_name, COALESCE(SUM(t.faab_bid),0) spent
                FROM teams te JOIN managers m ON m.manager_id=te.manager_id
                LEFT JOIN transaction_players tp ON tp.team_season_id=te.team_season_id AND tp.direction='incoming'
                LEFT JOIN transactions t ON t.transaction_id=tp.transaction_id AND t.season=? AND t.faab_bid IS NOT NULL AND t.status='successful'
                WHERE te.season=? GROUP BY m.manager_id HAVING spent < ? ORDER BY spent""", (REVIEW, REVIEW, FAAB_BUDGET))
        L.append(md_table(["Manager", "FAAB spent"], data) + "\n")
    except sqlite3.OperationalError as e:
        L.append(f"_(FAAB unavailable: {e})_\n")

    # Q6 — keeper efficiency (2025 points per $ of 2026 keeper cost)
    disp6 = manager_display(conn)
    dollar = {d: dol for d, dol in rows(conn, "SELECT drc, drc_dollars FROM drc_dollar_lookup")}
    eff = []
    for pid, tsid, pname, pos in rows(conn, "SELECT fr.player_id, fr.team_season_id, p.player_name, p.position FROM final_rosters fr JOIN players p ON p.player_id=fr.player_id WHERE fr.season=?", (REVIEW,)):
        res = drc.compute_drc(conn, pid, tsid)
        if not res:
            continue
        dol = dollar.get(res[0], 10)
        pts = one(conn, "SELECT COALESCE(SUM(fantasy_points),0) FROM player_weekly_stats WHERE season=? AND player_id=?", (REVIEW, pid)) or 0
        eff.append((dst(pname, pos), disp6.get(tsid, ("?",))[0], res[0], dol, round(pts, 1), round(pts / dol, 2)))
    top = sorted(eff, key=lambda r: -r[5])[:3]
    premium = [r for r in eff if r[3] >= 50]
    bot = sorted(premium, key=lambda r: r[5])[:3]
    cols6 = ["Player", "Manager", "DRC", "$", "2025 Pts", "Pts/$"]
    L.append("**Q6 — Most efficient keepers** (2025 fantasy points per $ of 2026 keeper cost)\n")
    L.append(md_table(cols6, top) + "\n")
    L.append("**Q6 — Least efficient keepers** (priciest underperformers, $50+ keepers)\n")
    L.append(md_table(cols6, bot) + "\n")
    return "\n".join(L)


def main():
    conn = sqlite3.connect(DB)
    body = "\n".join([
        "# 2026 Beach Summit — Data Pack\n",
        f"_Reviews the {REVIEW} season. Keeper costs are for {TARGET}. Auto-generated; sanity-check before slides._\n",
        recap(conn), trivia(conn), trades(conn), keepers(conn), lottery(conn), pricing(conn),
    ])
    OUT.write_text(body, encoding="utf-8")
    print("VERIFICATION")
    print("trades (real+synthetic):", one(conn, "SELECT COUNT(*) FROM all_transactions WHERE season=2025 AND event_type='trade'"))
    print("Wrote", OUT.name)
    conn.close()


if __name__ == "__main__":
    main()
