"""Shared aggregation logic for /metrics-report and /analyze-metrics.

Pure: no I/O, no argparse, no filesystem. Both skills load JSONL rows,
apply filters via _helpers, then call aggregate() to get every cut both
reports need. Single source of truth — keeps the markdown and HTML
formats in lockstep on every number they show.

The dict shape returned by aggregate() is the contract. tests/test_aggregate.py
pins it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable

from _helpers import _project_key, _project_label  # type: ignore


def _rates_for(model_id: str, pricing: dict) -> tuple[float, float] | None:
    """Look up (input_rate, cache_read_rate) for a model id.

    Uses the same prefix-matching rule as the hook's pricing lookup:
    longest matching prefix wins, falls through to `_default`. Returns
    None when the model is unknown or rates are incomplete — callers
    skip the row instead of guessing."""
    if not model_id or not pricing:
        return None
    keys = [k for k in pricing if not k.startswith("_") and model_id.startswith(k)]
    keys.sort(key=len, reverse=True)
    entry = pricing.get(keys[0]) if keys else pricing.get("_default")
    if not entry:
        return None
    rate_in = entry.get("input")
    rate_cr = entry.get("cache_read")
    if rate_in is None or rate_cr is None:
        return None
    return float(rate_in), float(rate_cr)


def _disambiguated_label_factory(by_proj_key: dict) -> Callable[[str], str]:
    """Build a labeler that disambiguates colliding human labels with a
    short suffix. Two distinct project keys that collapse to the same
    `_project_label` (e.g. two repos called "api") get suffixed with the
    last 8 chars of their canonical key so the report never silently
    merges them."""
    label_keys: dict[str, list[str]] = defaultdict(list)
    for k in by_proj_key:
        label_keys[_project_label(k)].append(k)

    def label(k: str) -> str:
        base = _project_label(k)
        if len(label_keys[base]) <= 1:
            return base
        suffix = k[-8:] if len(k) > 8 else k
        return f"{base} ({suffix})"

    return label


def aggregate(auto: list[dict], retro_by_id: dict[str, dict],
              pricing: dict) -> dict:
    """Produce every cut both reports render. Pure function: no I/O.

    Inputs:
      - auto: post-filter list of session rows (already windowed/projected).
      - retro_by_id: map of session_id → retro row, used for left-join.
      - pricing: pricing.json contents; empty dict disables cache savings.

    Returns a dict with stable keys (the contract). Adding new keys is
    backwards-compatible for consumers that only read what they need;
    renaming or dropping keys requires updating both consumers in lockstep.
    """
    joined = [{**a, **retro_by_id.get(a.get("session_id", ""), {})}
              for a in auto]

    costs = [a["cost_usd"] for a in auto if a.get("cost_usd") is not None]

    # Cost by model
    by_model: dict[str, list[float]] = defaultdict(list)
    for a in auto:
        if a.get("cost_usd") is not None:
            by_model[a.get("model") or "unknown"].append(a["cost_usd"])

    # Cost by project (canonical key — origin > git_root > cwd)
    by_proj_key: dict[str, list[float]] = defaultdict(list)
    for a in auto:
        if a.get("cost_usd") is not None:
            by_proj_key[_project_key(a)].append(a["cost_usd"])

    # Cost by task_outcome (retro)
    by_outcome: dict[str, list[float]] = defaultdict(list)
    for j in joined:
        if j.get("task_outcome") and j.get("cost_usd") is not None:
            by_outcome[j["task_outcome"]].append(j["cost_usd"])

    # Correction rate by model (retro)
    by_model_corr: dict[str, list[float]] = defaultdict(list)
    for j in joined:
        if j.get("correction_rate") is not None and j.get("model"):
            by_model_corr[j["model"]].append(j["correction_rate"])

    # Skill trigger accuracy distribution (retro)
    tri = Counter(j.get("skill_trigger_accuracy") for j in joined
                  if j.get("skill_trigger_accuracy"))

    # Top expensive sessions
    top_expensive = sorted(
        (a for a in auto if a.get("cost_usd") is not None),
        key=lambda x: x["cost_usd"], reverse=True,
    )[:10]

    # Cache efficiency by model — [cache_read, cache_create]
    cache_by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for a in auto:
        m_ = a.get("model") or "unknown"
        cache_by_model[m_][0] += a.get("cache_read_tokens") or 0
        cache_by_model[m_][1] += a.get("cache_creation_tokens") or 0

    # Cache savings counterfactual — [usd_saved, cache_read_tokens]
    savings_by_model: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    total_actual = 0.0
    total_counterfactual = 0.0
    for a in auto:
        cr = a.get("cache_read_tokens") or 0
        if cr <= 0:
            continue
        rates = _rates_for(a.get("model") or "", pricing)
        if not rates:
            continue
        rate_in, rate_cr = rates
        saved = cr * (rate_in - rate_cr) / 1_000_000
        m_ = a.get("model") or "unknown"
        savings_by_model[m_][0] += saved
        savings_by_model[m_][1] += cr
        if a.get("cost_usd") is not None:
            total_actual += a["cost_usd"]
            total_counterfactual += a["cost_usd"] + saved
    total_saved = sum(s for s, _ in savings_by_model.values())

    # Marathon sessions (turn_count > 300)
    all_marathons = [a for a in auto if (a.get("turn_count") or 0) > 300]
    marathons = sorted(
        all_marathons, key=lambda x: x.get("cost_usd") or 0, reverse=True,
    )[:10]
    marathon_total_count = len(all_marathons)
    marathon_total_cost = sum(a.get("cost_usd") or 0 for a in all_marathons)

    # Top tools across all sessions
    tool_totals: Counter = Counter()
    for a in auto:
        for t, c in (a.get("tool_distribution") or {}).items():
            tool_totals[t] += c

    # Subagent invocations (v3+)
    sub_totals: Counter = Counter()
    v3_sub_rows = 0
    for a in auto:
        if "subagent_invocations" in a:
            v3_sub_rows += 1
            for s, c in (a.get("subagent_invocations") or {}).items():
                sub_totals[s] += c

    # Subagent return surface (v4+)
    surface: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "return_chars": 0, "duration_s": 0.0,
        "errors": 0, "max_chars": 0, "max_dur": 0.0,
    })
    v4_rows = 0
    for a in auto:
        if "subagent_stats" not in a:
            continue
        v4_rows += 1
        for sub, stat in (a.get("subagent_stats") or {}).items():
            s = surface[sub]
            s["count"] += stat.get("count") or 0
            s["return_chars"] += stat.get("return_chars_total") or 0
            s["duration_s"] += stat.get("duration_s_total") or 0.0
            s["errors"] += stat.get("errors") or 0
            if (stat.get("max_return_chars") or 0) > s["max_chars"]:
                s["max_chars"] = stat.get("max_return_chars") or 0
            if (stat.get("max_duration_s") or 0) > s["max_dur"]:
                s["max_dur"] = stat.get("max_duration_s") or 0

    # Cheap subagent calls (v4+) — dispatches whose tool_result was <200 chars
    cheap_rows = [a for a in auto if "cheap_subagent_calls" in a]
    cheap_total = sum(a.get("cheap_subagent_calls") or 0 for a in cheap_rows)
    sub_total_dispatches = sum(
        sum((a.get("subagent_stats") or {}).get(s, {}).get("count") or 0
            for s in (a.get("subagent_stats") or {}))
        for a in cheap_rows
    )
    cheap_offenders = sorted(
        (a for a in cheap_rows if (a.get("cheap_subagent_calls") or 0) > 0),
        key=lambda x: x.get("cheap_subagent_calls") or 0, reverse=True,
    )[:5]

    # Tool errors (v3+)
    v3_with_errors = sorted(
        (a for a in auto
         if a.get("tool_errors_count") is not None
         and a.get("tool_errors_count") > 0),
        key=lambda x: x.get("tool_errors_count") or 0, reverse=True,
    )
    total_errors = sum(a.get("tool_errors_count") or 0 for a in auto
                       if "tool_errors_count" in a)
    v3_err_count = sum(1 for a in auto if "tool_errors_count" in a)

    # Correction-heavy sessions (v3+)
    v3_corr = [
        a for a in auto
        if "short_user_followups_count" in a
        and ((a.get("short_user_followups_count") or 0)
             + (a.get("correction_keyword_hits") or 0)) > 0
    ]
    v3_corr.sort(
        key=lambda x: ((x.get("short_user_followups_count") or 0)
                       + (x.get("correction_keyword_hits") or 0)),
        reverse=True,
    )

    # Cost trend by ISO week
    by_week: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for a in auto:
        ts = a.get("ts")
        if not ts or a.get("cost_usd") is None:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        y, w, _ = dt.isocalendar()
        key = f"{y}-W{w:02d}"
        by_week[key][0] += a["cost_usd"]
        by_week[key][1] += 1

    schema_versions = sorted({
        a.get("schema_version") for a in auto if a.get("schema_version") is not None
    })

    return {
        "joined": joined,
        "costs": costs,
        "by_model": dict(by_model),
        "by_proj_key": dict(by_proj_key),
        "by_outcome": dict(by_outcome),
        "by_model_corr": dict(by_model_corr),
        "trigger_accuracy": dict(tri),
        "top_expensive": top_expensive,
        "cache_by_model": {k: list(v) for k, v in cache_by_model.items()},
        "savings_by_model": {k: list(v) for k, v in savings_by_model.items()},
        "total_saved": total_saved,
        "total_actual": total_actual,
        "total_counterfactual": total_counterfactual,
        "marathons": marathons,
        "marathon_total_count": marathon_total_count,
        "marathon_total_cost": marathon_total_cost,
        "tool_totals": dict(tool_totals),
        "sub_totals": dict(sub_totals),
        "v3_sub_rows": v3_sub_rows,
        "surface": {k: dict(v) for k, v in surface.items()},
        "v4_rows": v4_rows,
        "cheap_total": cheap_total,
        "cheap_dispatches": sub_total_dispatches,
        "cheap_rows": len(cheap_rows),
        "cheap_offenders": cheap_offenders,
        "tool_errors": v3_with_errors[:10],
        "total_errors": total_errors,
        "v3_err_count": v3_err_count,
        "correction_heavy": v3_corr[:10],
        "by_week": dict(by_week),
        "schema_versions": schema_versions,
    }
