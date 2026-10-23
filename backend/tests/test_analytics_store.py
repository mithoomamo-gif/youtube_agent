"""Store tests for Round 5 analytics ingestion (app.analytics.store).

Covers:

* idempotent upsert — same (channel, video, kind, UTC day) twice
  updates the one row in place (same id / created_at, merged
  metrics, fresh updated_at), for both video snapshots and channel
  metric snapshots;
* day-boundary behaviour — captures either side of a UTC midnight
  land in different rows; ``capture_day_for`` pins the derivation;
* kind / channel separation and cross-channel isolation (a read
  scoped to another channel looks like "not found"; lists never
  leak across channels);
* the legacy discrete-column mirrors (views / watch_hours_x100 /
  ctr_bps / …) the learning loop reads, and metrics synthesis for
  legacy rows without ``metrics_json``;
* project link fill-in (set when known, never cleared), retention
  curve replace-only-when-supplied, NULL-video exemption;
* channel metrics: ``None`` means "not reported, keep the stored
  value", ``qualified_watch_hours`` is never derived from ordinary
  hours, ``latest_channel_metrics`` and ``last_synced_at``.
"""
from __future__ import annotations

import functools
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from app.analytics.schema import capture_day_for, ensure_analytics_schema
from app.analytics.store import AnalyticsStore


def _ms(year, month, day, hour=0, minute=0, second=0, millis=0) -> int:
    moment = datetime(
        year, month, day, hour, minute, second, millis * 1000,
        tzinfo=timezone.utc,
    )
    return int(moment.timestamp() * 1000)


DAY1_AM = _ms(2026, 10, 23, 9, 0, 0)
DAY1_PM = _ms(2026, 10, 23, 21, 30, 0)
DAY1_LAST_MS = _ms(2026, 10, 23, 23, 59, 59, 999)
DAY2_FIRST_MS = DAY1_LAST_MS + 1
DAY2_AM = _ms(2026, 10, 24, 8, 0, 0)


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    ensure_analytics_schema(connection)
    connection.execute("INSERT INTO channels (id) VALUES ('chan-a')")
    connection.execute("INSERT INTO channels (id) VALUES ('chan-b')")
    connection.execute("INSERT INTO projects (id) VALUES ('proj-1')")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def store(conn) -> AnalyticsStore:
    return AnalyticsStore(conn)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# capture_day derivation
# ---------------------------------------------------------------------------


def test_capture_day_for_utc_boundaries() -> None:
    assert capture_day_for(0) == "1970-01-01"
    assert capture_day_for(DAY1_LAST_MS) == "2026-10-23"
    assert capture_day_for(DAY2_FIRST_MS) == "2026-10-24"


# ---------------------------------------------------------------------------
# Video snapshots: upsert + mirrors + idempotency
# ---------------------------------------------------------------------------


def test_upsert_snapshot_creates_row_with_mirrors(store, conn) -> None:
    snap = store.upsert_snapshot(
        "chan-a",
        "vid-1",
        "d7",
        metrics={
            "views": 1000,
            "watch_minutes": 300.0,
            "likes": 10,
            "comments": 2,
            "shares": 1,
            "subs_gained": 5,
            "ctr": 0.0625,
            "avg_view_duration_sec": 95,
            "avg_view_percentage": 41.5,
        },
        retention_curve=[{"elapsed": 0.0, "ratio": 1.0},
                         {"elapsed": 0.5, "ratio": 0.6}],
        project_id="proj-1",
        captured_at_ms=DAY1_AM,
    )
    assert snap["channel_id"] == "chan-a"
    assert snap["youtube_video_id"] == "vid-1"
    assert snap["video_external_id"] == "vid-1"
    assert snap["snapshot_kind"] == "d7"
    assert snap["capture_day"] == "2026-10-23"
    assert snap["captured_at"] == DAY1_AM
    assert snap["snapshot_at"] == DAY1_AM
    assert snap["project_id"] == "proj-1"
    # Legacy discrete mirrors the learning loop reads.
    assert snap["views"] == 1000
    assert snap["watch_hours_x100"] == 500  # 300 min = 5 h
    assert snap["likes"] == 10
    assert snap["comments_count"] == 2
    assert snap["shares"] == 1
    assert snap["subs_delta"] == 5
    assert snap["ctr_bps"] == 625
    assert snap["avg_view_duration_sec"] == 95
    # Canonical metrics round-trip (plus the extra key preserved).
    assert snap["metrics"]["views"] == 1000
    assert snap["metrics"]["avg_view_percentage"] == 41.5
    assert snap["retention_curve"] == [
        {"elapsed": 0.0, "ratio": 1.0},
        {"elapsed": 0.5, "ratio": 0.6},
    ]
    assert _count(conn, "analytics_snapshots") == 1


def test_upsert_same_video_kind_day_updates_in_place(store, conn) -> None:
    first = store.upsert_snapshot(
        "chan-a", "vid-1", "d7",
        metrics={"views": 1000, "likes": 10},
        captured_at_ms=DAY1_AM,
    )
    second = store.upsert_snapshot(
        "chan-a", "vid-1", "d7",
        metrics={"views": 1200},
        captured_at_ms=DAY1_PM,
    )
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert second["captured_at"] == DAY1_PM
    assert second["views"] == 1200
    # Merge semantics: unreported keys keep their previous values.
    assert second["likes"] == 10
    assert second["metrics"]["likes"] == 10
    assert _count(conn, "analytics_snapshots") == 1


def test_upsert_day_boundary_creates_new_row(store, conn) -> None:
    store.upsert_snapshot(
        "chan-a", "vid-1", "scheduled", metrics={"views": 1},
        captured_at_ms=DAY1_LAST_MS,
    )
    store.upsert_snapshot(
        "chan-a", "vid-1", "scheduled", metrics={"views": 2},
        captured_at_ms=DAY2_FIRST_MS,
    )
    rows = store.list_snapshots("chan-a", video_external_id="vid-1")
    assert [r["capture_day"] for r in rows] == ["2026-10-23", "2026-10-24"]
    assert [r["views"] for r in rows] == [1, 2]
    assert _count(conn, "analytics_snapshots") == 2


def test_upsert_kind_and_channel_separation(store, conn) -> None:
    store.upsert_snapshot("chan-a", "vid-1", "d7", captured_at_ms=DAY1_AM)
    store.upsert_snapshot("chan-a", "vid-1", "d28", captured_at_ms=DAY1_AM)
    store.upsert_snapshot("chan-b", "vid-1", "d7", captured_at_ms=DAY1_AM)
    assert _count(conn, "analytics_snapshots") == 3
    assert len(store.list_snapshots("chan-a")) == 2
    assert len(store.list_snapshots("chan-b")) == 1
    d7 = store.list_snapshots("chan-a", snapshot_kind="d7")
    assert len(d7) == 1 and d7[0]["snapshot_kind"] == "d7"


def test_project_link_filled_when_known_never_cleared(store) -> None:
    first = store.upsert_snapshot("chan-a", "vid-1", "d3",
                                  captured_at_ms=DAY1_AM)
    assert first["project_id"] is None
    second = store.upsert_snapshot(
        "chan-a", "vid-1", "d3", project_id="proj-1",
        captured_at_ms=DAY1_PM,
    )
    assert second["project_id"] == "proj-1"
    third = store.upsert_snapshot("chan-a", "vid-1", "d3",
                                  captured_at_ms=DAY1_PM)
    assert third["project_id"] == "proj-1"


def test_retention_replaced_only_when_supplied(store) -> None:
    curve = [{"elapsed": 0.0, "ratio": 1.0}]
    store.upsert_snapshot("chan-a", "vid-1", "d7", retention_curve=curve,
                          captured_at_ms=DAY1_AM)
    kept = store.upsert_snapshot("chan-a", "vid-1", "d7",
                                 metrics={"views": 5},
                                 captured_at_ms=DAY1_PM)
    assert kept["retention_curve"] == curve
    new_curve = [{"elapsed": 0.0, "ratio": 0.9}]
    replaced = store.upsert_snapshot(
        "chan-a", "vid-1", "d7", retention_curve=new_curve,
        captured_at_ms=DAY1_PM,
    )
    assert replaced["retention_curve"] == new_curve


def test_null_video_snapshots_always_insert(store, conn) -> None:
    store.upsert_snapshot("chan-a", None, "scheduled",
                          captured_at_ms=DAY1_AM)
    store.upsert_snapshot("chan-a", None, "scheduled",
                          captured_at_ms=DAY1_PM)
    assert _count(conn, "analytics_snapshots") == 2


def test_upsert_validation(store) -> None:
    with pytest.raises(ValueError):
        store.upsert_snapshot("chan-a", "vid-1", "d1")
    with pytest.raises(ValueError):
        store.upsert_snapshot("chan-a", "vid-1", "d7",
                              retention_curve={"not": "a list"})
    with pytest.raises(ValueError):
        store.upsert_snapshot("chan-a", "vid-1", "d7",
                              youtube_video_id="vid-other")
    with pytest.raises(ValueError):
        store.upsert_channel_metrics("chan-a", snapshot_kind="d7")


def test_youtube_video_id_alias_and_watch_hours_input(store) -> None:
    snap = store.upsert_snapshot(
        "chan-a", snapshot_kind="manual",
        metrics={"watch_hours": 2.5, "ctr_bps": 500},
        youtube_video_id="vid-9", captured_at_ms=DAY1_AM,
    )
    assert snap["youtube_video_id"] == "vid-9"
    assert snap["watch_hours_x100"] == 250
    assert snap["ctr_bps"] == 500


def test_legacy_row_metrics_synthesised(store, conn) -> None:
    conn.execute(
        "INSERT INTO analytics_snapshots (id, channel_id,"
        " youtube_video_id, snapshot_at, views, watch_hours_x100,"
        " ctr_bps, created_at, snapshot_kind, capture_day,"
        " captured_at, updated_at) VALUES (?, 'chan-a', 'vid-old',"
        " ?, 700, 250, 500, ?, 'scheduled', '2026-10-23', ?, ?)",
        (uuid.uuid4().hex, DAY1_AM, DAY1_AM, DAY1_AM, DAY1_AM),
    )
    conn.commit()
    rows = store.list_snapshots("chan-a", video_external_id="vid-old")
    assert len(rows) == 1
    metrics = rows[0]["metrics"]
    assert metrics["views"] == 700
    assert metrics["watch_hours"] == 2.5
    assert metrics["watch_minutes"] == 150.0
    assert metrics["ctr"] == 0.05


def test_get_snapshot_cross_channel_is_not_found(store) -> None:
    snap = store.upsert_snapshot("chan-a", "vid-1", "d7",
                                 captured_at_ms=DAY1_AM)
    assert store.get_snapshot(snap["id"])["id"] == snap["id"]
    assert store.get_snapshot(snap["id"], channel_id="chan-a") is not None
    assert store.get_snapshot(snap["id"], channel_id="chan-b") is None
    assert store.get_snapshot("no-such-id") is None


def test_latest_snapshot(store) -> None:
    store.upsert_snapshot("chan-a", "vid-1", "d7", metrics={"views": 1},
                          captured_at_ms=DAY1_AM)
    store.upsert_snapshot("chan-a", "vid-1", "d28", metrics={"views": 2},
                          captured_at_ms=DAY2_AM)
    latest = store.latest_snapshot("chan-a", "vid-1")
    assert latest is not None and latest["snapshot_kind"] == "d28"
    assert store.latest_snapshot("chan-a", "vid-1",
                                 snapshot_kind="d7")["views"] == 1
    assert store.latest_snapshot("chan-b", "vid-1") is None


# ---------------------------------------------------------------------------
# Channel metric snapshots
# ---------------------------------------------------------------------------


def test_channel_metrics_upsert_idempotent_same_day(store, conn) -> None:
    first = store.upsert_channel_metrics(
        "chan-a", subscribers=900, total_views=50_000, watch_hours=120.5,
        captured_at_ms=DAY1_AM,
    )
    assert first["capture_day"] == "2026-10-23"
    assert first["qualified_watch_hours"] is None
    second = store.upsert_channel_metrics(
        "chan-a", subscribers=950, captured_at_ms=DAY1_PM,
    )
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["subscribers"] == 950
    # None means "not reported": the stored values survive.
    assert second["total_views"] == 50_000
    assert second["watch_hours"] == 120.5
    assert _count(conn, "channel_metric_snapshots") == 1

    store.upsert_channel_metrics("chan-a", subscribers=960,
                                 captured_at_ms=DAY2_AM)
    store.upsert_channel_metrics("chan-a", subscribers=955,
                                 snapshot_kind="manual",
                                 captured_at_ms=DAY1_PM)
    assert _count(conn, "channel_metric_snapshots") == 3


def test_channel_metrics_latest_and_isolation(store) -> None:
    store.upsert_channel_metrics("chan-a", subscribers=900,
                                 captured_at_ms=DAY1_AM)
    store.upsert_channel_metrics("chan-a", subscribers=990,
                                 qualified_watch_hours=3800.0,
                                 captured_at_ms=DAY2_AM)
    store.upsert_channel_metrics("chan-b", subscribers=10,
                                 captured_at_ms=DAY2_AM)
    latest = store.latest_channel_metrics("chan-a")
    assert latest is not None
    assert latest["subscribers"] == 990
    assert latest["qualified_watch_hours"] == 3800.0
    assert [r["subscribers"] for r in store.list_channel_metrics("chan-a")] == [
        900, 990,
    ]
    assert store.latest_channel_metrics("chan-b")["subscribers"] == 10
    assert store.list_channel_metrics("chan-missing") == []
    assert store.latest_channel_metrics("chan-missing") is None


# ---------------------------------------------------------------------------
# last_synced_at
# ---------------------------------------------------------------------------


def test_last_synced_at_across_both_tables(store) -> None:
    assert store.last_synced_at("chan-a") is None
    store.upsert_snapshot("chan-a", "vid-1", "d3", captured_at_ms=DAY1_AM)
    assert store.last_synced_at("chan-a") == DAY1_AM
    store.upsert_channel_metrics("chan-a", subscribers=1,
                                 captured_at_ms=DAY2_AM)
    assert store.last_synced_at("chan-a") == DAY2_AM
    assert store.last_synced_at("chan-b") is None


# ---------------------------------------------------------------------------
# conn_factory mode (fresh connection per call, file-backed DB)
# ---------------------------------------------------------------------------


def test_store_with_conn_factory(tmp_path) -> None:
    from app import db as db_module

    path = tmp_path / "analytics.db"
    seed = db_module.connect(path)
    try:
        seed.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
        seed.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        seed.execute("INSERT INTO channels (id) VALUES ('chan-a')")
        seed.commit()
    finally:
        seed.close()

    store = AnalyticsStore(functools.partial(db_module.connect, path))
    snap = store.upsert_snapshot(
        "chan-a", "vid-1", "d7", metrics={"views": 3},
        captured_at_ms=DAY1_AM,
    )
    again = store.upsert_snapshot(
        "chan-a", "vid-1", "d7", metrics={"views": 4},
        captured_at_ms=DAY1_PM,
    )
    assert again["id"] == snap["id"]
    assert store.list_snapshots("chan-a")[0]["views"] == 4
