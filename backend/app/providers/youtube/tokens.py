"""Token Manager — the single place that owns YouTube access tokens.

Per channel, a credential blob lives in the injected
:class:`~.secrets.SecretStore` (production: Windows Credential
Manager/DPAPI) under ``youtube:channel:<channel_id>``, containing the
refresh token, granted scopes and ``obtained_at``. Access tokens are
cached in memory only.

Rules (roadmap Section 25):

- **Single-flight refresh:** concurrent refreshes for one channel
  collapse into ONE HTTP call (10 parallel agents must not trigger 10
  refreshes).
- ``invalid_grant`` on refresh -> the channel is marked ``needs_reauth``
  and :class:`~.errors.NeedsReauth` is raised; callers pause that
  channel's jobs until a human reconnects it.
- **7-day Testing-mode trap:** if a token response carries
  ``refresh_token_expires_in`` ~= 604800s, the consent screen is still in
  Testing mode and the refresh token will die in 7 days. Surfaced as the
  ``testing_mode_token`` warning flag on the credential status — it is a
  release blocker, not a silent state.
- **Disconnect** = call Google's revoke endpoint AND delete the local
  blob (local deletion happens even if the revoke call fails).
- Exactly ONE stored refresh token per channel, reused — never re-run the
  consent flow per upload (issuing new tokens invalidates older ones).

Token values are never logged and never leave this module: the UI only
sees :class:`CredentialStatus`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from ..http_client import create_async_client
from .errors import NeedsReauth, OAuthError
from .oauth import REVOKE_URL, SCOPE_UPLOAD, TOKEN_URL, TokenResponse
from .secrets import SecretStore

#: refresh_token_expires_in for Testing-mode projects: 7 days, in seconds.
TESTING_MODE_REFRESH_TTL = 604_800

STATUS_CONNECTED = "connected"
STATUS_EXPIRED = "expired"
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUS_NOT_CONNECTED = "not_connected"


def _ref_for(channel_id: str) -> str:
    return f"youtube:channel:{channel_id}"


def _is_testing_mode_ttl(refresh_token_expires_in: int | None) -> bool:
    """True when a TTL looks like the 7-day Testing-mode expiry."""
    if refresh_token_expires_in is None:
        return False
    # "~604800": allow an hour of clock/rounding slack either side.
    return TESTING_MODE_REFRESH_TTL - 3600 <= refresh_token_expires_in <= (
        TESTING_MODE_REFRESH_TTL + 3600
    )


@dataclass
class CredentialStatus:
    """The ONLY credential information the frontend/UI may ever see."""

    channel_id: str
    status: str  # connected | expired | needs_reauth | not_connected
    scopes: list[str] = field(default_factory=list)
    obtained_at: float | None = None
    testing_mode_token: bool = False


@dataclass
class _Blob:
    refresh_token: str
    scopes: list[str]
    obtained_at: float
    status: str = STATUS_CONNECTED
    testing_mode_token: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "refresh_token": self.refresh_token,
                "scopes": self.scopes,
                "obtained_at": self.obtained_at,
                "status": self.status,
                "testing_mode_token": self.testing_mode_token,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "_Blob":
        data = json.loads(raw)
        return cls(
            refresh_token=data["refresh_token"],
            scopes=list(data.get("scopes", [])),
            obtained_at=float(data.get("obtained_at", 0.0)),
            status=data.get("status", STATUS_CONNECTED),
            testing_mode_token=bool(data.get("testing_mode_token", False)),
        )


@dataclass
class _AccessCache:
    token: str
    expires_at: float  # epoch seconds


class TokenManager:
    """Owns refresh/access tokens for every connected channel."""

    def __init__(
        self,
        secret_store: SecretStore,
        *,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient | None = None,
        expiry_margin: float = 60.0,
    ) -> None:
        self._store = secret_store
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._expiry_margin = expiry_margin
        self._cache: dict[str, _AccessCache] = {}
        self._inflight: dict[str, asyncio.Task[str]] = {}

    # -- storage ------------------------------------------------------

    def _load_blob(self, channel_id: str) -> _Blob | None:
        raw = self._store.get(_ref_for(channel_id))
        if raw is None:
            return None
        return _Blob.from_json(raw)

    def _save_blob(self, channel_id: str, blob: _Blob) -> None:
        self._store.put(_ref_for(channel_id), blob.to_json())

    def store_token_response(
        self, channel_id: str, token_response: TokenResponse
    ) -> CredentialStatus:
        """Persist a fresh token response (from the connect flow).

        If the response carries no refresh token (re-consent without
        ``prompt=consent``), the previously stored one is kept.
        """
        existing = self._load_blob(channel_id)
        refresh_token = token_response.refresh_token or (
            existing.refresh_token if existing else None
        )
        if refresh_token is None:
            raise OAuthError(
                "token response carried no refresh token; the connect flow "
                "must use access_type=offline + prompt=consent"
            )
        if token_response.refresh_token_expires_in is not None:
            testing = _is_testing_mode_ttl(token_response.refresh_token_expires_in)
        elif existing is not None:
            testing = existing.testing_mode_token
        else:
            testing = False
        blob = _Blob(
            refresh_token=refresh_token,
            scopes=token_response.scopes
            or (existing.scopes if existing else []),
            obtained_at=time.time(),
            status=STATUS_CONNECTED,
            testing_mode_token=testing,
        )
        self._save_blob(channel_id, blob)
        self._set_cache(channel_id, token_response)
        return self.get_status(channel_id)

    def get_status(self, channel_id: str) -> CredentialStatus:
        """Status for the UI — never includes any token value."""
        blob = self._load_blob(channel_id)
        if blob is None:
            return CredentialStatus(
                channel_id=channel_id, status=STATUS_NOT_CONNECTED
            )
        return CredentialStatus(
            channel_id=channel_id,
            status=blob.status,
            scopes=list(blob.scopes),
            obtained_at=blob.obtained_at,
            testing_mode_token=blob.testing_mode_token,
        )

    def require_upload_scope(self, channel_id: str) -> None:
        """Refuse (typed, locally, BEFORE any HTTP) when the stored
        grant cannot upload.

        The upload path calls this before obtaining/using an access
        token: the stored grant's scopes must include
        :data:`~.oauth.SCOPE_UPLOAD` (``youtube.upload`` — one of the
        scopes requested at connect time, see ``CONNECT_SCOPES``). A
        missing grant, a grant already marked ``needs_reauth``, or a
        grant whose scopes lack the upload scope all raise
        :class:`~.errors.NeedsReauth` — the user must (re)connect the
        channel; Google's own 403 is never the first line of defence.
        """
        blob = self._load_blob(channel_id)
        if blob is None:
            raise NeedsReauth(
                channel_id, f"channel {channel_id!r} is not connected"
            )
        if blob.status == STATUS_NEEDS_REAUTH:
            raise NeedsReauth(channel_id)
        if SCOPE_UPLOAD not in blob.scopes:
            raise NeedsReauth(
                channel_id,
                f"channel {channel_id!r} was connected without the "
                f"YouTube upload scope ({SCOPE_UPLOAD}); reconnect the "
                "channel to grant upload permission",
            )

    # -- access tokens --------------------------------------------------

    def _set_cache(self, channel_id: str, token_response: TokenResponse) -> None:
        self._cache[channel_id] = _AccessCache(
            token=token_response.access_token,
            expires_at=time.time() + max(0, token_response.expires_in),
        )

    def _cached_token(self, channel_id: str) -> str | None:
        cached = self._cache.get(channel_id)
        if cached and cached.expires_at - self._expiry_margin > time.time():
            return cached.token
        return None

    async def get_access_token(self, channel_id: str) -> str:
        """Return a valid access token, refreshing first if needed."""
        cached = self._cached_token(channel_id)
        if cached is not None:
            return cached
        return await self.refresh(channel_id)

    async def refresh(self, channel_id: str) -> str:
        """Refresh the access token for ``channel_id`` (single-flight).

        Concurrent calls for the same channel share one in-flight HTTP
        request. Raises :class:`NeedsReauth` on ``invalid_grant``.
        """
        task = self._inflight.get(channel_id)
        if task is None:
            task = asyncio.get_running_loop().create_task(
                self._do_refresh(channel_id)
            )
            self._inflight[channel_id] = task
            task.add_done_callback(
                lambda _t, cid=channel_id: self._inflight.pop(cid, None)
            )
        return await task

    async def _do_refresh(self, channel_id: str) -> str:
        blob = self._load_blob(channel_id)
        if blob is None:
            raise NeedsReauth(channel_id, f"channel {channel_id!r} is not connected")
        if blob.status == STATUS_NEEDS_REAUTH:
            raise NeedsReauth(channel_id)
        form = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": blob.refresh_token,
            "grant_type": "refresh_token",
        }
        resp = await self._post(TOKEN_URL, form)
        if resp.status_code != 200:
            error = _extract_error(resp)
            if error == "invalid_grant":
                blob.status = STATUS_NEEDS_REAUTH
                self._save_blob(channel_id, blob)
                self._cache.pop(channel_id, None)
                raise NeedsReauth(channel_id)
            raise OAuthError(
                f"token refresh failed with status {resp.status_code} ({error})"
            )
        token_response = TokenResponse.from_json(resp.json())
        # Google normally does not rotate refresh tokens on refresh; if it
        # does, persist the new one (and re-evaluate the Testing-mode flag).
        if token_response.refresh_token:
            blob.refresh_token = token_response.refresh_token
            blob.obtained_at = time.time()
        if token_response.refresh_token_expires_in is not None:
            blob.testing_mode_token = _is_testing_mode_ttl(
                token_response.refresh_token_expires_in
            )
        if token_response.scopes:
            blob.scopes = token_response.scopes
        blob.status = STATUS_CONNECTED
        self._save_blob(channel_id, blob)
        self._set_cache(channel_id, token_response)
        return token_response.access_token

    # -- disconnect -----------------------------------------------------

    async def disconnect(self, channel_id: str) -> None:
        """Revoke the refresh token at Google and delete the local blob.

        The local blob is deleted even if the revoke call fails — the user
        asked to disconnect this machine.
        """
        blob = self._load_blob(channel_id)
        try:
            if blob is not None:
                try:
                    await self._post(REVOKE_URL, {"token": blob.refresh_token})
                except Exception:
                    pass  # local deletion below is unconditional
        finally:
            self._store.delete(_ref_for(channel_id))
            self._cache.pop(channel_id, None)

    # -- http -----------------------------------------------------------

    async def _post(self, url: str, form: dict[str, str]) -> httpx.Response:
        if self._http is not None:
            return await self._http.post(url, data=form)
        async with create_async_client(timeout=30.0) as client:
            return await client.post(url, data=form)


def _extract_error(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            return error.get("status") or error.get("message")
    return None
