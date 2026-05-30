"""audit_keeper_misclassifications.py - find players currently being treated
as keepers by the DRC engine that Yahoo says were actually fresh drafts.

Misclassification pattern:
  - draft_picks row in year N with is_keeper_yahoo = 0 (Yahoo: fresh draft)
  - SAME player + SAME manager appears in year N-1's draft_picks
  - Current engine infers "kept" from "same manager appears last year" and ignores
    is_keeper_yahoo, so it decrements the player's DRC. The proposed fix is to
    trust is_keeper_yahoo, in which case the year N row should anchor at that
    year's draft round (no decrement).

Run from the project root:

    python audit_keeper_misclassifications.py
"""

import sqlite3

DB = "fantasy.db"
YEARS_TO_CHECK = (2024, 2025)  # 2023 is earliest; can't have prior-year overlap

DRC_DOLLARS = {1: 200, 2: 100, 3: 80, 4: 60, 5: 50,
               6: 30, 7: 30, 8: 30, 9: 30,
               10: 10, 11: 10, 12: 10, 13: 10, 14: 10, 15: 10, 16: 10}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    findings = []

    for year in YEARS_TO_CHECK:
        prev_year = year - 1
        # All year-N draft_picks rows Yahoo marked as fresh drafts (is_keeper=0)
        rows = list(conn.execute("""
            SELECT dp.player_id, dp.draft_round, dp.overall_pick,
                   p.player_name, p.position,
                   m.full_name AS manager_name,
                   t.manager_id
            FROM draft_picks dp
            JOIN players p ON p.player_id = dp.player_id
            JOIN teams t ON t.team_season_id = dp.team_season_id
            JOIN managers m ON m.manager_id = t.manager_id
            WHERE dp.season = ? AND dp.is_keeper = 0
        """, (year,)))

        for row in rows:
            prior = conn.execute("""
                SELECT dp.draft_round
                FROM draft_picks dp
                JOIN teams t ON t.team_season_id = dp.team_season_id
                WHERE dp.player_id = ? AND dp.season = ? AND t.manager_id = ?
            """, (row["player_id"], prev_year, row["manager_id"])).fetchone()

            if prior is None:
                continue

            # Misclassification candidate. Estimate engine-current DRC as a
            # simple one-tier decrement of the prior year's round (the
            # actual engine may walk further back, but for a 2-year window
            # this is the right first-order estimate).
            engine_drc_est = max(1, prior["draft_round"] - 1)
            proposed_drc = row["draft_round"]
            engine_cost = DRC_DOLLARS.get(engine_drc_est, 0)
            proposed_cost = DRC_DOLLARS.get(proposed_drc, 0)
            dollar_delta = engine_cost - proposed_cost  # positive = manager refunded

            findings.append({
                "year": year,
                "manager": row["manager_name"],
                "player": row["player_name"],
                "position": row["position"] or "—",
                "prior_round": prior["draft_round"],
                "this_round": row["draft_round"],
                "engine_drc": engine_drc_est,
                "proposed_drc": proposed_drc,
                "engine_cost": engine_cost,
                "proposed_cost": proposed_cost,
                "delta": dollar_delta,
            })

    # ---- Detail table ----
    findings.sort(key=lambda x: (x["year"], x["manager"], x["player"]))
    print(f"Found {len(findings)} potential misclassifications "
          f"(player drafted by same manager 2 years in a row, "
          f"Yahoo says not kept):\n")
    print(f"{'Year':<6}{'Manager':<22}{'Player':<26}{'Pos':<5}"
          f"{'PriorR':<8}{'ThisR':<8}"
          f"{'EngDRC$':<10}{'NewDRC$':<10}{'Refund':<8}")
    print("-" * 103)
    for f in findings:
        print(f"{f['year']:<6}{f['manager'][:21]:<22}{f['player'][:25]:<26}"
              f"{f['position']:<5}R{f['prior_round']:<7}R{f['this_round']:<7}"
              f"${f['engine_cost']:<9}${f['proposed_cost']:<9}"
              f"${f['delta']:<7}")

    # ---- Summary by manager ----
    by_mgr = {}
    for f in findings:
        by_mgr.setdefault(f["manager"], {"count": 0, "total_delta": 0})
        by_mgr[f["manager"]]["count"] += 1
        by_mgr[f["manager"]]["total_delta"] += f["delta"]
    print()
    print("Dollar impact by manager (positive = currently overcharged):")
    print(f"{'Manager':<25}{'Count':<8}{'Total $ delta':<15}")
    print("-" * 48)
    for mgr, stats in sorted(by_mgr.items(), key=lambda x: -x[1]["total_delta"]):
        print(f"{mgr[:24]:<25}{stats['count']:<8}${stats['total_delta']:<14}")

    # ---- Summary by year ----
    by_year = {}
    for f in findings:
        by_year.setdefault(f["year"], 0)
        by_year[f["year"]] += 1
    print()
    print("By year:")
    for y, n in sorted(by_year.items()):
        print(f"  {y}: {n}")


if __name__ == "__main__":
    main()
