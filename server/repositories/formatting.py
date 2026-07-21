"""Rendering helpers shared by the log repositories.

**Why this module exists.** The file-backed logs wrote
``now_in_user_timezone("%Y-%m-%d %H:%M:%S")`` — a local-time string with no
offset — directly into the log line, and ``load_transcript()`` embedded that
exact string in the LLM system prompt (``conversation/log.py:69,131``,
``execution/log_store.py:72-73``). Phase 0 correctly moved the column to
``timestamptz``.

If the read path renders UTC instead of the user's local time, **every prompt
silently changes meaning** — "you said this at 14:30" becomes a different hour —
and agent behaviour drifts with no error anywhere. So the conversion has exactly
one implementation and it lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from ..logging_config import logger

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

UTC = timezone.utc


def resolve_zone(timezone_name: str | None, default: str = "UTC") -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or default)
    except Exception:
        logger.warning("unknown timezone; falling back", extra={"timezone": timezone_name})
        return ZoneInfo(default)


def render_timestamp(moment: datetime | None, timezone_name: str | None) -> str:
    """Render a stored ``timestamptz`` the way the file logs used to write it.

    Naive values are assumed UTC, which matches what Postgres hands back for a
    ``timestamptz`` read through asyncpg with no tz configured.
    """
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(resolve_zone(timezone_name)).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(rendered: str | None, timezone_name: str | None) -> datetime:
    """Inverse of :func:`render_timestamp`.

    Used when the summarizer hands back a tail it only ever saw as rendered
    local-time strings. Falls back to now-UTC for anything unparseable rather
    than dropping the entry.
    """
    if rendered:
        try:
            naive = datetime.strptime(rendered, TIMESTAMP_FORMAT)
            return naive.replace(tzinfo=resolve_zone(timezone_name)).astimezone(UTC)
        except ValueError:
            logger.warning("unparseable log timestamp; using now()", extra={"value": rendered})
    return datetime.now(UTC)


def render_entry(tag: str, timestamp: str, payload: str) -> str:
    """One transcript line, exactly as ``load_transcript()`` used to build it.

    Payloads are stored raw in Postgres (the file format's ``\\n``-collapsing and
    HTML-escaping were storage concerns, not prompt concerns), so the only
    transformation left on the read path is the same ``escape(..., quote=False)``
    the old ``load_transcript`` applied.
    """
    safe = escape(payload, quote=False)
    if timestamp:
        return f'<{tag} timestamp="{timestamp}">{safe}</{tag}>'
    return f"<{tag}>{safe}</{tag}>"


__all__ = [
    "TIMESTAMP_FORMAT",
    "parse_timestamp",
    "render_entry",
    "render_timestamp",
    "resolve_zone",
]
