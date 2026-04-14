"""Integration tests for scripts/install.sh and scripts/uninstall.sh.

Runs the real scripts with an isolated HOME so ~/.claude is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install.sh"
UNINSTALL = REPO / "scripts" / "uninstall.sh"
TARGET_CMD = "python3 ~/.claude/hooks/session_end.py"


def run(script: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=env, timeout=20,
    )


def read_settings(home: Path) -> dict:
    p = home / ".claude" / "settings.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def hook_commands(settings: dict) -> list[str]:
    groups = settings.get("hooks", {}).get("SessionEnd", []) or []
    return [h.get("command") for g in groups for h in (g.get("hooks") or [])]


class TestInstallCycle(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- install ------------------------------------------------------------

    def test_install_writes_files_and_registers_hook(self):
        res = run(INSTALL, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertTrue((self.home / ".claude/hooks/session_end.py").is_file())
        self.assertTrue((self.home / ".claude/skills/retrospective/SKILL.md").is_file())
        self.assertTrue((self.home / ".claude/skills/analyze-metrics/SKILL.md").is_file())
        self.assertTrue((self.home / ".claude/metrics/pricing.json").is_file())

        self.assertIn(TARGET_CMD, hook_commands(read_settings(self.home)))

    def test_install_preserves_other_settings(self):
        (self.home / ".claude/settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls:*)"]},
            "theme": "dark",
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}]
            },
        }))
        res = run(INSTALL, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

        s = read_settings(self.home)
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls:*)"])
        self.assertIn("PreToolUse", s["hooks"])
        self.assertIn(TARGET_CMD, hook_commands(s))

    def test_install_idempotent(self):
        run(INSTALL, self.home)
        run(INSTALL, self.home)
        cmds = hook_commands(read_settings(self.home))
        self.assertEqual(cmds.count(TARGET_CMD), 1)

    # --- uninstall ----------------------------------------------------------

    def test_uninstall_clean_wipes_install(self):
        run(INSTALL, self.home)
        res = run(UNINSTALL, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertFalse((self.home / ".claude/hooks/session_end.py").exists())
        self.assertFalse((self.home / ".claude/skills/retrospective").exists())
        self.assertFalse((self.home / ".claude/skills/analyze-metrics").exists())
        self.assertFalse((self.home / ".claude/metrics/pricing.json").exists())

        self.assertNotIn(TARGET_CMD, hook_commands(read_settings(self.home)))

    def test_uninstall_preserves_other_hooks(self):
        (self.home / ".claude/settings.json").write_text(json.dumps({
            "hooks": {
                "SessionEnd": [
                    {"hooks": [
                        {"type": "command", "command": "echo someone-else"},
                    ]}
                ],
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "echo pre"}]}
                ],
            },
            "theme": "dark",
        }))
        run(INSTALL, self.home)
        run(UNINSTALL, self.home)

        s = read_settings(self.home)
        self.assertEqual(s["theme"], "dark")
        self.assertIn("PreToolUse", s["hooks"])
        self.assertIn("echo someone-else", hook_commands(s))
        self.assertNotIn(TARGET_CMD, hook_commands(s))

    def test_uninstall_removes_empty_session_end_key(self):
        run(INSTALL, self.home)
        # Ensure our hook is the only SessionEnd entry.
        run(UNINSTALL, self.home)
        s = read_settings(self.home)
        self.assertNotIn("SessionEnd", s.get("hooks", {}))

    def test_uninstall_idempotent(self):
        run(INSTALL, self.home)
        run(UNINSTALL, self.home)
        res = run(UNINSTALL, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_roundtrip_install_uninstall_install(self):
        run(INSTALL, self.home)
        run(UNINSTALL, self.home)
        run(INSTALL, self.home)
        self.assertIn(TARGET_CMD, hook_commands(read_settings(self.home)))
        self.assertTrue((self.home / ".claude/hooks/session_end.py").is_file())

    # --- data handling ------------------------------------------------------

    def test_uninstall_keeps_metrics_data_by_default(self):
        run(INSTALL, self.home)
        metrics = self.home / ".claude/metrics"
        metrics.mkdir(exist_ok=True)
        (metrics / "auto.jsonl").write_text('{"session_id":"x"}\n')
        (metrics / "retro.jsonl").write_text('{"session_id":"x"}\n')

        run(UNINSTALL, self.home)

        self.assertTrue((metrics / "auto.jsonl").is_file())
        self.assertTrue((metrics / "retro.jsonl").is_file())

    def test_uninstall_purge_data_wipes_metrics_dir(self):
        run(INSTALL, self.home)
        metrics = self.home / ".claude/metrics"
        (metrics / "auto.jsonl").write_text('{"session_id":"x"}\n')

        run(UNINSTALL, self.home, "--purge-data")

        self.assertFalse(metrics.exists())

    def test_uninstall_rejects_unknown_arg(self):
        res = run(UNINSTALL, self.home, "--make-coffee")
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
