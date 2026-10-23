#!/usr/bin/env python3
"""YouTube OAuth DEV harness — run the REAL PKCE loopback flow by hand.

This is a developer tool for Round 2 integration work. It contains NO
OAuth logic of its own: every step reuses the existing provider code in
``app.providers.youtube`` exactly as the app will use it —

* ``oauth.generate_state`` / ``oauth.generate_pkce_pair`` — CSRF state
  (>=128-bit) and the PKCE S256 pair; the verifier never leaves memory.
* ``oauth.build_auth_url`` — authorization URL (always
  ``access_type=offline`` + ``prompt=consent`` so a refresh token is
  issued on every new channel connection).
* ``oauth.wait_for_code`` — the one-shot loopback listener on
  ``127.0.0.1`` (ephemeral port, state-validated, ~180 s timeout).
* ``oauth.exchange_code`` — code -> tokens at Google's token endpoint.
* ``tokens.TokenManager.store_token_response`` — the ONLY persistence
  path used here. It writes the per-channel credential blob
  (``youtube:channel:<channel_id>``) through the injected
  :class:`~app.providers.youtube.secrets.SecretStore`.

Secret-store wiring: by default the store is ``KeyringSecretStore``
(below), a thin adapter from the provider's ``SecretStore`` protocol
(``get``/``put``/``delete``) onto the app's production store,
:mod:`app.secrets_store` (OS keyring / Windows Credential Manager,
service ``"YouTubeAgent"``). ``--store memory`` swaps in the provider's
own ``InMemorySecretStore`` for a throwaway smoke test — tokens then
live only in this process and are GONE when it exits. Tokens are never
printed and never written to a plaintext file by this script, in
either mode.

Usage (from ``backend/``, with the project venv)::

    # No credentials yet? Prints the Google Cloud setup guide, exit 0:
    .venv/bin/python scripts/youtube_oauth_dev.py

    # Real flow, Google desktop-app client_secret.json:
    .venv/bin/python scripts/youtube_oauth_dev.py \\
        --client-secret-file ~/Downloads/client_secret.json

    # Real flow, explicit credentials (env preferred over shell args):
    YOUTUBE_OAUTH_CLIENT_ID=... YOUTUBE_OAUTH_CLIENT_SECRET=... \\
        .venv/bin/python scripts/youtube_oauth_dev.py

Exit codes: 0 = success (or setup guide shown), 1 = flow / credential
store failure, 2 = bad or incomplete client configuration. Expected
failures print one clear ``Error: ...`` line — never a stack trace
(pass ``--debug`` to get the traceback while developing).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

# --- import the backend package -------------------------------------------
# Allow running from anywhere: backend/ is this file's parent's parent.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if (_BACKEND_DIR / "app").is_dir() and str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

try:
    import httpx

    from app import secrets_store as _keyring_store
    from app.errors import ApiError
    from app.providers.youtube import (
        CONNECT_SCOPES,
        InMemoryQuotaLedger,
        InMemorySecretStore,
        TokenManager,
        YouTubeClient,
    )
    from app.providers.youtube.errors import OAuthError
    from app.providers.youtube.oauth import (
        DEFAULT_CALLBACK_TIMEOUT,
        build_auth_url,
        exchange_code,
        generate_pkce_pair,
        generate_state,
        wait_for_code,
    )
    from app.providers.youtube.secrets import SecretStore
except ImportError as exc:  # pragma: no cover - wrong interpreter / cwd
    print(
        f"Error: could not import the backend package ({exc}).\n"
        "Run this script with the backend venv, from the backend/ folder:\n"
        "    .venv/bin/python scripts/youtube_oauth_dev.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# Client configuration (client_id / client_secret resolution)
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """A user-fixable client-configuration problem (exit code 2)."""


@dataclass(frozen=True)
class OAuthClientConfig:
    """What the existing provider code needs: a plain id + secret pair.

    ``exchange_code`` and ``TokenManager`` both take ``client_id`` /
    ``client_secret`` strings; this dataclass is only the loader that
    produces them from a Google ``client_secret.json`` file or the
    environment. No credentials are ever hardcoded in this script.
    """

    client_id: str
    client_secret: str
    project_id: str | None = None
    source: str = ""


def load_client_secret_file(path: str | Path) -> OAuthClientConfig:
    """Parse a Google OAuth client JSON file into :class:`OAuthClientConfig`.

    Supports the standard desktop-app download shape::

        {"installed": {"client_id": ..., "client_secret": ...,
                       "project_id": ..., "redirect_uris": [...]}}

    plus the ``"web"`` shape and a flat ``{"client_id": ...,
    "client_secret": ...}`` object. Raises :class:`ConfigError` with a
    plain-language message for a missing file, invalid JSON, or
    missing/empty fields — never a traceback.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"client secret file not found: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read client secret file {p}: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"client secret file {p} is not valid JSON ({exc.msg} at "
            f"line {exc.lineno}, column {exc.colno}). Download a fresh "
            "copy from Google Cloud Console -> APIs & Services -> "
            "Credentials -> your Desktop app client -> Download JSON."
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(
            f"client secret file {p} must contain a JSON object, "
            f"got {type(data).__name__}."
        )
    block: dict | None = None
    for key in ("installed", "web"):
        if isinstance(data.get(key), dict):
            block = data[key]
            break
    if block is None:
        block = data  # flat {"client_id": ..., "client_secret": ...}
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    missing = [
        name
        for name, value in (("client_id", client_id), ("client_secret", client_secret))
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ConfigError(
            f"client secret file {p} is missing a non-empty "
            f"{' and '.join(missing)}. Expected the standard Google "
            'desktop-app format: {"installed": {"client_id": ..., '
            '"client_secret": ..., ...}}. Make sure you downloaded the '
            "JSON for an OAuth client of type 'Desktop app'."
        )
    project_id = block.get("project_id")
    return OAuthClientConfig(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        project_id=project_id if isinstance(project_id, str) else None,
        source=f"file {p}",
    )


def resolve_config(args: argparse.Namespace) -> OAuthClientConfig | None:
    """Resolve credentials from args/env, or ``None`` when none were given.

    Precedence: ``--client-secret-file`` (or env
    ``YOUTUBE_OAUTH_CLIENT_SECRET_FILE`` / ``YOUTUBE_CLIENT_SECRET_FILE``)
    wins; otherwise the explicit pair ``--client-id`` +
    ``--client-secret`` (or env ``YOUTUBE_OAUTH_CLIENT_ID`` +
    ``YOUTUBE_OAUTH_CLIENT_SECRET``). Supplying only half of the pair is
    a :class:`ConfigError`, not a silent fallback.
    """
    file_arg = (
        args.client_secret_file
        or os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET_FILE")
        or os.environ.get("YOUTUBE_CLIENT_SECRET_FILE")
    )
    if file_arg:
        return load_client_secret_file(file_arg)

    client_id = args.client_id or os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if client_id or client_secret:
        if not (client_id and client_id.strip() and client_secret and client_secret.strip()):
            raise ConfigError(
                "client_id and client_secret must be supplied together "
                "(--client-id + --client-secret, or env "
                "YOUTUBE_OAUTH_CLIENT_ID + YOUTUBE_OAUTH_CLIENT_SECRET), "
                "or use --client-secret-file instead."
            )
        return OAuthClientConfig(
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            source="command line / environment",
        )
    return None


# ---------------------------------------------------------------------------
# Secret store: provider SecretStore protocol -> production keyring store
# ---------------------------------------------------------------------------


class KeyringSecretStore:
    """Adapt :mod:`app.secrets_store` to the provider ``SecretStore``.

    ``TokenManager`` expects the sync ``get``/``put``/``delete``
    protocol from ``app.providers.youtube.secrets``; the production
    implementation behind it is the OS credential store (Windows
    Credential Manager / DPAPI on the target platform, service
    ``"YouTubeAgent"``), reached through ``app.secrets_store``'s
    ``get_secret``/``set_secret``/``delete_secret``. Backend failures
    surface there as ``ApiError`` 503 ``credential_store_unavailable``
    and are allowed to propagate — ``main`` renders them cleanly.
    """

    def get(self, ref: str) -> str | None:
        return _keyring_store.get_secret(ref)

    def put(self, ref: str, value: str) -> None:
        _keyring_store.set_secret(ref, value)

    def delete(self, ref: str) -> None:
        _keyring_store.delete_secret(ref)


# Structural check: both stores must satisfy the provider protocol.
assert isinstance(KeyringSecretStore(), SecretStore)
assert isinstance(InMemorySecretStore(), SecretStore)


def _check_keyring_available() -> None:
    """Fail fast (before any browser flow) if the keyring is unusable.

    Probing with ``has_secret`` on a harness-only ref never reads or
    writes a real credential; on a headless box with no Secret Service
    it raises the same 503 the real ``put`` would — only earlier,
    before the user has spent a consent grant.
    """
    _keyring_store.has_secret("youtube:dev-harness:probe")


# ---------------------------------------------------------------------------
# Setup instructions (roadmap Part 7, sections 7.2 / 7.4 / 7.7)
# ---------------------------------------------------------------------------

SETUP_INSTRUCTIONS = """\
====================================================================
 YouTube OAuth — Google Cloud setup (one-time)  [roadmap Part 7]
====================================================================
No OAuth client credentials were supplied, so there is nothing to
connect yet. Create the Google Cloud project + Desktop OAuth client
once, then re-run this script with the downloaded JSON.

STEP 1 — Create ONE Google Cloud project
  * Go to https://console.cloud.google.com/ and create a project
    (any name, e.g. "YouTube Agent").
  * Use exactly ONE project for this app. Creating extra projects to
    multiply API quota violates the YouTube API Services Terms and
    risks suspension of all of them. There is no paid quota tier.

STEP 2 — Enable the APIs (APIs & Services -> Library)
  * Enable "YouTube Data API v3" (required — uploads, metadata,
    captions, playlists).
  * Also enable "YouTube Analytics API" in the SAME project if the
    analytics agents will be used.

STEP 3 — Configure the OAuth consent screen
  (Google Auth Platform -> Branding / Audience)
  * App name, support email, homepage URL, privacy policy URL and
    terms URL are all required to publish — a contact email alone
    is NOT sufficient.
  * While the app is in "Testing": sign-in is limited to registered
    test users (max 100) — add your own Google account as a test
    user if you test before publishing.
  * CRITICAL — publish the consent screen to "Production" before
    relying on unattended runs: in Testing mode Google expires
    EVERY refresh token after 7 days, so all channels would
    silently disconnect a week after connecting. Production-but-
    unverified still shows an "unverified app" warning (YouTube
    scopes are sensitive scopes) until OAuth verification passes —
    that warning is expected and is a separate gate from Step 6.

STEP 4 — Create the OAuth client (APIs & Services -> Credentials)
  * Create credentials -> OAuth client ID -> type: "Desktop app".
  * Desktop clients use the loopback flow: NO redirect URI needs to
    be registered — http://127.0.0.1:{ephemeral port} is implicit.
  * Download the JSON ("client_secret.json"). It has the shape
    {"installed": {"client_id": ..., "client_secret": ...}}.
  * The desktop client_secret is a non-confidential identifier (it
    ships inside every installed copy); the real protection is
    PKCE + per-user refresh tokens. Still: do not commit the file
    to git and do not paste it into issues/chat.

STEP 5 — Channel prerequisites (one-time, in YouTube Studio)
  * The Google account you consent with must own/manage the target
    channel (the app verifies channels.list?mine=true and refuses
    a mismatch — a wrong-channel upload is a hard stop).
  * The account must be phone-verified: unverified channels cannot
    upload videos longer than 15 minutes.

STEP 6 — Compliance-audit caveat (read before expecting public
         uploads — roadmap 7.7)
  * For API projects created after 28 July 2020, EVERY upload via
    videos.insert is forced to Private by YouTube until the project
    passes the YouTube API Services compliance audit — whatever
    privacyStatus the app requests. It cannot be appealed; passing
    the audit is the only fix.
  * The audit is also a prerequisite for any quota extension.
    OAuth verification (Step 3) and the compliance audit are two
    DIFFERENT gates — do not conflate them.
  * Google publishes no timeline/SLA; plan 4-8 weeks end-to-end.
    Until it passes, the app works fully in private-only mode:
    videos upload as Private and are published manually from
    YouTube Studio.

QUOTA (June 2026 buckets, per project per day, reset midnight PT)
  * 100 videos.insert calls, 100 search.list calls, 10,000 shared
    units for everything else (thumbnails.set 50, captions.insert
    400, playlistItems.insert 50, reads 1 each).
  * The live "Quotas" page in Google Cloud Console is the only
    source of truth — Google's quota-calculator docs have lagged
    the bucket change before.

THEN — run the real flow (from backend/, with the project venv):

  .venv/bin/python scripts/youtube_oauth_dev.py \\
      --client-secret-file /path/to/client_secret.json

  # or via the environment:
  YOUTUBE_OAUTH_CLIENT_SECRET_FILE=/path/to/client_secret.json \\
      .venv/bin/python scripts/youtube_oauth_dev.py
  YOUTUBE_OAUTH_CLIENT_ID=... YOUTUBE_OAUTH_CLIENT_SECRET=... \\
      .venv/bin/python scripts/youtube_oauth_dev.py

Your system browser opens Google's consent page (never an embedded
WebView); tokens are stored ONLY in the OS credential store via the
app's secret-store interface and are never printed or written to a
plaintext file. Useful flags: --channel-id UC... (skip discovery),
--no-browser (print the URL instead of opening it), --store memory
(throwaway in-process store for a smoke test on a machine without a
keyring — tokens are lost when the script exits).
====================================================================
"""


# ---------------------------------------------------------------------------
# The flow — existing provider code only
# ---------------------------------------------------------------------------


async def _discover_channel_id(access_token: str) -> str:
    """Learn the channel id for a just-minted access token.

    ``TokenManager`` / ``YouTubeClient.verify_channel`` both key off a
    channel id, so the harness needs one before it can persist via
    ``store_token_response``. This single ``channels.list?mine=true``
    read uses the fresh access token directly; everything after it
    (storage, verification) goes through the provider classes.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet,status", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise OAuthError(
            f"channel discovery failed with status {resp.status_code}; "
            "the token was NOT stored. Check the client is a Desktop "
            "app client and the consent granted the YouTube scopes."
        )
    items = resp.json().get("items") or []
    if not items or not items[0].get("id"):
        raise OAuthError(
            "channels.list?mine=true returned no channel for the "
            "consented account; the token was NOT stored. The Google "
            "account must own/manage a YouTube channel."
        )
    return str(items[0]["id"])


async def run_flow(
    config: OAuthClientConfig,
    *,
    store: SecretStore,
    store_label: str,
    channel_id_arg: str | None,
    login_hint: str | None,
    timeout: float,
    open_browser: bool,
) -> int:
    """Run generate-state/PKCE -> loopback -> exchange -> persist."""
    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()

    def _open_browser(redirect_uri: str) -> None:
        auth_url = build_auth_url(
            client_id=config.client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scopes=CONNECT_SCOPES,
            login_hint=login_hint,
        )
        print("Open this URL in your browser and approve access:")
        print(f"  {auth_url}")
        if open_browser:
            try:
                webbrowser.open(auth_url)
                print("(Opened in your default browser.)")
            except Exception:
                print("(Could not open a browser automatically — use the URL above.)")
        print(f"Waiting for the Google redirect (up to {timeout:.0f}s)...")

    code, redirect_uri = await wait_for_code(
        state, timeout=timeout, on_redirect_uri=_open_browser
    )
    tokens = await exchange_code(
        code=code,
        code_verifier=code_verifier,
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=redirect_uri,
    )
    if tokens.refresh_token is None:
        raise OAuthError(
            "Google returned no refresh token, so nothing was stored. "
            "The connect flow must send access_type=offline + "
            "prompt=consent (build_auth_url does); if you revoked and "
            "re-granted, retry once."
        )

    channel_id = channel_id_arg or await _discover_channel_id(tokens.access_token)

    manager = TokenManager(
        store, client_id=config.client_id, client_secret=config.client_secret
    )
    status = manager.store_token_response(channel_id, tokens)

    # Status only from here on — token values never leave TokenManager.
    print()
    print(f"Connected channel : {status.channel_id}")
    print(f"Credential status : {status.status}")
    print(f"Granted scopes    : {', '.join(status.scopes) or '(none reported)'}")
    print(f"Token store       : {store_label}")
    if status.testing_mode_token:
        print()
        print("WARNING: this refresh token expires in ~7 days — the OAuth")
        print("consent screen is still in Testing mode. Publish it to")
        print("Production (roadmap 7.4) or this channel will need weekly")
        print("re-connection.")

    # Best-effort verification through the quota-aware client (reuses
    # the access token TokenManager just cached; costs 1 shared unit).
    try:
        client = YouTubeClient(ledger=InMemoryQuotaLedger(), token_manager=manager)
        info = await client.verify_channel(channel_id)
        long_ok = "yes" if info.can_upload_long else "NO — account not phone-verified"
        print(f"Channel title     : {info.title or '(unknown)'}")
        print(f"Long uploads (>15m): {long_ok} (status: {info.long_uploads_status})")
    except Exception as exc:
        print(f"Note: post-connect verification skipped/failed: {exc}")
        print("The credential IS stored; the app will verify on first use.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="youtube_oauth_dev.py",
        description=(
            "DEV harness: run the app's real YouTube OAuth 2.0 + PKCE "
            "loopback flow and store tokens via the secret store. With "
            "no credentials, prints the Google Cloud setup guide."
        ),
    )
    parser.add_argument(
        "--client-secret-file",
        metavar="PATH",
        help="Google client_secret.json (Desktop app). Env: "
        "YOUTUBE_OAUTH_CLIENT_SECRET_FILE.",
    )
    parser.add_argument(
        "--client-id",
        help="OAuth client ID (needs --client-secret too). Env: "
        "YOUTUBE_OAUTH_CLIENT_ID. Prefer the file or env over shell "
        "history.",
    )
    parser.add_argument(
        "--client-secret",
        help="OAuth client secret (needs --client-id too). Env: "
        "YOUTUBE_OAUTH_CLIENT_SECRET.",
    )
    parser.add_argument(
        "--channel-id",
        metavar="UC...",
        help="Expected channel ID. Skips channel discovery and the "
        "stored credential is verified against exactly this channel.",
    )
    parser.add_argument(
        "--login-hint",
        metavar="EMAIL",
        help="Pre-select this Google account on the consent screen.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CALLBACK_TIMEOUT,
        help="Seconds to wait for the browser redirect (default: 180).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser.",
    )
    parser.add_argument(
        "--store",
        choices=("keyring", "memory"),
        default="keyring",
        help="Secret store for the tokens: 'keyring' (default) = the "
        "production OS credential store via app.secrets_store; "
        "'memory' = provider InMemorySecretStore, dev smoke test "
        "only — tokens are lost when the script exits.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show a full traceback on failure (default: one clean "
        "error line).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = resolve_config(args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if config is None:
        print(SETUP_INSTRUCTIONS, end="")
        return 0

    if args.store == "memory":
        store: SecretStore = InMemorySecretStore()
        store_label = "IN-MEMORY (dev only — tokens lost on exit)"
        print(
            "WARNING: --store memory keeps tokens only in this process; "
            "they will NOT persist after the script exits.",
            file=sys.stderr,
        )
    else:
        store = KeyringSecretStore()
        store_label = "OS credential store (app.secrets_store, service 'YouTubeAgent')"
        try:
            _check_keyring_available()
        except ApiError as exc:
            print(
                f"Error: {exc.message} Start a keyring/Secret Service "
                "on this machine, or re-run with --store memory for a "
                "throwaway smoke test (tokens will not persist). "
                f"(detail: {exc.details.get('detail', '')})",
                file=sys.stderr,
            )
            return 1

    print(f"OAuth client      : from {config.source} (id/secret not shown)")
    if config.project_id:
        print(f"Google project    : {config.project_id}")
    try:
        return asyncio.run(
            run_flow(
                config,
                store=store,
                store_label=store_label,
                channel_id_arg=args.channel_id,
                login_hint=args.login_hint,
                timeout=args.timeout,
                open_browser=not args.no_browser,
            )
        )
    except OAuthError as exc:
        print(f"Error: OAuth flow failed: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"Error: {exc.message} ({exc.code})", file=sys.stderr)
        return 1
    except (httpx.HTTPError, OSError) as exc:
        print(f"Error: network/OS failure during OAuth flow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception:
        # Last-resort guard: a dev harness must still fail cleanly.
        # --debug is handled by re-raising inside main's callers; here
        # we only see truly unexpected errors.
        import traceback

        if "--debug" in sys.argv:
            traceback.print_exc()
        else:
            print(
                "Error: unexpected failure. Re-run with --debug for details.",
                file=sys.stderr,
            )
        raise SystemExit(1) from None
