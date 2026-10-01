"""An in-process stand-in for Google's Calendar API and token endpoint.

The tests drive the real server binary over real stdio; only Google is faked.
That keeps the suite hermetic (no network, no real account) while still
exercising the HTTP status mapping, the silent-refresh path and the
revoked-grant path that matter most.

The stub also exposes a tiny control surface under ``/__control__`` so a test
can make the next request fail in a specific way.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CALENDARS: list[dict[str, Any]] = [
    {
        "id": "primary",
        "summary": "Albert Li",
        "timeZone": "America/Los_Angeles",
        "primary": True,
        "accessRole": "owner",
    },
    {
        "id": "team@group.calendar.google.com",
        "summary": "Team Calendar",
        "timeZone": "America/Los_Angeles",
        "accessRole": "reader",
    },
]

SEED_EVENTS: dict[str, dict[str, Any]] = {
    "evt_standup_001": {
        "id": "evt_standup_001",
        "summary": "Daily standup",
        "status": "confirmed",
        "start": {
            "dateTime": "2026-10-01T09:00:00-07:00",
            "timeZone": "America/Los_Angeles",
        },
        "end": {"dateTime": "2026-10-01T09:15:00-07:00"},
        "organizer": {"email": "albert@example.com", "self": True},
        "attendees": [
            {"email": "albert@example.com", "responseStatus": "accepted"},
            {"email": "dana@example.com", "responseStatus": "needsAction"},
        ],
        "description": "Quick sync on the MCP assignment.",
        "recurringEventId": "evt_standup_series",
        "htmlLink": "https://calendar.google.com/event?eid=evt_standup_001",
        "updated": "2026-09-28T18:00:00.000Z",
        "etag": '"ignored-by-the-server"',
        "iCalUID": "ignored@google.com",
    },
    "evt_review_002": {
        "id": "evt_review_002",
        "summary": "Design review",
        "status": "confirmed",
        "start": {"dateTime": "2026-10-01T10:00:00-07:00"},
        "end": {"dateTime": "2026-10-01T12:00:00-07:00"},
        "organizer": {"email": "dana@example.com"},
        "location": "Room 4",
        "attendees": [{"email": "albert@example.com", "responseStatus": "accepted"}],
        "htmlLink": "https://calendar.google.com/event?eid=evt_review_002",
    },
}

BUSY_PERIODS: list[dict[str, str]] = [
    {"start": "2026-10-01T09:00:00-07:00", "end": "2026-10-01T09:15:00-07:00"},
    {"start": "2026-10-01T10:00:00-07:00", "end": "2026-10-01T12:00:00-07:00"},
]


class FakeGoogleState:
    """Mutable state shared by the stub's request handler and the tests.

    Attributes:
        events: Event store, keyed by event id.
        created: Ids of events created through ``events.insert``.
        token_calls: Number of token-endpoint requests served, which is how a
            test proves a silent refresh actually happened.
        revoke_grant: When ``True`` the token endpoint answers
            ``invalid_grant``, simulating a user revoking access mid-session.
        rate_limit_next: When ``True`` the next Calendar API call answers
            ``429`` with a ``Retry-After`` header, then clears itself.
        next_access_token: Access token handed out by the next refresh.
    """

    def __init__(self) -> None:
        """Initialize a fresh stub state seeded with the sample events."""
        self.events: dict[str, dict[str, Any]] = {
            key: json.loads(json.dumps(value)) for key, value in SEED_EVENTS.items()
        }
        self.created: list[str] = []
        self.token_calls: int = 0
        self.revoke_grant: bool = False
        self.rate_limit_next: bool = False
        self.next_access_token: str = "refreshed-access-token"


def _google_error(status: int, message: str, reason: str) -> dict[str, Any]:
    """Build an error body in Google's documented shape.

    Args:
        status: HTTP status to report inside the body.
        message: Human-readable message.
        reason: Google's machine reason string.

    Returns:
        The error body.
    """
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason, "message": message}],
        }
    }


def _is_events_collection(segments: list[str]) -> bool:
    """Whether the path segments address ``/calendars/{id}/events``.

    Args:
        segments: Decoded, slash-split path segments.

    Returns:
        ``True`` for the events collection of some calendar.
    """
    return (
        len(segments) == 3 and segments[0] == "calendars" and segments[2] == "events"
    )


def _is_single_event(segments: list[str]) -> bool:
    """Whether the path segments address one event resource.

    Args:
        segments: Decoded, slash-split path segments.

    Returns:
        ``True`` for ``/calendars/{id}/events/{event_id}``.
    """
    return (
        len(segments) == 4 and segments[0] == "calendars" and segments[2] == "events"
    )


class _Handler(BaseHTTPRequestHandler):
    """Routes the handful of endpoints the server actually calls."""

    state: FakeGoogleState

    def _respond(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        """Write a JSON response.

        Args:
            status: HTTP status code.
            body: JSON-serializable response body.
            headers: Extra response headers.
        """
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        """Read and decode a JSON request body.

        Returns:
            The decoded body, or an empty dict.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _read_form(self) -> dict[str, str]:
        """Read and decode a URL-encoded request body.

        Returns:
            The decoded form fields.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _authorized(self) -> bool:
        """Whether the request carried the currently valid bearer token."""
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {type(self).state.next_access_token}"

    def _reject_unauthorized(self) -> None:
        """Answer 401 the way Google does for a dead access token."""
        self._respond(
            401,
            _google_error(401, "Invalid Credentials", "authError"),
        )

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        """Serve calendarList, events.list and events.get."""
        state = type(self).state
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/__control__":
            self._respond(
                200,
                {"token_calls": state.token_calls, "created": state.created},
            )
            return
        if state.rate_limit_next:
            state.rate_limit_next = False
            self._respond(
                429,
                _google_error(429, "Rate Limit Exceeded", "rateLimitExceeded"),
                headers={"Retry-After": "42"},
            )
            return
        if not self._authorized():
            self._reject_unauthorized()
            return

        if path == "/users/me/calendarList":
            self._respond(200, {"items": CALENDARS})
            return

        segments = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if _is_events_collection(segments):
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            self._respond(200, {"items": self._filter_events(query)})
            return
        if _is_single_event(segments):
            event = state.events.get(segments[3])
            if event is None:
                self._respond(
                    404,
                    _google_error(404, f"Event not found: {segments[3]}", "notFound"),
                )
                return
            self._respond(200, event)
            return

        self._respond(404, _google_error(404, f"No such path {path}", "notFound"))

    def _filter_events(self, query: dict[str, str]) -> list[dict[str, Any]]:
        """Apply the subset of events.list filtering the tests rely on.

        Args:
            query: Decoded query parameters.

        Returns:
            Matching events, ordered by start time.
        """
        text = query.get("q", "").lower()
        items = list(type(self).state.events.values())
        if text:
            items = [
                event
                for event in items
                if text in json.dumps(event).lower()
            ]
        return sorted(
            items,
            key=lambda event: str(
                event.get("start", {}).get("dateTime")
                or event.get("start", {}).get("date", "")
            ),
        )

    def do_POST(self) -> None:  # noqa: N802 - name fixed by the base class
        """Serve the token endpoint, freeBusy, events.insert and controls."""
        state = type(self).state
        path = urllib.parse.urlparse(self.path).path

        if path == "/__control__":
            payload = self._read_json()
            for key in ("revoke_grant", "rate_limit_next"):
                if key in payload:
                    setattr(state, key, bool(payload[key]))
            self._respond(200, {"ok": True})
            return

        if path == "/token":
            state.token_calls += 1
            form = self._read_form()
            if state.revoke_grant:
                self._respond(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
                return
            if not form.get("client_id") or not form.get("client_secret"):
                self._respond(401, {"error": "invalid_client"})
                return
            self._respond(
                200,
                {
                    "access_token": state.next_access_token,
                    "expires_in": 3599,
                    "scope": " ".join(
                        [
                            "https://www.googleapis.com/auth/calendar.readonly",
                            "https://www.googleapis.com/auth/calendar.events",
                        ]
                    ),
                    "token_type": "Bearer",
                },
            )
            return

        if state.rate_limit_next:
            state.rate_limit_next = False
            self._respond(
                429,
                _google_error(429, "Rate Limit Exceeded", "rateLimitExceeded"),
                headers={"Retry-After": "42"},
            )
            return
        if not self._authorized():
            self._reject_unauthorized()
            return

        if path == "/freeBusy":
            body = self._read_json()
            requested = [
                str(item.get("id", ""))
                for item in body.get("items", [])
                if isinstance(item, dict)
            ]
            calendars: dict[str, Any] = {}
            for calendar_id in requested:
                if calendar_id == "missing@example.com":
                    calendars[calendar_id] = {"errors": [{"reason": "notFound"}]}
                else:
                    calendars[calendar_id] = {"busy": BUSY_PERIODS}
            self._respond(
                200,
                {
                    "timeMin": body.get("timeMin"),
                    "timeMax": body.get("timeMax"),
                    "calendars": calendars,
                },
            )
            return

        segments = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if _is_events_collection(segments):
            if segments[1] == "team@group.calendar.google.com":
                self._respond(
                    403,
                    _google_error(
                        403,
                        "You need to have writer access to this calendar.",
                        "forbiddenForNonOrganizer",
                    ),
                )
                return
            body = self._read_json()
            event_id = f"evt_created_{len(state.created) + 1:03d}"
            created = {
                "id": event_id,
                "status": "confirmed",
                "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
                "organizer": {"email": "albert@example.com", "self": True},
                **body,
            }
            state.events[event_id] = created
            state.created.append(event_id)
            self._respond(200, created)
            return

        self._respond(404, _google_error(404, f"No such path {path}", "notFound"))

    def do_PATCH(self) -> None:  # noqa: N802 - name fixed by the base class
        """Serve events.patch."""
        state = type(self).state
        path = urllib.parse.urlparse(self.path).path
        if state.rate_limit_next:
            state.rate_limit_next = False
            self._respond(
                429,
                _google_error(429, "Rate Limit Exceeded", "rateLimitExceeded"),
                headers={"Retry-After": "42"},
            )
            return
        if not self._authorized():
            self._reject_unauthorized()
            return

        segments = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if _is_single_event(segments):
            event = state.events.get(segments[3])
            if event is None:
                self._respond(
                    404,
                    _google_error(404, f"Event not found: {segments[3]}", "notFound"),
                )
                return
            event.update(self._read_json())
            event["updated"] = "2026-09-30T20:00:00.000Z"
            self._respond(200, event)
            return

        self._respond(404, _google_error(404, f"No such path {path}", "notFound"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log."""


class FakeGoogle:
    """A running stub server plus the state the tests manipulate."""

    def __init__(self) -> None:
        """Bind an ephemeral loopback port and start serving."""
        self.state = FakeGoogleState()
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """Root URL of the stub, suitable for ``GCAL_MCP_API_BASE``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def token_uri(self) -> str:
        """Token endpoint URL, suitable for ``GCAL_MCP_TOKEN_URI``."""
        return f"{self.base_url}/token"

    def shutdown(self) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
