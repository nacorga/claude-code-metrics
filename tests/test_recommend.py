"""Tests for skills/recommend/_recommend.py.

Pin the rule firing behaviour at threshold boundaries so future tweaks to
constants surface here first. Each rule is exercised in isolation, then the
full evaluate() pipeline gets one integration smoke test.

Stdlib only — same pattern as test_aggregate.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SHARED_DIR = REPO / "skills" / "analyze-metrics"
RECO_DIR = REPO / "skills" / "recommend"

# _recommend imports _helpers and consumes _aggregate output, so both dirs
# must be on sys.path for the test process.
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(RECO_DIR))
os.environ["CCM_HELPERS_DIR"] = str(SHARED_DIR)

import _recommend as r  # noqa: E402
from _aggregate import aggregate  # noqa: E402


# ---------------------------------------------------------------------------
# Test row factory — the same shape as the v5 hook produces.
# ---------------------------------------------------------------------------

def _row(**overrides) -> dict:
    base = {
        "schema_version": 5,
        "session_id": "s1",
        "ts": "2026-04-27T10:00:00Z",
        "cwd": "/Users/me/code/foo",
        "git_root": "/Users/me/code/foo",
        "git_remote_origin": "github.com/me/foo",
        "model": "claude-sonnet-4-6",
        "cost_usd": 0.10,
        "duration_s": 60.0,
        "turn_count": 50,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_tokens": 2000,
        "cache_read_tokens": 8000,
        "tool_calls_total": 10,
        "tool_distribution": {"Read": 6, "Edit": 4},
        "tool_errors_count": 0,
        "subagent_invocations": {},
        "subagent_stats": {},
        "cheap_subagent_calls": 0,
        "user_msg_count": 5,
        "short_user_followups_count": 0,
        "correction_keyword_hits": 0,
    }
    base.update(overrides)
    return base


def _agg(rows: list[dict]) -> dict:
    """Convenience: most rule tests need both rows and the aggregate dict."""
    return aggregate(rows, {}, {})


# ---------------------------------------------------------------------------
# Recommendation dataclass and severity ordering.
# ---------------------------------------------------------------------------

class TestRecommendationShape(unittest.TestCase):
    def test_dataclass_has_required_fields(self):
        rec = r.Recommendation(
            id="x.y", severity="warn",
            title="t", why="w", action="a",
        )
        self.assertEqual(rec.id, "x.y")
        self.assertEqual(rec.evidence, [])  # default factory

    def test_severity_order_high_warn_info(self):
        # evaluate() sorts by severity; these are the only legal values.
        self.assertLess(r._SEVERITY_ORDER["high"], r._SEVERITY_ORDER["warn"])
        self.assertLess(r._SEVERITY_ORDER["warn"], r._SEVERITY_ORDER["info"])


# ---------------------------------------------------------------------------
# friction.tool_errors — fires at p90 (>=7) with at least min_evidence sessions.
# ---------------------------------------------------------------------------

class TestRuleFrictionToolErrors(unittest.TestCase):
    def test_fires_when_three_sessions_above_threshold(self):
        rows = [_row(session_id=f"e{i}", tool_errors_count=8) for i in range(3)]
        rec = r._rule_friction_tool_errors(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.id, "friction.tool_errors")
        self.assertEqual(rec.severity, "warn")
        self.assertEqual(len(rec.evidence), 3)

    def test_suppressed_when_below_min_evidence(self):
        rows = [_row(session_id=f"e{i}", tool_errors_count=8) for i in range(2)]
        rec = r._rule_friction_tool_errors(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_does_not_fire_below_threshold(self):
        # 6 < 7 (p90 threshold). Even with 5 sessions, no fire.
        rows = [_row(session_id=f"e{i}", tool_errors_count=6) for i in range(5)]
        rec = r._rule_friction_tool_errors(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_min_evidence_one_loosens_signal(self):
        rows = [_row(session_id="e0", tool_errors_count=10)]
        rec = r._rule_friction_tool_errors(_agg(rows), rows, min_evidence=1)
        self.assertIsNotNone(rec)


# ---------------------------------------------------------------------------
# friction.over_steering — fires at p90 (>=13 short follow-ups).
# ---------------------------------------------------------------------------

class TestRuleFrictionOverSteering(unittest.TestCase):
    def test_fires_when_concentrated_in_a_project(self):
        rows = [
            _row(session_id=f"s{i}",
                 short_user_followups_count=14,
                 git_remote_origin="github.com/me/api")
            for i in range(3)
        ]
        rec = r._rule_friction_over_steering(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.id, "friction.over_steering")
        # Title should include the disambiguated project label.
        self.assertIn("me/api", rec.title)

    def test_fires_globally_when_hits_scattered(self):
        rows = [
            _row(session_id="s1", short_user_followups_count=14,
                 git_remote_origin="github.com/me/a"),
            _row(session_id="s2", short_user_followups_count=14,
                 git_remote_origin="github.com/me/b"),
            _row(session_id="s3", short_user_followups_count=14,
                 git_remote_origin="github.com/me/c"),
        ]
        rec = r._rule_friction_over_steering(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        # No single project hits min_evidence → generic title.
        self.assertNotIn("me/a", rec.title)
        self.assertEqual(len(rec.evidence), 3)

    def test_does_not_fire_below_threshold(self):
        rows = [_row(session_id=f"s{i}", short_user_followups_count=12)
                for i in range(5)]
        rec = r._rule_friction_over_steering(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)


# ---------------------------------------------------------------------------
# agent.error_rate — high severity, >=20% with min invocations.
# ---------------------------------------------------------------------------

class TestRuleAgentErrorRate(unittest.TestCase):
    def test_fires_at_40_percent(self):
        # Mirrors the user's real ux-ui-reviewer signal: 2/5 = 40%.
        rows = [
            _row(session_id="a", subagent_stats={
                "ux-ui-reviewer": {
                    "count": 5, "return_chars_total": 10000,
                    "duration_s_total": 25.0, "errors": 2,
                    "max_return_chars": 3000, "max_duration_s": 8.0,
                },
            }),
        ]
        rec = r._rule_agent_error_rate(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.severity, "high")
        self.assertIn("ux-ui-reviewer", rec.title)
        self.assertIn("40%", rec.title)

    def test_does_not_fire_below_min_invocations(self):
        # 1 error in 2 invocations = 50%, but invocation count too low.
        rows = [_row(subagent_stats={
            "tiny": {"count": 2, "return_chars_total": 100,
                     "duration_s_total": 1.0, "errors": 1,
                     "max_return_chars": 100, "max_duration_s": 1.0},
        })]
        rec = r._rule_agent_error_rate(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_does_not_fire_below_error_rate(self):
        # 1 error in 10 invocations = 10% < 20%.
        rows = [_row(subagent_stats={
            "ok": {"count": 10, "return_chars_total": 5000,
                   "duration_s_total": 5.0, "errors": 1,
                   "max_return_chars": 1000, "max_duration_s": 1.0},
        })]
        rec = r._rule_agent_error_rate(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_min_evidence_tightens_invocation_floor(self):
        # 5 invocations clears the calibrated floor (3) but not
        # min_evidence=10 → rule must suppress, proving the CLI knob has
        # teeth on this rule too.
        rows = [_row(subagent_stats={
            "ux-ui-reviewer": {"count": 5, "return_chars_total": 10000,
                               "duration_s_total": 25.0, "errors": 2,
                               "max_return_chars": 3000,
                               "max_duration_s": 8.0},
        })]
        self.assertIsNotNone(
            r._rule_agent_error_rate(_agg(rows), rows, min_evidence=3),
        )
        self.assertIsNone(
            r._rule_agent_error_rate(_agg(rows), rows, min_evidence=10),
        )


# ---------------------------------------------------------------------------
# agent.return_too_short — info severity, avg < 1500 with >= 10 invocations.
# ---------------------------------------------------------------------------

class TestRuleAgentReturnTooShort(unittest.TestCase):
    def test_fires_when_avg_short_and_count_high(self):
        rows = [_row(subagent_stats={
            "summarizer": {
                "count": 12, "return_chars_total": 12 * 800,  # avg 800
                "duration_s_total": 24.0, "errors": 0,
                "max_return_chars": 1200, "max_duration_s": 3.0,
            },
        })]
        rec = r._rule_agent_return_too_short(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.severity, "info")
        self.assertIn("summarizer", rec.title)

    def test_suppressed_when_count_too_low(self):
        rows = [_row(subagent_stats={
            "summarizer": {
                "count": 5, "return_chars_total": 5 * 800,  # avg 800 but n<10
                "duration_s_total": 10.0, "errors": 0,
                "max_return_chars": 1200, "max_duration_s": 3.0,
            },
        })]
        rec = r._rule_agent_return_too_short(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_min_evidence_tightens_invocation_floor(self):
        # 12 invocations clears the calibrated floor (10); raising
        # min_evidence to 15 should suppress.
        rows = [_row(subagent_stats={
            "summarizer": {
                "count": 12, "return_chars_total": 12 * 800,
                "duration_s_total": 24.0, "errors": 0,
                "max_return_chars": 1200, "max_duration_s": 3.0,
            },
        })]
        self.assertIsNotNone(
            r._rule_agent_return_too_short(_agg(rows), rows, min_evidence=3),
        )
        self.assertIsNone(
            r._rule_agent_return_too_short(_agg(rows), rows, min_evidence=15),
        )


# ---------------------------------------------------------------------------
# cost.opus_overspend — fires with >=3 short, low-tool, low-cost-but-not-zero
# Opus sessions.
# ---------------------------------------------------------------------------

class TestRuleCostOpusOverspend(unittest.TestCase):
    def test_fires_with_three_qualifying_sessions(self):
        rows = [
            _row(session_id=f"o{i}", model="claude-opus-4-7",
                 tool_calls_total=3, duration_s=60.0, cost_usd=0.80)
            for i in range(3)
        ]
        rec = r._rule_cost_opus_overspend(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.severity, "info")

    def test_does_not_fire_for_sonnet(self):
        rows = [_row(session_id=f"s{i}", model="claude-sonnet-4-6",
                     tool_calls_total=3, duration_s=60.0, cost_usd=0.80)
                for i in range(3)]
        rec = r._rule_cost_opus_overspend(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_does_not_fire_for_long_sessions(self):
        # duration >= 120s → not "short" enough.
        rows = [_row(session_id=f"o{i}", model="claude-opus-4-7",
                     tool_calls_total=3, duration_s=200.0, cost_usd=0.80)
                for i in range(3)]
        rec = r._rule_cost_opus_overspend(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)


# ---------------------------------------------------------------------------
# pattern.tool_dominance — sources its data from agg["tool_dominance_sessions"].
# ---------------------------------------------------------------------------

class TestRulePatternToolDominance(unittest.TestCase):
    def test_fires_when_three_bash_dominant_sessions(self):
        rows = []
        for i in range(3):
            # 35 calls, 28 of them Bash → 80% dominance.
            rows.append(_row(
                session_id=f"b{i}",
                tool_calls_total=35,
                tool_distribution={"Bash": 28, "Read": 7},
            ))
        rec = r._rule_pattern_tool_dominance(_agg(rows), rows, min_evidence=3)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.id, "pattern.tool_dominance")
        self.assertIn("Bash", rec.title)

    def test_does_not_fire_below_30_calls(self):
        # Below the 30-call floor — high fraction but too small to matter.
        rows = [_row(session_id=f"b{i}", tool_calls_total=10,
                     tool_distribution={"Bash": 9, "Read": 1})
                for i in range(5)]
        rec = r._rule_pattern_tool_dominance(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_does_not_fire_below_70_percent(self):
        rows = [_row(session_id=f"b{i}", tool_calls_total=35,
                     tool_distribution={"Bash": 23, "Read": 12})
                for i in range(5)]
        # 23/35 = 65.7% < 70%
        rec = r._rule_pattern_tool_dominance(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)


# ---------------------------------------------------------------------------
# project.no_claude_md — touches the filesystem; tempdir simulates git_root.
# ---------------------------------------------------------------------------

class TestRuleProjectNoClaudeMd(unittest.TestCase):
    def test_fires_when_project_has_sessions_but_no_claude_md(self):
        with tempfile.TemporaryDirectory() as td:
            git_root = Path(td) / "myrepo"
            git_root.mkdir()
            # NO CLAUDE.md inside.
            rows = [_row(session_id=f"r{i}", git_root=str(git_root),
                         git_remote_origin="github.com/me/myrepo")
                    for i in range(10)]
            rec = r._rule_project_no_claude_md(_agg(rows), rows,
                                               min_evidence=3)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.id, "project.no_claude_md")
            self.assertIn("myrepo", rec.title)

    def test_does_not_fire_when_claude_md_present(self):
        with tempfile.TemporaryDirectory() as td:
            git_root = Path(td) / "myrepo"
            git_root.mkdir()
            (git_root / "CLAUDE.md").write_text("# project doc\n")
            rows = [_row(session_id=f"r{i}", git_root=str(git_root),
                         git_remote_origin="github.com/me/myrepo")
                    for i in range(10)]
            rec = r._rule_project_no_claude_md(_agg(rows), rows,
                                               min_evidence=3)
            self.assertIsNone(rec)

    def test_does_not_fire_below_session_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            git_root = Path(td) / "myrepo"
            git_root.mkdir()
            # 9 < 10 sessions; below the project hotspot threshold.
            rows = [_row(session_id=f"r{i}", git_root=str(git_root),
                         git_remote_origin="github.com/me/myrepo")
                    for i in range(9)]
            rec = r._rule_project_no_claude_md(_agg(rows), rows,
                                               min_evidence=3)
            self.assertIsNone(rec)

    def test_skips_non_existent_git_root(self):
        # git_root recorded but directory has been deleted on this machine.
        rows = [_row(session_id=f"r{i}",
                     git_root="/totally/nonexistent/path",
                     git_remote_origin="github.com/me/ghost")
                for i in range(15)]
        rec = r._rule_project_no_claude_md(_agg(rows), rows, min_evidence=3)
        self.assertIsNone(rec)

    def test_min_evidence_tightens_session_floor(self):
        # 12 sessions clears the calibrated floor (10) at min_evidence=3
        # but not at min_evidence=20.
        with tempfile.TemporaryDirectory() as td:
            git_root = Path(td) / "myrepo"
            git_root.mkdir()
            rows = [_row(session_id=f"r{i}", git_root=str(git_root),
                         git_remote_origin="github.com/me/myrepo")
                    for i in range(12)]
            self.assertIsNotNone(
                r._rule_project_no_claude_md(_agg(rows), rows, min_evidence=3),
            )
            self.assertIsNone(
                r._rule_project_no_claude_md(_agg(rows), rows,
                                             min_evidence=20),
            )


# ---------------------------------------------------------------------------
# evaluate() — integration: runs every rule, sorts by severity, swallows
# rule errors, returns deterministic order.
# ---------------------------------------------------------------------------

class TestEvaluatePipeline(unittest.TestCase):
    def test_no_rules_fire_returns_empty_list(self):
        rows = [_row()]
        recs = r.evaluate(_agg(rows), rows, min_evidence=3)
        self.assertEqual(recs, [])

    def test_severity_sort_high_then_warn_then_info(self):
        # Construct rows that trigger all three severity levels:
        # high: agent.error_rate (40% on ux-ui-reviewer)
        # warn: friction.tool_errors (3 sessions >= 7 errors)
        # info: cost.opus_overspend (3 short Opus sessions)
        rows = []
        # high
        rows.append(_row(session_id="ux", subagent_stats={
            "ux-ui-reviewer": {"count": 5, "return_chars_total": 10000,
                               "duration_s_total": 25.0, "errors": 2,
                               "max_return_chars": 3000,
                               "max_duration_s": 8.0},
        }))
        # warn (need >=3 separate sessions with high errors)
        for i in range(3):
            rows.append(_row(session_id=f"err{i}", tool_errors_count=10))
        # info (need >=3 short Opus sessions)
        for i in range(3):
            rows.append(_row(session_id=f"opus{i}",
                             model="claude-opus-4-7",
                             tool_calls_total=3, duration_s=60.0,
                             cost_usd=0.80))
        recs = r.evaluate(_agg(rows), rows, min_evidence=3)
        sevs = [rec.severity for rec in recs]
        # Must be sorted high → warn → info.
        self.assertEqual(sevs, sorted(sevs, key=lambda s: r._SEVERITY_ORDER[s]))
        self.assertIn("high", sevs)
        self.assertIn("warn", sevs)
        self.assertIn("info", sevs)

    def test_buggy_rule_does_not_break_others(self):
        # Inject a temporarily-broken rule into _RULES; evaluate() must
        # swallow the exception and still emit the others. patch.object
        # restores the list even if the assertion raises mid-test.
        def boom(agg, rows, min_evidence):
            raise RuntimeError("simulated rule crash")

        with patch.object(r, "_RULES", [boom, *r._RULES]):
            rows = [_row(session_id=f"e{i}", tool_errors_count=10)
                    for i in range(3)]
            recs = r.evaluate(_agg(rows), rows, min_evidence=3)
            ids = [rec.id for rec in recs]
            self.assertIn("friction.tool_errors", ids)


# ---------------------------------------------------------------------------
# render_markdown — empty state + populated.
# ---------------------------------------------------------------------------

class TestRenderMarkdown(unittest.TestCase):
    def test_empty_state(self):
        out = r.render_markdown([])
        self.assertIn("Recommendations", out)
        self.assertIn("No actionable recommendations", out)

    def test_populated_includes_severity_id_evidence(self):
        rec = r.Recommendation(
            id="x.y", severity="warn", title="t",
            why="w", action="a",
            evidence=[{"session_id": "abc", "count": 3}],
        )
        out = r.render_markdown([rec])
        self.assertIn("[WARN]", out)
        self.assertIn("`x.y`", out)
        self.assertIn("session_id", out)  # evidence header
        self.assertIn("abc", out)


# ---------------------------------------------------------------------------
# render_html_inner — empty state + escaping.
# ---------------------------------------------------------------------------

class TestRenderHtmlInner(unittest.TestCase):
    def test_empty_state_renders_paragraph(self):
        out = r.render_html_inner([])
        self.assertIn('<p class="empty"', out)

    def test_html_escaping_of_user_content(self):
        rec = r.Recommendation(
            id="x.y", severity="warn", title="<script>x</script>",
            why="& and <br>", action="a",
        )
        out = r.render_html_inner([rec])
        self.assertNotIn("<script>x</script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)


if __name__ == "__main__":
    unittest.main()
