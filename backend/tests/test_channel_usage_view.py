"""Per-channel budget/usage view tests (Round 4, Worker 3).

Covers app.execution.channel_context.build_channel_usage_views and the
GET /api/v1/usage/channels[/{id}] router surface (called directly with
a monkeypatched connection, following the usage router's _conn seam).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from app.execution.channel_context import (
    build_channel_usage_view,
    build_channel_usage_views,
    month_window_ms,
)
from tests.execution_fixtures import make_db, now_ms


def _insert(conn_factory, sql, params):
    conn = conn_factory()
    try:
        conn.execute(sql, params)
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


@pytest.fixture()
def factory(tmp_path):
    _, conn_factory = make_db(tmp_path)
    return conn_factory


def _seed_channel(factory, *, monthly=None, daily_upload_cap=None):
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    ts = now_ms()
    suffix = uuid.uuid4().hex[:8]
    _insert(factory,
            "INSERT INTO niche_profiles (id, key, label, source, risk_class,"
            " is_active, created_at, updated_at) VALUES (?, ?, 'N',"
            " 'builtin', 'normal', 1, ?, ?)",
            (niche_id, f"n-{suffix}", ts, ts))
    _insert(factory,
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " default_niche_profile_id, language, status, daily_upload_cap,"
            " monthly_budget_micros, created_at, updated_at) VALUES"
            " (?, ?, ?, ?, 'en-US', 'active', ?, ?, ?, ?)",
            (channel_id, f"UC-{suffix}", f"Ch {suffix}", niche_id,
             daily_upload_cap, monthly, ts, ts))
    return channel_id, niche_id


def _seed_project(factory, channel_id, niche_id, *, status="published",
                  total_cost=0, created_at=None):
    project_id = str(uuid.uuid4())
    ts = created_at if created_at is not None else now_ms()
    _insert(factory,
            "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
            " topic_source, status, publish_mode, total_cost_micros,"
            " created_at, updated_at) VALUES (?, ?, ?, 'T', 'user', ?,"
            " 'review_first', ?, ?, ?)",
            (project_id, channel_id, niche_id, status, total_cost, ts, ts))
    return project_id


def _seed_job(factory, project_id):
    job_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(factory,
            "INSERT INTO jobs (id, project_id, agent_key, kind, status,"
            " created_at, updated_at) VALUES (?, ?, 'stage.research',"
            " 'stage.research', 'done', ?, ?)",
            (job_id, project_id, ts, ts))
    return job_id


def _seed_run(factory, project_id, *, cost, created_at=None):
    job_id = _seed_job(factory, project_id)
    run_id = str(uuid.uuid4())
    ts = created_at if created_at is not None else now_ms()
    _insert(factory,
            "INSERT INTO agent_runs (id, job_id, project_id, agent_key,"
            " status, cost_micros, created_at) VALUES (?, ?, ?,"
            " 'stage.research', 'succeeded', ?, ?)",
            (run_id, job_id, project_id, cost, ts))
    return run_id


def _seed_usage(factory, project_id, *, cost, created_at=None):
    ts = created_at if created_at is not None else now_ms()
    _insert(factory,
            "INSERT INTO api_usage (id, provider, operation, project_id,"
            " cost_micros, status, created_at) VALUES (?, 'gemini',"
            " 'generate', ?, ?, 'ok', ?)",
            (str(uuid.uuid4()), project_id, cost, ts))


def _seed_schedule(factory, channel_id, niche_id, *, daily_cap):
    schedule_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(factory,
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id,"
            " publish_mode, daily_cost_cap_micros, is_enabled, created_at,"
            " updated_at) VALUES (?, 'S', ?, ?, 'auto', ?, 1, ?, ?)",
            (schedule_id, channel_id, niche_id, daily_cap, ts, ts))
    return schedule_id


def test_view_reports_caps_spend_and_counts(factory):
    start_ms, _ = month_window_ms()
    channel_id, niche_id = _seed_channel(
        factory, monthly=1_000_000, daily_upload_cap=3)
    _seed_schedule(factory, channel_id, niche_id, daily_cap=50_000)
    p1 = _seed_project(factory, channel_id, niche_id,
                       status="published", total_cost=70_000)
    p2 = _seed_project(factory, channel_id, niche_id,
                       status="in_production", total_cost=30_000)
    _seed_run(factory, p1, cost=40_000)
    _seed_run(factory, p1, cost=30_000)
    _seed_run(factory, p2, cost=30_000)
    # Last month's run must NOT count toward month-to-date.
    _seed_run(factory, p1, cost=999_000, created_at=start_ms - 86_400_000)
    _seed_usage(factory, p1, cost=12_000)
    _seed_usage(factory, p2, cost=3_000)

    conn = factory()
    try:
        view = build_channel_usage_view(conn, channel_id)
    finally:
        conn.close()
    assert view is not None
    assert view["channel_id"] == channel_id
    assert view["monthly_budget_micros"] == 1_000_000
    assert view["monthly_budget_usd"] == 1.0
    assert view["month_to_date_spend_micros"] == 100_000
    assert view["month_to_date_api_usage_micros"] == 15_000
    assert view["monthly_budget_remaining_micros"] == 900_000
    assert view["projects_total_cost_micros"] == 100_000
    assert view["project_count"] == 2
    assert view["project_counts_by_status"] == {
        "published": 1, "in_production": 1}
    assert view["daily_upload_cap"] == 3
    assert view["schedule_daily_caps_micros"] == [50_000]
    assert view["schedules"][0]["daily_cost_cap_micros"] == 50_000


def test_view_honest_zeros_and_unset_caps(factory):
    channel_id, _ = _seed_channel(factory, monthly=None)
    conn = factory()
    try:
        view = build_channel_usage_view(conn, channel_id)
        views = build_channel_usage_views(conn)
    finally:
        conn.close()
    assert view["month_to_date_spend_micros"] == 0
    assert view["month_to_date_api_usage_micros"] == 0
    assert view["monthly_budget_micros"] is None
    assert view["monthly_budget_remaining_micros"] is None
    assert view["project_count"] == 0
    assert view["project_counts_by_status"] == {}
    assert view["schedules"] == []
    assert [v["channel_id"] for v in views] == [channel_id]


def test_view_unknown_channel_returns_none(factory):
    conn = factory()
    try:
        assert build_channel_usage_view(conn, "missing") is None
    finally:
        conn.close()


def test_view_resolves_by_youtube_channel_id(factory):
    channel_id, _ = _seed_channel(factory, monthly=42)
    conn = factory()
    try:
        row = conn.execute(
            "SELECT youtube_channel_id FROM channels WHERE id = ?",
            (channel_id,)).fetchone()
        view = build_channel_usage_view(conn, row[0])
    finally:
        conn.close()
    assert view["channel_id"] == channel_id


def test_usage_router_channels_endpoints(factory, monkeypatch):
    from app.routers import usage as usage_router

    channel_id, niche_id = _seed_channel(factory, monthly=500_000)
    project_id = _seed_project(factory, channel_id, niche_id,
                               status="ready", total_cost=5_000)
    _seed_run(factory, project_id, cost=5_000)

    monkeypatch.setattr(usage_router, "_conn", lambda: factory())

    payload = asyncio.run(usage_router.get_usage_channels())
    assert payload["next_cursor"] is None
    items = payload["items"]
    assert len(items) == 1
    assert items[0]["channel_id"] == channel_id
    assert items[0]["month_to_date_spend_micros"] == 5_000

    single = asyncio.run(usage_router.get_usage_channel(channel_id))
    assert single["channel_id"] == channel_id

    from app.errors import ApiError
    with pytest.raises(ApiError) as excinfo:
        asyncio.run(usage_router.get_usage_channel("no-such-channel"))
    assert excinfo.value.status_code == 404
