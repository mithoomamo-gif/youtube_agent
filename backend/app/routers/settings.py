"""App settings endpoints (roadmap section 31).

Settings are non-secret key/value pairs persisted in the ``settings``
table (``value_json`` holds the JSON-encoded value).  Secrets are NOT
settings: any key whose name contains ``key`` / ``token`` / ``secret``
is rejected with 422 ``secret_in_settings`` — provider keys belong in
the credential store via ``PUT /providers/{id}/key`` (section 14).

Budget policy (Round 3): ``GET/PUT /api/v1/settings/budget`` is the
typed surface over the same table.  Storage choice: the whole policy
is persisted as ONE JSON object under the single settings key
``budget_policy`` (see ``app.providers.budget.policy`` for rationale
and the roadmap-section-35 defaults), so updates are atomic.

Uninstall data policy (Round 6): ``GET/PUT /api/v1/settings/privacy``
is the typed surface for the single boolean flag
``privacy.remove_data_on_uninstall`` (default OFF — uninstalling
preserves user data).  Setting it also writes/removes the
``uninstall-purge.marker`` file in the data root, which is what the
NSIS uninstaller hook checks; see ``app.uninstall_policy``.  The
generic ``PUT /settings`` honours the same flag (and syncs the
marker) when the key is present in ``values``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.errors import ApiError
from app.uninstall_policy import (
    MARKER_FILENAME,
    SETTINGS_KEY as PRIVACY_SETTINGS_KEY,
    flag_enabled,
    marker_present,
    sync_marker,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_SECRET_MARKERS = ("key", "token", "secret")

# In-memory mirror so PUT/GET round-trip even before the DB exists.
_MEMORY: dict[str, Any] = {}


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class BudgetPolicyUpdate(BaseModel):
    per_video_cap_usd: float
    daily_cap_usd: float
    monthly_cap_usd: float
    warn_pct: int = 70
    soft_pct: int = 90


class PrivacyUpdate(BaseModel):
    remove_data_on_uninstall: bool


def _data_dir(request: Request | None):
    """Data root for the uninstall marker: the running app's settings
    when available (``app.state.settings.data_dir``), else resolved
    from the environment by ``app.uninstall_policy``."""
    if request is not None:
        settings = getattr(request.app.state, "settings", None)
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is not None:
            return data_dir
    return None


def _privacy_view(request: Request | None = None) -> dict:
    """Current uninstall-data-policy state for API responses."""
    enabled = flag_enabled(_load())
    return {
        "remove_data_on_uninstall": enabled,
        "settings_key": PRIVACY_SETTINGS_KEY,
        "marker_filename": MARKER_FILENAME,
        "marker_present": marker_present(_data_dir(request)),
        # The invariant the UI copy states: data survives uninstall
        # unless the user opted in AND the marker is in place.
        "data_preserved_by_default": True,
    }


def _apply_privacy_flag(enabled: bool, request: Request | None = None) -> dict:
    """Persist the flag and sync the marker to match it."""
    _upsert({PRIVACY_SETTINGS_KEY: enabled})
    sync_marker(enabled, _data_dir(request))
    return _privacy_view(request)


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


def _load() -> dict[str, Any]:
    result: dict[str, Any] = dict(_MEMORY)
    conn = _conn()
    if conn is None:
        return result
    try:
        if "settings" not in _tables(conn):
            return result
        for row in conn.execute("SELECT key, value_json FROM settings"):
            raw = row[1]
            try:
                result[row[0]] = json.loads(raw)
            except (TypeError, ValueError):
                result[row[0]] = raw
        return result
    except Exception:
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _upsert(values: dict[str, Any]) -> None:
    _MEMORY.update(values)
    conn = _conn()
    if conn is None:
        return
    try:
        if "settings" not in _tables(conn):
            return
        now = int(time.time() * 1000)
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), now),
            )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/budget")
async def get_budget_policy() -> dict:
    """Persisted budget policy, or roadmap defaults when unset.

    Shape: ``{per_video_cap_usd, daily_cap_usd, monthly_cap_usd,
    warn_pct, soft_pct}`` (USD floats, percentages as ints).
    """
    from app.providers.budget.policy import policy_from_settings

    return policy_from_settings(_load())


@router.put("/budget")
async def put_budget_policy(body: BudgetPolicyUpdate) -> dict:
    """Validate + persist the budget policy (single settings key).

    Validation: all caps must be positive finite numbers and the
    threshold ladder must satisfy ``0 < warn_pct < soft_pct < 100``.
    Violations are 422 ``budget_policy_invalid``.
    """
    from app.providers.budget.policy import SETTINGS_KEY, validate_budget_policy

    try:
        clean = validate_budget_policy(body.model_dump())
    except ValueError as exc:
        raise ApiError(
            422,
            "budget_policy_invalid",
            str(exc),
            details={"field": "budget_policy"},
        ) from exc
    _upsert({SETTINGS_KEY: clean})
    return clean


@router.get("/privacy")
async def get_privacy_settings(request: Request) -> dict:
    """Uninstall data policy state.

    ``remove_data_on_uninstall`` defaults to ``False`` on a fresh
    profile (no persisted key): uninstalling preserves user data.
    ``marker_present`` reports the actual marker file in the data
    root — the file the NSIS uninstaller hook will check.
    """
    return _privacy_view(request)


@router.put("/privacy")
async def put_privacy_settings(body: PrivacyUpdate, request: Request) -> dict:
    """Persist the opt-in flag and write/remove the purge marker.

    ON  -> ``uninstall-purge.marker`` is written into the data root.
    OFF -> the marker is removed.  Nothing is deleted by this call —
    the marker only takes effect if/when the app is uninstalled.
    """
    return _apply_privacy_flag(bool(body.remove_data_on_uninstall), request)


@router.get("")
async def get_settings() -> dict:
    return _load()


@router.put("")
async def put_settings(body: SettingsUpdate, request: Request) -> dict:
    for key in body.values:
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise ApiError(
                422,
                "secret_in_settings",
                f"Setting '{key}' looks like a secret; secrets are stored "
                "via the providers endpoints, never as settings.",
                details={"key": key},
            )
    if PRIVACY_SETTINGS_KEY in body.values and not isinstance(
        body.values[PRIVACY_SETTINGS_KEY], bool
    ):
        raise ApiError(
            422,
            "privacy_setting_invalid",
            f"Setting '{PRIVACY_SETTINGS_KEY}' must be a boolean.",
            details={"key": PRIVACY_SETTINGS_KEY},
        )
    _upsert(body.values)
    if PRIVACY_SETTINGS_KEY in body.values:
        # Keep the marker in lockstep with the flag, whichever surface
        # the client used to change it.
        sync_marker(bool(body.values[PRIVACY_SETTINGS_KEY]), _data_dir(request))
    return {"updated": sorted(body.values.keys()), "settings": _load()}
