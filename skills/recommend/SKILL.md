---
name: recommend
description: Read ~/.claude/metrics/{auto,retro}.jsonl and surface actionable recommendations — agents to refine, projects that deserve a CLAUDE.md, model-overspend patterns, friction hotspots — based on threshold rules calibrated from real session distributions. Default window is the last 30 days; pass --since <window>, --project <name>, or --min-evidence <N> to scope. Manual invocation.
disable-model-invocation: true
---

# /recommend

Surface prescriptive next steps from the local metrics JSONL. Read-only. Each rule fires only when there is enough evidence (default >=3 supporting sessions) so absent recommendations always mean "no signal," never "rule broken."

## Arguments

```
/recommend                                    # default: last 30 days, all projects, min-evidence=3
/recommend --since 7d                         # last 7 days
/recommend --since 90d                        # last 90 days
/recommend --since all                        # full history
/recommend --since 2026-01-01                 # explicit ISO date (UTC)
/recommend --project foo/bar                  # only sessions in that repo
/recommend --min-evidence 5                   # require >=5 supporting sessions per rule (stricter)
/recommend --min-evidence 1                   # surface even single-session signals (looser)
/recommend --project foo --since 90d --min-evidence 5
```

`--project` matches at any granularity: full origin (`github.com/foo/bar`), label (`foo/bar`), git root path, cwd, or basename. Substring matches are intentionally rejected.

## Steps

1. **Load data.** Read `~/.claude/metrics/auto.jsonl` and `~/.claude/metrics/retro.jsonl`. If `auto.jsonl` is missing or empty, stop and tell the user "no data yet — run a Claude Code session first."

2. **Run the recommender.** Use this exact script (deterministic output — no LLM summarisation of the recommendations):

   ```bash
   CCM_FILTER_SINCE="$(echo "${ARGUMENTS:-}" | grep -oE '\-\-since[= ][^ ]+' | sed -E 's/^--since[= ]//' | tail -n1)" \
   CCM_FILTER_PROJECT="$(echo "${ARGUMENTS:-}" | grep -oE '\-\-project[= ][^ ]+' | sed -E 's/^--project[= ]//' | tail -n1)" \
   CCM_MIN_EVIDENCE="$(echo "${ARGUMENTS:-}" | grep -oE '\-\-min-evidence[= ][0-9]+' | sed -E 's/^--min-evidence[= ]//' | tail -n1)" \
   python3 - <<'PY'
   import json, os, sys
   from pathlib import Path

   # _recommend lives next to this SKILL when installed; _helpers and
   # _aggregate live in the analyze-metrics skill (single source of truth
   # for filtering and aggregation).
   _RECO_DIR = Path.home() / ".claude" / "skills" / "recommend"
   _SHARED_DIR = Path.home() / ".claude" / "skills" / "analyze-metrics"
   sys.path.insert(0, str(_SHARED_DIR))
   sys.path.insert(0, str(_RECO_DIR))
   from _helpers import (  # noqa: E402
       _parse_since, _window_label, _row_ts,
       _project_matches, _project_label, _project_key,
       _latest_session_ts, _humanize_age, _recent_log_errors,
   )
   from _aggregate import aggregate  # noqa: E402
   from _recommend import evaluate, render_markdown  # noqa: E402

   m = Path.home() / ".claude" / "metrics"
   auto_path = m / "auto.jsonl"
   if not auto_path.is_file():
       print("no data yet — run a Claude Code session first")
       sys.exit(0)
   raw = [json.loads(l) for l in auto_path.read_text().splitlines() if l.strip()]
   auto_all = [a for a in raw if not a.get("error")]
   if not auto_all:
       print("no usable sessions yet (only error rows). Run more Claude Code sessions and retry.")
       sys.exit(0)

   since_raw = (os.environ.get("CCM_FILTER_SINCE") or "").strip() or None
   proj_raw = (os.environ.get("CCM_FILTER_PROJECT") or "").strip() or None
   min_ev_raw = (os.environ.get("CCM_MIN_EVIDENCE") or "").strip()
   try:
       min_evidence = max(1, int(min_ev_raw)) if min_ev_raw else 3
   except ValueError:
       min_evidence = 3

   cutoff = _parse_since(since_raw, default_days=30)
   auto = list(auto_all)
   if cutoff is not None:
       auto = [a for a in auto if (ts := _row_ts(a)) is not None and ts >= cutoff]
   if proj_raw:
       auto = [a for a in auto if _project_matches(a, proj_raw)]

   retro_path = m / "retro.jsonl"
   retro = []
   if retro_path.is_file():
       retro = [json.loads(l) for l in retro_path.read_text().splitlines() if l.strip()]
   retro_by_id = {r["session_id"]: r for r in retro if isinstance(r, dict) and r.get("session_id")}

   pricing_path = m / "pricing.json"
   pricing = {}
   if pricing_path.is_file():
       try:
           pricing = json.loads(pricing_path.read_text())
       except Exception:
           pricing = {}

   agg = aggregate(auto, retro_by_id, pricing)
   recs = evaluate(agg, auto, min_evidence=min_evidence)

   print("# /recommend\n")
   print(
       f"**Window:** {_window_label(since_raw, default_days=30)}  ·  "
       f"**Project:** {proj_raw or 'all'}  ·  "
       f"**Min evidence:** {min_evidence}  ·  "
       f"**Sessions in window:** {len(auto)}"
   )
   _last_age = _humanize_age(_latest_session_ts(auto_all))
   _n_err = _recent_log_errors(m / "hook.log", days=7)
   print(f"_Last logged session: {_last_age}  ·  hook errors (7d): {_n_err}_\n")
   if _n_err > 0:
       print(f"> WARNING: {_n_err} hook errors in the last 7 days — check `~/.claude/metrics/hook.log`\n")
   if not auto:
       print("_no sessions match the current filter — try `--since all` or a different `--project`._")
       sys.exit(0)
   print(render_markdown(recs))
   PY
   ```

3. **Print the script output verbatim.** Do not reinterpret or summarise the recommendations.

## Rules

- Read-only. Never write to disk.
- If the auto file is empty, say so and stop.
- Recommendations are heuristics, not prescriptions. Each fires only with >= `--min-evidence` supporting sessions, but the user is the final judge of whether a given recommendation applies in their context.
- Dates are UTC (ISO8601) — show as-is; do not localise.
