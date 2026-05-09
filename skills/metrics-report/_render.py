"""Render a static, self-contained HTML report from local metrics JSONL.

Read-only. Loads ~/.claude/metrics/{auto,retro}.jsonl, applies the same
--since / --project filters as /analyze-metrics, and emits a single HTML
file with embedded CSS + inline SVG. Zero JavaScript, zero CDN, zero
network calls — opens directly via file://.

Stdlib-only Python 3.9+. Reuses filter/project helpers from the installed
analyze-metrics skill so both reports stay in lockstep on identity rules.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENERATOR_VERSION = "0.1.0"


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))


def _resolve_skill_dir(filename: str, skill_name: str = "analyze-metrics",
                       env_var: str = "CCM_HELPERS_DIR") -> Path:
    """Find a sibling skill directory containing `filename`.

    Resolution order:
      1. `env_var` env var (CCM_HELPERS_DIR for analyze-metrics, used by tests).
      2. Sibling skill in the repo (dev: skills/<skill_name> next to us).
      3. Installed skill at $CLAUDE_HOME/skills/<skill_name>.

    Fails loudly with a clear message rather than silently degrading — the
    filter and aggregation rules are load-bearing for correct numbers.
    """
    candidates = []
    env_dir = os.environ.get(env_var)
    if env_dir:
        candidates.append(Path(env_dir))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / skill_name)
    candidates.append(_claude_home() / "skills" / skill_name)
    for cand in candidates:
        if (cand / filename).is_file():
            return cand
    raise SystemExit(
        f"metrics-report: could not locate {skill_name}/{filename}.\n"
        "Tried: " + ", ".join(str(c) for c in candidates) + "\n"
        f"Reinstall with scripts/install.sh, or set {env_var}."
    )


def _import_from(d: Path, mod_name: str):
    """Import `mod_name` from `d`, then immediately clean up sys.path.

    Avoids leaking the analyze-metrics skill dir onto sys.path for the rest
    of the process — it would otherwise shadow any future `_helpers` /
    `_aggregate` imports made by callers that embed this module.
    """
    inserted = str(d)
    sys.path.insert(0, inserted)
    try:
        return __import__(mod_name)
    finally:
        try:
            sys.path.remove(inserted)
        except ValueError:
            pass


def _load_helpers():
    return _import_from(_resolve_skill_dir("_helpers.py"), "_helpers")


def _load_aggregate():
    # _aggregate imports from _helpers; loading helpers first puts the module
    # in sys.modules so the inner `from _helpers import ...` resolves from
    # cache without needing sys.path to stay polluted.
    return _import_from(_resolve_skill_dir("_aggregate.py"), "_aggregate")


def _load_recommend():
    # _recommend lives in the recommend skill dir; it imports _helpers from
    # the analyze-metrics dir lazily inside its rule functions, so the
    # caller (main) must load helpers first to populate sys.modules.
    return _import_from(
        _resolve_skill_dir(
            "_recommend.py",
            skill_name="recommend",
            env_var="CCM_RECOMMEND_DIR",
        ),
        "_recommend",
    )


# Lazy-loaded on first call to main(), so importing this module for tests
# without the helpers in place still works.
_h: Any = None
_agg: Any = None
_reco: Any = None


# ----------------------------------------------------------------------
# HTML / SVG primitives
# ----------------------------------------------------------------------

def _esc(s: Any) -> str:
    """HTML-escape a value, coercing to str. Quotes escaped too for safety
    in attribute contexts (we use a single helper everywhere)."""
    return html.escape("" if s is None else str(s), quote=True)


def _fmt_money(v: float | int | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:,.4f}"


def _fmt_int(v: int | float | None) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{_esc(sub)}</div>' if sub else ""
    return (
        '<div class="kpi">'
        f'<div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div>'
        f'{sub_html}'
        '</div>'
    )


def _bar_inline(value: float, max_value: float, width_px: int = 120,
                cls: str = "") -> str:
    """A small horizontal bar suitable for inline use inside table cells."""
    if max_value <= 0 or value <= 0:
        return ""
    w = max(1, round(value / max_value * width_px))
    cls_attr = f" {cls}" if cls else ""
    return f'<span class="bar{cls_attr}" style="width:{w}px"></span>'


def _bar_chart_svg(items: list[tuple[str, float, str]], width: int = 720,
                   height: int = 220) -> str:
    """Vertical-bar SVG for the cost-by-week chart.

    items: list of (label, value, tooltip). Renders gridlines at 25/50/75/100%,
    Y-axis ticks, and a min bar height so very small non-zero values stay
    visible (otherwise a single dominant week swallows the rest of the chart).
    """
    if not items:
        return ""
    n = len(items)
    pad_l, pad_r = 60, 16
    pad_t, pad_b = 18, 40
    chart_w = max(50, width - pad_l - pad_r)
    chart_h = max(40, height - pad_t - pad_b)
    max_v = max((v for _, v, _ in items), default=0.0)
    if max_v <= 0:
        max_v = 1.0
    slot = chart_w / n
    bw = max(2.0, min(48.0, slot * 0.7))
    min_visible_h = 3.0  # px; ensures tiny non-zero bars still register

    parts = [
        f'<svg class="svg-bars" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="cost by ISO week">'
    ]
    # Gridlines at 0%, 25%, 50%, 75%, 100%
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + chart_h - frac * chart_h
        is_axis = (frac == 0.0)
        opacity = 0.18 if is_axis else 0.08
        dash = "" if is_axis else 'stroke-dasharray="2 3"'
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" '
            f'x2="{width - pad_r}" y2="{gy:.1f}" '
            f'stroke="currentColor" stroke-opacity="{opacity}" {dash} />'
        )
        # Y-axis tick label
        tick_v = max_v * frac
        parts.append(
            f'<text x="{pad_l - 8}" y="{gy + 3.5:.1f}" font-size="10" '
            f'text-anchor="end" fill="currentColor" fill-opacity="0.55" '
            f'font-family="ui-monospace, SF Mono, Menlo, monospace">'
            f'${tick_v:,.2f}</text>'
        )

    for i, (label, value, tooltip) in enumerate(items):
        x = pad_l + i * slot + (slot - bw) / 2
        raw_h = (value / max_v) * chart_h if max_v > 0 else 0
        h = max(min_visible_h, raw_h) if value > 0 else 0
        y = pad_t + chart_h - h
        # Show every label if <=12 bars; else thin out to keep readable.
        show_label = (n <= 12) or (i % max(1, n // 8) == 0) or (i == n - 1)
        label_html = ""
        if show_label:
            label_html = (
                f'<text x="{x + bw/2:.1f}" y="{height - pad_b + 16}" '
                'font-size="10" text-anchor="middle" '
                'fill="currentColor" fill-opacity="0.65">'
                f'{_esc(label)}</text>'
            )
        parts.append(
            f'<g><title>{_esc(tooltip)}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" rx="2" fill="var(--color-accent)" />'
            f'{label_html}</g>'
        )
    parts.append('</svg>')
    return "".join(parts)


def _section(anchor: str, title: str, body: str, note: str = "") -> str:
    note_html = (
        f'<p class="section-note">{_esc(note)}</p>' if note else ""
    )
    return (
        f'<section id="{_esc(anchor)}">'
        f'<h2>{_esc(title)}</h2>'
        f'{note_html}'
        f'{body}'
        '</section>'
    )


def _empty(msg: str) -> str:
    return f'<p class="empty">{_esc(msg)}</p>'


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    """headers: list of (label, is_numeric). rows are pre-rendered HTML.

    Caller is responsible for escaping; this lets us inject tiny
    pre-built HTML like inline bars or <abbr> tags into cells.
    """
    if not rows:
        return _empty("no data")
    head = "".join(
        f'<th class="num">{_esc(h)}</th>' if num else f'<th>{_esc(h)}</th>'
        for h, num in headers
    )
    body_rows = []
    for r in rows:
        cells = []
        for (_, num), cell in zip(headers, r):
            if num:
                cells.append(f'<td class="num">{cell}</td>')
            else:
                cells.append(f'<td>{cell}</td>')
        body_rows.append('<tr>' + "".join(cells) + '</tr>')
    return (
        '<table><thead><tr>' + head + '</tr></thead>'
        '<tbody>' + "".join(body_rows) + '</tbody></table>'
    )


# ----------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip malformed lines silently — same forgiving stance as the
            # /analyze-metrics skill. The hook only ever writes valid JSON,
            # so a bad line is a manual edit; we don't crash the report.
            continue
    return rows


def _load_pricing(metrics_dir: Path) -> dict:
    p = metrics_dir / "pricing.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ----------------------------------------------------------------------
# Section renderers
# ----------------------------------------------------------------------

CSS = """
/* =========================================================
   Design tokens
   ---------------------------------------------------------
   Type scale: 1.125 modular (major-second) on 14px base.
   Spacing:    4px base, used as multiples (4 / 8 / 12 / 16 ...).
   Color:      Radix-inspired neutral scale, semantic naming.
   Targets WCAG AA contrast on body text in both modes.
========================================================= */
:root {
  color-scheme: light dark;

  /* color (light) */
  --color-bg:            #fbfbfc;
  --color-surface:       #ffffff;
  --color-surface-2:     #f5f6f8;
  --color-surface-3:     #eceef2;
  --color-border:        #e3e5ea;
  --color-border-strong: #c9cdd5;
  --color-text:          #14161a;
  --color-text-muted:    #545a66;
  --color-text-subtle:   #6b717c;
  --color-accent:        #5b6cf6;
  --color-accent-soft:   rgba(91, 108, 246, 0.10);
  --color-success:       #1f8a44;
  --color-success-soft:  rgba(31, 138, 68, 0.12);
  --color-warning:       #b46410;
  --color-warning-soft:  rgba(180, 100, 16, 0.12);
  --color-danger:        #b8311b;
  --color-danger-soft:   rgba(184, 49, 27, 0.10);

  /* type scale (1.125 ratio) */
  --text-2xs:   10.5px;
  --text-xs:    11.5px;
  --text-sm:    12.5px;
  --text-base:  14px;
  --text-md:    15.75px;
  --text-lg:    17.5px;
  --text-xl:    20px;
  --text-2xl:   24px;
  --text-3xl:   30px;
  --text-4xl:   34px;

  /* line-heights */
  --leading-tight:   1.2;
  --leading-snug:    1.35;
  --leading-normal:  1.5;
  --leading-relaxed: 1.6;

  /* spacing (4px base) */
  --sp-1:  4px;
  --sp-2:  8px;
  --sp-3:  12px;
  --sp-4:  16px;
  --sp-5:  20px;
  --sp-6:  24px;
  --sp-8:  32px;
  --sp-10: 40px;
  --sp-12: 48px;
  --sp-16: 64px;

  /* radii */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;
  --radius-xl: 14px;

  /* font stacks */
  --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI",
               system-ui, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;

  /* effects */
  --shadow-sm: 0 1px 2px rgba(15, 18, 25, 0.04);
  --shadow-md: 0 1px 2px rgba(15, 18, 25, 0.05),
               0 4px 12px rgba(15, 18, 25, 0.04);
  --focus-ring: 0 0 0 3px var(--color-accent-soft);
}

@media (prefers-color-scheme: dark) {
  :root {
    --color-bg:            #0c0e12;
    --color-surface:       #14171d;
    --color-surface-2:     #181c23;
    --color-surface-3:     #1f242c;
    --color-border:        #262b34;
    --color-border-strong: #353c47;
    --color-text:          #e8eaee;
    --color-text-muted:    #a8b0bb;
    --color-text-subtle:   #8a909c;
    --color-accent:        #8c9bff;
    --color-accent-soft:   rgba(140, 155, 255, 0.16);
    --color-success:       #4ade80;
    --color-success-soft:  rgba(74, 222, 128, 0.14);
    --color-warning:       #fbbf24;
    --color-warning-soft:  rgba(251, 191, 36, 0.14);
    --color-danger:        #f87171;
    --color-danger-soft:   rgba(248, 113, 113, 0.14);

    --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
    --shadow-md: 0 1px 2px rgba(0, 0, 0, 0.4),
                 0 4px 12px rgba(0, 0, 0, 0.3);
  }
}

/* =========================================================
   Reset + base
========================================================= */
*, *::before, *::after { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--color-bg);
  color: var(--color-text);
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: var(--leading-normal);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}
:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
  border-radius: var(--radius-sm);
}
a {
  color: var(--color-accent);
  text-decoration: none;
}
a:hover { text-decoration: underline; text-underline-offset: 3px; }

/* =========================================================
   Layout
========================================================= */
.layout {
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  gap: var(--sp-12);
  max-width: 1180px;
  margin: 0 auto;
  padding: var(--sp-10) var(--sp-8) var(--sp-16);
}
@media (max-width: 960px) {
  .layout {
    grid-template-columns: 1fr;
    gap: var(--sp-6);
    padding: var(--sp-5) var(--sp-4) var(--sp-12);
  }
  nav.toc { position: static !important; max-height: none !important; border-right: 0; padding-right: 0; }
}
main { min-width: 0; }

/* =========================================================
   Table of contents (sidebar)
========================================================= */
nav.toc {
  position: sticky;
  top: var(--sp-6);
  align-self: start;
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  max-height: calc(100vh - var(--sp-12));
  overflow-y: auto;
  padding-right: var(--sp-4);
  border-right: 1px solid var(--color-border);
}
nav.toc h3 {
  margin: 0 0 var(--sp-3);
  font-size: var(--text-2xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--color-text-muted);
}
nav.toc ul { list-style: none; padding: 0; margin: 0; }
nav.toc li { margin: 0; }
nav.toc a {
  display: block;
  padding: var(--sp-1) var(--sp-2);
  margin-left: calc(var(--sp-2) * -1);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
}
nav.toc a:hover {
  color: var(--color-text);
  background: var(--color-surface-2);
  text-decoration: none;
}

/* =========================================================
   Hero / page header
========================================================= */
header.hero {
  margin-bottom: var(--sp-6);
}
header.hero h1 {
  margin: 0 0 var(--sp-1);
  font-size: var(--text-4xl);
  font-weight: 700;
  line-height: var(--leading-tight);
  letter-spacing: -0.02em;
}
header.hero .subtitle {
  margin: 0 0 var(--sp-2);
  color: var(--color-text-muted);
  font-size: var(--text-md);
  line-height: var(--leading-snug);
}
header.hero .meta {
  margin: 0;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  overflow-wrap: anywhere;
}
header.hero .meta + .meta {
  margin-top: var(--sp-1);
}
header.hero .health-warning {
  margin: var(--sp-2) 0 0;
  padding: var(--sp-2) var(--sp-3);
  border-left: 3px solid var(--color-danger);
  background: var(--color-danger-soft);
  color: var(--color-danger);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 600;
}

/* =========================================================
   KPI cards
========================================================= */
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
  margin: var(--sp-6) 0 var(--sp-10);
}
.kpi {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--sp-4) var(--sp-5);
  box-shadow: var(--shadow-sm);
  transition: border-color 120ms ease;
}
.kpi:hover { border-color: var(--color-border-strong); }
.kpi .label {
  font-size: var(--text-2xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
.kpi .value {
  margin: var(--sp-2) 0 var(--sp-1);
  font-size: var(--text-3xl);
  font-weight: 600;
  line-height: var(--leading-tight);
  letter-spacing: -0.015em;
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
  /* Long values are clipped with an ellipsis rather than wrapping —
     keeps all KPI cards at a uniform height. */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kpi .sub {
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* =========================================================
   Sections
========================================================= */
section {
  margin: 0 0 var(--sp-10);
  scroll-margin-top: var(--sp-5);
}
section h2 {
  margin: 0 0 var(--sp-2);
  padding-bottom: var(--sp-2);
  font-size: var(--text-xl);
  font-weight: 600;
  line-height: var(--leading-snug);
  letter-spacing: -0.011em;
  border-bottom: 1px solid var(--color-border);
}
section h3 {
  margin: var(--sp-5) 0 var(--sp-2);
  font-size: var(--text-md);
  font-weight: 600;
  line-height: var(--leading-snug);
  color: var(--color-text);
}
section .section-note {
  margin: 0 0 var(--sp-4);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
  color: var(--color-text-muted);
  max-width: 68ch;
}

/* =========================================================
   Callout (highlight box)
========================================================= */
.callout {
  margin: var(--sp-3) 0 var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-accent);
  background: var(--color-accent-soft);
  border-radius: var(--radius-md);
  font-size: var(--text-base);
  line-height: var(--leading-normal);
  color: var(--color-text);
}
.callout strong {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.callout.good { background: var(--color-success-soft); border-left-color: var(--color-success); }
.callout.warn { background: var(--color-warning-soft); border-left-color: var(--color-warning); }
.callout.bad  { background: var(--color-danger-soft);  border-left-color: var(--color-danger);  }

/* =========================================================
   Tables
========================================================= */
table {
  width: 100%;
  margin-top: var(--sp-2);
  border-collapse: separate;
  border-spacing: 0;
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}
thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--color-surface-2);
  font-size: var(--text-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
th, td {
  padding: var(--sp-3) var(--sp-4);
  text-align: left;
  vertical-align: middle;
  border-bottom: 1px solid var(--color-border);
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr { transition: background-color 100ms ease; }
tbody tr:hover td { background: var(--color-surface-2); }
td.num, th.num {
  text-align: right;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--text-sm);
}

/* =========================================================
   Inline bar (used inside table cells)
========================================================= */
.bar {
  display: inline-block;
  height: 6px;
  margin-right: var(--sp-2);
  border-radius: 999px;
  background: var(--color-accent);
  vertical-align: middle;
  opacity: 0.85;
}
.bar.good { background: var(--color-success); }
.bar.warn { background: var(--color-warning); }
.bar.bad  { background: var(--color-danger); }

/* =========================================================
   Misc primitives
========================================================= */
.empty {
  margin: var(--sp-2) 0 0;
  padding: var(--sp-4);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  font-style: italic;
  background: var(--color-surface-2);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  text-align: center;
}
.foot-note {
  margin: var(--sp-3) 0 0;
  font-size: var(--text-sm);
  color: var(--color-text-muted);
  line-height: var(--leading-snug);
}
abbr[title] {
  text-decoration: underline dotted var(--color-text-subtle);
  text-underline-offset: 3px;
  cursor: help;
}
code, .mono {
  padding: 1px var(--sp-1);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.875em;
  color: var(--color-text);
}

/* =========================================================
   SVG charts
========================================================= */
.svg-bars {
  display: block;
  max-width: 100%;
  height: auto;
  margin-bottom: var(--sp-3);
  color: var(--color-text);
}

/* =========================================================
   Footer
========================================================= */
footer {
  margin-top: var(--sp-12);
  padding-top: var(--sp-5);
  border-top: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  line-height: var(--leading-relaxed);
}
footer p { margin: 0 0 var(--sp-1); }
footer .cmd {
  display: inline-block;
  padding: 2px var(--sp-2);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.875em;
  overflow-wrap: anywhere;
}

/* =========================================================
   Print
========================================================= */
@media print {
  :root { --color-bg: #ffffff; --color-surface: #ffffff; --color-surface-2: #f7f7f7; }
  body { background: #ffffff; color: #000000; }
  nav.toc { display: none; }
  .layout { grid-template-columns: 1fr; padding: 0; max-width: none; }
  header.hero h1 { font-size: 24px; }
  section { margin-bottom: var(--sp-6); }
  .callout, .kpi, table, section { break-inside: avoid; page-break-inside: avoid; }
  thead { display: table-header-group; }
  a { color: inherit; text-decoration: none; }
}

/* =========================================================
   Reduced motion
========================================================= */
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
  html { scroll-behavior: auto; }
}
"""


SECTIONS_INDEX = [
    ("hero", "Overview"),
    ("recommendations", "Recommendations"),
    ("trend", "Cost trend"),
    ("by-model", "Cost by model"),
    ("by-project", "Cost by project"),
    ("cache-savings", "Cache savings"),
    ("cache-eff", "Cache efficiency"),
    ("top-expensive", "Top sessions"),
    ("marathons", "Marathon sessions"),
    ("subagent-surface", "Subagent return surface"),
    ("subagent-slow", "Slowest subagents"),
    ("subagent-cheap", "Cheap subagent calls"),
    ("subagent-invocations", "Subagent invocations"),
    ("tool-errors", "Tool errors"),
    ("correction-heavy", "Correction-heavy sessions"),
    ("top-tools", "Top tools"),
    ("retro", "Retro insights"),
]


def _render_toc() -> str:
    items = "".join(
        f'<li><a href="#{anchor}">{_esc(label)}</a></li>'
        for anchor, label in SECTIONS_INDEX
    )
    return (
        '<nav class="toc">'
        '<h3>Sections</h3>'
        f'<ul>{items}</ul>'
        '</nav>'
    )


def _render_hero(meta: dict, agg: dict, all_time: dict) -> str:
    n = meta["n_sessions"]
    total_cost = sum(agg["costs"]) if agg["costs"] else 0.0
    retro_count = meta["retro_count"]

    if meta["ts_range"]:
        first, last = meta["ts_range"]
        try:
            d_first = datetime.fromisoformat(first.replace("Z", "+00:00"))
            d_last = datetime.fromisoformat(last.replace("Z", "+00:00"))
            span_days = max(1, (d_last - d_first).days + 1)
            span_str = "1 day" if span_days == 1 else f"{span_days} days"
            # Compact range: drop the year when first and last share it.
            if d_first.year == d_last.year:
                range_sub = (
                    f'{d_first.strftime("%b %d")} → {d_last.strftime("%b %d")}'
                )
            else:
                range_sub = (
                    f'{d_first.strftime("%Y-%m-%d")} → '
                    f'{d_last.strftime("%Y-%m-%d")}'
                )
        except (ValueError, TypeError):
            span_str = "—"
            range_sub = ""
    else:
        span_str = "—"
        range_sub = ""

    kpis = "".join([
        _kpi(
            "Sessions",
            _fmt_int(n),
            f'{meta["err_count"]} errored' if meta["err_count"] else "in window",
        ),
        _kpi("Estimated cost", _fmt_money(total_cost), "summed"),
        _kpi(
            "Retro coverage",
            f'{retro_count} / {n}',
            "subjective metrics" if retro_count else "run /retrospective",
        ),
        _kpi("Data span", span_str, range_sub),
    ])

    schema_str = ", ".join(f"v{v}" for v in meta["schema_versions"]) or "—"
    scope = "all-time (this project)" if meta.get("project_filter_active") else "all-time"
    all_time_str = (
        f'{scope}: {all_time["total_sessions"]} sessions, '
        f'{_fmt_money(all_time["total_cost"])} total'
    )

    n_err = meta.get("recent_hook_errors", 0) or 0
    last_age = meta.get("last_session_age", "never")
    health_line = (
        f'Last session: {_esc(last_age)}  ·  hook errors (7d): {_esc(str(n_err))}'
    )
    warning_html = (
        f'<p class="health-warning">⚠ {_esc(n_err)} hook errors in the last 7 days '
        '— check <code>~/.claude/metrics/hook.log</code></p>'
    ) if n_err > 0 else ''

    header = (
        '<header class="hero">'
        '<h1>claude-code-metrics</h1>'
        f'<p class="subtitle">Window: {_esc(meta["window_label"])}'
        f'  ·  project: {_esc(meta["project_filter"])}'
        f'  ·  schema rows: {_esc(schema_str)}</p>'
        f'<p class="meta">{_esc(all_time_str)}</p>'
        f'<p class="meta">{health_line}</p>'
        f'{warning_html}'
        '</header>'
        f'<section id="hero" aria-label="overview">'
        f'<div class="kpis">{kpis}</div>'
        '</section>'
    )
    return header


def _render_trend(by_week: dict[str, list[float]]) -> str:
    if not by_week:
        return _section("trend", "Cost trend by ISO week",
                        _empty("no dated sessions"))
    items = sorted(by_week.items())
    chart_items = [
        (k, c, f"{k}: {_fmt_money(c)} ({n} sessions)")
        for k, (c, n) in items
    ]
    svg = _bar_chart_svg(chart_items, width=820, height=220)
    rows = []
    for k, (c, n) in items:
        avg = c / n if n else 0
        rows.append([
            _esc(k),
            _esc(str(n)),
            _esc(_fmt_money(c)),
            _esc(_fmt_money(avg)),
        ])
    table = _table(
        [("Week", False), ("Sessions", True), ("Total", True), ("Avg/session", True)],
        rows,
    )
    body = svg + table
    return _section("trend", "Cost trend by ISO week", body,
                    note="Hover bars for week totals.")


def _render_by_model(by_model: dict[str, list[float]]) -> str:
    if not by_model:
        return _section("by-model", "Cost by model", _empty("no cost data"))
    max_total = max(sum(v) for v in by_model.values()) or 1.0
    rows = []
    for m, v in sorted(by_model.items(), key=lambda kv: sum(kv[1]), reverse=True):
        total = sum(v)
        avg = statistics.mean(v) if v else 0
        bar = _bar_inline(total, max_total)
        rows.append([
            _esc(m),
            _esc(str(len(v))),
            _esc(_fmt_money(avg)),
            f'{bar}<span>{_esc(_fmt_money(total))}</span>',
        ])
    return _section(
        "by-model", "Cost by model",
        _table(
            [("Model", False), ("Sessions", True), ("Avg cost", True), ("Total", True)],
            rows,
        ),
    )


def _render_by_project(by_proj_key: dict[str, list[float]]) -> str:
    if not by_proj_key:
        return _section("by-project", "Cost by project", _empty("no cost data"))
    label = _agg._disambiguated_label_factory(by_proj_key)
    max_total = max(sum(v) for v in by_proj_key.values()) or 1.0
    rows = []
    for k, v in sorted(by_proj_key.items(), key=lambda kv: sum(kv[1]), reverse=True):
        total = sum(v)
        avg = statistics.mean(v) if v else 0
        bar = _bar_inline(total, max_total)
        rows.append([
            _esc(label(k)),
            _esc(str(len(v))),
            _esc(_fmt_money(avg)),
            f'{bar}<span>{_esc(_fmt_money(total))}</span>',
        ])
    return _section(
        "by-project", "Cost by project",
        _table(
            [("Project", False), ("Sessions", True), ("Avg cost", True), ("Total", True)],
            rows,
        ),
    )


def _render_cache_savings(agg: dict) -> str:
    sb = agg["savings_by_model"]
    total_saved = agg["total_saved"]
    actual = agg["total_actual"]
    counter = agg["total_counterfactual"]
    if not sb:
        return _section(
            "cache-savings", "Cache savings (counterfactual)",
            _empty("no cache_read tokens captured, or pricing.json unavailable"),
        )
    rows = []
    for m, (s, cr) in sorted(sb.items(), key=lambda kv: kv[1][0], reverse=True):
        rows.append([
            _esc(m),
            _esc(_fmt_int(cr)),
            _esc(_fmt_money(s)),
        ])
    table = _table(
        [("Model", False), ("cache_read tokens", True), ("Saved vs no-cache", True)],
        rows,
    )
    callout = (
        f'<div class="callout good">'
        f'Cache saved you <strong>{_esc(_fmt_money(total_saved))}</strong> '
        f'in this window.'
    )
    if actual > 0:
        ratio = counter / actual
        callout += (
            f' Actual <strong>{_esc(_fmt_money(actual))}</strong> '
            f'vs no-cache counterfactual <strong>{_esc(_fmt_money(counter))}</strong>'
            f' (<strong>{ratio:.1f}×</strong> cheaper with cache).'
        )
    callout += '</div>'
    return _section(
        "cache-savings",
        "Cache savings (counterfactual)",
        callout + table,
        note=(
            "What you would have paid if cache_read tokens had been billed as "
            "fresh input tokens. Counterfactual, not invoiced."
        ),
    )


def _render_cache_eff(cache_by_model: dict[str, list[int]]) -> str:
    if not cache_by_model:
        return _section("cache-eff", "Cache efficiency by model",
                        _empty("no cache data"))
    rows = []
    for m, (r, c) in sorted(cache_by_model.items()):
        if r + c == 0:
            rows.append([_esc(m), "—", _esc(_fmt_int(r)), _esc(_fmt_int(c))])
            continue
        pct = r / (r + c) * 100
        bar = _bar_inline(pct, 100, width_px=80,
                          cls="good" if pct >= 60 else ("warn" if pct >= 30 else "bad"))
        rows.append([
            _esc(m),
            f'{bar}<span>{pct:.1f}%</span>',
            _esc(_fmt_int(r)),
            _esc(_fmt_int(c)),
        ])
    return _section(
        "cache-eff", "Cache efficiency by model",
        _table(
            [("Model", False),
             ("Hit rate", True),
             ("cache_read", True),
             ("cache_create", True)],
            rows,
        ),
        note=(
            "Hit rate = cache_read / (cache_read + cache_create). Low values "
            "mean the context is being rebuilt rather than reused."
        ),
    )


def _render_top_expensive(top: list[dict], retro_by_id: dict) -> str:
    if not top:
        return _section("top-expensive", "Top 10 most expensive sessions",
                        _empty("no cost data"))
    max_cost = max((a.get("cost_usd") or 0) for a in top) or 1.0
    rows = []
    for a in top:
        cost = a.get("cost_usd") or 0
        proj = _h._project_label(_h._project_key(a)) or "—"
        outcome = retro_by_id.get(a.get("session_id", ""), {}).get("task_outcome", "—")
        bar = _bar_inline(cost, max_cost)
        rows.append([
            f'<code>{_esc((a.get("session_id") or "")[:12])}</code>',
            f'{bar}<span>{_esc(_fmt_money(cost))}</span>',
            _esc(outcome),
            _esc(proj),
        ])
    return _section(
        "top-expensive", "Top 10 most expensive sessions",
        _table(
            [("Session", False), ("Cost", True), ("Outcome", False), ("Project", False)],
            rows,
        ),
    )


def _render_marathons(marathons: list[dict], total_cost_in_window: float,
                      marathon_total: float, marathon_count: int) -> str:
    if not marathons:
        return _section(
            "marathons", "Marathon sessions (turn_count > 300)",
            _empty("no marathon sessions detected"),
        )
    rows = []
    for a in marathons:
        proj = _h._project_label(_h._project_key(a)) or "—"
        rows.append([
            f'<code>{_esc((a.get("session_id") or "")[:12])}</code>',
            _esc(str(a.get("turn_count") or 0)),
            _esc(_fmt_int(a.get("tool_calls_total") or 0)),
            _esc(_fmt_money(a.get("cost_usd") or 0)),
            _esc(proj),
        ])
    shown = len(marathons)
    pct = (marathon_total / total_cost_in_window * 100) if total_cost_in_window else 0
    extra = (f' (showing top {shown})' if marathon_count > shown else '')
    callout = (
        f'<div class="callout warn">'
        f'<strong>{marathon_count}</strong> marathon sessions account for '
        f'<strong>{_esc(_fmt_money(marathon_total))}</strong> '
        f'(<strong>{pct:.1f}%</strong> of in-window spend){_esc(extra)}.</div>'
    )
    table = _table(
        [("Session", False), ("Turns", True), ("Tool calls", True),
         ("Cost", True), ("Project", False)],
        rows,
    )
    return _section(
        "marathons",
        "Marathon sessions (turn_count > 300)",
        callout + table,
        note=("turn_count > 300 typically signals an autonomous run that "
              "drifts; these are the prime candidates for review."),
    )


def _render_subagent_surface(surface: dict, v4_rows: int) -> str:
    if v4_rows == 0:
        return _section(
            "subagent-surface",
            "Subagent return surface (v4+)",
            _empty("no v4 sessions yet — schema bump captures this going forward"),
        )
    if not surface:
        return _section(
            "subagent-surface",
            "Subagent return surface (v4+)",
            _empty(f"no Agent calls in {v4_rows} v4 sessions"),
        )
    total_chars = sum(s["return_chars"] for s in surface.values()) or 1
    rows = []
    for sub, s in sorted(surface.items(),
                         key=lambda kv: kv[1]["return_chars"], reverse=True):
        avg = s["return_chars"] // s["count"] if s["count"] else 0
        pct = s["return_chars"] / total_chars * 100
        bar = _bar_inline(pct, 100, width_px=80)
        rows.append([
            _esc(sub),
            _esc(str(s["count"])),
            _esc(_fmt_int(s["return_chars"])),
            _esc(_fmt_int(avg)),
            _esc(f'{s["duration_s"]:.1f}'),
            _esc(str(s["errors"])),
            f'{bar}<span>{pct:.1f}%</span>',
        ])
    return _section(
        "subagent-surface",
        "Subagent return surface (v4+)",
        _table(
            [
                ("Subagent", False),
                ("Calls", True),
                ("Chars total", True),
                ("Avg chars", True),
                ("Duration s", True),
                ("Errors", True),
                ("% of return", True),
            ],
            rows,
        ),
        note=(
            "How much each subagent type dumps back into the parent context. "
            "Tokens cannot be attributed per-subagent (the transcript only "
            "records main-agent usage), so we measure the return surface — "
            "this is what actually inflates context and drives cost/latency."
        ),
    )


def _render_subagent_slow(surface: dict, v4_rows: int) -> str:
    if v4_rows == 0 or not surface:
        return _section(
            "subagent-slow", "Slowest subagent types (v4+)",
            _empty("no v4 sessions yet" if v4_rows == 0 else "no subagent activity"),
        )
    rows = []
    for sub, s in sorted(surface.items(),
                         key=lambda kv: kv[1]["duration_s"], reverse=True)[:5]:
        avg = s["duration_s"] / s["count"] if s["count"] else 0
        rows.append([
            _esc(sub),
            _esc(str(s["count"])),
            _esc(f'{s["duration_s"]:.1f}'),
            _esc(f'{avg:.1f}'),
            _esc(f'{s["max_dur"]:.1f}'),
        ])
    return _section(
        "subagent-slow", "Slowest subagent types (v4+)",
        _table(
            [("Subagent", False), ("Calls", True),
             ("Total s", True), ("Avg s", True), ("Max s", True)],
            rows,
        ),
    )


def _render_subagent_cheap(agg: dict) -> str:
    if agg["cheap_rows"] == 0:
        return _section(
            "subagent-cheap", "Cheap subagent calls (v4+)",
            _empty("no v4 sessions yet"),
        )
    if agg["cheap_dispatches"] == 0:
        return _section(
            "subagent-cheap", "Cheap subagent calls (v4+)",
            _empty(f'no Agent calls across {agg["cheap_rows"]} v4 sessions'),
        )
    pct = agg["cheap_total"] / agg["cheap_dispatches"] * 100
    cls = "warn" if pct >= 25 else "good"
    callout = (
        f'<div class="callout {cls}">'
        f'<strong>{agg["cheap_total"]}</strong> of '
        f'<strong>{agg["cheap_dispatches"]}</strong> subagent dispatches '
        f'(<strong>{pct:.1f}%</strong>) returned &lt;200 chars — likely '
        f'solvable with a direct grep/Read at much lower overhead.</div>'
    )
    if not agg["cheap_offenders"]:
        return _section("subagent-cheap", "Cheap subagent calls (v4+)", callout)
    rows = []
    for a in agg["cheap_offenders"]:
        proj = _h._project_label(_h._project_key(a)) or "—"
        rows.append([
            f'<code>{_esc((a.get("session_id") or "")[:12])}</code>',
            _esc(str(a.get("cheap_subagent_calls") or 0)),
            _esc(_fmt_money(a.get("cost_usd") or 0)),
            _esc(proj),
        ])
    table = _table(
        [("Session", False), ("Cheap calls", True), ("Cost", True), ("Project", False)],
        rows,
    )
    return _section(
        "subagent-cheap", "Cheap subagent calls (v4+)", callout + table,
        note="High counts here suggest subagent overhead was wasted.",
    )


def _render_subagent_invocations(agg: dict) -> str:
    # Always emit the section so the static TOC anchor never dangles; when
    # v4 rows exist we render a placeholder pointing readers at the richer
    # surface/slow/cheap sections instead of duplicating the count.
    if agg["v4_rows"] > 0:
        return _section(
            "subagent-invocations", "Subagent invocations (v3+)",
            _empty("covered by the v4+ subagent sections above — "
                   "see return surface, slowest, and cheap calls"),
        )
    if agg["v3_sub_rows"] == 0:
        return _section(
            "subagent-invocations", "Subagent invocations (v3+)",
            _empty("no v3 sessions yet"),
        )
    if not agg["sub_totals"]:
        return _section(
            "subagent-invocations", "Subagent invocations (v3+)",
            _empty(f'no Agent calls in {agg["v3_sub_rows"]} v3 sessions'),
        )
    rows = [[_esc(s), _esc(str(n))]
            for s, n in sorted(agg["sub_totals"].items(),
                               key=lambda kv: kv[1], reverse=True)]
    return _section(
        "subagent-invocations", "Subagent invocations (v3+)",
        _table([("Subagent", False), ("Invocations", True)], rows),
        note=f'Aggregated over {agg["v3_sub_rows"]} v3 sessions.',
    )


def _render_tool_errors(agg: dict) -> str:
    if agg["v3_err_count"] == 0:
        return _section("tool-errors", "Tool errors (v3+)",
                        _empty("no v3 sessions yet"))
    if not agg["tool_errors"]:
        return _section(
            "tool-errors", "Tool errors (v3+)",
            _empty("no tool errors detected in v3 sessions"),
        )
    rows = []
    for a in agg["tool_errors"]:
        proj = _h._project_label(_h._project_key(a)) or "—"
        rows.append([
            f'<code>{_esc((a.get("session_id") or "")[:12])}</code>',
            _esc(str(a.get("tool_errors_count") or 0)),
            _esc(_fmt_money(a.get("cost_usd") or 0)),
            _esc(proj),
        ])
    table = _table(
        [("Session", False), ("Errors", True), ("Cost", True), ("Project", False)],
        rows,
    )
    summary = (
        f'<p class="foot-note">total: {agg["total_errors"]} tool errors across '
        f'{agg["v3_err_count"]} v3 sessions</p>'
    )
    return _section(
        "tool-errors", "Tool errors — top 10 sessions (v3+)",
        table + summary,
        note=(
            "Counts tool_result blocks where is_error=true: failing tools, "
            "retry loops, permission denials."
        ),
    )


def _render_correction_heavy(corr: list[dict]) -> str:
    if not corr:
        return _section(
            "correction-heavy", "Correction-heavy sessions (v3+)",
            _empty("no friction signals detected"),
        )
    rows = []
    for a in corr:
        proj = _h._project_label(_h._project_key(a)) or "—"
        rows.append([
            f'<code>{_esc((a.get("session_id") or "")[:12])}</code>',
            _esc(str(a.get("short_user_followups_count") or 0)),
            _esc(str(a.get("correction_keyword_hits") or 0)),
            _esc(_fmt_money(a.get("cost_usd") or 0)),
            _esc(proj),
        ])
    return _section(
        "correction-heavy", "Correction-heavy sessions (v3+)",
        _table(
            [
                ("Session", False),
                ("Short follow-ups", True),
                ("KW hits", True),
                ("Cost", True),
                ("Project", False),
            ],
            rows,
        ),
        note=(
            "Sorted by short_user_followups_count + correction_keyword_hits. "
            "These are the prime candidates for /retrospective."
        ),
    )


def _render_top_tools(tool_totals: dict) -> str:
    if not tool_totals:
        return _section("top-tools", "Top tools across all sessions",
                        _empty("no tool data"))
    items = sorted(tool_totals.items(), key=lambda kv: kv[1], reverse=True)[:15]
    max_v = items[0][1] if items else 1
    rows = []
    for t, n in items:
        bar = _bar_inline(n, max_v, width_px=160)
        rows.append([_esc(t), f'{bar}<span>{_fmt_int(n)}</span>'])
    return _section(
        "top-tools", "Top tools across all sessions",
        _table([("Tool", False), ("Total calls", True)], rows),
    )


def _render_recommendations(recs: list) -> str:
    """Wrap _recommend.render_html_inner() in the section chrome the rest
    of the report uses. Always emits the section so the static TOC anchor
    never dangles — the empty state is rendered inside the inner HTML."""
    return _section(
        "recommendations",
        "Recommendations",
        _reco.render_html_inner(recs),
        note=(
            "Threshold-based heuristics: each rule fires only when there is "
            "enough supporting evidence in the window. Absence of a "
            "recommendation means no signal, not failure."
        ),
    )


def _render_retro(agg: dict) -> str:
    by_outcome = agg["by_outcome"]
    by_model_corr = agg["by_model_corr"]
    tri = agg["trigger_accuracy"]
    if not (by_outcome or by_model_corr or tri):
        return _section(
            "retro", "Retro insights",
            _empty("insufficient retro data — run /retrospective at session end"),
        )
    blocks = []
    # Cost by outcome
    if by_outcome:
        rows = []
        for o, v in sorted(by_outcome.items()):
            rows.append([
                _esc(o),
                _esc(str(len(v))),
                _esc(_fmt_money(statistics.mean(v))),
                _esc(_fmt_money(sum(v))),
            ])
        blocks.append(
            '<h3>Cost by task_outcome</h3>'
            + _table(
                [("Outcome", False), ("Sessions", True),
                 ("Avg cost", True), ("Total", True)],
                rows,
            )
        )
    # Avg correction by model
    if by_model_corr:
        rows = []
        for m, v in sorted(by_model_corr.items()):
            rows.append([
                _esc(m),
                _esc(str(len(v))),
                _esc(f'{statistics.mean(v):.2f}'),
            ])
        blocks.append(
            '<h3>Avg <abbr title="0-10, captured by /retrospective">correction_rate</abbr> '
            'by model</h3>'
            + _table(
                [("Model", False), ("Sessions", True),
                 ("Avg correction (0–10)", True)],
                rows,
            )
        )
    # Trigger accuracy
    if tri:
        rows = [[_esc(k), _esc(str(v))] for k, v in sorted(tri.items())]
        blocks.append(
            '<h3>Skill trigger accuracy</h3>'
            + _table([("Accuracy", False), ("Count", True)], rows)
        )
    return _section("retro", "Retro insights",
                    "".join(blocks) if blocks else _empty("no retro data"))


def _render_footer(meta: dict) -> str:
    return (
        '<footer>'
        f'<p>Generated by claude-code-metrics v{GENERATOR_VERSION} '
        f'on {_esc(meta["generated_at"])}.</p>'
        f'<p>Source: <span class="cmd">{_esc(str(meta["source_path"]))}</span></p>'
        f'<p>Command: <span class="cmd">{_esc(meta["command"])}</span></p>'
        '<p>100% local. Nothing leaves your disk. Re-run with different '
        '<code>--since</code> / <code>--project</code> flags for other slices.</p>'
        '</footer>'
    )


def render_html(auto: list[dict], retro: list[dict], pricing: dict,
                meta: dict, all_time: dict) -> str:
    retro_by_id = {r["session_id"]: r for r in retro
                   if isinstance(r, dict) and r.get("session_id")}
    if auto:
        agg = _agg.aggregate(auto, retro_by_id, pricing)
    else:
        agg = _agg.aggregate([], {}, pricing)

    body_parts = [
        _render_hero(meta, agg, all_time),
    ]
    if not auto:
        # Always render the Recommendations section so the static TOC anchor
        # (`#recommendations` in SECTIONS_INDEX) never dangles. On an empty
        # window the inner empty-state explains there's nothing to recommend.
        body_parts.append(_render_recommendations([]))
        body_parts.append(
            _section(
                "no-data",
                "No sessions match the current filter",
                _empty(
                    'Try a wider window (e.g. --since all) or a different --project.'
                ),
            )
        )
    else:
        total_cost_in_window = sum(agg["costs"]) if agg["costs"] else 0.0
        recs = _reco.evaluate(agg, auto, min_evidence=3)
        body_parts.extend([
            _render_recommendations(recs),
            _render_trend(agg["by_week"]),
            _render_by_model(agg["by_model"]),
            _render_by_project(agg["by_proj_key"]),
            _render_cache_savings(agg),
            _render_cache_eff(agg["cache_by_model"]),
            _render_top_expensive(agg["top_expensive"], retro_by_id),
            _render_marathons(agg["marathons"], total_cost_in_window,
                              agg["marathon_total_cost"],
                              agg["marathon_total_count"]),
            _render_subagent_surface(agg["surface"], agg["v4_rows"]),
            _render_subagent_slow(agg["surface"], agg["v4_rows"]),
            _render_subagent_cheap(agg),
            _render_subagent_invocations(agg),
            _render_tool_errors(agg),
            _render_correction_heavy(agg["correction_heavy"]),
            _render_top_tools(agg["tool_totals"]),
            _render_retro(agg),
        ])
    body_parts.append(_render_footer(meta))

    return (
        '<!DOCTYPE html>'
        '<html lang="en">'
        '<head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="generator" '
        f'content="claude-code-metrics v{GENERATOR_VERSION}" />'
        '<title>claude-code-metrics report</title>'
        f'<style>{CSS}</style>'
        '</head>'
        '<body>'
        '<div class="layout">'
        + _render_toc()
        + '<main>'
        + "".join(body_parts)
        + '</main>'
        '</div>'
        '</body>'
        '</html>'
    )


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def _build_meta(args, n_sessions: int, err_count: int,
                retro_count: int, ts_range: tuple[str, str] | None,
                source_path: Path, schema_versions: list,
                auto_all: list[dict], log_path: Path,
                now: datetime | None = None) -> dict:
    # Distinguish "no filter" (sentinel "all") from "user explicitly passed
    # --project <something>". Otherwise `--project all` would conflate both
    # in the hero header even though the row filter is real.
    project_arg = (args.project or "").strip()
    project_filter_active = bool(project_arg)
    project_filter = project_arg if project_filter_active else "all"
    cmd_parts = ["python3 _render.py"]
    if args.since:
        cmd_parts.append(f"--since {args.since}")
    if project_arg:
        cmd_parts.append(f"--project {project_arg}")
    if args.output:
        cmd_parts.append(f"--output {args.output}")
    # Single source of "now" for the report. Production passes None →
    # real clock (fresh value at every call site, fine because the skew
    # is sub-millisecond). Tests pass a fixed datetime so generated_at,
    # the --since cutoff, and the health-signal helpers all agree.
    _now = now if now is not None else datetime.now(timezone.utc)
    return {
        "n_sessions": n_sessions,
        "err_count": err_count,
        "retro_count": retro_count,
        "ts_range": ts_range,
        "window_label": _h._window_label(args.since, default_days=30),
        "project_filter": project_filter,
        "project_filter_active": project_filter_active,
        "generated_at": _now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": " ".join(cmd_parts),
        "source_path": source_path,
        "schema_versions": schema_versions,
        "last_session_age": _h._humanize_age(
            _h._latest_session_ts(auto_all), now=now),
        "recent_hook_errors": _h._recent_log_errors(
            log_path, days=7, now=now),
    }


def main(argv: list[str] | None = None,
         now: datetime | None = None) -> int:
    """Render the HTML report.

    `now` is an optional test seam: production callers omit it and every
    `datetime.now(timezone.utc)` site reads the real clock. Tests pass a
    fixed datetime so time-windowed assertions (the --since cutoff, the
    `generated_at` stamp, the health-signal helpers) all resolve against
    the same reference. The same seam is already exposed by
    `_helpers._parse_since` / `_humanize_age` / `_recent_log_errors`.
    """
    global _h, _agg, _reco
    _h = _load_helpers()
    _agg = _load_aggregate()
    _reco = _load_recommend()

    parser = argparse.ArgumentParser(
        prog="metrics-report",
        description="Render a static HTML report from local metrics JSONL.",
    )
    parser.add_argument("--since", default=None,
                        help="Window: 7d / 30d / 90d / all / YYYY-MM-DD (default: 30d).")
    parser.add_argument("--project", default=None,
                        help="Exact-match project filter (label, origin, path, or basename).")
    parser.add_argument("--output", default=None,
                        help="Output path (default: $CLAUDE_HOME/metrics/report.html).")
    parser.add_argument("--metrics-dir", default=None,
                        help="Override the metrics directory (advanced).")
    args = parser.parse_args(argv)

    metrics_dir = Path(args.metrics_dir) if args.metrics_dir else (_claude_home() / "metrics")
    auto_path = metrics_dir / "auto.jsonl"
    retro_path = metrics_dir / "retro.jsonl"

    raw = _read_jsonl(auto_path)
    if not raw:
        print(f"metrics-report: no sessions found at {auto_path}", file=sys.stderr)
        print("Run a Claude Code session first; the SessionEnd hook appends rows automatically.",
              file=sys.stderr)
        return 1

    auto_all = [a for a in raw if not a.get("error")]
    err_rows = [a for a in raw if a.get("error")]

    cutoff = _h._parse_since(args.since, default_days=30, now=now)
    auto = list(auto_all)
    if cutoff is not None:
        auto = [
            a for a in auto
            if (ts := _h._row_ts(a)) is not None and ts >= cutoff
        ]
    proj = (args.project or "").strip() or None
    if proj:
        auto_all = [a for a in auto_all if _h._project_matches(a, proj)]
        auto = [a for a in auto if _h._project_matches(a, proj)]
        err_rows = [a for a in err_rows if _h._project_matches(a, proj)]
    err_count = len(err_rows)

    retro = _read_jsonl(retro_path)
    # O(N+M) membership: retro/auto can both run into the thousands; the
    # nested any() form was O(N*M) and slowed materially as files grew.
    retro_session_ids = {
        r.get("session_id") for r in retro if r.get("session_id")
    }
    retro_count = sum(
        1 for a in auto if a.get("session_id") in retro_session_ids
    )

    pricing = _load_pricing(metrics_dir)

    ts_sorted = sorted(a["ts"] for a in auto if a.get("ts"))
    ts_range = (ts_sorted[0], ts_sorted[-1]) if ts_sorted else None
    schema_versions = sorted({
        a.get("schema_version") for a in auto if a.get("schema_version") is not None
    })

    meta = _build_meta(args, len(auto), err_count, retro_count, ts_range,
                       auto_path, schema_versions,
                       auto_all, metrics_dir / "hook.log",
                       now=now)

    all_time_costs = [a.get("cost_usd") for a in auto_all
                      if a.get("cost_usd") is not None]
    all_time = {
        "total_sessions": len(auto_all),
        "total_cost": sum(all_time_costs) if all_time_costs else 0.0,
    }

    html_doc = render_html(auto, retro, pricing, meta, all_time)

    out_path = Path(args.output) if args.output else (metrics_dir / "report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(html_doc, encoding="utf-8")
    tmp_path.replace(out_path)

    print(f"✓ report written: {out_path.resolve()}")
    print(f"  open with: open '{out_path.resolve()}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
