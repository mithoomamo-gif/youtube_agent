"""Analytics READ API — API-level tests (Round 5, analytics ingestion).

Seeds the sibling storage layer directly (:class:`AnalyticsStore`
upserts + raw rows for ``analytics_sync_state`` / publish records)
and exercises the read endpoints from
:mod:`app.routers.analytics` / :mod:`app.analytics_read` over real
HTTP:

* overview freshness (``last_synced_at`` + sync status on every
  response) and YPP derivation — subscribers vs 1,000 and QUALIFIED
  watch hours vs 4,000, never estimated from ordinary watch time;
* per-video median-baseline math (median of the channel's last-10
  other videos' same-kind latest snapshots; deltas absolute + pct),
  including the <2-baseline-videos case -> ``insufficient_baseline``;
* snapshot listing across kinds with ``captured_at`` provenance;
* honest empty states: ``not_connected`` / ``no_data`` shapes at
  channel and per-video level — never zeros dressed as data (a real
  stored 0 IS returned, with provenance);
* cross-channel isolation: another channel's video id answers 404,
  lists never contain the other channel's videos.

The backend DB is shared across the test session, so every id here
carries a per-module unique suffix.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CH_MAIN = f"an-main-{_SUFFIX}"
YT_MAIN = f"UCanMain{_SUFFIX}"
CH_NOTCONN = f"an-notconn-{_SUFFIX}"
CH_NODATA = f"an-nodata-{_SUFFIX}"
CH_REAUTH = f"an-reauth-{_SUFFIX}"
CH_NOQUAL = f"an-noqual-{_SUFFIX}"
CH_B = f"an-b-{_SUFFIX}"
YT_B = f"UCanB{_SUFFIX}xx"
NICHE = f"an-niche-{_SUFFIX}"

DAY_MS = 86_400_000
# Fixed capture instants (epoch ms) — distinct UTC days per snapshot.
T_OLD = 1_760_000_000_000
T_CHAN_1 = T_OLD + DAY_MS
T_CHAN_2 = T_OLD + 2 * DAY_MS
T_SYNC = T_CHAN_2 + 3_600_000  # sync-state last success (after captures)


def _insert_channel(conn, chan_id, yt_id, *, config=None, credential_id=None,
                    status="active"):
    import json

    now = int(time.time() * 1000)
    if credential_id is not None:
        # channels.credential_id is an FK to provider_credentials.
        conn.execute(
            "INSERT OR IGNORE INTO provider_credentials (id, provider,"
            " credential_kind, keyring_ref, account_label, scopes,"
            " status, last_validated_at, created_at, updated_at)"
            " VALUES (?, 'youtube', 'oauth', ?, ?, NULL, 'active', ?,"
            " ?, ?)",
            (credential_id, f"youtube:channel:{yt_id}", yt_id, now, now, now),
        )
    conn.execute(
        "INSERT OR IGNORE INTO channels (id, youtube_channel_id, title,"
        " credential_id, language, status, config_json, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, 'en', ?, ?, ?, ?)",
        (
            chan_id,
            yt_id,
            f"Analytics {chan_id}",
            credential_id,
            status,
            json.dumps(config) if config is not None else None,
            now,
            now,
        ),
    )


def _seed_channels() -> None:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label, source,"
            " created_at, updated_at) VALUES (?, ?, 'Analytics Niche',"
            " 'builtin', 1, 1)",
            (NICHE, NICHE),
        )
        connected = {"connect_status": "connected",
                     "credential_status": "connected"}
        _insert_channel(conn, CH_MAIN, YT_MAIN, config=connected,
                        credential_id=f"cred-{_SUFFIX}")
        _insert_channel(conn, CH_NODATA, f"UCanNoData{_SUFFIX}",
                        config=connected, credential_id=f"cred-nd-{_SUFFIX}")
        _insert_channel(conn, CH_NOTCONN, f"UCanNotConn{_SUFFIX}")
        _insert_channel(
            conn, CH_REAUTH, f"UCanReauth{_SUFFIX}",
            config={"connect_status": "needs_reauth",
                    "credential_status": "needs_reauth"},
            credential_id=f"cred-ra-{_SUFFIX}",
        )
        _insert_channel(conn, CH_NOQUAL, f"UCanNoQual{_SUFFIX}",
                        config=connected, credential_id=f"cred-nq-{_SUFFIX}")
        _insert_channel(conn, CH_B, YT_B, config=connected,
                        credential_id=f"cred-b-{_SUFFIX}")
    finally:
        conn.close()


def _store():
    from app import db as db_module
    from app.analytics.store import AnalyticsStore

    return AnalyticsStore(db_module.get_connection)


def _seed_main_channel_data() -> None:
    """Channel metrics (two days; latest wins) + sync state."""
    store = _store()
    store.upsert_channel_metrics(
        CH_MAIN, subscribers=800, total_views=100_000, watch_hours=4_000.0,
        qualified_watch_hours=2_000.0, captured_at_ms=T_CHAN_1,
    )
    store.upsert_channel_metrics(
        CH_MAIN, subscribers=950, total_views=123_456, watch_hours=5_000.0,
        qualified_watch_hours=2_500.5, captured_at_ms=T_CHAN_2,
        # Settled storage shape (post R5-ANALYTICS-001): the sync
        # records period figures in metrics_json under *_period keys.
        metrics={
            "views_period": 3_210,
            "watch_hours_period": 210.5,
            "subs_delta_period": 150,
            "subscribers_gained_period": 170,
            "subscribers_lost_period": 20,
            "likes_period": 400,
            "comments_period": 55,
            "shares_period": 12,
            "period_start_date": "2025-08-01",
            "period_end_date": "2025-08-28",
        },
    )
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        # The sync team's freshness surface — created lazily here the
        # same way app.analytics_sync.schema would create it.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS analytics_sync_state ("
            " channel_id TEXT PRIMARY KEY, last_synced_at INTEGER NULL,"
            " last_attempt_at INTEGER NULL, last_status TEXT NULL,"
            " last_reason TEXT NULL, last_run_id TEXT NULL,"
            " last_snapshots_written INTEGER NOT NULL DEFAULT 0,"
            " updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO analytics_sync_state (channel_id,"
            " last_synced_at, last_attempt_at, last_status, last_reason,"
            " last_run_id, last_snapshots_written, updated_at)"
            " VALUES (?, ?, ?, 'partial', 'retention_unavailable',"
            " 'run-an-1', 7, ?)",
            (CH_MAIN, T_SYNC, T_SYNC, T_SYNC),
        )
    finally:
        conn.close()


def _seed_noqual_channel_data() -> None:
    """Connected + synced, but NO qualification figure was reported:
    ordinary watch hours (9,999) must never become YPP progress."""
    _store().upsert_channel_metrics(
        CH_NOQUAL, subscribers=1_200, total_views=50_000,
        watch_hours=9_999.0, qualified_watch_hours=None,
        captured_at_ms=T_CHAN_1,
    )


def _publish(conn, project_id, video_id, title, published_at):
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, channel_id,"
        " niche_profile_id, topic, topic_source, created_at, updated_at)"
        " VALUES (?, ?, ?, 'topic', 'user', ?, ?)",
        (project_id, CH_MAIN, NICHE, now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO publish_records (id, project_id,"
        " youtube_video_id, title, privacy_status,"
        " upload_idempotency_key, published_at, created_at)"
        " VALUES (?, ?, ?, ?, 'public', ?, ?, ?)",
        (f"pub-{project_id}", project_id, video_id, title,
         f"idem-{project_id}", published_at, now),
    )


def _seed_videos() -> None:
    """Five published videos on CH_MAIN.  d7 views: V1=100, V2=200,
    V3=300, V4=400 (=> median baseline for any one of them is the
    median of the other three); V5 has NO snapshots (per-item
    no_data).  V1 also carries d3 + d28 snapshots for the listing
    test, and a real stored 0 (likes) to prove zeros-with-provenance
    are returned as data."""
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        base_pub = T_OLD
        for idx, vid in enumerate(["V1", "V2", "V3", "V4", "V5"]):
            _publish(conn, f"proj-{vid}-{_SUFFIX}", f"{vid}-{_SUFFIX}",
                     f"Video {vid}", base_pub + idx * DAY_MS)
        # A video on channel B — must never leak into CH_MAIN reads.
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, channel_id,"
            " niche_profile_id, topic, topic_source, created_at,"
            " updated_at) VALUES (?, ?, ?, 'topic', 'user', ?, ?)",
            (f"proj-b-{_SUFFIX}", CH_B, NICHE, base_pub, base_pub),
        )
        conn.execute(
            "INSERT OR IGNORE INTO publish_records (id, project_id,"
            " youtube_video_id, title, privacy_status,"
            " upload_idempotency_key, published_at, created_at)"
            " VALUES (?, ?, ?, 'B video', 'public', ?, ?, ?)",
            (f"pub-b-{_SUFFIX}", f"proj-b-{_SUFFIX}", f"VB-{_SUFFIX}",
             f"idem-b-{_SUFFIX}", base_pub, base_pub),
        )
    finally:
        conn.close()

    store = _store()
    views = {"V1": 100, "V2": 200, "V3": 300, "V4": 400}
    for idx, (vid, v) in enumerate(views.items()):
        store.upsert_snapshot(
            CH_MAIN, video_external_id=f"{vid}-{_SUFFIX}",
            snapshot_kind="d7",
            metrics={
                "views": v,
                "watch_minutes": float(v),  # watch_hours = v/60
                "likes": 0 if vid == "V1" else v // 10,
                "comments": v // 100,
                "shares": 1,
                "ctr": 0.05,
                "avg_view_duration_sec": 60,
                "subs_gained": 2,
            },
            captured_at_ms=T_OLD + 10 * DAY_MS + idx * 1_000,
        )
    # V1 across kinds for the listing endpoint.
    store.upsert_snapshot(
        CH_MAIN, video_external_id=f"V1-{_SUFFIX}", snapshot_kind="d3",
        metrics={"views": 40, "watch_minutes": 40.0},
        captured_at_ms=T_OLD + 3 * DAY_MS,
    )
    store.upsert_snapshot(
        CH_MAIN, video_external_id=f"V1-{_SUFFIX}", snapshot_kind="d28",
        metrics={"views": 160, "watch_minutes": 170.0},
        captured_at_ms=T_OLD + 28 * DAY_MS,
    )
    # Channel B's own snapshot (isolation).
    store.upsert_snapshot(
        CH_B, video_external_id=f"VB-{_SUFFIX}", snapshot_kind="d7",
        metrics={"views": 999_999, "watch_minutes": 9_999.0},
        captured_at_ms=T_OLD + 10 * DAY_MS,
    )


@pytest.fixture(autouse=True)
def _seeded(app):
    _seed_channels()
    _seed_main_channel_data()
    _seed_noqual_channel_data()
    _seed_videos()
    yield


def _get(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Overview: freshness + YPP derivation
# ---------------------------------------------------------------------------


def test_overview_latest_metrics_freshness_and_sync_status(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/overview")
    assert body["channel_id"] == CH_MAIN
    assert body["youtube_channel_id"] == YT_MAIN
    assert body["data_state"] == "ok"
    # Freshness stamp on the response: the sync state's last
    # successful sync wins over raw capture instants.
    assert body["last_synced_at"] == T_SYNC
    assert body["sync"]["status"] == "partial"
    assert body["sync"]["last_reason"] == "retention_unavailable"
    assert body["sync"]["last_attempt_at"] == T_SYNC
    assert body["sync"]["last_run_id"] == "run-an-1"
    assert body["sync"]["last_snapshots_written"] == 7
    assert body["sync"]["source"] == "analytics_sync_state"
    # Latest channel snapshot (day 2), not the older one.
    metrics = body["metrics"]
    assert metrics["subscriber_count"] == 950
    assert metrics["total_views"] == 123_456
    assert metrics["ordinary_watch_hours"] == 5_000.0
    assert metrics["qualified_watch_hours"] == 2_500.5
    assert metrics["captured_at"] == T_CHAN_2
    assert metrics["derived_from_snapshot"] is True
    # Period block populated from the metrics_json *_period keys of
    # the store-shaped write (regression: R5-ANALYTICS-001 follow-up
    # — the period branch previously only read the legacy columns).
    period = metrics["period"]
    assert period is not None
    assert period["views"] == 3_210
    assert period["watch_hours"] == 210.5
    assert period["subs_delta"] == 150
    assert period["subscribers_gained"] == 170
    assert period["subscribers_lost"] == 20
    assert period["likes"] == 400
    assert period["comments_count"] == 55
    assert period["shares"] == 12
    assert period["period_start_date"] == "2025-08-01"
    assert period["period_end_date"] == "2025-08-28"


def test_overview_ypp_progress_derived_from_snapshot(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/overview")
    ypp = body["ypp_progress"]
    assert ypp["derived_from_snapshot"] is True
    assert ypp["captured_at"] == T_CHAN_2
    subs = ypp["subscribers"]
    assert subs == {
        "current": 950, "required": 1000, "remaining": 50,
        "met": False, "pct": 95.0,
    }
    watch = ypp["watch_hours"]
    assert watch["current_qualified"] == 2_500.5
    assert watch["required"] == 4000.0
    assert watch["remaining"] == 1_499.5
    assert watch["met"] is False
    assert watch["pct"] == 62.51
    assert watch["qualification_data_available"] is True
    # Ordinary hours are reported separately — and are HIGHER than
    # the qualified figure here, so any estimation would be visible.
    assert watch["ordinary_watch_hours"] == 5_000.0


def test_overview_ypp_never_estimated_from_ordinary_hours(client):
    body = _get(client, f"/api/v1/channels/{CH_NOQUAL}/analytics/overview")
    assert body["data_state"] == "ok"
    assert body["metrics"]["ordinary_watch_hours"] == 9_999.0
    assert body["metrics"]["qualified_watch_hours"] is None
    ypp = body["ypp_progress"]
    assert ypp["subscribers"]["current"] == 1_200
    assert ypp["subscribers"]["met"] is True
    watch = ypp["watch_hours"]
    assert watch["qualification_data_available"] is False
    assert watch["current_qualified"] is None
    assert watch["remaining"] is None
    assert watch["met"] is None
    assert watch["pct"] is None


def test_ypp_progress_flat_alias_shape(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/ypp-progress")
    assert body["subscriber_count"] == 950
    assert body["qualified_watch_hours"] == 2_500.5
    assert body["qualification_data_available"] is True
    assert body["ordinary_watch_hours"] == 5_000.0
    assert body["as_of"] == T_CHAN_2
    assert body["last_synced_at"] == T_SYNC
    assert body["ypp_progress"]["subscribers"]["current"] == 950


def test_overview_resolves_youtube_channel_id(client):
    body = _get(client, f"/api/v1/channels/{YT_MAIN}/analytics/overview")
    assert body["channel_id"] == CH_MAIN
    assert body["data_state"] == "ok"


# ---------------------------------------------------------------------------
# Honest empty states
# ---------------------------------------------------------------------------


def test_overview_not_connected(client):
    body = _get(client, f"/api/v1/channels/{CH_NOTCONN}/analytics/overview")
    assert body["data_state"] == "not_connected"
    assert body["metrics"] is None
    assert body["ypp_progress"] is None
    assert body["last_synced_at"] is None
    assert body["sync"]["status"] == "never_synced"


def test_overview_needs_reauth_is_not_connected(client):
    body = _get(client, f"/api/v1/channels/{CH_REAUTH}/analytics/overview")
    assert body["data_state"] == "not_connected"
    assert body["metrics"] is None


def test_overview_connected_never_synced_is_no_data(client):
    body = _get(client, f"/api/v1/channels/{CH_NODATA}/analytics/overview")
    assert body["data_state"] == "no_data"
    assert body["metrics"] is None
    assert body["ypp_progress"] is None
    assert body["last_synced_at"] is None
    assert body["sync"]["status"] == "never_synced"


def test_overview_unknown_channel_404(client):
    resp = client.get(f"/api/v1/channels/nope-{_SUFFIX}/analytics/overview")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "channel_not_found"


def test_videos_not_connected_and_no_data_shapes(client):
    body = _get(client, f"/api/v1/channels/{CH_NOTCONN}/analytics/videos")
    assert body["data_state"] == "not_connected"
    assert body["items"] == []
    assert body["last_synced_at"] is None

    body = _get(client, f"/api/v1/channels/{CH_NODATA}/analytics/videos")
    assert body["data_state"] == "no_data"
    assert body["items"] == []
    assert body["last_synced_at"] is None


# ---------------------------------------------------------------------------
# Per-video performance + median baseline
# ---------------------------------------------------------------------------


def test_videos_newest_first_and_median_baseline_math(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/videos")
    assert body["data_state"] == "ok"
    assert body["last_synced_at"] == T_SYNC
    ids = [item["youtube_video_id"] for item in body["items"]]
    assert ids == [f"V{i}-{_SUFFIX}" for i in (5, 4, 3, 2, 1)]

    by_id = {item["youtube_video_id"]: item for item in body["items"]}
    v1 = by_id[f"V1-{_SUFFIX}"]
    assert v1["data_state"] == "ok"
    assert v1["title"] == "Video V1"
    latest = v1["latest_snapshot"]
    # V1's newest capture overall is its d28 snapshot.
    assert latest["snapshot_kind"] == "d28"
    assert latest["metrics"]["views"] == 160

    # Baseline math is same-kind: use V2..V4 whose latest is d7, and
    # check V3 (views 300) against the median of {100, 200, 400}.
    v3 = by_id[f"V3-{_SUFFIX}"]
    comp = v3["comparison"]
    assert comp is not None
    assert v3["comparison_unavailable_reason"] is None
    assert comp["baseline_kind"] == "d7"
    assert comp["baseline_video_count"] == 3
    assert comp["baseline_median"]["views"] == 200
    delta = comp["deltas"]["views"]
    assert delta == {"value": 300, "baseline": 200,
                     "absolute": 100, "pct": 50.0}
    # watch_hours: values are views/60 -> median 200/60, delta 100/60.
    wh = comp["deltas"]["watch_hours"]
    assert wh["baseline"] == pytest.approx(200 / 60)
    assert wh["absolute"] == pytest.approx(100 / 60)
    assert wh["pct"] == pytest.approx(50.0)


def test_videos_real_zero_is_data_not_absence(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/videos")
    by_id = {item["youtube_video_id"]: item for item in body["items"]}
    # V1's d7 likes were stored as a real 0.  Its LATEST snapshot is
    # d28 (no likes reported -> synthesised metrics carry the legacy
    # column default); check the d7 snapshot through the listing
    # endpoint instead: the 0 must survive as data.
    listing = _get(
        client,
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/V1-{_SUFFIX}"
        "/snapshots?kind=d7",
    )
    assert listing["data_state"] == "ok"
    assert len(listing["items"]) == 1
    snap = listing["items"][0]
    assert snap["metrics"]["likes"] == 0
    assert snap["metrics"]["views"] == 100
    assert snap["captured_at"] is not None
    assert by_id[f"V1-{_SUFFIX}"]["data_state"] == "ok"


def test_videos_item_without_snapshots_is_no_data(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/videos")
    by_id = {item["youtube_video_id"]: item for item in body["items"]}
    v5 = by_id[f"V5-{_SUFFIX}"]
    assert v5["data_state"] == "no_data"
    assert v5["latest_snapshot"] is None
    assert v5["comparison"] is None
    assert v5["comparison_unavailable_reason"] == "no_data"


def test_videos_insufficient_baseline_single_video_channel(client):
    # Channel B has exactly ONE video with snapshots: no baseline
    # pool exists, so the comparison must be null with the typed
    # reason — never a comparison against itself or a fake median.
    body = _get(client, f"/api/v1/channels/{CH_B}/analytics/videos")
    assert body["data_state"] == "ok"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["youtube_video_id"] == f"VB-{_SUFFIX}"
    assert item["latest_snapshot"]["metrics"]["views"] == 999_999
    assert item["comparison"] is None
    assert item["comparison_unavailable_reason"] == "insufficient_baseline"


# ---------------------------------------------------------------------------
# Snapshot listing
# ---------------------------------------------------------------------------


def test_snapshot_listing_across_kinds(client):
    body = _get(
        client,
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/V1-{_SUFFIX}"
        "/snapshots",
    )
    assert body["data_state"] == "ok"
    assert body["youtube_video_id"] == f"V1-{_SUFFIX}"
    assert body["last_synced_at"] == T_SYNC
    kinds = [item["snapshot_kind"] for item in body["items"]]
    assert kinds == ["d28", "d7", "d3"]  # newest capture first
    assert set(body["by_kind"]) == {"d3", "d7", "d28"}
    d3 = body["by_kind"]["d3"][0]
    assert d3["metrics"]["views"] == 40
    assert d3["captured_at"] == T_OLD + 3 * DAY_MS
    for item in body["items"]:
        assert item["captured_at"] is not None


def test_snapshot_listing_kind_filter_and_invalid_kind(client):
    body = _get(
        client,
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/V1-{_SUFFIX}"
        "/snapshots?kind=d3",
    )
    assert [i["snapshot_kind"] for i in body["items"]] == ["d3"]

    resp = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/V1-{_SUFFIX}"
        "/snapshots?kind=d45"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_snapshot_kind"


def test_snapshot_listing_video_without_snapshots(client):
    body = _get(
        client,
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/V5-{_SUFFIX}"
        "/snapshots",
    )
    assert body["data_state"] == "no_data"
    assert body["items"] == []
    assert body["by_kind"] == {}


# ---------------------------------------------------------------------------
# Dual-shape channel metrics (sync team's table shape)
# ---------------------------------------------------------------------------


def test_channel_metrics_sync_shape_normalisation():
    """The sync sibling's ``channel_metric_snapshots`` shape
    (``subscriber_count`` / ``snapshot_at`` / period
    ``watch_hours_x100``, no qualified column at all) must normalise
    honestly: lifetime figures map across, the period watch hours
    stay labelled as a period figure, and YPP qualification reports
    unavailable — never estimated."""
    import sqlite3

    from app import analytics_read

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE channel_metric_snapshots ("
        " id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,"
        " snapshot_at INTEGER NOT NULL, snapshot_day TEXT NOT NULL,"
        " subscriber_count INTEGER NULL, total_views INTEGER NULL,"
        " total_videos INTEGER NULL, views_period INTEGER NULL,"
        " watch_hours_x100 INTEGER NULL, subs_delta_period INTEGER NULL,"
        " likes_period INTEGER NULL, comments_period INTEGER NULL,"
        " shares_period INTEGER NULL,"
        " source TEXT NOT NULL DEFAULT 'youtube_analytics_api',"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
        " UNIQUE (channel_id, snapshot_day))"
    )
    conn.execute(
        "INSERT INTO channel_metric_snapshots (id, channel_id,"
        " snapshot_at, snapshot_day, subscriber_count, total_views,"
        " total_videos, views_period, watch_hours_x100,"
        " subs_delta_period, created_at, updated_at)"
        " VALUES ('row-1', 'chan-x', ?, '2025-01-01', 777, 4242, 12,"
        " 1000, 12345, 5, ?, ?)",
        (T_CHAN_1, T_CHAN_1, T_CHAN_1),
    )
    metrics = analytics_read.latest_channel_metrics(conn, "chan-x")
    assert metrics is not None
    assert metrics["subscriber_count"] == 777
    assert metrics["total_views"] == 4242
    assert metrics["total_videos"] == 12
    assert metrics["captured_at"] == T_CHAN_1
    assert metrics["ordinary_watch_hours"] is None
    assert metrics["qualified_watch_hours"] is None
    assert metrics["period"]["views"] == 1000
    assert metrics["period"]["watch_hours"] == pytest.approx(123.45)

    ypp = analytics_read.ypp_progress(metrics)
    assert ypp["subscribers"]["current"] == 777
    assert ypp["watch_hours"]["qualification_data_available"] is False
    assert ypp["watch_hours"]["current_qualified"] is None
    assert ypp["watch_hours"]["pct"] is None


def test_channel_metrics_storage_shape_period_from_metrics_json():
    """Storage-shaped row whose period watch time arrives as
    ``watch_minutes_period`` only: the period block converts it to
    hours, and a row with no *_period keys keeps ``period`` None."""
    import sqlite3

    from app import analytics_read

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE channel_metric_snapshots ("
        " id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,"
        " snapshot_kind TEXT NOT NULL DEFAULT 'scheduled',"
        " captured_at INTEGER NOT NULL, capture_day TEXT NOT NULL,"
        " subscribers INTEGER NULL, total_views INTEGER NULL,"
        " watch_hours REAL NULL, qualified_watch_hours REAL NULL,"
        " metrics_json TEXT NULL, created_at INTEGER NOT NULL,"
        " updated_at INTEGER NOT NULL)"
    )
    import json

    conn.execute(
        "INSERT INTO channel_metric_snapshots (id, channel_id,"
        " snapshot_kind, captured_at, capture_day, subscribers,"
        " total_views, watch_hours, qualified_watch_hours,"
        " metrics_json, created_at, updated_at) VALUES ('r1', 'cx',"
        " 'scheduled', ?, '2025-08-28', 950, 123456, 5000.0, 2500.5,"
        " ?, ?, ?)",
        (
            T_CHAN_2,
            json.dumps({"views_period": 10, "watch_minutes_period": 120.0}),
            T_CHAN_2,
            T_CHAN_2,
        ),
    )
    metrics = analytics_read.latest_channel_metrics(conn, "cx")
    assert metrics["period"]["views"] == 10
    assert metrics["period"]["watch_hours"] == pytest.approx(2.0)
    assert metrics["ordinary_watch_hours"] == 5_000.0

    conn.execute(
        "UPDATE channel_metric_snapshots SET metrics_json = '{}'"
    )
    metrics = analytics_read.latest_channel_metrics(conn, "cx")
    assert metrics["period"] is None


# ---------------------------------------------------------------------------
# Cross-channel isolation (404, never data)
# ---------------------------------------------------------------------------


def test_cross_channel_video_snapshots_404(client):
    # Channel B's video addressed under CH_MAIN: 404, never data.
    resp = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/VB-{_SUFFIX}"
        "/snapshots"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "video_not_found"
    # And the reverse direction.
    resp = client.get(
        f"/api/v1/channels/{CH_B}/analytics/videos/V1-{_SUFFIX}/snapshots"
    )
    assert resp.status_code == 404


def test_cross_channel_lists_never_leak(client):
    body = _get(client, f"/api/v1/channels/{CH_MAIN}/analytics/videos")
    ids = {item["youtube_video_id"] for item in body["items"]}
    assert f"VB-{_SUFFIX}" not in ids
    # Channel B's overview carries none of CH_MAIN's figures.
    body_b = _get(client, f"/api/v1/channels/{CH_B}/analytics/overview")
    assert body_b["data_state"] == "no_data"  # B has no channel metrics
    assert body_b["metrics"] is None
    assert body_b["last_synced_at"] is not None  # B DID sync video data
