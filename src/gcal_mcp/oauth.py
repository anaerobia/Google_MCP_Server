"""OAuth 2.0 authorization-code flow with caching and silent refresh.

Two halves live here, and the split is deliberate:

* :func:`run_login_flow` is **interactive**. It opens a browser, runs a
  loopback redirect listener, and performs the PKCE code exchange. It is only
  ever called by the ``gcal-mcp-login`` CLI, never by a tool.
* :class:`CredentialProvider` is **non-interactive**. It reads the cached
  token, refreshes it silently when it is close to expiry or rejected, and
  raises :class:`~gcal_mcp.errors.AuthRequiredError` if it cannot. A
  long-running stdio server must never block a tool call on a browser.

Secrets are read from the environment only. Nothing is written to the repo;
the cache lives outside the project tree and is created mode ``0600``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Final

import httpx

from .errors import AuthRequiredError, ErrorCode, ToolError

DEFAULT_AUTH_URI: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"
DEFAULT_TOKEN_PATH: Final[Path] = Path.home() / ".config" / "gcal-mcp" / "token.json"

#: Minimal scopes. ``calendar.readonly`` covers listing calendars, searching
#: events and free/busy queries; ``calendar.events`` adds create and update on
#: events only. Neither grants the ability to delete calendars or edit sharing
#: ACLs, which the broader ``.../auth/calendar`` scope would.
SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)

#: Refresh this many seconds before the recorded expiry, so a token cannot die
#: between the check and the API call.
EXPIRY_SKEW_SECONDS: Final[int] = 120

_LOGIN_TIMEOUT_SECONDS: Final[int] = 300
_TOKEN_FILE_MODE: Final[int] = 0o600


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Everything needed to obtain and refresh a token.

    Attributes:
        client_id: OAuth client id for a Google "Desktop app" credential.
        client_secret: Matching client secret. Google issues one even for
            installed apps; it is not a confidential secret in the web sense,
            but it is still read from the environment and never committed.
        token_path: Where the cached token JSON lives.
        auth_uri: Authorization endpoint.
        token_uri: Token endpoint, overridable so tests can point at a stub.
        scopes: Scopes to request.
    """

    client_id: str
    client_secret: str
    token_path: Path
    auth_uri: str = DEFAULT_AUTH_URI
    token_uri: str = DEFAULT_TOKEN_URI
    scopes: tuple[str, ...] = SCOPES

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OAuthConfig:
        """Build a config from environment variables.

        Missing client credentials are tolerated here rather than raised: the
        server must still start and still list its tools so that an agent can
        discover them and surface an actionable error on first use.

        Args:
            env: Mapping to read instead of :data:`os.environ`, for tests.

        Returns:
            A populated config; ``client_id``/``client_secret`` may be empty.
        """
        source = os.environ if env is None else env
        raw_path = source.get("GCAL_MCP_TOKEN_PATH", "").strip()
        return cls(
            client_id=source.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            client_secret=source.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            token_path=Path(raw_path).expanduser()
            if raw_path
            else DEFAULT_TOKEN_PATH,
            auth_uri=source.get("GCAL_MCP_AUTH_URI", DEFAULT_AUTH_URI).strip(),
            token_uri=source.get("GCAL_MCP_TOKEN_URI", DEFAULT_TOKEN_URI).strip(),
        )

    def require_client(self) -> None:
        """Raise if client credentials are absent.

        Raises:
            AuthRequiredError: When either half of the client credential is
                unset, with the variable names the user must export.
        """
        missing = [
            name
            for name, value in (
                ("GOOGLE_OAUTH_CLIENT_ID", self.client_id),
                ("GOOGLE_OAUTH_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise AuthRequiredError(
                "Google OAuth client credentials are not configured "
                f"({', '.join(missing)} unset in the server environment)."
            )


@dataclass(slots=True)
class Token:
    """A cached access token and the refresh token that renews it.

    Attributes:
        access_token: Bearer token sent to the Calendar API.
        refresh_token: Long-lived grant used for silent refresh.
        expires_at: Absolute Unix time at which ``access_token`` dies.
        scope: Space-separated scopes Google actually granted.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    def is_fresh(self, *, now: float | None = None) -> bool:
        """Whether the access token is safely usable right now.

        Args:
            now: Override for the current Unix time, for tests.

        Returns:
            ``True`` if the token will still be valid after the skew window.
        """
        current = time.time() if now is None else now
        return self.expires_at - EXPIRY_SKEW_SECONDS > current

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the on-disk cache."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Token:
        """Deserialize from the on-disk cache.

        Args:
            data: Parsed token JSON.

        Returns:
            The token.

        Raises:
            KeyError: If a required field is absent.
        """
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=float(data["expires_at"]),
            scope=str(data.get("scope", "")),
        )


class TokenStore:
    """Reads and writes the token cache file with owner-only permissions."""

    def __init__(self, path: Path) -> None:
        """Initialize the store.

        Args:
            path: Location of the token JSON file.
        """
        self._path = path

    @property
    def path(self) -> Path:
        """The cache file location."""
        return self._path

    def load(self) -> Token | None:
        """Read the cached token.

        Returns:
            The token, or ``None`` if the file is absent, unreadable, or
            malformed. A corrupt cache is treated as "not logged in" rather
            than a crash, because re-running the login flow fixes both.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return None
        try:
            return Token.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def save(self, token: Token) -> None:
        """Write the token atomically with mode 0600.

        Args:
            token: The token to persist.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(token.to_dict(), indent=2), encoding="utf-8")
        temp_path.chmod(_TOKEN_FILE_MODE)
        temp_path.replace(self._path)


def _parse_token_response(
    payload: dict[str, Any], *, fallback_refresh_token: str = ""
) -> Token:
    """Convert a Google token endpoint payload into a :class:`Token`.

    Args:
        payload: Decoded JSON from the token endpoint.
        fallback_refresh_token: Used when the response omits
            ``refresh_token``, which Google does on every refresh.

    Returns:
        The resulting token.

    Raises:
        AuthRequiredError: If no access token or no refresh token is available.
    """
    access_token = str(payload.get("access_token", ""))
    if not access_token:
        raise AuthRequiredError(
            "Google's token endpoint returned no access_token.", expired=True
        )
    refresh_token = str(payload.get("refresh_token", "")) or fallback_refresh_token
    if not refresh_token:
        raise AuthRequiredError(
            "Google returned no refresh_token, so this session could not be "
            "renewed silently. Re-run login with prompt=consent.",
            expired=True,
        )
    expires_in = float(payload.get("expires_in", 3600))
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + expires_in,
        scope=str(payload.get("scope", "")),
    )


class CredentialProvider:
    """Supplies a fresh access token to the API client, never interactively.

    The provider is shared by every tool call, so refresh is guarded by an
    :class:`asyncio.Lock`: ten concurrent calls that all see an expired token
    perform one refresh, not ten.
    """

    def __init__(
        self,
        config: OAuthConfig,
        *,
        store: TokenStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            config: OAuth endpoints, client credentials and cache location.
            store: Token cache; defaults to one at ``config.token_path``.
            client: HTTP client used for refresh; one is created on demand
                when omitted.
        """
        self._config = config
        self._store = store or TokenStore(config.token_path)
        self._client = client
        self._owns_client = client is None
        self._token: Token | None = None
        self._lock = asyncio.Lock()

    @property
    def token_path(self) -> Path:
        """Where this provider looks for cached credentials."""
        return self._store.path

    async def aclose(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating it on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
            self._owns_client = True
        return self._client

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable bearer token, refreshing silently if needed.

        Args:
            force_refresh: Refresh even if the cached token still looks fresh.
                The API client sets this after a single 401, which is how a
                token revoked mid-session is distinguished from one that is
                merely stale.

        Returns:
            A bearer access token.

        Raises:
            AuthRequiredError: If no token is cached, or the refresh token has
                been revoked or expired. The message tells the agent to have
                the user run the login command; the server does not open a
                browser.
        """
        async with self._lock:
            if self._token is None:
                self._token = self._store.load()
            if self._token is None:
                raise AuthRequiredError(
                    "No cached Google credentials were found at "
                    f"{self._store.path}."
                )
            if not force_refresh and self._token.is_fresh():
                return self._token.access_token
            self._token = await self._refresh(self._token)
            self._store.save(self._token)
            return self._token.access_token

    async def _refresh(self, token: Token) -> Token:
        """Exchange a refresh token for a new access token.

        Args:
            token: The token holding the refresh grant.

        Returns:
            A new token carrying the refreshed access token.

        Raises:
            AuthRequiredError: On ``invalid_grant`` (revoked or expired
                grant), which no retry can fix.
            ToolError: On network failure or an unexpected token-endpoint
                status, flagged retryable.
        """
        self._config.require_client()
        try:
            response = await self._http().post(
                self._config.token_uri,
                data={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "refresh_token": token.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(
                ErrorCode.NETWORK_ERROR,
                f"Could not reach Google's token endpoint: {exc}.",
                retry_after_seconds=5,
                remedy="Check connectivity, then retry the same call.",
            ) from exc

        if response.is_success:
            return _parse_token_response(
                response.json(), fallback_refresh_token=token.refresh_token
            )

        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            body = {}
        reason = str(body.get("error", "")) or f"HTTP {response.status_code}"
        if reason in {"invalid_grant", "invalid_client", "unauthorized_client"}:
            raise AuthRequiredError(
                "Google refused to refresh the cached token "
                f"({reason}); access was revoked or the grant expired.",
                expired=True,
            )
        raise ToolError(
            ErrorCode.UPSTREAM_ERROR,
            f"Google's token endpoint returned {reason} during refresh.",
            retry_after_seconds=10,
            remedy="Transient; retry the same call.",
        )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures the ``?code=`` redirect."""

    #: Populated by :func:`_serve_once` before the request arrives.
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        """Record the query parameters and show the user a closing page."""
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        type(self).result.update(params)
        ok = "code" in params
        body = (
            "<h2>Authorization complete.</h2><p>You can close this tab and "
            "return to the terminal.</p>"
            if ok
            else f"<h2>Authorization failed.</h2><pre>{params}</pre>"
        )
        encoded = body.encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log."""


def _serve_once(server: HTTPServer, timeout: int) -> dict[str, str]:
    """Handle exactly one redirect request, or time out.

    Args:
        server: A bound, unstarted HTTP server.
        timeout: Seconds to wait for the browser redirect.

    Returns:
        The query parameters of the redirect.

    Raises:
        AuthRequiredError: If no redirect arrives before the timeout.
    """
    _CallbackHandler.result = {}
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AuthRequiredError(
            f"No OAuth redirect received within {timeout}s; login aborted."
        )
    return dict(_CallbackHandler.result)


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE ``(verifier, S256 challenge)`` pair.

    Returns:
        The verifier to send at exchange time and the challenge to send at
        authorization time.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def run_login_flow(
    config: OAuthConfig,
    *,
    open_browser: bool = True,
    timeout: int = _LOGIN_TIMEOUT_SECONDS,
) -> Token:
    """Run the interactive authorization-code flow and cache the result.

    PKCE is used, the redirect listener binds an ephemeral loopback port, and
    ``state`` is verified on return. ``access_type=offline`` with
    ``prompt=consent`` guarantees a refresh token even on re-authorization,
    which is what makes later refreshes silent.

    Args:
        config: Client credentials, endpoints and cache location.
        open_browser: Whether to launch a browser automatically. The URL is
            always printed so a headless user can paste it.
        timeout: Seconds to wait for the redirect.

    Returns:
        The newly cached token.

    Raises:
        AuthRequiredError: If credentials are unset, the user denies consent,
            ``state`` does not match, or the code exchange fails.
        ToolError: If the token endpoint is unreachable.
    """
    config.require_client()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(config.scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{config.auth_uri}?{urllib.parse.urlencode(params)}"

    print("Open this URL to authorize Google Calendar access:\n")
    print(auth_url, "\n")
    if open_browser:
        webbrowser.open(auth_url)

    try:
        redirect = _serve_once(server, timeout)
    finally:
        server.server_close()

    if "error" in redirect:
        raise AuthRequiredError(
            f"Authorization was denied by the user ({redirect['error']})."
        )
    if redirect.get("state") != state:
        raise AuthRequiredError(
            "OAuth state mismatch; the redirect did not match this request."
        )
    code = redirect.get("code", "")
    if not code:
        raise AuthRequiredError("The OAuth redirect carried no authorization code.")

    try:
        response = httpx.post(
            config.token_uri,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            ErrorCode.NETWORK_ERROR,
            f"Could not reach Google's token endpoint: {exc}.",
            retry_after_seconds=5,
        ) from exc

    if not response.is_success:
        raise AuthRequiredError(
            "Authorization-code exchange failed "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )

    token = _parse_token_response(response.json())
    TokenStore(config.token_path).save(token)
    return token
