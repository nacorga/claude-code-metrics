"""Unit tests for hooks/session_end.py.

Run from repo root:
    python3 -m unittest discover tests/ -v

No external deps — stdlib unittest + subprocess only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "session_end.py"
AUTO_SCHEMA = REPO / "schema" / "auto.schema.json"

# Import the hook module directly for unit-testing pure helpers (no subprocess
# round-trip). Safe: session_end.py guards its main() behind __name__ check.
sys.path.insert(0, str(REPO / "hooks"))
import session_end  # noqa: E402

GIT_AVAILABLE = shutil.which("git") is not None


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
        self.assertEqual(row["schema_version"], 5)
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

    def test_correction_keywords_match_spanish_phrases(self):
        """Bilingual detector: Spanish corrections must count just like
        English ones, with anchored phrases avoiding false positives on
        common short tokens (`mal`, `roto`).

        Documented limitation: substring match has no negation awareness,
        so `no está mal` ("not bad", actually positive) WILL match the
        anchored `está mal` keyword. This is accepted — fixing it requires
        word-boundary regex on the hook's hot path. The matcher is a
        retro pre-fill heuristic, not a strict correction rate. /retrospective
        remains the source of truth for correction_rate."""
        transcript = self.home / "t.jsonl"
        entries = [
            # First user message — long enough not to count as a short
            # follow-up, just establishes the user_msg_count baseline.
            {"type": "user", "timestamp": "2026-04-14T10:00:00Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "Implementa el endpoint de login con JWT y un test que cubra el happy path completo."},
             ]}},
            make_assistant("claude-sonnet-4-5", "2026-04-14T10:00:10Z",
                           input_t=100, output_t=50, tools=["Read"]),
            # ES corrections — true positives (one match each):
            {"type": "user", "timestamp": "2026-04-14T10:00:20Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "deshaz eso"},
             ]}},
            {"type": "user", "timestamp": "2026-04-14T10:00:25Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "no es lo que pedí, rehaz"},
             ]}},
            {"type": "user", "timestamp": "2026-04-14T10:00:30Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "está roto, revierte el cambio"},
             ]}},
            # Documented FP: `no está mal` should be positive ("not bad"),
            # but our substring matcher catches `está mal` inside it. The
            # test pins the current behavior so a future word-boundary
            # refactor will surface here.
            {"type": "user", "timestamp": "2026-04-14T10:00:35Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "no está mal el approach"},
             ]}},
            # True negative: technical text mentioning excluded bare tokens
            # (`mal` inside `formal`, `error` excluded entirely).
            {"type": "user", "timestamp": "2026-04-14T10:00:40Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "El parser formal acepta este token sin error"},
             ]}},
        ]
        write_transcript(transcript, entries)

        run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "s-es",
            "transcript_path": str(transcript),
            "cwd": "/x",
        }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # 4 ES TPs (deshaz, no es lo que + rehaz, está roto + revierte,
        # no está mal — known FP). The negative-control message about
        # `formal` and `error` must NOT match.
        self.assertEqual(row["correction_keyword_hits"], 4)

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


class TestNormalizeOrigin(unittest.TestCase):
    """Pure unit tests for session_end._normalize_origin. No filesystem,
    no subprocess. Locks the SSH/HTTPS/auth/scheme normalisation contract
    against accidental drift."""

    def test_ssh_form(self):
        self.assertEqual(
            session_end._normalize_origin("git@github.com:foo/bar.git"),
            "github.com/foo/bar",
        )

    def test_https_form(self):
        self.assertEqual(
            session_end._normalize_origin("https://github.com/foo/bar.git"),
            "github.com/foo/bar",
        )

    def test_https_with_user_strips_auth(self):
        self.assertEqual(
            session_end._normalize_origin("https://user@github.com/foo/bar.git"),
            "github.com/foo/bar",
        )

    def test_https_with_token_strips_auth(self):
        self.assertEqual(
            session_end._normalize_origin("https://user:token@gitlab.com/g/h.git"),
            "gitlab.com/g/h",
        )

    def test_ssh_scheme(self):
        self.assertEqual(
            session_end._normalize_origin("ssh://git@github.com/foo/bar.git"),
            "github.com/foo/bar",
        )

    def test_already_normalized_passthrough(self):
        # 'github.com/foo/bar' has no scheme and no SSH ':' — should pass
        # through unchanged so the function is idempotent.
        self.assertEqual(
            session_end._normalize_origin("github.com/foo/bar"),
            "github.com/foo/bar",
        )

    def test_empty_input(self):
        self.assertEqual(session_end._normalize_origin(""), "")
        self.assertEqual(session_end._normalize_origin("   "), "")

    def test_unknown_form_falls_back(self):
        # file:// and other shapes we don't recognise: strip .git but
        # otherwise keep the input — never invent a value.
        self.assertEqual(
            session_end._normalize_origin("file:///srv/git/local.git"),
            "file:///srv/git/local",
        )


@unittest.skipUnless(GIT_AVAILABLE, "git not installed")
class TestCaptureGitMetadata(unittest.TestCase):
    """End-to-end via the hook: a real git repo in a tempdir, run the hook,
    inspect the row. Skipped if git is not on PATH (rare on CI but possible)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.auto_path = self.home / ".claude" / "metrics" / "auto.jsonl"
        self.repo_dir = Path(self.tmp.name) / "repo"
        self.repo_dir.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_dir),
            capture_output=True,
            check=True,
        )

    def _write_minimal_transcript(self) -> Path:
        transcript = self.home / "t.jsonl"
        write_transcript(transcript, [
            make_assistant("claude-sonnet-4-5", "2026-04-14T10:00:00Z",
                           input_t=100, output_t=50, tools=["Read"]),
        ])
        return transcript

    def _run_and_get_row(self, session_id: str, cwd: Path | str) -> dict:
        """Drive the hook with a minimal transcript, assert clean exit, and
        return the single auto.jsonl row for `session_id`. Surfacing
        returncode/stderr keeps failures actionable instead of bubbling up
        as a confusing IndexError on read_jsonl(...)[0]."""
        transcript = self._write_minimal_transcript()
        result = run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": str(cwd),
        }, self.home)
        self.assertEqual(
            result.returncode, 0,
            f"hook exited {result.returncode} for {session_id}\n"
            f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}",
        )
        rows = [r for r in read_jsonl(self.auto_path) if r["session_id"] == session_id]
        self.assertEqual(len(rows), 1, f"expected exactly one row for {session_id}")
        return rows[0]

    def test_repo_with_remote_captures_normalized_origin(self):
        self._git("init", "-q")
        self._git("remote", "add", "origin", "git@github.com:foo/bar.git")
        row = self._run_and_get_row("s-git-ssh", self.repo_dir)
        # git_root may be the repo dir or its realpath (macOS /private/var
        # symlink). Compare via resolve() to absorb that.
        self.assertEqual(Path(row["git_root"]).resolve(), self.repo_dir.resolve())
        self.assertEqual(row["git_remote_origin"], "github.com/foo/bar")

    def test_repo_without_remote_captures_root_only(self):
        self._git("init", "-q")
        row = self._run_and_get_row("s-git-noremote", self.repo_dir)
        self.assertEqual(Path(row["git_root"]).resolve(), self.repo_dir.resolve())
        self.assertEqual(row["git_remote_origin"], "")

    def test_outside_repo_yields_empty_strings(self):
        # Plain dir, never `git init`-ed. Both fields must be empty strings —
        # not null, not missing — so the row stays schema-valid.
        row = self._run_and_get_row("s-no-git", self.repo_dir)
        self.assertEqual(row["git_root"], "")
        self.assertEqual(row["git_remote_origin"], "")

    def test_nonexistent_cwd_yields_empty_strings(self):
        row = self._run_and_get_row("s-bad-cwd", "/this/does/not/exist")
        self.assertEqual(row["git_root"], "")
        self.assertEqual(row["git_remote_origin"], "")

    def test_https_remote_normalised(self):
        self._git("init", "-q")
        self._git("remote", "add", "origin",
                  "https://github.com/foo/bar.git")
        row = self._run_and_get_row("s-git-https", self.repo_dir)
        self.assertEqual(row["git_remote_origin"], "github.com/foo/bar")


class TestLogRotation(unittest.TestCase):
    """`log()` must cap hook.log at LOG_MAX_BYTES by rotating to hook.log.1.

    Rotation is the only growth bound on the debug log (the hook itself
    never raises and writes to it on every code path), so this is the
    health-of-the-app contract — not a cosmetic concern.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm_logrot_"))
        # Snapshot the module constants we'll patch so tearDown can restore
        # them — never mutate test state without a guaranteed restore path.
        self._orig_metrics_dir = session_end.METRICS_DIR
        self._orig_log_path = session_end.LOG_PATH
        self._orig_log_path_prev = session_end.LOG_PATH_PREV
        self._orig_max_bytes = session_end.LOG_MAX_BYTES
        session_end.METRICS_DIR = self.tmp
        session_end.LOG_PATH = self.tmp / "hook.log"
        session_end.LOG_PATH_PREV = self.tmp / "hook.log.1"
        # Tiny cap keeps tests fast without changing the rotation logic.
        session_end.LOG_MAX_BYTES = 256

    def tearDown(self):
        session_end.METRICS_DIR = self._orig_metrics_dir
        session_end.LOG_PATH = self._orig_log_path
        session_end.LOG_PATH_PREV = self._orig_log_path_prev
        session_end.LOG_MAX_BYTES = self._orig_max_bytes
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_under_threshold_does_not_rotate(self):
        session_end.log("first")
        session_end.log("second")
        self.assertTrue(session_end.LOG_PATH.is_file())
        self.assertFalse(session_end.LOG_PATH_PREV.exists())
        body = session_end.LOG_PATH.read_text()
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_over_threshold_rotates_to_prev(self):
        # Pre-fill log past the cap, then write a new line — that write must
        # observe the rotation and start fresh.
        session_end.LOG_PATH.write_text("x" * (session_end.LOG_MAX_BYTES + 1))
        old_size = session_end.LOG_PATH.stat().st_size
        session_end.log("after-rotate")
        self.assertTrue(session_end.LOG_PATH_PREV.is_file())
        self.assertEqual(session_end.LOG_PATH_PREV.stat().st_size, old_size)
        new_body = session_end.LOG_PATH.read_text()
        self.assertIn("after-rotate", new_body)
        # New file should contain only the post-rotation line, not the old
        # bytes.
        self.assertNotIn("x" * 100, new_body)

    def test_existing_prev_is_overwritten(self):
        (session_end.LOG_PATH_PREV).write_text("STALE-PREV-CONTENT")
        session_end.LOG_PATH.write_text("y" * (session_end.LOG_MAX_BYTES + 1))
        session_end.log("after-rotate")
        # os.replace must clobber the prior .1 without raising.
        self.assertNotIn("STALE-PREV-CONTENT",
                         session_end.LOG_PATH_PREV.read_text())
        self.assertIn("y" * 100, session_end.LOG_PATH_PREV.read_text())

    def test_log_never_raises_on_unwritable_dir(self):
        # Point at a path that cannot exist — log() must swallow any error
        # so it can never block the hook.
        session_end.METRICS_DIR = Path("/proc/0/cannot/create")
        session_end.LOG_PATH = session_end.METRICS_DIR / "hook.log"
        session_end.LOG_PATH_PREV = session_end.METRICS_DIR / "hook.log.1"
        # Must not raise.
        session_end.log("should be silently dropped")
        session_end.log_error("error path must also be silent")


class TestLogSeverity(unittest.TestCase):
    """`log()` is INFO; `log_error()` is ERROR. The /analyze-metrics health
    signal counts only ERROR — pinning the format here keeps the hook and the
    helper in lockstep."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm_logsev_"))
        self._orig_metrics_dir = session_end.METRICS_DIR
        self._orig_log_path = session_end.LOG_PATH
        self._orig_log_path_prev = session_end.LOG_PATH_PREV
        session_end.METRICS_DIR = self.tmp
        session_end.LOG_PATH = self.tmp / "hook.log"
        session_end.LOG_PATH_PREV = self.tmp / "hook.log.1"

    def tearDown(self):
        session_end.METRICS_DIR = self._orig_metrics_dir
        session_end.LOG_PATH = self._orig_log_path
        session_end.LOG_PATH_PREV = self._orig_log_path_prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_writes_info_tag(self):
        session_end.log("normal event")
        body = session_end.LOG_PATH.read_text()
        self.assertIn("[INFO] normal event", body)
        self.assertNotIn("[ERROR]", body)

    def test_log_error_writes_error_tag(self):
        session_end.log_error("real failure")
        body = session_end.LOG_PATH.read_text()
        self.assertIn("[ERROR] real failure", body)

    def test_line_format_starts_with_iso_timestamp(self):
        # Format contract: [<iso8601>] [<LEVEL>] <message>. The helper
        # _recent_log_errors parses this exact shape.
        session_end.log_error("boom")
        line = session_end.LOG_PATH.read_text().splitlines()[0]
        # Cheap structural check: opens with '[', has ']', then ' [LEVEL] '.
        self.assertTrue(line.startswith("["), line)
        self.assertIn("] [ERROR] ", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
