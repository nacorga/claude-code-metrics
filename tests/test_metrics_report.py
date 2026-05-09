"""Tests for skills/metrics-report/_render.py.

Drives main() against fixture JSONL files written to a tempdir, then asserts
on the resulting HTML: structural soundness, expected totals, conditional
sections, escaping, and the 'no remote URL / no script tag' security
contract that makes the report safe to open from disk.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "metrics-report"
HELPERS_DIR = REPO / "skills" / "analyze-metrics"

# Make _render importable without going through the installed path.
sys.path.insert(0, str(SKILL_DIR))
os.environ["CCM_HELPERS_DIR"] = str(HELPERS_DIR)

import _render  # noqa: E402


NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_row(**overrides) -> dict:
    """Schema v5 row with sensible defaults; override any field per test."""
    base = {
        "schema_version": 5,
        "session_id": "abc1234567890",
        "ts": _ts(2),
        "cwd": "/Users/me/code/foo",
        "git_root": "/Users/me/code/foo",
        "git_remote_origin": "github.com/me/foo",
        "end_reason": "close",
        "duration_s": 600.0,
        "turn_count": 25,
        "model": "claude-sonnet-4-6",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_tokens": 2000,
        "cache_read_tokens": 8000,
        "tool_calls_total": 12,
        "tool_distribution": {"Read": 7, "Edit": 3, "Bash": 2},
        "tool_errors_count": 0,
        "cost_usd": 0.50,
        "user_msg_count": 5,
        "short_user_followups_count": 1,
        "correction_keyword_hits": 0,
        "subagent_invocations": {"Explore": 2},
        "subagent_stats": {
            "Explore": {
                "count": 2, "return_chars_total": 4000,
                "duration_s_total": 8.0, "errors": 0,
                "max_return_chars": 2500, "max_duration_s": 5.0,
            }
        },
        "cheap_subagent_calls": 0,
    }
    base.update(overrides)
    return base


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


class _Fixture:
    """Convenience wrapper: creates a temp metrics dir + writes JSONL."""

    def __init__(self, auto_rows: list[dict],
                 retro_rows: list[dict] | None = None,
                 pricing: dict | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics_dir = Path(self.tmp.name) / "metrics"
        self.metrics_dir.mkdir()
        _write_jsonl(self.metrics_dir / "auto.jsonl", auto_rows)
        if retro_rows:
            _write_jsonl(self.metrics_dir / "retro.jsonl", retro_rows)
        if pricing is not None:
            (self.metrics_dir / "pricing.json").write_text(json.dumps(pricing))

    def run(self, *args: str,
            freeze_now: datetime | None = NOW) -> tuple[int, str, Path]:
        """Run main() in-process (stdout/stderr captured).

        `freeze_now` defaults to the module-level `NOW` so every time-windowed
        test sees a hermetic clock — fixtures are built from `_ts(N)` against
        the same NOW, and production reads it through the test seam in
        `_render.main(now=...)`. Pass `freeze_now=None` to opt out: tests
        that compose their own timestamped inputs from real-clock now (e.g.
        `TestHealthSignal` building log lines) need real-vs-real comparison
        and must NOT freeze production's clock.
        """
        out = self.metrics_dir / "report.html"
        argv = ["--metrics-dir", str(self.metrics_dir),
                "--output", str(out)] + list(args)
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = _render.main(argv, now=freeze_now)
        text = out.read_text() if out.is_file() else ""
        return rc, text, out

    def cleanup(self) -> None:
        self.tmp.cleanup()


class TestBasicRender(unittest.TestCase):

    def test_renders_with_minimal_v5_rows(self):
        rows = [
            _make_row(session_id="s-recent", ts=_ts(1), cost_usd=0.10),
            _make_row(session_id="s-week",   ts=_ts(8), cost_usd=0.20),
            _make_row(session_id="s-old",    ts=_ts(40), cost_usd=0.30),
        ]
        fx = _Fixture(rows)
        try:
            rc, html, path = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            self.assertTrue(path.is_file())
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIn("claude-code-metrics", html)
            # KPIs
            self.assertIn(">3<", html)  # session count
            # All-time line shows in window because --since all
            self.assertIn("3 sessions", html)
        finally:
            fx.cleanup()

    def test_default_window_excludes_old_rows(self):
        rows = [
            _make_row(session_id="s-recent", ts=_ts(1), cost_usd=0.10),
            _make_row(session_id="s-old",    ts=_ts(45), cost_usd=0.30),
        ]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run()  # default 30d
            self.assertEqual(rc, 0)
            # The window section should report 1 session.
            # 'last 30 days' is the window label.
            self.assertIn("last 30 days", html)
            # Old row excluded → its session_id snippet shouldn't appear.
            self.assertNotIn("s-old", html)
            self.assertIn("s-recent", html)
        finally:
            fx.cleanup()

    def test_since_flag_filters(self):
        rows = [
            _make_row(session_id="s-yesterday", ts=_ts(1), cost_usd=0.10),
            _make_row(session_id="s-last-week", ts=_ts(8), cost_usd=0.20),
        ]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "3d")
            self.assertEqual(rc, 0)
            self.assertIn("s-yesterday", html)
            self.assertNotIn("s-last-week", html)
        finally:
            fx.cleanup()

    def test_project_flag_filters(self):
        rows = [
            _make_row(session_id="s-foo", ts=_ts(1),
                      git_remote_origin="github.com/me/foo",
                      git_root="/x/foo", cwd="/x/foo"),
            _make_row(session_id="s-bar", ts=_ts(1),
                      git_remote_origin="github.com/me/bar",
                      git_root="/x/bar", cwd="/x/bar"),
        ]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--project", "foo")
            self.assertEqual(rc, 0)
            self.assertIn("s-foo", html)
            self.assertNotIn("s-bar", html)
        finally:
            fx.cleanup()


class TestNoData(unittest.TestCase):

    def test_empty_auto_returns_nonzero(self):
        fx = _Fixture([])
        try:
            rc, html, path = fx.run()
            self.assertNotEqual(rc, 0)
            self.assertFalse(path.is_file())
        finally:
            fx.cleanup()

    def test_filter_with_no_match_renders_friendly_message(self):
        rows = [_make_row(ts=_ts(40))]  # all rows older than 30d
        fx = _Fixture(rows)
        try:
            rc, html, path = fx.run()  # default 30d → no rows in window
            self.assertEqual(rc, 0)
            self.assertTrue(path.is_file())
            self.assertIn("No sessions match", html)
            self.assertIn("--since all", html)
        finally:
            fx.cleanup()

    def test_empty_window_still_renders_recommendations_anchor(self):
        # Regression: with no rows in window, the static TOC links to
        # `#recommendations` so the section must still exist (with an
        # empty state) — otherwise the TOC dangles.
        rows = [_make_row(ts=_ts(40))]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run()  # default 30d → empty window
            self.assertEqual(rc, 0)
            self.assertIn('id="recommendations"', html)
            self.assertIn("No actionable recommendations", html)
        finally:
            fx.cleanup()


class TestEscaping(unittest.TestCase):

    def test_html_special_chars_escaped(self):
        rows = [
            _make_row(
                session_id="<script>alert(1)</script>",
                model="evil&model",
                cwd="/path/<weird>/repo",
                ts=_ts(1),
            ),
        ]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Raw script tag content from the row must not appear unescaped.
            self.assertNotIn("<script>alert(1)</script>", html)
            # Properly escaped form should appear somewhere.
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("evil&amp;model", html)
        finally:
            fx.cleanup()

    def test_dict_keys_from_user_env_are_escaped(self):
        # tool_distribution / subagent_invocations / subagent_stats keys are
        # captured verbatim from the user's environment. Custom MCP servers
        # and user-defined subagent types could produce names with HTML
        # special chars; the renderer must escape them on every interpolation.
        rows = [
            _make_row(
                session_id="evil-keys",
                ts=_ts(1),
                model="<img src=x onerror=alert(1)>",
                tool_distribution={"<malicious-tool>": 5, "Read": 1},
                subagent_invocations={"<evil-subagent>": 3},
                subagent_stats={
                    "<evil-subagent>": {
                        "count": 3, "return_chars_total": 600,
                        "duration_s_total": 1.5, "errors": 0,
                        "max_return_chars": 300, "max_duration_s": 1.0,
                    },
                },
            ),
        ]
        retro = [
            {"schema_version": 1, "session_id": "evil-keys", "ts": _ts(1),
             "task_outcome": "<bad>outcome</bad>",
             "skill_trigger_accuracy": "<bad>",
             "correction_rate": 5},
        ]
        fx = _Fixture(rows, retro_rows=retro)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # No raw HTML from any user-controlled string survives the render.
            for raw in (
                "<img src=x onerror=alert(1)>",
                "<malicious-tool>",
                "<evil-subagent>",
                "<bad>outcome</bad>",
            ):
                self.assertNotIn(raw, html, f"unescaped: {raw}")
            # Escaped forms must be present somewhere in the document.
            self.assertIn("&lt;malicious-tool&gt;", html)
            self.assertIn("&lt;evil-subagent&gt;", html)
            self.assertIn("&lt;img src=x", html)
        finally:
            fx.cleanup()


class TestSecurityContract(unittest.TestCase):
    """The HTML must be safe to open offline from file:// — fully self-
    contained, no remote loads, no scripting."""

    def test_no_script_no_remote_no_cdn(self):
        rows = [_make_row(ts=_ts(1))]
        fx = _Fixture(rows, pricing={"_default": {"input": 3.0, "cache_read": 0.3}})
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # No JS at all.
            self.assertNotIn("<script", html.lower())
            # No remote URLs.
            self.assertFalse(re.search(r"https?://[^\"' >]+", html),
                             "HTML must not contain remote URLs")
            # No external stylesheet/font loads.
            self.assertNotIn("<link", html.lower())
            self.assertNotIn("googleapis", html)
            self.assertNotIn("cdn.", html)
            # Doctype + closing html tag present.
            self.assertTrue(html.startswith("<!DOCTYPE html>"))
            self.assertTrue(html.rstrip().endswith("</html>"))
        finally:
            fx.cleanup()


class TestRetroJoin(unittest.TestCase):

    def test_retro_sections_appear_when_data_present(self):
        rows = [
            _make_row(session_id="s1", ts=_ts(1), cost_usd=1.0),
            _make_row(session_id="s2", ts=_ts(2), cost_usd=2.0),
        ]
        retro = [
            {"schema_version": 1, "session_id": "s1", "ts": _ts(1),
             "correction_rate": 3, "skill_trigger_accuracy": "good",
             "task_outcome": "shipped"},
            {"schema_version": 1, "session_id": "s2", "ts": _ts(2),
             "correction_rate": 7, "skill_trigger_accuracy": "bad",
             "task_outcome": "abandoned"},
        ]
        fx = _Fixture(rows, retro_rows=retro)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            self.assertIn("Retro insights", html)
            self.assertIn("Cost by task_outcome", html)
            self.assertIn("shipped", html)
            self.assertIn("abandoned", html)
            self.assertIn("Skill trigger accuracy", html)
            self.assertIn("good", html)
            # KPI: retro coverage 2/2
            self.assertIn("2 / 2", html)
        finally:
            fx.cleanup()

    def test_no_retro_file_does_not_crash(self):
        rows = [_make_row(ts=_ts(1))]
        fx = _Fixture(rows)  # no retro
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Retro insights block must show empty message, not crash.
            self.assertIn("Retro insights", html)
            self.assertIn("insufficient retro data", html)
        finally:
            fx.cleanup()


class TestSchemaMix(unittest.TestCase):
    """v3 / v4 / v5 rows must coexist gracefully."""

    def test_legacy_v3_row_without_subagent_stats_or_git_fields(self):
        v3 = {
            "schema_version": 3,
            "session_id": "v3legacy",
            "ts": _ts(2),
            "cwd": "/Users/me/code/legacy",
            "end_reason": "close",
            "duration_s": 100.0,
            "turn_count": 10,
            "model": "claude-sonnet-4-5",
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_tokens": 200, "cache_read_tokens": 400,
            "tool_calls_total": 3,
            "tool_distribution": {"Read": 3},
            "tool_errors_count": 0,
            "cost_usd": 0.05,
            "user_msg_count": 3,
            "short_user_followups_count": 0,
            "correction_keyword_hits": 0,
            "subagent_invocations": {},
        }
        v5 = _make_row(session_id="v5modern", ts=_ts(1))
        fx = _Fixture([v3, v5])
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Cost by project should disambiguate via cwd basename for v3,
            # and via git_remote_origin label for v5.
            self.assertIn("legacy", html)
            self.assertIn("foo", html)
            # v4 surface section: only v5 row has subagent_stats.
            self.assertIn("Subagent return surface", html)
            # Even when v4 data is present the static TOC anchor must still
            # land on a real section — render emits a placeholder pointing
            # the reader at the richer v4+ sections instead of nothing.
            self.assertIn('id="subagent-invocations"', html)
            self.assertIn("covered by the v4+ subagent sections", html)
            # Check the v4 surface has Explore listed.
            self.assertIn("Explore", html)
        finally:
            fx.cleanup()

    def test_no_v4_rows_falls_back_to_v3_invocations_section(self):
        v3 = {
            "schema_version": 3,
            "session_id": "v3only",
            "ts": _ts(1),
            "cwd": "/Users/me/code/x",
            "end_reason": "close",
            "duration_s": 60.0,
            "turn_count": 5,
            "model": "claude-haiku-4-5",
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "tool_calls_total": 1,
            "tool_distribution": {"Read": 1},
            "tool_errors_count": 0,
            "cost_usd": 0.01,
            "user_msg_count": 1,
            "short_user_followups_count": 0,
            "correction_keyword_hits": 0,
            "subagent_invocations": {"Plan": 1},
        }
        fx = _Fixture([v3])
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # When no v4 surface exists, the v3 invocations section is shown.
            self.assertIn("Subagent invocations", html)
            self.assertIn("Plan", html)
            # And the v4 surface section reports 'no v4 sessions yet'.
            self.assertIn("no v4 sessions yet", html)
        finally:
            fx.cleanup()


class TestTotals(unittest.TestCase):

    def test_total_cost_in_callout_matches_sum(self):
        rows = [
            _make_row(session_id=f"s{i}", ts=_ts(i + 1),
                      cost_usd=round(0.10 * (i + 1), 4))
            for i in range(5)
        ]
        # Sum: 0.10+0.20+0.30+0.40+0.50 = 1.50
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Hero KPI
            self.assertIn("$1.50", html)
        finally:
            fx.cleanup()

    def test_cache_savings_callout_renders_with_pricing(self):
        rows = [
            _make_row(session_id="s1", ts=_ts(1),
                      cache_read_tokens=10_000_000,
                      cache_creation_tokens=1_000_000,
                      cost_usd=2.00),
        ]
        # input rate $3, cache_read $0.30: savings = 10M * (3 - 0.3) / 1M = $27
        pricing = {"claude-sonnet-4-6": {"input": 3.0, "cache_read": 0.30}}
        fx = _Fixture(rows, pricing=pricing)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            self.assertIn("Cache saved you", html)
            self.assertIn("$27.00", html)
        finally:
            fx.cleanup()


class TestErrorRows(unittest.TestCase):

    def test_error_rows_excluded_from_aggregates_but_counted(self):
        good = _make_row(session_id="good", ts=_ts(1), cost_usd=1.00)
        bad = {
            "schema_version": 5,
            "session_id": "bad",
            "ts": _ts(1),
            "error": "parse_crash",
        }
        fx = _Fixture([good, bad])
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Hero KPI should show 1 session in window
            self.assertIn("1 errored", html)
            # Cost should match the good row only
            self.assertIn("$1.00", html)
            # Error session_id must NOT appear in tables
            self.assertNotIn(">bad<", html)
        finally:
            fx.cleanup()


class TestHealthSignal(unittest.TestCase):
    """The hero header carries an at-a-glance health signal: time since last
    logged session, plus a count of recent hook errors. When the count is
    nonzero, a warning paragraph must appear so silent hook breakage gets
    surfaced to the user — that's the whole point of the feature."""

    def _write_log(self, fx: _Fixture, lines: list[str]) -> None:
        (fx.metrics_dir / "hook.log").write_text("\n".join(lines) + "\n")

    def test_clean_log_renders_health_line_no_warning(self):
        rows = [_make_row(session_id="s1", ts=_ts(0), cost_usd=0.10)]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            self.assertIn("Last session:", html)
            self.assertIn("hook errors (7d): 0", html)
            # The CSS class definition lives in <style> on every render;
            # only the warning *paragraph* is conditional. Check for the
            # tag, not the bare class name.
            self.assertNotIn('<p class="health-warning">', html)
        finally:
            fx.cleanup()

    def test_recent_errors_render_warning_paragraph(self):
        rows = [_make_row(session_id="s1", ts=_ts(0), cost_usd=0.10)]
        fx = _Fixture(rows)
        try:
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(hours=2)).isoformat()
            self._write_log(fx, [
                f"[{recent}] [ERROR] hook crashed parsing payload",
                f"[{recent}] [ERROR] another failure",
            ])
            # Log timestamps were composed from real-clock now → production
            # health-signal must compare against real now too. Opt out of
            # the fixture's hermetic clock.
            rc, html, _ = fx.run("--since", "all", freeze_now=None)
            self.assertEqual(rc, 0)
            self.assertIn("hook errors (7d): 2", html)
            self.assertIn('<p class="health-warning">', html)
            self.assertIn("hook errors in the last 7 days", html)
        finally:
            fx.cleanup()

    def test_info_lines_do_not_warn(self):
        # Regression guard: pricing-fallback / ghost-skip / dedupe entries
        # are tagged [INFO] and must not count toward the health warning,
        # otherwise users on a default install see false-positive errors
        # every session.
        rows = [_make_row(session_id="s1", ts=_ts(0), cost_usd=0.10)]
        fx = _Fixture(rows)
        try:
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(hours=2)).isoformat()
            self._write_log(fx, [
                f"[{recent}] [INFO] using _default pricing for foo",
                f"[{recent}] [INFO] skip: ghost session",
                f"[{recent}] [INFO] dedupe: session_id abc already recorded",
            ])
            # Log timestamps composed from real-clock now → opt out.
            rc, html, _ = fx.run("--since", "all", freeze_now=None)
            self.assertEqual(rc, 0)
            self.assertIn("hook errors (7d): 0", html)
            self.assertNotIn('<p class="health-warning">', html)
        finally:
            fx.cleanup()

    def test_old_log_lines_outside_window_dont_warn(self):
        rows = [_make_row(session_id="s1", ts=_ts(0), cost_usd=0.10)]
        fx = _Fixture(rows)
        try:
            now = datetime.now(timezone.utc)
            old = (now - timedelta(days=30)).isoformat()
            self._write_log(fx, [f"[{old}] [ERROR] long-ago crash"])
            # Log timestamps composed from real-clock now → opt out.
            rc, html, _ = fx.run("--since", "all", freeze_now=None)
            self.assertEqual(rc, 0)
            self.assertIn("hook errors (7d): 0", html)
            self.assertNotIn('<p class="health-warning">', html)
        finally:
            fx.cleanup()

    def test_rotated_log_errors_are_counted(self):
        # The active log might be empty right after rotation; the recent
        # errors must still surface from hook.log.1.
        rows = [_make_row(session_id="s1", ts=_ts(0), cost_usd=0.10)]
        fx = _Fixture(rows)
        try:
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(hours=2)).isoformat()
            (fx.metrics_dir / "hook.log").write_text(
                f"[{recent}] [INFO] benign skip\n"
            )
            (fx.metrics_dir / "hook.log.1").write_text(
                f"[{recent}] [ERROR] failure pre-rotation\n"
            )
            # Log timestamps composed from real-clock now → opt out.
            rc, html, _ = fx.run("--since", "all", freeze_now=None)
            self.assertEqual(rc, 0)
            self.assertIn("hook errors (7d): 1", html)
            self.assertIn('<p class="health-warning">', html)
        finally:
            fx.cleanup()


class TestTocAnchors(unittest.TestCase):
    """The static TOC links must always resolve. Adding a new section anchor
    here without rendering it would give the user a dead link — pin every
    anchor to its rendered section."""

    def test_every_toc_anchor_has_a_rendered_section(self):
        rows = [_make_row(ts=_ts(1))]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            # Pull every #fragment used in TOC links and assert there is a
            # matching id on a section element.
            anchors = set(re.findall(r'<a href="#([\w-]+)"', html))
            ids = set(re.findall(r'<section id="([\w-]+)"', html))
            missing = anchors - ids - {"hero"}  # hero anchor lives on a different element
            self.assertFalse(
                missing,
                f"TOC anchors point at sections that were not rendered: {sorted(missing)}",
            )
        finally:
            fx.cleanup()


class TestProjectFilterScope(unittest.TestCase):
    """`--project all` must be treated as an explicit (likely empty) filter,
    not as the unfiltered default — otherwise the hero header lies."""

    def test_no_flag_says_all_time(self):
        rows = [_make_row(ts=_ts(1))]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all")
            self.assertEqual(rc, 0)
            self.assertIn("all-time:", html)
            self.assertNotIn("all-time (this project)", html)
            self.assertIn("project: all", html)
        finally:
            fx.cleanup()

    def test_explicit_project_shows_this_project_scope(self):
        rows = [_make_row(ts=_ts(1), git_remote_origin="github.com/me/foo",
                           git_root="/x/foo", cwd="/x/foo")]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all", "--project", "foo")
            self.assertEqual(rc, 0)
            self.assertIn("all-time (this project)", html)
            self.assertIn("project: foo", html)
        finally:
            fx.cleanup()

    def test_project_all_is_treated_as_explicit_filter(self):
        # Edge case: user types `--project all`. The literal string is the
        # filter, not the "no filter" sentinel. The header must reflect that
        # so it is not silently misleading; no rows match → friendly empty.
        rows = [_make_row(ts=_ts(1), git_remote_origin="github.com/me/foo",
                           git_root="/x/foo", cwd="/x/foo")]
        fx = _Fixture(rows)
        try:
            rc, html, _ = fx.run("--since", "all", "--project", "all")
            self.assertEqual(rc, 0)
            self.assertIn("all-time (this project)", html)
            # The displayed filter is the literal value the user passed.
            self.assertIn("project: all", html)
        finally:
            fx.cleanup()


class TestSubprocessEntrypoint(unittest.TestCase):
    """Verify the script is runnable as `python3 _render.py ...` directly,
    matching how SKILL.md invokes it."""

    def test_script_runs_via_python3(self):
        rows = [_make_row(ts=_ts(1))]
        fx = _Fixture(rows)
        try:
            out_path = fx.metrics_dir / "report.html"
            env = {**os.environ, "CCM_HELPERS_DIR": str(HELPERS_DIR)}
            result = subprocess.run(
                [sys.executable, str(SKILL_DIR / "_render.py"),
                 "--metrics-dir", str(fx.metrics_dir),
                 "--output", str(out_path),
                 "--since", "all"],
                capture_output=True, text=True, env=env, timeout=20,
            )
            self.assertEqual(result.returncode, 0,
                             msg=f"stderr: {result.stderr}")
            self.assertTrue(out_path.is_file())
            self.assertIn("✓ report written", result.stdout)
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
