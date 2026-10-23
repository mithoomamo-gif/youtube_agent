"""Round 4 tests for the schedules router + channels niche/voice info.

Covers the Round 4 contract:

* create validates + persists a STRUCTURED cadence (the shape
  ``app.execution.scheduler`` actually reads), computes ``next_run_at``
  and returns ONE normalised shape with channel/niche names resolved;
* built-in niche ids (not DB rows) are upserted into ``niche_profiles``
  so the schedules FK and the scheduler's niche lookup both succeed;
* PATCH edit, pause/resume (``is_enabled`` toggle + next-run handling);
* run-now drives a real ``SchedulerTick`` — a ``schedule_runs`` row and
  a project materialise — and refuses (409) on a paused schedule;
* run history is listed per schedule; unknown ids 404;
* ``GET /channels`` carries the channel's default niche name + voice
  info resolved from real profile data.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

CHANNEL_ID = "chan-sched-test"
CHANNEL_TITLE = "Schedule Test Channel"


def _seed_channel() -> None:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        # The channel's default niche FK needs the profile row first.
        now = int(time.time() * 1000)
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label, "
            "source, voice_style, created_at, updated_at) VALUES "
            "('facts', 'facts', 'Facts / Curiosity', 'builtin', "
            "'energetic male/female, clear', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, youtube_channel_id, "
            "title, handle, default_niche_profile_id, language, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                CHANNEL_ID,
                "UC-sched-test",
                CHANNEL_TITLE,
                "@schedtest",
                "facts",
                "en",
                "active",
                int(time.time() * 1000),
                int(time.time() * 1000),
            ),
        )
    finally:
        conn.close()


def _create(client: TestClient, **overrides) -> "object":
    body = {
        "channel_id": CHANNEL_ID,
        "niche_profile_id": "facts",
        "name": "Morning facts",
        "cadence": {"type": "daily", "time": "09:00"},
        "topic_mode": "auto",
        "target_minutes": 12,
        "publish_mode": "review_first",
        "daily_budget_cap_usd": 2.5,
        "enabled": True,
    }
    body.update(overrides)
    return client.post(
        "/api/v1/schedules",
        json=body,
        headers={"Idempotency-Key": f"sched-{uuid.uuid4()}"},
    )


@pytest.fixture(autouse=True)
def _channel(app):
    # Depends on the conftest `app` fixture so the DB schema exists.
    _seed_channel()
    yield


def test_create_structured_cadence_normalised_and_persisted(client: TestClient):
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["channel_id"] == CHANNEL_ID
    assert body["channel_title"] == CHANNEL_TITLE
    # Built-in profile name resolved from the real profile library.
    assert body["niche_name"] == "Facts / Curiosity"
    assert body["cadence"] == {"type": "daily", "time": "09:00"}
    assert body["cadence_summary"] == "Daily at 09:00 UTC"
    assert body["publish_mode"] == "review_first"
    assert body["daily_cost_cap_micros"] == 2_500_000
    assert body["daily_budget_cap_usd"] == 2.5
    assert body["enabled"] is True and body["is_enabled"] == 1
    assert body["next_run_at"] is not None
    assert body["next_run_at"] > int(time.time() * 1000) - 60_000

    # Persisted in the shape the scheduler reads, niche row upserted.
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (body["id"],)
        ).fetchone()
        assert row is not None
        assert json.loads(row["cadence_json"]) == {
            "type": "daily",
            "time": "09:00",
        }
        niche = conn.execute(
            "SELECT * FROM niche_profiles WHERE id = 'facts'"
        ).fetchone()
        assert niche is not None and niche["source"] == "builtin"
    finally:
        conn.close()

    # And it reads back identically through list + get.
    listed = client.get("/api/v1/schedules").json()["items"]
    match = [s for s in listed if s["id"] == body["id"]]
    assert len(match) == 1
    assert match[0]["cadence_summary"] == "Daily at 09:00 UTC"
    got = client.get(f"/api/v1/schedules/{body['id']}")
    assert got.status_code == 200
    assert got.json()["next_run_at"] == body["next_run_at"]


def test_create_validation_errors(client: TestClient):
    bad_mode = _create(client, publish_mode="sometimes")
    assert bad_mode.status_code == 422
    assert bad_mode.json()["error"]["details"]["field"] == "publish_mode"

    bad_channel = _create(client, channel_id="no-such-channel")
    assert bad_channel.status_code == 422
    assert bad_channel.json()["error"]["details"]["field"] == "channel_id"

    bad_niche = _create(client, niche_profile_id="no-such-niche")
    assert bad_niche.status_code == 422
    assert bad_niche.json()["error"]["details"]["field"] == "niche_profile_id"

    bad_cadence = _create(client, cadence={"type": "daily", "time": "25:99"})
    assert bad_cadence.status_code == 422
    assert bad_cadence.json()["error"]["details"]["field"] == "cadence.time"

    fixed_no_topics = _create(client, topic_mode="fixed_list")
    assert fixed_no_topics.status_code == 422
    assert fixed_no_topics.json()["error"]["details"]["field"] == "cadence.topics"


def test_create_weekly_interval_and_fixed_list_summaries(client: TestClient):
    weekly = _create(
        client, cadence={"type": "weekly", "weekday": 4, "time": "18:30"}
    )
    assert weekly.status_code == 201, weekly.text
    assert weekly.json()["cadence_summary"] == "Weekly on Fri at 18:30 UTC"

    interval = _create(client, cadence={"type": "interval", "interval_hours": 6})
    assert interval.status_code == 201, interval.text
    assert interval.json()["cadence_summary"] == "Every 6h"
    now = int(time.time() * 1000)
    assert now < interval.json()["next_run_at"] <= now + 6 * 3600 * 1000 + 5000

    fixed = _create(
        client,
        topic_mode="fixed_list",
        cadence={
            "type": "daily",
            "time": "07:00",
            "topics": ["Topic one", "Topic two"],
        },
    )
    assert fixed.status_code == 201, fixed.text
    assert fixed.json()["cadence"]["topics"] == ["Topic one", "Topic two"]


def test_patch_edit_pause_resume(client: TestClient):
    created = _create(client).json()
    sid = created["id"]

    edited = client.patch(
        f"/api/v1/schedules/{sid}",
        json={"publish_mode": "auto", "target_minutes": 20,
              "daily_budget_cap_usd": 5},
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["publish_mode"] == "auto"
    assert body["target_minutes"] == 20
    assert body["daily_cost_cap_micros"] == 5_000_000

    paused = client.post(f"/api/v1/schedules/{sid}/pause")
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    assert paused.json()["next_run_at"] is None

    resumed = client.post(f"/api/v1/schedules/{sid}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True
    assert resumed.json()["next_run_at"] is not None

    missing = client.patch(
        "/api/v1/schedules/does-not-exist", json={"name": "x"}
    )
    assert missing.status_code == 404


def test_run_now_materialises_and_records_run(client: TestClient):
    created = _create(client).json()
    sid = created["id"]

    resp = client.post(
        f"/api/v1/schedules/{sid}/run-now",
        headers={"Idempotency-Key": f"run-{uuid.uuid4()}"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["tick"]["materialised"] >= 1
    assert body["run"] is not None
    assert body["run"]["status"] == "created"
    assert body["project_id"] == body["run"]["project_id"]

    runs = client.get(f"/api/v1/schedules/{sid}/runs")
    assert runs.status_code == 200
    items = runs.json()["items"]
    assert len(items) >= 1
    assert items[0]["status"] == "created"
    assert items[0]["project_id"] == body["project_id"]

    after = client.get(f"/api/v1/schedules/{sid}").json()
    assert after["last_run_at"] is not None


def test_run_now_refuses_paused_schedule(client: TestClient):
    created = _create(client, enabled=False).json()
    resp = client.post(
        f"/api/v1/schedules/{created['id']}/run-now",
        headers={"Idempotency-Key": f"run-{uuid.uuid4()}"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "schedule_disabled"


def test_get_runs_and_delete_unknown_and_existing(client: TestClient):
    assert client.get("/api/v1/schedules/nope").status_code == 404
    assert client.get("/api/v1/schedules/nope/runs").status_code == 404

    created = _create(client).json()
    sid = created["id"]
    runs = client.get(f"/api/v1/schedules/{sid}/runs")
    assert runs.status_code == 200
    assert runs.json()["items"] == []

    deleted = client.delete(f"/api/v1/schedules/{sid}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/schedules/{sid}").status_code == 404
    listed = client.get("/api/v1/schedules").json()["items"]
    assert all(s["id"] != sid for s in listed)


def test_channels_list_carries_default_niche_and_voice(client: TestClient):
    resp = client.get("/api/v1/channels")
    assert resp.status_code == 200
    match = [c for c in resp.json()["items"] if c["id"] == CHANNEL_ID]
    assert len(match) == 1
    channel = match[0]
    assert channel["default_niche_profile_id"] == "facts"
    assert channel["default_niche_name"] == "Facts / Curiosity"
    assert channel["default_voice_style"] == "energetic male/female, clear"
    # No voice id is configured for the built-in profile — honest null.
    assert channel["default_voice_id"] is None
