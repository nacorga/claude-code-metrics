#!/usr/bin/env bash
# claude-code-metrics uninstaller.
# Idempotent. Preserves unrelated hooks/settings and user data by default.
#
# Usage:
#   bash scripts/uninstall.sh              # remove hook + skills, keep metrics
#   bash scripts/uninstall.sh --purge-data # also wipe ~/.claude/metrics/
set -euo pipefail

PURGE_DATA=0
for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA=1 ;;
    -h|--help)
      sed -n '2,8p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      printf 'unknown arg: %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done

CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE_HOME/hooks"
SKILLS_DIR="$CLAUDE_HOME/skills"
METRICS_DIR="$CLAUDE_HOME/metrics"
SETTINGS="$CLAUDE_HOME/settings.json"

printf '\n→ uninstalling claude-code-metrics from %s\n' "$CLAUDE_HOME"

rm -f "$HOOKS_DIR/session_end.py"           && printf '  ✓ removed hook script\n' || true
rm -rf "$SKILLS_DIR/retrospective"          && printf '  ✓ removed /retrospective skill\n' || true
rm -rf "$SKILLS_DIR/analyze-metrics"        && printf '  ✓ removed /analyze-metrics skill\n' || true
rm -rf "$SKILLS_DIR/metrics-report"         && printf '  ✓ removed /metrics-report skill\n' || true
rm -f "$METRICS_DIR/pricing.json"           && printf '  ✓ removed pricing config\n' || true

if [ -f "$SETTINGS" ]; then
  python3 - <<PY
import json
from pathlib import Path

settings = Path("$SETTINGS")
try:
    data = json.loads(settings.read_text())
except json.JSONDecodeError:
    print("  ! settings.json is not valid JSON — skipping hook removal; remove manually.")
    raise SystemExit(0)

target_cmd = "python3 ~/.claude/hooks/session_end.py"
hooks = data.get("hooks") or {}
session_end = hooks.get("SessionEnd") or []

removed = 0
new_groups = []
for group in session_end:
    kept = [h for h in (group.get("hooks") or [])
            if not (h.get("type") == "command" and h.get("command") == target_cmd)]
    dropped = len(group.get("hooks") or []) - len(kept)
    removed += dropped
    if kept:
        new_group = {**group, "hooks": kept}
        new_groups.append(new_group)

if removed == 0:
    print("  · no matching hook entry in settings.json — no change")
    raise SystemExit(0)

if new_groups:
    hooks["SessionEnd"] = new_groups
else:
    hooks.pop("SessionEnd", None)

if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)

settings.write_text(json.dumps(data, indent=2) + "\n")
print(f"  ✓ removed {removed} hook entr{'y' if removed == 1 else 'ies'} from settings.json")
PY
else
  printf '  · no settings.json found — nothing to patch\n'
fi

if [ "$PURGE_DATA" -eq 1 ]; then
  if [ -d "$METRICS_DIR" ]; then
    rm -rf "$METRICS_DIR"
    printf '  ✓ purged metrics data at %s\n' "$METRICS_DIR"
  fi
else
  if [ -d "$METRICS_DIR" ]; then
    printf '  · kept metrics data at %s (use --purge-data to wipe)\n' "$METRICS_DIR"
  fi
fi

printf '\n✓ done. Restart Claude Code to unload the hook.\n\n'
