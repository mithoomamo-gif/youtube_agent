"""Channel endpoints (roadmap sections 25 / 27 / 31).

Channels are read from the SQLite ``channels`` table (owned by app.db).
Token material is NEVER returned and NEVER logged: any column whose name
suggests credential material is stripped from responses, and the only
credential information exposed anywhere is a status object
(connected / needs_reauth / needs_audit_check / ...).

Connect flow (REAL, Round 3)
-----------------------------
``POST /api/v1/channels/connect`` runs the installed-app OAuth flow from
:mod:`app.providers.youtube.oauth` (Authorization Code + PKCE S256,
256-bit ``state``, verifier kept in memory only, one-shot loopback
listener on 127.0.0.1, ``access_type=offline`` + ``prompt=consent``):

1. The OAuth **client** credentials are read from the secret store at
   refs ``youtube/client_id`` / ``youtube/client_secret``. When either
   is absent the endpoint returns **409** ``youtube_client_not_configured``
   with a plain-language, ordered Google Cloud setup-steps payload the
   UI can render verbatim.
2. When present, the loopback listener is bound, the authorization URL
   is built and opened in the system browser, and the endpoint returns
   **202** ``{"status": "connect_started", "flow_id": ..., "auth_url": ...}``
   immediately — the codebase's existing async pattern. ``GET
   /api/v1/channels/connect/{flow_id}`` reports the flow's status, and
   completion (or failure) is also emitted as a ``channel.updated``
   WebSocket event.
3. On callback, a background task exchanges the code, calls
   ``channels.list?mine=true`` with the fresh access token, and resolves
   the channel selection to EXACTLY one channel (the request's optional
   ``channel_id`` is verified against the list — a mismatch aborts the
   flow and stores nothing). Tokens are stored ONLY through the secret
   store via :class:`~app.providers.youtube.tokens.TokenManager`, and
   only AFTER that durable store succeeds is the channel row
   created/updated and success reported. The row lands in connect state
   ``needs_audit_check``: the YouTube API compliance-audit status is
   UNKNOWN at connect time and is never claimed as passed here.

OAuth client management
------------------------
``PUT /api/v1/channels/oauth-client`` is the ONE write path for the
client pair above: the shipped app's Settings/Channels UI collects the
Desktop-app ``client_id`` / ``client_secret`` there and they are stored
ONLY through the secret store at the refs named above. The endpoint is
write-only in the providers-router sense — no response from this
router ever contains either value, at most ``{"client_configured":
bool}``. ``GET`` on the same path reports that status (true only when
BOTH refs hold non-blank values); ``DELETE`` removes both refs so a
wrong pair can be cleared before re-entry. Blank values are a 422
``validation_error`` (the providers ``PUT /{id}/key`` convention).

Connect-state storage: the ``channels.status`` column is the lifecycle
status (its CHECK constraint allows active/paused/disconnected/at_risk),
so a freshly connected channel is ``paused`` there, while the connect /
credential state lives in ``config_json`` (``connect_status``,
``credential_status``, ``audit_status``, ``testing_mode_token``) and is
surfaced as derived top-level fields on every channel response.

Revalidate
----------
``POST /{channel_id}/revalidate`` forces a refresh-token grant check via
:class:`TokenManager`. On ``invalid_grant`` the channel's connect state
becomes ``needs_reauth`` (pause semantics elsewhere are unchanged); on
success the TokenManager has already persisted the refreshed blob
atomically in the secret store.

Quality tier (Round 5)
----------------------
``channels.quality_tier`` is the channel's production quality
setting (``draft`` / ``master_1080p`` / ``master_1440p_upscale``,
default ``draft`` — see :mod:`app.channel_quality` and migration
0008).  It is surfaced on every channel response (normalised to
``draft`` when the stored value is missing/unrecognised, e.g. a
pre-migration database) and is the one channel setting writable
through this router: ``PATCH /api/v1/channels/{channel_id}`` with
``{"quality_tier": ...}`` (422 ``validation_error`` for any other
value, 404 for an unknown channel).  Production resolves the tier
from the channel row at the spec seams, so flipping it changes the
next video produced for that channel — manually or via a schedule —
and nothing else.

Secret-store resolution (production keyring stays the default):
``app.state.youtube_secret_store`` override -> ``EnvSecretStore`` when
``YTA_SECRET_BACKEND=env`` (dev only) -> the OS-keyring adapter over
:mod:`app.secrets_store`. HTTP resolution mirrors it:
``app.state.youtube_http_client`` override -> a per-flow
``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import webbrowser
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.errors import ApiError
from app.providers.youtube.errors import NeedsReauth, OAuthError
from app.providers.youtube.oauth import (
    CONNECT_SCOPES,
    DEFAULT_CALLBACK_TIMEOUT,
    LoopbackReceiver,
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    generate_state,
)
from app.providers.youtube.tokens import TokenManager

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])

_SECRETY = ("token", "secret", "credential", "password", "keyring")

#: Secret-store refs for the Google OAuth *client* (Desktop app) pair.
CLIENT_ID_REF = "youtube/client_id"
CLIENT_SECRET_REF = "youtube/client_secret"

#: Google endpoint used for channel discovery/verification.
CHANNELS_LIST_URL = "https://www.googleapis.com/youtube/v3/channels"

#: Connect states surfaced to the UI (stored in channels.config_json).
CONNECT_NEEDS_AUDIT_CHECK = "needs_audit_check"
CONNECT_CONNECTED = "connected"
CONNECT_NEEDS_REAUTH = "needs_reauth"

#: Ordered, plain-language setup steps returned with the 409 so the UI
#: can render them verbatim when no OAuth client is configured yet.
SETUP_STEPS: list[dict[str, Any]] = [
    {
        "step": 1,
        "title": "Create a Google Cloud project",
        "detail": (
            "Go to console.cloud.google.com and create one project for this "
            "app (any name, for example 'YouTube Agent'). Use a single "
            "project — creating extra projects to multiply API quota "
            "violates YouTube's API terms."
        ),
    },
    {
        "step": 2,
        "title": "Enable the YouTube Data API v3",
        "detail": (
            "In that project open APIs & Services -> Library, search for "
            "'YouTube Data API v3' and click Enable. This API is used for "
            "uploads, thumbnails, captions and playlists."
        ),
    },
    {
        "step": 3,
        "title": "Enable the YouTube Analytics API",
        "detail": (
            "On the same Library page, search for 'YouTube Analytics API' "
            "and click Enable (the analytics features use it later)."
        ),
    },
    {
        "step": 4,
        "title": "Configure the OAuth consent screen",
        "detail": (
            "Open Google Auth Platform (Branding / Audience) and fill in the "
            "app name, support email and the other required fields. If you "
            "test before publishing, add your own Google account as a test "
            "user."
        ),
    },
    {
        "step": 5,
        "title": "Publish the consent screen to Production",
        "detail": (
            "In Google Auth Platform -> Audience, publish the app to "
            "Production. WARNING: while the consent screen stays in Testing "
            "mode, Google expires every refresh token after 7 days, so "
            "connected channels silently disconnect about a week after "
            "connecting."
        ),
    },
    {
        "step": 6,
        "title": "Create an OAuth client of type 'Desktop app'",
        "detail": (
            "Open APIs & Services -> Credentials -> Create credentials -> "
            "OAuth client ID, choose application type 'Desktop app' and "
            "create it. No redirect URI needs to be registered — the app "
            "uses a one-time loopback redirect on 127.0.0.1."
        ),
    },
    {
        "step": 7,
        "title": "Save the client_id and client_secret in the app",
        "detail": (
            "Copy that Desktop app client's client_id and client_secret "
            "into the app's credential store (secret refs youtube/client_id "
            "and youtube/client_secret), then try connecting again. The app "
            "never needs an API key for uploads — an API key cannot upload "
            "videos; OAuth with the channel owner's sign-in is the only way."
        ),
    },
]


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class ChannelUpdate(BaseModel):
    """Partial channel-settings edit (Round 5).

    ``quality_tier`` is currently the only writable channel setting
    here; every field is optional in the model so the endpoint can
    reject a body that sets nothing with a precise 422.
    """

    quality_tier: Optional[str] = None


class ConnectRequest(BaseModel):
    """Optional connect parameters.

    ``channel_id`` is the YouTube channel id (``UC...``) the user intends
    to connect; when given, the flow verifies the consented account owns
    exactly that channel and aborts (storing nothing) on a mismatch.
    """

    channel_id: Optional[str] = None
    login_hint: Optional[str] = None
    open_browser: bool = True


class OAuthClientBody(BaseModel):
    """The Google OAuth *client* pair (Desktop app), write-only.

    Both fields are optional in the model ONLY so a missing/blank value
    surfaces as the app's standard 422 ``validation_error`` envelope
    (providers-router convention) instead of a bare framework error;
    the endpoint itself requires both, non-blank after trimming.
    """

    client_id: Optional[str] = None
    client_secret: Optional[str] = None


# ---------------------------------------------------------------------------
# DB helpers (kept from the original router)
# ---------------------------------------------------------------------------


def _conn():
    try:
        from app import db as _db

        return _db.get_connection()
    except Exception:
        return None


def _tables(conn) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_config(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _public(row: dict) -> dict:
    """Strip anything that looks like credential material from a row,
    and surface the connect-state fields stored in ``config_json``."""
    out = {
        k: v
        for k, v in row.items()
        if not any(s in k.lower() for s in _SECRETY)
    }
    # Round 5: the channel's quality tier is always present in the
    # public shape — a missing column (pre-migration DB) or an
    # unrecognised stored value reads as the ``draft`` default.
    try:
        from app.channel_quality import quality_tier_of_row

        out["quality_tier"] = quality_tier_of_row(row)
    except Exception:
        out["quality_tier"] = "draft"
    config = _parse_config(row.get("config_json"))
    for key in (
        "connect_status",
        "credential_status",
        "audit_status",
        "testing_mode_token",
    ):
        if key in config:
            out[key] = config[key]
    return out


def _niche_info(conn, niche_profile_id: Any) -> dict[str, Any]:
    """Default-niche display info for one channel (Round 4).

    Resolves the channel's ``default_niche_profile_id`` to
    ``{default_niche_name, default_voice_id, default_voice_style}``
    from the ``niche_profiles`` row when one exists, falling back to
    the built-in profile library for the 8 shipped niches (built-ins
    are not DB rows).  Anything unresolved stays ``None`` — the UI
    shows an honest "not set" rather than an invented voice.
    """
    info: dict[str, Any] = {
        "default_niche_name": None,
        "default_voice_id": None,
        "default_voice_style": None,
    }
    if not niche_profile_id:
        return info
    niche_id = str(niche_profile_id)
    if "niche_profiles" in _tables(conn):
        cols = _cols(conn, "niche_profiles")
        try:
            row = conn.execute(
                "SELECT * FROM niche_profiles WHERE id = ? OR key = ?",
                (niche_id, niche_id),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            data = dict(row)
            info["default_niche_name"] = data.get("label") or data.get("key")
            if "default_voice_id" in cols:
                info["default_voice_id"] = data.get("default_voice_id")
            if "voice_style" in cols:
                info["default_voice_style"] = data.get("voice_style")
            return info
    try:
        from app.niches.builtins import BUILTIN_PROFILES

        profile = BUILTIN_PROFILES.get(niche_id)
        if profile is not None:
            info["default_niche_name"] = profile.name
            info["default_voice_style"] = profile.voice_style
    except Exception:
        pass
    return info


def _list_channels() -> list[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        if "channels" not in _tables(conn):
            return []
        cols = _cols(conn, "channels")
        where = "WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        rows = conn.execute(f"SELECT * FROM channels {where}").fetchall()
        items = []
        for raw in rows:
            row = dict(raw)
            item = _public(row)
            item.update(_niche_info(conn, row.get("default_niche_profile_id")))
            items.append(item)
        return items
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _find_channel(conn, channel_id: str) -> Optional[dict]:
    """Find a live channel row by internal id OR youtube_channel_id."""
    if "channels" not in _tables(conn):
        return None
    cols = _cols(conn, "channels")
    where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
    row = conn.execute(
        f"SELECT * FROM channels WHERE (id = ? OR youtube_channel_id = ?) "
        f"{where_deleted} LIMIT 1",
        (channel_id, channel_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _merge_channel_config(conn, row: dict, updates: dict) -> None:
    """Merge ``updates`` into the row's config_json + updated_at."""
    if "channels" not in _tables(conn):
        return
    cols = _cols(conn, "channels")
    config = _parse_config(row.get("config_json"))
    config.update(updates)
    sets, params = [], []
    if "config_json" in cols:
        sets.append("config_json = ?")
        params.append(json.dumps(config))
    if "updated_at" in cols:
        sets.append("updated_at = ?")
        params.append(_now_ms())
    if sets:
        params.append(row["id"])
        conn.execute(
            f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", params
        )


def _upsert_credential_row(conn, *, youtube_channel_id: str, scopes: list[str]) -> Optional[str]:
    """Create/update the provider_credentials metadata row for a channel.

    Stores ONLY a pointer: ``keyring_ref`` is the TokenManager blob ref
    (``youtube:channel:<id>``) inside the secret store — never a token.
    Returns the credential row id (the channels.credential_id FK).
    """
    if "provider_credentials" not in _tables(conn):
        return None
    ref = f"youtube:channel:{youtube_channel_id}"
    now = _now_ms()
    existing = conn.execute(
        "SELECT id FROM provider_credentials "
        "WHERE provider = 'youtube' AND account_label = ?",
        (youtube_channel_id,),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE provider_credentials SET keyring_ref = ?, scopes = ?, "
            "status = 'active', last_validated_at = ?, updated_at = ? "
            "WHERE id = ?",
            (ref, " ".join(scopes), now, now, existing[0]),
        )
        return str(existing[0])
    cred_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO provider_credentials (id, provider, credential_kind, "
        "keyring_ref, account_label, scopes, status, last_validated_at, "
        "created_at, updated_at) VALUES (?, 'youtube', 'oauth', ?, ?, ?, "
        "'active', ?, ?, ?)",
        (cred_id, ref, youtube_channel_id, " ".join(scopes), now, now, now),
    )
    return cred_id


def _upsert_channel_row(
    conn,
    *,
    youtube_channel_id: str,
    title: str,
    handle: Optional[str],
    language: str,
    country: Optional[str],
    credential_id: Optional[str],
    credential_status: str,
    testing_mode_token: bool,
) -> dict:
    """Create or update the channels row for a freshly connected channel.

    New rows start with lifecycle ``status='paused'`` (the CHECK
    constraint's vocabulary) and connect state ``needs_audit_check`` in
    config_json — audit status is ``unknown`` until a separate audit
    check runs; nothing here claims the audit passed.
    """
    now = _now_ms()
    cols = _cols(conn, "channels")
    existing = conn.execute(
        "SELECT * FROM channels WHERE youtube_channel_id = ?",
        (youtube_channel_id,),
    ).fetchone()
    config_updates = {
        "connect_status": CONNECT_NEEDS_AUDIT_CHECK,
        "credential_status": credential_status,
        "audit_status": "unknown",
        "testing_mode_token": bool(testing_mode_token),
    }
    if existing is not None:
        row = dict(existing)
        config = _parse_config(row.get("config_json"))
        config.update(config_updates)
        sets, params = [], []
        for col, val in (
            ("title", title),
            ("handle", handle),
            ("credential_id", credential_id),
            ("config_json", json.dumps(config)),
            ("updated_at", now),
            ("deleted_at", None),
        ):
            if col in cols:
                sets.append(f"{col} = ?")
                params.append(val)
        params.append(row["id"])
        conn.execute(
            f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", params
        )
        row.update({"title": title, "handle": handle})
        row["config_json"] = json.dumps(config)
        return row
    row = {
        "id": str(uuid.uuid4()),
        "youtube_channel_id": youtube_channel_id,
        "title": title,
        "handle": handle,
        "credential_id": credential_id,
        "default_niche_profile_id": None,
        "language": language,
        "country": country,
        "status": "paused",
        "daily_upload_cap": None,
        "monthly_budget_micros": None,
        "quality_tier": "draft",
        "config_json": json.dumps(config_updates),
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    insert_cols = [c for c in row if c in cols]
    conn.execute(
        f"INSERT INTO channels ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join('?' for _ in insert_cols)})",
        [row[c] for c in insert_cols],
    )
    return row


# ---------------------------------------------------------------------------
# Secret store / HTTP resolution
# ---------------------------------------------------------------------------


class _KeyringSecretStore:
    """Production SecretStore: adapter over app.secrets_store (OS keyring).

    Mirrors the dev harness (scripts/youtube_oauth_dev.py). Backend
    failures surface as ApiError 503 credential_store_unavailable from
    app.secrets_store and propagate to the caller.
    """

    def get(self, ref: str) -> Optional[str]:
        from app import secrets_store

        return secrets_store.get_secret(ref)

    def put(self, ref: str, value: str) -> None:
        from app import secrets_store

        secrets_store.set_secret(ref, value)

    def delete(self, ref: str) -> None:
        from app import secrets_store

        secrets_store.delete_secret(ref)


def _secret_store(request: Request):
    """Resolve the sync SecretStore (production keyring by default)."""
    override = getattr(request.app.state, "youtube_secret_store", None)
    if override is not None:
        return override
    if os.environ.get("YTA_SECRET_BACKEND", "").strip().lower() == "env":
        from app.providers.secrets_env import EnvSecretStore

        return EnvSecretStore()
    return _KeyringSecretStore()


def _http_client(request: Request) -> tuple[httpx.AsyncClient, bool]:
    """Resolve the HTTP client; returns (client, owned_by_caller)."""
    override = getattr(request.app.state, "youtube_http_client", None)
    if override is not None:
        return override, False
    return httpx.AsyncClient(timeout=30.0), True


def _client_credentials(store) -> tuple[str, str]:
    """Read the OAuth client pair, or raise the 409 setup error."""
    client_id = store.get(CLIENT_ID_REF)
    client_secret = store.get(CLIENT_SECRET_REF)
    missing = []
    if not client_id or not str(client_id).strip():
        missing.append(CLIENT_ID_REF)
    if not client_secret or not str(client_secret).strip():
        missing.append(CLIENT_SECRET_REF)
    if missing:
        raise ApiError(
            409,
            "youtube_client_not_configured",
            "YouTube sign-in is not set up yet: the app has no Google "
            "OAuth client credentials. Follow the setup steps once, then "
            "try connecting again.",
            details={
                "missing_refs": missing,
                "required_refs": [CLIENT_ID_REF, CLIENT_SECRET_REF],
                "setup_steps": SETUP_STEPS,
            },
        )
    return str(client_id).strip(), str(client_secret).strip()


def _client_configured(store) -> bool:
    """True only when BOTH client refs hold non-blank values."""
    client_id = store.get(CLIENT_ID_REF)
    client_secret = store.get(CLIENT_SECRET_REF)
    return bool(
        client_id
        and str(client_id).strip()
        and client_secret
        and str(client_secret).strip()
    )


def _emit_channel_updated(app: Any, payload: dict) -> None:
    """Best-effort ``channel.updated`` event via the shared WS hub."""
    try:
        from app.routers.ws import get_hub

        get_hub(app).publish("channel.updated", payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Connect flow
# ---------------------------------------------------------------------------

#: In-flight/completed connect flows, keyed by flow_id. Records NEVER
#: contain token material — status fields only.
_FLOWS: dict[str, dict[str, Any]] = {}
_FLOW_TASKS: dict[str, asyncio.Task] = {}


def _public_flow(record: dict) -> dict:
    return {k: v for k, v in record.items() if not k.startswith("_")}


async def _list_my_channels(http: httpx.AsyncClient, access_token: str) -> list[dict]:
    """``channels.list?mine=true`` for the just-consented account."""
    resp = await http.get(
        CHANNELS_LIST_URL,
        params={"part": "snippet,status", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code != 200:
        raise OAuthError(
            f"channels.list?mine=true failed with status {resp.status_code}"
        )
    items = resp.json().get("items") or []
    return [it for it in items if isinstance(it, dict) and it.get("id")]


def _select_channel(items: list[dict], expected: Optional[str]) -> dict:
    """Resolve the flow's channel selection to EXACTLY one channel."""
    if expected:
        for item in items:
            if item.get("id") == expected:
                return item
        raise OAuthError(
            "channel_mismatch: the consented account does not own/manage "
            f"the requested channel {expected!r}; nothing was stored"
        )
    if not items:
        raise OAuthError(
            "no_channel_found: channels.list?mine=true returned no channel "
            "for the consented account; nothing was stored"
        )
    if len(items) > 1:
        raise OAuthError(
            "channel_selection_required: the consented account controls "
            "more than one channel; reconnect naming the channel_id to "
            "connect; nothing was stored"
        )
    return items[0]


async def _complete_connect_flow(
    *,
    app: Any,
    record: dict,
    receiver: LoopbackReceiver,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    store: Any,
    http: httpx.AsyncClient,
    http_owned: bool,
    expected_channel_id: Optional[str],
) -> None:
    """Background half of the connect flow (after the 202 is returned).

    Order matters (roadmap section 27): exchange -> verify exact channel
    -> store tokens via TokenManager -> create/update channel row ->
    only then report success (record + ``channel.updated`` event).
    """
    flow_id = record["flow_id"]
    try:
        code = await receiver.wait(DEFAULT_CALLBACK_TIMEOUT)
        record["status"] = "exchanging"
        tokens = await exchange_code(
            http,
            code=code,
            code_verifier=code_verifier,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=receiver.redirect_uri,
        )
        if tokens.refresh_token is None:
            raise OAuthError(
                "Google returned no refresh token; nothing was stored. The "
                "connect flow always sends access_type=offline + "
                "prompt=consent — retry the connection."
            )
        items = await _list_my_channels(http, tokens.access_token)
        item = _select_channel(items, expected_channel_id)
        youtube_channel_id = str(item["id"])
        snippet = item.get("snippet") or {}

        manager = TokenManager(
            store, client_id=client_id, client_secret=client_secret, http=http
        )
        # Tokens are durably stored BEFORE any success is reported.
        cred_status = manager.store_token_response(youtube_channel_id, tokens)

        conn = _conn()
        row: Optional[dict] = None
        if conn is not None:
            try:
                if "channels" in _tables(conn):
                    cred_id = _upsert_credential_row(
                        conn,
                        youtube_channel_id=youtube_channel_id,
                        scopes=cred_status.scopes,
                    )
                    row = _upsert_channel_row(
                        conn,
                        youtube_channel_id=youtube_channel_id,
                        title=str(snippet.get("title") or f"YouTube channel {youtube_channel_id}"),
                        handle=snippet.get("customUrl"),
                        language=str(snippet.get("defaultLanguage") or "en"),
                        country=snippet.get("country"),
                        credential_id=cred_id,
                        credential_status=cred_status.status,
                        testing_mode_token=cred_status.testing_mode_token,
                    )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        record["status"] = CONNECT_NEEDS_AUDIT_CHECK
        record["channel_id"] = row["id"] if row else None
        record["youtube_channel_id"] = youtube_channel_id
        record["credential_status"] = cred_status.status
        record["testing_mode_token"] = cred_status.testing_mode_token
        _emit_channel_updated(
            app,
            {
                "channel_id": record["channel_id"],
                "youtube_channel_id": youtube_channel_id,
                "status": CONNECT_NEEDS_AUDIT_CHECK,
                "connect_status": CONNECT_NEEDS_AUDIT_CHECK,
                "credential_status": cred_status.status,
                "audit_status": "unknown",
                "testing_mode_token": cred_status.testing_mode_token,
                "flow_id": flow_id,
            },
        )
    except Exception as exc:  # OAuthError + transport failures land here.
        record["status"] = "connect_failed"
        record["error_code"] = (
            "oauth_flow_failed" if isinstance(exc, OAuthError) else "connect_failed"
        )
        # Exception text from the provider layer never contains token
        # material (tokens are values, not messages); keep it short.
        record["error_message"] = str(exc)[:300]
        _emit_channel_updated(
            app,
            {
                "status": "connect_failed",
                "connect_status": "connect_failed",
                "error_code": record["error_code"],
                "flow_id": flow_id,
            },
        )
    finally:
        try:
            await receiver.close()
        except Exception:
            pass
        if http_owned:
            try:
                await http.aclose()
            except Exception:
                pass
        _FLOW_TASKS.pop(flow_id, None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_channels() -> dict:
    items = _list_channels()
    return {"items": items, "next_cursor": None}


# NOTE: the oauth-client routes are registered BEFORE the
# ``/{channel_id}`` routes below so DELETE /oauth-client is not
# swallowed by DELETE /{channel_id}.


@router.put("/oauth-client")
async def put_oauth_client(request: Request, payload: OAuthClientBody) -> dict:
    """Store the Google OAuth client pair in the secret store.

    Write-only: the values go ONLY to the secret store (refs
    ``youtube/client_id`` / ``youtube/client_secret``) and the response
    carries just the configured status — the pair is never echoed, in
    whole or in part (not even a last-4: a client_secret has no safe
    display hint). Re-PUTting replaces the pair, which is how a stale
    or mistyped client is corrected. Like the providers key PUT this is
    a plain idempotent settings write — no Idempotency-Key is required
    (that convention covers work-creating POSTs).
    """
    client_id = (payload.client_id or "").strip()
    if not client_id:
        raise ApiError(
            422,
            "validation_error",
            "client_id must not be empty.",
            details={"field": "client_id"},
        )
    client_secret = (payload.client_secret or "").strip()
    if not client_secret:
        raise ApiError(
            422,
            "validation_error",
            "client_secret must not be empty.",
            details={"field": "client_secret"},
        )
    store = _secret_store(request)
    # Raises ApiError 503 credential_store_unavailable on backend failure.
    store.put(CLIENT_ID_REF, client_id)
    store.put(CLIENT_SECRET_REF, client_secret)
    return {"client_configured": True}


@router.get("/oauth-client")
async def get_oauth_client(request: Request) -> dict:
    """Configured status of the OAuth client pair (never the values)."""
    store = _secret_store(request)
    return {"client_configured": _client_configured(store)}


@router.delete("/oauth-client")
async def delete_oauth_client(request: Request) -> dict:
    """Remove BOTH OAuth client refs from the secret store.

    Deleting when nothing is stored is a graceful no-op (the secret
    store's delete already tolerates a missing ref), so the UI can
    offer "remove" unconditionally before re-entry.
    """
    store = _secret_store(request)
    store.delete(CLIENT_ID_REF)
    store.delete(CLIENT_SECRET_REF)
    return {"client_configured": False}


@router.patch("/{channel_id}")
async def update_channel(channel_id: str, payload: ChannelUpdate, request: Request) -> dict:
    """Update a channel's settings (Round 5: ``quality_tier``).

    The tier is validated against the exact render vocabulary
    (``draft`` / ``master_1080p`` / ``master_1440p_upscale``); any
    other value — or a body that sets nothing — is a 422
    ``validation_error`` naming the field, and an unknown channel is
    a 404 ``channel_not_found``.  The column is ensured lazily first
    (migration 0008's runtime twin), so the endpoint works on
    databases that have not run migrations.  Returns the channel in
    the same public shape as the list endpoint.
    """
    if payload.quality_tier is None:
        raise ApiError(
            422,
            "validation_error",
            "Nothing to update: provide quality_tier.",
            details={"field": "quality_tier"},
        )
    from app.channel_quality import QUALITY_TIERS, normalize_quality_tier

    try:
        tier = normalize_quality_tier(payload.quality_tier)
    except ValueError:
        raise ApiError(
            422,
            "validation_error",
            "quality_tier must be one of: " + ", ".join(QUALITY_TIERS) + ".",
            details={"field": "quality_tier"},
        )

    conn = _conn()
    if conn is None:
        raise ApiError(503, "database_unavailable", "Channel store unavailable.")
    try:
        row = _find_channel(conn, channel_id)
        if row is None:
            raise ApiError(404, "channel_not_found", "Unknown channel.")
        from app.channel_quality import ensure_quality_tier_column

        if not ensure_quality_tier_column(conn):
            raise ApiError(
                503, "database_unavailable", "Channel store unavailable."
            )
        cols = _cols(conn, "channels")
        sets, params = ["quality_tier = ?"], [tier]
        if "updated_at" in cols:
            sets.append("updated_at = ?")
            params.append(_now_ms())
        params.append(row["id"])
        conn.execute(
            f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", params
        )
        fresh = _find_channel(conn, row["id"]) or dict(row, quality_tier=tier)
        item = _public(fresh)
        item.update(_niche_info(conn, fresh.get("default_niche_profile_id")))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _emit_channel_updated(
        request.app,
        {
            "channel_id": item["id"],
            "youtube_channel_id": item.get("youtube_channel_id"),
            "quality_tier": tier,
        },
    )
    return item


@router.post("/connect", status_code=202)
async def connect_channel(
    request: Request, payload: Optional[ConnectRequest] = None
) -> dict:
    """Start the real PKCE loopback connect flow (see module docstring).

    No client credentials -> 409 ``youtube_client_not_configured`` with
    the ordered setup-steps payload. Otherwise 202 with the flow id and
    authorization URL; completion arrives via ``channel.updated``.
    """
    payload = payload or ConnectRequest()
    store = _secret_store(request)
    client_id, client_secret = _client_credentials(store)

    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()
    receiver = LoopbackReceiver(state)
    redirect_uri = await receiver.start()
    auth_url = build_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        scopes=CONNECT_SCOPES,
        login_hint=payload.login_hint,
    )
    if payload.open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass  # the URL is in the response either way

    flow_id = uuid.uuid4().hex
    http, http_owned = _http_client(request)
    record: dict[str, Any] = {
        "flow_id": flow_id,
        "status": "awaiting_callback",
        "auth_url": auth_url,
        "channel_id": None,
        "youtube_channel_id": None,
    }
    _FLOWS[flow_id] = record
    task = asyncio.get_running_loop().create_task(
        _complete_connect_flow(
            app=request.app,
            record=record,
            receiver=receiver,
            code_verifier=code_verifier,
            client_id=client_id,
            client_secret=client_secret,
            store=store,
            http=http,
            http_owned=http_owned,
            expected_channel_id=payload.channel_id,
        )
    )
    _FLOW_TASKS[flow_id] = task
    return {
        "status": "connect_started",
        "flow_id": flow_id,
        "auth_url": auth_url,
    }


@router.get("/connect/{flow_id}")
async def connect_flow_status(flow_id: str) -> dict:
    """Status of a connect flow started by POST /connect (no secrets)."""
    record = _FLOWS.get(flow_id)
    if record is None:
        raise ApiError(404, "connect_flow_not_found", "Unknown connect flow.")
    return _public_flow(record)


@router.delete("/{channel_id}")
async def disconnect_channel(channel_id: str, request: Request) -> dict:
    """Disconnect: revoke at Google FIRST, delete the local credential
    SECOND, then soft-mark the channel row disconnected (history kept).

    The revoke + local delete are :meth:`TokenManager.disconnect`'s
    own semantics: the revoke is attempted before the local secret
    deletion, the local deletion happens even if the revoke call
    fails, and a channel with no stored credential is a graceful
    no-op (no HTTP, no error). TokenManager is resolved exactly as
    the revalidate endpoint resolves it (same secret-store / HTTP
    overrides); the OAuth *client* credentials are read when present
    but are not required here — Google's revoke endpoint needs only
    the token being revoked, and a missing client pair must never
    block a user from disconnecting a channel.
    """
    # Resolve the YouTube channel id (the TokenManager blob ref is
    # keyed by it) from the row addressed by internal id OR youtube id.
    youtube_channel_id = channel_id
    conn = _conn()
    if conn is not None:
        try:
            row = _find_channel(conn, channel_id)
            if row is not None and row.get("youtube_channel_id"):
                youtube_channel_id = str(row["youtube_channel_id"])
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Revoke-at-Google FIRST, local secret deletion SECOND (both
    # inside TokenManager.disconnect), BEFORE the row is soft-marked.
    store = _secret_store(request)
    client_id = store.get(CLIENT_ID_REF)
    client_secret = store.get(CLIENT_SECRET_REF)
    http, http_owned = _http_client(request)
    manager = TokenManager(
        store,
        client_id=str(client_id or ""),
        client_secret=str(client_secret or ""),
        http=http,
    )
    try:
        await manager.disconnect(youtube_channel_id)
    finally:
        if http_owned:
            try:
                await http.aclose()
            except Exception:
                pass

    conn = _conn()
    if conn is not None:
        try:
            if "channels" in _tables(conn):
                cols = _cols(conn, "channels")
                sets, params = [], []
                if "status" in cols:
                    sets.append("status = 'disconnected'")
                if "deleted_at" in cols:
                    sets.append("deleted_at = ?")
                    params.append(int(time.time() * 1000))
                if "updated_at" in cols:
                    sets.append("updated_at = ?")
                    params.append(int(time.time() * 1000))
                if sets:
                    params.append(channel_id)
                    conn.execute(
                        f"UPDATE channels SET {', '.join(sets)} WHERE id = ?",
                        params,
                    )
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {"id": channel_id, "status": "disconnected"}


@router.post("/{channel_id}/revalidate")
async def revalidate_channel(channel_id: str, request: Request) -> dict:
    """Force a refresh-token grant check for one channel via TokenManager.

    ``invalid_grant`` -> the channel's connect state becomes
    ``needs_reauth`` (its jobs' pause semantics are unchanged — that is
    the scheduler's existing behaviour). Success -> the TokenManager has
    already persisted the refreshed blob atomically in the secret store.
    Only a status object is returned — never token material.
    """
    conn = _conn()
    if conn is None:
        raise ApiError(503, "database_unavailable", "Channel store unavailable.")
    try:
        row = _find_channel(conn, channel_id)
        if row is None:
            raise ApiError(404, "channel_not_found", "Unknown channel.")
        youtube_channel_id = str(row["youtube_channel_id"])

        store = _secret_store(request)
        client_id, client_secret = _client_credentials(store)
        http, http_owned = _http_client(request)
        manager = TokenManager(
            store, client_id=client_id, client_secret=client_secret, http=http
        )
        try:
            await manager.refresh(youtube_channel_id)
        except NeedsReauth:
            _merge_channel_config(
                conn,
                row,
                {
                    "connect_status": CONNECT_NEEDS_REAUTH,
                    "credential_status": CONNECT_NEEDS_REAUTH,
                },
            )
            _emit_channel_updated(
                request.app,
                {
                    "channel_id": row["id"],
                    "youtube_channel_id": youtube_channel_id,
                    "status": CONNECT_NEEDS_REAUTH,
                    "connect_status": CONNECT_NEEDS_REAUTH,
                    "credential_status": CONNECT_NEEDS_REAUTH,
                },
            )
            return {
                "id": row["id"],
                "youtube_channel_id": youtube_channel_id,
                "status": CONNECT_NEEDS_REAUTH,
                "connect_status": CONNECT_NEEDS_REAUTH,
                "credential_status": CONNECT_NEEDS_REAUTH,
            }
        except OAuthError as exc:
            raise ApiError(
                502,
                "youtube_revalidate_failed",
                "YouTube token revalidation failed.",
                details={"detail": str(exc)[:300]},
                retryable=True,
            )
        finally:
            if http_owned:
                try:
                    await http.aclose()
                except Exception:
                    pass

        cred = manager.get_status(youtube_channel_id)
        updates: dict[str, Any] = {
            "credential_status": cred.status,
            "testing_mode_token": cred.testing_mode_token,
        }
        config = _parse_config(row.get("config_json"))
        if config.get("connect_status") not in (
            CONNECT_NEEDS_AUDIT_CHECK,
            CONNECT_NEEDS_REAUTH,
        ):
            updates["connect_status"] = CONNECT_CONNECTED
        _merge_channel_config(conn, row, updates)
        if "provider_credentials" in _tables(conn):
            try:
                conn.execute(
                    "UPDATE provider_credentials SET last_validated_at = ?, "
                    "updated_at = ? WHERE provider = 'youtube' AND "
                    "account_label = ?",
                    (_now_ms(), _now_ms(), youtube_channel_id),
                )
            except Exception:
                pass
        result_status = updates.get(
            "connect_status", config.get("connect_status", CONNECT_CONNECTED)
        )
        return {
            "id": row["id"],
            "youtube_channel_id": youtube_channel_id,
            "status": result_status,
            "connect_status": result_status,
            "credential_status": cred.status,
            "testing_mode_token": cred.testing_mode_token,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
