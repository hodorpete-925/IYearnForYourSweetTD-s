"""build_lottery_post.py — write the Commissioner's Desk post for the 2026
draft lottery from lottery_result.json (run lottery_draw.py first, then
lottery_gif.py). Facts only; Pete edits voice before publishing."""
import json
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
RESULT = json.loads((HERE / "lottery_result.json").read_text())
OUT = HERE / "comms" / "2026-draft-lottery.md"

lot = sorted(RESULT["lottery"], key=lambda p: p["pick"])
playoff = sorted(RESULT["playoff"], key=lambda p: p["pick"])
winner = lot[0]

lines = [
    "---",
    "slug: 2026-draft-lottery",
    "title: 2026 draft lottery results",
    f"date: {date.today().isoformat()}",
    f"summary: The first flipped-odds lottery is in the books. "
    f"{winner['manager']} lands the 1.01 at {winner['weight']:g}% odds.",
    "---",
    "The first draw under the amended weights is done. Six teams, "
    "one ball each round, winner removed, same weights renormalized. "
    "Watch the reveal:",
    "",
    "![2026 draft lottery reveal](img/2026-draft-lottery.gif)",
    "",
    "*Lottery results (picks 1-6):*",
    "",
]
for p in lot:
    lines.append(f"- **Pick {p['pick']}: {p['manager']}** ({p['team']}), "
                 f"{p['weight']:g}% base odds, finished {p['final_rank']}th in 2025")
lines += ["", "*Playoff teams (picks 7-12, reverse finish):*", ""]
for p in playoff:
    lines.append(f"- **Pick {p['pick']}: {p['manager']}** ({p['team']})")
lines += [
    "",
    f"*How the draw ran:* one pass, {RESULT['drawn_at_utc']} UTC, using a "
    "cryptographic random source (no seed, no re-rolls). Every round's "
    "weights, cutoff bands, and drawn value are in the published draw log, "
    "so anyone can re-walk the math. Ask Pete for lottery_draw_log.json if "
    "you want to audit it.",
]
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote {OUT}")
