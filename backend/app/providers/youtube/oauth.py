"""OAuth 2.0 Authorization Code + PKCE loopback flow (desktop app client).

IMPORTANT — why OAuth is the only option:

- **API keys cannot upload.** An API key identifies the *project*, not a
  channel owner, and ``videos.insert`` rejects key-only auth.
- **Service accounts do not work for YouTube channels.** A service account
  has no linked YouTube channel (Google returns
  ``youtubeSignupRequired`` / NoLinkedYouTubeAccount); uploads require a
  user access token granted by the channel owner.

So this app connects each channel with the standard installed-app flow:

1. Generate ``state`` (>=128-bit random, CSRF binding) and a PKCE pair.
   The ``code_verifier`` is high-entropy (43-128 chars) and is kept ONLY
   in sidecar memory — it never goes to the browser, disk, or logs.
2. Bind a one-shot loopback listener on ``127.0.0.1`` (ephemeral port,
   ~180s timeout) and open the authorization URL in the OS browser.
3. The listener receives ``?code=...&state=...``, rejects a mismatched
   state, and shuts down.
4. Exchange the code at the token endpoint. ``access_type=offline`` +
   ``prompt=consent`` are sent on EVERY new channel connection so Google
   always issues a refresh token.

Scopes are requested incrementally (roadmap Section 25): at connect time
``youtube.upload`` + ``youtube`` (the full publish package — thumbnail /
captions / playlists cannot complete on upload scope alone);
``yt-analytics.readonly`` is added later, when analytics is enabled.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from ..http_client import create_async_client
from .errors import OAuthDenied, OAuthError, StateMismatch

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube"
SCOPE_YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_ANALYTICS_READONLY = "https://www.googleapis.com/auth/yt-analytics.readonly"

#: Scopes requested at first channel connection.
CONNECT_SCOPES: tuple[str, ...] = (SCOPE_UPLOAD, SCOPE_YOUTUBE)

DEFAULT_CALLBACK_TIMEOUT = 180.0

_SUCCESS_HTML = (
    b"<html><head><title>Connected</title></head><body>"
    b"<h1>Connected - you can close this tab.</h1></body></html>"
)


def generate_state() -> str:
    """Return a fresh CSRF ``state`` value with >=128 bits of entropy."""
    return secrets.token_urlsafe(32)  # 256 bits


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256.

    The verifier is 43-128 chars from the unreserved URI charset
    ``[A-Z a-z 0-9 - . _ ~]`` (we use the base64url subset). The challenge
    is ``base64url(SHA256(verifier))`` with padding stripped.
    """
    verifier = secrets.token_urlsafe(64)  # 86 chars, 512 bits of entropy
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...] | list[str] | None = None,
    login_hint: str | None = None,
) -> str:
    """Build the Google authorization URL for the loopback flow.

    Always includes ``access_type=offline`` and ``prompt=consent`` so a
    refresh token is issued on every new channel connection, plus
    ``include_granted_scopes=true`` for incremental scope grants.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes if scopes is not None else CONNECT_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


@dataclass
class TokenResponse:
    """Parsed response from Google's token endpoint.

    ``access_token`` / ``refresh_token`` are secret values: never log them,
    never send them to the frontend.
    """

    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    scope: str = ""
    token_type: str = "Bearer"
    #: Present on tokens issued while the consent screen is in Testing
    #: mode (~604800s = 7 days). The TokenManager turns this into the
    #: ``testing_mode_token`` warning flag.
    refresh_token_expires_in: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def scopes(self) -> list[str]:
        return self.scope.split() if self.scope else []

    @classmethod
    def from_json(cls, data: dict) -> "TokenResponse":
        rte = data.get("refresh_token_expires_in")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=int(data.get("expires_in", 3600)),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
            refresh_token_expires_in=int(rte) if rte is not None else None,
            raw=dict(data),
        )


async def exchange_code(
    http: httpx.AsyncClient | None = None,
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> TokenResponse:
    """Exchange an authorization ``code`` for tokens (one-shot).

    Sends the original ``code_verifier`` — the intercepted code is useless
    to anyone who does not hold it.
    """
    form = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if http is not None:
        resp = await http.post(TOKEN_URL, data=form)
    else:
        async with create_async_client(timeout=30.0) as client:
            resp = await client.post(TOKEN_URL, data=form)
    if resp.status_code != 200:
        raise OAuthError(
            f"token exchange failed with status {resp.status_code}",
        )
    return TokenResponse.from_json(resp.json())


class LoopbackReceiver:
    """One-shot HTTP listener on 127.0.0.1 that receives the OAuth code.

    Factored out of :func:`wait_for_code` so tests (and the UI layer) can
    drive the listener directly — or bypass it entirely by stubbing
    :func:`wait_for_code` / :func:`exchange_code`.
    """

    def __init__(
        self,
        expected_state: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._expected_state = expected_state
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._future: asyncio.Future[str] | None = None

    @property
    def redirect_uri(self) -> str:
        if self._server is None or not self._server.sockets:
            raise OAuthError("loopback listener is not running")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{self._host}:{port}"

    async def start(self) -> str:
        """Bind the listener and return the redirect URI to embed in the
        authorization URL."""
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port
        )
        return self.redirect_uri

    async def wait(self, timeout: float = DEFAULT_CALLBACK_TIMEOUT) -> str:
        """Wait (up to ``timeout`` seconds) for the code; one-shot."""
        if self._future is None:
            raise OAuthError("loopback listener is not running")
        try:
            return await asyncio.wait_for(asyncio.shield(self._future), timeout)
        except asyncio.TimeoutError:
            raise OAuthError(
                f"timed out after {timeout}s waiting for OAuth callback"
            ) from None

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            # Drain headers.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            target = parts[1] if len(parts) >= 2 else "/"
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)

            def _reply(status: int, body: bytes) -> None:
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )

            fut = self._future
            if fut is not None and not fut.done():
                error = _first(query, "error")
                if error:
                    _reply(400, b"<h1>Authorization denied.</h1>")
                    fut.set_exception(
                        OAuthDenied(f"authorization denied: {error}")
                    )
                elif _first(query, "state") != self._expected_state:
                    _reply(400, b"<h1>State mismatch.</h1>")
                    fut.set_exception(
                        StateMismatch("OAuth callback state does not match")
                    )
                else:
                    code = _first(query, "code")
                    if not code:
                        _reply(400, b"<h1>Missing code.</h1>")
                        fut.set_exception(OAuthError("callback carried no code"))
                    else:
                        _reply(200, _SUCCESS_HTML)
                        fut.set_result(code)
            await writer.drain()
        except Exception:
            # A malformed probe connection must not kill the listener.
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


async def wait_for_code(
    expected_state: str,
    *,
    timeout: float = DEFAULT_CALLBACK_TIMEOUT,
    host: str = "127.0.0.1",
    port: int = 0,
    on_redirect_uri: Callable[[str], None | Awaitable[None]] | None = None,
) -> tuple[str, str]:
    """Run the loopback listener and wait for the authorization code.

    Returns ``(code, redirect_uri)``. ``on_redirect_uri`` (sync or async)
    is invoked with the redirect URI as soon as the listener is bound, so
    the caller can build the authorization URL and open the browser.
    The listener is one-shot and is always closed before returning.
    """
    receiver = LoopbackReceiver(expected_state, host=host, port=port)
    redirect_uri = await receiver.start()
    try:
        if on_redirect_uri is not None:
            result = on_redirect_uri(redirect_uri)
            if asyncio.iscoroutine(result):
                await result
        code = await receiver.wait(timeout)
        return code, redirect_uri
    finally:
        await receiver.close()
