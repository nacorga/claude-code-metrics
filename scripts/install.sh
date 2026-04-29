#!/usr/bin/env bash
# claude-code-metrics installer.
# Idempotent: safe to run multiple times. Preserves existing hooks/settings.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

HOOKS_DIR="$CLAUDE_HOME/hooks"
SKILLS_DIR="$CLAUDE_HOME/skills"
METRICS_DIR="$CLAUDE_HOME/metrics"
SETTINGS="$CLAUDE_HOME/settings.json"

printf '\n→ installing claude-code-metrics to %s\n' "$CLAUDE_HOME"

mkdir -p "$HOOKS_DIR" "$SKILLS_DIR" "$METRICS_DIR"

cp "$REPO_DIR/hooks/session_end.py" "$HOOKS_DIR/session_end.py"
chmod +x "$HOOKS_DIR/session_end.py"
printf '  ✓ hook installed → %s\n' "$HOOKS_DIR/session_end.py"

# Wipe any prior copy before re-copying — `cp -R` MERGES into existing dirs,
# which silently leaves stale files (renamed or removed in the source) behind
# and, in pathological past states, can leave nested skill subdirectories.
# Removing first makes install truly idempotent regardless of prior state.
for skill in retrospective analyze-metrics metrics-report; do
  rm -rf "$SKILLS_DIR/$skill"
  cp -R "$REPO_DIR/skills/$skill" "$SKILLS_DIR/"
done
printf '  ✓ skills installed → %s/{retrospective,analyze-metrics,metrics-report}\n' "$SKILLS_DIR"

cp "$REPO_DIR/config/pricing.json" "$METRICS_DIR/pricing.json"
printf '  ✓ pricing config → %s/pricing.json\n' "$METRICS_DIR"

python3 - <<PY
import json
from pathlib import Path

settings = Path("$SETTINGS")
settings.parent.mkdir(parents=True, exist_ok=True)

if settings.is_file() and settings.stat().st_size > 0:
    try:
        data = json.loads(settings.read_text())
    except json.JSONDecodeError:
        print("  ! settings.json is not valid JSON — aborting patch; install manually.")
        raise SystemExit(2)
else:
    data = {}

hooks = data.setdefault("hooks", {})
session_end = hooks.setdefault("SessionEnd", [])

target_cmd = "python3 ~/.claude/hooks/session_end.py"

already = False
for group in session_end:
    for h in (group.get("hooks") or []):
        if h.get("type") == "command" and h.get("command") == target_cmd:
            already = True

if not already:
    session_end.append({
        "hooks": [{"type": "command", "command": target_cmd}]
    })
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print("  ✓ hook registered in settings.json")
else:
    print("  · hook already registered — no change")
PY

printf '\n✓ done. Restart Claude Code to activate the SessionEnd hook.\n'
printf '  Metrics will land in %s/{auto,retro}.jsonl\n' "$METRICS_DIR"
printf '  Run /retrospective at session end to capture subjective metrics.\n'
printf '  Run /analyze-metrics any time for a markdown report.\n'
printf '  Run /metrics-report for a static HTML report (no JS, no CDN).\n\n'
