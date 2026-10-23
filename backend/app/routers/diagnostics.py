"""Diagnostics bundle endpoint ("Copy diagnostics").

Preview-before-share contract
-----------------------------
``GET /api/v1/diagnostics`` returns *exactly* the bytes that would be
shared: the payload is produced by the single builder in
:mod:`app.diagnostics` and serialised by its single serialiser, and
this router returns those bytes verbatim as the response body. There
is no separate hidden "real" bundle — what the user previews IS what
gets copied/sent.

``GET /api/v1/diagnostics/preview`` is an alias over the very same
code path, kept so the UI can label one call "preview" and the other
"copy" while both are guaranteed identical bytes.

Like every other ``/api/v1/*`` route, both endpoints sit behind the
launch-token middleware (``X-Local-Token``); there is no exemption.

Bundle contents are documented in :mod:`app.diagnostics`: versions,
runtime/system info, a health snapshot, aggregate counts, safe config
state, and redacted log lines — never secrets, tokens, user content,
or identifying paths.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.diagnostics import (
    DEFAULT_LOG_LINES,
    MAX_LOG_LINES,
    build_diagnostics_bundle,
    serialize_bundle,
)

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


def _settings(request: Request):
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings
    try:
        from app.config import get_settings

        return get_settings()
    except Exception:
        return None


def _bundle_response(request: Request, log_lines: int) -> Response:
    bundle = build_diagnostics_bundle(
        settings=_settings(request), log_line_limit=log_lines
    )
    # The response body is the serialised bundle itself — verbatim, so
    # preview bytes == share bytes by construction.
    return Response(content=serialize_bundle(bundle), media_type="application/json")


@router.get("")
async def get_diagnostics(
    request: Request,
    log_lines: int = Query(default=DEFAULT_LOG_LINES, ge=0, le=MAX_LOG_LINES),
) -> Response:
    """Return the diagnostics bundle — the exact bytes that would be shared."""
    return _bundle_response(request, log_lines)


@router.get("/preview")
async def preview_diagnostics(
    request: Request,
    log_lines: int = Query(default=DEFAULT_LOG_LINES, ge=0, le=MAX_LOG_LINES),
) -> Response:
    """Preview alias: byte-identical to ``GET /api/v1/diagnostics``."""
    return _bundle_response(request, log_lines)


__all__ = ["router"]
