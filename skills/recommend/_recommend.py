"""Rule-based recommendation engine for /recommend.

Pure stdlib. Reads the aggregate dict (single source of truth produced by
analyze-metrics/_aggregate.aggregate) plus the underlying rows, applies a
small set of conservative threshold rules, and returns a sorted list of
Recommendation records.

Design principles:
  * Only ship rules whose underlying signal is dense enough to fire on real
    user data. A rule that never triggers is worse than no rule — it hides
    real friction behind apparent "all clear".
  * Suppress rules below `min_evidence` supporting sessions; we'd rather be
    silent than confidently wrong on N=1.
  * Each rule explains WHY it fired and proposes a concrete action. The
    user should be able to act on a recommendation without re-reading the
    raw metrics.
  * Thresholds were calibrated from real distributions on the user's own
    data (see plan/humming-plotting-blossom.md). Constants below carry the
    p-value or anchor that justified them.

Evaluated and rejected (do not re-propose without new evidence):
  * `agent.too_chatty` (avg_return_chars over a threshold) — calibration
    on real data showed no statistical outlier; top investigative agents
    cluster 6k–7k chars avg, the proposed 8k threshold would fire on
    zero, lower thresholds would punish exactly the agents whose long
    returns are intentional summaries. A real waste signal would need
    return-vs-next-turn-tool-use correlation, which requires transcript-
    level analysis the hook deliberately avoids.

Evaluated and deferred (auto-activates when data is dense enough):
  * `trend.*` rules (week-over-week regressions on tool_errors, short
    user follow-ups, cost, subagent error rate) — when implemented, a
    single generic `_rule_trend_regression` parameterised over a metric
    table. Self-suppresses until ≥5 ISO weeks each have ≥5 v3+ sessions.
    Direction: only "got worse"; scope: per project. Aggregate adds a
    `by_week_v3` key (additive, back-compat).
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Severity ranking for sort order; lower number wins.
_SEVERITY_ORDER = {"high": 0, "warn": 1, "info": 2}

# ---------- thresholds (calibrated from real data) ----------

# tool_errors_count p90 across the user's v3+ sessions = 7.
_TOOL_ERRORS_HIGH = 7

# short_user_followups_count p90 across v3+ sessions = 13.
_SHORT_FOLLOWUPS_HIGH = 13

# Subagent error rate. 20% is high enough to be a clear outlier and low
# enough to catch real problems before they consume budget. Min invocation
# count avoids firing on N=1 noise.
_AGENT_ERROR_RATE = 0.20
_AGENT_ERROR_MIN_INVOCATIONS = 3

# Subagents that average very short returns are candidates for "do this
# inline". Threshold is conservative; small returns can still be valuable
# (a one-line summary is fine) so we require a high invocation count.
_AGENT_SHORT_RETURN_AVG = 1500
_AGENT_SHORT_RETURN_MIN_INVOCATIONS = 10

# Opus session shape that almost certainly didn't need Opus. All three
# conditions must hold; any one alone is too noisy.
_OPUS_LOW_TOOLS = 5
_OPUS_LOW_DURATION = 120  # seconds
_OPUS_MIN_COST = 0.50

# Project hotspot threshold for the no-CLAUDE.md recommendation.
_PROJECT_MIN_SESSIONS = 10


@dataclass
class Recommendation:
    id: str
    severity: str  # "high" | "warn" | "info"
    title: str
    why: str
    evidence: list[dict] = field(default_factory=list)
    action: str = ""


# ---------- rules ----------

def _rule_friction_tool_errors(agg: dict, rows: list[dict],
                               min_evidence: int):
    hits = [r for r in rows
            if (r.get("tool_errors_count") or 0) >= _TOOL_ERRORS_HIGH]
    if len(hits) < min_evidence:
        return None
    # The transcript doesn't tell us which exact tool errored, so we group
    # by the dominant tool in each session as a proxy. Imperfect but useful
    # as a hint.
    by_tool: dict[str, list] = defaultdict(list)
    for r in hits:
        td = r.get("tool_distribution") or {}
        if not td:
            continue
        top_tool = max(td, key=lambda k: td[k])
        by_tool[top_tool].append(r)
    top_tools = sorted(by_tool.items(), key=lambda x: -len(x[1]))[:3]
    examples = (
        ", ".join(f"{t} ({len(v)})" for t, v in top_tools) if top_tools else "—"
    )
    n = len(hits)
    return Recommendation(
        id="friction.tool_errors",
        severity="warn",
        title=f"{n} sessions hit >={_TOOL_ERRORS_HIGH} tool errors",
        why=(
            f"{n} sessions had {_TOOL_ERRORS_HIGH}+ tool errors (the p90 across "
            f"all v3+ sessions). Dominant tools in those sessions: {examples}. "
            "Spikes in tool_errors usually mean repeated permission prompts, "
            "flaky external services, or a workflow fighting its environment."
        ),
        evidence=[
            {"session_id": r.get("session_id"),
             "tool_errors_count": r.get("tool_errors_count"),
             "cost_usd": r.get("cost_usd")}
            for r in sorted(
                hits, key=lambda x: x.get("tool_errors_count") or 0,
                reverse=True,
            )[:10]
        ],
        action=(
            "Review the listed sessions: tighten allowlists for the dominant "
            "tool, wrap retry-prone flows in a script, or add the failing "
            "operation as an explicit skill so the agent stops re-deriving it."
        ),
    )


def _rule_friction_over_steering(agg: dict, rows: list[dict],
                                 min_evidence: int):
    hits = [
        r for r in rows
        if (r.get("short_user_followups_count") or 0) >= _SHORT_FOLLOWUPS_HIGH
    ]
    if len(hits) < min_evidence:
        return None
    from _helpers import _project_key, _project_label  # type: ignore
    by_proj: dict[str, list] = defaultdict(list)
    for r in hits:
        by_proj[_project_key(r)].append(r)
    hot = sorted(
        ((k, v) for k, v in by_proj.items() if len(v) >= min_evidence),
        key=lambda x: -len(x[1]),
    )
    if hot:
        top_k, top_hits = hot[0]
        label = _project_label(top_k)
        return Recommendation(
            id="friction.over_steering",
            severity="warn",
            title=f"Over-steering hotspot: {label}",
            why=(
                f"{len(top_hits)} sessions in {label} have "
                f">={_SHORT_FOLLOWUPS_HIGH} short user follow-ups (p90). "
                "That density of corrective nudges usually means the "
                "agent's first answer is consistently off-target for this "
                "codebase — the user keeps re-steering instead of getting "
                "it right the first time."
            ),
            evidence=[
                {"session_id": r.get("session_id"),
                 "short_user_followups_count":
                     r.get("short_user_followups_count"),
                 "cost_usd": r.get("cost_usd")}
                for r in sorted(
                    top_hits,
                    key=lambda x: x.get("short_user_followups_count") or 0,
                    reverse=True,
                )[:10]
            ],
            action=(
                f"Add or tighten CLAUDE.md in {label}, or build a "
                "project-specific skill that captures the pattern you keep "
                "having to correct. Encoding it once beats re-explaining "
                "it every session."
            ),
        )
    # Hits aren't concentrated in a single project — still actionable, but
    # the action is generic.
    return Recommendation(
        id="friction.over_steering",
        severity="warn",
        title=f"{len(hits)} sessions show heavy steering",
        why=(
            f"{len(hits)} sessions have >={_SHORT_FOLLOWUPS_HIGH} short user "
            "follow-ups. High follow-up density usually means the agent's "
            "first answer was off and the user is correcting it step by step."
        ),
        evidence=[
            {"session_id": r.get("session_id"),
             "short_user_followups_count":
                 r.get("short_user_followups_count"),
             "project": _project_label(_project_key(r))}
            for r in sorted(
                hits,
                key=lambda x: x.get("short_user_followups_count") or 0,
                reverse=True,
            )[:10]
        ],
        action=(
            "Look at the listed sessions: which prompt patterns kept "
            "missing? Encode the recurring intent in a skill or a global "
            "CLAUDE.md rule."
        ),
    )


def _rule_agent_error_rate(agg: dict, rows: list[dict], min_evidence: int):
    surface = agg.get("surface", {})
    flagged = []
    for name, s in surface.items():
        count = s.get("count", 0)
        errors = s.get("errors", 0)
        if count < _AGENT_ERROR_MIN_INVOCATIONS:
            continue
        rate = errors / count if count else 0
        if rate >= _AGENT_ERROR_RATE:
            flagged.append((name, count, errors, rate))
    if not flagged:
        return None
    flagged.sort(key=lambda x: x[3], reverse=True)
    top_name, top_count, top_errors, top_rate = flagged[0]
    rest = flagged[1:]
    rest_str = (
        " Other agents above the threshold: "
        + ", ".join(f"{n} ({e}/{c})" for n, c, e, _ in rest) + "."
    ) if rest else ""
    return Recommendation(
        id="agent.error_rate",
        severity="high",
        title=(
            f"`{top_name}` is failing {top_rate * 100:.0f}% of "
            f"its invocations"
        ),
        why=(
            f"`{top_name}` was invoked {top_count} times and errored "
            f"{top_errors} times ({top_rate * 100:.0f}%).{rest_str} An error "
            f"rate over {int(_AGENT_ERROR_RATE * 100)}% usually points at a "
            "misconfigured agent definition (allowed tools, prompt scope, "
            "expected output format) rather than task difficulty."
        ),
        evidence=[
            {"agent": n, "count": c, "errors": e,
             "error_rate": f"{r * 100:.0f}%"}
            for n, c, e, r in flagged
        ],
        action=(
            f"Review {top_name}'s definition: prompt clarity, allowed tools, "
            "and whether the work it's asked to do matches its scope. If the "
            "errors are real failures, narrow the agent; if they're just "
            "non-zero exit codes from healthy commands, fix the wrapper."
        ),
    )


def _rule_agent_return_too_short(agg: dict, rows: list[dict],
                                 min_evidence: int):
    surface = agg.get("surface", {})
    flagged = []
    for name, s in surface.items():
        count = s.get("count", 0)
        if count < _AGENT_SHORT_RETURN_MIN_INVOCATIONS:
            continue
        avg = (s.get("return_chars", 0) / count) if count else 0
        if avg < _AGENT_SHORT_RETURN_AVG:
            flagged.append((name, count, avg))
    if not flagged:
        return None
    flagged.sort(key=lambda x: x[2])
    top_name, top_count, top_avg = flagged[0]
    return Recommendation(
        id="agent.return_too_short",
        severity="info",
        title=f"`{top_name}` returns very short results on average",
        why=(
            f"`{top_name}` averages {top_avg:.0f} chars across {top_count} "
            "invocations. Subagent dispatches carry overhead (context "
            "bootstrap, parallel cost); when the answer is consistently "
            f"under {_AGENT_SHORT_RETURN_AVG} chars, the agent is often "
            "doing work that would be cheaper inline."
        ),
        evidence=[
            {"agent": n, "count": c, "avg_return_chars": int(a)}
            for n, c, a in flagged
        ],
        action=(
            f"Consider doing simple lookups inline instead of dispatching "
            f"`{top_name}`. Reserve subagents for work where the summarized "
            "result actually saves parent context."
        ),
    )


def _rule_cost_opus_overspend(agg: dict, rows: list[dict],
                              min_evidence: int):
    hits = []
    for r in rows:
        model = (r.get("model") or "").lower()
        if "opus" not in model:
            continue
        tools = r.get("tool_calls_total") or 0
        dur = r.get("duration_s") or 0
        cost = r.get("cost_usd") or 0
        if (tools <= _OPUS_LOW_TOOLS
                and 0 < dur < _OPUS_LOW_DURATION
                and cost >= _OPUS_MIN_COST):
            hits.append(r)
    if len(hits) < min_evidence:
        return None
    total = sum(r.get("cost_usd") or 0 for r in hits)
    return Recommendation(
        id="cost.opus_overspend",
        severity="info",
        title=(
            f"{len(hits)} short Opus sessions look light enough for Sonnet"
        ),
        why=(
            f"{len(hits)} Opus sessions had <={_OPUS_LOW_TOOLS} tool calls "
            f"and finished in under {_OPUS_LOW_DURATION}s, totaling "
            f"${total:.2f}. Short, low-tool sessions usually don't need "
            "Opus's extra reasoning capacity — Sonnet typically handles "
            "them with no quality loss at a fraction of the cost."
        ),
        evidence=[
            {"session_id": r.get("session_id"),
             "tool_calls": r.get("tool_calls_total"),
             "duration_s": r.get("duration_s"),
             "cost_usd": r.get("cost_usd")}
            for r in sorted(
                hits, key=lambda x: x.get("cost_usd") or 0, reverse=True,
            )[:10]
        ],
        action=(
            "Set Sonnet as the default for quick questions; reserve Opus "
            "for sessions you expect to do extended reasoning or many tool "
            "calls."
        ),
    )


def _rule_pattern_tool_dominance(agg: dict, rows: list[dict],
                                 min_evidence: int):
    dominant = agg.get("tool_dominance_sessions", [])
    if len(dominant) < min_evidence:
        return None
    by_tool = Counter(d["tool"] for d in dominant)
    top_tool, top_n = by_tool.most_common(1)[0]
    if top_n < min_evidence:
        return None
    matching = [d for d in dominant if d["tool"] == top_tool]
    return Recommendation(
        id="pattern.tool_dominance",
        severity="info",
        title=f"{top_n} sessions are {top_tool}-dominated",
        why=(
            f"{top_n} sessions had `{top_tool}` accounting for >=70% of "
            "all tool calls (and at least 30 calls total). Heavy "
            "single-tool workflows are usually a sign of a repeatable task "
            "that could live in a script or skill instead of being "
            "re-derived each session."
        ),
        evidence=[
            {"session_id": d["session_id"],
             "tool": d["tool"],
             "fraction": f"{d['fraction'] * 100:.0f}%",
             "calls": d["total"]}
            for d in matching[:10]
        ],
        action=(
            f"For the {top_tool}-heavy flow, capture it as a slash command + "
            "small Python helper (skill) or a shell script the agent can "
            "call once. Cuts permission prompts and iteration count, and "
            "the next session benefits automatically."
        ),
    )


def _rule_project_no_claude_md(agg: dict, rows: list[dict],
                               min_evidence: int):
    from _helpers import _project_key, _project_label  # type: ignore
    by_proj: Counter = Counter()
    git_roots: dict[str, str] = {}
    for r in rows:
        k = _project_key(r)
        by_proj[k] += 1
        gr = r.get("git_root")
        if gr and isinstance(gr, str) and gr.strip():
            git_roots[k] = gr.strip()
    candidates = []
    for k, count in by_proj.most_common():
        if count < _PROJECT_MIN_SESSIONS:
            break  # most_common is sorted descending; once below threshold, done
        gr = git_roots.get(k)
        if not gr:
            continue  # no git_root → can't check (probably a v3/v4 row)
        try:
            gr_path = Path(gr)
            if not gr_path.is_dir():
                continue
            if (gr_path / "CLAUDE.md").is_file():
                continue
        except OSError:
            continue
        candidates.append((k, count, gr))
    if not candidates:
        return None
    top_k, top_n, top_path = candidates[0]
    label = _project_label(top_k)
    rest = candidates[1:]
    rest_str = (
        " Others: "
        + ", ".join(f"{_project_label(k)} ({c})" for k, c, _ in rest)
        + "."
    ) if rest else ""
    return Recommendation(
        id="project.no_claude_md",
        severity="info",
        title=f"{label} has {top_n} sessions but no CLAUDE.md",
        why=(
            f"{label} accounts for {top_n} sessions in this window but lacks "
            f"a `CLAUDE.md` at its git root.{rest_str} High-frequency "
            "projects are the highest-leverage place to encode conventions: "
            "every future session in that repo benefits from the same "
            "instructions."
        ),
        evidence=[
            {"project": _project_label(k), "sessions": c, "git_root": p}
            for k, c, p in candidates
        ],
        action=(
            f"From {label}: run `/init` to scaffold a CLAUDE.md, or write "
            "one capturing the project's invariants, naming conventions, "
            "and workflow rules."
        ),
    )


_RULES = [
    _rule_friction_tool_errors,
    _rule_friction_over_steering,
    _rule_agent_error_rate,
    _rule_agent_return_too_short,
    _rule_cost_opus_overspend,
    _rule_pattern_tool_dominance,
    _rule_project_no_claude_md,
]


def evaluate(agg: dict, rows: list[dict],
             min_evidence: int = 3) -> list[Recommendation]:
    """Run every rule, drop None results, sort by severity then id.

    A buggy rule must not break the report — exceptions inside a rule are
    swallowed silently. The user still gets every other recommendation.
    """
    out: list[Recommendation] = []
    for rule in _RULES:
        try:
            rec = rule(agg, rows, min_evidence)
        except Exception:
            continue
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: (_SEVERITY_ORDER.get(r.severity, 99), r.id))
    return out


def _evidence_keys(recs: list[Recommendation]) -> list[str]:
    """First evidence record's keys define column order — every rule emits
    a stable shape, so this is deterministic."""
    if not recs or not recs[0].evidence:
        return []
    return list(recs[0].evidence[0].keys())


def render_markdown(recs: list[Recommendation]) -> str:
    if not recs:
        return (
            "## Recommendations\n\n"
            "_No actionable recommendations in this window. Either things "
            "are running smoothly or there's not enough signal — try "
            "`--since all` to widen the window._\n"
        )
    badge = {"high": "[HIGH]", "warn": "[WARN]", "info": "[INFO]"}
    parts = ["## Recommendations\n"]
    for r in recs:
        parts.append(f"### {badge.get(r.severity, r.severity)} {r.title}")
        parts.append(f"_id: `{r.id}`_\n")
        parts.append(r.why + "\n")
        parts.append(f"**Suggested:** {r.action}\n")
        if r.evidence:
            keys = list(r.evidence[0].keys())
            header = "| " + " | ".join(keys) + " |"
            sep = "| " + " | ".join(["---"] * len(keys)) + " |"
            parts.append("**Evidence**\n")
            parts.append(header)
            parts.append(sep)
            for e in r.evidence:
                parts.append(
                    "| " + " | ".join(str(e.get(k, "—")) for k in keys) + " |"
                )
            parts.append("")
    return "\n".join(parts)


# ---------- HTML helpers (used by metrics-report/_render.py) ----------

def _esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def render_html_inner(recs: list[Recommendation]) -> str:
    """Inner HTML for the Recommendations section. The caller wraps this
    in its own _section() to match the rest of the report's chrome."""
    if not recs:
        return (
            '<p class="empty">No actionable recommendations in this window. '
            'Either things are running smoothly, or there is not enough '
            'signal yet.</p>'
        )
    callout_class = {"high": "callout bad", "warn": "callout warn",
                     "info": "callout"}
    parts = []
    for r in recs:
        parts.append(f'<div class="{callout_class.get(r.severity, "callout")}">')
        parts.append(
            f'<strong>[{_esc(r.severity.upper())}]</strong> '
            f'{_esc(r.title)} '
            f'<code>{_esc(r.id)}</code>'
        )
        parts.append('</div>')
        parts.append(f'<p>{_esc(r.why)}</p>')
        parts.append(
            f'<p><strong>Suggested:</strong> {_esc(r.action)}</p>'
        )
        if r.evidence:
            keys = list(r.evidence[0].keys())
            head = "".join(
                f'<th class="num">{_esc(k)}</th>' for k in keys
            )
            body_rows = []
            for e in r.evidence:
                cells = "".join(
                    f'<td class="num">{_esc(e.get(k, "—"))}</td>'
                    for k in keys
                )
                body_rows.append(f'<tr>{cells}</tr>')
            parts.append(
                '<table><thead><tr>' + head + '</tr></thead>'
                '<tbody>' + "".join(body_rows) + '</tbody></table>'
            )
    return "".join(parts)
