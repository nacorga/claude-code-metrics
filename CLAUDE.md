# CLAUDE.md

Guidance for Claude Code (and contributors) working in this repo. See [README.md](./README.md) for the user-facing docs.

## Invariants (never violate without an ADR-level discussion)

- **JSONL-only.** No SQLite, no embedded DB, no server, no dashboard. Output is plain JSON lines the user can `grep`, `jq`, pipe into a notebook. *(One-shot derivative artefacts that read JSONL — markdown, CSV, static self-contained HTML — are fine; long-running processes and live dashboards are not.)*
- **Zero runtime deps.** Hook uses stdlib Python 3.9+. Installer uses bash + python3. If a feature needs a dep, reconsider the feature.
- **Always local.** The hook must never make a network call. No telemetry, no remote reporting, no "opt-in analytics".
- **Never block Claude Code.** The hook must always `exit 0`, even on crashes. Any failure logs to `~/.claude/metrics/hook.log` and moves on.
- **Return to the shell in <100ms.** Heavy work (transcript parsing) runs in a double-forked, detached grandchild. Large sessions (60MB+ transcripts) cannot stall session close. Set `CCM_NO_FORK=1` for synchronous execution (tests).
- **Append-only.** We never rewrite `auto.jsonl` / `retro.jsonl`. Migrations happen by bumping `schema_version` and letting old + new rows coexist.
- **Repo SKILL.md is ground truth, not the installed copy.** `~/.claude/skills/{retrospective,analyze-metrics,metrics-report}/SKILL.md` are install artefacts. Edits made directly there are lost on the next `scripts/install.sh`. Always edit the repo source and re-install.

## Critical files

| Path                                  | Purpose                                                         |
| ------------------------------------- | --------------------------------------------------------------- |
| `hooks/session_end.py`                | Core hook. Parses transcript, estimates cost, appends JSONL row |
| `config/pricing.json`                 | Model pricing table (prefix match + `_default` fallback)        |
| `skills/retrospective/SKILL.md`       | Manual `/retrospective` — captures subjective metrics           |
| `skills/analyze-metrics/SKILL.md`     | Manual `/analyze-metrics` — markdown report from local JSONL    |
| `skills/analyze-metrics/_helpers.py`  | Shared filter / project-identity helpers (also used by `/metrics-report`) |
| `skills/metrics-report/SKILL.md`      | Manual `/metrics-report` — invokes `_render.py`                 |
| `skills/metrics-report/_render.py`    | Static HTML report generator. Stdlib-only, no JS, no CDN        |
| `scripts/install.sh` / `uninstall.sh` | Idempotent; patch `~/.claude/settings.json` via Python (json)   |
| `schema/{auto,retro}.schema.json`     | JSON Schema docs. Source of truth for row shape                 |
| `tests/`                              | Stdlib `unittest`. Zero external deps. Must stay green          |

## Non-negotiable rules

- **Bump `schema_version`** whenever you change the shape of a JSONL row. Update `schema/*.schema.json` in the same change. Add a migration note in the schema file.
- **Tests are the only QA gate.** No manual "I tested it" — write a `unittest` case. All cases must pass: `python3 -m unittest discover tests/ -v`.
- **Cross-platform in hook + tests.** `fcntl` restricts us to Unix (macOS + Linux). Windows is explicitly out of scope for v0.x. Never add code that silently breaks on macOS (e.g. GNU-only `date` flags).
- **Installer patches settings.json via Python's `json` module.** Never regex-replace JSON. Always preserve other hooks and unrelated keys.
- **Preserve user data on uninstall by default.** Only `--purge-data` may delete `~/.claude/metrics/` contents.
- **No fabricated metrics.** The Claude Code transcript records `usage` only for the main agent; tokens consumed *inside* a subagent's own context window are not exposed. We measure subagent cost indirectly via the **return surface** (`return_chars_total`, `duration_s_total`, `errors`) — never by inventing per-subagent token figures.

## Workflow

- **Before committing:** `python3 -m unittest discover tests/ -v` (must be 90+ green), then `bash -n scripts/*.sh`.
- **Adding a metric:** decide auto vs retro → update schema → update hook or skill → add test case → bump `schema_version`.

## Schema history

| Version | Change                                                                                                  |
| ------- | ------------------------------------------------------------------------------------------------------- |
| v1      | Initial.                                                                                                |
| v2      | Dropped `permission_mode` (Claude Code does not send it in SessionEnd payload).                         |
| v3      | Added five conversation-shape signals: `tool_errors_count`, `subagent_invocations`, `user_msg_count`, `short_user_followups_count`, `correction_keyword_hits`. Old v1/v2 rows coexist; `/analyze-metrics` uses `field in row` guards to gracefully skip them in v3-only sections. |
| v4      | Added per-subagent return-surface aggregates: `subagent_stats` (count / return_chars_total / duration_s_total / errors / maxes per `subagent_type`) and `cheap_subagent_calls` (dispatches with <200-char `tool_result`). `subagent_invocations` kept additive for back-compat. v3 rows coexist; the skill guards on `"subagent_stats" in row`. |
| v5      | Added two project-identity fields: `git_root` (absolute path of the containing git working tree) and `git_remote_origin` (normalized origin URL, e.g. `github.com/owner/repo`). Captured by the hook via `git rev-parse` / `git config --get remote.origin.url` post-fork (timeout 2s, full fallback to empty strings on any failure). Old v3/v4 rows coexist transparently — `_project_key` in `/analyze-metrics` falls back to `cwd` when git fields are missing. |
- **Adding a model to pricing:** add a prefix entry in `config/pricing.json`. Prefix matcher picks longest-first, so more specific SKUs can be added without reordering.

## Scope rules

If a proposed change would require a DB, a long-running process, or a network call — it does not belong here. "Web UI" specifically means a server, live dashboard, or anything that persists state across requests; a one-shot static HTML file the user opens locally is fine (see `/metrics-report`). Point the proposer at the roadmap section of the README when in doubt.
