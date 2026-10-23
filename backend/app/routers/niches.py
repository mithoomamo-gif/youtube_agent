"""Niche profile endpoints (roadmap section 31).

One list merges the 8 built-in profiles (from the v1.1 prototype) with
user profiles persisted in the ``niche_profiles`` table.  ``POST`` is
how the headline **Other** path persists: the user supplies a name + a
one-line idea; profile expansion (WPM, structure, voice, visual style)
is a later agent task, so a created profile starts ``provisional``.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.errors import ApiError

router = APIRouter(prefix="/api/v1/niches", tags=["niches"])

BUILTIN_NICHES: list[dict[str, Any]] = [
    {"id": "facts", "name": "Facts", "source": "builtin"},
    {"id": "islamic_stories", "name": "Islamic Stories", "source": "builtin"},
    {"id": "horror_stories", "name": "Horror Stories", "source": "builtin"},
    {"id": "history", "name": "History", "source": "builtin"},
    {"id": "motivation", "name": "Motivation", "source": "builtin"},
    {"id": "tech", "name": "Tech", "source": "builtin"},
    {"id": "crime_stories", "name": "Crime Stories", "source": "builtin"},
    {"id": "health", "name": "Health", "source": "builtin"},
]

# Profiles created in this process (also persisted to DB when available;
# the in-memory copy keeps GET /niches/{id} working within the session).
_CREATED: dict[str, dict[str, Any]] = {}


class NicheCreate(BaseModel):
    name: str
    idea: str
    language: Optional[str] = None
    tone: Optional[str] = None
    target_minutes: Optional[int] = None


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


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "custom"


def _row_to_profile(row: dict) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "key": row.get("key"),
        "name": row.get("label") or row.get("name") or row.get("key"),
        "source": row.get("source") or "other",
        "idea": row.get("user_idea"),
        "language": row.get("language_mix") or row.get("language"),
        "tone": row.get("tone"),
        "status": "active" if row.get("is_active", 1) else "inactive",
    }


def _db_profiles() -> list[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        if "niche_profiles" not in _tables(conn):
            return []
        rows = conn.execute("SELECT * FROM niche_profiles").fetchall()
        return [_row_to_profile(dict(r)) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("")
async def list_niches() -> dict:
    items: list[dict[str, Any]] = [dict(n) for n in BUILTIN_NICHES]
    seen = {n["id"] for n in items}
    for profile in _db_profiles():
        pid = profile.get("id")
        key = profile.get("key")
        if pid in seen or (key and key in seen):
            continue
        items.append(profile)
        if pid:
            seen.add(pid)
    for pid, profile in _CREATED.items():
        if pid not in seen:
            items.append(profile)
    return {"items": items, "next_cursor": None}


@router.post("", status_code=201)
async def create_niche(body: NicheCreate) -> dict:
    name = (body.name or "").strip()
    idea = (body.idea or "").strip()
    if not (2 <= len(name) <= 40):
        raise ApiError(
            422,
            "validation_error",
            "Niche name must be 2..40 characters.",
            details={"field": "name", "length": len(name)},
        )
    if not (10 <= len(idea) <= 500):
        raise ApiError(
            422,
            "validation_error",
            "Niche idea must be 10..500 characters.",
            details={"field": "idea", "length": len(idea)},
        )

    profile_id = str(uuid.uuid4())
    now = _now_ms()
    profile: dict[str, Any] = {
        "id": profile_id,
        "name": name,
        "source": "other",
        "status": "provisional",
        "idea": idea,
        "language": body.language,
        "tone": body.tone,
        "target_minutes": body.target_minutes,
    }

    conn = _conn()
    if conn is None:
        raise ApiError(
            503,
            "database_unavailable",
            "Cannot persist a niche profile: database is unavailable.",
            retryable=True,
        )
    try:
        if "niche_profiles" not in _tables(conn):
            raise ApiError(
                503,
                "database_unavailable",
                "Cannot persist a niche profile: niche_profiles table missing.",
                retryable=True,
            )
        cols = _cols(conn, "niche_profiles")
        key = _slug(name)
        existing_keys = {
            r[0] for r in conn.execute("SELECT key FROM niche_profiles")
        } if "key" in cols else set()
        builtin_keys = {n["id"] for n in BUILTIN_NICHES}
        base_key, suffix = key, 2
        while key in existing_keys or key in builtin_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        values: dict[str, Any] = {
            "id": profile_id,
            "key": key,
            "label": name,
            "source": "other",
            "user_idea": idea,
            "language_mix": body.language,
            "language": body.language,
            "tone": body.tone,
            "created_at": now,
            "updated_at": now,
        }
        use = {k: v for k, v in values.items() if k in cols and v is not None}
        placeholders = ", ".join("?" for _ in use)
        conn.execute(
            f"INSERT INTO niche_profiles ({', '.join(use)}) "
            f"VALUES ({placeholders})",
            tuple(use.values()),
        )
        profile["key"] = key
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _CREATED[profile_id] = profile
    return profile


@router.get("/{niche_id}")
async def get_niche(niche_id: str) -> dict:
    for niche in BUILTIN_NICHES:
        if niche["id"] == niche_id:
            return dict(niche)
    if niche_id in _CREATED:
        return dict(_CREATED[niche_id])
    for profile in _db_profiles():
        if profile.get("id") == niche_id or profile.get("key") == niche_id:
            return profile
    raise ApiError(
        404, "niche_not_found", f"No niche profile with id '{niche_id}'."
    )
