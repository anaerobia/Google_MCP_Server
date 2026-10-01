"""MCP server exposing Google Calendar to a coding agent over stdio.

The tool surface is designed around one fact: the agent sees these signatures,
these docstrings and the JSON they return, and nothing else. So

* every constrained argument is constrained *in the schema* (``Literal``,
  ``Field(ge=..., le=...)``), never only in prose, which keeps the docstring
  and the validator from ever disagreeing;
* every docstring names where its ids come from, so the chain
  ``list_calendars → search_events → get_event → update_event`` is discoverable
  without trial and error;
* every failure returns ``{"error": {...}}`` rather than raising, so a bad id
  costs the agent one turn instead of ending it;
* the two mutating tools default to ``dry_run=True``, so the agent must look
  before it writes.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from .calendar_api import CalendarClient
from .errors import AuthRequiredError, ErrorCode, ToolError
from .oauth import SCOPES, CredentialProvider, OAuthConfig, TokenStore
from .scheduling import (
    TimeParseError,
    busy_intervals,
    parse_rfc3339,
)
from .scheduling import find_free_slots as compute_free_slots
from .shaping import shape_calendar, shape_event_detail, shape_event_summary

INSTRUCTIONS: Final[str] = """\
Read-and-write access to the signed-in user's Google Calendar.

Typical flow:
  1. `list_calendars` once per session to learn the `calendar_id` values and
     each calendar's `access_role`. Everything else takes a `calendar_id`;
     "primary" is a valid shorthand for the user's own main calendar.
  2. `search_events` to find events. It returns `event_id` values.
  3. `get_event` for the full detail of one of those events (attendees,
     description, meeting link).
  4. `find_free_slots` before proposing a meeting time; it returns timestamps
     that `create_event` accepts verbatim.
  5. `create_event` / `update_event` to write. Both default to
     `dry_run=true`: call once to see exactly what would change, then call
     again with identical arguments and `dry_run=false` to commit.

All timestamps in and out are RFC 3339 with a UTC offset (for example
`2026-10-01T09:00:00-07:00`). All-day events use plain `YYYY-MM-DD` dates.

Credentials are obtained out-of-band. If a tool returns an error with code
`auth_required` or `auth_expired`, this server cannot fix it on its own: tell
the user to run the login command named in the error's `remedy` field, then
retry. Do not keep retrying the same call before they have done so.

Errors are returned as data, never raised: a result containing an `error` key
has `retryable` set, which tells you whether calling again can help.
"""

MAX_CALENDARS: Final[int] = 100
MAX_EVENTS_PER_SEARCH: Final[int] = 100
MAX_FREE_SLOTS: Final[int] = 20
MAX_FREEBUSY_CALENDARS: Final[int] = 10
MAX_DURATION_MINUTES: Final[int] = 60 * 24
_DATE_ONLY: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SendUpdates = Literal["none", "all", "externalOnly"]

mcp: FastMCP = FastMCP(
    name="google-calendar",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)

_client: CalendarClient | None = None


def get_client() -> CalendarClient:
    """Return the process-wide Calendar client, building it on first use.

    Construction is lazy and never fails on missing credentials, so the server
    always starts and always advertises its tools; a missing credential becomes
    an actionable error on the first call instead of a startup crash that the
    agent cannot see.

    Returns:
        The shared client.
    """
    global _client
    if _client is None:
        _client = CalendarClient(CredentialProvider(OAuthConfig.from_env()))
    return _client


def set_client(client: CalendarClient | None) -> None:
    """Replace the process-wide Calendar client.

    Args:
        client: The client to install, or ``None`` to reset to lazy default.
    """
    global _client
    _client = client


ResultT = TypeVar("ResultT", bound=dict[str, Any])


def as_data(
    func: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Convert raised :class:`ToolError`/parse failures into result payloads.

    A traceback crossing the MCP boundary ends the agent's turn and tells it
    nothing. This decorator is the single place where that is prevented, so no
    individual tool has to remember to do it.

    Args:
        func: The tool coroutine to wrap.

    Returns:
        A coroutine that returns ``{"error": {...}}`` instead of raising.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except TimeParseError as exc:
            return ToolError(
                ErrorCode.INVALID_ARGUMENT,
                str(exc),
                remedy="Fix the timestamp argument; do not retry unchanged.",
            ).to_payload()
        except ToolError as exc:
            return exc.to_payload()

    return wrapper


def _time_field(raw: str, *, field: str) -> dict[str, str]:
    """Build a Google ``start``/``end`` object from a caller timestamp.

    Args:
        raw: Either ``YYYY-MM-DD`` (all-day) or an RFC 3339 timestamp.
        field: Argument name, used in error messages.

    Returns:
        ``{"date": ...}`` for all-day values, else ``{"dateTime": ...}``.

    Raises:
        TimeParseError: If a non-date value is not valid RFC 3339.
    """
    text = raw.strip()
    if _DATE_ONLY.match(text):
        return {"date": text}
    parse_rfc3339(text, field=field)
    return {"dateTime": text}


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "List calendars",
    }
)
@as_data
async def list_calendars(
    max_results: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_CALENDARS,
            description="Maximum calendars to return.",
        ),
    ] = 25,
) -> dict[str, Any]:
    """List the calendars the signed-in user can see.

    Start here. The `calendar_id` of each result is what every other tool in
    this server takes as its `calendar_id` argument. Check `access_role`
    before attempting a write: only `owner` and `writer` can be modified,
    so a `reader` calendar will reject `create_event` with
    `permission_denied`.

    The user's own main calendar is always reachable as the literal string
    `"primary"`, so you may skip this call if that is all you need.

    Returns:
        `{"calendars": [{calendar_id, summary, time_zone, primary,
        access_role}], "count": int}`, or `{"error": {...}}`.
    """
    raw = await get_client().list_calendars(max_results=max_results)
    calendars = [
        shape_calendar(item)
        for item in raw.get("items", [])
        if isinstance(item, dict)
    ]
    return {"calendars": calendars, "count": len(calendars)}


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Search events",
    }
)
@as_data
async def search_events(
    calendar_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Calendar to search: a calendar_id from list_calendars, or "
                "'primary' for the user's own main calendar."
            ),
        ),
    ] = "primary",
    query: Annotated[
        str | None,
        Field(
            description=(
                "Free-text match against title, description, location and "
                "attendee emails. Omit to list everything in the window."
            )
        ),
    ] = None,
    time_min: Annotated[
        str | None,
        Field(
            description=(
                "Only events ending at or after this RFC 3339 timestamp, e.g. "
                "'2026-10-01T00:00:00-07:00'. Omit for no lower bound."
            )
        ),
    ] = None,
    time_max: Annotated[
        str | None,
        Field(
            description=(
                "Only events starting before this RFC 3339 timestamp. Omit "
                "for no upper bound."
            )
        ),
    ] = None,
    max_results: Annotated[
        int,
        Field(ge=1, le=MAX_EVENTS_PER_SEARCH, description="Page size cap."),
    ] = 20,
    order_by: Annotated[
        Literal["startTime", "updated"],
        Field(description="Sort order of the returned events."),
    ] = "startTime",
    include_cancelled: Annotated[
        bool,
        Field(description="Include events the organizer has cancelled."),
    ] = False,
) -> dict[str, Any]:
    """Find events on one calendar, newest-first or by start time.

    This is the tool that produces `event_id` values; pass one to `get_event`
    for full detail, or to `update_event` to change it. Recurring series are
    expanded into individual occurrences, so each result is a single dated
    event you can act on.

    Timestamps are RFC 3339 and must carry a UTC offset; a naive
    `2026-10-01T09:00:00` is rejected with `invalid_argument` rather than
    silently guessed at.

    Returns:
        `{"events": [{event_id, calendar_id, summary, start, end, all_day,
        location, status, organizer, attendee_count, recurring}], "count": int,
        "truncated": bool}`, or `{"error": {...}}`. `truncated` means more
        events matched than `max_results` allowed; narrow the window or raise
        the cap.
    """
    if time_min:
        parse_rfc3339(time_min, field="time_min")
    if time_max:
        parse_rfc3339(time_max, field="time_max")

    params: dict[str, Any] = {
        "q": query,
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": order_by,
        "showDeleted": include_cancelled,
    }
    raw = await get_client().list_events(calendar_id, params)
    events = [
        shape_event_summary(item, calendar_id=calendar_id)
        for item in raw.get("items", [])
        if isinstance(item, dict)
    ]
    return {
        "events": events,
        "count": len(events),
        "truncated": bool(raw.get("nextPageToken")),
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Get event detail",
    }
)
@as_data
async def get_event(
    calendar_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "The calendar_id the event lives on, as returned in each "
                "search_events result."
            ),
        ),
    ],
    event_id: Annotated[
        str,
        Field(
            min_length=1,
            description="The event_id from a search_events result.",
        ),
    ],
) -> dict[str, Any]:
    """Read one event in full, including attendees and the meeting link.

    Both arguments come from a single `search_events` result, which echoes the
    `calendar_id` back alongside each `event_id` precisely so you can pass the
    pair straight here. A mismatched pair returns `not_found`, which is not
    retryable: re-run `search_events` rather than guessing.

    Long descriptions are truncated and very large attendee lists are reported
    as a count instead of a list.

    Returns:
        `{"event": {...}}` with the search fields plus `description`,
        `attendees`, `attendee_count`, `time_zone`, `conference_url`,
        `recurrence`, `recurring_event_id`, `html_link`, `updated` and
        `organizer_is_self`; or `{"error": {...}}`.
    """
    raw = await get_client().get_event(calendar_id, event_id)
    return {"event": shape_event_detail(raw, calendar_id=calendar_id)}


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Find free time",
    }
)
@as_data
async def find_free_slots(
    time_min: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Start of the search window, RFC 3339 with offset. The UTC "
                "offset you pass is the offset the results come back in."
            ),
        ),
    ],
    time_max: Annotated[
        str,
        Field(min_length=1, description="End of the search window, RFC 3339."),
    ],
    duration_minutes: Annotated[
        int,
        Field(
            ge=5,
            le=MAX_DURATION_MINUTES,
            description="Length of the meeting you intend to book.",
        ),
    ] = 30,
    calendar_ids: Annotated[
        list[str] | None,
        Field(
            max_length=MAX_FREEBUSY_CALENDARS,
            description=(
                "Calendars that must all be free, from list_calendars. "
                "Defaults to ['primary']."
            ),
        ),
    ] = None,
    max_slots: Annotated[
        int,
        Field(ge=1, le=MAX_FREE_SLOTS, description="Maximum gaps to return."),
    ] = 5,
) -> dict[str, Any]:
    """Find gaps in the window where every listed calendar is free.

    Call this before proposing a time to the user. Each slot's `start` and
    `suggested_end` are formatted exactly as `create_event` expects, so a
    chosen slot can be passed through without reformatting.

    This looks only at busy/free blocks, so it ignores working hours and time
    zones of other attendees — narrow the window yourself if the user cares
    about those.

    Returns:
        `{"slots": [{start, end, gap_minutes, suggested_end}],
        "count": int, "window": {...}, "warnings": [str]}`, or
        `{"error": {...}}`. A non-empty `warnings` list names calendars that
        could not be read, meaning the slots may be less free than they look.
    """
    window_start = parse_rfc3339(time_min, field="time_min")
    window_end = parse_rfc3339(time_max, field="time_max")
    if window_end <= window_start:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"time_max ({time_max}) must be after time_min ({time_min}).",
            remedy="Swap or correct the window bounds.",
        )

    targets = calendar_ids or ["primary"]
    raw = await get_client().free_busy(
        {
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "items": [{"id": calendar_id} for calendar_id in targets],
        }
    )
    busy, warnings = busy_intervals(raw)
    slots = compute_free_slots(
        window_start=window_start,
        window_end=window_end,
        busy=busy,
        duration_minutes=duration_minutes,
        max_slots=max_slots,
    )
    return {
        "slots": slots,
        "count": len(slots),
        "window": {
            "time_min": window_start.isoformat(),
            "time_max": window_end.isoformat(),
            "duration_minutes": duration_minutes,
            "calendars_checked": targets,
        },
        "warnings": warnings,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "title": "Create event (dry-run by default)",
    }
)
@as_data
async def create_event(
    calendar_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Calendar to write to: a calendar_id from list_calendars "
                "whose access_role is 'owner' or 'writer', or 'primary'."
            ),
        ),
    ],
    summary: Annotated[
        str,
        Field(min_length=1, max_length=500, description="Event title."),
    ],
    start: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Start time: RFC 3339 with offset for a timed event, or "
                "'YYYY-MM-DD' for an all-day event. A free slot's `start` "
                "from find_free_slots works as-is."
            ),
        ),
    ],
    end: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "End time, same formats as `start`. For an all-day event this "
                "is the exclusive end date."
            ),
        ),
    ],
    description: Annotated[
        str | None, Field(max_length=8000, description="Event body text.")
    ] = None,
    location: Annotated[
        str | None, Field(max_length=1000, description="Free-text location.")
    ] = None,
    attendee_emails: Annotated[
        list[str] | None,
        Field(
            max_length=50,
            description=(
                "Guests to add. They are only emailed if send_updates is not "
                "'none'."
            ),
        ),
    ] = None,
    send_updates: Annotated[
        SendUpdates,
        Field(
            description=(
                "Whether Google emails the attendees. Defaults to 'none' so "
                "a mistake stays private; ask the user before using 'all'."
            )
        ),
    ] = "none",
    dry_run: Annotated[
        bool,
        Field(
            description=(
                "When true (the default) nothing is written: the normalized "
                "event is returned for review. Call again with identical "
                "arguments and dry_run=false to actually create it."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Create a calendar event. Previews by default; writes only on request.

    This tool mutates real state and sends real invitations, so it has a
    brake: the first call with `dry_run=true` (the default) returns exactly
    what would be created without touching the calendar. Show that preview to
    the user, then repeat the call with `dry_run=false`.

    Nothing about the write is idempotent — calling twice with
    `dry_run=false` creates two events. If a call fails with a retryable
    error, use `search_events` to check whether the first attempt landed
    before retrying.

    Returns:
        With `dry_run=true`: `{"dry_run": true, "would_create": {...},
        "next_step": str}`. With `dry_run=false`: `{"created": true,
        "event": {...}}` whose `event_id` can be passed to `get_event` or
        `update_event`. On failure: `{"error": {...}}`.
    """
    body: dict[str, Any] = {
        "summary": summary,
        "start": _time_field(start, field="start"),
        "end": _time_field(end, field="end"),
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendee_emails:
        body["attendees"] = [{"email": email} for email in attendee_emails]

    if dry_run:
        return {
            "dry_run": True,
            "would_create": {
                "calendar_id": calendar_id,
                "summary": summary,
                "start": start,
                "end": end,
                "location": location or "",
                "description": description or "",
                "attendee_emails": attendee_emails or [],
                "notifies_attendees": send_updates != "none",
            },
            "next_step": (
                "Nothing was written. Repeat this call with identical "
                "arguments and dry_run=false to create the event."
            ),
        }

    raw = await get_client().insert_event(
        calendar_id, body, send_updates=send_updates
    )
    return {
        "created": True,
        "event": shape_event_detail(raw, calendar_id=calendar_id),
    }


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Update event (dry-run by default)",
    }
)
@as_data
async def update_event(
    calendar_id: Annotated[
        str,
        Field(
            min_length=1,
            description="The calendar_id from the search_events result.",
        ),
    ],
    event_id: Annotated[
        str,
        Field(
            min_length=1,
            description="The event_id from search_events or create_event.",
        ),
    ],
    summary: Annotated[
        str | None,
        Field(min_length=1, max_length=500, description="New title."),
    ] = None,
    start: Annotated[
        str | None,
        Field(
            description=(
                "New start, RFC 3339 or 'YYYY-MM-DD'. If you change `start` "
                "you almost always want to change `end` too."
            )
        ),
    ] = None,
    end: Annotated[str | None, Field(description="New end time.")] = None,
    description: Annotated[
        str | None, Field(max_length=8000, description="Replacement body text.")
    ] = None,
    location: Annotated[
        str | None, Field(max_length=1000, description="Replacement location.")
    ] = None,
    send_updates: Annotated[
        SendUpdates,
        Field(description="Whether Google emails existing attendees."),
    ] = "none",
    dry_run: Annotated[
        bool,
        Field(
            description=(
                "When true (the default) nothing is written: the current "
                "event and the proposed change are returned side by side."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Change fields on an existing event. Previews by default.

    This overwrites the fields you pass and leaves every other field alone,
    but an overwrite still loses the previous value, so the default
    `dry_run=true` fetches the event and shows `current` beside `changes`
    before anything happens. Review that, then repeat with `dry_run=false`.

    Editing one occurrence of a recurring series affects only that
    occurrence; `get_event` reports `recurring_event_id` when that is the
    case.

    Returns:
        With `dry_run=true`: `{"dry_run": true, "current": {...},
        "changes": {...}, "next_step": str}`. With `dry_run=false`:
        `{"updated": true, "event": {...}}`. On failure: `{"error": {...}}`,
        including `invalid_argument` if you passed no fields to change.
    """
    changes: dict[str, Any] = {}
    if summary is not None:
        changes["summary"] = summary
    if start is not None:
        changes["start"] = _time_field(start, field="start")
    if end is not None:
        changes["end"] = _time_field(end, field="end")
    if description is not None:
        changes["description"] = description
    if location is not None:
        changes["location"] = location

    if not changes:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "No fields to change were provided.",
            remedy=(
                "Pass at least one of summary, start, end, description or "
                "location."
            ),
            details={"calendar_id": calendar_id, "event_id": event_id},
        )

    client = get_client()
    if dry_run:
        current = await client.get_event(calendar_id, event_id)
        return {
            "dry_run": True,
            "current": shape_event_detail(current, calendar_id=calendar_id),
            "changes": changes,
            "notifies_attendees": send_updates != "none",
            "next_step": (
                "Nothing was written. Repeat this call with identical "
                "arguments and dry_run=false to apply the changes."
            ),
        }

    raw = await client.patch_event(
        calendar_id, event_id, changes, send_updates=send_updates
    )
    return {
        "updated": True,
        "event": shape_event_detail(raw, calendar_id=calendar_id),
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Check authorization",
    }
)
@as_data
async def check_auth(
    verify: Annotated[
        bool,
        Field(
            description=(
                "When true, prove the credentials still work by exchanging "
                "the refresh token with Google. Leave false for a free, "
                "local-only report."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Report whether this server currently holds usable Google credentials.

    Call this when another tool returned `auth_required` or `auth_expired`,
    to confirm the user's login worked before retrying real work. It never
    opens a browser and never reads calendar data.

    By default the answer is local: `authorized` means a cached token and an
    OAuth client are both present. That cannot detect a grant the user revoked
    in their Google account settings — only contacting Google can. Pass
    `verify=true` to do exactly that, and read `grant_valid`, which is `null`
    when unverified.

    Returns:
        `{"authorized": bool, "grant_valid": bool | null, "token_path": str,
        "oauth_client_configured": bool, "token_state": str,
        "scopes_requested": [str], "scopes_granted": [str], "detail": str}`.
        `token_state` is one of `absent`, `stale` or `fresh`, and a successful
        `verify` leaves it `fresh` because verification refreshes the token.
        `authorized: false` is a normal result here, not an error.
    """
    config = OAuthConfig.from_env()
    token = TokenStore(config.token_path).load()
    client_configured = bool(config.client_id and config.client_secret)

    if token is None:
        token_state = "absent"
    elif token.is_fresh():
        token_state = "fresh"
    else:
        token_state = "stale"

    grant_valid: bool | None = None
    detail_parts: list[str] = []
    if token is None:
        detail_parts.append("No cached token found.")
    elif token_state == "fresh":
        detail_parts.append("Cached access token is valid.")
    else:
        detail_parts.append(
            "Cached access token is stale; it will be refreshed silently on "
            "the next call."
        )
    if not client_configured:
        detail_parts.append(
            "The OAuth client env vars (GOOGLE_OAUTH_CLIENT_ID / "
            "GOOGLE_OAUTH_CLIENT_SECRET) are unset in the server environment."
        )

    if verify and token is not None and client_configured:
        provider = CredentialProvider(config)
        try:
            await provider.access_token(force_refresh=True)
            grant_valid = True
            token_state = "fresh"
            detail_parts.append("Google accepted the refresh token.")
        except AuthRequiredError as exc:
            grant_valid = False
            detail_parts.append(exc.message)
        finally:
            await provider.aclose()
    elif verify:
        detail_parts.append("Nothing to verify against.")

    authorized = token is not None and client_configured and grant_valid is not False
    return {
        "authorized": authorized,
        "grant_valid": grant_valid,
        "token_path": str(config.token_path),
        "oauth_client_configured": client_configured,
        "token_state": token_state,
        "scopes_requested": list(SCOPES),
        "scopes_granted": token.scope.split() if token else [],
        "detail": " ".join(detail_parts),
    }


def main() -> None:
    """Run the server over stdio.

    stdout carries the MCP protocol, so nothing else may be printed there.
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
