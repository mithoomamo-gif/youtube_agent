"""System / meta endpoints (roadmap section 31).

* ``GET /api/v1/system/info`` — version + capability handshake; the FIRST
  call the UI makes after connecting.
* ``POST /api/v1/system/shutdown`` — begins graceful drain (202).  The
  actual drain (checkpoint agents, close sockets, exit) is the shell /
  supervisor's job; here we only record the request on ``app.state``.
* ``GET /api/v1/system/capabilities`` — best-effort hardware detection.
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1/system", tags=["system"])

BACKEND_VERSION = "0.1.0"
API_VERSION = 1
WS_SCHEMA = 1
MIN_UI_API = 1
CAPABILITIES = ["gates.v1", "cost_ticker.v1", "ws_commands.v1"]


def _app_version() -> str:
    try:
        from app import __version__

        return str(__version__)
    except Exception:
        return BACKEND_VERSION


def _boot_id(request: Request) -> str:
    """Boot id for this backend process.

    Sources, in order: ``app.state.boot_id`` (set by main at startup),
    ``app.config`` settings (``YTA_BOOT_ID`` env contract), the
    ``YTA_BOOT_ID`` env var, finally a per-process generated id cached
    on app.state so it is stable for the life of the process.
    """
    boot_id = getattr(request.app.state, "boot_id", None)
    if boot_id:
        return str(boot_id)
    try:
        from app.config import get_settings

        boot_id = getattr(get_settings(), "boot_id", None)
        if boot_id:
            request.app.state.boot_id = str(boot_id)
            return str(boot_id)
    except Exception:
        pass
    boot_id = os.environ.get("YTA_BOOT_ID")
    if boot_id:
        request.app.state.boot_id = boot_id
        return boot_id
    boot_id = uuid.uuid4().hex
    request.app.state.boot_id = boot_id
    return boot_id


@router.get("/info")
async def system_info(request: Request) -> dict:
    return {
        "app_version": _app_version(),
        "backend_version": BACKEND_VERSION,
        "api_version": API_VERSION,
        "ws_schema": WS_SCHEMA,
        "min_ui_api": MIN_UI_API,
        "boot_id": _boot_id(request),
        "capabilities": list(CAPABILITIES),
    }


@router.post("/shutdown", status_code=202)
async def system_shutdown(request: Request) -> JSONResponse:
    # Record the request; the shell/supervisor performs the real drain
    # (checkpoint running agents, broadcast system.draining, close WS
    # with 4402, WAL-checkpoint + close SQLite, exit 0 — section 31).
    request.app.state.shutdown_requested = True
    return JSONResponse(status_code=202, content={"status": "draining"})


@router.get("/capabilities")
async def system_capabilities() -> dict:
    ram_gb = None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        if page_size and phys_pages:
            ram_gb = round(page_size * phys_pages / (1024**3), 2)
    except (ValueError, OSError, AttributeError):
        ram_gb = None
    return {
        "cpu_cores": os.cpu_count(),
        "ram_gb": ram_gb,
        # GPU / encoder detection (NVENC/QSV/AMF, Whisper presence) is a
        # later diagnostics task; reported as unknown for the skeleton.
        "gpu": None,
    }
