"""End-to-end acceptance tests for Round 5 analytics ingestion.

These tests wire the WHOLE ingestion path together with
transport-mocked HTTP (``httpx.MockTransport`` — no real network):

    seed a connected channel (+ known published videos)
      -> POST /api/v1/channels/{id}/analytics/sync        (sync router)
      -> app.analytics_sync.service.AnalyticsSyncService  (sync layer)
      -> app.providers.youtube.analytics.YouTubeAnalyticsClient
         (transport; returns the FIXTURE payloads below)
      -> analytics_snapshots + channel_metric_snapshots   (storage)
      -> GET .../analytics/overview | /videos | /videos/{id}/snapshots
         and GET .../analytics/sync-status                (read API)
      -> freshness stamps (``last_synced_at`` + ``sync`` block) on
         every read response

The HTTP payloads are NOT invented in this file: they are loaded
from ``tests/fixtures/analytics_api/`` — JSON fixtures shaped
exactly like real YouTube Analytics API v2 ``resultTable``
responses (``columnHeaders`` with name/columnType/dataType +
positional ``rows``) and a real-shaped Data API v3
``channels.list`` response. See that directory's README.

Covered acceptance criteria:

1. Fixture set sanity: exact resultTable shape, wire metric names,
   plus the edge shapes — an explicit all-zero video row, a
   retention curve that ends early (max elapsed ratio 0.62 < 1),
   and an empty-``rows`` response.
2. Happy path end-to-end: sync stores per-video snapshots (with
   retention curves) + the channel metric snapshot, charges the
   shared quota bucket via the ``api_usage`` ledger, and the read
   API serves overview / per-video / median-baseline responses
   carrying freshness stamps.
3. Idempotency end-to-end: a second sync on the same day updates
   in place — same snapshot row ids, no duplicate rows.
4. Negative path end-to-end: no credential -> the sync records a
   ``not_connected`` run, the mock transport's request log proves
   ZERO HTTP requests were made, storage stays empty, and the
   read API answers ``data_state='not_connected'``.
5. Quota accounting end-to-end: with the shared daily bucket at
   its safety ceiling the sync stops before ANY call; with the
   bucket nearly exhausted (one call of headroom) it makes exactly
   that one call, records the partial progress, and settles as
   ``quota_exhausted`` with the reason recorded.
6. Empty-rows edge end-to-end: a channel-totals response with no
   rows is stored as ABSENCE (period figures stay NULL), never
   as fabricated zeroes.

Each test runs against an isolated tmp-path database (the
``test_quota_view`` pattern: ``app.db.get_connection`` is
monkeypatched and a minimal FastAPI app mounts the real aggregate
router), so row counts are exact and nothing leaks into the
shared session DB.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as app_db
from app.errors import register_exception_handlers
from app.providers.youtube import InMemorySecretStore, TokenManager, TokenResponse
from app.providers.youtube.analytics import YouTubeAnalyticsClient
from app.routers import api as api_module
from tests.execution_fixtures import conn_factory_for, seed_channel_niche, seed_project

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "analytics_api"

YT_CHANNEL_ID = "UC1234567890123456789012"  # fixture channel (see fixtures README)
VIDEO_A = "vidA1234567"
VIDEO_B = "vidB1234567"
VIDEO_ZERO = "vidZero1234"
ACCESS_TOKEN = "fixture-access-token-e2e"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly "
    "https://www.googleapis.com/auth/yt-analytics.readonly"
)
_DAY_MS = 86_400_000


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Mock transport (records EVERY request; the negative path asserts on it)
# ---------------------------------------------------------------------------


class RecordingFactory:
    """Replacement for ``analytics.create_async_client`` (house style,
    mirroring tests/providers/test_youtube_analytics.py)."""

    def __init__(self, handler):
        self._handler = handler
        self.factory_calls: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, **kwargs):
        self.factory_calls.append(kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fixture_handler(*, empty_totals: bool = False):
    """Route mocked Google endpoints to the fixture payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "youtubeanalytics.googleapis.com" in url:
            params = dict(urllib.parse.parse_qsl(request.url.query.decode()))
            if params.get("dimensions") == "elapsedVideoTimeRatio":
                # VIDEO_B's curve ends early; everyone else's is full.
                if params.get("filters") == f"video=={VIDEO_B}":
                    return httpx.Response(200, json=_fixture("retention_early_end.json"))
                return httpx.Response(200, json=_fixture("retention_full.json"))
            if params.get("dimensions") == "video":
                return httpx.Response(200, json=_fixture("per_video_metrics.json"))
            if empty_totals:
                return httpx.Response(200, json=_fixture("empty_rows.json"))
            return httpx.Response(200, json=_fixture("channel_totals.json"))
        if "/youtube/v3/channels" in url:
            return httpx.Response(200, json=_fixture("data_api_channels_list.json"))
        return httpx.Response(
            404, json={"error": {"errors": [{"reason": "notFound"}]}}
        )

    return handler


# ---------------------------------------------------------------------------
# Isolated app + DB environment
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "analytics_e2e.db"
    app_db.init_db(str(db_file)).close()
    factory = conn_factory_for(db_file)
    monkeypatch.setattr(
        app_db, "get_connection", lambda db_path=None: app_db.connect(str(db_file))
    )
    app = FastAPI(title="analytics ingestion e2e")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    client = TestClient(app)
    return {"client": client, "app": app, "conn_factory": factory, "db_file": db_file}


def _query(env, sql: str, params: tuple = ()) -> list[dict]:
    conn = env["conn_factory"]()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _seed_channel(env, *, connected: bool) -> tuple[str, str]:
    """A channel row (+ niche). When ``connected``, it also carries
    the credential link + connect state the read API checks, exactly
    as the real connect flow leaves a channel."""
    channel_id, niche_id = seed_channel_niche(
        env["conn_factory"], youtube_channel_id=YT_CHANNEL_ID
    )
    if connected:
        now = int(time.time() * 1000)
        cred_id = uuid.uuid4().hex
        conn = env["conn_factory"]()
        try:
            conn.execute(
                "INSERT INTO provider_credentials (id, provider,"
                " credential_kind, keyring_ref, account_label, scopes,"
                " status, last_validated_at, created_at, updated_at)"
                " VALUES (?, 'youtube', 'oauth', ?, ?, ?, 'active', ?, ?, ?)",
                (
                    cred_id,
                    f"youtube:channel:{YT_CHANNEL_ID}",
                    YT_CHANNEL_ID,
                    SCOPES,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE channels SET credential_id = ?, config_json = ?"
                " WHERE id = ?",
                (
                    cred_id,
                    json.dumps(
                        {
                            "connect_status": "needs_audit_check",
                            "credential_status": "connected",
                            "audit_status": "unknown",
                        }
                    ),
                    channel_id,
                ),
            )
        finally:
            conn.close()
    return channel_id, niche_id


def _seed_videos(env, channel_id: str, niche_id: str) -> None:
    """Three published projects the sync can discover, all young
    enough (< 3 days) to land in the trigger ('manual') snapshot
    kind, so the read API's same-kind baseline pool includes all
    three."""
    now = int(time.time() * 1000)
    ages = {VIDEO_A: 2 * _DAY_MS, VIDEO_B: 1 * _DAY_MS, VIDEO_ZERO: 12 * 3_600_000}
    for video_id, age in ages.items():
        project_id = seed_project(
            env["conn_factory"], channel_id, niche_id,
            topic=f"Fixture video {video_id}", status="published",
        )
        conn = env["conn_factory"]()
        try:
            conn.execute(
                "UPDATE projects SET youtube_video_id = ?, published_at = ?"
                " WHERE id = ?",
                (video_id, now - age, project_id),
            )
        finally:
            conn.close()


def _connected_client() -> YouTubeAnalyticsClient:
    """The REAL analytics client over a REAL TokenManager whose blob
    was seeded through the manager's own store path — the access
    token is cached, so no token-endpoint HTTP is ever needed."""
    store = InMemorySecretStore()
    manager = TokenManager(
        store, client_id="fixture-client-id", client_secret="fixture-client-secret"
    )
    manager.store_token_response(
        YT_CHANNEL_ID,
        TokenResponse(
            access_token=ACCESS_TOKEN,
            refresh_token="fixture-refresh-token",
            expires_in=3600,
            scope=SCOPES,
        ),
    )
    return YouTubeAnalyticsClient(token_manager=manager)


def _install_factory(monkeypatch, *, empty_totals: bool = False) -> RecordingFactory:
    from app.providers.youtube import analytics as analytics_module

    rec = RecordingFactory(_fixture_handler(empty_totals=empty_totals))
    monkeypatch.setattr(analytics_module, "create_async_client", rec)
    return rec


def _seed_shared_bucket(env, units: int) -> None:
    """Charge ``units`` against today's shared quota bucket through
    the real ``api_usage`` ledger (the accounting the sync's quota
    gate reads via app.quota_view)."""
    conn = env["conn_factory"]()
    try:
        conn.execute(
            "INSERT INTO api_usage (id, provider, operation, units,"
            " cost_micros, status, created_at)"
            " VALUES (?, 'youtube', 'videos.list', ?, 0, 'ok', ?)",
            (uuid.uuid4().hex, units, int(time.time() * 1000)),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Fixture set: exact real-API shape + edge shapes
# ---------------------------------------------------------------------------


def test_fixture_set_shape_matches_real_analytics_responses():
    for name in (
        "channel_totals.json",
        "per_video_metrics.json",
        "retention_full.json",
        "retention_early_end.json",
        "empty_rows.json",
    ):
        payload = _fixture(name)
        assert payload["kind"] == "youtubeAnalytics#resultTable", name
        headers = payload["columnHeaders"]
        assert headers, name
        for header in headers:
            # The exact header triple of a real resultTable.
            assert set(header) == {"name", "columnType", "dataType"}, name
            assert header["columnType"] in ("DIMENSION", "METRIC"), name
            assert header["dataType"] in ("STRING", "INTEGER", "FLOAT"), name
        for row in payload["rows"]:
            assert len(row) == len(headers), name


def test_fixture_channel_totals_and_per_video_values():
    totals = _fixture("channel_totals.json")
    names = [h["name"] for h in totals["columnHeaders"]]
    # Real wire names — never the task-facing aliases.
    assert "estimatedMinutesWatched" in names and "comments" in names
    assert totals["rows"] == [[45210, 198234.5, 263, 54.25, 3120, 486, 210, 890, 145]]

    videos = _fixture("per_video_metrics.json")
    assert videos["columnHeaders"][0]["name"] == "video"
    by_id = {row[0]: row for row in videos["rows"]}
    assert by_id[VIDEO_A][1] == 15234 and by_id[VIDEO_B][1] == 4210


def test_fixture_edge_shapes_zero_views_early_retention_empty_rows():
    videos = _fixture("per_video_metrics.json")
    by_id = {row[0]: row for row in videos["rows"]}
    # Edge: a video with zero views — an explicit all-zero row.
    assert by_id[VIDEO_ZERO] == [VIDEO_ZERO, 0, 0.0, 0, 0.0, 0, 0, 0, 0]

    full = _fixture("retention_full.json")
    assert max(row[0] for row in full["rows"]) == 1.0
    early = _fixture("retention_early_end.json")
    elapsed = [row[0] for row in early["rows"]]
    # Edge: the curve ENDS EARLY — coverage stops at 0.62 (< 1).
    assert max(elapsed) == 0.62 and max(elapsed) < 1.0
    assert elapsed == sorted(elapsed)

    # Edge: valid headers, no rows at all (absence, not zeroes).
    empty = _fixture("empty_rows.json")
    assert empty["rows"] == [] and empty["columnHeaders"]

    # Data API lifetime fixture: counters are STRINGS, as really returned.
    channels = _fixture("data_api_channels_list.json")
    stats = channels["items"][0]["statistics"]
    assert stats == {
        "viewCount": "9876543",
        "subscriberCount": "54321",
        "hiddenSubscriberCount": False,
        "videoCount": "187",
    }


# ---------------------------------------------------------------------------
# 2 + 3. Happy path end-to-end + idempotent re-sync
# ---------------------------------------------------------------------------


def test_end_to_end_sync_stores_snapshots_and_read_api_serves_them(env, monkeypatch):
    rec = _install_factory(monkeypatch)
    channel_id, niche_id = _seed_channel(env, connected=True)
    _seed_videos(env, channel_id, niche_id)
    env["app"].state.analytics_client = _connected_client()
    client: TestClient = env["client"]

    # -- run the sync through the real router -------------------------
    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "success", run
    assert run["reason"] is None
    assert run["videos_seen"] == 3
    assert run["snapshots_written"] == 3
    assert run["channel_snapshots_written"] == 1
    assert run["retention_written"] == 3
    # 1 lifetime + 1 totals + 1 batched videos query + 3 retention.
    assert run["quota_units_charged"] == 6

    # -- the transport saw exactly those calls, all authorised --------
    assert len(rec.requests) == 6
    for request in rec.requests:
        assert request.headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}"
    analytics_params = [
        dict(urllib.parse.parse_qsl(r.url.query.decode()))
        for r in rec.requests
        if "youtubeanalytics.googleapis.com" in str(r.url)
    ]
    assert len(analytics_params) == 5
    assert all(p["ids"] == f"channel=={YT_CHANNEL_ID}" for p in analytics_params)

    # -- storage: per-video snapshots + retention ----------------------
    snapshots = _query(
        env,
        "SELECT * FROM analytics_snapshots WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(snapshots) == 3
    by_video = {row["youtube_video_id"]: row for row in snapshots}
    a = by_video[VIDEO_A]
    assert a["snapshot_kind"] == "manual"  # manual trigger, age < 3d
    assert a["views"] == 15234 and a["likes"] == 1102
    assert a["comments_count"] == 203 and a["shares"] == 87
    assert a["watch_hours_x100"] == round(81230.0 * 100 / 60)
    assert a["avg_view_duration_sec"] == 320
    assert a["subs_delta"] == 340
    assert a["ctr_bps"] is None  # CTR is never invented by the transport
    curve_a = json.loads(a["retention_curve_json"])
    assert len(curve_a) == 9 and curve_a[0] == {"elapsed_ratio": 0.0, "watch_ratio": 1.0}
    curve_b = json.loads(by_video[VIDEO_B]["retention_curve_json"])
    # The early-ending curve is stored as-is: coverage stops at 0.62.
    assert max(p["elapsed_ratio"] for p in curve_b) == 0.62
    zero = by_video[VIDEO_ZERO]
    assert zero["views"] == 0 and zero["watch_hours_x100"] == 0  # a real zero

    # -- storage: channel metric snapshot (lifetime merged w/ period) --
    channel_rows = _query(
        env,
        "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(channel_rows) == 1
    crow = channel_rows[0]
    # Canonical shape (app.analytics.schema / AnalyticsStore): lifetime
    # counters live in the subscribers/total_views columns, the period
    # watch time in the watch_hours column, and everything else in
    # metrics_json (lifetime total_videos + the *_period keys).
    assert crow["subscribers"] == 54321
    assert crow["total_views"] == 9876543
    assert crow["watch_hours"] == pytest.approx(198234.5 / 60)
    # Sync never writes a qualification figure: always NULL here.
    assert crow["qualified_watch_hours"] is None
    assert crow["snapshot_kind"] == "manual"
    assert crow["capture_day"]
    assert crow["captured_at"] is not None
    cmetrics = json.loads(crow["metrics_json"])
    assert cmetrics["total_videos"] == 187
    assert cmetrics["views_period"] == 45210
    assert cmetrics["watch_hours_period"] == pytest.approx(198234.5 / 60)
    assert cmetrics["watch_minutes_period"] == 198234.5
    assert cmetrics["subs_delta_period"] == 890 - 145
    assert cmetrics["subscribers_gained_period"] == 890
    assert cmetrics["subscribers_lost_period"] == 145
    assert cmetrics["likes_period"] == 3120
    assert cmetrics["comments_period"] == 486
    assert cmetrics["shares_period"] == 210
    assert cmetrics["avg_view_duration_sec_period"] == 263
    assert cmetrics["period_start_date"]
    assert cmetrics["period_end_date"]

    # -- quota accounting landed in the shared ledger ------------------
    usage = _query(env, "SELECT operation, units FROM api_usage WHERE provider = 'youtube'")
    assert sum(row["units"] for row in usage) == 6
    assert {row["operation"] for row in usage} == {
        "channels.list",
        "youtubeAnalytics.reports.query",
    }

    # -- freshness surface ---------------------------------------------
    status = client.get(f"/api/v1/channels/{channel_id}/analytics/sync-status")
    assert status.status_code == 200
    status = status.json()
    assert status["last_status"] == "success"
    assert status["last_synced_at"] is not None
    assert status["last_reason"] is None

    # -- read API: overview with freshness stamps ----------------------
    overview = client.get(f"/api/v1/channels/{channel_id}/analytics/overview")
    assert overview.status_code == 200
    overview = overview.json()
    assert overview["data_state"] == "ok"
    assert overview["last_synced_at"] == status["last_synced_at"]
    assert overview["sync"]["status"] == "success"
    assert overview["sync"]["source"] == "analytics_sync_state"
    metrics = overview["metrics"]
    assert metrics["subscriber_count"] == 54321
    assert metrics["total_views"] == 9876543
    assert metrics["total_videos"] == 187
    assert metrics["period"]["views"] == 45210
    assert metrics["period"]["watch_hours"] == pytest.approx(3303.91, abs=0.01)
    assert metrics["period"]["subs_delta"] == 745
    assert metrics["captured_at"] is not None
    ypp = overview["ypp_progress"]
    assert ypp["subscribers"]["current"] == 54321
    assert ypp["subscribers"]["met"] is True
    # The sync shape carries no qualification figure: honestly
    # unavailable, never estimated from ordinary watch time.
    assert ypp["watch_hours"]["qualification_data_available"] is False
    assert ypp["watch_hours"]["current_qualified"] is None

    # -- idempotency: re-run the same day, no duplicate rows ------------
    snapshot_ids_before = sorted(row["id"] for row in snapshots)
    resp2 = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "success"
    snapshots_after = _query(
        env,
        "SELECT * FROM analytics_snapshots WHERE channel_id = ?",
        (channel_id,),
    )
    assert sorted(row["id"] for row in snapshots_after) == snapshot_ids_before
    assert len(_query(
        env,
        "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
        (channel_id,),
    )) == 1
    # Runs are history (append-only by design): exactly 2 run rows,
    # both success — the STORAGE rows above are what must not duplicate.
    runs = _query(
        env,
        "SELECT * FROM analytics_sync_runs WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(runs) == 2 and all(r["status"] == "success" for r in runs)
    # The second sync charged the bucket again (calls really happened).
    assert len(rec.requests) == 12

    # NOTE: the per-video read path (GET /analytics/videos and
    # /videos/{id}/snapshots) is asserted by the dedicated test
    # below.


def test_end_to_end_read_api_videos_baseline_and_snapshot_listing(env, monkeypatch):
    """Per-video read path — REGRESSION test for R5-ANALYTICS-001.

    History: this path originally failed end-to-end because of a
    dual ``channel_metric_snapshots`` schema — the sync layer's
    parallel DDL (``snapshot_at``/``snapshot_day``/``subscriber_count``)
    raced the storage layer's canonical DDL, the storage layer's
    ``ensure_analytics_schema`` then raised ``OperationalError``
    ('no such column: snapshot_kind') on its index statements, every
    ``AnalyticsStore`` call failed, and GET /analytics/videos
    answered ``data_state='no_data'`` despite a successful sync.
    The sync sibling settled it (see ``app.analytics_sync.schema``:
    the storage shape is canonical and the sync writes through
    ``AnalyticsStore`` exclusively); this test passed in full from
    that point and now guards the settlement.
    """
    _install_factory(monkeypatch)
    channel_id, niche_id = _seed_channel(env, connected=True)
    _seed_videos(env, channel_id, niche_id)
    env["app"].state.analytics_client = _connected_client()
    client: TestClient = env["client"]

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    status = client.get(
        f"/api/v1/channels/{channel_id}/analytics/sync-status"
    ).json()

    # -- read API: per-video + median baseline --------------------------
    videos = client.get(f"/api/v1/channels/{channel_id}/analytics/videos")
    assert videos.status_code == 200
    videos = videos.json()
    assert videos["data_state"] == "ok"
    assert videos["last_synced_at"] == status["last_synced_at"]
    items = {item["youtube_video_id"]: item for item in videos["items"]}
    assert set(items) == {VIDEO_A, VIDEO_B, VIDEO_ZERO}
    item_a = items[VIDEO_A]
    assert item_a["data_state"] == "ok"
    assert item_a["latest_snapshot"]["snapshot_kind"] == "manual"
    assert item_a["latest_snapshot"]["metrics"]["views"] == 15234
    assert item_a["latest_snapshot"]["metrics"]["watch_hours"] == pytest.approx(
        81230.0 / 60, abs=0.02
    )
    assert item_a["latest_snapshot"]["metrics"]["ctr"] is None
    comparison = item_a["comparison"]
    assert comparison is not None, item_a["comparison_unavailable_reason"]
    assert comparison["baseline_kind"] == "manual"
    assert comparison["baseline_video_count"] == 2
    # Median of the other two videos' views: (4210 + 0) / 2.
    assert comparison["baseline_median"]["views"] == 2105.0
    delta = comparison["deltas"]["views"]
    assert delta["value"] == 15234 and delta["baseline"] == 2105.0
    assert delta["pct"] == pytest.approx((15234 - 2105.0) / 2105.0 * 100, abs=0.01)
    # The zero-view video reads as DATA (a real 0), not absence.
    assert items[VIDEO_ZERO]["data_state"] == "ok"
    assert items[VIDEO_ZERO]["latest_snapshot"]["metrics"]["views"] == 0

    # -- read API: one video's snapshot listing -------------------------
    listing = client.get(
        f"/api/v1/channels/{channel_id}/analytics/videos/{VIDEO_A}/snapshots"
    )
    assert listing.status_code == 200
    listing = listing.json()
    assert listing["data_state"] == "ok"
    assert listing["last_synced_at"] == status["last_synced_at"]
    assert len(listing["items"]) == 1
    assert listing["by_kind"]["manual"][0]["metrics"]["views"] == 15234


# ---------------------------------------------------------------------------
# 4. Negative path end-to-end: no credential -> recorded, zero HTTP
# ---------------------------------------------------------------------------


def test_end_to_end_not_connected_records_run_and_makes_zero_http(env, monkeypatch):
    rec = _install_factory(monkeypatch)
    channel_id, niche_id = _seed_channel(env, connected=False)
    _seed_videos(env, channel_id, niche_id)
    # No token manager at all: the client's honest not_connected mode.
    env["app"].state.analytics_client = YouTubeAnalyticsClient(token_manager=None)
    client: TestClient = env["client"]

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200  # a sync outcome is data, not an HTTP error
    run = resp.json()
    assert run["status"] == "not_connected"
    assert run["reason"] == "no_youtube_credential"
    assert run["snapshots_written"] == 0
    assert run["channel_snapshots_written"] == 0
    assert run["quota_units_charged"] == 0

    # ZERO HTTP: the transport factory was never even invoked.
    assert rec.factory_calls == []
    assert rec.requests == []

    # The attempt is durably recorded; storage stays empty.
    runs = _query(
        env,
        "SELECT * FROM analytics_sync_runs WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "not_connected"
    assert runs[0]["reason"] == "no_youtube_credential"
    assert _query(env, "SELECT * FROM analytics_snapshots WHERE channel_id = ?",
                  (channel_id,)) == []
    assert _query(env, "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
                  (channel_id,)) == []

    status = client.get(f"/api/v1/channels/{channel_id}/analytics/sync-status")
    assert status.status_code == 200
    status = status.json()
    assert status["last_status"] == "not_connected"
    assert status["last_reason"] == "no_youtube_credential"
    assert status["last_synced_at"] is None  # never synced — honestly null

    overview = client.get(f"/api/v1/channels/{channel_id}/analytics/overview")
    assert overview.status_code == 200
    overview = overview.json()
    assert overview["data_state"] == "not_connected"
    assert overview["metrics"] is None
    assert overview["ypp_progress"] is None
    assert overview["last_synced_at"] is None
    assert overview["sync"]["status"] == "not_connected"

    videos = client.get(f"/api/v1/channels/{channel_id}/analytics/videos")
    assert videos.status_code == 200
    videos = videos.json()
    assert videos["data_state"] == "not_connected"
    assert videos["items"] == []


# ---------------------------------------------------------------------------
# 5. Quota accounting end-to-end
# ---------------------------------------------------------------------------


def test_end_to_end_quota_at_ceiling_stops_before_any_call(env, monkeypatch):
    rec = _install_factory(monkeypatch)
    channel_id, niche_id = _seed_channel(env, connected=True)
    _seed_videos(env, channel_id, niche_id)
    env["app"].state.analytics_client = _connected_client()
    # Shared bucket AT the 90% safety ceiling (9000 / 10000 units).
    _seed_shared_bucket(env, 9000)
    client: TestClient = env["client"]

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "quota_exhausted"
    assert run["reason"] == "quota_exceeded"
    assert run["quota_units_charged"] == 0
    assert run["snapshots_written"] == 0
    # The gate stopped the sync BEFORE the first client call.
    assert rec.requests == []

    runs = _query(
        env,
        "SELECT * FROM analytics_sync_runs WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(runs) == 1 and runs[0]["status"] == "quota_exhausted"
    assert runs[0]["reason"] == "quota_exceeded"
    assert _query(env, "SELECT * FROM analytics_snapshots WHERE channel_id = ?",
                  (channel_id,)) == []
    assert _query(env, "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
                  (channel_id,)) == []

    status = client.get(f"/api/v1/channels/{channel_id}/analytics/sync-status").json()
    assert status["last_status"] == "quota_exhausted"
    assert status["last_reason"] == "quota_exceeded"
    assert status["last_synced_at"] is None

    # The shared bucket view agrees: 9000 used, nothing more charged.
    quota = client.get("/api/v1/usage/quota")
    assert quota.status_code == 200
    assert quota.json()["buckets"]["shared"]["used"] == 9000


def test_end_to_end_quota_nearly_exhausted_partial_progress_recorded(env, monkeypatch):
    rec = _install_factory(monkeypatch)
    channel_id, niche_id = _seed_channel(env, connected=True)
    _seed_videos(env, channel_id, niche_id)
    env["app"].state.analytics_client = _connected_client()
    # One unit of headroom below the ceiling: exactly ONE call lands.
    _seed_shared_bucket(env, 8999)
    client: TestClient = env["client"]

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "quota_exhausted"
    assert run["reason"] == "quota_exceeded"
    assert run["quota_units_charged"] == 1
    assert run["snapshots_written"] == 0
    assert run["retention_written"] == 0
    # Exactly the lifetime-stats call was made, then the gate closed.
    assert len(rec.requests) == 1
    assert "/youtube/v3/channels" in str(rec.requests[0].url)

    # Partial progress is durable: the lifetime figures landed, the
    # period figures (which needed the refused call) did not.
    channel_rows = _query(
        env,
        "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(channel_rows) == 1
    # Canonical shape: lifetime counters in the dedicated columns;
    # period figures live ONLY as *_period keys in metrics_json, so
    # "period did not land" means those keys are absent (and the
    # period watch_hours column stays NULL) — not a NULL period column.
    assert channel_rows[0]["subscribers"] == 54321
    assert channel_rows[0]["total_views"] == 9876543
    assert channel_rows[0]["watch_hours"] is None
    assert channel_rows[0]["qualified_watch_hours"] is None
    qmetrics = json.loads(channel_rows[0]["metrics_json"])
    assert qmetrics["total_videos"] == 187
    assert "views_period" not in qmetrics
    assert not any(key.endswith("_period") for key in qmetrics)
    assert _query(env, "SELECT * FROM analytics_snapshots WHERE channel_id = ?",
                  (channel_id,)) == []

    status = client.get(f"/api/v1/channels/{channel_id}/analytics/sync-status").json()
    assert status["last_status"] == "quota_exhausted"
    assert status["last_reason"] == "quota_exceeded"

    overview = client.get(f"/api/v1/channels/{channel_id}/analytics/overview").json()
    assert overview["sync"]["status"] == "quota_exhausted"
    assert overview["sync"]["last_reason"] == "quota_exceeded"
    assert overview["metrics"]["subscriber_count"] == 54321
    assert overview["metrics"]["period"] is None

    # Bucket accounting end-to-end: 8999 seeded + 1 charged by the sync.
    quota = client.get("/api/v1/usage/quota")
    assert quota.json()["buckets"]["shared"]["used"] == 9000


# ---------------------------------------------------------------------------
# 6. Empty-rows edge end-to-end: absence is stored, never zeroes
# ---------------------------------------------------------------------------


def test_end_to_end_empty_totals_rows_store_absence_not_zeroes(env, monkeypatch):
    _install_factory(monkeypatch, empty_totals=True)
    channel_id, niche_id = _seed_channel(env, connected=True)
    _seed_videos(env, channel_id, niche_id)
    env["app"].state.analytics_client = _connected_client()
    client: TestClient = env["client"]

    resp = client.post(f"/api/v1/channels/{channel_id}/analytics/sync")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    channel_rows = _query(
        env,
        "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
        (channel_id,),
    )
    assert len(channel_rows) == 1
    # Lifetime figures are real; the empty period response contributed
    # NOTHING — absence stays absence: the period watch_hours column
    # stays NULL (never 0) and NO *_period keys are written at all.
    assert channel_rows[0]["subscribers"] == 54321
    assert channel_rows[0]["total_views"] == 9876543
    assert channel_rows[0]["watch_hours"] is None
    assert channel_rows[0]["qualified_watch_hours"] is None
    emetrics = json.loads(channel_rows[0]["metrics_json"])
    assert emetrics["total_videos"] == 187
    assert "views_period" not in emetrics
    assert "watch_hours_period" not in emetrics
    assert "subs_delta_period" not in emetrics
    assert not any(key.endswith("_period") for key in emetrics)

    overview = client.get(f"/api/v1/channels/{channel_id}/analytics/overview").json()
    assert overview["data_state"] == "ok"
    assert overview["metrics"]["subscriber_count"] == 54321
    assert overview["metrics"]["period"] is None
