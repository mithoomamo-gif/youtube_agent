"""Quota exposure tests (Round 3) — ``app.quota_view`` + endpoints.

Covers:
- empty ledger -> honest zeros + exact Pacific reset countdown math
  (fixed ``now``, winter PST and summer PDT);
- seeded ``api_usage`` rows -> correct bucket totals, consistent with
  ``providers.youtube.quota.classify_method`` (stored ``units`` win;
  ``units IS NULL`` falls back to the per-method cost), with
  other-provider / other-Pacific-day rows excluded;
- ``GET /api/v1/usage/quota`` response shape;
- ``GET /api/v1/queue/snapshot`` embeds the real quota object.

Endpoint tests build a minimal FastAPI app (aggregate router only, no
launch-token middleware — same pattern as ``test_routers.py``) and point
``app.db.get_connection`` at an isolated tmp-path database, so no test
touches the shared session DB.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as app_db
from app.errors import register_exception_handlers
from app.providers.youtube.quota import PACIFIC, next_reset, pacific_day
from app.quota_view import get_quota_snapshot, quota_snapshot_from_conn
from app.routers import api as api_module

# Fixed clocks: 10:30 Pacific on a winter (PST, UTC-8) and a summer
# (PDT, UTC-7) day.  Reset is next Pacific midnight in both cases.
WINTER_NOW = datetime(2026, 1, 15, 10, 30, tzinfo=PACIFIC)
SUMMER_NOW = datetime(2026, 7, 15, 10, 30, tzinfo=PACIFIC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _seed(
    conn: sqlite3.Connection,
    *,
    provider: str = "youtube",
    operation: str,
    units,
    created_at_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO api_usage "
        "(id, provider, operation, project_id, agent_run_id, units, "
        " tokens_in, tokens_out, characters, images, cost_micros, "
        " status, created_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL, 0, "
        " 'ok', ?)",
        (uuid.uuid4().hex, provider, operation, units, created_at_ms),
    )


@pytest.fixture()
def ledger_conn(tmp_path):
    """An isolated, schema-initialised DB connection (caller-owned)."""
    db_file = tmp_path / "quota.db"
    app_db.init_db(str(db_file)).close()
    conn = app_db.connect(str(db_file))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Unit: empty ledger + reset math
# ---------------------------------------------------------------------------


def test_empty_ledger_zeros_and_reset_math_winter(ledger_conn) -> None:
    snap = quota_snapshot_from_conn(ledger_conn, now=WINTER_NOW)
    assert snap["buckets"] == {
        "uploads": {"used": 0, "limit": 100},
        "search": {"used": 0, "limit": 100},
        "shared": {"used": 0, "limit": 10000},
    }
    assert snap["date"] == "2026-01-15"
    assert snap["pacific_date"] == "2026-01-15"
    # Midnight PST = 08:00 UTC; 10:30 -> 13.5 h = 48,600 s remain.
    assert snap["resets_at"] == "2026-01-16T08:00:00Z"
    assert snap["seconds_until_reset"] == 48_600


def test_empty_ledger_reset_math_summer_dst(ledger_conn) -> None:
    snap = quota_snapshot_from_conn(ledger_conn, now=SUMMER_NOW)
    assert snap["buckets"]["uploads"] == {"used": 0, "limit": 100}
    assert snap["pacific_date"] == "2026-07-15"
    # Midnight PDT = 07:00 UTC (DST shifts the UTC reset instant).
    assert snap["resets_at"] == "2026-07-16T07:00:00Z"
    assert snap["seconds_until_reset"] == 48_600


def test_missing_table_returns_honest_zeros(tmp_path) -> None:
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        snap = quota_snapshot_from_conn(bare, now=WINTER_NOW)
    finally:
        bare.close()
    assert snap["buckets"]["shared"] == {"used": 0, "limit": 10000}
    assert snap["resets_at"] == "2026-01-16T08:00:00Z"


# ---------------------------------------------------------------------------
# Unit: seeded ledger -> bucket totals
# ---------------------------------------------------------------------------


def test_seeded_rows_bucket_totals(ledger_conn) -> None:
    noon = datetime(2026, 1, 15, 12, 0, tzinfo=PACIFIC)
    at = _ms(noon)
    # uploads bucket: videos.insert (incl. prefixed spelling), 1 each.
    _seed(ledger_conn, operation="videos.insert", units=1, created_at_ms=at)
    _seed(ledger_conn, operation="videos.insert", units=1, created_at_ms=at)
    _seed(
        ledger_conn,
        operation="youtube.videos.insert",
        units=1,
        created_at_ms=at,
    )
    # search bucket: search.list, 1 each.
    for _ in range(3):
        _seed(ledger_conn, operation="search.list", units=1, created_at_ms=at)
    # shared bucket: stored units are authoritative...
    _seed(ledger_conn, operation="thumbnails.set", units=50, created_at_ms=at)
    _seed(ledger_conn, operation="captions.insert", units=400, created_at_ms=at)
    _seed(ledger_conn, operation="videos.list", units=7, created_at_ms=at)
    # ...and NULL units fall back to classify_method's per-method cost.
    _seed(
        ledger_conn, operation="playlistItems.insert", units=None,
        created_at_ms=at,
    )  # -> 50
    _seed(
        ledger_conn, operation="videos.update", units=None,
        created_at_ms=at,
    )  # -> 50
    # Excluded: another provider on the same day.
    _seed(
        ledger_conn, provider="gemini", operation="chat.completion",
        units=999, created_at_ms=at,
    )
    # Excluded: previous Pacific day and the reset instant itself.
    _seed(
        ledger_conn, operation="videos.insert", units=1,
        created_at_ms=_ms(noon - timedelta(days=1)),
    )
    _seed(
        ledger_conn, operation="videos.insert", units=1,
        created_at_ms=_ms(datetime(2026, 1, 16, 0, 0, tzinfo=PACIFIC)),
    )

    snap = quota_snapshot_from_conn(ledger_conn, now=WINTER_NOW)
    assert snap["buckets"]["uploads"] == {"used": 3, "limit": 100}
    assert snap["buckets"]["search"] == {"used": 3, "limit": 100}
    # 50 + 400 + 7 (stored) + 50 + 50 (fallbacks) = 557.
    assert snap["buckets"]["shared"] == {"used": 557, "limit": 10000}
    assert snap["pacific_date"] == "2026-01-15"


def test_null_units_fallback_costs_match_classify(ledger_conn) -> None:
    """One NULL-units row per method: totals must equal quota.py costs."""
    at = _ms(datetime(2026, 1, 15, 12, 0, tzinfo=PACIFIC))
    for op in (
        "videos.insert",          # uploads 1
        "search.list",            # search 1
        "thumbnails.set",         # shared 50
        "captions.insert",        # shared 400
        "captions.update",        # shared 450
        "playlistItems.insert",   # shared 50
        "videos.update",          # shared 50
        "videos.list",            # shared 1 (read class)
        "channels.list",          # shared 1 (read class)
    ):
        _seed(ledger_conn, operation=op, units=None, created_at_ms=at)

    snap = quota_snapshot_from_conn(ledger_conn, now=WINTER_NOW)
    assert snap["buckets"]["uploads"]["used"] == 1
    assert snap["buckets"]["search"]["used"] == 1
    assert snap["buckets"]["shared"]["used"] == 50 + 400 + 450 + 50 + 50 + 1 + 1


def test_get_quota_snapshot_opens_configured_db(tmp_path, monkeypatch) -> None:
    """The no-conn entry point resolves the DB via app.db at call time."""
    db_file = tmp_path / "via_settings.db"
    app_db.init_db(str(db_file)).close()
    monkeypatch.setattr(
        app_db, "get_connection", lambda db_path=None: app_db.connect(str(db_file))
    )
    snap = get_quota_snapshot(now=WINTER_NOW)
    assert snap["buckets"]["uploads"] == {"used": 0, "limit": 100}
    assert snap["seconds_until_reset"] == 48_600


# ---------------------------------------------------------------------------
# Endpoints (minimal app, isolated DB via monkeypatched get_connection)
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    db_file = tmp_path / "endpoint.db"
    app_db.init_db(str(db_file)).close()
    monkeypatch.setattr(
        app_db, "get_connection", lambda db_path=None: app_db.connect(str(db_file))
    )
    app = FastAPI(title="quota endpoint tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return TestClient(app), db_file


def _seed_file(db_file, **kwargs) -> None:
    conn = app_db.connect(str(db_file))
    try:
        _seed(conn, **kwargs)
    finally:
        conn.close()


def test_usage_quota_endpoint_empty_zeros_and_countdown(api_env) -> None:
    client, _ = api_env
    before = datetime.now(timezone.utc)
    resp = client.get("/api/v1/usage/quota")
    after = datetime.now(timezone.utc)
    assert resp.status_code == 200
    body = resp.json()
    assert body["buckets"] == {
        "uploads": {"used": 0, "limit": 100},
        "search": {"used": 0, "limit": 100},
        "shared": {"used": 0, "limit": 10000},
    }
    assert body["pacific_date"] == pacific_day(after).isoformat()
    assert body["date"] == body["pacific_date"]
    # resets_at is the next Pacific midnight, expressed in UTC...
    expected_reset = next_reset(after).astimezone(timezone.utc)
    got_reset = datetime.fromisoformat(body["resets_at"].replace("Z", "+00:00"))
    assert got_reset == expected_reset
    # ...and the countdown agrees with it (small tolerance for the
    # request round-trip; a Pacific day is 23-25 h, never more).
    expected_seconds = (expected_reset - before).total_seconds()
    assert 0 < body["seconds_until_reset"] <= 25 * 3600
    assert abs(body["seconds_until_reset"] - expected_seconds) < 15


def test_usage_quota_endpoint_seeded_totals(api_env) -> None:
    client, db_file = api_env
    now_ms = int(time.time() * 1000)
    _seed_file(db_file, operation="videos.insert", units=1, created_at_ms=now_ms)
    _seed_file(db_file, operation="videos.insert", units=1, created_at_ms=now_ms)
    _seed_file(db_file, operation="search.list", units=1, created_at_ms=now_ms)
    _seed_file(db_file, operation="thumbnails.set", units=50, created_at_ms=now_ms)
    # NULL units -> fallback cost 400 for captions.insert.
    _seed_file(db_file, operation="captions.insert", units=None, created_at_ms=now_ms)

    resp = client.get("/api/v1/usage/quota")
    assert resp.status_code == 200
    buckets = resp.json()["buckets"]
    assert buckets["uploads"] == {"used": 2, "limit": 100}
    assert buckets["search"] == {"used": 1, "limit": 100}
    assert buckets["shared"] == {"used": 450, "limit": 10000}


def test_queue_snapshot_contains_real_quota_object(api_env) -> None:
    client, db_file = api_env
    now_ms = int(time.time() * 1000)
    _seed_file(db_file, operation="videos.insert", units=1, created_at_ms=now_ms)
    _seed_file(db_file, operation="search.list", units=1, created_at_ms=now_ms)
    _seed_file(db_file, operation="thumbnails.set", units=50, created_at_ms=now_ms)

    resp = client.get("/api/v1/queue/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    quota = body["quota"]
    assert isinstance(quota, dict) and quota != {}
    assert quota["buckets"]["uploads"] == {"used": 1, "limit": 100}
    assert quota["buckets"]["search"] == {"used": 1, "limit": 100}
    assert quota["buckets"]["shared"] == {"used": 50, "limit": 10000}
    assert quota["pacific_date"] == pacific_day().isoformat()
    assert quota["resets_at"]
    assert 0 < quota["seconds_until_reset"] <= 25 * 3600

    # The snapshot's quota agrees with the dedicated endpoint.
    usage = client.get("/api/v1/usage/quota").json()
    assert usage["buckets"] == quota["buckets"]
    assert usage["pacific_date"] == quota["pacific_date"]
