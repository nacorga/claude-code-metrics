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
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "session_end.py"


def run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    """Run the hook with a synthetic stdin payload and an isolated HOME."""
    env = {**os.environ, "HOME": str(home)}
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
            "permission_mode": "default",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema_version"], 1)
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

    def test_missing_transcript_writes_error_row(self):
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-miss",
            "transcript_path": "/does/not/exist",
            "cwd": "/x",
            "permission_mode": "plan",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "no_transcript")
        self.assertIsNone(rows[0]["cost_usd"])

    def test_empty_transcript_writes_error_row(self):
        transcript = self.home / "empty.jsonl"
        transcript.write_text("")
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "s-empty",
            "transcript_path": str(transcript),
            "cwd": "/x",
            "permission_mode": "default",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(rows[0]["error"], "no_transcript")

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
            "permission_mode": "default",
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
            "permission_mode": "default",
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
            "permission_mode": "default",
        }
        res = run_hook(payload, self.home)
        self.assertEqual(res.returncode, 0)
        rows = read_jsonl(self.auto_path)
        expected = 2 * 3.0 + 0.5 * 15.0 + 0.1 * 3.75 + 10 * 0.30  # 16.875
        self.assertAlmostEqual(rows[0]["cost_usd"], expected, places=4)

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
                "permission_mode": "default",
            }, self.home)
        rows = read_jsonl(self.auto_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["session_id"] for r in rows}, {"s-a", "s-b"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
