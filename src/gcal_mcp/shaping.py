"""Projection of Google Calendar resources into compact agent-facing shapes.

Google's ``event`` resource carries roughly forty fields, most of which an
agent will never read: etags, iCalUID, sequence numbers, per-attendee
``self`` flags, HTML links to the old UI. Passing it through would cost tokens
on every call and would couple this server's contract to Google's.

Every function here answers one question: what does the agent need in order to
make the *next* call or the next statement to the user? Keys are snake_case and
stable even if Google renames something upstream.
"""

from __future__ import annotations

from typing import Any, Final

#: Attendees beyond this many are summarised by count rather than listed, so a
#: 300-person all-hands cannot blow up a tool result.
MAX_LISTED_ATTENDEES: Final[int] = 25
#: Descriptions are truncated in detail views; the full text is rarely needed
#: to decide what to do next.
MAX_DESCRIPTION_CHARS: Final[int] = 600


def _truncate(text: str, limit: int) -> str:
    """Shorten text to ``limit`` characters, marking that it was cut.

    Args:
        text: The source text.
        limit: Maximum characters to keep.

    Returns:
        The text, suffixed with an ellipsis marker when truncated.
    """
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit].rstrip()}… [truncated]"


def _timestamp(slot: dict[str, Any] | None) -> str:
    """Flatten Google's ``start``/``end`` object to a single string.

    Google uses ``dateTime`` for timed events and ``date`` for all-day ones.
    An agent that has to branch on which key is present will eventually get it
    wrong, so both collapse to one field and ``all_day`` carries the
    distinction separately.

    Args:
        slot: An event ``start`` or ``end`` object, possibly absent.

    Returns:
        An RFC 3339 timestamp, a ``YYYY-MM-DD`` date, or an empty string.
    """
    if not slot:
        return ""
    return str(slot.get("dateTime") or slot.get("date") or "")


def _is_all_day(event: dict[str, Any]) -> bool:
    """Whether an event is all-day (date-only start).

    Args:
        event: A raw Google ``event`` resource.

    Returns:
        ``True`` for all-day events.
    """
    start = event.get("start") or {}
    return "date" in start and "dateTime" not in start


def shape_calendar(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a ``calendarList`` entry.

    Args:
        raw: A raw calendar list entry.

    Returns:
        A dict with ``calendar_id``, ``summary``, ``time_zone``, ``primary``
        and ``access_role``. ``calendar_id`` is the value every other tool in
        this server takes as its ``calendar_id`` argument, and ``access_role``
        tells the agent in advance whether a write will be permitted.
    """
    return {
        "calendar_id": str(raw.get("id", "")),
        "summary": str(raw.get("summary", "")),
        "time_zone": str(raw.get("timeZone", "")),
        "primary": bool(raw.get("primary", False)),
        "access_role": str(raw.get("accessRole", "")),
    }


def shape_event_summary(raw: dict[str, Any], *, calendar_id: str) -> dict[str, Any]:
    """Project an event for list and search results.

    Deliberately omits description, attendee lists and conferencing data:
    those belong to ``get_event``, which the agent calls once it has chosen an
    event. ``calendar_id`` is echoed back so each result is self-sufficient as
    the input to the next call.

    Args:
        raw: A raw Google ``event`` resource.
        calendar_id: The calendar the event was read from.

    Returns:
        A compact event summary.
    """
    return {
        "event_id": str(raw.get("id", "")),
        "calendar_id": calendar_id,
        "summary": str(raw.get("summary", "(no title)")),
        "start": _timestamp(raw.get("start")),
        "end": _timestamp(raw.get("end")),
        "all_day": _is_all_day(raw),
        "location": str(raw.get("location", "")),
        "status": str(raw.get("status", "")),
        "organizer": str((raw.get("organizer") or {}).get("email", "")),
        "attendee_count": len(raw.get("attendees") or []),
        "recurring": bool(raw.get("recurringEventId") or raw.get("recurrence")),
    }


def _shape_attendees(raw: dict[str, Any]) -> dict[str, Any]:
    """Summarise attendees, listing them only while the list stays small.

    Args:
        raw: A raw Google ``event`` resource.

    Returns:
        A dict with ``attendee_count`` and either ``attendees`` (a list of
        ``{email, response_status, optional}``) or ``attendees_omitted``.
    """
    attendees = raw.get("attendees") or []
    result: dict[str, Any] = {"attendee_count": len(attendees)}
    if len(attendees) > MAX_LISTED_ATTENDEES:
        result["attendees_omitted"] = (
            f"{len(attendees)} attendees; too many to list "
            f"(limit {MAX_LISTED_ATTENDEES})."
        )
        return result
    result["attendees"] = [
        {
            "email": str(person.get("email", "")),
            "response_status": str(person.get("responseStatus", "needsAction")),
            "optional": bool(person.get("optional", False)),
        }
        for person in attendees
    ]
    return result


def _conference_url(raw: dict[str, Any]) -> str:
    """Extract a video meeting link if the event has one.

    Args:
        raw: A raw Google ``event`` resource.

    Returns:
        The first video entry point URI, or an empty string.
    """
    entry_points = (raw.get("conferenceData") or {}).get("entryPoints") or []
    for point in entry_points:
        if isinstance(point, dict) and point.get("entryPointType") == "video":
            return str(point.get("uri", ""))
    return str(raw.get("hangoutLink", ""))


def shape_event_detail(raw: dict[str, Any], *, calendar_id: str) -> dict[str, Any]:
    """Project a single event for ``get_event``.

    Args:
        raw: A raw Google ``event`` resource.
        calendar_id: The calendar the event was read from.

    Returns:
        The summary fields plus description, attendees, conferencing link,
        recurrence rules, whether the signed-in user organizes the event, and
        the ``html_link`` a human can open.
    """
    detail = shape_event_summary(raw, calendar_id=calendar_id)
    detail.update(_shape_attendees(raw))
    detail.update(
        {
            "description": _truncate(
                str(raw.get("description", "")), MAX_DESCRIPTION_CHARS
            ),
            "time_zone": str((raw.get("start") or {}).get("timeZone", "")),
            "conference_url": _conference_url(raw),
            "recurrence": [str(rule) for rule in (raw.get("recurrence") or [])],
            "recurring_event_id": str(raw.get("recurringEventId", "")),
            "html_link": str(raw.get("htmlLink", "")),
            "updated": str(raw.get("updated", "")),
            "organizer_is_self": bool((raw.get("organizer") or {}).get("self")),
        }
    )
    return detail
