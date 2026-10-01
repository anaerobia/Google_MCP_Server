"""Thin async client for the Google Calendar v3 REST API.

This layer owns two concerns and nothing else: attaching a valid bearer token
to every request, and turning transport/HTTP failures into
:class:`~gcal_mcp.errors.ToolError`. Field selection and renaming happen in
:mod:`gcal_mcp.shaping`; argument validation happens in the tool signatures.

A request that comes back ``401`` is retried exactly once after a forced token
refresh. That single retry is what makes a token dying mid-session invisible to
the agent: the refresh happens inside the tool call, and only a refresh grant
that is genuinely dead surfaces as an ``auth_expired`` error.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Final

import httpx

from .errors import ErrorCode, ToolError, from_http_status
from .oauth import CredentialProvider

DEFAULT_API_BASE: Final[str] = "https://www.googleapis.com/calendar/v3"
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0


def api_base_from_env(env: dict[str, str] | None = None) -> str:
    """Return the Calendar API base URL, overridable for tests.

    Args:
        env: Mapping to read instead of :data:`os.environ`.

    Returns:
        The base URL without a trailing slash.
    """
    source = os.environ if env is None else env
    return source.get("GCAL_MCP_API_BASE", DEFAULT_API_BASE).rstrip("/")


class CalendarClient:
    """Authenticated access to the Calendar endpoints this server needs."""

    def __init__(
        self,
        provider: CredentialProvider,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            provider: Supplies and refreshes the bearer token.
            base_url: Calendar API base; defaults to :func:`api_base_from_env`.
            client: HTTP client to reuse; one is created on demand otherwise.
        """
        self._provider = provider
        self._base_url = (base_url or api_base_from_env()).rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Release the HTTP client and the credential provider's client."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        await self._provider.aclose()

    def _http(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating it on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
            self._owns_client = True
        return self._client

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> httpx.Response:
        """Issue one authenticated request.

        Args:
            method: HTTP verb.
            path: Path below the API base, beginning with ``/``.
            token: Bearer token.
            params: Query parameters, ``None`` values already removed.
            json_body: JSON request body, if any.

        Returns:
            The raw response, successful or not.

        Raises:
            ToolError: On a transport-level failure, flagged retryable.
        """
        try:
            return await self._http().request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ToolError(
                ErrorCode.NETWORK_ERROR,
                f"Could not reach the Google Calendar API: {exc}.",
                retry_after_seconds=5,
                remedy="Check connectivity, then retry the same call.",
            ) from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        context: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the Calendar API and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: Path below the API base, beginning with ``/``.
            context: Short phrase describing the operation, used in error
                messages (e.g. ``"search events on calendar primary"``).
            params: Query parameters; keys whose value is ``None`` are dropped.
            json_body: JSON request body, if any.
            details: Extra fields attached to any error, typically the ids.

        Returns:
            The decoded response body, or an empty dict for ``204``.

        Raises:
            ToolError: On transport failure, a non-2xx status, or a body that
                is not JSON.
        """
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        token = await self._provider.access_token()
        response = await self._send(
            method, path, token=token, params=clean_params, json_body=json_body
        )

        if response.status_code == httpx.codes.UNAUTHORIZED:
            token = await self._provider.access_token(force_refresh=True)
            response = await self._send(
                method, path, token=token, params=clean_params, json_body=json_body
            )

        if not response.is_success:
            raise from_http_status(response, context=context, details=details)

        if response.status_code == httpx.codes.NO_CONTENT or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError(
                ErrorCode.UPSTREAM_ERROR,
                f"Google returned a non-JSON body while trying to {context}.",
                retry_after_seconds=5,
                details=details,
            ) from exc
        return body if isinstance(body, dict) else {"items": body}

    async def list_calendars(self, *, max_results: int) -> dict[str, Any]:
        """Fetch the signed-in user's calendar list.

        Args:
            max_results: Page size cap.

        Returns:
            The raw ``calendarList`` response body.
        """
        return await self.request(
            "GET",
            "/users/me/calendarList",
            context="list the user's calendars",
            params={"maxResults": max_results, "showHidden": False},
        )

    async def list_events(
        self, calendar_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch events from one calendar.

        Args:
            calendar_id: Calendar identifier.
            params: Query parameters for ``events.list``.

        Returns:
            The raw ``events`` response body.
        """
        return await self.request(
            "GET",
            f"/calendars/{_quote(calendar_id)}/events",
            context=f"search events on calendar {calendar_id!r}",
            params=params,
            details={"calendar_id": calendar_id},
        )

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        """Fetch one event.

        Args:
            calendar_id: Calendar identifier.
            event_id: Event identifier.

        Returns:
            The raw ``event`` resource.
        """
        return await self.request(
            "GET",
            f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
            context=f"get event {event_id!r}",
            details={"calendar_id": calendar_id, "event_id": event_id},
        )

    async def insert_event(
        self, calendar_id: str, body: dict[str, Any], *, send_updates: str
    ) -> dict[str, Any]:
        """Create an event.

        Args:
            calendar_id: Calendar identifier.
            body: Event resource to create.
            send_updates: Google's ``sendUpdates`` value.

        Returns:
            The created ``event`` resource.
        """
        return await self.request(
            "POST",
            f"/calendars/{_quote(calendar_id)}/events",
            context=f"create an event on calendar {calendar_id!r}",
            params={"sendUpdates": send_updates},
            json_body=body,
            details={"calendar_id": calendar_id},
        )

    async def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        body: dict[str, Any],
        *,
        send_updates: str,
    ) -> dict[str, Any]:
        """Apply a partial update to an event.

        Args:
            calendar_id: Calendar identifier.
            event_id: Event identifier.
            body: Fields to change.
            send_updates: Google's ``sendUpdates`` value.

        Returns:
            The updated ``event`` resource.
        """
        return await self.request(
            "PATCH",
            f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
            context=f"update event {event_id!r}",
            params={"sendUpdates": send_updates},
            json_body=body,
            details={"calendar_id": calendar_id, "event_id": event_id},
        )

    async def free_busy(self, body: dict[str, Any]) -> dict[str, Any]:
        """Query busy intervals across calendars.

        Args:
            body: ``freeBusy.query`` request body.

        Returns:
            The raw ``freeBusy`` response body.
        """
        return await self.request(
            "POST",
            "/freeBusy",
            context="query free/busy information",
            json_body=body,
        )


def _quote(value: str) -> str:
    """Percent-encode a path segment such as a calendar or event id.

    Calendar ids are email-like and may contain ``@`` and ``#``, which must not
    be interpreted as URL syntax.

    Args:
        value: The raw identifier.

    Returns:
        The encoded segment.
    """
    return urllib.parse.quote(value, safe="")
