# claude-code-metrics

[![tests](https://github.com/nacorga/claude-code-metrics/actions/workflows/test.yml/badge.svg)](https://github.com/nacorga/claude-code-metrics/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

**Instrumentation for agentic engineering.** Measure what your coding agent actually does — cost, tool usage, correction rate, task outcome — all in a local JSONL file. No dashboard, no database, no telemetry. Just data you can `grep`.

## Why

If you're orchestrating Claude Code with custom agents, skills, and hooks, you're doing agentic engineering. And you probably have no idea:

- How much each session actually costs.
- Which tools your agent reaches for most.
- Whether plan mode reduces your correction rate.
- Which project chews the most tokens.

This repo gives you a 2-file answer: a **SessionEnd hook** that captures objective metrics automatically, plus a **`/retrospective` skill** for the subjective ones. Then `/analyze-metrics` joins them and shows you the correlations.

## Quickstart

```bash
git clone https://github.com/YOUR_USERNAME/claude-code-metrics.git
cd claude-code-metrics
./scripts/install.sh
```

Restart Claude Code. That's it. Every session from now on appends a line to `~/.claude/metrics/auto.jsonl`.

At the end of a session (optional but recommended), run:

```
/retrospective
```

And whenever you want to see your data:

```
/analyze-metrics
```

## The four metrics

| Metric                   | How captured     | Why it matters                                                  |
| ------------------------ | ---------------- | --------------------------------------------------------------- |
| `cost_usd`               | Auto (estimated) | Cost is the one number everyone wants but nobody tracks locally |
| `correction_rate` (0-10) | `/retrospective` | Proxy for "did Claude do the right thing without fighting"      |
| `skill_trigger_accuracy` | `/retrospective` | Reveals whether your skill design actually works in practice    |
| `task_outcome`           | `/retrospective` | Anchors cost/corrections to whether the session shipped         |

## What gets captured (auto.jsonl)

Every real session close appends one row with:

- `session_id`, `ts`, `cwd`, `end_reason`
- `duration_s`, `turn_count`, `tool_calls_total`, `tool_distribution`
- `model`, `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`
- `cost_usd` (estimated from `config/pricing.json`)

`/compact` and `/clear` are filtered out — only real session ends are recorded.

See [`schema/auto.schema.json`](./schema/auto.schema.json) and [`schema/retro.schema.json`](./schema/retro.schema.json) for the full shapes.

## Uninstall

```bash
bash scripts/uninstall.sh              # removes hook + skills, keeps your metrics data
bash scripts/uninstall.sh --purge-data # also wipes ~/.claude/metrics/
```

Idempotent. Preserves any other hooks you have registered in `settings.json`.

## Your terminal never hangs on session close

When you exit Claude Code, the hook returns instantly and finishes its work in the background — even if the session's transcript is 60MB+. You won't see the terminal freeze.

## Privacy

Everything is local under `~/.claude/metrics/`. Zero network calls. Nothing is sent to me, Anthropic, or anyone else. Your transcripts are parsed on your machine, aggregated, then thrown away — only the aggregates are written to the JSONL.

You can inspect the data any time:

```bash
jq . ~/.claude/metrics/auto.jsonl | less
```

Don't sync `~/.claude/metrics/` to iCloud/Dropbox — `flock` is unreliable on network filesystems.

## Pricing caveat

`cost_usd` is **estimated from token counts**, not billed cost. It will drift 10-20% from your actual Anthropic invoice (cache accounting edge cases, tool result tokens, etc.). Use it for **relative** comparisons between sessions, not as your accounting source of truth.

When Anthropic ships new model IDs, update [`config/pricing.json`](./config/pricing.json). The script uses prefix matching (longest-first) with a `_default` fallback, so unknown models still get a rough estimate.

## Roadmap

Explicitly **not** on the roadmap for v0.x:

- Web dashboard
- SQLite / any database
- Remote telemetry / cloud sync
- Multi-user / team features

The whole point is a single JSONL you own. If you want charts, pipe it into a notebook.

Things that **are** on the table:

- `npx claude-code-metrics init` installer (v0.2)
- Schema validation step in the hook
- Opt-in markdown export for sharing anonymised benchmarks
- Additional skills: `/metrics-export`, `/metrics-diff` (week over week)

## Contributing

Issues welcome. PRs should keep the JSONL-only philosophy and include tests. If you're adding fields to a row, bump `schema_version` and add a migration note in the schema file.

Run the test suite (stdlib `unittest`, zero external deps):

```bash
python3 -m unittest discover tests/ -v
```

## License

MIT. See [LICENSE](./LICENSE).
