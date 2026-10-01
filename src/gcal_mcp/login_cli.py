"""Interactive one-time login command: ``gcal-mcp-login``.

The browser step lives here rather than in the server on purpose. A stdio MCP
server has no terminal and no user attention; if it tried to open a browser
mid-tool-call it would hang the agent's turn. So authorization is a separate,
human-run command, and the server only ever reads the cache it writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from .errors import ToolError
from .oauth import OAuthConfig, TokenStore, run_login_flow


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="gcal-mcp-login",
        description=(
            "Authorize this machine to read and write the signed-in user's "
            "Google Calendar, and cache the resulting token."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report the cached token's state and exit without authorizing.",
    )
    return parser.parse_args(argv)


def _print_status(config: OAuthConfig) -> int:
    """Print the state of the cached token.

    Args:
        config: Resolved OAuth configuration.

    Returns:
        ``0`` if a token is cached, ``1`` otherwise.
    """
    token = TokenStore(config.token_path).load()
    print(f"token cache: {config.token_path}")
    print(
        "oauth client: "
        + ("configured" if config.client_id and config.client_secret else "MISSING")
    )
    if token is None:
        print("status:       not authorized (no cached token)")
        return 1
    expires = dt.datetime.fromtimestamp(token.expires_at, tz=dt.timezone.utc)
    print(f"status:       authorized ({'fresh' if token.is_fresh() else 'stale'})")
    print(f"expires at:   {expires.isoformat()}")
    print(f"scopes:       {token.scope or '(not reported)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the login command.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` on success, ``1`` on any auth failure.
    """
    args = _parse_args(argv)
    config = OAuthConfig.from_env()

    if args.status:
        return _print_status(config)

    try:
        token = run_login_flow(config, open_browser=not args.no_browser)
    except ToolError as exc:
        payload = exc.to_payload()["error"]
        print(
            f"\nLogin failed [{payload['code']}]: {payload['message']}",
            file=sys.stderr,
        )
        if payload.get("remedy"):
            print(payload["remedy"], file=sys.stderr)
        return 1

    expires = dt.datetime.fromtimestamp(token.expires_at, tz=dt.timezone.utc)
    print("Authorized. Token cached at", config.token_path)
    print("Access token expires at", expires.isoformat())
    print("Granted scopes:", token.scope or "(not reported)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
