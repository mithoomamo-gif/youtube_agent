"""Publication endpoints (roadmap section 31).

Published/scheduled videos come from the ``publish_records`` table.
``refresh-status`` re-polls YouTube processing/privacy state — a stub
until the YouTube adapter lands (201 from upload ≠ live; the
audit-pending forced-private state surfaces through this endpoint).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/publications", tags=["publications"])


def _conn():
    try:
        from app import db as _db

        return _db.get_connection()
    except Exception:
        return None


@router.get("")
async def list_publications() -> dict:
    items: list[dict[str, Any]] = []
    conn = _conn()
    if conn is not None:
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "publish_records" in tables:
                rows = conn.execute(
                    "SELECT * FROM publish_records ORDER BY created_at DESC"
                ).fetchall()
                items = [dict(r) for r in rows]
        except Exception:
            items = []
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {"items": items, "next_cursor": None}


@router.post("/{publication_id}/refresh-status", status_code=202)
async def refresh_status(publication_id: str) -> dict:
    # STUB: real implementation re-polls the YouTube API for processing
    # + privacy status via the Upload agent's adapter.
    return {
        "id": publication_id,
        "status": "refresh_queued",
        "detail": "stub — YouTube status re-poll pending provider team",
    }
