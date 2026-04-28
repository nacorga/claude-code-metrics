---
name: analyze-metrics
description: Read ~/.claude/metrics/{auto,retro}.jsonl, left-join by session_id, and render a markdown report to stdout. Default window is the last 30 days; pass --since <window> and/or --project <name> to scope the report. Manual invocation.
disable-model-invocation: true
---

# /analyze-metrics

Summarise the local metrics JSONL files. Read-only — writes nothing to disk.

## Arguments

```
/analyze-metrics                                    # default: last 30 days, all projects
/analyze-metrics --since 7d                         # last 7 days
/analyze-metrics --since 90d                        # last 90 days
/analyze-metrics --since all                        # full history (legacy default)
/analyze-metrics --since 2026-01-01                 # explicit ISO date (UTC)
/analyze-metrics --project foo/bar                  # only sessions in that repo
/analyze-metrics --project github.com/foo/bar       # match by full normalized origin
/analyze-metrics --project foo --since 90d          # combined
```

`--project` matches at any granularity: full origin (`github.com/foo/bar`), label (`foo/bar`), git root path, cwd, or basename of either path. Substring matches are intentionally rejected to avoid silently merging unrelated repos.

## Steps

1. **Load data.** Read `~/.claude/metrics/auto.jsonl` and `~/.claude/metrics/retro.jsonl`. If `auto.jsonl` is missing or empty, stop and tell the user "no data yet — run a Claude Code session first". If `retro.jsonl` has `<3` rows, still render the auto-only sections and label the joined sections as "insufficient retro data".

2. **Run the report.** Use this exact script (deterministic output — no LLM summarisation of the numbers):

   ```bash
   CCM_FILTER_SINCE="$(echo "${ARGUMENTS:-}" | grep -oE '\-\-since[= ][^ ]+' | sed -E 's/^--since[= ]//' | tail -n1)" \
   CCM_FILTER_PROJECT="$(echo "${ARGUMENTS:-}" | grep -oE '\-\-project[= ][^ ]+' | sed -E 's/^--project[= ]//' | tail -n1)" \
   python3 - <<'PY'
   import json, os, statistics, sys
   from pathlib import Path

   # Helpers + aggregation logic live next to this SKILL when installed
   # (~/.claude/skills/analyze-metrics). The heredoc has no __file__, so
   # resolve via the canonical install path.
   _SKILL_DIR = Path.home() / ".claude" / "skills" / "analyze-metrics"
   sys.path.insert(0, str(_SKILL_DIR))
   from _helpers import (
       _parse_since, _window_label, _row_ts,
       _project_key, _project_label, _project_matches,
   )
   from _aggregate import aggregate

   m = Path.home() / ".claude" / "metrics"
   raw = [json.loads(l) for l in (m / "auto.jsonl").read_text().splitlines() if l.strip()]
   # Error rows (no_transcript, parse_crash, etc.) are counted but excluded
   # from aggregates — they'd skew averages with zero-cost zero-turn noise.
   auto_all = [a for a in raw if not a.get("error")]
   err_count = len(raw) - len(auto_all)

   since_raw = (os.environ.get("CCM_FILTER_SINCE") or "").strip() or None
   proj_raw = (os.environ.get("CCM_FILTER_PROJECT") or "").strip() or None
   cutoff = _parse_since(since_raw, default_days=30)

   auto = list(auto_all)
   if cutoff is not None:
       # Walrus avoids re-parsing the ISO timestamp twice per row.
       auto = [a for a in auto if (ts := _row_ts(a)) is not None and ts >= cutoff]
   if proj_raw:
       auto = [a for a in auto if _project_matches(a, proj_raw)]

   retro_path = m / "retro.jsonl"
   retro = []
   if retro_path.is_file():
       retro = [json.loads(l) for l in retro_path.read_text().splitlines() if l.strip()]
   retro_by_id = {r["session_id"]: r for r in retro}

   pricing_path = m / "pricing.json"
   pricing = {}
   if pricing_path.is_file():
       try:
           pricing = json.loads(pricing_path.read_text())
       except Exception:
           pricing = {}

   agg = aggregate(auto, retro_by_id, pricing)

   def table(headers, rows):
       out = ["| " + " | ".join(headers) + " |",
              "| " + " | ".join(["---"] * len(headers)) + " |"]
       for r in rows:
           out.append("| " + " | ".join(str(c) for c in r) + " |")
       return "\n".join(out)

   print("# claude-code-metrics report\n")
   print(f"**Window:** {_window_label(since_raw, default_days=30)}  ·  **Project filter:** {proj_raw or 'all'}")
   all_costs = [a.get("cost_usd") for a in auto_all if a.get("cost_usd") is not None]
   print(f"_All-time: {len(auto_all)} sessions, ${sum(all_costs):.4f} total cost_\n")

   if not auto:
       print("_no sessions match the current filter — try `--since all` or a different `--project`._")
       sys.exit(0)

   print(f"- sessions in window: **{len(auto)}**")
   if err_count:
       print(f"- error rows excluded: **{err_count}** (no transcript / parse crash)")
   costs = agg["costs"]
   print(f"- total estimated cost in window: **${sum(costs):.4f}**")
   print(f"- sessions with retro: **{len(retro)} / {len(auto)}**")
   ts = sorted(a["ts"] for a in auto if a.get("ts"))
   if ts:
       print(f"- range: {ts[0]} → {ts[-1]}")
   print()

   # Cost by model
   print("## Cost by model\n")
   by_model = agg["by_model"]
   rows = [(m_, len(v), f"${statistics.mean(v):.4f}", f"${sum(v):.4f}")
           for m_, v in sorted(by_model.items())]
   print(table(["model", "sessions", "avg cost", "total"], rows) if rows else "_no cost data_")
   print()

   # Cost by outcome (needs retro)
   print("## Cost by task_outcome\n")
   by_outcome = agg["by_outcome"]
   if by_outcome:
       rows = [(o, len(v), f"${statistics.mean(v):.4f}", f"${sum(v):.4f}")
               for o, v in sorted(by_outcome.items())]
       print(table(["outcome", "sessions", "avg cost", "total"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Correction rate by model (needs retro)
   print("## Avg correction_rate by model\n")
   by_model_corr = agg["by_model_corr"]
   if by_model_corr:
       rows = [(m_, len(v), f"{statistics.mean(v):.2f}")
               for m_, v in sorted(by_model_corr.items())]
       print(table(["model", "sessions", "avg correction (0-10)"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Skill trigger accuracy distribution
   print("## Skill trigger accuracy\n")
   tri = agg["trigger_accuracy"]
   if tri:
       rows = [(k, v) for k, v in sorted(tri.items())]
       print(table(["accuracy", "count"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Top 10 most expensive sessions
   print("## Top 10 most expensive sessions\n")
   ranked = agg["top_expensive"]
   if ranked:
       rows = []
       for a in ranked:
           proj_label = _project_label(_project_key(a)) or "-"
           outcome = retro_by_id.get(a["session_id"], {}).get("task_outcome", "-")
           rows.append((a["session_id"][:12], f"${a['cost_usd']:.4f}", outcome, proj_label))
       print(table(["session_id", "cost", "outcome", "project"], rows))
   else:
       print("_no cost data_")
   print()

   # Cost by project. Grouped by canonical project key (origin > git_root >
   # cwd) so two repos with the same basename never collide. The displayed
   # label is the human-friendly form; if two distinct keys collapse to the
   # same label, disambiguate with a short suffix from the key.
   print("## Cost by project\n")
   by_proj_key = agg["by_proj_key"]
   if by_proj_key:
       from _aggregate import _disambiguated_label_factory
       label = _disambiguated_label_factory(by_proj_key)
       rows = sorted(
           ((label(k), len(v),
             f"${statistics.mean(v):.4f}", f"${sum(v):.4f}")
            for k, v in by_proj_key.items()),
           key=lambda r: float(r[3].lstrip("$")), reverse=True
       )
       print(table(["project", "sessions", "avg cost", "total"], rows))
   else:
       print("_no cost data_")
   print()

   # Cache efficiency by model — read/(read+create); low ratio means context is being rebuilt repeatedly
   print("## Cache efficiency by model\n")
   cache_by_model = agg["cache_by_model"]
   rows = []
   for m_, (r, c) in sorted(cache_by_model.items()):
       if r + c == 0:
           rows.append((m_, "-", f"{r:,}", f"{c:,}"))
       else:
           pct = r / (r + c) * 100
           rows.append((m_, f"{pct:.1f}%", f"{r:,}", f"{c:,}"))
   print(table(["model", "hit rate", "cache_read", "cache_create"], rows) if rows else "_no data_")
   print()

   # Cache savings — how much cheaper each model got via cache_read vs full input rate.
   # Counterfactual: if cache_read tokens had been billed as input, the bill would be higher
   # by (input_rate - cache_read_rate) per cache_read token. Sums to a per-model and total figure.
   print("## Cache savings (counterfactual: no cache)\n")
   savings_by_model = agg["savings_by_model"]
   if savings_by_model:
       rows = sorted(
           ((m_, f"{cr:,}", f"${s:.2f}") for m_, (s, cr) in savings_by_model.items()),
           key=lambda r: float(r[2].lstrip("$")), reverse=True,
       )
       print(table(["model", "cache_read tokens", "saved vs no-cache"], rows))
       print(f"\n_total saved by cache: **${agg['total_saved']:.2f}**_")
       if agg["total_actual"] > 0:
           ratio = agg["total_counterfactual"] / agg["total_actual"]
           print(f"_actual estimated cost ${agg['total_actual']:.2f} vs no-cache counterfactual ${agg['total_counterfactual']:.2f} ({ratio:.1f}× cheaper with cache)_")
   else:
       print("_no cache_read tokens captured, or pricing.json unavailable_")
   print()

   # Marathon sessions — turn_count > 300 indicates autonomous runs that drift expensive
   print("## Marathon sessions (turn_count > 300)\n")
   marathons = agg["marathons"]
   if marathons:
       rows = []
       for a in marathons:
           proj_label = _project_label(_project_key(a)) or "-"
           cost = a.get("cost_usd") or 0
           tools = a.get("tool_calls_total") or 0
           rows.append((a["session_id"][:12], a.get("turn_count"), tools, f"${cost:.2f}", proj_label))
       print(table(["session_id", "turns", "tool calls", "cost", "project"], rows))
       print()
       n = agg["marathon_total_count"]
       total_cost = agg["marathon_total_cost"]
       all_cost = sum(costs)
       pct = total_cost / all_cost * 100 if all_cost else 0
       extra = f" (showing top {len(marathons)})" if n > len(marathons) else ""
       print(f"_{n} marathon sessions account for ${total_cost:.2f} ({pct:.1f}% of total spend){extra}._")
   else:
       print("_no marathon sessions detected_")
   print()

   # Top tools across ALL sessions — aggregated tool_distribution
   print("## Top tools across all sessions\n")
   tool_totals = agg["tool_totals"]
   if tool_totals:
       rows = sorted(tool_totals.items(), key=lambda kv: kv[1], reverse=True)[:15]
       print(table(["tool", "total calls"], rows))
   else:
       print("_no tool data_")
   print()

   # Subagent usage (v3+) — only available on rows captured by the v3 hook
   print("## Subagent invocations (v3+ rows only)\n")
   sub_totals = agg["sub_totals"]
   v3_rows = agg["v3_sub_rows"]
   if v3_rows == 0:
       print("_no v3 sessions yet — schema bump captures this going forward_")
   elif sub_totals:
       rows = sorted(sub_totals.items(), key=lambda kv: kv[1], reverse=True)
       print(table(["subagent", "invocations"], rows))
       print(f"\n_aggregated over {v3_rows} v3 sessions_")
   else:
       print(f"_no Agent calls in {v3_rows} v3 sessions_")
   print()

   # Subagent return surface (v4+) — what each subagent type dumps back into the main context.
   # Tokens cannot be attributed per subagent (transcript records only main-agent usage),
   # so we measure the return surface: chars, duration, errors. This is what actually
   # bloats the parent's context window and drives cost/latency.
   print("## Subagent return surface (v4+ rows only)\n")
   surface = agg["surface"]
   v4_rows = agg["v4_rows"]
   if v4_rows == 0:
       print("_no v4 sessions yet — schema bump captures this going forward_")
   elif surface:
       total_chars = sum(s["return_chars"] for s in surface.values()) or 1
       rows = []
       for sub, s in sorted(surface.items(), key=lambda kv: kv[1]["return_chars"], reverse=True):
           avg_chars = s["return_chars"] // s["count"] if s["count"] else 0
           pct = s["return_chars"] / total_chars * 100
           rows.append((
               sub,
               s["count"],
               f"{s['return_chars']:,}",
               f"{avg_chars:,}",
               f"{s['duration_s']:.1f}",
               s["errors"],
               f"{pct:.1f}%",
           ))
       print(table(["subagent", "calls", "chars total", "avg chars", "dur s", "errors", "% of return"], rows))
       print(f"\n_aggregated over {v4_rows} v4 sessions_")
   else:
       print(f"_no Agent calls in {v4_rows} v4 sessions_")
   print()

   # Slowest subagents (v4+) — by total wall-clock and by single-call max.
   print("## Slowest subagent types (v4+ rows only)\n")
   if v4_rows == 0:
       print("_no v4 sessions yet_")
   elif surface:
       rows = []
       for sub, s in sorted(surface.items(), key=lambda kv: kv[1]["duration_s"], reverse=True)[:5]:
           avg_dur = s["duration_s"] / s["count"] if s["count"] else 0
           rows.append((
               sub,
               s["count"],
               f"{s['duration_s']:.1f}",
               f"{avg_dur:.1f}",
               f"{s['max_dur']:.1f}",
           ))
       print(table(["subagent", "calls", "total s", "avg s", "max s"], rows))
   else:
       print("_no subagent activity in v4 sessions_")
   print()

   # Cheap subagent calls (v4+) — dispatches whose tool_result was <200 chars.
   # High counts suggest the subagent overhead was wasted; a direct grep/Read
   # would have produced the same answer cheaper and faster.
   print("## Cheap subagent calls (v4+ rows only)\n")
   cheap_rows = agg["cheap_rows"]
   if cheap_rows == 0:
       print("_no v4 sessions yet_")
   else:
       cheap_total = agg["cheap_total"]
       sub_total = agg["cheap_dispatches"]
       if sub_total == 0:
           print(f"_no Agent calls across {cheap_rows} v4 sessions_")
       else:
           pct = cheap_total / sub_total * 100
           print(f"**{cheap_total} of {sub_total} subagent dispatches ({pct:.1f}%) returned <200 chars** — likely solvable with a direct grep/Read.\n")
           offenders = agg["cheap_offenders"]
           if offenders:
               rows = []
               for a in offenders:
                   proj_label = _project_label(_project_key(a)) or "-"
                   cost = a.get("cost_usd") or 0
                   rows.append((
                       a["session_id"][:12],
                       a.get("cheap_subagent_calls") or 0,
                       f"${cost:.2f}",
                       proj_label,
                   ))
               print(table(["session_id", "cheap calls", "cost", "project"], rows))
   print()

   # Tool errors aggregated (v3+) — uses the captured field, no transcript reads
   print("## Tool errors — top 10 sessions (v3+ rows)\n")
   tool_errors = agg["tool_errors"]
   v3_err_count = agg["v3_err_count"]
   if tool_errors:
       rows = []
       for a in tool_errors:
           proj_label = _project_label(_project_key(a)) or "-"
           cost = a.get("cost_usd") or 0
           rows.append((a["session_id"][:12], a.get("tool_errors_count"), f"${cost:.2f}", proj_label))
       print(table(["session_id", "errors", "cost", "project"], rows))
       print(f"\n_total: {agg['total_errors']} tool errors across {v3_err_count} v3 sessions_")
   elif v3_err_count > 0:
       print("_no tool errors detected in v3 sessions_")
   else:
       print("_no v3 sessions yet_")
   print()

   # Correction-heavy sessions (v3+) — high-signal candidates for retro
   print("## Correction-heavy sessions (v3+, by short_followups + correction_keywords)\n")
   v3_corr = agg["correction_heavy"]
   any_v3_corr_field = any("short_user_followups_count" in a for a in auto)
   if v3_corr:
       rows = []
       for a in v3_corr:
           proj_label = _project_label(_project_key(a)) or "-"
           cost = a.get("cost_usd") or 0
           rows.append((
               a["session_id"][:12],
               a.get("short_user_followups_count") or 0,
               a.get("correction_keyword_hits") or 0,
               f"${cost:.2f}",
               proj_label,
           ))
       print(table(["session_id", "short_fups", "kw_hits", "cost", "project"], rows))
   elif any_v3_corr_field:
       print("_no friction signals detected in v3 sessions_")
   else:
       print("_no v3 sessions yet_")
   print()

   # Cost trend by ISO week — measures impact of setup changes over time
   print("## Cost trend by ISO week\n")
   by_week = agg["by_week"]
   if by_week:
       rows = [(k, n, f"${c:.2f}", f"${c/n:.2f}") for k, (c, n) in sorted(by_week.items())]
       print(table(["week", "sessions", "total", "avg/session"], rows))
   else:
       print("_no dated sessions_")
   PY
   ```

3. **Print the script output verbatim.** Do not reinterpret the numbers. Add no commentary beyond a one-line preamble if useful.

## Rules

- Read-only. Never write to disk.
- If both files are empty, say so and stop.
- Dates are UTC (ISO8601) — show as-is; do not localise.
