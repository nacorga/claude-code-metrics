"""Unit tests for skills/analyze-metrics/_helpers.py.

The helpers are imported by SKILL.md's heredoc at runtime (from the installed
copy at ~/.claude/skills/analyze-metrics/). These tests pin the contract so
filter / project-key logic cannot drift silently.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "analyze-metrics"))

import _helpers as h  # noqa: E402


FROZEN_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


class TestParseSince(unittest.TestCase):

    def test_none_returns_default_30d(self):
        cutoff = h._parse_since(None, now=FROZEN_NOW)
        self.assertEqual(cutoff, FROZEN_NOW - timedelta(days=30))

    def test_empty_string_returns_default(self):
        self.assertEqual(
            h._parse_since("", now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=30),
        )
        self.assertEqual(
            h._parse_since("   ", now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=30),
        )

    def test_all_disables_temporal_filter(self):
        self.assertIsNone(h._parse_since("all", now=FROZEN_NOW))
        self.assertIsNone(h._parse_since("ALL", now=FROZEN_NOW))
        self.assertIsNone(h._parse_since("0d", now=FROZEN_NOW))
        self.assertIsNone(h._parse_since("0", now=FROZEN_NOW))

    def test_relative_days(self):
        self.assertEqual(
            h._parse_since("7d", now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=7),
        )
        self.assertEqual(
            h._parse_since("90d", now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=90),
        )

    def test_iso_date_naive_treated_as_utc(self):
        self.assertEqual(
            h._parse_since("2026-01-15", now=FROZEN_NOW),
            datetime(2026, 1, 15, tzinfo=timezone.utc),
        )

    def test_iso_with_timezone_preserved(self):
        self.assertEqual(
            h._parse_since("2026-01-15T10:00:00+00:00", now=FROZEN_NOW),
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        )

    def test_garbage_falls_back_to_default(self):
        # Must NOT crash. Falls back to the default window so the report
        # still runs even when the user typoes a flag value.
        self.assertEqual(
            h._parse_since("banana", now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=30),
        )

    def test_custom_default_days(self):
        self.assertEqual(
            h._parse_since(None, default_days=7, now=FROZEN_NOW),
            FROZEN_NOW - timedelta(days=7),
        )


class TestWindowLabel(unittest.TestCase):
    """The header label must mirror _parse_since's rules so the report never
    advertises a window the data does not match."""

    def test_none_says_default(self):
        self.assertEqual(h._window_label(None), "last 30 days")

    def test_empty_says_default(self):
        self.assertEqual(h._window_label(""), "last 30 days")
        self.assertEqual(h._window_label("   "), "last 30 days")

    def test_all_says_all_time(self):
        self.assertEqual(h._window_label("all"), "all-time")
        self.assertEqual(h._window_label("0d"), "all-time")

    def test_relative_days(self):
        self.assertEqual(h._window_label("7d"), "last 7d")
        self.assertEqual(h._window_label("90d"), "last 90d")

    def test_iso_says_since(self):
        self.assertEqual(h._window_label("2026-01-15"), "since 2026-01-15")

    def test_garbage_says_default_and_surfaces_input(self):
        # Mirrors _parse_since's fallback: garbage uses the default window
        # AND tells the reader the input was not understood.
        self.assertIn("default", h._window_label("banana"))
        self.assertIn("banana", h._window_label("banana"))

    def test_custom_default_days(self):
        self.assertEqual(h._window_label(None, default_days=7), "last 7 days")


class TestRowTs(unittest.TestCase):

    def test_z_suffix_and_offset_yield_same_dt(self):
        a = h._row_ts({"ts": "2026-04-01T10:00:00Z"})
        b = h._row_ts({"ts": "2026-04-01T10:00:00+00:00"})
        self.assertEqual(a, b)
        self.assertEqual(a, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))

    def test_naive_ts_assumed_utc(self):
        # Defensive: legacy rows may lack a tz suffix. Treat as UTC rather
        # than crashing, so windowed comparisons still work.
        dt = h._row_ts({"ts": "2026-04-01T10:00:00"})
        self.assertEqual(dt, datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc))

    def test_missing_or_bad_ts_returns_none(self):
        self.assertIsNone(h._row_ts({}))
        self.assertIsNone(h._row_ts({"ts": ""}))
        self.assertIsNone(h._row_ts({"ts": "not-a-date"}))
        self.assertIsNone(h._row_ts({"ts": 123}))


class TestProjectKey(unittest.TestCase):

    def test_origin_wins(self):
        row = {
            "git_remote_origin": "github.com/foo/bar",
            "git_root": "/x/y",
            "cwd": "/x/y/sub",
        }
        self.assertEqual(h._project_key(row), "github.com/foo/bar")

    def test_falls_through_to_git_root_when_no_origin(self):
        row = {"git_remote_origin": "", "git_root": "/x/y", "cwd": "/x/y"}
        self.assertEqual(h._project_key(row), "/x/y")

    def test_falls_through_to_cwd_for_legacy_rows(self):
        # v3/v4 rows: no git_* fields at all. Must group by cwd, same as the
        # legacy report behaviour, so the upgrade path is a strict superset.
        row = {"cwd": "/Users/me/code/legacy-repo"}
        self.assertEqual(h._project_key(row), "/Users/me/code/legacy-repo")

    def test_unknown_when_everything_empty(self):
        self.assertEqual(h._project_key({}), "unknown")
        self.assertEqual(
            h._project_key({"cwd": "", "git_root": "", "git_remote_origin": ""}),
            "unknown",
        )


class TestProjectLabel(unittest.TestCase):

    def test_host_owner_repo_strips_host(self):
        self.assertEqual(h._project_label("github.com/foo/bar"), "foo/bar")
        self.assertEqual(h._project_label("gitlab.com/group/sub/repo"),
                         "group/sub/repo")

    def test_absolute_path_returns_basename(self):
        self.assertEqual(h._project_label("/Users/me/code/api"), "api")
        self.assertEqual(h._project_label("/srv/work/repo/"), "repo")

    def test_unknown_passthrough(self):
        self.assertEqual(h._project_label("unknown"), "unknown")

    def test_empty_yields_unknown(self):
        self.assertEqual(h._project_label(""), "unknown")

    def test_short_key_passthrough(self):
        self.assertEqual(h._project_label("scratch"), "scratch")


class TestProjectMatches(unittest.TestCase):

    def setUp(self) -> None:
        self.row = {
            "git_remote_origin": "github.com/foo/bar",
            "git_root": "/Users/me/code/bar",
            "cwd": "/Users/me/code/bar/subdir",
        }

    def test_match_by_full_origin(self):
        self.assertTrue(h._project_matches(self.row, "github.com/foo/bar"))

    def test_match_by_origin_label(self):
        self.assertTrue(h._project_matches(self.row, "foo/bar"))

    def test_match_by_git_root(self):
        self.assertTrue(h._project_matches(self.row, "/Users/me/code/bar"))

    def test_match_by_basename_of_git_root(self):
        self.assertTrue(h._project_matches(self.row, "bar"))

    def test_match_by_cwd_basename(self):
        self.assertTrue(h._project_matches(self.row, "subdir"))

    def test_no_match_for_unrelated(self):
        self.assertFalse(h._project_matches(self.row, "other-repo"))

    def test_substring_does_not_match(self):
        # 'ba' is a substring of 'bar' but must not match — substring
        # matching would silently merge unrelated repos.
        self.assertFalse(h._project_matches(self.row, "ba"))

    def test_empty_query_matches_anything(self):
        self.assertTrue(h._project_matches(self.row, ""))
        self.assertTrue(h._project_matches(self.row, "   "))

    def test_legacy_row_matches_by_cwd(self):
        legacy = {"cwd": "/Users/me/code/legacy"}
        self.assertTrue(h._project_matches(legacy, "legacy"))
        self.assertTrue(h._project_matches(legacy, "/Users/me/code/legacy"))


class TestEnvToFilterPipeline(unittest.TestCase):
    """End-to-end of the env-var → filter pipeline that SKILL.md applies.
    Validates the contract the heredoc relies on without invoking bash."""

    def setUp(self) -> None:
        self.now = FROZEN_NOW
        self.rows = [
            {
                "session_id": "old-other",
                "ts": (self.now - timedelta(days=60)).isoformat(),
                "git_remote_origin": "github.com/foo/other",
                "git_root": "/x/other",
                "cwd": "/x/other",
                "cost_usd": 1.0,
            },
            {
                "session_id": "recent-bar",
                "ts": (self.now - timedelta(days=5)).isoformat(),
                "git_remote_origin": "github.com/foo/bar",
                "git_root": "/x/bar",
                "cwd": "/x/bar",
                "cost_usd": 2.0,
            },
            {
                "session_id": "recent-other",
                "ts": (self.now - timedelta(days=2)).isoformat(),
                "git_remote_origin": "github.com/foo/other",
                "git_root": "/x/other",
                "cwd": "/x/other",
                "cost_usd": 3.0,
            },
            {
                # legacy v3 row — no git_* fields at all
                "session_id": "legacy-bar",
                "ts": (self.now - timedelta(days=3)).isoformat(),
                "cwd": "/Users/me/code/bar",
                "cost_usd": 4.0,
            },
        ]

    def _filter(self, since_raw, project_raw):
        cutoff = h._parse_since(since_raw, now=self.now)
        out = self.rows
        if cutoff:
            out = [r for r in out if h._row_ts(r) and h._row_ts(r) >= cutoff]
        if project_raw:
            out = [r for r in out if h._project_matches(r, project_raw)]
        return out

    def test_default_window_excludes_old_row(self):
        ids = {r["session_id"] for r in self._filter(None, None)}
        self.assertEqual(ids, {"recent-bar", "recent-other", "legacy-bar"})

    def test_since_all_includes_everything(self):
        ids = {r["session_id"] for r in self._filter("all", None)}
        self.assertEqual(ids, {"old-other", "recent-bar",
                               "recent-other", "legacy-bar"})

    def test_since_7d_window(self):
        ids = {r["session_id"] for r in self._filter("7d", None)}
        self.assertEqual(ids, {"recent-bar", "recent-other", "legacy-bar"})

    def test_project_origin_filter_includes_legacy_via_cwd_basename(self):
        # --project bar matches: github.com/foo/bar (label 'foo/bar' or
        # basename 'bar'), and the legacy row whose cwd basename is 'bar'.
        ids = {r["session_id"] for r in self._filter("all", "bar")}
        self.assertEqual(ids, {"recent-bar", "legacy-bar"})

    def test_project_full_origin_filter(self):
        ids = {r["session_id"] for r in self._filter(
            "all", "github.com/foo/other")}
        self.assertEqual(ids, {"old-other", "recent-other"})

    def test_combined_since_and_project(self):
        ids = {r["session_id"] for r in self._filter("30d", "other")}
        self.assertEqual(ids, {"recent-other"})


class TestLatestSessionTs(unittest.TestCase):

    def test_empty_rows_return_none(self):
        self.assertIsNone(h._latest_session_ts([]))

    def test_picks_max_across_valid_rows(self):
        rows = [
            {"ts": "2026-04-01T10:00:00Z"},
            {"ts": "2026-04-15T10:00:00Z"},
            {"ts": "2026-04-10T10:00:00Z"},
        ]
        self.assertEqual(
            h._latest_session_ts(rows),
            datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_skips_rows_with_missing_or_malformed_ts(self):
        rows = [
            {"ts": "2026-04-01T10:00:00Z"},
            {"ts": "garbage"},
            {},  # missing ts entirely
            {"ts": None},
        ]
        self.assertEqual(
            h._latest_session_ts(rows),
            datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
        )


class TestHumanizeAge(unittest.TestCase):

    def test_none_returns_never(self):
        self.assertEqual(h._humanize_age(None, now=FROZEN_NOW), "never")

    def test_under_a_minute_is_just_now(self):
        dt = FROZEN_NOW - timedelta(seconds=20)
        self.assertEqual(h._humanize_age(dt, now=FROZEN_NOW), "just now")

    def test_minute_resolution(self):
        dt = FROZEN_NOW - timedelta(minutes=12)
        self.assertEqual(h._humanize_age(dt, now=FROZEN_NOW), "12m ago")

    def test_hour_resolution(self):
        dt = FROZEN_NOW - timedelta(hours=4, minutes=30)
        self.assertEqual(h._humanize_age(dt, now=FROZEN_NOW), "4h ago")

    def test_day_resolution(self):
        dt = FROZEN_NOW - timedelta(days=3)
        self.assertEqual(h._humanize_age(dt, now=FROZEN_NOW), "3d ago")

    def test_future_timestamp_collapses_to_just_now(self):
        # Clock skew between machines / NTP correction must not produce a
        # negative or nonsense age in the report header.
        dt = FROZEN_NOW + timedelta(minutes=5)
        self.assertEqual(h._humanize_age(dt, now=FROZEN_NOW), "just now")


class TestRecentLogErrors(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm_logerr_"))
        self.log_path = self.tmp / "hook.log"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_zero(self):
        self.assertEqual(
            h._recent_log_errors(self.log_path, days=7, now=FROZEN_NOW),
            0,
        )

    def test_counts_lines_within_window(self):
        recent_a = (FROZEN_NOW - timedelta(hours=1)).isoformat()
        recent_b = (FROZEN_NOW - timedelta(days=2)).isoformat()
        old = (FROZEN_NOW - timedelta(days=30)).isoformat()
        self.log_path.write_text(
            f"[{recent_a}] boom\n"
            f"[{recent_b}] fizz\n"
            f"[{old}] ancient\n"
        )
        self.assertEqual(
            h._recent_log_errors(self.log_path, days=7, now=FROZEN_NOW),
            2,
        )

    def test_malformed_lines_are_ignored_silently(self):
        recent = (FROZEN_NOW - timedelta(hours=1)).isoformat()
        self.log_path.write_text(
            "no timestamp prefix at all\n"
            f"[not-an-iso-date] junk\n"
            f"[{recent}] real one\n"
            "[] empty bracket\n"
        )
        self.assertEqual(
            h._recent_log_errors(self.log_path, days=7, now=FROZEN_NOW),
            1,
        )

    def test_returns_zero_on_unreadable_path(self):
        # Directory in place of a file → open() raises IsADirectoryError /
        # OSError; helper must coerce to 0 rather than propagate.
        (self.tmp / "subdir").mkdir()
        self.assertEqual(
            h._recent_log_errors(self.tmp / "subdir", days=7, now=FROZEN_NOW),
            0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
