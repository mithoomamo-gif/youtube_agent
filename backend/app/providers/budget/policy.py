"""Persisted budget policy — the Settings-facing view of the caps.

Storage choice (documented, Round 3)
------------------------------------
The policy lives in the SQLite ``settings`` table under a **single
key**, ``budget_policy``, whose ``value_json`` is one JSON object::

    {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0,
     "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90}

A single key (rather than five ``budget.*`` keys) keeps a PUT atomic:
a reader can never observe a half-updated policy (new per-video cap
with the old daily cap).  The generic settings endpoints still work —
``GET /api/v1/settings`` simply shows the ``budget_policy`` entry —
while ``GET/PUT /api/v1/settings/budget`` (app.routers.settings) is
the typed, validated surface.

Defaults (roadmap section 35, ``35_cost_control.md``)
-----------------------------------------------------
* ``per_video_cap_usd = 1.00`` — L3 *Standard* stack hard cap
  (Free $0.25 / Standard $1.00 / Premium $5.00).  Note this is the
  whole-video cap; the LLM router's internal per-video *LLM* sub-cap
  remains $0.50 (``app.providers.llm.caps.PER_VIDEO_CAP_USD``).
* ``daily_cap_usd = 10.00`` — L5 global/day default.
* ``monthly_cap_usd = 30.00`` — L6 global/month suggested starter.
* ``warn_pct = 70`` / ``soft_pct = 90`` — the 70/90/100 threshold
  ladder (70% warn, 90% soft stop / downgrades, 100% hard stop).
  Only warn/soft are persisted here; 100% is definitional.

All money crosses this module as USD floats in JSON but is compared
in integer micros (see :mod:`app.providers.budget.money`) wherever
enforcement happens — floats are never accumulated.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .money import usd_to_micros

__all__ = [
    "SETTINGS_KEY",
    "DEFAULT_BUDGET_POLICY",
    "validate_budget_policy",
    "policy_from_settings",
    "load_budget_policy",
    "save_budget_policy",
    "per_video_cap_micros",
]

#: settings-table key holding the whole policy as one JSON object.
SETTINGS_KEY = "budget_policy"

DEFAULT_BUDGET_POLICY: dict[str, Any] = {
    "per_video_cap_usd": 1.00,
    "daily_cap_usd": 10.00,
    "monthly_cap_usd": 30.00,
    "warn_pct": 70,
    "soft_pct": 90,
}

_AMOUNT_FIELDS = ("per_video_cap_usd", "daily_cap_usd", "monthly_cap_usd")
_PCT_FIELDS = ("warn_pct", "soft_pct")


def _now_ms() -> int:
    return int(time.time() * 1000)


def validate_budget_policy(data: Any) -> dict[str, Any]:
    """Validate + normalise a candidate policy; return a clean dict.

    Raises :class:`ValueError` with a human message on any problem:
    missing / non-numeric / non-positive amounts, non-integer or
    out-of-range percentages, or ``warn_pct >= soft_pct`` (the ladder
    requires ``0 < warn < soft < 100``).
    """
    if not isinstance(data, dict):
        raise ValueError("budget policy must be an object")
    clean: dict[str, Any] = {}
    for field in _AMOUNT_FIELDS:
        if field not in data or data[field] is None:
            raise ValueError(f"{field} is required")
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number")
        value = float(value)
        # NaN / inf are floats in Python and JSON-adjacent; reject them.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field} must be a finite number")
        if value <= 0:
            raise ValueError(f"{field} must be positive")
        clean[field] = value
    for field in _PCT_FIELDS:
        if field not in data or data[field] is None:
            raise ValueError(f"{field} is required")
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number")
        if float(value) != int(float(value)):
            raise ValueError(f"{field} must be a whole percentage")
        value = int(value)
        if not (0 < value < 100):
            raise ValueError(f"{field} must be between 1 and 99")
        clean[field] = value
    if not clean["warn_pct"] < clean["soft_pct"] < 100:
        raise ValueError("warn_pct must be less than soft_pct, and soft_pct less than 100")
    return clean


def policy_from_settings(settings: Any) -> dict[str, Any]:
    """Build the effective policy from a settings mapping.

    ``settings`` is the full ``{key: value}`` dict from the settings
    table (or any mapping).  A stored ``budget_policy`` object wins;
    individual ``budget.<field>`` keys (a legacy/alternate spelling)
    override single fields.  Anything missing or invalid falls back
    field-by-field to :data:`DEFAULT_BUDGET_POLICY` — a corrupt stored
    value must never crash a reader or widen a cap silently beyond
    the documented defaults.
    """
    merged: dict[str, Any] = dict(DEFAULT_BUDGET_POLICY)
    if isinstance(settings, dict):
        stored = settings.get(SETTINGS_KEY)
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except (ValueError, TypeError):
                stored = None
        if isinstance(stored, dict):
            for field in merged:
                if stored.get(field) is not None:
                    merged[field] = stored[field]
        for field in list(merged):
            alt = settings.get(f"budget.{field}")
            if alt is not None:
                merged[field] = alt
    try:
        return validate_budget_policy(merged)
    except ValueError:
        # Field-by-field salvage: keep each valid stored field, default
        # the rest.  If even that fails (e.g. warn>=soft pair), defaults.
        salvaged = dict(DEFAULT_BUDGET_POLICY)
        for field in _AMOUNT_FIELDS:
            try:
                salvaged[field] = validate_budget_policy(
                    {**DEFAULT_BUDGET_POLICY, field: merged[field]}
                )[field]
            except ValueError:
                pass
        try:
            pair = validate_budget_policy(
                {**salvaged, "warn_pct": merged["warn_pct"], "soft_pct": merged["soft_pct"]}
            )
            salvaged["warn_pct"], salvaged["soft_pct"] = pair["warn_pct"], pair["soft_pct"]
        except ValueError:
            pass
        return salvaged


def _open_conn(conn_factory: Any):
    """Return ``(conn, should_close)`` — same convention as gates_bridge."""
    if conn_factory is None:
        from app import db as _db

        return _db.get_connection(), True
    if callable(conn_factory):
        try:
            conn = conn_factory()
        except TypeError:
            conn = conn_factory(None)  # type: ignore[call-arg]
        try:
            probe = conn_factory()
        except Exception:
            return conn, True
        if probe is conn:
            return conn, False
        try:
            probe.close()
        except Exception:
            pass
        return conn, True
    if hasattr(conn_factory, "execute"):
        return conn_factory, False
    raise TypeError("conn_factory must be callable or a DB connection")


def _read_settings(conn: Any) -> dict[str, Any]:
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "settings" not in tables:
            return {}
        result: dict[str, Any] = {}
        for row in conn.execute("SELECT key, value_json FROM settings"):
            raw = row[1]
            try:
                result[row[0]] = json.loads(raw)
            except (TypeError, ValueError):
                result[row[0]] = raw
        return result
    except Exception:
        return {}


def load_budget_policy(conn_factory: Any = None) -> dict[str, Any]:
    """Effective budget policy from the settings table (or defaults)."""
    conn, should_close = _open_conn(conn_factory)
    try:
        return policy_from_settings(_read_settings(conn))
    finally:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass


def save_budget_policy(policy: Any, conn_factory: Any = None) -> dict[str, Any]:
    """Validate + persist ``policy`` under the single settings key."""
    clean = validate_budget_policy(policy)
    conn, should_close = _open_conn(conn_factory)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " key TEXT PRIMARY KEY, value_json TEXT NOT NULL,"
            " updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(clean), _now_ms()),
        )
        try:
            conn.commit()
        except Exception:
            pass
        return clean
    finally:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass


def per_video_cap_micros(policy: Optional[dict[str, Any]] = None, conn_factory: Any = None) -> int:
    """Per-video hard cap in integer micros for ``policy`` (or stored)."""
    effective = policy if policy is not None else load_budget_policy(conn_factory)
    return usd_to_micros(str(effective["per_video_cap_usd"]))
