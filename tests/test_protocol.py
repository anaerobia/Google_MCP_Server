"""End-to-end tests driving the server over a real MCP stdio connection.

Nothing here imports a tool function. Every call is a JSON-RPC ``tools/call``
to a subprocess, so the schemas, the structured results and the error payloads
are checked exactly as an agent would see them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import call, control, write_token

pytestmark = pytest.mark.asyncio

EXPECTED_TOOLS = {
    "list_calendars",
    "search_events",
    "get_event",
    "find_free_slots",
    "create_event",
    "update_event",
    "check_auth",
}
READ_ONLY_TOOLS = {
    "list_calendars",
    "search_events",
    "get_event",
    "find_free_slots",
    "check_auth",
}
WRITE_TOOLS = {"create_event", "update_event"}


async def test_handshake_advertises_tools_and_instructions(
    connect: Any, token_path: Path
) -> None:
    """The server lists exactly its documented tools and ships instructions."""
    write_token(token_path)
    async with connect() as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        instructions = client.instructions or ""
        assert "dry_run=false" in instructions
        assert "list_calendars" in instructions


async def test_annotations_mark_readers_and_writers(
    connect: Any, token_path: Path
) -> None:
    """Readers carry readOnlyHint; writers do not and are not idempotent-safe."""
    write_token(token_path)
    async with connect() as client:
        by_name = {tool.name: tool for tool in await client.list_tools()}

    for name in READ_ONLY_TOOLS:
        assert by_name[name].annotations.read_only_hint is True, name
    for name in WRITE_TOOLS:
        assert by_name[name].annotations.read_only_hint is False, name
    assert by_name["create_event"].annotations.idempotent_hint is False
    assert by_name["update_event"].annotations.destructive_hint is True


async def test_constraints_live_in_the_schema(
    connect: Any, token_path: Path
) -> None:
    """Enumerations and bounds are schema-level, not prose-level."""
    write_token(token_path)
    async with connect() as client:
        by_name = {tool.name: tool for tool in await client.list_tools()}

    search = by_name["search_events"].input_schema["properties"]
    assert search["order_by"]["enum"] == ["startTime", "updated"]
    assert search["max_results"]["maximum"] == 100
    assert search["max_results"]["minimum"] == 1

    create = by_name["create_event"].input_schema["properties"]
    assert create["send_updates"]["enum"] == ["none", "all", "externalOnly"]
    assert create["dry_run"]["default"] is True
    assert by_name["update_event"].input_schema["properties"]["dry_run"][
        "default"
    ] is True

    slots = by_name["find_free_slots"].input_schema["properties"]
    assert slots["duration_minutes"]["minimum"] == 5
    assert slots["duration_minutes"]["maximum"] == 1440


async def test_chain_calendars_to_search_to_get(
    connect: Any, token_path: Path
) -> None:
    """list_calendars → search_events → get_event chains on returned ids."""
    write_token(token_path)
    async with connect() as client:
        calendars = await call(client, "list_calendars")
        primary = next(
            item for item in calendars["calendars"] if item["primary"]
        )
        assert primary["access_role"] == "owner"

        found = await call(
            client,
            "search_events",
            calendar_id=primary["calendar_id"],
            query="standup",
            time_min="2026-10-01T00:00:00-07:00",
            time_max="2026-10-02T00:00:00-07:00",
        )
        assert found["count"] == 1
        hit = found["events"][0]
        assert hit["event_id"] == "evt_standup_001"
        assert hit["calendar_id"] == primary["calendar_id"]
        assert hit["recurring"] is True

        detail = await call(
            client,
            "get_event",
            calendar_id=hit["calendar_id"],
            event_id=hit["event_id"],
        )
        event = detail["event"]
        assert event["summary"] == "Daily standup"
        assert [person["email"] for person in event["attendees"]] == [
            "albert@example.com",
            "dana@example.com",
        ]
        # Shaped output: Google's bookkeeping fields never reach the agent.
        assert "etag" not in event
        assert "iCalUID" not in event


async def test_free_slots_feed_create_event(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """find_free_slots output is accepted verbatim by create_event."""
    write_token(token_path)
    async with connect() as client:
        slots = await call(
            client,
            "find_free_slots",
            time_min="2026-10-01T09:00:00-07:00",
            time_max="2026-10-01T17:00:00-07:00",
            duration_minutes=45,
        )
        assert slots["count"] >= 1
        chosen = slots["slots"][0]
        # 09:00-09:15 standup and 10:00-12:00 review are busy, so the first
        # 45-minute gap starts when the standup ends.
        assert chosen["start"] == "2026-10-01T09:15:00-07:00"
        assert isinstance(chosen["gap_minutes"], int)

        preview = await call(
            client,
            "create_event",
            calendar_id="primary",
            summary="Assignment work block",
            start=chosen["start"],
            end=chosen["suggested_end"],
        )
        assert preview["dry_run"] is True
        assert preview["would_create"]["notifies_attendees"] is False
        assert fake_google.state.created == [], "dry run must not write"

        committed = await call(
            client,
            "create_event",
            calendar_id="primary",
            summary="Assignment work block",
            start=chosen["start"],
            end=chosen["suggested_end"],
            dry_run=False,
        )
        assert committed["created"] is True
        assert fake_google.state.created == [committed["event"]["event_id"]]

        # The id it returns is usable by the read tools: the chain closes.
        reread = await call(
            client,
            "get_event",
            calendar_id="primary",
            event_id=committed["event"]["event_id"],
        )
        assert reread["event"]["summary"] == "Assignment work block"


async def test_free_slots_reports_unreadable_calendars(
    connect: Any, token_path: Path
) -> None:
    """A calendar Google cannot read becomes a warning, not a silent gap."""
    write_token(token_path)
    async with connect() as client:
        slots = await call(
            client,
            "find_free_slots",
            time_min="2026-10-01T09:00:00-07:00",
            time_max="2026-10-01T17:00:00-07:00",
            calendar_ids=["primary", "missing@example.com"],
        )
    assert slots["warnings"] == ["missing@example.com: notFound"]


async def test_update_event_previews_then_commits(
    connect: Any, token_path: Path, fake_google: Any
) -> None:
    """update_event shows current-vs-changes before touching anything."""
    write_token(token_path)
    async with connect() as client:
        preview = await call(
            client,
            "update_event",
            calendar_id="primary",
            event_id="evt_review_002",
            location="Room 7",
        )
        assert preview["dry_run"] is True
        assert preview["current"]["location"] == "Room 4"
        assert preview["changes"] == {"location": "Room 7"}
        assert fake_google.state.events["evt_review_002"]["location"] == "Room 4"

        applied = await call(
            client,
            "update_event",
            calendar_id="primary",
            event_id="evt_review_002",
            location="Room 7",
            dry_run=False,
        )
        assert applied["updated"] is True
        assert applied["event"]["location"] == "Room 7"
        assert fake_google.state.events["evt_review_002"]["location"] == "Room 7"
