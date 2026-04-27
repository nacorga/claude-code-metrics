"""Unit tests for hooks/session_end.py.

Run from repo root:
    python3 -m unittest discover tests/ -v

No external deps — stdlib unittest + subprocess only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "session_end.py"
AUTO_SCHEMA = REPO / "schema" / "auto.schema.json"


def run_hook(payload: dict, home: Path, fork: bool = False) -> subprocess.CompletedProcess:
    """Run the hook with a synthetic stdin payload and an isolated HOME.

    By default tests run synchronously (CCM_NO_FORK=1) so the process exits
    after writing the row. Pass fork=True to exercise the detach path.
    """
    env = {**os.environ, "HOME": str(home)}
    if not fork:
        env["CCM_NO_FORK"] = "1"
    else:
        env.pop("CCM_NO_FORK", None)
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


def make_assistant(model: str, ts: str, input_t: int = 0, output_t: int = 0,
                   cache_w: int = 0, cache_r: int = 0, tools: list[str] | None = None) -> dict:
    content: list[dict] = []
    for t in tools or []:
        content.append({"type": "tool_use", "name": t})
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_creation_input_tokens": cache_w,
                "cache_read_input_tokens": cache_r,
            },
            "content": content,
        },
    }


class TestSessionEndHook(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.auto_path = self.home / ".claude" / "metrics" / "auto.jsonl"
        self.log_path = self.home / ".claude" / "metrics" / "hook.log"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- happy path ---------------------------------------------------------

    def test_happy_path_writes_valid_row(self):
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-04-14T10:00:00Z", "message": {"role": "user", "content": "hi"}},
            make_assistant("claude-sonnet-4-5", "2026-04-14T10:00:05Z",
                           input_t=1000, output_t=200, cache_w=500, cache_r=8000,
                           tools=["Read"]),
            make_assistant("claude-sonnet-4-5", "2026-04-14T10:02:30Z",
                           input_t=2000, output_t=400, cache_w=200, cache_r=9500,
                           tools=["Read", "Edit"]),
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-happy",
            "transcript_path": str(transcript),
            "cwd": "/tmp/demo",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema_version"], 4)
        self.assertEqual(row["session_id"], "s-happy")
        self.assertEqual(row["model"], "claude-sonnet-4-5")
        self.assertEqual(row["turn_count"], 2)
        self.assertEqual(row["tool_calls_total"], 3)
        self.assertEqual(row["tool_distribution"], {"Read": 2, "Edit": 1})
        self.assertEqual(row["input_tokens"], 3000)
        self.assertEqual(row["output_tokens"], 600)
        self.assertEqual(row["cache_creation_tokens"], 700)
        self.assertEqual(row["cache_read_tokens"], 17500)
        self.assertEqual(row["duration_s"], 150.0)
        self.assertGreater(row["cost_usd"], 0)
        self.assertEqual(row["end_reason"], "close")
        self.assertNotIn("error", row)
        # v3 signals: this fixture has one short benign user msg ("hi"), no
        # tool errors, no Agent calls, no correction keywords.
        self.assertEqual(row["user_msg_count"], 1)
        self.assertEqual(row["short_user_followups_count"], 0)
        self.assertEqual(row["tool_errors_count"], 0)
        self.assertEqual(row["subagent_invocations"], {})
        self.assertEqual(row["correction_keyword_hits"], 0)

    # --- skip conditions ----------------------------------------------------

    def test_compact_reason_skipped(self):
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-compact",
            "transcript_path": "/nonexistent",
            "reason": "compact",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists())

    def test_clear_source_skipped(self):
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-clear",
            "transcript_path": "/nonexistent",
            "source": "clear",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists())

    def test_wrong_event_ignored(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "s-wrong",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists())

    # --- defensive paths ----------------------------------------------------

    def test_missing_transcript_writes_no_row(self):
        """Ghost session: payload points at a file that was never written.
        Treated as silent skip (like /compact / /clear) — no row appended."""
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-miss",
            "transcript_path": "/does/not/exist",
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists(),
                         "ghost session must not produce a row")

    def test_zero_byte_transcript_writes_no_row(self):
        """Transcript file exists but is 0 bytes (flush race or abandoned
        session). Treated as ghost — silent skip."""
        transcript = self.home / "empty.jsonl"
        transcript.write_text("")
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-zerobyte",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists())

    def test_missing_transcript_path_writes_no_row(self):
        """Payload with no transcript_path key at all — also a ghost."""
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-nopath",
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.auto_path.exists())

    def test_corrupt_lines_are_skipped(self):
        transcript = self.home / "t.jsonl"
        transcript.write_text(
            "not-json\n"
            + json.dumps(make_assistant("claude-sonnet-4-5", "2026-04-14T10:00:00Z",
                                         input_t=100, output_t=50)) + "\n"
            + "{broken\n"
        )
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-corrupt",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(rows[0]["turn_count"], 1)
        self.assertEqual(rows[0]["input_tokens"], 100)

    # --- pricing fallbacks --------------------------------------------------

    def test_unknown_model_uses_default_and_logs(self):
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-future-99-0", "2026-01-01T00:00:00Z",
                           input_t=1_000_000, output_t=0),
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-unknown",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        # _default is $3/M input → 1M tokens = $3
        self.assertEqual(rows[0]["cost_usd"], 3.0)
        self.assertEqual(rows[0]["model"], "claude-future-99-0")
        self.assertTrue(self.log_path.is_file())
        self.assertIn("_default", self.log_path.read_text())

    def test_large_tokens_compute_correct_cost(self):
        transcript = self.home / "t.jsonl"
        # sonnet rates: input 3, output 15, cache_write 3.75, cache_read 0.30 per 1M
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z",
                           input_t=2_000_000, output_t=500_000,
                           cache_w=100_000, cache_r=10_000_000),
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-big",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        expected = 2 * 3.0 + 0.5 * 15.0 + 0.1 * 3.75 + 10 * 0.30  # 16.875
        self.assertAlmostEqual(rows[0]["cost_usd"], expected, places=4)

    # --- detach path --------------------------------------------------------

    def test_fork_parent_returns_fast_and_grandchild_writes(self):
        """Without CCM_NO_FORK the parent double-forks and returns fast;
        the detached grandchild still writes the row."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z",
                           input_t=100, output_t=50, tools=["Read"]),
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-fork",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }

        start = time.monotonic()
        res = run_hook(payload, self.home, fork=True)
        elapsed = time.monotonic() - start

        self.assertEqual(res.returncode, 0, res.stderr)
        # Parent must return in well under 1s even with a transcript to parse.
        self.assertLess(elapsed, 1.0,
                        f"parent blocked {elapsed:.2f}s — detach is broken")

        # Wait up to 3s for the grandchild to finish writing.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.auto_path.is_file() and self.auto_path.stat().st_size > 0:
                break
            time.sleep(0.05)

        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1, "grandchild did not write row")
        self.assertEqual(rows[0]["session_id"], "s-fork")
        self.assertEqual(rows[0]["input_tokens"], 100)

    def test_fork_returns_fast_with_large_transcript(self):
        """Regression guard: detach must absorb long parse work. A 2000-entry
        transcript takes ~seconds to parse synchronously, but the parent
        process must still return in well under 0.5s."""
        transcript = self.home / "big.jsonl"
        entries = [
            make_assistant("claude-sonnet-4-5",
                           f"2026-01-01T00:00:{i % 60:02d}Z",
                           input_t=100, output_t=50, tools=["Grep"])
            for i in range(2000)
        ]
        write_transcript(transcript, entries)
        # Sanity: transcript should be meaningful in size (>300KB)
        self.assertGreater(transcript.stat().st_size, 300_000)

        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-big-fork",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }

        start = time.monotonic()
        res = run_hook(payload, self.home, fork=True)
        elapsed = time.monotonic() - start

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(elapsed, 0.5,
                        f"parent blocked {elapsed:.2f}s on 2000-entry transcript "
                        f"— detach did not decouple parse time")

        # Grandchild may take longer; wait up to 10s.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.auto_path.is_file() and self.auto_path.stat().st_size > 0:
                break
            time.sleep(0.1)

        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_count"], 2000)
        self.assertEqual(rows[0]["input_tokens"], 200_000)

    # --- schema drift guard -------------------------------------------------

    def test_happy_row_has_all_schema_required_fields(self):
        """Whatever the hook writes must satisfy schema/auto.schema.json's
        required[]. Catches silent drift when we add/remove row fields."""
        schema = json.loads(AUTO_SCHEMA.read_text())
        required = set(schema.get("required", []))
        allowed = set(schema.get("properties", {}).keys())

        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z",
                           input_t=100, output_t=50, tools=["Read"]),
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-schema",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        row = read_jsonl(self.auto_path)[0]

        missing = required - set(row.keys())
        unknown = set(row.keys()) - allowed
        self.assertFalse(missing, f"row missing required fields: {missing}")
        self.assertFalse(unknown, f"row has fields not in schema: {unknown}")

    def test_error_row_has_all_schema_required_fields(self):
        """Error path must also satisfy the schema. Triggered via
        empty_transcript (file exists but no assistant turns), since ghost
        sessions (missing/zero-byte file) no longer produce rows."""
        schema = json.loads(AUTO_SCHEMA.read_text())
        required = set(schema.get("required", []))
        allowed = set(schema.get("properties", {}).keys())

        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": "hi"}},
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-err-schema",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        row = read_jsonl(self.auto_path)[0]

        missing = required - set(row.keys())
        unknown = set(row.keys()) - allowed
        self.assertFalse(missing, f"error row missing required fields: {missing}")
        self.assertFalse(unknown, f"error row has fields not in schema: {unknown}")

    # --- concurrency proxy --------------------------------------------------

    def test_two_sequential_runs_append_both_rows(self):
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z", input_t=100),
        ])
        for sid in ("s-a", "s-b"):
            run_hook({
                "hook_event_name": "SessionEnd",
                "session_id": sid,
                "transcript_path": str(transcript),
                "cwd": "/x",
            }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["session_id"] for r in rows}, {"s-a", "s-b"})

    # --- dedupe -------------------------------------------------------------

    def test_duplicate_session_id_is_skipped(self):
        """If a session_id already has a row, a second call must not append."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z",
                           input_t=100, output_t=50, tools=["Read"]),
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-dupe",
            "transcript_path": str(transcript),
            "cwd": "/first",
        }
        run_hook(payload, self.home)
        payload["cwd"] = "/second"
        run_hook(payload, self.home)

        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1, "dedupe failed: duplicate row appended")
        self.assertEqual(rows[0]["cwd"], "/first", "first-write-wins violated")

    def test_unknown_session_id_not_deduped(self):
        """session_id='unknown' must never dedupe against itself — otherwise
        every error row after the first would silently vanish. Triggered via
        empty_transcript so rows actually get written (ghost sessions now
        silently skip)."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": "hi"}},
        ])
        payload = {
            "hook_event_name": "SessionEnd",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }
        run_hook(payload, self.home)
        run_hook(payload, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["session_id"] == "unknown" for r in rows))
        self.assertTrue(all(r["error"] == "empty_transcript" for r in rows))

    # --- duration cap -------------------------------------------------------

    def test_implausible_duration_is_nulled(self):
        """first_ts to last_ts > 24h must null duration_s (stale transcript)."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z", input_t=100),
            make_assistant("claude-sonnet-4-5", "2026-01-03T00:00:00Z", input_t=100),
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-long",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertIsNone(rows[0]["duration_s"],
                          "duration_s > 24h should be nulled")
        self.assertEqual(rows[0]["turn_count"], 2)

    def test_short_duration_preserved(self):
        """Sanity: durations under the cap are untouched."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z", input_t=100),
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:10:00Z", input_t=100),
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-short",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(rows[0]["duration_s"], 600.0)

    # --- v3 conversation-shape signals --------------------------------------

    def test_v3_signals_captured_from_full_transcript(self):
        """End-to-end check that the five v3 signals are extracted correctly:
        tool_errors_count, subagent_invocations, user_msg_count,
        short_user_followups_count, correction_keyword_hits."""
        transcript = self.home / "t.jsonl"
        long_first_prompt = "Please audit the codebase and tell me " + ("x" * 250)

        entries = [
            # First user message (long → not a follow-up).
            {"type": "user", "timestamp": "2026-04-14T10:00:00Z",
             "message": {"role": "user", "content": long_first_prompt}},
            # Assistant dispatches an Explore subagent.
            {"type": "assistant", "timestamp": "2026-04-14T10:00:05Z",
             "message": {
                 "model": "claude-sonnet-4-5",
                 "usage": {"input_tokens": 100, "output_tokens": 50,
                           "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0},
                 "content": [
                     {"type": "tool_use", "name": "Agent",
                      "input": {"subagent_type": "Explore",
                                "description": "map repo"}},
                     {"type": "tool_use", "name": "Read"},
                 ],
             }},
            # Synthetic user entry with a tool_error.
            {"type": "user", "timestamp": "2026-04-14T10:00:10Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "x", "is_error": True,
                  "content": "tool failed"},
             ]}},
            # Short user follow-up — should count as steering nudge.
            {"type": "user", "timestamp": "2026-04-14T10:00:15Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "undo that, the change is wrong"},
             ]}},
            # Another assistant turn with a second Agent dispatch.
            {"type": "assistant", "timestamp": "2026-04-14T10:00:20Z",
             "message": {
                 "model": "claude-sonnet-4-5",
                 "usage": {"input_tokens": 50, "output_tokens": 25,
                           "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0},
                 "content": [
                     {"type": "tool_use", "name": "Agent",
                      "input": {"subagent_type": "Plan",
                                "description": "plan fix"}},
                 ],
             }},
            # System-injected user prompt — must NOT count toward followups.
            {"type": "user", "timestamp": "2026-04-14T10:00:25Z",
             "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
            # Another short user follow-up with a correction keyword.
            {"type": "user", "timestamp": "2026-04-14T10:00:30Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "this is incorrect, please redo"},
             ]}},
        ]
        write_transcript(transcript, entries)

        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-v3",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # 4 user-text msgs total: long_first + "undo that..." + system + "this is incorrect..."
        # System-injected starts with "<" → still counts as a user msg
        # (it has user-authored shape) but is excluded from short follow-up tally.
        self.assertEqual(row["user_msg_count"], 4)
        # Short followups (user_msg_count > 1, len < 200, not system-injected):
        # "undo that, the change is wrong" + "this is incorrect, please redo" = 2
        self.assertEqual(row["short_user_followups_count"], 2)
        # 1 tool_result with is_error=true
        self.assertEqual(row["tool_errors_count"], 1)
        # 2 Agent calls: Explore + Plan
        self.assertEqual(row["subagent_invocations"], {"Explore": 1, "Plan": 1})
        # Correction keyword hits: "undo that, the change is wrong" matches
        # ("undo", "wrong"); "this is incorrect, please redo" matches
        # ("incorrect", "redo"). Counted per matching message → 2.
        self.assertEqual(row["correction_keyword_hits"], 2)
        # Sanity on existing aggregates
        self.assertEqual(row["turn_count"], 2)
        self.assertEqual(row["tool_distribution"], {"Agent": 2, "Read": 1})
        self.assertEqual(row["tool_calls_total"], 3)

    def test_v3_signals_zero_on_assistant_only_transcript(self):
        """Sessions with no user-authored text and no tool errors should
        still emit the v3 fields with zero/empty defaults."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-01-01T00:00:00Z",
                           input_t=100, output_t=50, tools=["Read"]),
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-v3-zero",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        row = read_jsonl(self.auto_path)[0]
        self.assertEqual(row["user_msg_count"], 0)
        self.assertEqual(row["short_user_followups_count"], 0)
        self.assertEqual(row["tool_errors_count"], 0)
        self.assertEqual(row["subagent_invocations"], {})
        self.assertEqual(row["correction_keyword_hits"], 0)

    def test_v3_signals_present_on_error_row(self):
        """Error rows (empty_transcript / parse_crash) must include v3 fields
        as zero/empty so the schema stays valid."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": "hi"}},
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-v3-err",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        row = read_jsonl(self.auto_path)[0]
        self.assertEqual(row["error"], "empty_transcript")
        self.assertEqual(row["tool_errors_count"], 0)
        self.assertEqual(row["subagent_invocations"], {})
        self.assertEqual(row["user_msg_count"], 0)
        self.assertEqual(row["short_user_followups_count"], 0)
        self.assertEqual(row["correction_keyword_hits"], 0)

    def test_empty_transcript_is_marked_error(self):
        """Transcript with timestamps but zero assistant turns must be
        error='empty_transcript' so /analyze-metrics excludes it."""
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": "hi"}},
        ])
        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-empty",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "empty_transcript")
        self.assertEqual(rows[0]["turn_count"], 0)
        self.assertIsNone(rows[0]["cost_usd"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
