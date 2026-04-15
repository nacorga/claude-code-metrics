#!/usr/bin/env python3
"""
claude-code-metrics — SessionEnd hook.

Reads the stdin payload Claude Code sends when a session ends, parses the
transcript to extract tokens / tools / duration, estimates cost from a local
pricing table, and appends a single JSON line to ~/.claude/metrics/auto.jsonl.

Design invariants:
  - Always exits 0. Never blocks Claude Code.
  - Returns to the shell in <100ms: heavy work is detached via double-fork
    so large transcripts (tens of MB) cannot stall session close.
  - Stderr is captured to ~/.claude/metrics/hook.log for debugging.
  - Skips /compact and /clear ends (we only record real session closes).
  - Uses fcntl.flock to serialise concurrent writers.

Set CCM_NO_FORK=1 to run synchronously (used by tests).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

METRICS_DIR = Path.home() / ".claude" / "metrics"
AUTO_JSONL = METRICS_DIR / "auto.jsonl"
LOCK_PATH = METRICS_DIR / ".auto.lock"
LOG_PATH = METRICS_DIR / "hook.log"

# Pricing config: first looks next to this script (repo layout), then at
# ~/.claude/metrics/pricing.json (installed layout).
SCRIPT_DIR = Path(__file__).resolve().parent
PRICING_CANDIDATES = [
    SCRIPT_DIR.parent / "config" / "pricing.json",
    METRICS_DIR / "pricing.json",
    Path.home() / ".claude" / "hooks" / "pricing.json",
]

SKIP_REASONS = {"compact", "clear", "prompt_input_submit"}


def log(msg: str) -> None:
    try:
        METRICS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


def load_pricing() -> dict:
    for candidate in PRICING_CANDIDATES:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except Exception as e:
                log(f"pricing load failed at {candidate}: {e}")
    log("pricing config not found; cost will be null")
    return {}


def lookup_price(pricing: dict, model: str | None) -> tuple[dict | None, str | None]:
    """Return (rates, matched_key). matched_key is the model prefix that hit,
    or '_default' if the fallback was used, or None if no match at all."""
    if not model or not pricing:
        return None, None
    keys = [k for k in pricing if not k.startswith("_")]
    keys.sort(key=len, reverse=True)
    for k in keys:
        if model.startswith(k):
            return pricing[k], k
    if "_default" in pricing:
        return pricing["_default"], "_default"
    return None, None


def parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_transcript(path: Path) -> dict:
    """Extract aggregates from the Claude Code transcript JSONL."""
    model: str | None = None
    first_ts = None
    last_ts = None
    turn_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_creation = 0
    cache_read = 0
    tool_counts: dict[str, int] = {}

    if not path.is_file() or path.stat().st_size == 0:
        return {"error": "no_transcript"}

    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = parse_iso(entry.get("timestamp"))
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            if entry.get("type") != "assistant":
                continue

            turn_count += 1
            msg = entry.get("message") or {}

            m = msg.get("model")
            if m:
                model = m

            usage = msg.get("usage") or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)

            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "<unknown>"
                    tool_counts[name] = tool_counts.get(name, 0) + 1

    duration_s = None
    if first_ts and last_ts:
        duration_s = round((last_ts - first_ts).total_seconds(), 2)

    return {
        "model": model,
        "duration_s": duration_s,
        "turn_count": turn_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "tool_distribution": tool_counts,
        "tool_calls_total": sum(tool_counts.values()),
    }


def estimate_cost(parsed: dict, pricing: dict) -> tuple[float | None, str | None]:
    rates, matched = lookup_price(pricing, parsed.get("model"))
    if not rates:
        return None, matched
    cost = round(
        (parsed.get("input_tokens", 0) / 1_000_000) * rates.get("input", 0)
        + (parsed.get("output_tokens", 0) / 1_000_000) * rates.get("output", 0)
        + (parsed.get("cache_creation_tokens", 0) / 1_000_000) * rates.get("cache_write", 0)
        + (parsed.get("cache_read_tokens", 0) / 1_000_000) * rates.get("cache_read", 0),
        6,
    )
    return cost, matched


def append_row(row: dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with AUTO_JSONL.open("a") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def should_skip(payload: dict) -> bool:
    """Only record real session closes. Ignore /compact and /clear."""
    for key in ("reason", "source", "end_reason", "matcher", "trigger"):
        val = payload.get(key)
        if isinstance(val, str) and val.lower() in SKIP_REASONS:
            return True
    return False


def run_worker(payload: dict) -> None:
    """Full parse + cost + append. May take seconds on large transcripts."""
    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd") or ""
    permission_mode = payload.get("permission_mode")

    row: dict = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "cwd": cwd,
        "permission_mode": permission_mode,
        "end_reason": "close",
    }

    if not transcript_path:
        row["error"] = "no_transcript_path"
        row.update(_empty_metrics())
        append_row(row)
        return

    try:
        parsed = parse_transcript(Path(transcript_path))
    except Exception as e:
        log(f"transcript parse crashed: {e}\n{traceback.format_exc()}")
        parsed = {"error": "parse_crash"}

    if "error" in parsed:
        row["error"] = parsed["error"]
        row.update(_empty_metrics())
        append_row(row)
        return

    pricing = load_pricing()
    cost, matched = estimate_cost(parsed, pricing)

    row.update({
        "duration_s": parsed["duration_s"],
        "turn_count": parsed["turn_count"],
        "tool_calls_total": parsed["tool_calls_total"],
        "tool_distribution": parsed["tool_distribution"],
        "model": parsed["model"],
        "input_tokens": parsed["input_tokens"],
        "output_tokens": parsed["output_tokens"],
        "cache_creation_tokens": parsed["cache_creation_tokens"],
        "cache_read_tokens": parsed["cache_read_tokens"],
        "cost_usd": cost,
    })

    model = parsed.get("model")
    if model and cost is None:
        log(f"unknown model, no _default fallback, cost=null: {model}")
    elif model and matched == "_default":
        log(f"using _default pricing for {model} — add explicit entry to pricing.json for accuracy")

    append_row(row)


def _detach_and_run(payload: dict) -> None:
    """Double-fork so the worker survives parent exit and does not tie the
    shell to transcript-parse duration. Grandchild closes stdio and runs
    run_worker; errors go to hook.log."""
    try:
        pid = os.fork()
    except OSError as e:
        log(f"fork failed, running synchronously: {e}")
        run_worker(payload)
        return

    if pid > 0:
        # Parent: reap the intermediate child so it does not become a zombie,
        # then return to main() which exits 0. The grandchild is reparented
        # to init and keeps running.
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return

    # First child: detach from the controlling terminal and fork again.
    try:
        os.setsid()
    except OSError:
        pass
    try:
        pid2 = os.fork()
    except OSError:
        os._exit(0)
    if pid2 > 0:
        os._exit(0)

    # Grandchild: become a daemon. Close std fds so Claude Code does not
    # wait on our pipes.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except Exception:
        pass

    try:
        run_worker(payload)
    except Exception as e:
        log(f"async worker crashed: {e}\n{traceback.format_exc()}")
    finally:
        os._exit(0)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"invalid stdin: {e}")
        return 0

    if payload.get("hook_event_name") and payload["hook_event_name"] != "SessionEnd":
        return 0

    if should_skip(payload):
        return 0

    if os.environ.get("CCM_NO_FORK"):
        run_worker(payload)
        return 0

    _detach_and_run(payload)
    return 0


def _empty_metrics() -> dict:
    return {
        "duration_s": None,
        "turn_count": 0,
        "tool_calls_total": 0,
        "tool_distribution": {},
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": None,
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log(f"fatal: {e}\n{traceback.format_exc()}")
        sys.exit(0)
