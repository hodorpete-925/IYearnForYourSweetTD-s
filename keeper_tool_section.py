"""keeper_tool_section.py - Embedded keeper roster manager section.

Brian Malconian designed the underlying tool (logic, slide-rule cascade,
overflow detection, etc.). This module ports his HTML body and JavaScript
into our dashboard, restyled to match Pete's Advent palette and section
patterns. All visual rules are scoped under .keeper-tool-root so they
don't leak into other sections.
"""
import base64
from pathlib import Path

_EXT = Path(__file__).parent / "External Sources" / "BG-keeper-roster-manager.html"

_CSS = """<style>
/* ---- Brian's keeper-tool, restyled to match the dashboard ---- */
.keeper-tool-root {
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 22px 26px;
  font-size: 14px;
  color: var(--gray-800);
}
.keeper-tool-root * { box-sizing: border-box; }
.keeper-tool-root > header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  margin-bottom: 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--gray-200);
}
.keeper-tool-root > header h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--blue-800);
  letter-spacing: -0.01em;
}
.keeper-tool-root .totals {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.keeper-tool-root .totals .stat {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  padding: 8px 12px;
  min-width: 80px;
}
.keeper-tool-root .totals .stat .label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gray-500);
}
.keeper-tool-root .totals .stat .value {
  font-size: 18px;
  font-weight: 700;
  color: var(--blue-800);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.keeper-tool-root .totals .stat.cost .value { color: #8C6E10; }
.keeper-tool-root .totals .stat.keepers .value { color: var(--blue-600); }
.keeper-tool-root .totals .stat.ktc .value { color: var(--blue-800); }
.keeper-tool-root .totals .stat.eff .value { color: var(--gray-700); font-size: 16px; }
.keeper-tool-root .totals .stat.overflow .value { color: #b91c1c; }
.keeper-tool-root .info-banner {
  background: rgba(0, 56, 255, 0.05);
  border: 1px solid rgba(0, 56, 255, 0.2);
  border-left: 3px solid var(--blue-600);
  border-radius: 4px;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--gray-700);
  margin-bottom: 16px;
}
.keeper-tool-root main {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 18px;
}
@media (max-width: 900px) {
  .keeper-tool-root main { grid-template-columns: 1fr; }
}
.keeper-tool-root section.card {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 16px;
}
.keeper-tool-root h2 {
  margin: 0 0 12px 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--blue-800);
  letter-spacing: -0.005em;
}
.keeper-tool-root h3 {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gray-500);
}
.keeper-tool-root form input,
.keeper-tool-root form select,
.keeper-tool-root .pool-controls select {
  font-family: inherit;
  font-size: 13px;
  padding: 7px 10px;
  border: 1px solid var(--gray-300);
  border-radius: 4px;
  background: #fff;
  color: var(--gray-800);
  width: 100%;
  margin-bottom: 8px;
}
.keeper-tool-root form input:focus,
.keeper-tool-root form select:focus {
  outline: none;
  border-color: var(--blue-600);
  box-shadow: 0 0 0 2px rgba(0, 56, 255, 0.15);
}
.keeper-tool-root form button,
.keeper-tool-root .actions button {
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  padding: 8px 14px;
  border-radius: 4px;
  border: 1px solid var(--blue-600);
  background: var(--blue-600);
  color: #fff;
  cursor: pointer;
  transition: background 0.12s;
}
.keeper-tool-root form button:hover,
.keeper-tool-root .actions button:hover {
  background: var(--blue-800);
  border-color: var(--blue-800);
}
.keeper-tool-root .actions button {
  background: #fff;
  color: var(--blue-800);
}
.keeper-tool-root .actions button:hover {
  background: var(--gray-50);
}
.keeper-tool-root .actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.keeper-tool-root .form-hint {
  font-size: 11.5px;
  color: var(--gray-500);
  font-style: italic;
  margin: 6px 0 14px 0;
  line-height: 1.4;
}
.keeper-tool-root .pool-controls {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  align-items: center;
}
.keeper-tool-root .pool-controls label {
  font-size: 11px;
  color: var(--gray-500);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.keeper-tool-root #player-pool {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 360px;
  overflow-y: auto;
  padding-right: 4px;
}
.keeper-tool-root .player-chip {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  font-size: 12.5px;
}
.keeper-tool-root .player-chip.rostered {
  background: rgba(0, 56, 255, 0.05);
  border-color: var(--blue-400);
}
.keeper-tool-root .player-chip .chip-meta {
  font-size: 11px;
  color: var(--gray-500);
  font-variant-numeric: tabular-nums;
}
.keeper-tool-root .player-chip button {
  font-size: 11px;
  padding: 3px 8px;
  background: #fff;
  color: var(--blue-800);
  border: 1px solid var(--gray-300);
  border-radius: 3px;
  cursor: pointer;
}
.keeper-tool-root .player-chip button:hover { background: var(--gray-50); }
.keeper-tool-root .player-chip .cascade-note {
  font-size: 10.5px;
  color: var(--gray-500);
  margin-left: 6px;
}
.keeper-tool-root .roster-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 14px;
}
.keeper-tool-root .roster-totals { display: flex; gap: 8px; flex-wrap: wrap; }
.keeper-tool-root .roster-total-badge {
  font-size: 11px;
  background: #fff;
  border: 1px solid var(--gray-200);
  padding: 4px 10px;
  border-radius: 3px;
  font-variant-numeric: tabular-nums;
}
.keeper-tool-root .roster-total-badge strong { color: var(--blue-800); font-weight: 700; }
.keeper-tool-root .roster-group { margin-bottom: 18px; }
.keeper-tool-root .slots {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
  margin-top: 10px;
}
.keeper-tool-root .slot {
  background: var(--gray-50);
  border: 1px dashed var(--gray-300);
  border-radius: 4px;
  padding: 8px;
  min-height: 56px;
  position: relative;
  font-size: 12px;
}
.keeper-tool-root .slot.filled {
  background: rgba(0, 56, 255, 0.04);
  border-style: solid;
  border-color: var(--blue-400);
}
.keeper-tool-root .slot .slot-position {
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gray-500);
  margin-bottom: 4px;
}
.keeper-tool-root .slot .slot-player { font-weight: 600; color: var(--blue-800); }
.keeper-tool-root .slot .slot-meta {
  font-size: 10.5px;
  color: var(--gray-500);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.keeper-tool-root .slot button {
  position: absolute;
  top: 4px;
  right: 4px;
  font-size: 10px;
  padding: 2px 6px;
  background: #fff;
  border: 1px solid var(--gray-300);
  border-radius: 3px;
  cursor: pointer;
  color: var(--gray-700);
}
.keeper-tool-root .round-grid {
  display: grid;
  grid-template-columns: repeat(17, 1fr);
  gap: 6px;
  margin-top: 14px;
}
@media (max-width: 720px) {
  .keeper-tool-root .round-grid { grid-template-columns: repeat(6, 1fr); }
}
.keeper-tool-root .round-cell {
  background: #fff;
  border: 1px solid var(--gray-300);
  border-radius: 4px;
  padding: 8px 4px;
  text-align: center;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.keeper-tool-root .round-cell.open { background: rgba(16, 185, 129, 0.12); border-color: #047857; }
.keeper-tool-root .round-cell.kept { background: var(--gray-100); border-color: var(--gray-400); }
.keeper-tool-root .round-cell.slid-back { background: rgba(225, 181, 35, 0.18); border-color: #E1B523; }
.keeper-tool-root .round-cell.slid-up { background: rgba(249, 115, 22, 0.15); border-color: #f97316; }
.keeper-tool-root .round-cell.overflow { background: rgba(220, 38, 38, 0.12); border-color: #b91c1c; }
.keeper-tool-root .round-cell .round-num { font-weight: 700; color: var(--gray-800); }
.keeper-tool-root .round-cell .round-player {
  font-size: 10px;
  color: var(--gray-700);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.keeper-tool-root .legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 14px;
  font-size: 11px;
  color: var(--gray-700);
}
.keeper-tool-root .legend-item { display: flex; align-items: center; gap: 6px; }
.keeper-tool-root .legend-swatch {
  width: 14px;
  height: 14px;
  border: 1px solid var(--gray-400);
  border-radius: 3px;
}
.keeper-tool-credit {
  margin-top: 24px;
  padding: 14px 16px;
  font-size: 11.5px;
  color: var(--gray-500);
  font-style: italic;
  text-align: center;
  border-top: 1px solid var(--gray-200);
  letter-spacing: 0.02em;
}
.keeper-tool-credit strong { color: var(--gray-700); font-weight: 600; font-style: normal; }
</style>"""


def _load_brian_resources():
    """Extract Brian's body HTML and JS from his standalone HTML file."""
    import re
    raw = _EXT.read_text(encoding="utf-8")
    body_m = re.search(r"<body[^>]*>(.*?)</body>", raw, re.DOTALL)
    body = body_m.group(1).strip() if body_m else ""
    script_m = re.search(r"<script[^>]*>(.*?)</script>", raw, re.DOTALL)
    js = script_m.group(1).strip() if script_m else ""
    # Strip the script tag out of the body so we can place it ourselves
    body = re.sub(r"<script.*?</script>\s*$", "", body, flags=re.DOTALL).strip()
    return body, js


def render():
    """Return the Keeper roster manager section as HTML (CSS + body + JS).
    Returns empty string if Brian's source file is missing."""
    if not _EXT.exists():
        return """
        <section class="team-section" id="keeper-tool" hidden>
          <header class="section-header">
            <h1 class="section-title">Keeper roster manager</h1>
            <p class="section-sub">Source file External Sources/BG-keeper-roster-manager.html not found.</p>
          </header>
        </section>"""
    body_html, js = _load_brian_resources()
    return f"""
    <section class="team-section" id="keeper-tool" hidden>
      <header class="section-header">
        <h1 class="section-title">Keeper roster manager</h1>
        <p class="section-sub">Plan your 2026 keeper stack: add players, assign to rounds, and see the slide rule's cost, overflow, and round availability in real time.</p>
      </header>
      {_CSS}
      <div class="keeper-tool-root">
        {body_html}
      </div>
      <footer class="keeper-tool-credit">
        Tool designed and built by <strong>Brian Malconian</strong> for the league. Ported into the dashboard with light restyling to match the rest of the layout.
      </footer>
      <script>{js}</script>
    </section>"""
