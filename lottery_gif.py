"""lottery_gif.py — render the 2026 draft lottery reveal as an animated GIF.

Reads lottery_result.json (written by lottery_draw.py) and produces
comms/img/2026-draft-lottery.gif in league brand style (Advent tokens from
Design Brief/Style Guide.md; Liberation Sans stands in for Inter/Arial).

Usage:  python lottery_gif.py           # real render from lottery_result.json
        python lottery_gif.py --test    # placeholder data + TEST watermark
"""
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT = HERE / "comms" / "img" / "2026-draft-lottery.gif"
RESULT = HERE / "lottery_result.json"

W, H = 720, 900
NAVY = (2, 36, 121)        # Blue 800 #022479
BLUE = (0, 56, 255)        # Blue 600 #0038FF
BLUE4 = (38, 154, 255)     # Blue 400 #269AFF
BLUE2 = (119, 206, 255)    # Blue 200 #77CEFF
GOLD = (225, 181, 35)      # Gold 400 #E1B523
WHITE = (255, 255, 255)
GRAY = (96, 108, 113)      # #606C71 secondary text
TRACK = (229, 229, 221)    # #E5E5DD gray fill
INK = (0, 0, 0)

FB = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FR = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


def F(size, bold=True):
    return ImageFont.truetype(FB if bold else FR, size)


def clean(s):
    """Strip emoji/symbols Liberation Sans can't draw; keep quotes/degrees."""
    return re.sub(r"[\U0001F000-\U0001FAFF☀-➿️\U0001F3FB-\U0001F3FF]",
                  "", s).strip()


def ellipsize(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return text + "…"


def base(title_eyebrow=None):
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    # thin brand gradient bar across the top (600 -> 400, left to right)
    for x in range(W):
        t = x / W
        c = tuple(int(BLUE[i] + (BLUE4[i] - BLUE[i]) * t) for i in range(3))
        d.line([(x, 0), (x, 6)], fill=c)
    d.text((48, 40), "I Yearn For Your Sweet TD’s", font=F(17, False), fill=BLUE2)
    if title_eyebrow:
        d.text((48, 66), title_eyebrow, font=F(15), fill=(255, 255, 255, 200))
    return img, d


def title_card():
    img, d = base()
    d.text((48, 330), "2026 draft lottery", font=F(58), fill=WHITE)
    d.rectangle([48, 412, 148, 417], fill=GOLD)
    d.text((48, 442), "Six teams. Flipped odds. One 1.01.", font=F(24, False), fill=BLUE2)
    d.text((48, H - 70), "New for 2026: the team that just missed the playoffs\ngets the best shot. No prize for tanking.",
           font=F(16, False), fill=(200, 214, 240))
    return img


def odds_card(lottery_sorted_by_rank):
    img, d = base()
    d.text((48, 110), "The odds", font=F(40), fill=WHITE)
    d.text((48, 166), "Chance at the first overall pick", font=F(17, False), fill=BLUE2)
    y = 230
    maxw = 240
    bar_x = 356
    for p in lottery_sorted_by_rank:
        nm = clean(p["manager"])
        d.text((48, y), f"{p['final_rank']}th", font=F(16), fill=BLUE2)
        d.text((100, y - 4), ellipsize(d, nm, F(24), 240), font=F(24), fill=WHITE)
        d.rounded_rectangle([bar_x, y, bar_x + maxw, y + 18], 9, fill=(11, 48, 138))
        w = int(maxw * p["weight"] / 50.0)
        d.rounded_rectangle([bar_x, y, bar_x + max(w, 18), y + 18], 9, fill=BLUE4)
        d.text((bar_x + maxw + 14, y - 2), f"{p['weight']:g}%", font=F(18), fill=WHITE)
        y += 92
    return img


def dots_frame(pick, n_on, revealed):
    img, d = base()
    label = "THE FIRST OVERALL PICK" if pick == 1 else f"PICK {pick}"
    d.text((48, 140), label, font=F(22), fill=GOLD if pick == 1 else BLUE2)
    for i in range(3):
        cx = W // 2 - 76 + i * 76
        on = (i == n_on % 3)
        r = 17 if on else 11
        d.ellipse([cx - r, 340 - r, cx + r, 340 + r],
                  fill=(GOLD if pick == 1 else WHITE) if on else (60, 90, 170))
    draw_revealed_stack(d, revealed)
    return img


def reveal_card(p, slide=0, revealed=(), final_pick=False):
    """slide: 0 = rest position; positive = card offset down (px)."""
    img, d = base()
    label = "THE FIRST OVERALL PICK" if final_pick else f"PICK {p['pick']}"
    d.text((48, 140), label, font=F(22), fill=GOLD if final_pick else BLUE2)
    cy = 210 + slide
    accent = GOLD if final_pick else BLUE
    d.rounded_rectangle([48, cy, W - 48, cy + 190], 14, fill=WHITE)
    d.rectangle([50, cy + 4, 56, cy + 186], fill=accent)
    d.rounded_rectangle([48, cy, W - 48, cy + 190], 14, outline=accent, width=2)
    nm = clean(p["manager"])
    d.text((84, cy + 34), ellipsize(d, nm, F(44), W - 160), font=F(44), fill=INK)
    d.text((84, cy + 98), ellipsize(d, clean(p["team"]), F(23, False), W - 160),
           font=F(23, False), fill=GRAY)
    chip = f"{p['weight']:g}% odds  ·  finished {p['final_rank']}th"
    cw = d.textlength(chip, font=F(16)) + 28
    d.rounded_rectangle([84, cy + 140, 84 + cw, cy + 168], 14,
                        fill=(250, 244, 214) if final_pick else (233, 240, 255))
    d.text((98, cy + 145), chip, font=F(16),
           fill=(103, 79, 0) if final_pick else NAVY)
    draw_revealed_stack(d, revealed)
    return img


def draw_revealed_stack(d, revealed):
    """Compact list of already-revealed picks, bottom of frame, high to low."""
    if not revealed:
        return
    y = H - 64 - 34 * len(revealed)
    d.text((48, y - 30), "SO FAR", font=F(13), fill=(140, 160, 205))
    for p in sorted(revealed, key=lambda x: -x["pick"]):
        d.text((48, y), f"{p['pick']}", font=F(17), fill=BLUE2)
        d.text((84, y), ellipsize(d, clean(p["manager"]), F(17), 300),
               font=F(17), fill=WHITE)
        d.text((400, y), f"{p['weight']:g}%", font=F(15, False), fill=(150, 168, 210))
        y += 34


def final_board(lottery, playoff):
    img, d = base()
    d.text((48, 96), "2026 draft order: round 1", font=F(34), fill=WHITE)
    d.rectangle([48, 148, 128, 152], fill=GOLD)

    def row(x, y, pick, name, team, gold=False):
        d.text((x, y), f"{pick:>2}", font=F(20), fill=GOLD if gold else BLUE2)
        d.text((x + 40, y), ellipsize(d, clean(name), F(20), 210), font=F(20), fill=WHITE)
        d.text((x + 40, y + 24), ellipsize(d, clean(team), F(14, False), 230),
               font=F(14, False), fill=(150, 168, 210))

    d.text((48, 182), "LOTTERY", font=F(14), fill=BLUE2)
    y = 214
    for p in sorted(lottery, key=lambda x: x["pick"]):
        if p["pick"] == 1:
            d.rounded_rectangle([36, y - 8, 350, y + 48], 10, outline=GOLD, width=2)
        row(48, y, p["pick"], p["manager"], p["team"], gold=(p["pick"] == 1))
        y += 64
    d.text((388, 182), "PLAYOFF TEAMS (REVERSE FINISH)", font=F(14), fill=BLUE2)
    y = 214
    for p in sorted(playoff, key=lambda x: x["pick"]):
        row(388, y, p["pick"], p["manager"], p["team"])
        y += 64
    d.text((48, H - 56), "Full draw log on the dashboard · Commissioner’s Desk",
           font=F(15, False), fill=(150, 168, 210))
    return img


def render(result, out_path=OUT, watermark=None):
    lottery = result["lottery"]
    playoff = result["playoff"]
    by_rank = sorted(lottery, key=lambda p: p["final_rank"])

    frames, durs = [], []

    def add(img, ms):
        if watermark:
            d = ImageDraw.Draw(img)
            d.text((W - 190, 14), watermark, font=F(22), fill=(255, 90, 90))
        frames.append(img)
        durs.append(ms)

    add(title_card(), 2400)
    add(odds_card(by_rank), 4000)

    revealed = []
    for pick in range(6, 0, -1):
        p = next(x for x in lottery if x["pick"] == pick)
        is_one = pick == 1
        cycles = 7 if is_one else 3
        for i in range(cycles):
            add(dots_frame(pick, i, revealed), 400 if is_one else 360)
        for s in (90, 40, 12):
            add(reveal_card(p, slide=s, revealed=revealed, final_pick=is_one), 60)
        add(reveal_card(p, slide=0, revealed=revealed, final_pick=is_one),
            3400 if is_one else 2100)
        revealed.append(p)

    add(final_board(lottery, playoff), 6000)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    q = [f.quantize(colors=128, dither=Image.Dither.NONE) for f in frames]
    q[0].save(out_path, save_all=True, append_images=q[1:], duration=durs,
              loop=0, optimize=True)
    kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path}  ({len(frames)} frames, {kb:,.0f} KB)")


TEST = {
    "lottery": [
        {"pick": 1, "final_rank": 9, "manager": "George Mensing", "team": "Dreorge Bledsoe", "weight": 12.5},
        {"pick": 2, "final_rank": 7, "manager": "Dan Vescuso", "team": "Fear the Peel", "weight": 50.0},
        {"pick": 3, "final_rank": 12, "manager": "Pete Hodor", "team": "Are Bonita Fish Big?", "weight": 5.0},
        {"pick": 4, "final_rank": 8, "manager": "New Manager", "team": "The Lady Boys", "weight": 15.0},
        {"pick": 5, "final_rank": 10, "manager": "Scott Montgomery", "team": "JUST THE TUA US", "weight": 10.0},
        {"pick": 6, "final_rank": 11, "manager": "Aric Tao", "team": "High Priest Gabagool 🤌🏻", "weight": 7.5},
    ],
    "playoff": [
        {"pick": 7, "final_rank": 6, "manager": "Dan MacNulty", "team": "TheDarkKnight06"},
        {"pick": 8, "final_rank": 5, "manager": "Alex Schlosberg", "team": "Vesco’s banging bathroom"},
        {"pick": 9, "final_rank": 4, "manager": "Tom Watson", "team": "The Prince of Darkness"},
        {"pick": 10, "final_rank": 3, "manager": "Greg Pearson", "team": "Mr. McGibblets"},
        {"pick": 11, "final_rank": 2, "manager": "Paul Lewis", "team": "Briguy’s Strange Boutte"},
        {"pick": 12, "final_rank": 1, "manager": "Brian Malconian", "team": "42°19'50.3\"N 71°01'52.0\"W"},
    ],
}

if __name__ == "__main__":
    if "--test" in sys.argv:
        render(TEST, HERE / "lottery_TEST.gif", watermark="TEST DATA")
    else:
        render(json.loads(RESULT.read_text()))
