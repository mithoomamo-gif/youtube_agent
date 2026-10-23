"""Publish-window suggestions (Round 5, Learning Loop).

Covers the roadmap Auto (Analytics) rule (MASTER_ROADMAP §3.10):

* eligibility thresholds are EXACT — 9 videos (even with a long
  analytics span) is not eligible; 10 videos with only 20 days of
  data is not eligible; 10 videos + exactly 28 days is eligible;
* windows are ranked by mean first-24h velocity with full evidence
  (count / mean / min / max / spread) and low-evidence flags;
* bucketing uses the CHANNEL's stored timezone
  (``channels.config_json["timezone"]``): a video published
  23:00 UTC in a UTC+5 channel buckets to 04:00 local next day;
* the service NEVER mutates schedules (schedule tables are compared
  byte-for-byte before/after) and scopes everything per channel;
* an eligible suggestion is stored as a *candidate*
  ``publish_window`` rule via the landed Learning Store — never
  activated by this module.

All fixtures are synthetic, seeded into the real section-15 schema.
"""

from __future__ import annotations

import json
import statistics
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.learning.store import LearningStore
from app.publish_window import (
    PublishWindowService,
    SqlitePublishWindowSource,
)
from app.publish_window.adapter import (
    ChannelPublishData,
    InMemoryPublishWindowSource,
    PublishedVideoVelocity,
)
from tests.execution_fixtures import make_db

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Seeding helpers (real schema)
# ---------------------------------------------------------------------------


def _seed_channel(factory, *, tz_name=None, niche_key="facts", label="A"):
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    ts = _ms(BASE)
    config = json.dumps({"timezone": tz_name}) if tz_name else None
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, risk_class,"
            " is_active, created_at, updated_at)"
            " VALUES (?, ?, ?, 'builtin', 'normal', 1, ?, ?)",
            (niche_id, niche_key, f"Niche {label}", ts, ts),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " default_niche_profile_id, language, status, config_json,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'en-US', 'active', ?, ?, ?)",
            (channel_id, f"UC-{label}-{uuid.uuid4().hex[:8]}",
             f"Channel {label}", niche_id, config, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id, niche_id


def _snapshot(conn, project_id, channel_id, yt_id, snapshot_at, views):
    conn.execute(
        "INSERT INTO analytics_snapshots (id, project_id, channel_id,"
        " youtube_video_id, snapshot_at, views, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), project_id, channel_id, yt_id,
         snapshot_at, views, snapshot_at),
    )


def _publish(factory, channel_id, niche_id, published_at_ms, velocity,
             *, extra_snapshots=()):
    """Seed one published video + its cumulative snapshots.

    ``velocity`` is the views count at publish+23h (baseline 0 at
    publish), i.e. the expected first-24h velocity.  ``velocity=None``
    gives the video NO snapshot inside its first 24h.
    ``extra_snapshots`` are (absolute_ms, views) pairs.
    Returns the YouTube video id.
    """
    project_id = str(uuid.uuid4())
    yt_id = "yt" + uuid.uuid4().hex[:11]
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
            " topic_source, status, publish_mode, total_cost_micros,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, 'topic', 'user', 'published', 'review_first',"
            " 0, ?, ?)",
            (project_id, channel_id, niche_id, published_at_ms,
             published_at_ms),
        )
        conn.execute(
            "INSERT INTO publish_records (id, project_id, youtube_video_id,"
            " privacy_status, upload_idempotency_key, published_at,"
            " created_at) VALUES (?, ?, ?, 'public', ?, ?, ?)",
            (str(uuid.uuid4()), project_id, yt_id, uuid.uuid4().hex,
             published_at_ms, published_at_ms),
        )
        _snapshot(conn, project_id, channel_id, yt_id, published_at_ms, 0)
        if velocity is None:
            _snapshot(conn, project_id, channel_id, yt_id,
                      published_at_ms + 30 * HOUR_MS, 500)
        else:
            _snapshot(conn, project_id, channel_id, yt_id,
                      published_at_ms + 23 * HOUR_MS, velocity)
        for at_ms, views in extra_snapshots:
            _snapshot(conn, project_id, channel_id, yt_id, at_ms, views)
        conn.commit()
    finally:
        conn.close()
    return yt_id


def _service(factory, store=None):
    return PublishWindowService(
        SqlitePublishWindowSource(factory), learning_store=store
    )


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    return {"factory": factory}


# ---------------------------------------------------------------------------
# Eligibility — exact thresholds
# ---------------------------------------------------------------------------


def test_nine_videos_not_eligible_even_with_long_span(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)
    for i in range(9):  # 9 videos across 32 days — span is plenty
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=4 * i, hours=12)), 400)
    store = LearningStore(factory)
    result = _service(factory, store).suggest(channel_id)

    assert result["eligible"] is False
    assert result["reason"] == "not_enough_data"
    assert result["published_count"] == 9
    assert result["days_of_data"] >= 28
    assert result["required"] == {"videos": 10, "days": 28}
    assert result["suggestions"] == []
    assert result["learning_rule"] is None
    # Nothing was stored for an ineligible channel.
    assert store.list_rule_history(channel_id, "facts") == []


def test_ten_videos_but_only_twenty_days_not_eligible(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)
    first_publish = _ms(BASE + timedelta(hours=12))
    yt_ids = [
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=2 * i, hours=12)), 400)
        for i in range(10)
    ]
    # Extend the analytics span to EXACTLY 20 days from the first
    # snapshot (a late cumulative snapshot for the first video).
    _publish_extra = [(first_publish + 20 * DAY_MS, 9999)]
    conn = factory()
    try:
        row = conn.execute(
            "SELECT project_id FROM publish_records WHERE youtube_video_id = ?",
            (yt_ids[0],),
        ).fetchone()
        _snapshot(conn, row[0], channel_id, yt_ids[0],
                  _publish_extra[0][0], _publish_extra[0][1])
        conn.commit()
    finally:
        conn.close()

    result = _service(factory).suggest(channel_id)
    assert result["published_count"] == 10
    assert result["days_of_data"] == 20
    assert result["eligible"] is False
    assert result["reason"] == "not_enough_data"
    assert result["suggestions"] == []


def test_exact_thresholds_ten_videos_28_days_eligible(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)
    first_publish = _ms(BASE + timedelta(hours=12))
    yt_ids = [
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=2 * i, hours=12)), 400)
        for i in range(10)
    ]
    conn = factory()
    try:
        row = conn.execute(
            "SELECT project_id FROM publish_records WHERE youtube_video_id = ?",
            (yt_ids[0],),
        ).fetchone()
        _snapshot(conn, row[0], channel_id, yt_ids[0],
                  first_publish + 28 * DAY_MS, 9999)
        conn.commit()
    finally:
        conn.close()

    result = _service(factory).suggest(channel_id)
    assert result["published_count"] == 10
    assert result["days_of_data"] == 28
    assert result["eligible"] is True
    assert result["reason"] is None
    assert result["suggestions"], "eligible channel must get windows"


# ---------------------------------------------------------------------------
# Ranking + evidence
# ---------------------------------------------------------------------------


def _ranking_channel(factory):
    channel_id, niche_id = _seed_channel(factory)  # no tz -> UTC fallback
    ids = {"h18": [], "h9": [], "h3": []}
    for i, day in enumerate([0, 6, 12, 18, 24, 30]):
        ids["h18"].append(_publish(
            factory, channel_id, niche_id,
            _ms(BASE + timedelta(days=day, hours=18)), 1000))
    for day in [3, 9, 15, 21, 27, 33]:
        ids["h9"].append(_publish(
            factory, channel_id, niche_id,
            _ms(BASE + timedelta(days=day, hours=9)), 100))
    ids["h3"].append(_publish(
        factory, channel_id, niche_id,
        _ms(BASE + timedelta(days=15, hours=3)), 5000))
    return channel_id, ids


def test_window_ranking_known_best_hours_and_low_evidence_flag(env):
    channel_id, ids = _ranking_channel(env["factory"])
    result = _service(env["factory"]).suggest(channel_id)

    assert result["eligible"] is True
    assert result["published_count"] == 13
    top, second, third = result["suggestions"]
    # A single very lucky video tops the MEAN ranking — but is
    # flagged low-evidence rather than silently trusted.
    assert top["start_hour"] == 3 and top["video_count"] == 1
    assert top["mean_velocity"] == 5000.0 and top["low_evidence"] is True
    assert top["video_ids"] == ids["h3"]
    # The established best hour: 6 videos, mean 1000, no spread.
    assert second["start_hour"] == 18 and second["rank"] == 2
    assert second["video_count"] == 6 and second["mean_velocity"] == 1000.0
    assert second["min_velocity"] == 1000.0 and second["max_velocity"] == 1000.0
    assert second["spread"] == 0.0 and second["low_evidence"] is False
    assert set(second["video_ids"]) == set(ids["h18"])
    assert third["start_hour"] == 9 and third["mean_velocity"] == 100.0
    assert len(result["hour_evidence"]) == 3
    # Secondary view: weekdays, ranked, with evidence.
    weekdays = result["weekday_windows"]
    assert weekdays == sorted(
        weekdays, key=lambda w: -w["mean_velocity"])
    assert all(w["video_count"] >= 1 for w in weekdays)


def test_spread_min_max_mean_reported(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)
    for day, velocity in [(0, 100), (10, 200), (20, 300)]:
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=day, hours=18)), velocity)
    for day in [2, 5, 8, 12, 15, 22, 29]:
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=day, hours=10)), 50)
    result = _service(factory).suggest(channel_id)
    top = result["suggestions"][0]
    assert top["start_hour"] == 18 and top["video_count"] == 3
    assert top["mean_velocity"] == 200.0
    assert top["min_velocity"] == 100.0 and top["max_velocity"] == 300.0
    assert top["spread"] == pytest.approx(
        round(statistics.pstdev([100, 200, 300]), 3))
    assert top["low_evidence"] is False  # exactly at the minimum of 3


# ---------------------------------------------------------------------------
# Timezone correctness
# ---------------------------------------------------------------------------


def test_timezone_bucketing_utc_plus5_channel(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory, tz_name="Asia/Karachi")
    # Mondays 23:00 UTC for 10 weeks -> Tuesdays 04:00 local (UTC+5).
    for week in range(10):
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(weeks=week, hours=23)), 800)
    result = _service(factory).suggest(channel_id)

    assert result["eligible"] is True
    assert result["timezone"] == "Asia/Karachi"
    assert result["timezone_source"] == "channel_config"
    top = result["suggestions"][0]
    assert top["start_hour"] == 4 and top["video_count"] == 10
    assert all(w["start_hour"] != 23 for w in result["hour_evidence"])
    top_weekday = result["weekday_windows"][0]
    assert top_weekday["weekday"] == 1  # Tuesday locally, not Monday
    assert top_weekday["weekday_name"] == "Tuesday"


def test_same_publish_times_bucket_to_23_in_utc_channel(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory, tz_name="UTC")
    for week in range(10):
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(weeks=week, hours=23)), 800)
    result = _service(factory).suggest(channel_id)
    assert result["suggestions"][0]["start_hour"] == 23
    assert result["weekday_windows"][0]["weekday"] == 0  # Monday in UTC


def test_missing_channel_timezone_falls_back_to_utc_and_says_so(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)  # no config_json tz
    for i in range(10):
        _publish(factory, channel_id, niche_id,
                 _ms(BASE + timedelta(days=4 * i, hours=12)), 100)
    result = _service(factory).suggest(channel_id)
    assert result["eligible"] is True
    assert result["timezone"] == "UTC"
    assert result["timezone_source"] == "fallback_utc"


# ---------------------------------------------------------------------------
# No schedule mutation + Learning Store persistence
# ---------------------------------------------------------------------------


def _seed_schedule(factory, channel_id, niche_id):
    ts = _ms(BASE)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id,"
            " topic_mode, publish_mode, is_enabled, created_at, updated_at)"
            " VALUES ('sched-1', 'Morning', ?, ?, 'auto', 'review_first',"
            " 1, ?, ?)",
            (channel_id, niche_id, ts, ts),
        )
        conn.execute(
            "INSERT INTO schedule_runs (id, schedule_id, status, created_at)"
            " VALUES ('run-1', 'sched-1', 'created', ?)",
            (ts,),
        )
        conn.commit()
    finally:
        conn.close()


def _table_rows(factory, table):
    conn = factory()
    try:
        return [tuple(r) for r in conn.execute(
            f"SELECT * FROM {table} ORDER BY id")]
    finally:
        conn.close()


def test_no_schedule_mutation_and_rule_stored_as_candidate(env):
    factory = env["factory"]
    channel_id, ids = _ranking_channel(factory)
    # _ranking_channel seeded its own channel/niche; find the niche id.
    conn = factory()
    try:
        niche_id = conn.execute(
            "SELECT default_niche_profile_id FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    _seed_schedule(factory, channel_id, niche_id)

    watched = ["schedules", "schedule_runs", "publish_records",
               "analytics_snapshots", "projects", "channels"]
    before = {t: _table_rows(factory, t) for t in watched}

    store = LearningStore(factory)
    result = _service(factory, store).suggest(channel_id)

    assert result["eligible"] is True
    for table in watched:  # byte-for-byte: suggestions mutate nothing
        assert _table_rows(factory, table) == before[table], table

    rule = result["learning_rule"]
    assert rule is not None and rule["kind"] == "publish_window"
    assert rule["status"] == "candidate"  # never auto-activated
    assert rule["channel_id"] == channel_id and rule["niche"] == "facts"
    assert rule["statement_family"] == "publish_window"
    history = store.list_rule_history(channel_id, "facts",
                                      kind="publish_window")
    assert [r["id"] for r in history] == [rule["id"]]
    assert store.list_active_rules(channel_id, "facts") == []


def test_learning_rule_evidence_matches_top_window(env):
    factory = env["factory"]
    channel_id, ids = _ranking_channel(factory)
    store = LearningStore(factory)
    result = _service(factory, store).suggest(channel_id)
    top = result["suggestions"][0]
    rule = result["learning_rule"]
    assert rule["evidence_count"] == top["video_count"] == 1
    assert rule["effect_value"] == top["mean_velocity"] == 5000.0
    assert rule["effect_unit"] == "mean_views_first_24h"
    assert rule["created_from"] == top["video_ids"] == ids["h3"]
    assert rule["is_thin_evidence"] is True  # store-side label, n < 5


def test_persist_false_and_missing_store_return_data_only(env):
    factory = env["factory"]
    channel_id, _ids = _ranking_channel(factory)
    store = LearningStore(factory)

    result = _service(factory, store).suggest(channel_id, persist=False)
    assert result["eligible"] is True and result["suggestions"]
    assert result["learning_rule"] is None
    assert store.list_rule_history(channel_id, "facts") == []

    result2 = _service(factory).suggest(channel_id)  # no store at all
    assert result2["learning_rule"] is None
    assert "no_learning_store_configured" in result2["learning_rule_note"]
    assert result2["suggestions"]


# ---------------------------------------------------------------------------
# Scope isolation per channel
# ---------------------------------------------------------------------------


def test_scope_isolation_per_channel(env):
    factory = env["factory"]
    chan_a, niche_a = _seed_channel(factory, label="A")
    a_ids = [
        _publish(factory, chan_a, niche_a,
                 _ms(BASE + timedelta(days=4 * i, hours=18)), 900)
        for i in range(10)
    ]
    chan_b, niche_b = _seed_channel(factory, niche_key="horror", label="B")
    for i in range(3):
        _publish(factory, chan_b, niche_b,
                 _ms(BASE + timedelta(days=4 * i, hours=18)), 900)

    store = LearningStore(factory)
    service = _service(factory, store)
    result_a = service.suggest(chan_a)
    result_b = service.suggest(chan_b)

    assert result_a["eligible"] is True
    a_evidence_ids = {
        vid for w in result_a["hour_evidence"] for vid in w["video_ids"]}
    assert a_evidence_ids == set(a_ids)

    assert result_b["eligible"] is False
    assert result_b["published_count"] == 3
    assert not (set(a_ids) & set(json.dumps(result_b)))
    # The stored rule belongs to A only; B's scope stays empty.
    assert len(store.list_rule_history(chan_a, "facts")) == 1
    assert store.list_rule_history(chan_b, "horror") == []
    assert store.list_rule_history(chan_b, "facts") == []


# ---------------------------------------------------------------------------
# Adapter: velocity derivation from cumulative snapshots
# ---------------------------------------------------------------------------


def test_velocity_derivation_uses_latest_snapshot_within_24h(env):
    factory = env["factory"]
    channel_id, niche_id = _seed_channel(factory)
    published = _ms(BASE + timedelta(hours=12))
    yt_id = _publish(factory, channel_id, niche_id, published, None)
    # Replace the default snapshots with a hand-built cumulative series.
    conn = factory()
    try:
        conn.execute(
            "DELETE FROM analytics_snapshots WHERE youtube_video_id = ?",
            (yt_id,))
        project_id = conn.execute(
            "SELECT project_id FROM publish_records"
            " WHERE youtube_video_id = ?", (yt_id,)).fetchone()[0]
        for offset_h, views in [(0, 0), (2, 100), (20, 350), (30, 900)]:
            _snapshot(conn, project_id, channel_id, yt_id,
                      published + offset_h * HOUR_MS, views)
        conn.commit()
    finally:
        conn.close()
    yt_no24 = _publish(factory, channel_id, niche_id,
                       published + 3 * DAY_MS, None)

    data = SqlitePublishWindowSource(factory).fetch_channel(channel_id)
    by_id = {v.video_id: v for v in data.videos}
    assert by_id[yt_id].first_24h_velocity == 350.0  # not 900 (that's +30h)
    assert by_id[yt_no24].first_24h_velocity is None  # no snapshot <= 24h
    assert data.exists is True


def test_unknown_channel_is_honest(env):
    factory = env["factory"]
    data = SqlitePublishWindowSource(factory).fetch_channel("nope")
    assert data.exists is False and data.videos == ()
    result = _service(factory).suggest("nope")
    assert result["eligible"] is False
    assert result["reason"] == "channel_not_found"
    assert result["published_count"] == 0 and result["days_of_data"] == 0
    assert result["required"] == {"videos": 10, "days": 28}


# ---------------------------------------------------------------------------
# Seam: in-memory synthetic source
# ---------------------------------------------------------------------------


def test_inmemory_synthetic_source_ranking():
    videos = tuple(
        PublishedVideoVelocity(
            video_id=f"syn-{i}",
            project_id=None,
            published_at_ms=_ms(BASE + timedelta(days=3 * i, hours=14)),
            first_24h_velocity=700.0,
        )
        for i in range(10)
    ) + tuple(
        PublishedVideoVelocity(
            video_id=f"syn-low-{i}",
            project_id=None,
            published_at_ms=_ms(BASE + timedelta(days=3 * i, hours=6)),
            first_24h_velocity=50.0,
        )
        for i in range(4)
    )
    data = ChannelPublishData(
        channel_id="synthetic-channel",
        exists=True,
        timezone_name="UTC",
        timezone_source="channel_config",
        niche="facts",
        videos=videos,
        analytics_first_ms=_ms(BASE),
        analytics_last_ms=_ms(BASE) + 40 * DAY_MS,
    )
    service = PublishWindowService(InMemoryPublishWindowSource(
        {"synthetic-channel": data}))
    result = service.suggest("synthetic-channel")
    assert result["eligible"] is True and result["days_of_data"] == 40
    assert result["suggestions"][0]["start_hour"] == 14
    assert result["suggestions"][0]["video_count"] == 10
    assert result["videos_with_velocity"] == 14
    assert result["videos_without_velocity"] == 0
