"""Analytics sync tests (Round 5 — Analytics Ingestion).

Covers ``app.analytics_sync.service`` (sync service + scheduler
poller), the schema module, the ExecutionService hosting and the
manual-trigger router:

1. Full sync (mocked client returning the REAL typed results from
   ``app.providers.youtube.analytics``): channel lifetime + period
   totals land in ``channel_metric_snapshots``; per-video metrics +
   retention land in ``analytics_snapshots`` with age-based
   ``snapshot_kind``; the run + freshness state are recorded; the
   reported quota units land in the shared ``api_usage`` ledger.
2. Idempotency: a same-UTC-day re-sync updates in place (the
   store's ``capture_day`` basis governs both layers — R5-002).
3. not_connected: typed result -> recorded run with the reason,
   ZERO snapshot writes, ``last_synced_at`` stays NULL.
4. Quota: pre-flight against the shared bucket (seeded ``api_usage``)
   stops before ANY client call; a mid-sync ``quota_exceeded`` from
   the client stops with partial progress recorded.
5. Failures: failed results and raising clients are recorded, never
   raised; STOP ALL records a ``skipped`` run and claims nothing.
6. Empty sync: a channel with no known videos is an honest success.
7. Poller: due/throttle behaviour + STOP ALL latch.
8. Router: manual trigger + sync-status are owner-scoped (404 for
   unknown channels), and ExecutionService hosts the poller without
   changing its ``status()`` shape.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from app.analytics_sync.schema import ensure_analytics_sync_schema
from app.analytics_sync.service import (
    AnalyticsSyncPoller,
    AnalyticsSyncService,
    snapshot_kind_for,
)
from app.providers.youtube.analytics import (
    AnalyticsResult,
    ChannelLifetimeStats,
    ChannelTotals,
    RetentionCurve,
    RetentionPoint,
    VideoMetrics,
)
from tests.execution_fixtures import make_db, now_ms, seed_channel_niche, seed_project

_DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Fake analytics client (async, real typed results — the landed contract)
# ---------------------------------------------------------------------------


class FakeAnalyticsClient:
    """Modes: ok | not_connected | quota_on_retention | failed | raising."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def _gate(self, name: str):
        self.calls.append(name)
        if self.mode == "not_connected":
            return AnalyticsResult(
                status="not_connected", reason="no_youtube_credential",
                detail="no credential", quota_units=0,
            )
        if self.mode == "failed":
            return AnalyticsResult(
                status="failed", reason="network_error",
                detail="boom", quota_units=1,
                quota_method="youtubeAnalytics.reports.query",
            )
        if self.mode == "raising":
            raise RuntimeError("client exploded")
        return None

    async def get_channel_lifetime_stats(self, *, channel_id):
        gate = self._gate("lifetime")
        if gate is not None:
            return gate
        return AnalyticsResult(
            status="ok",
            data=ChannelLifetimeStats(
                channel_id=channel_id, view_count=99999,
                subscriber_count=1234, video_count=42,
            ),
            quota_units=1, quota_method="channels.list",
        )

    async def get_channel_totals(self, *, channel_id, start_date, end_date):
        gate = self._gate("totals")
        if gate is not None:
            return gate
        return AnalyticsResult(
            status="ok",
            data=ChannelTotals(
                channel_id=channel_id, start_date=start_date, end_date=end_date,
                views=5000, watch_time_minutes=6000.0,
                average_view_duration_seconds=72.0, likes=300,
                comment_count=40, shares=10, subscribers_gained=50,
                subscribers_lost=5, has_data=True,
            ),
            quota_units=1, quota_method="youtubeAnalytics.reports.query",
        )

    async def get_videos_metrics(self, *, channel_id, video_ids, start_date, end_date):
        gate = self._gate("videos")
        if gate is not None:
            return gate
        return AnalyticsResult(
            status="ok",
            data=[
                VideoMetrics(
                    video_id=vid, start_date=start_date, end_date=end_date,
                    views=1000, watch_time_minutes=120.0,
                    average_view_duration_seconds=45.0, likes=90,
                    comment_count=12, shares=3, subscribers_gained=7,
                    has_data=True,
                )
                for vid in video_ids
            ],
            quota_units=1, quota_method="youtubeAnalytics.reports.query",
        )

    async def get_retention_curve(self, *, channel_id, video_id, start_date, end_date):
        self.calls.append("retention")
        if self.mode == "not_connected":
            return AnalyticsResult(
                status="not_connected", reason="no_youtube_credential",
                quota_units=0,
            )
        if self.mode == "quota_on_retention":
            return AnalyticsResult(
                status="failed", reason="quota_exceeded",
                detail="quota gone", quota_units=0,
                quota_method="youtubeAnalytics.reports.query",
            )
        if self.mode == "failed":
            return AnalyticsResult(
                status="failed", reason="network_error", quota_units=1,
                quota_method="youtubeAnalytics.reports.query",
            )
        if self.mode == "raising":
            raise RuntimeError("client exploded")
        return AnalyticsResult(
            status="ok",
            data=RetentionCurve(
                video_id=video_id, start_date=start_date, end_date=end_date,
                points=[
                    RetentionPoint(elapsed_ratio=0.0, watch_ratio=1.0),
                    RetentionPoint(elapsed_ratio=0.5, watch_ratio=0.6),
                    RetentionPoint(elapsed_ratio=1.0, watch_ratio=0.3),
                ],
            ),
            quota_units=1, quota_method="youtubeAnalytics.reports.query",
        )


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_video(conn_factory, channel_id, niche_id, video_id, published_at):
    project_id = seed_project(
        conn_factory, channel_id, niche_id, status="published"
    )
    conn = conn_factory()
    try:
        conn.execute(
            "UPDATE projects SET youtube_video_id = ?, published_at = ? WHERE id = ?",
            (video_id, published_at, project_id),
        )
        conn.execute(
            "INSERT INTO publish_records (id, project_id, youtube_video_id,"
            " video_url, title, privacy_status, upload_idempotency_key,"
            " published_at, created_at) VALUES (?, ?, ?, ?, ?, 'public', ?, ?, ?)",
            (
                uuid.uuid4().hex, project_id, video_id,
                f"https://youtu.be/{video_id}", "Seeded video",
                f"seed-{video_id}", published_at, published_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return project_id


def _rows(conn_factory, sql, params=()):
    conn = conn_factory()
    try:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _service(conn_factory, client) -> AnalyticsSyncService:
    return AnalyticsSyncService(conn_factory, client=client)


@pytest.fixture()
def db(tmp_path):
    return make_db(tmp_path)


# ---------------------------------------------------------------------------
# snapshot_kind_for (pure)
# ---------------------------------------------------------------------------


def test_snapshot_kind_for_milestones():
    now = 200 * _DAY_MS
    assert snapshot_kind_for(now - 2 * _DAY_MS, now, "scheduled") == "scheduled"
    assert snapshot_kind_for(now - 2 * _DAY_MS, now, "manual") == "manual"
    assert snapshot_kind_for(now - 3 * _DAY_MS, now, "scheduled") == "d3"
    assert snapshot_kind_for(now - 10 * _DAY_MS, now, "scheduled") == "d7"
    assert snapshot_kind_for(now - 40 * _DAY_MS, now, "manual") == "d28"
    assert snapshot_kind_for(now - 120 * _DAY_MS, now, "scheduled") == "d90"
    assert snapshot_kind_for(None, now, "manual") == "manual"


# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------


def test_full_sync_writes_snapshots_and_channel_metrics(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-d7", now - 10 * _DAY_MS)
    _seed_video(factory, channel_id, niche_id, "vid-d28", now - 40 * _DAY_MS)

    client = FakeAnalyticsClient("ok")
    result = _service(factory, client).sync_channel(
        channel_id, trigger="scheduled", now_ms=now
    )

    assert result["status"] == "success"
    assert result["reason"] is None
    assert result["videos_seen"] == 2
    assert result["videos_with_data"] == 2
    assert result["snapshots_written"] == 2
    assert result["channel_snapshots_written"] == 1
    assert result["retention_written"] == 2
    # 1 lifetime + 1 totals + 1 batched metrics + 2 retention calls.
    assert result["quota_units_charged"] == 5
    assert client.calls == ["lifetime", "totals", "videos", "retention", "retention"]

    snapshots = _rows(
        factory, "SELECT * FROM analytics_snapshots ORDER BY youtube_video_id"
    )
    assert [s["youtube_video_id"] for s in snapshots] == ["vid-d28", "vid-d7"]
    kinds = {s["youtube_video_id"]: s["snapshot_kind"] for s in snapshots}
    assert kinds == {"vid-d7": "d7", "vid-d28": "d28"}
    for snap in snapshots:
        assert snap["channel_id"] == channel_id
        assert snap["views"] == 1000
        assert snap["watch_hours_x100"] == 200  # 120 min -> 2h -> x100
        assert snap["likes"] == 90
        assert snap["comments_count"] == 12
        assert snap["avg_view_duration_sec"] == 45
        assert snap["subs_delta"] == 7
        assert snap["ctr_bps"] is None  # client honestly reports no CTR
        curve = json.loads(snap["retention_curve_json"])
        assert len(curve) == 3 and curve[1]["watch_ratio"] == 0.6

    # Canonical (storage-team) shape: subscribers/total_views/watch_hours
    # columns + period figures inside metrics_json (see R5-ANALYTICS-001).
    channel_rows = _rows(factory, "SELECT * FROM channel_metric_snapshots")
    assert len(channel_rows) == 1
    crow = channel_rows[0]
    assert crow["channel_id"] == channel_id
    assert crow["subscribers"] == 1234
    assert crow["total_views"] == 99999
    assert crow["watch_hours"] == 100.0  # 6000 min -> 100h, ordinary hours
    assert crow["qualified_watch_hours"] is None  # never estimated
    assert crow["snapshot_kind"] == "scheduled"
    assert crow["capture_day"] is not None and crow["captured_at"] is not None
    cmetrics = json.loads(crow["metrics_json"])
    assert cmetrics["total_videos"] == 42
    assert cmetrics["views_period"] == 5000
    assert cmetrics["watch_hours_period"] == 100.0
    assert cmetrics["subs_delta_period"] == 45
    assert cmetrics["likes_period"] == 300
    # The sync's old parallel shape must NOT exist (exactly one shape).
    assert "subscriber_count" not in crow
    assert "snapshot_day" not in crow

    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert len(runs) == 1 and runs[0]["status"] == "success"
    assert runs[0]["quota_units_charged"] == 5

    state = _rows(factory, "SELECT * FROM analytics_sync_state")
    assert len(state) == 1
    assert state[0]["last_status"] == "success"
    assert state[0]["last_synced_at"] is not None
    assert state[0]["last_reason"] is None

    usage = _rows(
        factory,
        "SELECT COALESCE(SUM(units), 0) AS total FROM api_usage"
        " WHERE provider = 'youtube'",
    )
    assert usage[0]["total"] == 5


def test_sync_then_analytics_store_read_round_trip(db):
    """Fresh DB: a full sync's writes are readable through the storage
    team's AnalyticsStore (the canonical read path the read API uses)
    — exactly one schema shape, no sync-side parallel table."""
    from app.analytics.store import AnalyticsStore

    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-rt", now - 10 * _DAY_MS)

    result = _service(factory, FakeAnalyticsClient("ok")).sync_channel(
        channel_id, trigger="manual", now_ms=now
    )
    assert result["status"] == "success"

    store = AnalyticsStore(factory)
    snaps = store.list_snapshots(channel_id)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["youtube_video_id"] == "vid-rt"
    assert snap["snapshot_kind"] == "d7"
    assert snap["metrics"]["views"] == 1000
    assert snap["metrics"]["watch_minutes"] == 120.0
    assert snap["metrics"].get("ctr") is None  # unreported = absent/None
    assert snap["retention_curve"][1]["watch_ratio"] == 0.6
    latest = store.latest_snapshot(channel_id, "vid-rt")
    assert latest is not None and latest["id"] == snap["id"]

    channel_metrics = store.latest_channel_metrics(channel_id)
    assert channel_metrics is not None
    assert channel_metrics["subscribers"] == 1234
    assert channel_metrics["total_views"] == 99999
    assert channel_metrics["watch_hours"] == 100.0
    assert channel_metrics["metrics"]["total_videos"] == 42
    assert channel_metrics["metrics"]["views_period"] == 5000
    assert store.last_synced_at(channel_id) is not None

    # The canonical table shape is the storage team's: the sync's old
    # parallel columns must not exist anywhere in the settled tree.
    conn = factory()
    try:
        chan_cols = {r[1] for r in conn.execute("PRAGMA table_info(channel_metric_snapshots)")}
    finally:
        conn.close()
    assert "subscriber_count" not in chan_cols
    assert "snapshot_day" not in chan_cols
    assert {"snapshot_kind", "capture_day", "captured_at", "subscribers"} <= chan_cols


def test_resync_same_day_is_idempotent(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-1", now - 10 * _DAY_MS)
    svc = _service(factory, FakeAnalyticsClient("ok"))
    svc.sync_channel(channel_id, now_ms=now)
    result = svc.sync_channel(channel_id, now_ms=now + 60_000)
    assert result["status"] == "success"
    assert len(_rows(factory, "SELECT * FROM analytics_snapshots")) == 1
    assert len(_rows(factory, "SELECT * FROM channel_metric_snapshots")) == 1
    assert len(_rows(factory, "SELECT * FROM analytics_sync_runs")) == 2


def test_no_videos_known_is_honest_empty_sync(db):
    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    result = _service(factory, FakeAnalyticsClient("ok")).sync_channel(
        channel_id, now_ms=now_ms()
    )
    assert result["status"] == "success"
    assert result["empty"] is True
    assert result["videos_seen"] == 0
    assert result["snapshots_written"] == 0
    # Channel totals still sync — an empty video list is not an error.
    assert result["channel_snapshots_written"] == 1


# ---------------------------------------------------------------------------
# not_connected / failures — always recorded, never silent, never raised
# ---------------------------------------------------------------------------


def test_not_connected_records_reason_and_zero_writes(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-1", now - 10 * _DAY_MS)

    result = _service(factory, FakeAnalyticsClient("not_connected")).sync_channel(
        channel_id, now_ms=now
    )
    assert result["status"] == "not_connected"
    assert result["reason"] == "no_youtube_credential"
    assert result["snapshots_written"] == 0
    assert _rows(factory, "SELECT * FROM analytics_snapshots") == []
    assert _rows(factory, "SELECT * FROM channel_metric_snapshots") == []

    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "not_connected"
    assert runs[0]["reason"] == "no_youtube_credential"

    status = _service(factory, FakeAnalyticsClient("ok")).get_sync_status(channel_id)
    assert status["last_status"] == "not_connected"
    assert status["last_reason"] == "no_youtube_credential"
    assert status["last_synced_at"] is None  # never synced — honestly


def test_no_client_configured_is_recorded_not_connected(db):
    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    result = AnalyticsSyncService(factory, client=None).sync_channel(
        channel_id, now_ms=now_ms()
    )
    assert result["status"] == "not_connected"
    assert result["reason"] == "analytics_client_unavailable"
    assert _rows(factory, "SELECT * FROM analytics_snapshots") == []


def test_failed_client_is_recorded_never_raised(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-1", now - 10 * _DAY_MS)
    result = _service(factory, FakeAnalyticsClient("failed")).sync_channel(
        channel_id, now_ms=now
    )
    assert result["status"] == "failed"
    assert result["reason"] == "network_error"
    assert result["errors"]  # per-step failures recorded
    assert _rows(factory, "SELECT * FROM analytics_snapshots") == []
    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert runs[0]["status"] == "failed" and runs[0]["reason"] == "network_error"


def test_raising_client_is_recorded_never_raised(db):
    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    result = _service(factory, FakeAnalyticsClient("raising")).sync_channel(
        channel_id, now_ms=now_ms()
    )
    assert result["status"] == "failed"
    assert result["reason"] == "RuntimeError"
    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert runs[0]["status"] == "failed"


def test_unknown_channel_returns_failed_summary(db):
    _path, factory = db
    result = _service(factory, FakeAnalyticsClient("ok")).sync_channel(
        "no-such-channel", now_ms=now_ms()
    )
    assert result["status"] == "failed"
    assert result["reason"] == "channel_not_found"
    assert _rows(factory, "SELECT * FROM analytics_sync_runs") == []


# ---------------------------------------------------------------------------
# Quota awareness
# ---------------------------------------------------------------------------


def test_quota_preflight_stops_before_any_call(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-1", now - 10 * _DAY_MS)
    # Seed the shared bucket at its 90% safety ceiling for today (PT):
    # quota_view aggregates api_usage, so this is the real accounting.
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO api_usage (id, provider, operation, units,"
            " cost_micros, status, created_at) VALUES (?, 'youtube',"
            " 'channels.list', 9000, 0, 'ok', ?)",
            (uuid.uuid4().hex, now),
        )
        conn.commit()
    finally:
        conn.close()

    client = FakeAnalyticsClient("ok")
    result = _service(factory, client).sync_channel(channel_id, now_ms=now)
    assert result["status"] == "quota_exhausted"
    assert result["reason"] == "quota_exceeded"
    assert client.calls == []  # pre-flight refused before ANY call
    assert _rows(factory, "SELECT * FROM analytics_snapshots") == []
    assert _rows(factory, "SELECT * FROM channel_metric_snapshots") == []
    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert runs[0]["status"] == "quota_exhausted"


def test_quota_exhausted_mid_sync_records_partial_progress(db):
    _path, factory = db
    channel_id, niche_id = seed_channel_niche(factory)
    now = now_ms()
    _seed_video(factory, channel_id, niche_id, "vid-1", now - 10 * _DAY_MS)
    _seed_video(factory, channel_id, niche_id, "vid-2", now - 10 * _DAY_MS)

    result = _service(factory, FakeAnalyticsClient("quota_on_retention")).sync_channel(
        channel_id, now_ms=now
    )
    assert result["status"] == "quota_exhausted"
    assert result["reason"] == "quota_exceeded"
    # Partial progress kept: channel snapshot + both metric snapshots,
    # only the retention step was cut off.
    assert result["channel_snapshots_written"] == 1
    assert result["snapshots_written"] == 2
    assert result["retention_written"] == 0
    assert len(_rows(factory, "SELECT * FROM analytics_snapshots")) == 2
    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert runs[0]["status"] == "quota_exhausted"
    assert runs[0]["snapshots_written"] == 2


# ---------------------------------------------------------------------------
# Control flags + poller (scheduler hook)
# ---------------------------------------------------------------------------


def test_stop_all_records_skipped_and_claims_nothing(db):
    from app.execution.flags import set_flag

    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    set_flag(factory, "stop_all", True)
    client = FakeAnalyticsClient("ok")
    result = _service(factory, client).sync_channel(channel_id, now_ms=now_ms())
    assert result["status"] == "skipped"
    assert result["reason"] == "stop_all"
    assert client.calls == []
    runs = _rows(factory, "SELECT * FROM analytics_sync_runs")
    assert runs[0]["status"] == "skipped" and runs[0]["reason"] == "stop_all"

    poller = AnalyticsSyncPoller(factory, client=FakeAnalyticsClient("ok"))
    assert poller.poll_once() == []
    assert poller.stop_requested is True


def test_poller_due_throttle_and_resync(db):
    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    poller = AnalyticsSyncPoller(factory, client=FakeAnalyticsClient("ok"))
    now = now_ms()
    reports = poller.poll_once(now_ms=now)
    assert len(reports) == 1 and reports[0]["channel_id"] == channel_id
    assert reports[0]["status"] == "success"
    # Immediately again: last attempt is fresh -> not due.
    assert poller.poll_once(now_ms=now + 60_000) == []
    # After the minimum interval: due again.
    later = poller.poll_once(now_ms=now + 7 * 3_600_000)
    assert len(later) == 1


# ---------------------------------------------------------------------------
# Router (manual trigger + sync status, owner-scoped) + service hosting
# ---------------------------------------------------------------------------


def test_manual_endpoint_owner_scope(client, app, settings):
    from app import db as db_module

    factory = lambda: db_module.get_connection(settings.db_path)  # noqa: E731
    channel_id, _niche = seed_channel_niche(factory)
    app.state.analytics_client = FakeAnalyticsClient("ok")

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["trigger"] == "manual"
    assert body["channel_id"] == channel_id

    status = client.get(f"/api/v1/channels/{channel_id}/analytics/sync-status")
    assert status.status_code == 200
    assert status.json()["last_status"] == "success"
    assert status.json()["last_synced_at"] is not None

    missing = client.post("/api/v1/channels/nope/analytics/sync")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "channel_not_found"
    missing_status = client.get("/api/v1/channels/nope/analytics/sync-status")
    assert missing_status.status_code == 404


def test_execution_service_hosts_analytics_poller(db):
    from app.execution.service import ExecutionService

    _path, factory = db
    channel_id, _niche = seed_channel_niche(factory)
    service = ExecutionService(
        factory,
        analytics_client=FakeAnalyticsClient("ok"),
        analytics_initial_delay_s=0.05,
        analytics_interval_s=0.1,
        scheduler_initial_delay_s=60.0,
        stylelab_initial_delay_s=60.0,
    )
    assert service.analytics_poller is not None
    # status() shape is unchanged (existing lifecycle contract).
    assert set(service.status()) == {"running", "runner_alive", "scheduler_alive"}

    async def _cycle():
        await service.start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                rows = _rows(factory, "SELECT * FROM analytics_sync_state")
            except Exception:  # table appears with the first poll's sync
                rows = []
            if rows and rows[0]["last_status"] == "success":
                break
            await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(_cycle())
    rows = _rows(factory, "SELECT * FROM analytics_sync_state")
    assert rows and rows[0]["channel_id"] == channel_id
    assert rows[0]["last_status"] == "success"
