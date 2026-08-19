# Style Guide — Design System

*The visual language for the dashboard. It's derived from Pete's brand style guide — the Advent Capital brand book ("Style Guide (AILP).pptx", Oct 2024 edition; canonical source lives at `C:\Users\hodor\Claude\Style Guide\`). Apply this to every visual output unless told otherwise. Web/HTML deliverables use the adaptations at the bottom.*

---

## Fonts

- **Headlines:** Advent Graveur (proprietary — recipients won't have it). **On web, substitute Inter 600** (or the system stack fallback below).
- **Sub-titles:** Inter Bold. **Body:** Inter Regular.
- **Sentence case** for headlines — never Title Case or ALL CAPS. (Small eyebrow/column micro-labels may be uppercase + letter-spaced.)
- **Left-align** text.

## Color

**Primary palette**

| Token | Hex | Use |
|---|---|---|
| Blue 800 | `#022479` | Deep navy — primary dark background, headers, sidebar |
| Blue 600 | `#0038FF` | Vivid blue — primary highlight, links, deltas |
| Blue 400 | `#269AFF` | Medium blue — graphic accent |
| Blue 200 | `#77CEFF` | Light blue — accent on dark |
| Gold 400 | `#E1B523` | Accent only, sparingly |
| Black | `#000000` | Default text on light |
| White | `#FFFFFF` | Default background |
| Gray (secondary text) | `#606C71` | Sub-labels |
| Gray scale | `#666662` · `#999893` · `#CCCBC4` · `#E5E5DD` | Borders, fills, supporting data |

**Extended sets** — use sparingly, **max 3 sets per page** (no rainbows):

- Purple: `#260D6E` / `#3E13BA` / `#7552FF` / `#9886FF`
- Green: `#343C00` / `#6B7D00` / `#B5D208` / `#DDF25D`
- Red: `#4F1B04` / `#982B09` / `#FA6526` / `#FF996D`
- Orange: `#663100` / `#AA5200` / `#FF9600` / `#FFBA57`
- Yellow: `#674F00` / `#A88100` / `#E1B523` / `#FFD95C`

**Gradients:** built from the 600 + 400 of one set, linear left→right (600 on the left). Blue `#0038FF → #269AFF` is the brand default. Max 3 gradient sets per page.

## Components

- **Big numbers:** Inter Bold, left-aligned. Blue gradient on light backgrounds, white on dark. Blue 600 if body-sized. Never gradient numerals inside numbered lists.
- **Bullets:** filled round, same size/color as the text, no trailing period.
- **Tables / data viz:** bold headers and labels. Brand color sequence dark→light. Grays for secondary data. Max 3 color sets per chart.
- **Carrier shapes (KPI cards, callouts):** rectangles or circles only. 800/600 backgrounds → white text; 400 backgrounds → white for large text, black for small; 200 backgrounds → black text. Blue (or blue gradient) is the default. Left-align in rectangles, center in circles.
- **Backgrounds:** pure white primary; off-white acceptable for digital-only; Blue 800 for dark sections. **Never use black as a background.**

## Restraint (the meta-rule)

- Limit dark, gradient, and full-bleed areas.
- Accent colors are strategic, not decorative.
- White space is part of the design.

## Web / HTML specifics

- Font stack: `-apple-system, BlinkMacSystemFont, "Inter", "Helvetica Neue", Arial, sans-serif` — Inter 600 stands in for Advent Graveur on headlines.
- Numeric columns: `font-variant-numeric: tabular-nums`.
- Subtle row dividers `#ebebed`; thicker borders (e.g. `#4a4a4a`) for total rows.
- These exact tokens are already wired into the dashboard's CSS `:root`, so working within them keeps everything consistent.
