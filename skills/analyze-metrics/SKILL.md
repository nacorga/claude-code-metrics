---
name: analyze-metrics
description: Read ~/.claude/metrics/{auto,retro}.jsonl, left-join by session_id, and render a markdown report to stdout with totals, cost breakdowns, and correlation tables. Manual invocation.
disable-model-invocation: true
---

# /analyze-metrics

Summarise the local metrics JSONL files. Read-only — writes nothing to disk.

## Steps

1. **Load data.** Read `~/.claude/metrics/auto.jsonl` and `~/.claude/metrics/retro.jsonl`. If `auto.jsonl` is missing or empty, stop and tell the user "no data yet — run a Claude Code session first". If `retro.jsonl` has `<3` rows, still render the auto-only sections and label the joined sections as "insufficient retro data".

2. **Run the report.** Use this exact script (deterministic output — no LLM summarisation of the numbers):

   ```bash
   python3 - <<'PY'
   import json, os, statistics
   from collections import Counter, defaultdict
   from pathlib import Path

   m = Path.home() / ".claude" / "metrics"
   raw = [json.loads(l) for l in (m / "auto.jsonl").read_text().splitlines() if l.strip()]
   # Error rows (no_transcript, parse_crash, etc.) are counted but excluded
   # from aggregates — they'd skew averages with zero-cost zero-turn noise.
   auto = [a for a in raw if not a.get("error")]
   err_count = len(raw) - len(auto)

   retro_path = m / "retro.jsonl"
   retro = []
   if retro_path.is_file():
       retro = [json.loads(l) for l in retro_path.read_text().splitlines() if l.strip()]
   retro_by_id = {r["session_id"]: r for r in retro}

   joined = [{**a, **retro_by_id.get(a["session_id"], {})} for a in auto]

   def table(headers, rows):
       out = ["| " + " | ".join(headers) + " |",
              "| " + " | ".join(["---"] * len(headers)) + " |"]
       for r in rows:
           out.append("| " + " | ".join(str(c) for c in r) + " |")
       return "\n".join(out)

   print("# claude-code-metrics report\n")
   print(f"- sessions (real): **{len(auto)}**")
   if err_count:
       print(f"- error rows excluded: **{err_count}** (no transcript / parse crash)")
   costs = [a["cost_usd"] for a in auto if a.get("cost_usd") is not None]
   print(f"- total estimated cost: **${sum(costs):.4f}**")
   print(f"- sessions with retro: **{len(retro)} / {len(auto)}**")
   ts = sorted(a["ts"] for a in auto if a.get("ts"))
   if ts:
       print(f"- range: {ts[0]} → {ts[-1]}")
   print()

   # Cost by model
   by_model = defaultdict(list)
   for a in auto:
       if a.get("cost_usd") is not None:
           by_model[a.get("model") or "unknown"].append(a["cost_usd"])
   print("## Cost by model\n")
   rows = [(m, len(v), f"${statistics.mean(v):.4f}", f"${sum(v):.4f}") for m, v in sorted(by_model.items())]
   print(table(["model", "sessions", "avg cost", "total"], rows) if rows else "_no cost data_")
   print()

   # Cost by outcome (needs retro)
   by_outcome = defaultdict(list)
   for j in joined:
       if j.get("task_outcome") and j.get("cost_usd") is not None:
           by_outcome[j["task_outcome"]].append(j["cost_usd"])
   print("## Cost by task_outcome\n")
   if by_outcome:
       rows = [(o, len(v), f"${statistics.mean(v):.4f}", f"${sum(v):.4f}") for o, v in sorted(by_outcome.items())]
       print(table(["outcome", "sessions", "avg cost", "total"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Correction rate by model (needs retro)
   by_model_corr = defaultdict(list)
   for j in joined:
       if j.get("correction_rate") is not None and j.get("model"):
           by_model_corr[j["model"]].append(j["correction_rate"])
   print("## Avg correction_rate by model\n")
   if by_model_corr:
       rows = [(m, len(v), f"{statistics.mean(v):.2f}") for m, v in sorted(by_model_corr.items())]
       print(table(["model", "sessions", "avg correction (0-10)"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Skill trigger accuracy distribution
   tri = Counter(j.get("skill_trigger_accuracy") for j in joined if j.get("skill_trigger_accuracy"))
   print("## Skill trigger accuracy\n")
   if tri:
       rows = [(k, v) for k, v in sorted(tri.items())]
       print(table(["accuracy", "count"], rows))
   else:
       print("_insufficient retro data_")
   print()

   # Top 10 most expensive sessions
   print("## Top 10 most expensive sessions\n")
   ranked = sorted((a for a in auto if a.get("cost_usd") is not None),
                   key=lambda x: x["cost_usd"], reverse=True)[:10]
   if ranked:
       rows = []
       for a in ranked:
           cwd = (a.get("cwd") or "").split("/")[-1] or "-"
           outcome = retro_by_id.get(a["session_id"], {}).get("task_outcome", "-")
           rows.append((a["session_id"][:12], f"${a['cost_usd']:.4f}", outcome, cwd))
       print(table(["session_id", "cost", "outcome", "project"], rows))
   else:
       print("_no cost data_")
   print()

   # Cost by project (cwd basename)
   by_proj = defaultdict(list)
   for a in auto:
       if a.get("cost_usd") is not None:
           proj = (a.get("cwd") or "unknown").split("/")[-1] or "unknown"
           by_proj[proj].append(a["cost_usd"])
   print("## Cost by project\n")
   if by_proj:
       rows = sorted(
           ((p, len(v), f"${statistics.mean(v):.4f}", f"${sum(v):.4f}") for p, v in by_proj.items()),
           key=lambda r: float(r[3].lstrip("$")), reverse=True
       )
       print(table(["project", "sessions", "avg cost", "total"], rows))
   else:
       print("_no cost data_")
   print()

   # Cache efficiency by model — read/(read+create); low ratio means context is being rebuilt repeatedly
   print("## Cache efficiency by model\n")
   cache_by_model = defaultdict(lambda: [0, 0])  # [read, create]
   for a in auto:
       m_ = a.get("model") or "unknown"
       cache_by_model[m_][0] += a.get("cache_read_tokens") or 0
       cache_by_model[m_][1] += a.get("cache_creation_tokens") or 0
   rows = []
   for m_, (r, c) in sorted(cache_by_model.items()):
       if r + c == 0:
           rows.append((m_, "-", f"{r:,}", f"{c:,}"))
       else:
           pct = r / (r + c) * 100
           rows.append((m_, f"{pct:.1f}%", f"{r:,}", f"{c:,}"))
   print(table(["model", "hit rate", "cache_read", "cache_create"], rows) if rows else "_no data_")
   print()

   # Marathon sessions — turn_count > 300 indicates autonomous runs that drift expensive
   print("## Marathon sessions (turn_count > 300)\n")
   marathons = sorted(
       (a for a in auto if (a.get("turn_count") or 0) > 300),
       key=lambda x: x.get("cost_usd") or 0, reverse=True
   )[:10]
   if marathons:
       rows = []
       for a in marathons:
           cwd = (a.get("cwd") or "").split("/")[-1] or "-"
           cost = a.get("cost_usd") or 0
           tools = a.get("tool_calls_total") or 0
           rows.append((a["session_id"][:12], a.get("turn_count"), tools, f"${cost:.2f}", cwd))
       print(table(["session_id", "turns", "tool calls", "cost", "project"], rows))
       print()
       n = len(marathons)
       total_cost = sum(a.get("cost_usd") or 0 for a in auto if (a.get("turn_count") or 0) > 300)
       all_cost = sum(costs)
       pct = total_cost / all_cost * 100 if all_cost else 0
       print(f"_{n} marathon sessions account for ${total_cost:.2f} ({pct:.1f}% of total spend)._")
   else:
       print("_no marathon sessions detected_")
   print()

   # Top tools across ALL sessions — aggregated tool_distribution
   print("## Top tools across all sessions\n")
   tool_totals = Counter()
   for a in auto:
       td = a.get("tool_distribution") or {}
       for t, c in td.items():
           tool_totals[t] += c
   if tool_totals:
       rows = [(t, n) for t, n in tool_totals.most_common(15)]
       print(table(["tool", "total calls"], rows))
   else:
       print("_no tool data_")
   print()

   # Subagent usage (v3+) — only available on rows captured by the v3 hook
   print("## Subagent invocations (v3+ rows only)\n")
   sub_totals = Counter()
   v3_rows = 0
   for a in auto:
       if "subagent_invocations" in a:
           v3_rows += 1
           for s, c in (a.get("subagent_invocations") or {}).items():
               sub_totals[s] += c
   if v3_rows == 0:
       print("_no v3 sessions yet — schema bump captures this going forward_")
   elif sub_totals:
       rows = [(s, n) for s, n in sub_totals.most_common()]
       print(table(["subagent", "invocations"], rows))
       print(f"\n_aggregated over {v3_rows} v3 sessions_")
   else:
       print(f"_no Agent calls in {v3_rows} v3 sessions_")
   print()

   # Tool errors aggregated (v3+) — uses the captured field, no transcript reads
   print("## Tool errors — top 10 sessions (v3+ rows)\n")
   v3_with_errors = [a for a in auto if a.get("tool_errors_count") is not None and a.get("tool_errors_count") > 0]
   v3_with_errors.sort(key=lambda x: x.get("tool_errors_count") or 0, reverse=True)
   if v3_with_errors:
       rows = []
       for a in v3_with_errors[:10]:
           cwd = (a.get("cwd") or "").split("/")[-1] or "-"
           cost = a.get("cost_usd") or 0
           rows.append((a["session_id"][:12], a.get("tool_errors_count"), f"${cost:.2f}", cwd))
       print(table(["session_id", "errors", "cost", "project"], rows))
       total_err = sum(a.get("tool_errors_count") or 0 for a in auto)
       v3_count = sum(1 for a in auto if "tool_errors_count" in a)
       print(f"\n_total: {total_err} tool errors across {v3_count} v3 sessions_")
   elif any("tool_errors_count" in a for a in auto):
       print("_no tool errors detected in v3 sessions_")
   else:
       print("_no v3 sessions yet_")
   print()

   # Correction-heavy sessions (v3+) — high-signal candidates for retro
   print("## Correction-heavy sessions (v3+, by short_followups + correction_keywords)\n")
   v3_corr = [a for a in auto
              if "short_user_followups_count" in a
              and ((a.get("short_user_followups_count") or 0) + (a.get("correction_keyword_hits") or 0)) > 0]
   v3_corr.sort(
       key=lambda x: (x.get("short_user_followups_count") or 0) + (x.get("correction_keyword_hits") or 0),
       reverse=True,
   )
   if v3_corr:
       rows = []
       for a in v3_corr[:10]:
           cwd = (a.get("cwd") or "").split("/")[-1] or "-"
           cost = a.get("cost_usd") or 0
           rows.append((
               a["session_id"][:12],
               a.get("short_user_followups_count") or 0,
               a.get("correction_keyword_hits") or 0,
               f"${cost:.2f}",
               cwd,
           ))
       print(table(["session_id", "short_fups", "kw_hits", "cost", "project"], rows))
   elif any("short_user_followups_count" in a for a in auto):
       print("_no friction signals detected in v3 sessions_")
   else:
       print("_no v3 sessions yet_")
   print()

   # Cost trend by ISO week — measures impact of setup changes over time
   print("## Cost trend by ISO week\n")
   from datetime import datetime as _dt
   by_week = defaultdict(lambda: [0.0, 0])  # [cost, sessions]
   for a in auto:
       ts = a.get("ts")
       if not ts or a.get("cost_usd") is None:
           continue
       try:
           dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
       except Exception:
           continue
       y, w, _ = dt.isocalendar()
       key = f"{y}-W{w:02d}"
       by_week[key][0] += a["cost_usd"]
       by_week[key][1] += 1
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
