"""Unit tests for v4 subagent return-surface metrics.

Run from repo root:
    python3 -m unittest discover tests/ -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "session_end.py"


def run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "CCM_NO_FORK": "1"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def assistant_with_agent(ts: str, agent_id: str, subagent_type: str,
                         extra_blocks: list[dict] | None = None) -> dict:
    """Build an assistant entry that dispatches one Agent (Task) call."""
    content = [
        {"type": "tool_use", "id": agent_id, "name": "Agent",
         "input": {"subagent_type": subagent_type, "description": "go"}},
    ]
    if extra_blocks:
        content.extend(extra_blocks)
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 50, "output_tokens": 25,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
            "content": content,
        },
    }


def tool_result_user(ts: str, tool_use_id: str, text: str,
                     is_error: bool = False) -> dict:
    """Synthetic user entry carrying a tool_result for an Agent dispatch."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result",
                 "tool_use_id": tool_use_id,
                 "is_error": is_error,
                 "content": [{"type": "text", "text": text}]},
            ],
        },
    }


class TestSubagentStats(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.auto_path = self.home / ".claude" / "metrics" / "auto.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, entries: list[dict], session_id: str = "s") -> dict:
        transcript = self.home / f"{session_id}.jsonl"
        write_transcript(transcript, entries)
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = [r for r in read_jsonl(self.auto_path) if r["session_id"] == session_id]
        self.assertEqual(len(rows), 1, f"expected exactly one row for {session_id}")
        return rows[0]

    def test_schema_version_is_current_and_v4_fields_present(self):
        row = self._run([
            {"type": "user", "timestamp": "2026-04-14T10:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            assistant_with_agent("2026-04-14T10:00:01Z", "toolu_a", "Explore"),
            tool_result_user("2026-04-14T10:00:30Z", "toolu_a", "x" * 4000),
        ], "s-v4")
        # Schema is at v5 (project-identity fields). v4 return-surface fields
        # are still required and populated — additive.
        self.assertEqual(row["schema_version"], 5)
        self.assertIn("subagent_stats", row)
        self.assertIn("cheap_subagent_calls", row)

    def test_happy_dispatch_aggregates_chars_and_duration(self):
        row = self._run([
            assistant_with_agent("2026-04-14T10:00:00Z", "toolu_1", "Explore"),
            tool_result_user("2026-04-14T10:01:30Z", "toolu_1", "x" * 4000),
        ], "s-happy")
        stats = row["subagent_stats"]["Explore"]
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["return_chars_total"], 4000)
        self.assertEqual(stats["max_return_chars"], 4000)
        self.assertEqual(stats["errors"], 0)
        self.assertAlmostEqual(stats["duration_s_total"], 90.0)
        self.assertAlmostEqual(stats["max_duration_s"], 90.0)
        self.assertEqual(row["cheap_subagent_calls"], 0)

    def test_cheap_call_below_threshold(self):
        row = self._run([
            assistant_with_agent("2026-04-14T10:00:00Z", "toolu_2", "general-purpose"),
            tool_result_user("2026-04-14T10:00:05Z", "toolu_2", "x" * 50),
        ], "s-cheap")
        self.assertEqual(row["cheap_subagent_calls"], 1)
        self.assertEqual(row["subagent_stats"]["general-purpose"]["count"], 1)
        self.assertEqual(row["subagent_stats"]["general-purpose"]["return_chars_total"], 50)

    def test_error_dispatch_counted_as_error_and_call(self):
        row = self._run([
            assistant_with_agent("2026-04-14T10:00:00Z", "toolu_3", "Plan"),
            tool_result_user("2026-04-14T10:00:10Z", "toolu_3",
                             "tool failed: bad input", is_error=True),
        ], "s-err")
        stats = row["subagent_stats"]["Plan"]
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["errors"], 1)
        # tool_errors_count (session-wide) also catches it
        self.assertEqual(row["tool_errors_count"], 1)

    def test_parallel_dispatches_in_one_assistant_turn(self):
        """Two Agent tool_use blocks in a single assistant message, each with
        its own id and matching tool_result. Both must aggregate."""
        row = self._run([
            {
                "type": "assistant",
                "timestamp": "2026-04-14T10:00:00Z",
                "message": {
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 50, "output_tokens": 25,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0},
                    "content": [
                        {"type": "tool_use", "id": "toolu_p1", "name": "Agent",
                         "input": {"subagent_type": "Explore"}},
                        {"type": "tool_use", "id": "toolu_p2", "name": "Agent",
                         "input": {"subagent_type": "Explore"}},
                    ],
                },
            },
            tool_result_user("2026-04-14T10:00:20Z", "toolu_p1", "y" * 1000),
            tool_result_user("2026-04-14T10:00:40Z", "toolu_p2", "z" * 2000),
        ], "s-par")
        stats = row["subagent_stats"]["Explore"]
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["return_chars_total"], 3000)
        self.assertEqual(stats["max_return_chars"], 2000)
        # invocation counter also reflects both dispatches
        self.assertEqual(row["subagent_invocations"]["Explore"], 2)

    def test_orphan_dispatch_not_in_stats_but_counted_in_invocations(self):
        """Agent tool_use with no matching tool_result: session was abandoned
        mid-Task, or the result lives outside our parse window. Must not
        appear in subagent_stats but should still increment subagent_invocations."""
        row = self._run([
            assistant_with_agent("2026-04-14T10:00:00Z", "toolu_orphan", "Explore"),
            # No tool_result for toolu_orphan. Add a real assistant turn so the
            # session isn't classified as empty.
            {"type": "assistant", "timestamp": "2026-04-14T10:00:05Z",
             "message": {"model": "claude-sonnet-4-5",
                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0},
                         "content": [{"type": "text", "text": "thinking"}]}},
        ], "s-orphan")
        self.assertEqual(row["subagent_invocations"], {"Explore": 1})
        self.assertEqual(row["subagent_stats"], {},
                         "orphan dispatch must not pollute subagent_stats")
        self.assertEqual(row["cheap_subagent_calls"], 0)

    def test_string_content_in_tool_result(self):
        """tool_result.content can be a plain string (not a list of blocks).
        Char count must still work."""
        row = self._run([
            assistant_with_agent("2026-04-14T10:00:00Z", "toolu_s", "Explore"),
            {"type": "user", "timestamp": "2026-04-14T10:00:10Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_s",
                  "is_error": False, "content": "plain string of length 30!!!!!"},
             ]}},
        ], "s-str")
        self.assertEqual(row["subagent_stats"]["Explore"]["return_chars_total"], 30)

    def test_unknown_subagent_type_grouped_as_unknown(self):
        """Agent dispatch without a subagent_type input falls back to '<unknown>'
        for both invocations and stats."""
        row = self._run([
            {"type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
             "message": {"model": "claude-sonnet-4-5",
                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0},
                         "content": [
                             {"type": "tool_use", "id": "toolu_u", "name": "Agent",
                              "input": {}},
                         ]}},
            tool_result_user("2026-04-14T10:00:05Z", "toolu_u", "x" * 500),
        ], "s-unknown")
        self.assertIn("<unknown>", row["subagent_stats"])
        self.assertEqual(row["subagent_stats"]["<unknown>"]["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
