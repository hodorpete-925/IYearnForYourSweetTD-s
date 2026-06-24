"""
mine_chat.py — mine the league group-chat export for summit material.

Produces a categorized candidates report (markdown) covering:
  1. Chat by the numbers (volume per person / season)
  2. Most-reacted messages (Laughed at / Loved) -> funniest candidates
  3. Bold takes & predictions
  4. Rules & governance discussion (for reconciling rule changes)
  5. Contentious moments (vetoes, collusion gripes, arguments)
  6. Trade talk highlights

These are CANDIDATES to curate, not finished slides. Read-only on the CSV.

Usage: python mine_chat.py <chat.csv> <out.md>
"""
import csv, collections, re, sys

CSV = sys.argv[1] if len(sys.argv) > 1 else "fantasy_football_full_history.csv"
OUT = sys.argv[2] if len(sys.argv) > 2 else "chat_intelligence_report.md"

rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
for r in rows:
    r["message"] = (r.get("message") or "")
    r["sender"] = r.get("sender") or ""
    r["date"] = r.get("date") or ""

TAPS = ("Laughed at", "Loved", "Liked", "Emphasized", "Disliked", "Questioned")
def tap_kind(m):
    for k in TAPS:
        if m.strip().startswith(k):
            return k
    return None

def one_line(s, n=160):
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:n] + "…") if len(s) > n else s

def disp(sender):
    return "Pete (Me)" if sender == "Me" else sender

# Split tapbacks from real messages
reactions, real = [], []
for r in rows:
    m = r["message"].strip()
    k = tap_kind(m)
    if k:
        q = m[len(k):].strip().lstrip("“\"'").rstrip("”\"'")
        q = q.rstrip("…").strip().rstrip("”\"'").strip()
        reactions.append((k, q))
    elif m and m != "[attachment]":
        real.append((r["date"], r["sender"], r["message"]))

def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()

real_norm = [(d, s, m, norm(m)) for (d, s, m) in real]

# Match each tapback's quoted prefix back to its original message
rc = collections.Counter()
rkinds = collections.defaultdict(collections.Counter)
for k, q in reactions:
    if not q or q.lower() in ("an image", "a movie", "an attachment") or len(q) < 6:
        continue
    pref = norm(q)[:38]
    for i, (d, s, m, mn) in enumerate(real_norm):
        if mn.startswith(pref):
            rc[i] += 1
            rkinds[i][k] += 1
            break

L = []
def H(t): L.append("\n## " + t + "\n")
def line(s): L.append(s)

L.append("# League chat — summit mining report\n")
L.append(f"_Source: {CSV}. {len(rows):,} rows → {len(real):,} real messages + "
         f"{len(reactions):,} reactions. Candidates to curate, not final copy._\n")

# 1. By the numbers
H("1. Chat by the numbers")
per = collections.Counter(s for (_, s, _) in real)
line("**Most talkative (real messages):**\n")
line("| Manager | Messages |\n| --- | --- |")
for s, c in per.most_common(13):
    line(f"| {disp(s)} | {c} |")
line("")
per_react_recv = collections.Counter()
for i, c in rc.items():
    per_react_recv[real[i][1]] += c
line("**Most reactions received (laughs/loves/likes on their messages):**\n")
line("| Manager | Reactions |\n| --- | --- |")
for s, c in per_react_recv.most_common(8):
    line(f"| {disp(s)} | {c} |")

# 2. Most-reacted messages
H("2. Most-reacted messages (funniest / most-loved candidates)")
line("_Ranked by total tapbacks. L=Laughed, ♥=Loved, +=Liked._\n")
for i, c in rc.most_common(30):
    d, s, m = real[i]
    kk = rkinds[i]
    tag = f"{kk.get('Laughed at',0)}L {kk.get('Loved',0)}♥ {kk.get('Liked',0)}+"
    line(f"- **[{c}] ({tag})** [{d[:10]}] {disp(s)}: {one_line(m, 180)}")

# helper for keyword categories
def category(title, patterns, note="", limit=30, require_len=12):
    H(title)
    if note:
        line("_" + note + "_\n")
    rx = re.compile("|".join(patterns), re.I)
    hits = []
    for (d, s, m) in real:
        if len(m.strip()) >= require_len and rx.search(m):
            hits.append((d, s, m))
    for (d, s, m) in hits[:limit]:
        line(f"- [{d[:10]}] {disp(s)}: {one_line(m)}")
    if len(hits) > limit:
        line(f"\n_…and {len(hits)-limit} more matches in this category._")

# 3. Bold takes & predictions
category("3. Bold takes & predictions",
    [r"\bguarantee", r"\block(s|ing|ed)?\s+(it|him|in)\b", r"\bcalling it\b",
     r"\bbook it\b", r"\bmark my\b", r"\bbust\b", r"\bsleeper\b", r"\bwill win\b",
     r"\bno way\b", r"\beasily\b", r"\bleague winner\b", r"\bsmash\b", r"\bregret\b",
     r"\bi'?m telling you\b", r"\btrust me\b", r"\bbold\b", r"\bprediction"],
    note="Predictions and hot takes — Pete to pick the ones that aged hilariously.", limit=35)

# 4. Rules & governance
category("4. Rules & governance (rule-change reconciliation)",
    [r"\brule\b", r"\bvote\b", r"\bpropos", r"\bamend", r"\bconstitution\b",
     r"\bcommish", r"\bdues\b", r"\bpayout", r"\bdraft order\b", r"\bplayoff",
     r"\bkeeper\b.*\brule", r"\bbylaw"],
    note="Rule discussions/votes — use to reconcile what was actually adopted vs proposed.", limit=45)

# 5. Contentious moments
category("5. Contentious moments (vetoes, collusion, arguments)",
    [r"\bveto", r"\bcollusion\b", r"\bcollud", r"\bunfair\b", r"\brigged\b",
     r"\bprotest\b", r"\bbullshit\b", r"\bbs\b", r"\bcheat", r"\bscam\b",
     r"\bare you kidding\b", r"\bridiculous\b", r"\bcorrupt"],
    note="Heated exchanges and disputes — context for the season's drama.", limit=35)

# 6. Trade talk
category("6. Trade talk (incl. contentious trades)",
    [r"\btrade\b", r"\boffer\b", r"\bveto.*trade|trade.*veto", r"\bdecline"],
    note="High volume — skim for the trades that caused the most back-and-forth.", limit=30)

open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
print("WROTE", OUT, f"({len(L)} lines)")
print("real messages:", len(real), "| matched-reaction messages:", len(rc))
print("top reacted:", rc.most_common(1))
