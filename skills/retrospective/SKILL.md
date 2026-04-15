---
name: retrospective
description: Capture subjective session metrics (corrections, skill trigger accuracy, task outcome) and append to ~/.claude/metrics/retro.jsonl. Manual invocation only — run at the end of a Claude Code session.
disable-model-invocation: true
---

# /retrospective

Log how the session actually went. Joins to `auto.jsonl` by `session_id`.

## Steps

1. **Resolve session_id.** If the user passed an argument, use it. Otherwise read the last line of `~/.claude/metrics/auto.jsonl` with `tail -n1` and extract `session_id`. Ask the user to confirm: "Log retro for session `<id>` (ended `<ts>`)? (y/n)". If `n`, ask for the session_id explicitly.

2. **Ask three questions, one at a time.** Do not batch. Wait for each answer before asking the next.
   - **Q1: correction_rate (0-10).** "On a scale of 0-10, how often did you correct, redirect, or undo my work? 0 = never, 10 = constantly."
   - **Q2: skill_trigger_accuracy (good | bad | n-a).** "Did the right skills and agents fire at the right times?"
   - **Q3: task_outcome (shipped | blocked | abandoned | partial).** "What was the outcome?"
   - **Q4 (optional): notes.** "Any short note to remember? (press enter to skip)"

3. **Append to `~/.claude/metrics/retro.jsonl`.** Use this exact shell pattern (the heredoc inside python protects against quoting issues in notes):

   ```bash
   python3 - <<'PY'
   import fcntl, json
   from datetime import datetime, timezone
   from pathlib import Path

   row = {
       "schema_version": 1,
       "session_id": "<ID>",
       "ts": datetime.now(timezone.utc).isoformat(),
       "correction_rate": <0-10>,
       "skill_trigger_accuracy": "<good|bad|n-a>",
       "task_outcome": "<shipped|blocked|abandoned|partial>",
       "notes": "<optional>",
   }
   metrics = Path.home() / ".claude" / "metrics"
   metrics.mkdir(parents=True, exist_ok=True)
   lock = metrics / ".retro.lock"
   out = metrics / "retro.jsonl"
   with lock.open("a") as l:
       fcntl.flock(l.fileno(), fcntl.LOCK_EX)
       with out.open("a") as f:
           f.write(json.dumps(row) + "\n")
   print(f"Logged retro for {row['session_id']}")
   PY
   ```

   Drop the `notes` key entirely if empty.

4. **Confirm and stop.** Output a one-line confirmation with the session_id. Do not summarise the whole session.

## Rules

- Never invent answers. Always ask the user.
- Validate inputs before writing: correction_rate must be 0-10 int, skill_trigger_accuracy must be one of `good|bad|n-a`, task_outcome must be one of `shipped|blocked|abandoned|partial`. If invalid, re-ask.
- Never write if `session_id` is `unknown` — warn and stop.
