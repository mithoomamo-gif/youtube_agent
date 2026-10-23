"""Provider endpoints (roadmap sections 14 + 31).

Key material is WRITE-ONLY: ``PUT /providers/{id}/key`` stores the value
in the OS credential store via :mod:`app.secrets_store` and records ONLY
metadata (keyring reference + last-4 hint) in the ``provider_credentials``
table.  No response from this router ever contains the key value — at
most ``configured: true`` plus ``key_last4``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app import secrets_store
from app.errors import ApiError
from app.providers.secrets_env import create_secret_provider

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])

# Test hook: when set (unit tests only), provider test-connection calls
# run over this httpx transport instead of the real network. Production
# leaves it None. Typed as Any to keep httpx out of the router surface.
_TEST_TRANSPORT: Any = None

# Last test-connection result per provider (process-local read model).
# ``list_providers`` surfaces it so downstream code can distinguish a
# merely-configured key from a VERIFIED connection: ``connected`` is
# true only when the last recorded test status is "ok" (section 28).
_LAST_TEST: dict[str, dict[str, Any]] = {}

PROVIDERS: list[str] = [
    "gemini",
    "openai",
    "anthropic",
    "elevenlabs",
    "pexels",
    "pixabay",
    "azure_tts",
    "google_tts",
]

# Last-4 cache: migration 0002 adds a real provider_credentials.last4
# column, so the hint is persisted on PUT (see _persist_last4) and read
# back from the DB whenever the column exists; this process-local dict
# is only a fallback for databases that predate the migration.
_LAST4: dict[str, str] = {}


class KeyBody(BaseModel):
    value: str


def _keyring_ref(provider_id: str) -> str:
    return f"apikey/{provider_id}"


def _require_provider(provider_id: str) -> None:
    if provider_id not in PROVIDERS:
        raise ApiError(
            404,
            "provider_not_found",
            f"Unknown provider '{provider_id}'.",
            details={"known_providers": PROVIDERS},
        )


def _configured(provider_id: str) -> bool:
    try:
        return secrets_store.has_secret(_keyring_ref(provider_id))
    except ApiError:
        return False


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


def _db_credential(provider_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return None
    try:
        if "provider_credentials" not in _tables(conn):
            return None
        row = conn.execute(
            "SELECT * FROM provider_credentials "
            "WHERE provider = ? AND credential_kind = 'api_key' "
            "ORDER BY updated_at DESC LIMIT 1",
            (provider_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _last4(provider_id: str) -> Optional[str]:
    row = _db_credential(provider_id)
    if row is not None and row.get("last4"):
        return str(row["last4"])
    return _LAST4.get(provider_id)


def _persist_last4(provider_id: str, last4: str) -> None:
    """Persist the last-4 hint into provider_credentials.last4.

    No-ops gracefully when the table or the column is absent (a
    database that predates migration 0002): the in-process ``_LAST4``
    cache remains the fallback in that case.  Metadata only — the key
    value itself is never written to the DB.
    """
    conn = _conn()
    if conn is None:
        return
    try:
        if "provider_credentials" not in _tables(conn):
            return
        if "last4" not in _cols(conn, "provider_credentials"):
            return
        conn.execute(
            "UPDATE provider_credentials SET last4 = ? "
            "WHERE provider = ? AND credential_kind = 'api_key'",
            (last4, provider_id),
        )
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _upsert_metadata(provider_id: str, last4: str) -> None:
    """Record metadata ONLY (keyring ref + last-4) — never the value."""
    conn = _conn()
    if conn is None:
        return
    try:
        if "provider_credentials" not in _tables(conn):
            return
        cols = _cols(conn, "provider_credentials")
        now = int(time.time() * 1000)
        existing = _db_credential(provider_id)
        values: dict[str, Any] = {
            "id": (existing or {}).get("id") or str(uuid.uuid4()),
            "provider": provider_id,
            "credential_kind": "api_key",
            "keyring_ref": _keyring_ref(provider_id),
            "last4": last4,
            "status": "active",
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        use = {k: v for k, v in values.items() if k in cols}
        placeholders = ", ".join("?" for _ in use)
        conn.execute(
            f"INSERT OR REPLACE INTO provider_credentials "
            f"({', '.join(use)}) VALUES ({placeholders})",
            tuple(use.values()),
        )
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        # e.g. providers outside the schema CHECK list (azure_tts /
        # google_tts): the keyring entry is the source of truth; the
        # metadata row lands when the schema grows those providers.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clear_metadata(provider_id: str) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        if "provider_credentials" not in _tables(conn):
            return
        cols = _cols(conn, "provider_credentials")
        if "status" in cols:
            conn.execute(
                "UPDATE provider_credentials SET status = 'missing', "
                "updated_at = ? WHERE provider = ? AND credential_kind = 'api_key'",
                (int(time.time() * 1000), provider_id),
            )
        else:
            conn.execute(
                "DELETE FROM provider_credentials "
                "WHERE provider = ? AND credential_kind = 'api_key'",
                (provider_id,),
            )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("")
async def list_providers() -> dict:
    items = []
    for provider_id in PROVIDERS:
        configured = _configured(provider_id)
        last_test = _LAST_TEST.get(provider_id)
        items.append(
            {
                "id": provider_id,
                "configured": configured,
                "has_key": configured,
                "key_last4": _last4(provider_id) if configured else None,
                # Read model for the §28 test flow: a provider is only
                # "connected" once a real test has passed — ``configured``
                # alone never implies connected.
                "status": (
                    last_test["status"]
                    if last_test
                    else ("configured" if configured else "not_configured")
                ),
                "connected": bool(last_test and last_test["status"] == "ok"),
                "last_test": last_test,
            }
        )
    return {"items": items, "next_cursor": None}


@router.put("/{provider_id}/key")
async def put_provider_key(provider_id: str, body: KeyBody) -> dict:
    _require_provider(provider_id)
    value = body.value or ""
    if not value.strip():
        raise ApiError(
            422,
            "validation_error",
            "API key value must not be empty.",
            details={"field": "value"},
        )
    # Raises ApiError 503 credential_store_unavailable on backend failure.
    secrets_store.set_secret(_keyring_ref(provider_id), value)
    last4 = value[-4:] if len(value) >= 4 else value
    _LAST4[provider_id] = last4
    # A replaced key has not been tested yet — drop any stale result so
    # the new key is never presented as verified on the old key's test.
    _LAST_TEST.pop(provider_id, None)
    _upsert_metadata(provider_id, last4)
    _persist_last4(provider_id, last4)
    # NEVER return the value itself — metadata only.
    return {"provider": provider_id, "configured": True, "key_last4": last4}


@router.delete("/{provider_id}/key")
async def delete_provider_key(provider_id: str) -> dict:
    _require_provider(provider_id)
    secrets_store.delete_secret(_keyring_ref(provider_id))
    _LAST4.pop(provider_id, None)
    _LAST_TEST.pop(provider_id, None)
    _clear_metadata(provider_id)
    return {"provider": provider_id, "configured": False, "key_last4": None}


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str) -> dict:
    """REAL test-connection (roadmap section 28).

    Performs the provider's CHEAPEST authenticated call (never a
    quota-costing one) via :func:`app.providers.connection_test.
    run_provider_test` and records the outcome in the ``_LAST_TEST``
    read model surfaced by ``GET /providers``. Always HTTP 200 for the
    three states: ``not_configured`` (no key stored — not an error),
    ``ok`` (call succeeded — only now is the provider "connected"), and
    ``error`` with a §28 ``category`` plus the provider message in
    ``detail``. Key material never appears in the response.
    """
    _require_provider(provider_id)
    from app.providers.connection_test import run_provider_test

    result = await run_provider_test(
        provider_id,
        create_secret_provider(),
        transport=_TEST_TRANSPORT,
        timeout_s=10.0,
    )
    _LAST_TEST[provider_id] = {
        **result,
        "tested_at": int(time.time() * 1000),
    }
    return result
