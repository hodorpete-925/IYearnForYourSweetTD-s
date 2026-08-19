# Design Brief — Keeper Dashboard (handoff package)

A self-contained package for a design polish pass on the **"I Yearn For Your Sweet TD's"** fantasy football keeper dashboard. Everything a designer (or another tool) needs is in this folder.

## What's in here

1. **README.md** — this file.
2. **Design Brief.md** — the brief. What the site is, the technical reality (it's a *generated* single HTML file), the four areas to improve, suggested priority, and exactly how to hand work back so it drops straight into the build.
3. **Style Guide.md** — the design system / brand tokens (colors, fonts, components) to work within.
4. **Trade Analyzer Mockup.html** — open it in a browser. The visual target for the biggest change (the trade-analyzer redesign), built with the real brand tokens.
5. **Current Dashboard (reference - do not edit).html** — a snapshot of the live site exactly as it stands today. Open it to see (and click through) what you're improving. **Reference only:** it's a *generated* file, so any change lands in the Python source, never in this HTML (see Design Brief §2). Live version: https://hodorpete-925.github.io/IYearnForYourSweetTD-s/

## Start here

Read **Design Brief.md**, keep **Style Guide.md** open as reference, and open **Trade Analyzer Mockup.html** to see where the trade analyzer is headed.

## The project in one line

A 12-team keeper league. Managers need to see 2026 keeper costs (the league's custom "DRC" cost system), draft history, trades, and be able to model a proposed trade. Live site: `https://hodorpete-925.github.io/IYearnForYourSweetTD-s/`

## The one rule that keeps the loop tight

The site is **generated from a Python script** — never hand-edit the built `dashboard.html`. Hand work back as annotated mockups/screenshots or token-based HTML/CSS snippets, and note which component each change targets. Full detail in **Design Brief.md → §6**.
