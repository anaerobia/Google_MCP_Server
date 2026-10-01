"""Record a real MCP transcript of the tool chain, as markdown.

Run this to regenerate the transcript section of ``writeup.md``:

    uv run python scripts/capture_transcript.py

It launches the actual server as a stdio subprocess and calls it over the MCP
protocol, so every argument and result printed below is traffic that really
happened. Google itself is the test stub from ``tests/fake_google.py``, which
keeps the capture reproducible on any machine and without a real account; the
server code under test is unmodified.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fake_google import FakeGoogle  # noqa: E402 - needs the path set above

VALID_ACCESS_TOKEN = "refreshed-access-token"


def _seed_token(path: Path, *, access_token: str, expires_in: float) -> None:
    """Write a token cache for the server to read.

    Args:
        path: Cache location.
        access_token: Cached access token.
        expires_in: Seconds until expiry; negative simulates an expired token.
    """
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": "stored-refresh-token",
                "expires_at": time.time() + expires_in,
                "scope": (
                    "https://www.googleapis.com/auth/calendar.readonly "
                    "https://www.googleapis.com/auth/calendar.events"
                ),
            }
        ),
        encoding="utf-8",
    )


@asynccontextmanager
async def _session(stub: FakeGoogle, token_path: Path) -> AsyncIterator[Client]:
    """Connect a client to a freshly launched server subprocess.

    Args:
        stub: The running stub Google API.
        token_path: Token cache the server should read.

    Yields:
        A connected MCP client.
    """
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "gcal_mcp"],
        env={
            **os.environ,
            "GOOGLE_OAUTH_CLIENT_ID": "demo-client-id.apps.googleusercontent.com",
            "GOOGLE_OAUTH_CLIENT_SECRET": "demo-client-secret",
            "GCAL_MCP_TOKEN_PATH": str(token_path),
            "GCAL_MCP_API_BASE": stub.base_url,
            "GCAL_MCP_TOKEN_URI": stub.token_uri,
        },
    )
    async with Client(transport) as client:
        yield client


async def _step(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one tool and print it as a markdown transcript entry.

    Args:
        client: A connected MCP client.
        name: Tool name.
        arguments: Tool arguments.

    Returns:
        The structured result.
    """
    result = await client.call_tool(name, arguments, raise_on_error=False)
    payload = (
        result.data if isinstance(result.data, dict) else {"raw": str(result.data)}
    )
    print(f"**`{name}`** — arguments:")
    print("```json")
    print(json.dumps(arguments, indent=2))
    print("```")
    print("result:")
    print("```json")
    print(json.dumps(payload, indent=2))
    print("```\n")
    return payload


async def transcript_success(stub: FakeGoogle, token_path: Path) -> None:
    """Capture the happy-path chain: discover, search, read, schedule, write."""
    _seed_token(token_path, access_token="expired-access-token", expires_in=-60)
    print("### Transcript A — chained read + write\n")
    print(
        "> **Prompt:** *“Find my design review on Oct 1 and tell me who's on "
        "it, then book me a 45-minute prep block before it on my own "
        "calendar.”*\n"
    )
    print(
        "The cached access token is seeded **already expired**, so step 1 also "
        "exercises the silent refresh: the agent never sees it.\n"
    )
    async with _session(stub, token_path) as client:
        calendars = await _step(client, "list_calendars", {"max_results": 25})
        primary = next(item for item in calendars["calendars"] if item["primary"])

        found = await _step(
            client,
            "search_events",
            {
                "calendar_id": primary["calendar_id"],
                "query": "design review",
                "time_min": "2026-10-01T00:00:00-07:00",
                "time_max": "2026-10-02T00:00:00-07:00",
            },
        )
        hit = found["events"][0]

        await _step(
            client,
            "get_event",
            {"calendar_id": hit["calendar_id"], "event_id": hit["event_id"]},
        )

        slots = await _step(
            client,
            "find_free_slots",
            {
                "time_min": "2026-10-01T08:00:00-07:00",
                "time_max": hit["start"],
                "duration_minutes": 45,
                "calendar_ids": [primary["calendar_id"]],
            },
        )
        chosen = slots["slots"][0]

        await _step(
            client,
            "create_event",
            {
                "calendar_id": primary["calendar_id"],
                "summary": "Prep: design review",
                "start": chosen["start"],
                "end": chosen["suggested_end"],
                "description": "Read the deck before the review.",
            },
        )
        await _step(
            client,
            "create_event",
            {
                "calendar_id": primary["calendar_id"],
                "summary": "Prep: design review",
                "start": chosen["start"],
                "end": chosen["suggested_end"],
                "description": "Read the deck before the review.",
                "dry_run": False,
            },
        )
    print(f"Events actually created on the calendar: `{stub.state.created}`\n")


async def transcript_failures(stub: FakeGoogle, token_path: Path) -> None:
    """Capture the failure paths: bad id, rate limit, and a revoked grant."""
    print("### Transcript B — failure paths\n")
    _seed_token(token_path, access_token=VALID_ACCESS_TOKEN, expires_in=3600)
    async with _session(stub, token_path) as client:
        print("**B1. A hallucinated event id.** Non-retryable, with a way back:\n")
        await _step(
            client,
            "get_event",
            {"calendar_id": "primary", "event_id": "evt_design_review_oct1"},
        )

        print("**B2. A write to a calendar the user can only read:**\n")
        await _step(
            client,
            "create_event",
            {
                "calendar_id": "team@group.calendar.google.com",
                "summary": "Team offsite",
                "start": "2026-10-01T13:00:00-07:00",
                "end": "2026-10-01T14:00:00-07:00",
                "dry_run": False,
            },
        )

        print("**B3. A rate limit.** Retryable, with Google's own delay:\n")
        httpx.post(
            f"{stub.base_url}/__control__", json={"rate_limit_next": True}
        ).raise_for_status()
        await _step(client, "search_events", {"calendar_id": "primary"})

    print("**B4. The OAuth grant revoked mid-session.**\n")
    print(
        "The access token is expired and the user has revoked the app in their "
        "Google account settings, so the refresh fails. The server returns an "
        "instruction instead of trying to open a browser:\n"
    )
    _seed_token(token_path, access_token="expired-access-token", expires_in=-60)
    httpx.post(
        f"{stub.base_url}/__control__", json={"revoke_grant": True}
    ).raise_for_status()
    async with _session(stub, token_path) as client:
        await _step(client, "list_calendars", {})
        print(
            "The agent then confirms the credential state directly, without "
            "spending a Calendar call:\n"
        )
        await _step(client, "check_auth", {"verify": True})
        print(
            "After the user runs `gcal-mcp-login`, the same check turns green "
            "and the agent knows it is safe to retry:\n"
        )
        httpx.post(
            f"{stub.base_url}/__control__", json={"revoke_grant": False}
        ).raise_for_status()
        await _step(client, "check_auth", {"verify": True})
        await _step(client, "list_calendars", {"max_results": 5})


async def main() -> None:
    """Run both captures against one stub instance."""
    stub = FakeGoogle()
    token_path = REPO_ROOT / ".transcript-token.json"
    try:
        await transcript_success(stub, token_path)
        await transcript_failures(stub, token_path)
    finally:
        token_path.unlink(missing_ok=True)
        stub.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
