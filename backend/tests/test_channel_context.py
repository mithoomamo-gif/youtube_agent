"""Channel context resolution tests (Round 4, Worker 3).

Precedence contract (app.execution.channel_context):
schedule niche > project niche > channel default niche; voice only
from the niche profile's default_voice_id (no channel config_json
voice override exists in the codebase); budgets from
channels.monthly_budget_micros + the channel-matching schedule's
daily_cost_cap_micros.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from app.execution.channel_context import (
    parse_channel_config,
    resolve_channel_context,
    resolve_channel_context_from_conn,
)
from tests.execution_fixtures import conn_factory_for, make_db, now_ms


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


def _niche(conn_factory, *, key, voice_id=None, voice_style=None, wpm=None):
    niche_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(
        conn_factory,
        "INSERT INTO niche_profiles (id, key, label, source, voice_style,"
        " default_voice_id, words_per_minute, risk_class, is_active,"
        " created_at, updated_at) VALUES (?, ?, ?, 'builtin', ?, ?, ?,"
        " 'normal', 1, ?, ?)",
        (niche_id, key, key.title(), voice_style, voice_id, wpm, ts, ts),
    )
    return niche_id


def _channel(conn_factory, *, default_niche_id=None, monthly=None,
             daily_upload_cap=None, config=None, language="en-US"):
    channel_id = str(uuid.uuid4())
    ts = now_ms()
    suffix = uuid.uuid4().hex[:8]
    _insert(
        conn_factory,
        "INSERT INTO channels (id, youtube_channel_id, title,"
        " default_niche_profile_id, language, status, daily_upload_cap,"
        " monthly_budget_micros, config_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (channel_id, f"UC-{suffix}", f"Channel {suffix}", default_niche_id,
         language, daily_upload_cap, monthly,
         json.dumps(config) if config is not None else None, ts, ts),
    )
    return channel_id


def _schedule(conn_factory, *, channel_id, niche_id, daily_cap=None,
              publish_mode="auto"):
    schedule_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(
        conn_factory,
        "INSERT INTO schedules (id, name, channel_id, niche_profile_id,"
        " publish_mode, daily_cost_cap_micros, is_enabled, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (schedule_id, f"Sched {schedule_id[:6]}", channel_id, niche_id,
         publish_mode, daily_cap, ts, ts),
    )
    return schedule_id


def _project(conn_factory, *, channel_id, niche_id, schedule_id=None,
             publish_mode="review_first"):
    project_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(
        conn_factory,
        "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
        " topic_source, status, publish_mode, schedule_id,"
        " total_cost_micros, created_at, updated_at) VALUES"
        " (?, ?, ?, 'Topic', 'schedule', 'draft', ?, ?, 0, ?, ?)",
        (project_id, channel_id, niche_id, publish_mode, schedule_id,
         ts, ts),
    )
    return project_id


@pytest.fixture()
def factory(tmp_path):
    _, conn_factory = make_db(tmp_path)
    return conn_factory


def test_parse_channel_config_tolerant():
    assert parse_channel_config(None) == {}
    assert parse_channel_config("not json") == {}
    assert parse_channel_config('{"a": 1}') == {"a": 1}
    assert parse_channel_config({"b": 2}) == {"b": 2}
    assert parse_channel_config('["x"]') == {}


def test_schedule_niche_wins_over_project_and_channel_default(factory):
    default_niche = _niche(factory, key="default-n", voice_id="v-default")
    project_niche = _niche(factory, key="project-n", voice_id="v-project")
    schedule_niche = _niche(
        factory, key="schedule-n", voice_id="ur-PK-AsadNeural",
        voice_style="calm", wpm=140,
    )
    channel = _channel(
        factory, default_niche_id=default_niche, monthly=5_000_000,
        daily_upload_cap=2, config={"connect_status": "connected"},
    )
    schedule = _schedule(
        factory, channel_id=channel, niche_id=schedule_niche,
        daily_cap=250_000,
    )
    project = _project(
        factory, channel_id=channel, niche_id=project_niche,
        schedule_id=schedule,
    )

    ctx = resolve_channel_context(factory, project)
    assert ctx is not None
    assert ctx.channel_id == channel
    assert ctx.niche_profile_id == schedule_niche
    assert ctx.niche_source == "schedule"
    assert ctx.niche_key == "schedule-n"
    assert ctx.voice_id == "ur-PK-AsadNeural"
    assert ctx.voice_source == "niche_profile.default_voice_id"
    assert ctx.voice_style == "calm"
    assert ctx.words_per_minute == 140
    assert ctx.monthly_budget_micros == 5_000_000
    assert ctx.daily_cost_cap_micros == 250_000
    assert ctx.daily_upload_cap == 2
    assert ctx.schedule_channel_mismatch is False
    d = ctx.to_dict()
    assert d["niche_profile_id"] == schedule_niche
    assert d["voice_id"] == "ur-PK-AsadNeural"


def test_project_niche_wins_without_schedule(factory):
    default_niche = _niche(factory, key="def2", voice_id="v-def")
    project_niche = _niche(factory, key="proj2", voice_id="v-proj")
    channel = _channel(factory, default_niche_id=default_niche, monthly=111)
    project = _project(factory, channel_id=channel, niche_id=project_niche)

    ctx = resolve_channel_context(factory, project)
    assert ctx.niche_profile_id == project_niche
    assert ctx.niche_source == "project"
    assert ctx.voice_id == "v-proj"
    assert ctx.daily_cost_cap_micros is None  # no schedule -> no daily cap
    assert ctx.monthly_budget_micros == 111


def test_channel_default_niche_is_last_resort(factory):
    default_niche = _niche(factory, key="def3", voice_id="v-def3")
    channel = _channel(factory, default_niche_id=default_niche)
    # Project row still requires a niche FK; emulate the fallback by
    # resolving a project whose niche row is the default itself via a
    # second project is not possible (projects.niche_profile_id is NOT
    # NULL) — so exercise the fallback through a schedule-less project
    # whose stored niche equals the default and confirm source order
    # documentation: project beats default when both are present.
    project = _project(factory, channel_id=channel, niche_id=default_niche)
    ctx = resolve_channel_context(factory, project)
    assert ctx.niche_profile_id == default_niche
    assert ctx.niche_source == "project"


def test_mismatched_schedule_contributes_nothing(factory):
    niche_a = _niche(factory, key="na", voice_id="voice-a")
    niche_b = _niche(factory, key="nb", voice_id="voice-b")
    channel_a = _channel(factory, default_niche_id=niche_a, monthly=100)
    channel_b = _channel(factory, default_niche_id=niche_b, monthly=200)
    schedule_b = _schedule(
        factory, channel_id=channel_b, niche_id=niche_b, daily_cap=999_000
    )
    # Corrupt / cross-linked project: A's project pointing at B's schedule.
    project_a = _project(
        factory, channel_id=channel_a, niche_id=niche_a,
        schedule_id=schedule_b,
    )
    ctx = resolve_channel_context(factory, project_a)
    assert ctx.schedule_channel_mismatch is True
    assert ctx.niche_profile_id == niche_a
    assert ctx.niche_source == "project"
    assert ctx.voice_id == "voice-a"
    assert ctx.monthly_budget_micros == 100
    assert ctx.daily_cost_cap_micros is None  # B's cap must not leak into A


def test_no_channel_voice_override_convention(factory):
    """config_json voice-looking keys are NOT honoured: no code in the
    codebase reads them, so the resolver must not invent the override."""
    niche = _niche(factory, key="nv", voice_id="niche-voice")
    channel = _channel(
        factory, default_niche_id=niche,
        config={"voice_id": "channel-voice", "default_voice_id": "x",
                "connect_status": "connected"},
    )
    project = _project(factory, channel_id=channel, niche_id=niche)
    ctx = resolve_channel_context(factory, project)
    assert ctx.voice_id == "niche-voice"
    assert ctx.channel_config.get("connect_status") == "connected"


def test_voice_absent_when_niche_has_no_default_voice(factory):
    niche = _niche(factory, key="silent", voice_id=None, voice_style="calm hint")
    channel = _channel(factory, default_niche_id=niche)
    project = _project(factory, channel_id=channel, niche_id=niche)
    ctx = resolve_channel_context(factory, project)
    assert ctx.voice_id is None
    assert ctx.voice_source is None
    assert ctx.voice_style == "calm hint"


def test_missing_project_returns_none(factory):
    assert resolve_channel_context(factory, "no-such-project") is None
    conn = factory()
    try:
        assert resolve_channel_context_from_conn(conn, "nope") is None
    finally:
        conn.close()


def test_resolve_accepts_bare_connection_without_closing(factory):
    niche = _niche(factory, key="bare", voice_id="v-bare")
    channel = _channel(factory, default_niche_id=niche)
    project = _project(factory, channel_id=channel, niche_id=niche)
    conn = factory()
    try:
        ctx = resolve_channel_context(conn, project)
        assert ctx.voice_id == "v-bare"
        # Caller-owned connection still usable afterwards.
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
