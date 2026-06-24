/*
 * build_deck_final.js — 2026 Beach Summit deck generator (consolidated).
 * Standings use drawn ICON images (medals/crown/rocket/poop); keeper menus load
 * from keeper_data.json; trades show green ✓/red ✗; Jon -> "Manager TBD";
 * "Pete Hodor"; emoji stripped from team names; Q6 efficiency (when in data pack).
 * Usage: node build_deck_final.js [datapack.md] [out.pptx] [assetsDir] [keeperJson]
 */
const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");

const MD = process.argv[2] || path.join(__dirname, "summit_2026_datapack.md");
const OUT = process.argv[3] || path.join(__dirname, "IYFYSTDs_Beach_Summit_2026.pptx");
const ASSETS = process.argv[4] || __dirname;
const KEEPER_JSON = process.argv[5] || path.join(path.dirname(MD), "keeper_data.json");
const TITLE_BG = path.join(ASSETS, "title_bg.png");
const DIV_BG = path.join(ASSETS, "section_bg.png");
const ICON = (n) => path.join(ASSETS, "icons", n + ".png");

const NAVY = "022479", BLUE = "0038FF", BLUE2 = "77CEFF";
const GOLD = "E1B523", GOLDTINT = "FBF0CF", INK = "1A1A1A", GRAY = "5B6573";
const GREEN = "1F9D55", RED = "C0392B";
const BAND = "F1F5FC", WHITE = "FFFFFF";
const FONT = "Calibri";

function fixName(s) { return String(s).replace(/Jon Lewitus/g, "Manager TBD").replace(/Pete \(Me\)/g, "Pete Hodor"); }
function stripBadges(s) { return String(s).replace(/\s*[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{FE0F}\u{1F3FB}-\u{1F3FF}].*$/u, "").trim(); }
function stripEmoji(s) { return String(s).replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE0F}\u{1F3FB}-\u{1F3FF}\u{1F1E6}-\u{1F1FF}]/gu, "").replace(/\s+/g, " ").trim(); }

const lines = fs.readFileSync(MD, "utf8").split(/\r?\n/);
function parseTableAt(i) {
  const cells = (s) => s.split("|").slice(1, -1).map((c) => c.trim());
  const headers = cells(lines[i]); const rows = [];
  let j = i + 2;
  while (j < lines.length && lines[j].trim().startsWith("|")) { rows.push(cells(lines[j])); j++; }
  return { headers, rows };
}
function tableAfter(label, from = 0) {
  for (let i = from; i < lines.length; i++) {
    if (lines[i].includes(label)) {
      for (let k = i + 1; k < lines.length; k++) {
        if (lines[k].trim().startsWith("|")) return parseTableAt(k);
        if (lines[k].startsWith("#")) break;
      }
    }
  }
  return { headers: [], rows: [] };
}
const standings = tableAfter("**Final standings**").rows;
const weeklyHigh = tableAfter("Highest single-week team scores").rows;
const blowouts = tableAfter("Biggest blowouts").rows;
const bestWeeks = tableAfter("Best individual weeks").rows;
const tradesRows = tableAfter("## 2025 Trades").rows;
const q1 = tableAfter("Q1 —").rows;
const q2 = tableAfter("Q2 —").rows;
const q3 = tableAfter("Q3 —").rows;
const q4 = tableAfter("Q4 —").rows;
const q5 = tableAfter("Q5 —").rows;
const q6top = tableAfter("Q6 — Most efficient").rows;
const q6bot = tableAfter("Q6 — Least efficient").rows;

const DRC_DOLLARS = { 1: 200, 2: 100, 3: 80, 4: 60, 5: 50, 6: 30, 7: 30, 8: 30, 9: 30, 10: 10, 11: 10, 12: 10, 13: 10, 14: 10, 15: 10, 16: 10 };
const KEEPER_DATA = JSON.parse(fs.readFileSync(KEEPER_JSON, "utf8"));
const keepers = Object.keys(KEEPER_DATA).map((mgr) => {
  const ps = KEEPER_DATA[mgr].slice().sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]));
  const kr = ps.map(([nm, d]) => [nm, String(d), "$" + (DRC_DOLLARS[d] || 10)]);
  const total = ps.reduce((s, p) => s + (DRC_DOLLARS[p[1]] || 10), 0);
  return { name: fixName(mgr), total: total.toLocaleString(), rows: kr };
}).sort((a, b) => parseInt(b.total.replace(/,/g, "")) - parseInt(a.total.replace(/,/g, "")));

const steals = [["Tom Watson", "Puka Nacua", "14", "$10", "10", "1", "-13"], ["George Mensing", "Tyler Shough", "16", "$10", "59", "5", "-11"], ["Scott Montgomery", "Chase Brown", "13", "$10", "29", "3", "-10"], ["Brian Malconian", "Trey McBride", "14", "$10", "41", "4", "-10"], ["Brian Malconian", "Malik Willis", "15", "$10", "56", "5", "-10"], ["Manager TBD", "Rashee Rice", "14", "$10", "42", "4", "-10"]];
const overpriced = [["Aric Tao", "Deebo Samuel", "3", "$80", "191", "16", "13"], ["Tom Watson", "Travis Hunter", "4", "$60", "189", "16", "12"], ["George Mensing", "James Conner", "1", "$200", "130", "11", "10"], ["Aric Tao", "Brandon Aubrey", "7", "$30", "194", "17", "10"], ["Aric Tao", "Justin Fields", "8", "$30", "203", "17", "9"], ["Manager TBD", "Isiah Pacheco", "2", "$100", "122", "11", "9"]];
const pricing = [["1", "200"], ["2", "100"], ["3", "80"], ["4", "60"], ["5", "50"], ["6", "30"], ["7", "30"], ["8", "30"], ["9", "30"], ["10", "10"], ["11", "10"], ["12", "10"], ["13", "10"], ["14", "10"], ["15", "10"], ["16", "10"]];
const lotteryTeams = standings.filter((r) => +r[0] >= 7).sort((a, b) => +a[0] - +b[0]).map((r) => [r[0], fixName(stripBadges(r[1])), stripEmoji(r[2])]);

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";
pres.author = "Pete Hodor";
pres.title = "I Yearn For Your Sweet TD's — Beach Summit 2026";
const W = 10, M = 0.5;

function contentTitle(slide, text) {
  slide.background = { color: WHITE };
  slide.addText(text, { x: M, y: 0.32, w: W - 1, h: 0.6, margin: 0, fontFace: FONT, fontSize: 27, bold: true, color: NAVY });
}
function divider(slide, kicker, title) {
  slide.background = { path: DIV_BG };
  slide.addShape(pres.shapes.OVAL, { x: M, y: 2.18, w: 0.16, h: 0.16, fill: { color: GOLD } });
  slide.addText(kicker.toUpperCase(), { x: 0.78, y: 2.05, w: 8, h: 0.4, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE2, charSpacing: 2 });
  slide.addText(title, { x: M, y: 2.5, w: W - 1, h: 1.2, margin: 0, fontFace: FONT, fontSize: 40, bold: true, color: WHITE });
}
function tbl(slide, headers, rows, opt) {
  const o = Object.assign({ x: M, y: 1.2, w: W - 1, fs: 11, colW: null, highlight: -1, numCols: [] }, opt);
  const head = headers.map((h, ci) => ({ text: h, options: { bold: true, color: WHITE, fill: { color: NAVY }, fontSize: o.fs, align: o.numCols.includes(ci) ? "right" : "left", valign: "middle", margin: [3, 6, 3, 6] } }));
  const body = rows.map((r, ri) => r.map((c, ci) => ({ text: fixName(c), options: { color: INK, fontSize: o.fs, valign: "middle", align: o.numCols.includes(ci) ? "right" : "left", bold: ri === o.highlight, fill: { color: ri === o.highlight ? GOLDTINT : (ri % 2 ? WHITE : BAND) }, margin: [3, 6, 3, 6] } })));
  const t = { x: o.x, y: o.y, w: o.w, fontFace: FONT, border: { type: "none" }, valign: "middle", autoPage: false };
  if (o.colW) t.colW = o.colW;
  slide.addTable([head, ...body], t);
}
function tradesTable(slide, rows, y) {
  const headers = ["Date", "Manager", "Win?", "Acquired", "Counterparty", "Win?", "Acquired"];
  const head = headers.map((h) => ({ text: h, options: { bold: true, color: WHITE, fill: { color: NAVY }, fontSize: 9, valign: "middle", align: "left", margin: [3, 5, 3, 5] } }));
  const body = rows.map((r, ri) => r.map((c, ci) => {
    const opts = { fontSize: 9, valign: "middle", align: "left", margin: [3, 5, 3, 5], color: INK, fill: { color: ri % 2 ? WHITE : BAND } };
    const txt = fixName(c);
    if (ci === 2 || ci === 5) { opts.align = "center"; opts.bold = true; if (txt === "✓") opts.color = GREEN; else if (txt === "✗") opts.color = RED; }
    return { text: txt, options: opts };
  }));
  slide.addTable([head, ...body], { x: M, y, w: 9.0, colW: [0.9, 1.5, 0.4, 2.0, 1.5, 0.4, 2.0], fontFace: FONT, border: { type: "none" }, valign: "middle", autoPage: false });
}
function standingsSlide() {
  const sl = pres.addSlide();
  contentTitle(sl, "2025 final standings");
  const top = 1.12, rowH = 0.315, hH = 0.34;
  const maxpf = Math.max.apply(null, standings.map((r) => parseFloat(r[4]) || 0));
  const cols = [["#", 0.55, 0.4, "left"], ["Manager", 1.34, 1.95, "left"], ["Team", 3.4, 2.6, "left"], ["W-L-T", 6.1, 1.0, "left"], ["PF", 7.2, 1.05, "right"], ["PA", 8.35, 1.1, "right"]];
  sl.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: top, w: 9.0, h: hH, fill: { color: NAVY }, line: { color: NAVY } });
  cols.forEach((c) => sl.addText(c[0], { x: c[1], y: top, w: c[2], h: hH, margin: 0, fontFace: FONT, fontSize: 10.5, bold: true, color: WHITE, align: c[3], valign: "middle" }));
  standings.forEach((r, i) => {
    const ry = top + hH + i * rowH;
    const rank = parseInt(r[0]);
    const champ = rank === 1;
    const bg = champ ? GOLDTINT : (i % 2 ? WHITE : BAND);
    sl.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: ry, w: 9.0, h: rowH, fill: { color: bg }, line: { color: bg } });
    const vals = [r[0], fixName(stripBadges(r[1])), stripEmoji(r[2]), r[3], r[4], r[5]];
    cols.forEach((c, ci) => sl.addText(vals[ci], { x: c[1], y: ry, w: c[2], h: rowH, margin: 0, fontFace: FONT, fontSize: 10, bold: champ, color: INK, align: c[3], valign: "middle" }));
    let badge = null;
    if (rank === 1) badge = "medal_gold"; else if (rank === 2) badge = "medal_silver"; else if (rank === 3) badge = "medal_bronze"; else if (rank === 7) badge = "rocket"; else if (rank === standings.length) badge = "poop";
    if (badge) sl.addImage({ path: ICON(badge), x: 1.0, y: ry + (rowH - 0.24) / 2, w: 0.24, h: 0.24 });
    if ((parseFloat(r[4]) || 0) === maxpf) sl.addImage({ path: ICON("crown"), x: 3.04, y: ry + (rowH - 0.22) / 2, w: 0.26, h: 0.22 });
  });
  sl.addText("Medals = top 3 · crown = most points · rocket = won the losers bracket · poop = last place.    Manager TBD = The Lady Boys (FKA Jon Lewitus).",
    { x: 0.5, y: top + hH + standings.length * rowH + 0.12, w: 9.0, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });
}

// TITLE
let s = pres.addSlide();
s.background = { path: TITLE_BG };
s.addText("I Yearn For Your Sweet TD's", { x: M, y: 1.7, w: W - 1, h: 1.0, margin: 0, fontFace: FONT, fontSize: 44, bold: true, color: WHITE });
s.addText("Beach Summit 2026", { x: M, y: 2.8, w: W - 1, h: 0.7, margin: 0, fontFace: FONT, fontSize: 26, bold: true, color: GOLD });
s.addText("Reviewing the 2025 season  ·  keeper costs for 2026", { x: M, y: 3.5, w: W - 1, h: 0.4, margin: 0, fontFace: FONT, fontSize: 14, color: "CFE0FF" });

// SEASON RECAP
divider(pres.addSlide(), "Section 01", "2025 season in review");
standingsSlide();
s = pres.addSlide();
contentTitle(s, "Champion — Brian Malconian");
s.addText("“King Of The Castle” ran away with 2025.", { x: M, y: 1.0, w: W - 1, h: 0.4, margin: 0, fontFace: FONT, fontSize: 15, color: GRAY });
kpiCard(M, 1.7, 2.85, 1.5, "11–3", "Regular-season record");
kpiCard(3.57, 1.7, 2.85, 1.5, "2,430.7", "Points for — league high by ~450");
kpiCard(6.64, 1.7, 2.86, 1.5, "4 of top 5", "Of the 5 highest single-week scores all year, four were his");
s.addText("Also took the regular-season points crown. The only question left: who dethrones him?", { x: M, y: 3.5, w: W - 1, h: 0.5, margin: 0, fontFace: FONT, fontSize: 13, color: INK });
function kpiCard(x, y, w, h, big, label) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: NAVY }, line: { color: NAVY, width: 1 } });
  s.addText(big, { x: x + 0.14, y: y + 0.1, w: w - 0.28, h: h * 0.5, margin: 0, fontFace: FONT, fontSize: 25, bold: true, color: WHITE, valign: "middle" });
  s.addText(label, { x: x + 0.14, y: y + h * 0.52, w: w - 0.28, h: h * 0.42, margin: 0, fontFace: FONT, fontSize: 10, color: BLUE2, valign: "top" });
}
s = pres.addSlide();
contentTitle(s, "Season superlatives");
s.addText("Highest single-week scores", { x: M, y: 1.0, w: 6, h: 0.32, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(s, ["Manager", "Wk", "Opponent", "Score", "Margin"], weeklyHigh.map((r) => [r[0], r[1], r[3], r[2] + "-" + r[4], (parseFloat(r[2]) - parseFloat(r[4])).toFixed(1)]), { y: 1.32, w: 9.0, fs: 10, colW: [2.0, 0.6, 2.0, 1.6, 1.0], numCols: [1, 4] });
s.addText("Biggest blowouts (top 5)", { x: M, y: 3.45, w: 6, h: 0.32, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(s, ["Winner", "Wk", "Loser", "Score", "Margin"], blowouts, { y: 3.77, w: 9.0, fs: 10, colW: [2.0, 0.6, 2.0, 1.6, 1.0], numCols: [1, 4] });
s = pres.addSlide();
contentTitle(s, "Best individual weeks of 2025");
tbl(s, ["#", "Player", "NFL opp", "Wk", "Pts", "Manager", "Result", "Score (for-vs)", "vs Manager"], bestWeeks.map((r, i) => [String(i + 1), ...r]),
  { y: 1.2, fs: 9, colW: [0.4, 1.5, 0.85, 0.4, 0.55, 1.5, 0.65, 1.5, 1.4], numCols: [3, 4] });
s.addText("A monster game doesn't always mean a win — see Gibbs' 49.9 in a loss.", { x: M, y: 5.25, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });

// TRIVIA
divider(pres.addSlide(), "Section 02", "League trivia");
const triviaQs = [
  ["Q1", "Which players were the most transacted in 2025? (Who churned them most, where they finished, total FAAB)"],
  ["Q2", "Which manager made the MOST moves in 2025?"],
  ["Q3", "Which manager made the FEWEST moves in 2025?"],
  ["Q4", "Who spent the most FAAB on a single player in 2025? How much, and on whom?"],
  ["Q5", "Which managers did NOT use all their $100 FAAB — and how much did they spend?"],
  ["Q6", "Most & least efficient keepers — 2025 fantasy points per $ of keeper cost?"],
];
for (const [q, text] of triviaQs) {
  if (q === "Q6" && !q6top.length) continue;
  s = pres.addSlide();
  s.background = { color: WHITE };
  s.addShape(pres.shapes.OVAL, { x: M, y: 1.9, w: 1.5, h: 1.5, fill: { color: NAVY } });
  s.addText(q, { x: M, y: 1.9, w: 1.5, h: 1.5, margin: 0, fontFace: FONT, fontSize: 34, bold: true, color: WHITE, align: "center", valign: "middle" });
  s.addText(text, { x: 2.3, y: 1.6, w: 7.2, h: 2.2, margin: 0, fontFace: FONT, fontSize: 22, bold: true, color: NAVY, valign: "middle" });
  s.addText("Answers at the end — no cheating.", { x: 2.3, y: 3.8, w: 7, h: 0.4, margin: 0, fontFace: FONT, fontSize: 12, italic: true, color: GRAY });
}

// KEEPER PRICING
divider(pres.addSlide(), "Section 03", "Keeper pricing");
s = pres.addSlide();
contentTitle(s, "Keeper cost by draft round");
{
  const half = Math.ceil(pricing.length / 2);
  tbl(s, ["Round", "Keeper $"], pricing.slice(0, half), { x: M, y: 1.2, w: 3.0, fs: 11, colW: [1.5, 1.5], numCols: [0, 1] });
  tbl(s, ["Round", "Keeper $"], pricing.slice(half), { x: 3.7, y: 1.2, w: 3.0, fs: 11, colW: [1.5, 1.5], numCols: [0, 1] });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.0, y: 1.2, w: 2.6, h: 3.4, rectRadius: 0.08, fill: { color: BAND } });
  s.addText([
    { text: "What is DRC?\n", options: { bold: true, color: NAVY, fontSize: 14, breakLine: true } },
    { text: "Draft Round Cost — the draft round you give up to keep a player.\n", options: { fontSize: 11.5, color: INK, breakLine: true } },
    { text: "Lower DRC = more expensive (DRC 1 = a 1st-round pick = $200). Each year a keeper's cost rises one round, capping at round 1.", options: { fontSize: 11.5, color: INK } },
  ], { x: 7.2, y: 1.35, w: 2.25, h: 3.1, margin: 0, valign: "top" });
}

// KEEPER MENUS
divider(pres.addSlide(), "Section 04", "Keeper selection menus");
for (const k of keepers) {
  s = pres.addSlide();
  contentTitle(s, k.name);
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.55, y: 0.34, w: 2.95, h: 0.62, rectRadius: 0.08, fill: { color: NAVY } });
  s.addText([{ text: "Total 2026 keeper cost  ", options: { fontSize: 11, color: BLUE2 } }, { text: "$" + k.total, options: { fontSize: 16, bold: true, color: WHITE } }],
    { x: 6.67, y: 0.34, w: 2.75, h: 0.62, margin: 0, valign: "middle" });
  const rows = k.rows; const half = Math.ceil(rows.length / 2);
  tbl(s, ["Player", "DRC", "$"], rows.slice(0, half), { x: M, y: 1.15, w: 4.45, fs: 9, colW: [3.05, 0.7, 0.7], numCols: [1, 2] });
  if (rows.length > half) tbl(s, ["Player", "DRC", "$"], rows.slice(half), { x: 5.15, y: 1.15, w: 4.35, fs: 9, colW: [2.95, 0.7, 0.7], numCols: [1, 2] });
}

// KEEPER VALUE
divider(pres.addSlide(), "Section 05", "Keeper value vs 2-QB ADP");
s = pres.addSlide();
contentTitle(s, "Steals & reaches");
s.addText([
  { text: "DRC", options: { bold: true, color: NAVY, fontSize: 11 } },
  { text: " = round you pay to keep him (lower = pricier).   ", options: { color: GRAY, fontSize: 11 } },
  { text: "ADP", options: { bold: true, color: NAVY, fontSize: 11 } },
  { text: " = where he's drafted (2-QB).   ", options: { color: GRAY, fontSize: 11 } },
  { text: "Δ", options: { bold: true, color: NAVY, fontSize: 11 } },
  { text: " = ADP round − DRC round. Negative = steal.", options: { color: GRAY, fontSize: 11 } },
], { x: M, y: 0.98, w: W - 1, h: 0.3, margin: 0 });
const vCols = ["Manager", "Player", "DRC", "$", "ADP", "ADP rd", "Δ"];
const vW = [1.7, 2.2, 0.7, 0.8, 0.8, 1.0, 0.8];
s.addText("Biggest steals", { x: M, y: 1.35, w: 4, h: 0.3, margin: 0, fontFace: FONT, fontSize: 12, bold: true, color: BLUE });
tbl(s, vCols, steals, { x: M, y: 1.62, w: 9.0, fs: 9, colW: vW, numCols: [2, 3, 4, 5, 6] });
s.addText("Most overpriced", { x: M, y: 3.5, w: 4, h: 0.3, margin: 0, fontFace: FONT, fontSize: 12, bold: true, color: GOLD });
tbl(s, vCols, overpriced, { x: M, y: 3.77, w: 9.0, fs: 9, colW: vW, numCols: [2, 3, 4, 5, 6] });

// 2025 TRADES
divider(pres.addSlide(), "Section 06", "2025 trades in review");
{
  const per = 8;
  const pages = Math.max(1, Math.ceil(tradesRows.length / per));
  for (let p = 0; p < pages; p++) {
    s = pres.addSlide();
    contentTitle(s, "2025 trades" + (pages > 1 ? `  (${p + 1}/${pages})` : ""));
    s.addText("✓ = acquired players outscored since the trade · ✗ = lost the points battle · blank = player-for-pick", { x: M, y: 0.95, w: W - 1, h: 0.28, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });
    if (tradesRows.length) tradesTable(s, tradesRows.slice(p * per, p * per + per), 1.3);
    else s.addText("(no trades parsed)", { x: M, y: 1.4, w: 6, h: 0.4, fontFace: FONT, fontSize: 12, color: GRAY });
  }
}

// BEST OF THE GROUP CHAT
divider(pres.addSlide(), "Section 07", "Best of the group chat");
let cs = pres.addSlide();
contentTitle(cs, "Chat by the numbers");
cs.addText("Most talkative", { x: M, y: 1.05, w: 4.3, h: 0.35, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(cs, ["Manager", "Messages"], [["Pete (Me)", "2,110"], ["Paul Lewis", "1,498"], ["Tom Watson", "1,120"], ["Brian Malconian", "888"], ["George Mensing", "367"], ["Alex Schlosberg", "308"]], { x: M, y: 1.45, w: 4.3, fs: 11, colW: [3.0, 1.3], numCols: [1] });
cs.addText("Most reactions received", { x: 5.2, y: 1.05, w: 4.3, h: 0.35, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(cs, ["Manager", "Laughs/loves/likes"], [["Pete (Me)", "17"], ["Tom Watson", "15"], ["Paul Lewis", "15"], ["Brian Malconian", "10"], ["George Mensing", "6"]], { x: 5.2, y: 1.45, w: 4.3, fs: 11, colW: [2.9, 1.4], numCols: [1] });
cs.addText("You, Paul, Tom and Brian are ~80% of all 7,000 messages.", { x: M, y: 5.2, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });

// RULES & VOTING
divider(pres.addSlide(), "Section 08", "Rules & voting");
s = pres.addSlide();
contentTitle(s, "On the docket for 2026 — to vote");
s.addText("Pulled from the league thread — let's settle these.", { x: M, y: 0.96, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 11, italic: true, color: GRAY });
s.addText([
  { text: "Replace Jon — pick a new manager for the open seat (The Lady Boys, now Manager TBD).", options: { bullet: true, breakLine: true, fontSize: 12.5, color: INK } },
  { text: "1.01 free-keeper loophole — ban the 1.01 holder from dropping a player in keeper selection and re-grabbing him at 1.01. Failed last year; voting again. (Hodor: opportunity cost; others want it closed.)", options: { bullet: true, breakLine: true, fontSize: 12.5, color: INK } },
  { text: "Keepers to the back of the draft (Tom) — keepers fill from the back; a pick's round only matters for cost. Replaces slide/chasm.", options: { bullet: true, breakLine: true, fontSize: 12.5, color: INK } },
  { text: "Counter-offer rule — a counter MUST include one of the original players (current rule). Given this year's confusion: keep as-is, or adjust?", options: { bullet: true, breakLine: true, fontSize: 12.5, color: INK } },
  { text: "Last-place +300 parlay — did this pass last year? Confirm or vote.   ·   Paul's pick-trading tweak — Paul to clarify, then vote.", options: { bullet: true, breakLine: true, fontSize: 12.5, color: INK } },
  { text: "Already on the books: pick chasm (“Cannobie Lake”), 6th-seed points wildcard, 10% top-points skim, no drop-and-immediately-re-add.", options: { bullet: true, fontSize: 12.5, color: GRAY, italic: true } },
], { x: M, y: 1.34, w: W - 1, h: 3.9, margin: 0, valign: "top", paraSpaceAfter: 8 });
s = pres.addSlide();
contentTitle(s, "Random thoughts & musings");
s.addText("[STARTER — replace with this year's material in your voice]", { x: M, y: 0.98, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: GOLD });
s.addText([
  { text: "The annual State of Paul address.", options: { bullet: true, breakLine: true, fontSize: 14, color: INK } },
  { text: "Verdell watch.", options: { bullet: true, breakLine: true, fontSize: 14, color: INK } },
  { text: "We need Vesco physically present.", options: { bullet: true, breakLine: true, fontSize: 14, color: INK } },
  { text: "Proxies turn Paul into even more of a tyrant.", options: { bullet: true, fontSize: 14, color: INK } },
], { x: M, y: 1.45, w: W - 1, h: 3.5, margin: 0, valign: "top", paraSpaceAfter: 12 });

// DRAFT ORDER & LOTTERY
divider(pres.addSlide(), "Section 09", "Draft order & lottery");
s = pres.addSlide();
contentTitle(s, "2026 draft lottery — non-playoff teams");
{
  const odds = ["50%", "15%", "12.5%", "10%", "7.5%", "5%"];
  const rowsL = lotteryTeams.map((r, i) => [r[1], r[2], odds[i] || "—"]);
  tbl(s, ["Manager", "Team", "Odds at 1.01"], rowsL, { y: 1.2, w: 6.6, fs: 12, colW: [2.4, 3.0, 1.2], numCols: [2] });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.3, y: 1.2, w: 2.3, h: 3.2, rectRadius: 0.08, fill: { color: BAND } });
  s.addText([
    { text: "How it works\n", options: { bold: true, color: NAVY, fontSize: 13, breakLine: true } },
    { text: "Six non-playoff teams enter the lottery for the 1.01 pick. The 2026-27 odds reward the team that just missed the cut.\n\n", options: { fontSize: 11, color: INK, breakLine: true } },
    { text: "We'll run the live draw at the summit.", options: { fontSize: 11, italic: true, color: GRAY } },
  ], { x: 7.5, y: 1.35, w: 1.95, h: 2.9, margin: 0, valign: "top" });
  s.addText("Playoff teams pick by result (champion picks last). Non-playoff order is set by the lottery.", { x: M, y: 5.2, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });
}

// TRIVIA ANSWERS
divider(pres.addSlide(), "Section 10", "Trivia answers");
s = pres.addSlide(); contentTitle(s, "Q1 — Most-transacted players");
tbl(s, ["Player", "Moves", "Most by", "Finished on", "Total FAAB"], q1, { y: 1.15, fs: 9.5, colW: [2.2, 0.9, 2.1, 2.1, 1.2], numCols: [1, 4] });
s = pres.addSlide(); contentTitle(s, "Q2 & Q3 — Most and fewest moves");
s.addText("Most moves", { x: M, y: 1.05, w: 4.5, h: 0.35, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(s, ["Manager", "Moves", "Trades"], q2, { x: M, y: 1.45, w: 4.5, fs: 11, colW: [2.7, 0.9, 0.9], numCols: [1, 2] });
s.addText("Fewest moves", { x: 5.2, y: 1.05, w: 4.3, h: 0.35, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
tbl(s, ["Manager", "Moves", "Trades"], q3, { x: 5.2, y: 1.45, w: 4.3, fs: 11, colW: [2.5, 0.9, 0.9], numCols: [1, 2] });
s.addText("Vescuso made the fewest adds (10) but the most trades (5).", { x: M, y: 5.2, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: GRAY });
s = pres.addSlide(); contentTitle(s, "Q4 — Biggest single FAAB bids");
tbl(s, ["Manager", "Player", "FAAB $", "Date"], q4, { y: 1.2, fs: 11, colW: [2.4, 3.0, 1.4, 2.2], numCols: [2] });
s = pres.addSlide(); contentTitle(s, "Q5 — Left FAAB on the table");
tbl(s, ["Manager", "FAAB spent (of $100)"], q5, { y: 1.2, w: 6.0, fs: 11, colW: [3.0, 3.0], numCols: [1] });
if (q6top.length) {
  s = pres.addSlide(); contentTitle(s, "Q6 — Most & least efficient keepers");
  s.addText("Most efficient — 2025 points per $ of 2026 keeper cost", { x: M, y: 1.0, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: BLUE });
  tbl(s, ["Player", "Manager", "DRC", "$", "2025 Pts", "Pts/$"], q6top, { y: 1.32, w: 9.0, fs: 10.5, colW: [2.1, 2.0, 0.8, 1.0, 1.5, 1.1], numCols: [2, 3, 4, 5] });
  s.addText("Least efficient — priciest underperformers ($50+ keepers)", { x: M, y: 3.35, w: W - 1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: GOLD });
  tbl(s, ["Player", "Manager", "DRC", "$", "2025 Pts", "Pts/$"], q6bot, { y: 3.67, w: 9.0, fs: 10.5, colW: [2.1, 2.0, 0.8, 1.0, 1.5, 1.1], numCols: [2, 3, 4, 5] });
}

pres.writeFile({ fileName: OUT }).then((f) => console.log("WROTE", f)).catch((e) => { console.error(e); process.exit(1); });
