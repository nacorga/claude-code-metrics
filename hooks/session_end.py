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
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5

# A subagent dispatch whose tool_result text is under this many characters is
# flagged as "cheap": likely a question that could have been answered with a
# direct grep/Read. The subagent overhead (extra context window, latency,
# tokens) is wasted on questions with tiny answers. 200 picked from
# observation: real Explore reports run thousands of chars; a 50-char "yes,
# the file exists at X" is exactly the failure mode worth surfacing.
CHEAP_RETURN_THRESHOLD = 200

# Conversation-shape heuristics (v3).
# Short follow-up threshold: any user text message under this many chars
# (after the first) is counted as a "steering nudge". 200 picked from
# observation: real prompts are usually longer; quick redirects are shorter.
SHORT_FOLLOWUP_MAX_CHARS = 200

# Correction keywords. Conservative English-only set: each phrase is a strong
# signal that the user is undoing, redoing, or rejecting the assistant's last
# action. Matched as case-insensitive substrings on the first 200 chars of
# each user message — short corrections live at the top. Order does not
# matter; we count distinct messages that hit at least once.
#
# Kept intentionally short and English-only to stay project-agnostic. Locale-
# specific sets can be added downstream by analyzers; the hook only ships a
# safe default.
CORRECTION_KEYWORDS = (
    "undo",
    "revert",
    "rollback",
    "redo",
    "incorrect",
    "wrong",
    "broken",
    "doesn't work",
    "doesnt work",
    "not what i asked",
    "not what i wanted",
)

# Transcript timestamps sometimes span days when a session is resumed after
# a long idle period. Anything above this cap is almost certainly wall-clock,
# not work time — null it out rather than pollute aggregates.
MAX_DURATION_S = 86400  # 24h

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


# v5. Project-identity helpers. Captured per-session in run_worker so reports
# can group by stable repo identity instead of cwd basename. Pure best-effort:
# any failure (no git, no repo, no remote, broken URL) yields empty strings —
# the row stays valid and legacy behaviour (cwd-based grouping) takes over.
_SSH_ORIGIN = re.compile(r"^[A-Za-z0-9_-]+@([^:]+):(.+)$")
_URL_ORIGIN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/]+)/(.+)$")


def _normalize_origin(url: str) -> str:
    """Normalize a git remote.origin.url into a stable 'host/owner/repo' key.

    Handles SSH (`git@github.com:foo/bar.git`), HTTPS with optional auth
    (`https://user@github.com/foo/bar.git`), and ssh:// / git:// schemes.
    Strips the trailing `.git`. Falls back to the raw input (minus `.git`) for
    shapes we don't recognize (file://, custom hosts) — never invents a value.
    """
    if not url:
        return ""
    s = url.strip()
    if not s:
        return ""
    if s.endswith(".git"):
        s = s[:-4]
    m = _SSH_ORIGIN.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = _URL_ORIGIN.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return s


def _capture_git_metadata(cwd: str) -> tuple[str, str]:
    """Return (git_root, raw_origin_url) for cwd. Both empty when cwd is not
    inside a git repo, no origin is configured, or git is unavailable.

    Runs only inside the detached worker (post double-fork), so the 2s
    timeout cannot stall Claude Code's session close. Expected failures —
    git binary missing (FileNotFoundError), cwd outside a repo, missing
    remote — are silent. Only timeouts and genuine OS errors log, since
    those signal something worth investigating.
    """
    if not cwd:
        return "", ""
    try:
        if not Path(cwd).is_dir():
            return "", ""
    except OSError:
        return "", ""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
    except FileNotFoundError:
        return "", ""
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"git rev-parse failed at {cwd}: {e}")
        return "", ""
    if proc.returncode != 0:
        return "", ""
    git_root = proc.stdout.strip()
    if not git_root:
        return "", ""
    try:
        proc2 = subprocess.run(
            ["git", "-C", git_root, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=2,
        )
    except FileNotFoundError:
        return git_root, ""
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"git config failed at {git_root}: {e}")
        return git_root, ""
    if proc2.returncode != 0:
        return git_root, ""
    return git_root, proc2.stdout.strip()


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


def _user_text(content) -> str | None:
    """Extract the user-authored text from a user-entry's content payload.

    Claude Code transcripts represent user entries in two shapes:
      - content: "free text"           → real user message
      - content: [{type: text, text}…] → real user message (one or more text blocks)
      - content: [{type: tool_result…}] → synthetic, NOT a user-authored message
    We only treat the first two as "user messages".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        has_text_block = False
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                has_text_block = True
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        if has_text_block:
            return "\n".join(parts)
    return None


def _tool_result_text_len(content) -> int:
    """Char-count of a tool_result's payload — the size of what the subagent
    injects back into the main agent's context. Handles both shapes the
    transcript uses: a plain string, or a list of {type:text,text:…} blocks."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text")
                if isinstance(t, str):
                    n += len(t)
        return n
    return 0


def _empty_subagent_stat() -> dict:
    return {
        "count": 0,
        "return_chars_total": 0,
        "duration_s_total": 0.0,
        "errors": 0,
        "max_return_chars": 0,
        "max_duration_s": 0.0,
    }


def parse_transcript(path: Path) -> dict:
    """Extract aggregates from the Claude Code transcript JSONL.

    Caller (run_worker) guarantees the file exists and is non-empty.
    """
    model: str | None = None
    first_ts = None
    last_ts = None
    turn_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_creation = 0
    cache_read = 0
    tool_counts: dict[str, int] = {}
    subagent_counts: dict[str, int] = {}
    tool_errors_count = 0
    user_msg_count = 0
    short_user_followups_count = 0
    correction_keyword_hits = 0
    # v4: per-subagent return-surface aggregates. We cannot attribute tokens
    # to a subagent (the transcript only records main-agent usage), but we can
    # measure what the subagent dumps back into the main context: chars,
    # duration, errors. pending_dispatches holds Agent tool_use blocks waiting
    # for their matching tool_result by tool_use_id.
    pending_dispatches: dict[str, tuple[str, "datetime | None"]] = {}
    subagent_stats: dict[str, dict] = {}
    cheap_subagent_calls = 0

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

            etype = entry.get("type")

            if etype == "user":
                msg = entry.get("message") or {}
                content = msg.get("content")
                # Walk tool_result blocks once: count errors AND resolve any
                # pending Agent dispatches whose results have arrived.
                if isinstance(content, list):
                    for c in content:
                        if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                            continue
                        is_err = c.get("is_error") is True
                        if is_err:
                            tool_errors_count += 1
                        tu_id = c.get("tool_use_id")
                        if tu_id and tu_id in pending_dispatches:
                            sub, dispatch_ts = pending_dispatches.pop(tu_id)
                            ret_chars = _tool_result_text_len(c.get("content"))
                            dur = None
                            if dispatch_ts and ts:
                                delta = round((ts - dispatch_ts).total_seconds(), 2)
                                # Negative deltas can appear in resumed/edited
                                # transcripts where timestamps are non-monotonic.
                                # Drop them rather than poison aggregates.
                                if delta >= 0:
                                    dur = delta
                            stat = subagent_stats.setdefault(sub, _empty_subagent_stat())
                            stat["count"] += 1
                            stat["return_chars_total"] += ret_chars
                            if ret_chars > stat["max_return_chars"]:
                                stat["max_return_chars"] = ret_chars
                            if dur is not None:
                                stat["duration_s_total"] = round(stat["duration_s_total"] + dur, 2)
                                if dur > stat["max_duration_s"]:
                                    stat["max_duration_s"] = dur
                            if is_err:
                                stat["errors"] += 1
                            if ret_chars < CHEAP_RETURN_THRESHOLD:
                                cheap_subagent_calls += 1
                # Real user messages
                text = _user_text(content)
                if text is not None:
                    user_msg_count += 1
                    stripped = text.strip()
                    # Skip system-injected prompts (caveats, hooks) for follow-up signal.
                    is_system_injected = stripped.startswith("<") or stripped.startswith("Caveat:")
                    if user_msg_count > 1 and not is_system_injected and 0 < len(stripped) < SHORT_FOLLOWUP_MAX_CHARS:
                        short_user_followups_count += 1
                    head = stripped[:200].lower()
                    if any(kw in head for kw in CORRECTION_KEYWORDS):
                        correction_keyword_hits += 1
                continue

            if etype != "assistant":
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
                    # Capture subagent dispatches separately so we can answer
                    # "how often is Explore actually used?" without re-parsing
                    # transcripts. Falls back to "<unknown>" when input is malformed.
                    if name == "Agent":
                        inp = block.get("input") or {}
                        sub = inp.get("subagent_type") if isinstance(inp, dict) else None
                        sub = sub or "<unknown>"
                        subagent_counts[sub] = subagent_counts.get(sub, 0) + 1
                        tu_id = block.get("id")
                        if tu_id:
                            pending_dispatches[tu_id] = (sub, ts)

    # Transcript existed but no assistant ever responded (session abandoned,
    # /clear before first turn, hook fired mid-flush). Mark as error so
    # /analyze-metrics excludes it from aggregates.
    if turn_count == 0:
        return {"error": "empty_transcript"}

    duration_s = None
    if first_ts and last_ts:
        duration_s = round((last_ts - first_ts).total_seconds(), 2)
        if duration_s > MAX_DURATION_S:
            log(f"implausible duration_s={duration_s} (>{MAX_DURATION_S}s), nulling")
            duration_s = None

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
        "tool_errors_count": tool_errors_count,
        "subagent_invocations": subagent_counts,
        "subagent_stats": subagent_stats,
        "cheap_subagent_calls": cheap_subagent_calls,
        "user_msg_count": user_msg_count,
        "short_user_followups_count": short_user_followups_count,
        "correction_keyword_hits": correction_keyword_hits,
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


def _session_already_recorded(session_id: str) -> bool:
    """Scan auto.jsonl for an existing row with the given session_id.
    Called while holding the lock in append_row."""
    if not AUTO_JSONL.is_file():
        return False
    try:
        with AUTO_JSONL.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("session_id") == session_id:
                    return True
    except Exception as e:
        log(f"dedupe scan failed: {e}")
    return False


def append_row(row: dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            sid = row.get("session_id")
            if sid and sid != "unknown" and _session_already_recorded(sid):
                log(f"dedupe: session_id {sid} already recorded, skipping")
                return
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

    # Ghost session guard: Claude Code fires SessionEnd even for sessions
    # that never produced a transcript (opened-and-closed, cancelled prompt,
    # etc.). No transcript on disk means no data worth recording — treat it
    # the same as /compact and /clear: silent skip, no row.
    if not transcript_path:
        log(f"skip: no transcript_path in payload (session_id={session_id})")
        return
    tp = Path(transcript_path)
    if not tp.is_file() or tp.stat().st_size == 0:
        log(f"skip: ghost session, transcript missing or empty at {transcript_path} (session_id={session_id})")
        return

    git_root, git_origin_raw = _capture_git_metadata(cwd)
    row: dict = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "cwd": cwd,
        "git_root": git_root,
        "git_remote_origin": _normalize_origin(git_origin_raw),
        "end_reason": "close",
    }

    try:
        parsed = parse_transcript(tp)
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
        "tool_errors_count": parsed["tool_errors_count"],
        "subagent_invocations": parsed["subagent_invocations"],
        "subagent_stats": parsed["subagent_stats"],
        "cheap_subagent_calls": parsed["cheap_subagent_calls"],
        "user_msg_count": parsed["user_msg_count"],
        "short_user_followups_count": parsed["short_user_followups_count"],
        "correction_keyword_hits": parsed["correction_keyword_hits"],
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
        "tool_errors_count": 0,
        "subagent_invocations": {},
        "subagent_stats": {},
        "cheap_subagent_calls": 0,
        "user_msg_count": 0,
        "short_user_followups_count": 0,
        "correction_keyword_hits": 0,
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log(f"fatal: {e}\n{traceback.format_exc()}")
        sys.exit(0)
