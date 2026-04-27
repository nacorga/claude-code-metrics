"""Pure helpers for the /analyze-metrics report.

Extracted from SKILL.md so the filter and project-identity logic can be
unit-tested in isolation. The SKILL heredoc imports this module from its
installed location (`~/.claude/skills/analyze-metrics/`); tests import via
the repo path. No side effects, no I/O — only stdlib.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

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
