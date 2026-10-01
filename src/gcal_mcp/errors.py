"""Structured, agent-readable errors for the Google Calendar MCP server.

Tools in this server never raise tracebacks across the MCP boundary. A
traceback ends an agent's turn and tells it nothing about whether to retry.
Instead every failure is returned as a small JSON object:

    {"error": {"code": ..., "message": ..., "retryable": ..., "remedy": ...}}

``code`` is a closed vocabulary (:class:`ErrorCode`) so an agent can branch on
it, and ``retryable`` answers the only question that matters at the call site:
should I call this tool again, or do I need to change my arguments / ask the
user for help?
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

import httpx


class ErrorCode(StrEnum):
    """Closed vocabulary of failure modes an agent may observe.

    The ``retryable`` flag that accompanies each code in the payload is
    derived from :data:`RETRYABLE_CODES`, so the two can never disagree.
    """

    AUTH_REQUIRED = "auth_required"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    NETWORK_ERROR = "network_error"


#: Codes where calling the same tool with the same arguments may succeed later.
RETRYABLE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_ERROR,
        ErrorCode.NETWORK_ERROR,
    }
)

DEFAULT_RETRY_AFTER_SECONDS: Final[int] = 30
LOGIN_COMMAND: Final[str] = "uv run gcal-mcp-login"

_AUTH_REMEDY: Final[str] = (
    f"Ask the user to run `{LOGIN_COMMAND}` in a terminal, then retry. "
    "This server will not open a browser during a tool call."
)


class ToolError(Exception):
    """A failure that should reach the agent as data rather than a traceback.

    Args:
        code: The failure mode.
        message: One sentence an agent can act on or relay to the user.
        remedy: Optional concrete next step (a command to run, an argument to
            change). Omitted from the payload when ``None``.
        retry_after_seconds: Hint for retryable codes; defaults to
            :data:`DEFAULT_RETRY_AFTER_SECONDS` for rate limits.
        details: Optional extra fields (e.g. the offending id) merged into the
            error object. Keep it small; this is tokens the agent pays for.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        remedy: str | None = None,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remedy = remedy
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        """Whether retrying the identical call could plausibly succeed."""
        return self.code in RETRYABLE_CODES

    def to_payload(self) -> dict[str, Any]:
        """Render the error as the dict a tool returns to the agent.

        Returns:
            A single-key ``{"error": {...}}`` mapping. The ``error`` key is
            always present on failure and never present on success, so an
            agent can test for it without inspecting types.
        """
        error: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after_seconds is not None:
            error["retry_after_seconds"] = self.retry_after_seconds
        if self.remedy:
            error["remedy"] = self.remedy
        error.update(self.details)
        return {"error": error}


class AuthRequiredError(ToolError):
    """No usable credentials: the user has never authorized, or the grant died.

    Args:
        message: What specifically is missing or broken.
        expired: ``True`` when a token existed but can no longer be refreshed
            (revoked, or the refresh token itself expired), which is the
            mid-session death case; ``False`` when the user never logged in.
    """

    def __init__(self, message: str, *, expired: bool = False) -> None:
        super().__init__(
            ErrorCode.AUTH_EXPIRED if expired else ErrorCode.AUTH_REQUIRED,
            message,
            remedy=_AUTH_REMEDY,
        )


def _retry_after(response: httpx.Response, fallback: int) -> int:
    """Read ``Retry-After`` as whole seconds, falling back when absent or odd.

    Args:
        response: The upstream response carrying the header.
        fallback: Value to use when the header is missing or not an integer.

    Returns:
        A non-negative retry delay in seconds.
    """
    raw = response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return int(raw)
    return fallback


def _google_error_fields(response: httpx.Response) -> tuple[str, str]:
    """Extract Google's ``(message, reason)`` pair from an error response.

    Google returns ``{"error": {"message": ..., "errors": [{"reason": ...}]}}``
    for Calendar API failures. Both fields are best-effort: a proxy or an HTML
    error page yields no JSON at all.

    Args:
        response: The failed upstream response.

    Returns:
        A ``(message, reason)`` tuple; either element may be an empty string.
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text[:200].strip(), "")
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ("", "")
    message = str(error.get("message", "")).strip()
    reasons = error.get("errors")
    reason = ""
    if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
        reason = str(reasons[0].get("reason", "")).strip()
    return (message, reason)


_RATE_LIMIT_REASONS: Final[frozenset[str]] = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
)


def from_http_status(
    response: httpx.Response,
    *,
    context: str,
    details: dict[str, Any] | None = None,
) -> ToolError:
    """Translate a failed Calendar API response into a :class:`ToolError`.

    The mapping exists so that HTTP semantics the agent cannot see become the
    retryable/non-retryable distinction that it can.

    Args:
        response: The non-2xx upstream response.
        context: Short description of the attempted operation, used as the
            message prefix (e.g. ``"get event"``).
        details: Extra fields to attach, typically the ids involved.

    Returns:
        A :class:`ToolError` whose code reflects the status and Google reason.
    """
    status = response.status_code
    message, reason = _google_error_fields(response)
    suffix = f": {message}" if message else ""

    if status == httpx.codes.UNAUTHORIZED:
        return AuthRequiredError(
            f"Google rejected the access token while trying to {context}"
            f"{suffix}.",
            expired=True,
        )
    if status == httpx.codes.TOO_MANY_REQUESTS or reason in _RATE_LIMIT_REASONS:
        return ToolError(
            ErrorCode.RATE_LIMITED,
            f"Google rate-limited the request to {context}{suffix}.",
            retry_after_seconds=_retry_after(
                response, DEFAULT_RETRY_AFTER_SECONDS
            ),
            remedy="Wait for retry_after_seconds, then retry the same call.",
            details=details,
        )
    if status == httpx.codes.FORBIDDEN:
        return ToolError(
            ErrorCode.PERMISSION_DENIED,
            f"Not allowed to {context}{suffix}.",
            remedy=(
                "The signed-in account may lack write access to this "
                "calendar, or the granted scopes are read-only. Check "
                "access_role from list_calendars."
            ),
            details=details,
        )
    if status == httpx.codes.NOT_FOUND:
        return ToolError(
            ErrorCode.NOT_FOUND,
            f"Nothing found to {context}{suffix}.",
            remedy=(
                "Do not retry with the same ids. Re-resolve them with "
                "list_calendars or search_events."
            ),
            details=details,
        )
    if status in (httpx.codes.CONFLICT, httpx.codes.PRECONDITION_FAILED):
        return ToolError(
            ErrorCode.CONFLICT,
            f"Conflicting state while trying to {context}{suffix}.",
            remedy="Re-read the event with get_event, then reapply the change.",
            details=details,
        )
    if status in (httpx.codes.BAD_REQUEST, httpx.codes.UNPROCESSABLE_ENTITY):
        return ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"Google rejected the arguments for {context}{suffix}.",
            remedy="Fix the arguments named in the message; do not retry as-is.",
            details=details,
        )
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        return ToolError(
            ErrorCode.UPSTREAM_ERROR,
            f"Google returned {status} while trying to {context}{suffix}.",
            retry_after_seconds=_retry_after(response, 5),
            remedy="Transient on Google's side; retry the same call.",
            details=details,
        )
    return ToolError(
        ErrorCode.UPSTREAM_ERROR,
        f"Unexpected HTTP {status} while trying to {context}{suffix}.",
        retry_after_seconds=_retry_after(response, 5),
        details=details,
    )
