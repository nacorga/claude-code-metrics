---
name: retrospective
description: Capture subjective session metrics (corrections, skill trigger accuracy, task outcome) and append to ~/.claude/metrics/retro.jsonl. Manual invocation only — run at the end of a Claude Code session.
disable-model-invocation: true
---

# /retrospective

Log how the session actually went. Joins to `auto.jsonl` by `session_id`.

## Steps

1. **Resolve session_id.** If the user passed an argument, use it. Otherwise read the last line of `~/.claude/metrics/auto.jsonl` with `tail -n1` and extract `session_id`. Ask the user to confirm: "Log retro for session `<id>` (ended `<ts>`)? (y/n)". If `n`, ask for the session_id explicitly.

2. **Show context (v3+ rows only) and pre-suggest a correction_rate.** After resolving `session_id`, look up its row in `auto.jsonl` and, if available, surface the captured signals to the user before asking. This makes the retro 10s instead of a guess. Use this exact snippet:

   ```bash
   python3 - <<'PY'
   import json, sys
   from pathlib import Path
   sid = "<RESOLVED_SESSION_ID>"
   auto = Path.home() / ".claude" / "metrics" / "auto.jsonl"
   row = None
   if auto.is_file():
       for line in auto.read_text().splitlines():
           if not line.strip():
               continue
           try:
               r = json.loads(line)
           except Exception:
               continue
           if r.get("session_id") == sid:
               row = r  # last match wins (latest entry for the same id)
   if not row:
       print("_no auto row for this session — answer from memory_")
       sys.exit(0)
   cost = row.get("cost_usd")
   print(f"Session: model={row.get('model')} cost=${cost:.2f}" if cost is not None else f"Session: model={row.get('model')} cost=?")
   print(f"Turns: {row.get('turn_count')}  tool_calls: {row.get('tool_calls_total')}  duration_s: {row.get('duration_s')}")
   if "tool_errors_count" in row:
       print(f"Tool errors: {row.get('tool_errors_count')}")
       print(f"User msgs: {row.get('user_msg_count')}  short follow-ups: {row.get('short_user_followups_count')}  correction kw hits: {row.get('correction_keyword_hits')}")
       sub = row.get("subagent_invocations") or {}
       if sub:
           print(f"Subagents: {sub}")
       # Heuristic suggestion for correction_rate (only as a hint — user decides)
       hits = row.get("correction_keyword_hits") or 0
       fups = row.get("short_user_followups_count") or 0
       umsgs = max(row.get("user_msg_count") or 1, 1)
       ratio = (hits + fups) / umsgs
       suggested = min(10, round(ratio * 10))
       print(f"Suggested correction_rate (heuristic, override freely): {suggested}/10")
   else:
       print("_pre-v3 session: no friction signals captured_")
   PY
   ```

3. **Ask three questions, one at a time.** Do not batch. Wait for each answer before asking the next.
   - **Q1: correction_rate (0-10).** "On a scale of 0-10, how often did you correct, redirect, or undo my work? 0 = never, 10 = constantly." If a heuristic suggestion was shown above, present it as the default but never auto-fill — the user always confirms.
   - **Q2: skill_trigger_accuracy (good | bad | n-a).** "Did the right skills and agents fire at the right times?"
   - **Q3: task_outcome (shipped | blocked | abandoned | partial).** "What was the outcome?"
   - **Q4 (optional): notes.** "Any short note to remember? (press enter to skip)"

4. **Append to `~/.claude/metrics/retro.jsonl`.** Use this exact shell pattern (the heredoc inside python protects against quoting issues in notes):

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

5. **Confirm and stop.** Output a one-line confirmation with the session_id. Do not summarise the whole session.

## Rules

- Never invent answers. Always ask the user.
- Validate inputs before writing: correction_rate must be 0-10 int, skill_trigger_accuracy must be one of `good|bad|n-a`, task_outcome must be one of `shipped|blocked|abandoned|partial`. If invalid, re-ask.
- Never write if `session_id` is `unknown` — warn and stop.
