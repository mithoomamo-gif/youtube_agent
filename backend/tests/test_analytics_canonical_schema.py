"""R5-002 closure — ONE canonical ``channel_metric_snapshots`` schema.

Regression guards for the competing-schema defect (R5-002 /
R5-ANALYTICS-001): ``app.analytics`` and ``app.analytics_sync`` once
each carried a ``CREATE TABLE IF NOT EXISTS`` for
``channel_metric_snapshots`` in different shapes (store:
``subscribers`` / ``captured_at`` / ``capture_day`` /
``snapshot_kind``; sync: ``subscriber_count`` / ``snapshot_at`` /
``snapshot_day``), so the first creator won and — in sync-first
(production) order — the store's index statements raised
``OperationalError: no such column: snapshot_kind`` and per-video
analytics reads answered 404 / ``no_data`` despite synced data
existing.

Settled contract proven here:

1. The store's DDL (``app.analytics.schema``) is the ONLY definition;
   the sync layer defines none — it delegates to
   ``ensure_analytics_schema`` (strictly: failures propagate, they
   are not swallowed).
2. Fresh DBs built in BOTH creation orders (sync-first and
   store-first) yield IDENTICAL ``channel_metric_snapshots``
   schemas, equal to the canonical DDL.
3. A sync performed sync-first is readable back through the store
   (the per-video read path the read API is built on), exactly as
   in store-first order.
4. The idempotency day basis is UTC (the store's ``capture_day``)
   in BOTH layers: two capture instants on the same UTC day but
   different Pacific days update ONE row in place, at store level
   and through a full sync. (Pacific time still governs the shared
   quota bucket and the Analytics API query dates — never snapshot
   identity.)
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analytics.schema import (
    CHANNEL_METRIC_SNAPSHOTS_DDL,
    capture_day_for,
    ensure_analytics_schema,
)
from app.analytics.store import AnalyticsStore
from app.analytics_sync.schema import ensure_analytics_sync_schema
from app.analytics_sync.service import AnalyticsSyncService
from tests.execution_fixtures import make_db, seed_channel_niche, seed_project

_PACIFIC = ZoneInfo("America/Los_Angeles")

#: 2026-01-15T02:00:00Z — UTC day 2026-01-15, Pacific day 2026-01-14.
_INSTANT_A = int(
    datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc).timestamp() * 1000
)
#: 2026-01-15T09:00:00Z — UTC day 2026-01-15, Pacific day 2026-01-15.
_INSTANT_B = int(
    datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc).timestamp() * 1000
)

_CANONICAL_COLUMNS = [
    "id",
    "channel_id",
    "snapshot_kind",
    "captured_at",
    "capture_day",
    "subscribers",
    "total_views",
    "watch_hours",
    "qualified_watch_hours",
    "metrics_json",
    "created_at",
    "updated_at",
]

_SYNC_SHAPE_COLUMNS = {"subscriber_count", "snapshot_at", "snapshot_day"}


def _normalise_sql(sql: str) -> str:
    return " ".join(sql.replace("IF NOT EXISTS ", "").split())


def _schema_dump(conn: sqlite3.Connection) -> dict:
    """Full comparable dump of the channel_metric_snapshots schema."""
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'channel_metric_snapshots'"
    ).fetchone()[0]
    columns = [
        (row[1], row[2], row[3], row[5])
        for row in conn.execute("PRAGMA table_info(channel_metric_snapshots)")
    ]
    indexes = sorted(
        (row[1], row[2])
        for row in conn.execute("PRAGMA index_list(channel_metric_snapshots)")
    )
    index_sql = sorted(
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name = 'channel_metric_snapshots' AND sql IS NOT NULL"
        )
    )
    return {
        "table_sql": _normalise_sql(table_sql),
        "columns": columns,
        "indexes": indexes,
        "index_sql": [_normalise_sql(s) for s in index_sql],
    }


def _build_in_order(order: str) -> dict:
    conn = sqlite3.connect(":memory:")
    try:
        if order == "sync-first":
            ensure_analytics_sync_schema(conn)
            ensure_analytics_schema(conn)
        elif order == "store-first":
            ensure_analytics_schema(conn)
            ensure_analytics_sync_schema(conn)
        else:  # pragma: no cover - guard
            raise AssertionError(order)
        return _schema_dump(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1+2. One canonical DDL; identical schema in both creation orders
# ---------------------------------------------------------------------------


def test_both_creation_orders_yield_identical_canonical_schema():
    sync_first = _build_in_order("sync-first")
    store_first = _build_in_order("store-first")

    assert sync_first == store_first
    assert sync_first["table_sql"] == _normalise_sql(
        CHANNEL_METRIC_SNAPSHOTS_DDL
    )
    column_names = [c[0] for c in sync_first["columns"]]
    assert column_names == _CANONICAL_COLUMNS
    assert not (_SYNC_SHAPE_COLUMNS & set(column_names))


def test_sync_layer_carries_no_channel_metric_snapshots_ddl():
    """The sync schema module must not define the table at all —
    delegation only (a reintroduced parallel CREATE TABLE would
    silently race the canonical one again)."""
    import app.analytics_sync.schema as sync_schema

    module_values = vars(sync_schema).values()
    for value in module_values:
        if isinstance(value, str):
            assert "channel_metric_snapshots" not in value or (
                "CREATE TABLE" not in value.upper()
            )
    ddl_statements = getattr(sync_schema, "ANALYTICS_SYNC_DDL", ())
    assert all(
        "channel_metric_snapshots" not in stmt for stmt in ddl_statements
    )


# ---------------------------------------------------------------------------
# 3. Synced data is readable per-video in BOTH creation orders
# ---------------------------------------------------------------------------


class _DictClient:
    """Minimal duck-typed analytics client (dict results — the sync
    service normalises dicts and typed results alike)."""

    async def get_channel_lifetime_stats(self, *, channel_id):
        return {
            "status": "ok",
            "data": {
                "subscriber_count": 4321,
                "view_count": 777000,
                "video_count": 12,
            },
            "quota_units": 1,
            "quota_method": "channels.list",
        }

    async def get_channel_totals(self, *, channel_id, start_date, end_date):
        return {
            "status": "ok",
            "data": {
                "views": 500,
                "watch_time_minutes": 900.0,
                "average_view_duration_seconds": 60.0,
                "likes": 40,
                "comment_count": 5,
                "shares": 2,
                "subscribers_gained": 9,
                "subscribers_lost": 1,
                "has_data": True,
            },
            "quota_units": 1,
            "quota_method": "youtubeAnalytics.reports.query",
        }

    async def get_videos_metrics(
        self, *, channel_id, video_ids, start_date, end_date
    ):
        return {
            "status": "ok",
            "data": [
                {
                    "video_id": vid,
                    "views": 2222,
                    "watch_time_minutes": 300.0,
                    "average_view_duration_seconds": 50.0,
                    "likes": 70,
                    "comment_count": 8,
                    "shares": 3,
                    "subscribers_gained": 4,
                    "has_data": True,
                }
                for vid in video_ids
            ],
            "quota_units": 1,
            "quota_method": "youtubeAnalytics.reports.query",
        }

    async def get_retention_curve(
        self, *, channel_id, video_id, start_date, end_date
    ):
        return {
            "status": "ok",
            "data": {
                "points": [
                    {"elapsed_ratio": 0.0, "watch_ratio": 1.0},
                    {"elapsed_ratio": 1.0, "watch_ratio": 0.4},
                ]
            },
            "quota_units": 1,
            "quota_method": "youtubeAnalytics.reports.query",
        }


def _seed_video(factory, channel_id, niche_id, video_id, published_at):
    project_id = seed_project(factory, channel_id, niche_id, status="published")
    conn = factory()
    try:
        conn.execute(
            "UPDATE projects SET youtube_video_id = ?, published_at = ?"
            " WHERE id = ?",
            (video_id, published_at, project_id),
        )
        conn.execute(
            "INSERT INTO publish_records (id, project_id, youtube_video_id,"
            " video_url, title, privacy_status, upload_idempotency_key,"
            " published_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'public', ?, ?, ?)",
            (
                uuid.uuid4().hex,
                project_id,
                video_id,
                f"https://youtu.be/{video_id}",
                "Seeded video",
                f"seed-{video_id}",
                published_at,
                published_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("order", ["sync-first", "store-first"])
def test_synced_data_readable_in_both_creation_orders(tmp_path, order):
    _path, factory = make_db(tmp_path / order)
    channel_id, niche_id = seed_channel_niche(factory)
    published = _INSTANT_A - 10 * 86_400_000
    _seed_video(factory, channel_id, niche_id, "vid-r5002", published)

    # Build the schema in the requested order BEFORE any service or
    # store call touches the database.
    conn = factory()
    try:
        if order == "sync-first":
            ensure_analytics_sync_schema(conn)
        else:
            ensure_analytics_schema(conn)
    finally:
        conn.close()

    service = AnalyticsSyncService(factory, client=_DictClient())
    result = service.sync_channel(
        channel_id, trigger="manual", now_ms=_INSTANT_A
    )
    assert result["status"] == "success", result
    assert result["snapshots_written"] == 1
    assert result["channel_snapshots_written"] == 1

    # Per-video read path (the store reads the read API is built on).
    store = AnalyticsStore(factory)
    snaps = store.list_snapshots(channel_id, video_external_id="vid-r5002")
    assert len(snaps) == 1
    assert snaps[0]["metrics"]["views"] == 2222
    assert snaps[0]["capture_day"] == "2026-01-15"

    channel_metrics = store.latest_channel_metrics(channel_id)
    assert channel_metrics is not None
    assert channel_metrics["subscribers"] == 4321
    assert channel_metrics["capture_day"] == "2026-01-15"
    assert store.last_synced_at(channel_id) == _INSTANT_A


# ---------------------------------------------------------------------------
# 4. Idempotency day basis: UTC in both layers
# ---------------------------------------------------------------------------


def test_day_basis_premise_instants_straddle_pacific_midnight():
    """Guard the premise of the test below: A and B share a UTC day
    but fall on DIFFERENT Pacific days — under a Pacific basis they
    would be two rows; under UTC they are one."""
    day_a = datetime.fromtimestamp(_INSTANT_A / 1000, tz=_PACIFIC).date()
    day_b = datetime.fromtimestamp(_INSTANT_B / 1000, tz=_PACIFIC).date()
    assert day_a != day_b
    assert capture_day_for(_INSTANT_A) == capture_day_for(_INSTANT_B)


def test_store_idempotency_day_is_utc_not_pacific(tmp_path):
    _path, factory = make_db(tmp_path)
    channel_id, _niche_id = seed_channel_niche(factory)
    store = AnalyticsStore(factory)

    first = store.upsert_channel_metrics(
        channel_id, subscribers=100, snapshot_kind="scheduled",
        captured_at_ms=_INSTANT_A,
    )
    second = store.upsert_channel_metrics(
        channel_id, subscribers=101, snapshot_kind="scheduled",
        captured_at_ms=_INSTANT_B,
    )
    assert second["id"] == first["id"]
    assert second["capture_day"] == "2026-01-15"
    assert len(store.list_channel_metrics(channel_id)) == 1

    snap_a = store.upsert_snapshot(
        channel_id, video_external_id="vid-day", snapshot_kind="d7",
        metrics={"views": 10}, captured_at_ms=_INSTANT_A,
    )
    snap_b = store.upsert_snapshot(
        channel_id, video_external_id="vid-day", snapshot_kind="d7",
        metrics={"views": 11}, captured_at_ms=_INSTANT_B,
    )
    assert snap_b["id"] == snap_a["id"]
    assert snap_b["capture_day"] == "2026-01-15"
    assert len(store.list_snapshots(channel_id)) == 1


def test_sync_idempotency_day_is_utc_not_pacific(tmp_path):
    """Two full syncs at instants A and B (same UTC day, different
    Pacific days) must leave exactly ONE channel snapshot row and
    ONE video snapshot row — the sync inherits the store's UTC
    basis because it writes through the store."""
    _path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    published = _INSTANT_A - 10 * 86_400_000
    _seed_video(factory, channel_id, niche_id, "vid-r5002-day", published)

    service = AnalyticsSyncService(factory, client=_DictClient())
    first = service.sync_channel(
        channel_id, trigger="scheduled", now_ms=_INSTANT_A
    )
    second = service.sync_channel(
        channel_id, trigger="scheduled", now_ms=_INSTANT_B
    )
    assert first["status"] == "success", first
    assert second["status"] == "success", second

    conn = factory()
    try:
        channel_rows = conn.execute(
            "SELECT capture_day FROM channel_metric_snapshots"
            " WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
        video_rows = conn.execute(
            "SELECT capture_day FROM analytics_snapshots"
            " WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in channel_rows] == [("2026-01-15",)]
    assert [tuple(r) for r in video_rows] == [("2026-01-15",)]
