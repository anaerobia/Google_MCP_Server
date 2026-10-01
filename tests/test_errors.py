"""Protocol-level tests of the failure paths an agent has to recover from.

Each test asserts on the error payload an agent would actually receive: a
``code`` it can branch on, a ``retryable`` flag, and a ``remedy`` when there is
something concrete to do. None of these failures arrive as a traceback, which
is the point — ``is_error`` stays ``False`` and the agent's turn continues.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_ACCESS_TOKEN, call, control, write_token

pytestmark = pytest.mark.asyncio


async def test_unknown_event_id_is_not_retryable(
    connect: Any, token_path: Path
) -> None:
    """A bad id yields not_found with a pointer back to search_events."""
    write_token(token_path)
    async with connect() as client:
        result = await client.call_tool(
            "get_event",
            {"calendar_id": "primary", "event_id": "evt_does_not_exist"},
        )

    assert result.is_error is False, "failures are data, not protocol errors"
    error = result.data["error"]
    assert error["code"] == "not_found"
    assert error["retryable"] is False
    assert "search_events" in error["remedy"]
    # The ids are echoed back so the agent knows which call failed.
    assert error["event_id"] == "evt_does_not_exist"
    assert error["calendar_id"] == "primary"


async def test_naive_timestamp_is_rejected_before_the_api_call(
    connect: Any, token_path: Path
) -> None:
    """A timestamp with no UTC offset is invalid_argument, never a guess."""
    write_token(token_path)
    async with connect() as client:
        result = await call(
            client,
            "search_events",
            calendar_id="primary",
            time_min="2026-10-01T09:00:00",
        )
    error = result["error"]
    assert error["code"] == "invalid_argument"
    assert error["retryable"] is False
    assert "time_min" in error["message"]


async def test_out_of_range_argument_is_caught_by_the_schema(
    connect: Any, token_path: Path
) -> None:
    """Bounds declared in the schema are enforced by the protocol layer.

    ``max_results`` is capped at 100 in the schema, so the server rejects 5000
    before any tool code runs. This is the payoff of putting the constraint in
    the schema rather than in prose: the agent is told the argument is wrong
    instead of silently getting a different page size.
    """
    write_token(token_path)
    async with connect() as client:
        result = await client.call_tool(
            "search_events",
            {"calendar_id": "primary", "max_results": 5000},
            raise_on_error=False,
        )
    assert result.is_error is True
    assert "max_results" in str(result.content[0].text)


async def test_update_with_no_fields_explains_what_to_pass(
    connect: Any, token_path: Path
) -> None:
    """An empty update is refused with the list of usable fields."""
    write_token(token_path)
    async with connect() as client:
        result = await call(
            client,
            "update_event",
            calendar_id="primary",
            event_id="evt_review_002",
        )
    error = result["error"]
    assert error["code"] == "invalid_argument"
    assert "summary" in error["remedy"]


async def test_write_to_read_only_calendar_is_permission_denied(
    connect: Any, token_path: Path
) -> None:
    """A reader calendar refuses the write and names access_role as the check."""
    write_token(token_path)
    async with connect() as client:
        result = await call(
            client,
            "create_event",
            calendar_id="team@group.calendar.google.com",
            summary="Not allowed",
            start="2026-10-01T13:00:00-07:00",
            end="2026-10-01T14:00:00-07:00",
            dry_run=False,
        )
    error = result["error"]
    assert error["code"] == "permission_denied"
    assert error["retryable"] is False
    assert "access_role" in error["remedy"]


async def test_rate_limit_is_retryable_with_a_delay(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """429 becomes rate_limited carrying Google's own Retry-After value."""
    write_token(token_path)
    async with connect() as client:
        control(fake_google, rate_limit_next=True)
        result = await call(client, "list_calendars")
        error = result["error"]
        assert error["code"] == "rate_limited"
        assert error["retryable"] is True
        assert error["retry_after_seconds"] == 42

        # The flag was one-shot: the agent's retry now succeeds.
        retried = await call(client, "list_calendars")
        assert retried["count"] == 2


async def test_expired_access_token_is_refreshed_silently(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """A stale cached token is refreshed inside the call; the agent sees success."""
    write_token(token_path, access_token="stale-token", expires_in=-60)
    async with connect() as client:
        result = await call(client, "list_calendars")

    assert result["count"] == 2
    assert fake_google.state.token_calls == 1, "exactly one refresh"
    cached = json.loads(token_path.read_text())
    assert cached["access_token"] == VALID_ACCESS_TOKEN
    assert cached["refresh_token"] == "stored-refresh-token", "grant is preserved"
    assert token_path.stat().st_mode & 0o777 == 0o600


async def test_token_rejected_mid_session_triggers_one_refresh(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """A token Google rejects as 401 is refreshed and the call is retried once."""
    # The cache looks fresh, so nothing prompts a proactive refresh; only the
    # upstream 401 does. This is the mid-session revocation shape.
    write_token(token_path, access_token="no-longer-accepted", expires_in=3600)
    async with connect() as client:
        result = await call(client, "list_calendars")

    assert result["count"] == 2
    assert fake_google.state.token_calls == 1


async def test_revoked_grant_returns_actionable_auth_error(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """A dead refresh grant yields auth_expired telling the user to re-login."""
    write_token(token_path, access_token="stale-token", expires_in=-60)
    control(fake_google, revoke_grant=True)
    async with connect() as client:
        result = await call(client, "list_calendars")

    error = result["error"]
    assert error["code"] == "auth_expired"
    assert error["retryable"] is False
    assert "gcal-mcp-login" in error["remedy"]
    # The server must not have tried to open a browser; it returned instead.
    assert "browser" in error["remedy"]


async def test_missing_credentials_report_auth_required(
    connect: Any, token_path: Path
) -> None:
    """With no cached token at all, tools fail closed with auth_required."""
    async with connect() as client:
        result = await call(client, "list_calendars")
    error = result["error"]
    assert error["code"] == "auth_required"
    assert str(token_path) in error["message"]


async def test_check_auth_reports_state_without_calling_google(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """check_auth describes credentials and never touches the Calendar API."""
    write_token(token_path)
    async with connect() as client:
        authorized = await call(client, "check_auth")

    assert authorized["authorized"] is True
    assert authorized["grant_valid"] is None, "unverified unless asked"
    assert authorized["token_state"] == "fresh"
    assert authorized["scopes_requested"] == [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]
    assert "calendar.events" in " ".join(authorized["scopes_granted"])
    assert fake_google.state.token_calls == 0


async def test_check_auth_reports_missing_login(
    connect: Any, token_path: Path
) -> None:
    """Not being logged in is a normal result here, not an error."""
    async with connect() as client:
        unauthorized = await call(client, "check_auth")
    assert unauthorized["authorized"] is False
    assert unauthorized["token_state"] == "absent"
    assert "error" not in unauthorized
    assert str(token_path) == unauthorized["token_path"]


async def test_check_auth_verify_detects_a_revoked_grant(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """A cached token that Google no longer honours is reported as invalid.

    Without `verify`, `check_auth` can only see that a token file exists. This
    is the call an agent makes to tell "the user has not logged in yet" apart
    from "the user logged in and it worked".
    """
    write_token(token_path)
    control(fake_google, revoke_grant=True)
    async with connect() as client:
        revoked = await call(client, "check_auth", verify=True)
        assert revoked["grant_valid"] is False
        assert revoked["authorized"] is False
        assert "revoked" in revoked["detail"]

        control(fake_google, revoke_grant=False)
        healthy = await call(client, "check_auth", verify=True)
        assert healthy["grant_valid"] is True
        assert healthy["authorized"] is True
        assert healthy["token_state"] == "fresh", "verification refreshes"
