"""Backend log query endpoint (roadmap section 31).

Rows come from the ``event_log`` table (the WebSocket event journal:
id, seq, boot_id, type, payload_json, created_at_ms).  Every piece of
text returned passes through ``app.redaction.redact_text`` — redaction
is a read-time backstop here on top of the write-time redaction the
logging pipeline performs (section 14).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


def _redact(text: str) -> str:
    try:
        from app.redaction import redact_text

        return redact_text(text)
    except Exception:
        # Fallback redactor (app.redaction unavailable): still never let
        # obvious key shapes through.
        out = text or ""
        out = re.sub(r"AIza[0-9A-Za-z\-_]{10,}", "***", out)
        out = re.sub(r"\bsk-[A-Za-z0-9\-_]{10,}", "***", out)
        out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]+", r"\1***", out)
        out = re.sub(r"(?i)((?:api_)?key=)[^&\s]+", r"\1***", out)
        return out


def _iso_from_ms(ms: Any) -> Optional[str]:
    try:
        return (
            datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None


@router.get("")
async def get_logs(
    level: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    items: list[dict[str, Any]] = []
    conn = None
    try:
        from app import db as _db

        conn = _db.get_connection()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "event_log" in tables:
            rows = conn.execute(
                "SELECT * FROM event_log ORDER BY created_at_ms DESC "
                "LIMIT ?",
                (max(limit * 5, limit),),
            ).fetchall()
            for row in rows:
                record = dict(row)
                text = str(record.get("payload_json") or "")
                entry = {
                    "id": record.get("id"),
                    "seq": record.get("seq"),
                    "boot_id": record.get("boot_id"),
                    "type": _redact(str(record.get("type") or "")),
                    "message": _redact(text),
                    "created_at": _iso_from_ms(record.get("created_at_ms")),
                }
                if level and level.lower() not in (
                    entry["type"] + " " + entry["message"]
                ).lower():
                    continue
                if q and q.lower() not in (
                    entry["type"] + " " + entry["message"]
                ).lower():
                    continue
                items.append(entry)
                if len(items) >= limit:
                    break
    except Exception:
        items = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return {"items": items, "next_cursor": None}
