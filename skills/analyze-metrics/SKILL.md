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
   PY
   ```

3. **Print the script output verbatim.** Do not reinterpret the numbers. Add no commentary beyond a one-line preamble if useful.

## Rules

- Read-only. Never write to disk.
- If both files are empty, say so and stop.
- Dates are UTC (ISO8601) — show as-is; do not localise.
