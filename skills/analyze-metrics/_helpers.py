"""Helpers for the /analyze-metrics and /metrics-report renderers.

Extracted from SKILL.md so the filter, project-identity and health-signal
logic can be unit-tested in isolation. The SKILL heredoc imports this module
from its installed location (`~/.claude/skills/analyze-metrics/`); tests
import via the repo path. Stdlib-only. The only I/O performed here is reading
a log path passed explicitly (`_recent_log_errors`); everything else is pure.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# /analyze-metrics --since <X>
#   missing/empty  → default window (default_days, currently 30)
#   "all" or "0d"  → no temporal filter (full history)
#   "7d", "90d"    → now - Nd
#   ISO date       → that instant (UTC, midnight if no time)
_RELATIVE_DAYS = re.compile(r"^(\d+)d$")


def _parse_since(s: str | None, default_days: int = 30,
                 now: datetime | None = None) -> datetime | None:
    """Resolve a `--since` argument to a UTC tz-aware cutoff datetime, or
    None when no temporal filter should be applied.

    Garbage input falls back to the default window — never raises. The `now`
    parameter exists for deterministic testing; production callers omit it.
    """
    base = now if now is not None else datetime.now(timezone.utc)
    if s is None or not s.strip():
        return base - timedelta(days=default_days)
    val = s.strip().lower()
    if val in ("all", "0d", "0"):
        return None
    m = _RELATIVE_DAYS.match(val)
    if m:
        days = int(m.group(1))
        return base - timedelta(days=days)
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return base - timedelta(days=default_days)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window_label(s: str | None, default_days: int = 30) -> str:
    """Human-friendly description of the temporal window. Mirrors the rules
    in `_parse_since` so the report header never contradicts the data:
    garbage input falls back to the default window AND says so, instead of
    echoing the unparseable value.
    """
    if s is None or not s.strip():
        return f"last {default_days} days"
    val = s.strip().lower()
    if val in ("all", "0d", "0"):
        return "all-time"
    if _RELATIVE_DAYS.match(val):
        return f"last {val}"
    try:
        datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return f"since {s.strip()}"
    except ValueError:
        return f"last {default_days} days (default; '{s.strip()}' not understood)"


def _row_ts(row: dict) -> datetime | None:
    """Parse a row's `ts` field to a tz-aware UTC datetime. Returns None
    on missing/malformed values so callers can drop the row from windowed
    aggregates without poisoning them."""
    ts = row.get("ts")
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _project_key(row: dict) -> str:
    """Canonical project identity for a session row.

    Priority: git_remote_origin > git_root > cwd > 'unknown'. The first
    non-empty value wins. v3/v4 rows (which lack git_* fields) fall through
    to cwd transparently — same identity rule, no special-casing needed.
    """
    for field in ("git_remote_origin", "git_root", "cwd"):
        v = row.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "unknown"


def _project_label(key: str) -> str:
    """Human-friendly display label for a project key.

    - 'github.com/foo/bar' → 'foo/bar'  (host/owner/repo → owner/repo)
    - '/Users/me/code/api' → 'api'      (absolute path → basename)
    - 'unknown' / ''       → 'unknown'  (passthrough)
    Falls back to the input when no rule matches.
    """
    if not key:
        return "unknown"
    if key.startswith("/"):
        base = key.rstrip("/").rsplit("/", 1)[-1]
        return base or key
    parts = key.split("/")
    if len(parts) >= 3 and "." in parts[0]:
        # host/owner/repo → owner/repo (also handles host/group/sub/repo)
        return "/".join(parts[1:])
    return key


def _project_matches(row: dict, query: str) -> bool:
    """Exact-match a row against a `--project` query at any granularity:
    canonical key, git_root, cwd, or basename of any of those.

    Substring matching is intentionally NOT supported — short queries like
    'api' would match unrelated repos and silently corrupt aggregates.
    """
    if not query:
        return True
    q = query.strip()
    if not q:
        return True
    candidates: list[str] = []
    for field in ("git_remote_origin", "git_root", "cwd"):
        v = row.get(field)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    candidates.append(_project_key(row))
    for c in candidates:
        if c == q:
            return True
        if _project_label(c) == q:
            return True
        if c.startswith("/") and c.rstrip("/").rsplit("/", 1)[-1] == q:
            return True
    return False


# Health-signal helpers — surface "is the hook still recording?" in both
# /analyze-metrics and /metrics-report. The hook itself never raises (it
# silently swallows everything to hook.log), so these helpers are the only
# user-facing way to notice that something stopped working.

def _latest_session_ts(rows: list[dict]) -> datetime | None:
    """Most recent parseable `ts` across rows, or None if none parse."""
    candidates = [t for t in (_row_ts(r) for r in rows) if t is not None]
    return max(candidates, default=None)


def _humanize_age(dt: datetime | None, now: datetime | None = None) -> str:
    """Coarse age string for the report header.

    None → 'never'. Future timestamps (clock skew) collapse to 'just now'
    rather than negative ages. Resolution is intentionally coarse — exact
    seconds are noise on a session-end timeline.
    """
    if dt is None:
        return "never"
    base = now if now is not None else datetime.now(timezone.utc)
    delta = base - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# Hook log lines have the shape "[<iso8601>] [<LEVEL>] free-form text" since
# v0.x's severity-tagged logging. Pre-tag lines (legacy logs) won't match the
# level group and are silently ignored — the health signal stays meaningful
# even when the log file straddles an upgrade.
_LOG_LINE_FORMAT = re.compile(r"^\[([^\]]+)\]\s*\[(?P<level>[A-Z]+)\]")

# Defensive cap when reading the log. The hook rotates each file at 1MB so
# total log volume on disk is bounded near 2MB; this cap (10MB) is the
# belt-and-braces ceiling for runaway pre-rotation states. We tail-read so
# the most recent entries always fit in the window.
_LOG_READ_CAP = 10 * 1024 * 1024


def _read_log_tail(log_path: Path, max_bytes: int) -> str:
    """Read up to `max_bytes` from the END of `log_path`. If the file is
    larger than the cap, the leading partial line is dropped so we never
    half-parse a timestamp. Returns '' on any I/O failure or empty file."""
    try:
        size = log_path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    try:
        with log_path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
    return text


def _recent_log_errors(log_path: Path, days: int = 7,
                       now: datetime | None = None) -> int:
    """Count `[ERROR]`-tagged lines within `days` of `now` across the active
    log and its rotated `.1` sibling.

    The hook tags each line with INFO or ERROR; only ERROR is counted so
    informational notices (skips, dedupes, pricing fallbacks) don't fire
    false-positive warnings. Returns 0 on any failure — the health signal
    must never raise.
    """
    if log_path is None:
        return 0
    base = now if now is not None else datetime.now(timezone.utc)
    cutoff = base - timedelta(days=days)
    # Include the rotated generation so post-rotation gaps don't hide errors.
    paths = [log_path, Path(str(log_path) + ".1")]
    count = 0
    for p in paths:
        text = _read_log_tail(p, _LOG_READ_CAP)
        if not text:
            continue
        for line in text.splitlines():
            m = _LOG_LINE_FORMAT.match(line)
            if not m or m.group("level") != "ERROR":
                continue
            try:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                count += 1
    return count
