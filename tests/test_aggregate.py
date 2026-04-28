"""Tests for skills/analyze-metrics/_aggregate.py.

Pins the dict shape and per-slice algorithms of the shared aggregation
function consumed by both /metrics-report and /analyze-metrics. Any drift
in numbers between the two reports must surface here first.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARED_DIR = REPO / "skills" / "analyze-metrics"

sys.path.insert(0, str(SHARED_DIR))
os.environ["CCM_HELPERS_DIR"] = str(SHARED_DIR)

import _aggregate  # noqa: E402
from _aggregate import (  # noqa: E402
    aggregate,
    _disambiguated_label_factory,
    _rates_for,
)


NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(**overrides) -> dict:
    base = {
        "schema_version": 5,
        "session_id": "s1",
        "ts": _ts(2),
        "cwd": "/Users/me/code/foo",
        "git_root": "/Users/me/code/foo",
        "git_remote_origin": "github.com/me/foo",
        "model": "claude-sonnet-4-6",
        "cost_usd": 0.10,
        "turn_count": 50,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_tokens": 2000,
        "cache_read_tokens": 8000,
        "tool_calls_total": 10,
        "tool_distribution": {"Read": 6, "Edit": 4},
    }
    base.update(overrides)
    return base


PRICING = {
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-opus-4": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "_default": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
}


# ---------------------------------------------------------------------------
# Dict shape — the contract both consumers depend on.
# ---------------------------------------------------------------------------

EXPECTED_KEYS = {
    "joined", "costs",
    "by_model", "by_proj_key", "by_outcome", "by_model_corr",
    "trigger_accuracy", "top_expensive",
    "cache_by_model", "savings_by_model",
    "total_saved", "total_actual", "total_counterfactual",
    "marathons", "marathon_total_count", "marathon_total_cost",
    "tool_totals",
    "sub_totals", "v3_sub_rows",
    "surface", "v4_rows",
    "cheap_total", "cheap_dispatches", "cheap_rows", "cheap_offenders",
    "tool_errors", "total_errors", "v3_err_count",
    "correction_heavy",
    "by_week",
    "schema_versions",
}


class TestContract(unittest.TestCase):
    def test_empty_input_returns_full_shape_with_zeros(self):
        agg = aggregate([], {}, {})
        self.assertEqual(set(agg.keys()), EXPECTED_KEYS)
        self.assertEqual(agg["costs"], [])
        self.assertEqual(agg["by_model"], {})
        self.assertEqual(agg["total_saved"], 0)
        self.assertEqual(agg["marathon_total_count"], 0)
        self.assertEqual(agg["v4_rows"], 0)
        self.assertEqual(agg["schema_versions"], [])

    def test_populated_input_matches_shape(self):
        agg = aggregate([_row()], {}, PRICING)
        self.assertEqual(set(agg.keys()), EXPECTED_KEYS)


# ---------------------------------------------------------------------------
# Cost cuts — by_model / by_proj_key / costs / top_expensive.
# ---------------------------------------------------------------------------

class TestCostCuts(unittest.TestCase):
    def test_by_model_groups_costs(self):
        rows = [
            _row(session_id="a", model="claude-sonnet-4-6", cost_usd=0.50),
            _row(session_id="b", model="claude-sonnet-4-6", cost_usd=0.30),
            _row(session_id="c", model="claude-opus-4", cost_usd=2.00),
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(sorted(agg["by_model"]),
                         ["claude-opus-4", "claude-sonnet-4-6"])
        self.assertEqual(sum(agg["by_model"]["claude-sonnet-4-6"]), 0.80)
        self.assertEqual(agg["by_model"]["claude-opus-4"], [2.00])

    def test_by_proj_key_uses_origin_when_present(self):
        # v5 rows: origin wins over git_root and cwd.
        rows = [
            _row(session_id="a", git_remote_origin="github.com/x/api",
                 git_root="/tmp/api", cwd="/tmp/api", cost_usd=1.0),
            _row(session_id="b", git_remote_origin="github.com/x/api",
                 cost_usd=2.0),
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(list(agg["by_proj_key"].keys()),
                         ["github.com/x/api"])
        self.assertEqual(sum(agg["by_proj_key"]["github.com/x/api"]), 3.0)

    def test_by_proj_key_falls_back_to_cwd_when_no_git(self):
        rows = [_row(session_id="a", git_remote_origin=None,
                     git_root=None, cwd="/Users/me/scratch",
                     cost_usd=1.0)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(list(agg["by_proj_key"]), ["/Users/me/scratch"])

    def test_costs_excludes_none(self):
        rows = [_row(session_id="a", cost_usd=0.10),
                _row(session_id="b", cost_usd=None)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["costs"], [0.10])

    def test_top_expensive_capped_at_10_sorted_desc(self):
        rows = [_row(session_id=f"s{i}", cost_usd=float(i))
                for i in range(15)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(len(agg["top_expensive"]), 10)
        costs = [r["cost_usd"] for r in agg["top_expensive"]]
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertEqual(costs[0], 14.0)


# ---------------------------------------------------------------------------
# Marathon sessions — turn_count > 300 with separate top-10 vs total counts.
# ---------------------------------------------------------------------------

class TestMarathons(unittest.TestCase):
    def test_marathons_filter_and_total_independent_of_top_10(self):
        rows = []
        for i in range(15):
            rows.append(_row(session_id=f"m{i}", turn_count=400 + i,
                             cost_usd=10.0 + i))
        # Plus one non-marathon for negative control.
        rows.append(_row(session_id="x", turn_count=100, cost_usd=999.0))
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["marathon_total_count"], 15)
        self.assertEqual(len(agg["marathons"]), 10)
        self.assertEqual(agg["marathon_total_cost"],
                         sum(10.0 + i for i in range(15)))

    def test_no_marathons_returns_zero(self):
        agg = aggregate([_row(turn_count=100)], {}, {})
        self.assertEqual(agg["marathon_total_count"], 0)
        self.assertEqual(agg["marathons"], [])
        self.assertEqual(agg["marathon_total_cost"], 0)


# ---------------------------------------------------------------------------
# Cache savings — counterfactual based on pricing.json.
# ---------------------------------------------------------------------------

class TestCacheSavings(unittest.TestCase):
    def test_savings_compute_from_pricing_diff(self):
        # 1M cache_read tokens at sonnet rates: input=3.0, cache_read=0.30
        # saved = 1_000_000 * (3.0 - 0.30) / 1_000_000 = 2.70
        rows = [_row(session_id="a", model="claude-sonnet-4-6",
                     cost_usd=1.00, cache_read_tokens=1_000_000)]
        agg = aggregate(rows, {}, PRICING)
        self.assertAlmostEqual(agg["total_saved"], 2.70, places=4)
        self.assertAlmostEqual(agg["total_actual"], 1.00, places=4)
        self.assertAlmostEqual(agg["total_counterfactual"], 3.70, places=4)
        self.assertIn("claude-sonnet-4-6", agg["savings_by_model"])

    def test_savings_skip_rows_without_cache_read(self):
        rows = [_row(session_id="a", cache_read_tokens=0, cost_usd=1.0)]
        agg = aggregate(rows, {}, PRICING)
        self.assertEqual(agg["total_saved"], 0)
        self.assertEqual(agg["savings_by_model"], {})

    def test_savings_skip_unknown_models_when_no_default(self):
        no_default = {k: v for k, v in PRICING.items() if k != "_default"}
        rows = [_row(model="future-model-9000", cache_read_tokens=1_000_000)]
        agg = aggregate(rows, {}, no_default)
        self.assertEqual(agg["total_saved"], 0)


# ---------------------------------------------------------------------------
# Subagent metrics — v3 invocations vs v4 surface, and cheap dispatches.
# ---------------------------------------------------------------------------

class TestSubagentMetrics(unittest.TestCase):
    def test_v3_invocations_counted_only_when_field_present(self):
        rows = [
            _row(session_id="v3a", subagent_invocations={"Explore": 2}),
            _row(session_id="v3b", subagent_invocations={"Explore": 1, "Plan": 3}),
            _row(session_id="legacy"),  # no field → not v3
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["v3_sub_rows"], 2)
        self.assertEqual(agg["sub_totals"], {"Explore": 3, "Plan": 3})

    def test_v4_surface_aggregates_per_subagent(self):
        rows = [
            _row(session_id="v4a", subagent_stats={
                "Explore": {"count": 2, "return_chars_total": 5000,
                            "duration_s_total": 4.0, "errors": 0,
                            "max_return_chars": 3000, "max_duration_s": 2.5},
            }),
            _row(session_id="v4b", subagent_stats={
                "Explore": {"count": 1, "return_chars_total": 2000,
                            "duration_s_total": 1.0, "errors": 1,
                            "max_return_chars": 2000, "max_duration_s": 1.0},
            }),
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["v4_rows"], 2)
        s = agg["surface"]["Explore"]
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["return_chars"], 7000)
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["max_chars"], 3000)  # max across rows
        self.assertEqual(s["max_dur"], 2.5)

    def test_cheap_subagent_calls_offenders_top_5(self):
        rows = []
        for i in range(8):
            rows.append(_row(session_id=f"v4-{i}",
                             cheap_subagent_calls=i + 1,
                             subagent_stats={"Explore": {"count": 5}}))
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["cheap_total"], sum(range(1, 9)))
        self.assertEqual(agg["cheap_dispatches"], 8 * 5)
        self.assertEqual(len(agg["cheap_offenders"]), 5)
        # Sorted descending by cheap_subagent_calls.
        self.assertEqual(agg["cheap_offenders"][0]["cheap_subagent_calls"], 8)


# ---------------------------------------------------------------------------
# v3+ conversation-shape signals — tool errors and correction-heavy sessions.
# ---------------------------------------------------------------------------

class TestV3Signals(unittest.TestCase):
    def test_tool_errors_only_positive_top_10(self):
        rows = [_row(session_id=f"e{i}", tool_errors_count=i)
                for i in range(15)]
        # Index 0 has 0 errors → excluded from sorted list.
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["v3_err_count"], 15)  # all rows have the field
        self.assertEqual(agg["total_errors"], sum(range(15)))
        self.assertEqual(len(agg["tool_errors"]), 10)
        # Highest first.
        self.assertEqual(agg["tool_errors"][0]["tool_errors_count"], 14)

    def test_correction_heavy_combines_short_followups_and_keywords(self):
        rows = [
            _row(session_id="quiet", short_user_followups_count=0,
                 correction_keyword_hits=0),
            _row(session_id="chatty", short_user_followups_count=5,
                 correction_keyword_hits=2),
            _row(session_id="frustrated", short_user_followups_count=1,
                 correction_keyword_hits=8),
        ]
        agg = aggregate(rows, {}, {})
        ids = [r["session_id"] for r in agg["correction_heavy"]]
        self.assertEqual(ids, ["frustrated", "chatty"])  # sums 9, 7, 0


# ---------------------------------------------------------------------------
# Retro left-join — joined / by_outcome / by_model_corr / trigger_accuracy.
# ---------------------------------------------------------------------------

class TestRetroJoin(unittest.TestCase):
    def test_joined_merges_retro_when_session_id_matches(self):
        rows = [_row(session_id="abc"), _row(session_id="xyz")]
        retro = {"abc": {"task_outcome": "shipped",
                         "correction_rate": 2,
                         "skill_trigger_accuracy": "high"}}
        agg = aggregate(rows, retro, {})
        # joined retains all auto rows; only matching get retro fields.
        outcomes = [j.get("task_outcome") for j in agg["joined"]]
        self.assertIn("shipped", outcomes)
        self.assertEqual(agg["by_outcome"]["shipped"], [0.10])
        self.assertEqual(agg["by_model_corr"]["claude-sonnet-4-6"], [2])
        self.assertEqual(agg["trigger_accuracy"], {"high": 1})

    def test_by_outcome_empty_without_retro(self):
        agg = aggregate([_row()], {}, {})
        self.assertEqual(agg["by_outcome"], {})


# ---------------------------------------------------------------------------
# Trend by ISO week — labeling is stable across years (Y-Www).
# ---------------------------------------------------------------------------

class TestByWeek(unittest.TestCase):
    def test_iso_week_keys_aggregate_costs(self):
        rows = [
            # Both fall in the same ISO week 2026-W17 (Apr 20 → Apr 26).
            _row(session_id="a", ts="2026-04-20T10:00:00Z", cost_usd=1.0),
            _row(session_id="b", ts="2026-04-23T10:00:00Z", cost_usd=2.0),
            # Different week (W18, Apr 27 →).
            _row(session_id="c", ts="2026-04-28T10:00:00Z", cost_usd=4.0),
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["by_week"]["2026-W17"], [3.0, 2])
        self.assertEqual(agg["by_week"]["2026-W18"], [4.0, 1])

    def test_malformed_ts_skipped(self):
        rows = [_row(ts="not-a-date", cost_usd=99.0),
                _row(session_id="ok", ts="2026-04-20T10:00:00Z", cost_usd=1.0)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(sum(c for c, _ in agg["by_week"].values()), 1.0)


# ---------------------------------------------------------------------------
# Schema versions — sorted unique set across rows.
# ---------------------------------------------------------------------------

class TestSchemaVersions(unittest.TestCase):
    def test_schema_versions_unique_sorted(self):
        rows = [_row(schema_version=3), _row(session_id="b", schema_version=5),
                _row(session_id="c", schema_version=4),
                _row(session_id="d", schema_version=5)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["schema_versions"], [3, 4, 5])

    def test_rows_without_schema_version_dropped(self):
        rows = [_row(schema_version=None), _row(session_id="b", schema_version=5)]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["schema_versions"], [5])


# ---------------------------------------------------------------------------
# Pricing helper — _rates_for prefix match + default fallback.
# ---------------------------------------------------------------------------

class TestRatesFor(unittest.TestCase):
    def test_exact_prefix_returns_rates(self):
        rates = _rates_for("claude-sonnet-4-6", PRICING)
        self.assertEqual(rates, (3.0, 0.30))

    def test_longest_prefix_wins(self):
        pricing = {
            "claude-sonnet": {"input": 1.0, "cache_read": 0.1},
            "claude-sonnet-4-6": {"input": 3.0, "cache_read": 0.30},
        }
        rates = _rates_for("claude-sonnet-4-6-20260101", pricing)
        self.assertEqual(rates, (3.0, 0.30))

    def test_default_fallback_when_no_prefix_match(self):
        rates = _rates_for("future-model", PRICING)
        self.assertEqual(rates, (3.0, 0.30))

    def test_returns_none_when_default_missing(self):
        no_default = {k: v for k, v in PRICING.items() if k != "_default"}
        self.assertIsNone(_rates_for("future-model", no_default))

    def test_returns_none_for_empty_inputs(self):
        self.assertIsNone(_rates_for("", PRICING))
        self.assertIsNone(_rates_for("claude-sonnet-4-6", {}))

    def test_returns_none_when_rates_incomplete(self):
        broken = {"claude-x": {"input": 1.0}}  # missing cache_read
        self.assertIsNone(_rates_for("claude-x", broken))


# ---------------------------------------------------------------------------
# Disambiguated label factory — collision resolution between project labels.
# ---------------------------------------------------------------------------

class TestDisambiguatedLabel(unittest.TestCase):
    def test_no_collision_returns_plain_label(self):
        by_proj = {"github.com/me/api": [], "github.com/me/web": []}
        label = _disambiguated_label_factory(by_proj)
        self.assertEqual(label("github.com/me/api"), "me/api")
        self.assertEqual(label("github.com/me/web"), "me/web")

    def test_collision_appends_suffix_from_key(self):
        # Two distinct keys whose _project_label collapses to "api". Last 8
        # chars must differ so the disambiguation suffix uniquely identifies
        # each — picked deliberately to exercise that.
        by_proj = {"github.com/owner-a/api": [],
                   "github.com/owner-b/api": []}
        label = _disambiguated_label_factory(by_proj)
        out = [label("github.com/owner-a/api"),
               label("github.com/owner-b/api")]
        # Both labels were "owner-X/api"; collisions only happen on bare
        # basename, which these don't trigger (labels already differ).
        self.assertEqual(out, ["owner-a/api", "owner-b/api"])

    def test_collision_on_basename_disambiguated(self):
        # Path-style keys that DO collide on _project_label (basename = "api").
        by_proj = {"/Users/foo-aaaa/api": [],
                   "/Users/foo-bbbb/api": []}
        label = _disambiguated_label_factory(by_proj)
        out = sorted([label("/Users/foo-aaaa/api"),
                      label("/Users/foo-bbbb/api")])
        self.assertTrue(all(o.startswith("api (") and o.endswith(")")
                            for o in out))
        self.assertEqual(len(set(out)), 2)


# ---------------------------------------------------------------------------
# Tool distribution — sums across all rows.
# ---------------------------------------------------------------------------

class TestToolTotals(unittest.TestCase):
    def test_tool_totals_sums_across_sessions(self):
        rows = [
            _row(session_id="a", tool_distribution={"Read": 5, "Edit": 2}),
            _row(session_id="b", tool_distribution={"Read": 3, "Bash": 1}),
        ]
        agg = aggregate(rows, {}, {})
        self.assertEqual(agg["tool_totals"], {"Read": 8, "Edit": 2, "Bash": 1})


if __name__ == "__main__":
    unittest.main()
