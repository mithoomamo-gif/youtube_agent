"""Tests for the YouTube Analytics client (Agent 29 transport layer).

ALL HTTP is mocked with httpx.MockTransport — no live network. Every
payload below is a FIXTURE shaped like a REAL API response:

* Analytics fixtures reproduce the real ``youtubeAnalytics#resultTable``
  shape (``kind`` / ``columnHeaders`` with name+columnType+dataType /
  ``rows`` as positional arrays), using the real wire metric names
  (``estimatedMinutesWatched``, ``comments``, ...) — see
  https://developers.google.com/youtube/analytics/reference/reports/query
* the Data API fixture reproduces ``youtube#channelListResponse`` with
  string-valued ``statistics`` counters, as ``channels.list`` returns.

Covered contract points:
- credential gate: no token manager / channel not connected ->
  ``not_connected`` typed result and ZERO HTTP (factory never invoked),
  for every public method;
- successful parse of channel totals, per-video metrics (single +
  batch, incl. requested-order preservation and missing-row honesty),
  lifetime stats, the generic report table, and the retention curve;
- CTR is always None with the documented unavailable reason, and the
  legacy card/annotation click-rate metrics are never requested;
- quota: 1 unit per query (shared bucket via classify_method), result
  carries quota_units, injected ledger records success AND failure,
  pre-flight ceiling refusal makes zero HTTP;
- auth/quota/other error mapping to typed failures, single attempt,
  no exception leaks; malformed / non-JSON bodies; invalid date range
  makes zero HTTP.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass

import httpx
import pytest

from app.providers.youtube import analytics
from app.providers.youtube.analytics import (
    CTR_UNAVAILABLE_REASON,
    METHOD_ANALYTICS_QUERY,
    METHOD_CHANNELS_LIST,
    QUOTA_COSTS,
    YouTubeAnalyticsClient,
)
from app.providers.youtube.quota import (
    BUCKET_SHARED,
    InMemoryQuotaLedger,
    LedgerEntry,
    classify_method,
    pacific_day,
)

CHANNEL_ID = "UC1234567890123456789012"  # UC + 22 chars (fixture)
VIDEO_A = "vidA1234567"  # 11 chars (fixture)
VIDEO_B = "vidB1234567"
START, END = "2026-10-01", "2026-10-21"
ACCESS_TOKEN = "fake-access-token-1234567890"

# --- FIXTURE: real Analytics resultTable for channel totals -------------
TOTALS_FIXTURE = {
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [
        {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "estimatedMinutesWatched", "columnType": "METRIC", "dataType": "FLOAT"},
        {"name": "averageViewDuration", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "averageViewPercentage", "columnType": "METRIC", "dataType": "FLOAT"},
        {"name": "likes", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "comments", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "shares", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "subscribersGained", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "subscribersLost", "columnType": "METRIC", "dataType": "INTEGER"},
    ],
    "rows": [[45210, 198234.5, 263, 54.25, 3120, 486, 210, 890, 145]],
}

# --- FIXTURE: real Analytics resultTable, dimensions=video --------------
VIDEOS_FIXTURE = {
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [
        {"name": "video", "columnType": "DIMENSION", "dataType": "STRING"},
        {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "estimatedMinutesWatched", "columnType": "METRIC", "dataType": "FLOAT"},
        {"name": "averageViewDuration", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "averageViewPercentage", "columnType": "METRIC", "dataType": "FLOAT"},
        {"name": "likes", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "comments", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "shares", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "subscribersGained", "columnType": "METRIC", "dataType": "INTEGER"},
    ],
    "rows": [
        [VIDEO_A, 15234, 81230.0, 320, 61.5, 1102, 203, 87, 340],
        [VIDEO_B, 4210, 15980.5, 228, 47.0, 260, 41, 12, 58],
    ],
}

# --- FIXTURE: real Analytics resultTable for audience retention ---------
# (report type audience-retention: elapsedVideoTimeRatio x audienceWatchRatio)
RETENTION_FIXTURE = {
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [
        {"name": "elapsedVideoTimeRatio", "columnType": "DIMENSION", "dataType": "FLOAT"},
        {"name": "audienceWatchRatio", "columnType": "METRIC", "dataType": "FLOAT"},
    ],
    "rows": [[0.0, 1.0], [0.25, 0.71], [0.05, 0.88], [0.5, 0.55], [1.0, 0.31]],
}

# --- FIXTURE: real Data API channels.list response -----------------------
CHANNELS_LIST_FIXTURE = {
    "kind": "youtube#channelListResponse",
    "items": [
        {
            "id": CHANNEL_ID,
            "statistics": {
                "viewCount": "9876543",
                "subscriberCount": "54321",
                "hiddenSubscriberCount": False,
                "videoCount": "187",
            },
        }
    ],
}


def run(coro):
    return asyncio.run(coro)


@dataclass
class _Status:
    status: str


class FakeTokenManager:
    """Duck-compatible TokenManager: get_status + get_access_token."""

    def __init__(self, *, connected: bool = True, token: str | None = ACCESS_TOKEN):
        self._connected = connected
        self._token = token
        self.token_calls: list[str] = []

    def get_status(self, channel_id: str) -> _Status:
        return _Status("connected" if self._connected else "not_connected")

    async def get_access_token(self, channel_id: str) -> str:
        self.token_calls.append(channel_id)
        if not self._connected or self._token is None:
            raise RuntimeError("not connected (fixture)")
        return self._token


class RecordingFactory:
    """Replacement for ``analytics.create_async_client`` (MockTransport)."""

    def __init__(self, handler):
        self._handler = handler
        self.factory_calls: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, **kwargs):
        self.factory_calls.append(kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        transport = kwargs.get("transport") or httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport)

    def params_of(self, index: int = 0) -> dict:
        return dict(urllib.parse.parse_qsl(self.requests[index].url.query.decode()))


def analytics_handler(request: httpx.Request) -> httpx.Response:
    params = dict(urllib.parse.parse_qsl(request.url.query.decode()))
    metrics = params.get("metrics", "")
    if params.get("dimensions") == "elapsedVideoTimeRatio":
        return httpx.Response(200, json=RETENTION_FIXTURE)
    if params.get("dimensions") == "video":
        return httpx.Response(200, json=VIDEOS_FIXTURE)
    if "views" in metrics:
        return httpx.Response(200, json=TOTALS_FIXTURE)
    return httpx.Response(200, json={"kind": "youtubeAnalytics#resultTable",
                                     "columnHeaders": [], "rows": []})


def ok_handler(request: httpx.Request) -> httpx.Response:
    if "youtubeanalytics.googleapis.com" in str(request.url):
        return analytics_handler(request)
    if request.url.path.endswith("/channels"):
        return httpx.Response(200, json=CHANNELS_LIST_FIXTURE)
    return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


@pytest.fixture()
def factory(monkeypatch):
    rec = RecordingFactory(ok_handler)
    monkeypatch.setattr(analytics, "create_async_client", rec)
    return rec


def install_handler(monkeypatch, handler) -> RecordingFactory:
    rec = RecordingFactory(handler)
    monkeypatch.setattr(analytics, "create_async_client", rec)
    return rec


def connected_client(**kwargs) -> YouTubeAnalyticsClient:
    kwargs.setdefault("token_manager", FakeTokenManager())
    return YouTubeAnalyticsClient(**kwargs)


ALL_CALLS = [
    lambda c: c.get_channel_totals(channel_id=CHANNEL_ID, start_date=START, end_date=END),
    lambda c: c.get_video_metrics(channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END),
    lambda c: c.get_videos_metrics(channel_id=CHANNEL_ID, video_ids=[VIDEO_A], start_date=START, end_date=END),
    lambda c: c.get_retention_curve(channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END),
    lambda c: c.get_channel_lifetime_stats(channel_id=CHANNEL_ID),
    lambda c: c.query_report(channel_id=CHANNEL_ID, start_date=START, end_date=END, metrics="views"),
]


# ---------------------------------------------------------------------------
# Credential gate — not_connected, ZERO HTTP
# ---------------------------------------------------------------------------
def test_fixture_no_token_manager_not_connected_zero_http(factory):
    client = YouTubeAnalyticsClient()  # no token manager at all
    for call in ALL_CALLS:
        result = run(call(client))
        assert result.status == "not_connected"
        assert result.reason == "no_youtube_credential"
        assert result.data is None
        assert result.quota_units == 0
    assert factory.factory_calls == []
    assert factory.requests == []


def test_fixture_channel_not_connected_zero_http(factory):
    client = YouTubeAnalyticsClient(token_manager=FakeTokenManager(connected=False))
    for call in ALL_CALLS:
        result = run(call(client))
        assert result.status == "not_connected"
        assert result.reason == "oauth_token_unavailable"
        assert result.data is None
    assert factory.factory_calls == []
    assert factory.requests == []


def test_fixture_token_mint_failure_is_not_connected_zero_http(factory):
    manager = FakeTokenManager(connected=True, token=None)  # status ok, mint raises
    client = YouTubeAnalyticsClient(token_manager=manager)
    result = run(
        client.get_channel_totals(channel_id=CHANNEL_ID, start_date=START, end_date=END)
    )
    assert result.status == "not_connected"
    assert result.reason == "oauth_token_unavailable"
    assert factory.factory_calls == []
    assert factory.requests == []


# ---------------------------------------------------------------------------
# Successful parses (fixtures shaped like real Analytics responses)
# ---------------------------------------------------------------------------
def test_fixture_channel_totals_parse(factory):
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "ok"
    assert result.quota_units == 1
    assert result.quota_method == METHOD_ANALYTICS_QUERY
    totals = result.data
    assert totals is not None and totals.has_data is True
    assert totals.channel_id == CHANNEL_ID
    assert totals.views == 45210
    assert totals.watch_time_minutes == 198234.5  # estimatedMinutesWatched
    assert totals.average_view_duration_seconds == 263
    assert totals.average_view_percentage == 54.25
    assert totals.likes == 3120
    assert totals.comment_count == 486  # wire metric "comments"
    assert totals.shares == 210
    assert totals.subscribers_gained == 890
    assert totals.subscribers_lost == 145
    assert totals.net_subscribers == 745

    assert len(factory.requests) == 1
    request = factory.requests[0]
    assert str(request.url).startswith("https://youtubeanalytics.googleapis.com/v2/reports")
    assert request.headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}"
    params = factory.params_of(0)
    assert params["ids"] == f"channel=={CHANNEL_ID}"
    assert params["startDate"] == START and params["endDate"] == END
    assert "estimatedMinutesWatched" in params["metrics"]
    # Legacy card/annotation click rates are NEVER requested as "CTR".
    assert "cardClickRate" not in params["metrics"]
    assert "annotationClickRate" not in params["metrics"]


def test_fixture_video_metrics_parse_and_ctr_unavailable(factory):
    result = run(
        connected_client().get_video_metrics(
            channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END
        )
    )
    assert result.status == "ok" and result.quota_units == 1
    m = result.data
    assert m is not None and m.has_data is True
    assert m.video_id == VIDEO_A
    assert m.views == 15234
    assert m.watch_time_minutes == 81230.0
    assert m.average_view_duration_seconds == 320
    assert m.average_view_percentage == 61.5
    assert m.likes == 1102
    assert m.comment_count == 203
    assert m.shares == 87
    assert m.subscribers_gained == 340
    # CTR decision: never invented for a plain reports query.
    assert m.ctr is None
    assert m.ctr_unavailable_reason == CTR_UNAVAILABLE_REASON
    params = factory.params_of(0)
    assert params["dimensions"] == "video"
    assert params["filters"] == f"video=={VIDEO_A}"


def test_fixture_videos_metrics_batch_order_and_missing_row(factory):
    missing = "vidC1234567"
    result = run(
        connected_client().get_videos_metrics(
            channel_id=CHANNEL_ID, video_ids=[VIDEO_B, VIDEO_A, missing],
            start_date=START, end_date=END,
        )
    )
    assert result.status == "ok" and result.quota_units == 1  # ONE query for all
    assert len(factory.requests) == 1
    assert [m.video_id for m in result.data] == [VIDEO_B, VIDEO_A, missing]
    assert result.data[0].views == 4210 and result.data[0].has_data is True
    assert result.data[1].views == 15234
    # Missing row: honest absence, not zeros.
    assert result.data[2].has_data is False
    assert result.data[2].views is None and result.data[2].likes is None


def test_fixture_video_with_no_data_is_ok_not_fabricated(factory):
    result = run(
        connected_client().get_video_metrics(
            channel_id=CHANNEL_ID, video_id="vidZ1234567",
            start_date=START, end_date=END,
        )
    )
    assert result.status == "ok"
    assert result.data is not None and result.data.has_data is False
    assert result.data.views is None and result.data.watch_time_minutes is None


def test_fixture_retention_curve_parse(factory):
    result = run(
        connected_client().get_retention_curve(
            channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END
        )
    )
    assert result.status == "ok" and result.quota_units == 1
    curve = result.data
    assert curve is not None and curve.video_id == VIDEO_A
    # Points parsed as (elapsed_ratio, watch_ratio) and sorted ascending
    # even though the fixture row order is not sorted.
    assert [(p.elapsed_ratio, p.watch_ratio) for p in curve.points] == [
        (0.0, 1.0), (0.05, 0.88), (0.25, 0.71), (0.5, 0.55), (1.0, 0.31),
    ]
    params = factory.params_of(0)
    assert params["metrics"] == "audienceWatchRatio"
    assert params["dimensions"] == "elapsedVideoTimeRatio"
    assert params["filters"] == f"video=={VIDEO_A}"


def test_fixture_channel_lifetime_stats_via_data_api(factory):
    result = run(connected_client().get_channel_lifetime_stats(channel_id=CHANNEL_ID))
    assert result.status == "ok"
    assert result.quota_units == 1
    assert result.quota_method == METHOD_CHANNELS_LIST
    stats = result.data
    assert stats is not None
    assert stats.view_count == 9876543  # string counter parsed to int
    assert stats.subscriber_count == 54321
    assert stats.video_count == 187
    assert stats.hidden_subscriber_count is False
    params = factory.params_of(0)
    assert params["part"] == "statistics" and params["id"] == CHANNEL_ID


def test_fixture_query_report_generic_table(factory):
    result = run(
        connected_client().query_report(
            channel_id=CHANNEL_ID, start_date=START, end_date=END,
            metrics="views,estimatedMinutesWatched",
        )
    )
    assert result.status == "ok"
    table = result.data
    assert table is not None
    assert [c.name for c in table.columns] == [
        "views", "estimatedMinutesWatched", "averageViewDuration",
        "averageViewPercentage", "likes", "comments", "shares",
        "subscribersGained", "subscribersLost",
    ]
    assert table.rows[0]["views"] == 45210


def test_fixture_empty_totals_rows_means_no_data_not_zeros(monkeypatch):
    empty = {"kind": "youtubeAnalytics#resultTable",
             "columnHeaders": TOTALS_FIXTURE["columnHeaders"], "rows": []}
    install_handler(monkeypatch, lambda request: httpx.Response(200, json=empty))
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "ok"
    assert result.data is not None and result.data.has_data is False
    assert result.data.views is None


# ---------------------------------------------------------------------------
# Quota accounting seam
# ---------------------------------------------------------------------------
def test_quota_costs_mirror_shared_bucket_classification():
    # The documented model: Analytics query == Data API read == 1 unit,
    # shared bucket — locked against the app's own classify_method.
    assert QUOTA_COSTS[METHOD_ANALYTICS_QUERY] == 1
    assert QUOTA_COSTS[METHOD_CHANNELS_LIST] == 1
    assert classify_method(METHOD_ANALYTICS_QUERY) == (BUCKET_SHARED, 1)
    assert classify_method(METHOD_CHANNELS_LIST) == (BUCKET_SHARED, 1)


def test_quota_ledger_records_success(factory):
    ledger = InMemoryQuotaLedger()
    client = connected_client(quota_ledger=ledger)
    result = run(
        client.get_channel_totals(channel_id=CHANNEL_ID, start_date=START, end_date=END)
    )
    assert result.status == "ok"
    entries = ledger.entries_for(pacific_day())
    assert len(entries) == 1
    assert entries[0].bucket == BUCKET_SHARED
    assert entries[0].method == METHOD_ANALYTICS_QUERY
    assert entries[0].units == 1 and entries[0].success is True
    assert entries[0].channel_id == CHANNEL_ID


def test_quota_preflight_refusal_is_zero_http(factory):
    ledger = InMemoryQuotaLedger()
    ledger.record(LedgerEntry(bucket=BUCKET_SHARED, method="videos.list",
                              units=9000, day_pt=pacific_day(), success=True))
    client = connected_client(quota_ledger=ledger)
    result = run(
        client.get_channel_totals(channel_id=CHANNEL_ID, start_date=START, end_date=END)
    )
    assert result.status == "failed"
    assert result.reason == "quota_exceeded"
    assert result.quota_units == 0  # refused before charging
    assert factory.factory_calls == [] and factory.requests == []


# ---------------------------------------------------------------------------
# Error mapping — typed failures, single attempt, no exception leaks
# ---------------------------------------------------------------------------
def test_fixture_quota_403_maps_to_quota_exceeded_and_ledgers(monkeypatch):
    body = {"error": {"code": 403, "errors": [
        {"reason": "quotaExceeded", "domain": "youtube.quota"}]}}
    rec = install_handler(monkeypatch, lambda request: httpx.Response(403, json=body))
    ledger = InMemoryQuotaLedger()
    result = run(
        connected_client(quota_ledger=ledger).get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "failed"
    assert result.reason == "quota_exceeded"
    assert result.data is None
    assert result.quota_units == 1  # Google charges failed calls too
    assert len(rec.requests) == 1  # single attempt
    entries = ledger.entries_for(pacific_day())
    assert len(entries) == 1 and entries[0].success is False


def test_fixture_429_maps_to_quota_exceeded(monkeypatch):
    install_handler(monkeypatch, lambda request: httpx.Response(429, json={
        "error": {"errors": [{"reason": "rateLimitExceeded"}]}}))
    result = run(
        connected_client().get_retention_curve(
            channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "quota_exceeded"


def test_fixture_auth_401_maps_to_authentication_failed(monkeypatch):
    install_handler(monkeypatch, lambda request: httpx.Response(401, json={
        "error": {"errors": [{"reason": "authError"}]}}))
    result = run(
        connected_client().get_video_metrics(
            channel_id=CHANNEL_ID, video_id=VIDEO_A, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "authentication_failed"
    assert result.data is None


def test_fixture_insufficient_scope_403_maps_to_reason_slug(monkeypatch):
    install_handler(monkeypatch, lambda request: httpx.Response(403, json={
        "error": {"errors": [{"reason": "insufficientPermissions"}]}}))
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "insufficient_permissions"


def test_fixture_network_error_no_exception_leak(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused (fixture)", request=request)

    rec = install_handler(monkeypatch, boom)
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "network_error"
    assert len(rec.requests) == 1


def test_fixture_malformed_result_table_no_exception(monkeypatch):
    # 200 but not a resultTable: no columnHeaders at all.
    install_handler(monkeypatch, lambda request: httpx.Response(200, json={"rows": []}))
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "malformed_response"


def test_fixture_non_json_body_no_exception(monkeypatch):
    install_handler(monkeypatch, lambda request: httpx.Response(
        200, content=b"<html>not json</html>",
        headers={"content-type": "text/html"}))
    result = run(
        connected_client().get_channel_totals(
            channel_id=CHANNEL_ID, start_date=START, end_date=END
        )
    )
    assert result.status == "failed" and result.reason == "invalid_response_json"


def test_fixture_lifetime_stats_channel_not_found(monkeypatch):
    install_handler(monkeypatch, lambda request: httpx.Response(
        200, json={"kind": "youtube#channelListResponse", "items": []}))
    result = run(connected_client().get_channel_lifetime_stats(channel_id=CHANNEL_ID))
    assert result.status == "failed" and result.reason == "not_found"


def test_invalid_date_range_zero_http(factory):
    client = connected_client()
    for start, end in [(END, START), ("2026-13-01", END), ("not-a-date", END)]:
        result = run(
            client.get_channel_totals(channel_id=CHANNEL_ID, start_date=start, end_date=end)
        )
        assert result.status == "failed" and result.reason == "invalid_date_range"
        assert result.data is None and result.quota_units == 0
    assert factory.factory_calls == [] and factory.requests == []
