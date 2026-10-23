"""Health endpoints.

* ``GET /health/live`` — liveness only: ``{"status": "ok"}`` and
  NOTHING else. The one token-exempt endpoint (enforced by
  LaunchTokenMiddleware's path rule) so the shell can poll before the
  UI exists.
* ``GET /health/ready`` — token-required. Reports DB reachability
  (and migration/schema state when app.db exposes it). Returns the
  error envelope with code ``not_ready`` and HTTP 503 when the DB is
  unavailable.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import build_error_envelope, get_request_id

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    return {"status": "ok"}


def _not_ready(request: Request) -> JSONResponse:
    rid = get_request_id(request)
    body = build_error_envelope(
        code="not_ready",
        message="Backend is not ready",
        details={"db": "down"},
        request_id=rid,
        retryable=True,
    )
    return JSONResponse(
        status_code=503, content=body, headers={"X-Request-Id": rid}
    )


@router.get("/health/ready")
async def ready(request: Request):
    # Imported lazily/defensively: app.db is another worker's module
    # and may not exist (or may not be initialised) yet.
    try:
        from app import db as db_module
    except Exception:
        return _not_ready(request)

    conn = None
    try:
        get_connection = getattr(db_module, "get_connection", None)
        if callable(get_connection):
            conn = get_connection()
        elif callable(getattr(db_module, "connect", None)):
            conn = db_module.connect()
        else:
            return _not_ready(request)

        if conn is None:
            return _not_ready(request)

        # Prove the connection is actually usable.
        if hasattr(conn, "execute"):
            conn.execute("SELECT 1").fetchone()

        # Migrations: ok when app.db.schema_version exists and runs
        # without error; when the function is absent we just report
        # db ok (migrations still reported "ok" per the contract).
        migrations = "ok"
        schema_version = getattr(db_module, "schema_version", None)
        if callable(schema_version):
            try:
                schema_version(conn)
            except TypeError:
                schema_version()
    except Exception:
        return _not_ready(request)
    finally:
        # Only close connections we opened ad-hoc via connect(); a
        # shared get_connection() handle is owned by app.db.
        if conn is not None and callable(
            getattr(db_module, "connect", None)
        ) and not callable(getattr(db_module, "get_connection", None)):
            try:
                conn.close()
            except Exception:
                pass

    return {"status": "ready", "db": "ok", "migrations": migrations}
