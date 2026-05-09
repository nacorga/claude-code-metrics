---
name: metrics-report
description: Render a self-contained HTML report from ~/.claude/metrics/{auto,retro}.jsonl and write it to ~/.claude/metrics/report.html. Same window/project filters as /analyze-metrics. Read-only — appends nothing to the metrics files. Manual invocation.
disable-model-invocation: true
---

# /metrics-report

Generate a single static HTML file with the same insights as `/analyze-metrics`, organized for at-a-glance reading: hero KPIs, weekly cost chart (inline SVG), tables with proportional bars, callouts for the high-signal findings (cache savings, marathon spend share, cheap subagent calls). Zero JavaScript, zero CDN, zero network calls — opens directly via `file://`.

## Arguments

```
/metrics-report                                    # default: last 30 days, all projects
/metrics-report --since 7d                         # last 7 days
/metrics-report --since 90d                        # last 90 days
/metrics-report --since all                        # full history
/metrics-report --since 2026-01-01                 # explicit ISO date (UTC)
/metrics-report --project foo/bar                  # only sessions in that repo
/metrics-report --project github.com/foo/bar       # match by full normalized origin
/metrics-report --output ~/Desktop/report.html     # custom output path
/metrics-report --min-evidence 5                   # stricter recommendations (≥5 supporting sessions)
/metrics-report --min-evidence 1                   # surface even single-session signals
/metrics-report --since 90d --project foo/bar      # combined
```

`--since` and `--project` follow the exact same rules as `/analyze-metrics` (shared helpers): substring matches on `--project` are intentionally rejected, garbage `--since` falls back to the default 30-day window. `--min-evidence` controls the threshold for the embedded Recommendations section (default 3) and matches the flag exposed by `/recommend`.

## Steps

1. **Run the renderer.** It loads `auto.jsonl` + `retro.jsonl`, applies filters, aggregates, and writes a single self-contained HTML file:

   ```bash
   python3 ~/.claude/skills/metrics-report/_render.py ${ARGUMENTS:-}
   ```

2. **Print the renderer's stdout verbatim** (it tells the user the resolved output path and an `open` command). Add no commentary.

## Rules

- Read-only on the JSONL files. Writes only the HTML output.
- Default output: `~/.claude/metrics/report.html` (override with `--output`).
- The HTML file is fully self-contained: CSS inline, SVG inline, no `<script>`, no remote URLs. Safe to inspect with `view-source:` or `grep`.
- If `auto.jsonl` is empty, the script exits non-zero with a friendly message — do not retry, do not fabricate data.
- The header always shows the active window + project filter so the file is self-describing; re-run with different flags to get a different slice.
