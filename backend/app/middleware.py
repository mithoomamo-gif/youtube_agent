"""Launch-token / Host / Origin guard middleware for the local sidecar.

Implements the loopback defence-in-depth from MASTER_ROADMAP Part 4
and research 14_security.md / 31_backend_protocol.md:

* Every HTTP request gets ``request.state.request_id`` (uuid4 hex),
  echoed as ``X-Request-Id`` on the response.
* Only ``/health/live`` is token-exempt (shell polls it before the UI
  exists). Every other path — all ``/api/v1/*`` and ``/health/ready`` —
  requires the per-launch token, presented either in the
  ``X-Local-Token`` header or as ``Authorization: Bearer <token>``
  (Round-6 closure G2 / R6-018 — the Tauri shell's ``mini_http``
  client sends both). Both forms are compared in constant time; if
  both headers are present, either valid token authenticates (see
  the precedence note at the token check in ``dispatch``).
* Host allowlist (anti DNS-rebinding): only loopback hosts.
* Origin allowlist (anti browser-CSRF): state-changing methods, and
  any request carrying an Origin header, must come from the Tauri
  origin; requests with no Origin (native/curl) are allowed unless
  ``Sec-Fetch-Site: cross-site`` is present.
* Bad-token rate limiting: >10 failed token checks inside 60 s turns
  token failures into blanket 429s for 60 s.
* CORS is answered ONLY for the allowlisted Tauri webview origins
  (never a wildcard): an allowlisted preflight ``OPTIONS`` is answered
  before the token check (browsers never send the token on a
  preflight), and every response to an allowlisted ``Origin`` carries
  ``Access-Control-Allow-Origin``. Without this the WebView2 webview
  blocks every UI->backend response and the app reports "Backend
  offline" on a healthy backend (installer test on a real Windows PC,
  2026-10-25 — patch v14).

WebSocket scope is passed through untouched — BaseHTTPMiddleware only
intercepts ``http`` scope; WS auth happens via the one-time ticket at
handshake, not this header check.
"""

from __future__ import annotations

import hmac
import time
import uuid
from collections import deque
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.errors import error_response

EXEMPT_PATHS = frozenset({"/health/live"})
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
ALLOWED_ORIGINS = frozenset({"https://tauri.localhost", "tauri://localhost"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

TOKEN_HEADER = "x-local-token"
AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "bearer "

MAX_TOKEN_FAILURES = 10
FAILURE_WINDOW_SECONDS = 60.0
BLOCK_SECONDS = 60.0


def _resolve_settings(request: Request):
    """app.state.settings override wins; else app.config.get_settings()."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings
    try:
        from app.config import get_settings

        return get_settings()
    except Exception:
        return None


def _expected_token(request: Request) -> str:
    settings = _resolve_settings(request)
    token = getattr(settings, "token", None) if settings is not None else None
    return token or ""


def _bearer_token(request: Request) -> Optional[str]:
    """Token from ``Authorization: Bearer <token>``, or None.

    None when the header is absent, uses a different scheme, or
    carries no token — a non-Bearer Authorization header is not a
    launch-token attempt and must not turn a ``missing_token``
    rejection into ``invalid_token`` (or vice versa).
    """
    raw = request.headers.get(AUTHORIZATION_HEADER)
    if raw is None:
        return None
    value = raw.strip()
    if not value.lower().startswith(BEARER_PREFIX):
        return None
    token = value[len(BEARER_PREFIX) :].strip()
    return token or None


def _provided_tokens(request: Request) -> list[str]:
    """Every launch-token candidate the request presented.

    Precedence (G2 / R6-018): there is no ranking between the two
    headers — if EITHER candidate matches the expected token in
    constant time the request is authenticated. The shell sends
    both headers with the same token; accepting either also keeps
    Bearer-only and X-Local-Token-only clients working identically.
    """
    candidates: list[str] = []
    legacy = request.headers.get(TOKEN_HEADER)
    if legacy is not None:
        candidates.append(legacy)
    bearer = _bearer_token(request)
    if bearer is not None:
        candidates.append(bearer)
    return candidates


def _host_allowed(host_header: Optional[str]) -> bool:
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):
        # IPv6 literal: [::1] or [::1]:<port>
        end = host.find("]")
        if end == -1:
            return False
        hostname = host[: end + 1]
    else:
        hostname = host.split(":", 1)[0]
    return hostname.lower() in ALLOWED_HOSTS


class LaunchTokenMiddleware(BaseHTTPMiddleware):
    """Guard all HTTP traffic to the sidecar (see module docstring)."""

    def __init__(self, app, settings=None) -> None:
        super().__init__(app)
        self._settings_override = settings
        self._failures: deque[float] = deque()
        self._blocked_until: float = 0.0

    # -- rate limiting -------------------------------------------------
    def _prune_failures(self, now: float) -> None:
        cutoff = now - FAILURE_WINDOW_SECONDS
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _record_failure(self, now: float) -> bool:
        """Record a failed token check; True if the block is now active."""
        self._prune_failures(now)
        self._failures.append(now)
        if len(self._failures) > MAX_TOKEN_FAILURES:
            self._blocked_until = max(self._blocked_until, now + BLOCK_SECONDS)
        return now < self._blocked_until

    def _is_blocked(self, now: float) -> bool:
        return now < self._blocked_until

    # -- main ----------------------------------------------------------
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id

        # The CORS origin is decided once, up front: only an Origin on
        # the allowlist earns CORS headers (and only after it survives
        # check 2 below); anything else gets none, exactly as before.
        origin = request.headers.get("origin")
        cors_origin = origin if origin in ALLOWED_ORIGINS else None

        def reject(status: int, code: str, message: str, retryable: bool = False):
            resp = error_response(
                status_code=status,
                code=code,
                message=message,
                request_id=request_id,
                retryable=retryable,
            )
            if cors_origin is not None:
                # Even a rejection must be READABLE by the webview, or
                # it surfaces as a network error instead of this body.
                resp.headers["Access-Control-Allow-Origin"] = cors_origin
                resp.headers["Vary"] = "Origin"
            return resp

        # 1) Host allowlist (applies to every HTTP request, incl. exempt).
        if not _host_allowed(request.headers.get("host")):
            return reject(403, "host_not_allowed", "Host is not allowed")

        # 2) Origin / Sec-Fetch-Site checks.
        if origin is not None:
            if origin not in ALLOWED_ORIGINS:
                return reject(403, "origin_not_allowed", "Origin is not allowed")
        elif request.method in STATE_CHANGING_METHODS:
            # No Origin on a state-changing request is fine for native
            # clients — unless the browser tells us it is cross-site.
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return reject(403, "origin_not_allowed", "Origin is not allowed")
        else:
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return reject(403, "origin_not_allowed", "Origin is not allowed")

        # 2b) CORS preflight for the allowlisted webview origins. A
        # preflight OPTIONS never carries the launch token (browsers
        # withhold credentials/headers on preflights by design), so it
        # must be answered here, before the token check — otherwise
        # WebView2 aborts every real request and the UI reports the
        # backend as offline. Allow-Private-Network covers Chromium's
        # loopback (Local Network Access) preflight from the secure
        # tauri.localhost origin to 127.0.0.1.
        if (
            cors_origin is not None
            and request.method == "OPTIONS"
            and request.headers.get("access-control-request-method") is not None
        ):
            resp = Response(status_code=204)
            resp.headers["Access-Control-Allow-Origin"] = cors_origin
            resp.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            requested_headers = request.headers.get(
                "access-control-request-headers"
            )
            resp.headers["Access-Control-Allow-Headers"] = (
                requested_headers
                if requested_headers
                else "authorization, content-type, x-local-token"
            )
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
            resp.headers["Access-Control-Max-Age"] = "600"
            resp.headers["Vary"] = "Origin, Access-Control-Request-Headers"
            resp.headers["X-Request-Id"] = request_id
            return resp

        # 3) Token check (all paths except the exact exempt set).
        #    Accepted presentations: ``X-Local-Token: <token>`` and
        #    ``Authorization: Bearer <token>``. Precedence: either
        #    valid token authenticates (see _provided_tokens); a
        #    request presenting a token in neither form is
        #    ``missing_token``, one presenting only non-matching
        #    token(s) is ``invalid_token``. One failure is recorded
        #    per rejected request, exactly as before Bearer support.
        if request.url.path not in EXEMPT_PATHS:
            expected = self._settings_override_token(request)
            candidates = _provided_tokens(request)
            now = time.monotonic()
            if not candidates:
                blocked = self._record_failure(now)
                if blocked:
                    return reject(
                        429,
                        "too_many_bad_tokens",
                        "Too many failed authentication attempts",
                        retryable=True,
                    )
                return reject(401, "missing_token", "Missing authentication token")
            if not expected or not any(
                hmac.compare_digest(candidate, expected)
                for candidate in candidates
            ):
                blocked = self._record_failure(now)
                if blocked or self._is_blocked(now):
                    return reject(
                        429,
                        "too_many_bad_tokens",
                        "Too many failed authentication attempts",
                        retryable=True,
                    )
                # No detail about the token itself, by design.
                return reject(401, "invalid_token", "Invalid authentication token")

        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request_id)
        if cors_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = cors_origin
            response.headers.setdefault("Vary", "Origin")
        return response

    def _settings_override_token(self, request: Request) -> str:
        if self._settings_override is not None:
            return getattr(self._settings_override, "token", None) or ""
        return _expected_token(request)
