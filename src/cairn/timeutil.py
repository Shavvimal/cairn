"""Shared time helpers for the source skills.

Centralises the few time literals that were otherwise copy-pasted across every
skill's export loop (the "recent" cutoff window and today's date stamp), so the
values have one named home instead of recurring as bare literals.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

# The "--today"/"recent" lookback window. One day, expressed in seconds.
SECONDS_PER_DAY: int = 86_400

_SINCE_DURATION = re.compile(r"(\d+)([dw])")

# Weekday name -> Monday=0..Sunday=6, for "last <weekday>" recall expressions.
_DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_since(token: str) -> float:
    """Parse a ``--since`` window into an absolute POSIX timestamp (the cutoff).

    Accepts ``today``, ``yesterday``, durations ``Nd`` / ``Nw`` (days / weeks back),
    or an ISO date ``YYYY-MM-DD``. Returns the Unix timestamp items must be newer
    than. Raises :class:`ValueError` on anything else so argparse reports it as an
    invalid argument (exit 2) rather than silently defaulting.
    """
    raw = token.strip()
    key = raw.lower()
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        return midnight.timestamp()
    if key == "yesterday":
        return (midnight - timedelta(days=1)).timestamp()
    m = _SINCE_DURATION.fullmatch(key)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return (now - timedelta(days=n * (7 if unit == "w" else 1))).timestamp()
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        raise ValueError(
            f"invalid --since value {token!r}; use today|yesterday|Nd|Nw|YYYY-MM-DD"
        ) from None


def parse_date_range(expr: str) -> tuple[datetime, datetime]:
    """Parse a recall date expression into a ``(start, end)`` UTC day-boundary range.

    ``start`` is inclusive, ``end`` exclusive. Unlike :func:`parse_since` (a single
    cutoff for export windows), recall needs a bounded span. Accepts ``today``,
    ``yesterday``, ``YYYY-MM-DD``, ``N days ago``, ``last N days``, ``this week``,
    ``last week``, and ``last monday``..``last sunday``. Raises :class:`ValueError`
    on anything else so the caller fails loudly instead of recalling the wrong window.
    """
    key = expr.strip().lower()
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if key == "today":
        return today, today + timedelta(days=1)
    if key == "yesterday":
        return today - timedelta(days=1), today

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", key)
    if m:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
        return d, d + timedelta(days=1)

    m = re.fullmatch(r"(\d+)\s+days?\s+ago", key)
    if m:
        start = today - timedelta(days=int(m.group(1)))
        return start, start + timedelta(days=1)

    m = re.fullmatch(r"last\s+(\d+)\s+days?", key)
    if m:
        return today - timedelta(days=int(m.group(1))), today + timedelta(days=1)

    if key == "this week":
        monday = today - timedelta(days=today.weekday())
        return monday, today + timedelta(days=1)
    if key == "last week":
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday

    m = re.fullmatch(r"last\s+(\w+)", key)
    if m and m.group(1) in _DAY_NAMES:
        days_back = (today.weekday() - _DAY_NAMES[m.group(1)]) % 7 or 7
        start = today - timedelta(days=days_back)
        return start, start + timedelta(days=1)

    raise ValueError(
        f"invalid date expression {expr!r}; use today|yesterday|YYYY-MM-DD|"
        "'N days ago'|'last N days'|'this week'|'last week'|'last <weekday>'"
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into a ``datetime``, or ``None`` if it can't be.

    Returns ``None`` for a falsy or unparseable ``value``. A trailing ``Z`` is
    normalised to ``+00:00`` so :meth:`datetime.fromisoformat` accepts it on all
    supported Pythons. This is the single home for the ``fromisoformat`` + ``"Z"``
    shim that was otherwise copy-pasted across recall and the Granola adapter, each
    with its own ``(ValueError, TypeError)`` swallow. (Export-window parsing in
    :func:`parse_since` is deliberately *not* built on this - it must raise on bad
    input so argparse rejects it, rather than degrade to ``None``.)
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def today_str() -> str:
    """Today's date as ``YYYY-MM-DD`` (local time).

    Used as the date fallback when a session has no parseable timestamp of its own.
    """
    return datetime.now().strftime("%Y-%m-%d")


def recent_cutoff_timestamp() -> float:
    """UTC Unix timestamp marking the start of the ``--today`` lookback window."""
    return datetime.now(UTC).timestamp() - SECONDS_PER_DAY


__all__ = [
    "SECONDS_PER_DAY",
    "parse_date_range",
    "parse_iso",
    "parse_since",
    "recent_cutoff_timestamp",
    "today_str",
]
