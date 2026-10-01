"""Interval arithmetic for turning busy periods into bookable gaps.

``find_free_slots`` exists so that the agent never has to do date arithmetic
itself. Given the busy blocks Google reports, it returns the gaps that are long
enough to hold the meeting the agent is trying to book — in exactly the
``start``/``end`` RFC 3339 form that ``create_event`` accepts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

SECONDS_PER_MINUTE: Final[int] = 60


class TimeParseError(ValueError):
    """Raised when a caller-supplied timestamp is not RFC 3339."""


def parse_rfc3339(value: str, *, field: str) -> datetime:
    """Parse an RFC 3339 timestamp into an aware :class:`datetime`.

    Args:
        value: The timestamp, e.g. ``2026-10-01T09:00:00-07:00`` or a
            ``Z``-suffixed UTC form.
        field: Name of the argument being parsed, used in the error message so
            the agent knows which argument to fix.

    Returns:
        A timezone-aware datetime.

    Raises:
        TimeParseError: If the value is unparseable or lacks a UTC offset.
    """
    text = value.strip()
    if not text:
        raise TimeParseError(f"{field} must not be empty.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeParseError(
            f"{field}={value!r} is not an RFC 3339 timestamp; expected "
            "something like 2026-10-01T09:00:00-07:00."
        ) from exc
    if parsed.tzinfo is None:
        raise TimeParseError(
            f"{field}={value!r} has no UTC offset. Include one (e.g. "
            "-07:00 or Z) so the time is unambiguous."
        )
    return parsed


def _merge(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Sort and coalesce overlapping or touching intervals.

    Args:
        intervals: Possibly overlapping ``(start, end)`` pairs.

    Returns:
        Disjoint intervals in ascending order.
    """
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def busy_intervals(
    free_busy_response: dict[str, Any],
) -> tuple[list[tuple[datetime, datetime]], list[str]]:
    """Extract busy periods and per-calendar errors from a free/busy response.

    Args:
        free_busy_response: Raw ``freeBusy.query`` response body.

    Returns:
        A ``(intervals, warnings)`` tuple. Warnings name calendars Google
        could not read, so the agent can say "I checked 2 of 3 calendars"
        instead of silently reporting a slot that is actually taken.
    """
    intervals: list[tuple[datetime, datetime]] = []
    warnings: list[str] = []
    for calendar_id, payload in (free_busy_response.get("calendars") or {}).items():
        if not isinstance(payload, dict):
            continue
        for problem in payload.get("errors") or []:
            reason = (
                problem.get("reason", "unknown")
                if isinstance(problem, dict)
                else ""
            )
            warnings.append(f"{calendar_id}: {reason}")
        for period in payload.get("busy") or []:
            if not isinstance(period, dict):
                continue
            try:
                start = parse_rfc3339(str(period.get("start", "")), field="busy.start")
                end = parse_rfc3339(str(period.get("end", "")), field="busy.end")
            except TimeParseError:
                continue
            if end > start:
                intervals.append((start, end))
    return _merge(intervals), warnings


def find_free_slots(
    *,
    window_start: datetime,
    window_end: datetime,
    busy: list[tuple[datetime, datetime]],
    duration_minutes: int,
    max_slots: int,
) -> list[dict[str, Any]]:
    """Return gaps in ``busy`` long enough to hold ``duration_minutes``.

    Args:
        window_start: Earliest acceptable start.
        window_end: Latest acceptable end.
        busy: Disjoint, ascending busy intervals from :func:`busy_intervals`.
        duration_minutes: Required meeting length.
        max_slots: Maximum gaps to return.

    Returns:
        Up to ``max_slots`` dicts with ``start``, ``end`` (RFC 3339, matching
        the window's offset) and ``duration_minutes`` for the whole gap. Each
        ``start`` can be passed straight to ``create_event``.
    """
    needed = timedelta(minutes=duration_minutes)
    slots: list[dict[str, Any]] = []
    cursor = window_start
    for start, end in busy:
        if start > cursor and start - cursor >= needed:
            slots.append(_slot(cursor, min(start, window_end), needed))
        cursor = max(cursor, end)
        if cursor >= window_end or len(slots) >= max_slots:
            break
    if len(slots) < max_slots and window_end - cursor >= needed:
        slots.append(_slot(cursor, window_end, needed))
    return slots[:max_slots]


def _slot(start: datetime, end: datetime, needed: timedelta) -> dict[str, Any]:
    """Build one free-slot record.

    Args:
        start: Gap start.
        end: Gap end.
        needed: Requested duration, used to derive the suggested end time.

    Returns:
        A dict describing the gap and a concrete bookable end time.
    """
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "gap_minutes": int((end - start).total_seconds() // SECONDS_PER_MINUTE),
        "suggested_end": (start + needed).isoformat(),
    }
