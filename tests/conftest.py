"""Fixtures that boot the real server over stdio against a stubbed Google."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from fake_google import FakeGoogle

#: The stub only accepts this bearer token, so a cached token holding it is
#: "already valid" and a cached token holding anything else must be refreshed.
VALID_ACCESS_TOKEN = "refreshed-access-token"


@pytest.fixture
def fake_google() -> Any:
    """Start the stub Google API and shut it down after the test."""
    stub = FakeGoogle()
    try:
        yield stub
    finally:
        stub.shutdown()


def write_token(
    path: Path,
    *,
    access_token: str = VALID_ACCESS_TOKEN,
    expires_in: float = 3600.0,
    refresh_token: str = "stored-refresh-token",
) -> None:
    """Seed the token cache the server will read.

    Args:
        path: Token cache location.
        access_token: Cached access token.
        expires_in: Seconds until expiry; pass a negative value to simulate an
            already-expired token.
        refresh_token: Cached refresh grant.
    """
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
                "scope": (
                    "https://www.googleapis.com/auth/calendar.readonly "
                    "https://www.googleapis.com/auth/calendar.events"
                ),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """Path to this test's isolated token cache."""
    return tmp_path / "token.json"


@pytest.fixture
def connect(
    fake_google: Any, token_path: Path
) -> Callable[..., Any]:
    """Return a factory that connects an MCP client to a fresh server process.

    The server is launched as a real subprocess speaking MCP over stdio, so
    every assertion in the suite goes through the protocol — schema
    validation, structured content and all — rather than calling the Python
    functions directly.

    Returns:
        An async context manager factory accepting environment overrides.
    """

    @asynccontextmanager
    async def _connect(**env_overrides: str) -> AsyncIterator[Client]:
        env = {
            **os.environ,
            "GOOGLE_OAUTH_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
            "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
            "GCAL_MCP_TOKEN_PATH": str(token_path),
            "GCAL_MCP_API_BASE": fake_google.base_url,
            "GCAL_MCP_TOKEN_URI": fake_google.token_uri,
            **env_overrides,
        }
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "gcal_mcp"],
            env=env,
        )
        async with Client(transport) as client:
            yield client

    return _connect


async def call(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool through the protocol and return its structured result.

    Args:
        client: A connected MCP client.
        name: Tool name.
        **arguments: Tool arguments.

    Returns:
        The tool's result object.
    """
    result = await client.call_tool(name, arguments)
    assert isinstance(result.data, dict), f"{name} returned {result.data!r}"
    return result.data


def control(stub: Any, **flags: bool) -> None:
    """Flip a behaviour flag on the stub Google server.

    Args:
        stub: The :class:`FakeGoogle` instance.
        **flags: Flags such as ``revoke_grant`` or ``rate_limit_next``.
    """
    httpx.post(f"{stub.base_url}/__control__", json=flags).raise_for_status()
