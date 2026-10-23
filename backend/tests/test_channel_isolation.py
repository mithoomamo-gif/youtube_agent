"""Multi-channel isolation proof (Round 4, Worker 3).

Two channels x two projects each, run interleaved + concurrently
through the real execution handlers (stub stage path — isolation is
about data, not media), plus direct rows for the tables the handlers
do not write (licences, publish records, api_usage).  Every assertion
checks that a channel-scoped read for channel A contains *only*
channel A's data — and symmetrically for B:

* project_artefacts / agent_runs / projects.total_cost_micros
* asset_licences (via project_assets -> assets)
* approvals (via gates_bridge)
* publish_records
* api_usage + the per-channel usage view (cost attribution)
* channel-context resolution (niche / voice / budget never cross)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.execution import gates_bridge
from app.execution.channel_context import (
    build_channel_usage_views,
    resolve_channel_context,
)
from app.execution.dag import create_project_dag
from app.execution.handlers import build_registry
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import make_db, now_ms


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


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


def _seed_side(factory, label, voice_id, monthly, daily_cap):
    """One channel + niche + schedule; returns their ids."""
    ts = now_ms()
    suffix = uuid.uuid4().hex[:8]
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    schedule_id = str(uuid.uuid4())
    _insert(factory,
            "INSERT INTO niche_profiles (id, key, label, source,"
            " voice_style, default_voice_id, words_per_minute, risk_class,"
            " is_active, created_at, updated_at) VALUES (?, ?, ?,"
            " 'builtin', ?, ?, 140, 'normal', 1, ?, ?)",
            (niche_id, f"niche-{label}-{suffix}", label,
             f"style-{label}", voice_id, ts, ts))
    _insert(factory,
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " default_niche_profile_id, language, status,"
            " monthly_budget_micros, created_at, updated_at) VALUES"
            " (?, ?, ?, ?, 'en-US', 'active', ?, ?, ?)",
            (channel_id, f"UC-{label}-{suffix}", f"Channel {label}",
             niche_id, monthly, ts, ts))
    _insert(factory,
            "INSERT INTO schedules (id, name, channel_id,"
            " niche_profile_id, publish_mode, daily_cost_cap_micros,"
            " is_enabled, created_at, updated_at) VALUES (?, ?, ?, ?,"
            " 'review_first', ?, 1, ?, ?)",
            (schedule_id, f"Sched {label}", channel_id, niche_id,
             daily_cap, ts, ts))
    return channel_id, niche_id, schedule_id


def _seed_project(factory, channel_id, niche_id, schedule_id, topic):
    project_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(factory,
            "INSERT INTO projects (id, channel_id, niche_profile_id,"
            " topic, topic_source, status, publish_mode, schedule_id,"
            " total_cost_micros, created_at, updated_at) VALUES"
            " (?, ?, ?, ?, 'schedule', 'draft', 'review_first', ?, 0,"
            " ?, ?)",
            (project_id, channel_id, niche_id, topic, schedule_id, ts, ts))
    return project_id


@pytest.fixture()
def world(tmp_path):
    _, factory = make_db(tmp_path)
    queue = SQLiteJobQueue(factory)
    a = _seed_side(factory, "alpha", "voice-alpha", 1_000_000, 100_000)
    b = _seed_side(factory, "beta", "voice-beta", 2_000_000, 200_000)
    sides = {}
    for label, (channel_id, niche_id, schedule_id) in (("A", a), ("B", b)):
        projects = [
            _seed_project(factory, channel_id, niche_id, schedule_id,
                          f"{label} topic {i}")
            for i in (1, 2)
        ]
        sides[label] = {
            "channel_id": channel_id, "niche_id": niche_id,
            "schedule_id": schedule_id, "projects": projects,
        }
    return {"factory": factory, "queue": queue, "sides": sides}


def _all_project_ids(world):
    return [p for side in world["sides"].values() for p in side["projects"]]


def _run_interleaved_stages(world):
    """Run research+script for A1,B1,A2,B2 interleaved, then a
    concurrent batch (one stage for every project via asyncio.gather).
    Returns {project_id: {stage: result}}."""
    factory, queue = world["factory"], world["queue"]
    registry = build_registry(factory)
    dag = {pid: create_project_dag(queue, pid) for pid in _all_project_ids(world)}

    def job(pid, kind):
        ids = dag[pid]
        kinds = [queue.get(j)["kind"] for j in ids]
        return queue.get(ids[kinds.index(kind)])

    results: dict[str, dict] = {pid: {} for pid in dag}
    order = _all_project_ids(world)  # A1, A2, B1, B2 by construction? no:
    # _all_project_ids groups by side; interleave explicitly instead.
    a_projects = world["sides"]["A"]["projects"]
    b_projects = world["sides"]["B"]["projects"]
    interleaved = [a_projects[0], b_projects[0], a_projects[1], b_projects[1]]
    for pid in interleaved:
        results[pid]["research"] = asyncio.run(
            registry["stage.research"](job(pid, "stage.research")))

    async def _concurrent():
        return await asyncio.gather(*[
            registry["stage.script"](job(pid, "stage.script"))
            for pid in interleaved
        ])

    for pid, result in zip(interleaved, asyncio.run(_concurrent())):
        results[pid]["script"] = result
    return results


def _rows(factory, sql, params=()):
    conn = factory()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Context isolation
# ---------------------------------------------------------------------------


def test_context_resolution_never_crosses_channels(world):
    factory = world["factory"]
    expected = {
        "A": ("voice-alpha", 1_000_000, 100_000),
        "B": ("voice-beta", 2_000_000, 200_000),
    }
    for label, side in world["sides"].items():
        voice, monthly, daily = expected[label]
        for pid in side["projects"]:
            ctx = resolve_channel_context(factory, pid)
            assert ctx.channel_id == side["channel_id"]
            assert ctx.niche_profile_id == side["niche_id"]
            assert ctx.niche_source == "schedule"
            assert ctx.voice_id == voice
            assert ctx.monthly_budget_micros == monthly
            assert ctx.daily_cost_cap_micros == daily

    # Concurrent resolution from threads: 40 mixed lookups, none cross.
    def resolve(pid):
        return resolve_channel_context(factory, pid)

    jobs = []
    owners = {}
    for label, side in world["sides"].items():
        for pid in side["projects"]:
            owners[pid] = side
            jobs.extend([pid] * 10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for pid, ctx in zip(jobs, pool.map(resolve, jobs)):
            assert ctx.project_id == pid
            assert ctx.channel_id == owners[pid]["channel_id"]
            assert ctx.niche_profile_id == owners[pid]["niche_id"]


def test_stage_results_carry_only_owning_channel_context(world):
    results = _run_interleaved_stages(world)
    for label, side in world["sides"].items():
        for pid in side["projects"]:
            for stage in ("research", "script"):
                ctx = results[pid][stage]["channel_context"]
                assert ctx["channel_id"] == side["channel_id"]
                assert ctx["niche_profile_id"] == side["niche_id"]
                assert ctx["voice_id"] == (
                    "voice-alpha" if label == "A" else "voice-beta")


# ---------------------------------------------------------------------------
# Execution-layer data isolation
# ---------------------------------------------------------------------------


def test_artefacts_runs_and_costs_do_not_cross(world):
    factory = world["factory"]
    _run_interleaved_stages(world)

    for label, side in world["sides"].items():
        for pid in side["projects"]:
            artefacts = _rows(
                factory,
                "SELECT * FROM project_artefacts WHERE project_id = ?",
                (pid,))
            assert {a["artefact_type"] for a in artefacts} == {
                "research_brief", "script"}
            assert all(a["project_id"] == pid for a in artefacts)

            runs = _rows(
                factory,
                "SELECT * FROM agent_runs WHERE project_id = ?", (pid,))
            assert len(runs) == 2
            assert all(r["project_id"] == pid for r in runs)
            # Artefacts were produced only by this project's own runs.
            run_ids = {r["id"] for r in runs}
            assert {a["produced_by_run_id"] for a in artefacts} <= run_ids

            project = _rows(
                factory, "SELECT * FROM projects WHERE id = ?", (pid,))[0]
            assert project["channel_id"] == side["channel_id"]
            assert project["total_cost_micros"] == sum(
                r["cost_micros"] for r in runs)
            assert project["total_cost_micros"] > 0

    # No artefact / run row references a project of the other channel
    # through its producing run (join-level cross-talk check).
    crossed = _rows(
        factory,
        "SELECT a.id FROM project_artefacts a "
        "JOIN agent_runs r ON r.id = a.produced_by_run_id "
        "WHERE r.project_id <> a.project_id")
    assert crossed == []


def test_approvals_publish_licences_and_usage_do_not_cross(world):
    factory = world["factory"]
    _run_interleaved_stages(world)
    ts = now_ms()

    # Approvals: one Gate A approval per project via the real bridge.
    approval_ids = {}
    for pid in _all_project_ids(world):
        project = _rows(
            factory, "SELECT * FROM projects WHERE id = ?", (pid,))[0]
        approval_ids[pid] = gates_bridge.ensure_approval(
            factory, project, "a", {"project_id": pid})
    assert len(set(approval_ids.values())) == 4
    for pid, approval_id in approval_ids.items():
        rows = _rows(
            factory, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
        assert len(rows) == 1 and rows[0]["project_id"] == pid

    # Publish records + licences + api_usage, seeded per project.
    for label, side in world["sides"].items():
        for i, pid in enumerate(side["projects"]):
            _insert(factory,
                    "INSERT INTO publish_records (id, project_id,"
                    " youtube_video_id, privacy_status,"
                    " upload_idempotency_key, created_at) VALUES"
                    " (?, ?, ?, 'private', ?, ?)",
                    (str(uuid.uuid4()), pid, f"vid-{label}-{i}",
                     f"up-{pid}", ts))
            asset_id = str(uuid.uuid4())
            digest = hashlib.sha256(f"asset-{pid}".encode()).hexdigest()
            _insert(factory,
                    "INSERT INTO assets (id, content_hash, asset_type,"
                    " file_path, source_provider, created_at) VALUES"
                    " (?, ?, 'stock_video', ?, 'pixabay', ?)",
                    (asset_id, digest, f"/assets/{pid}.mp4", ts))
            _insert(factory,
                    "INSERT INTO asset_licences (asset_id, licence_type,"
                    " commercial_use_ok, verified_at) VALUES"
                    " (?, 'pixabay_content', 1, ?)", (asset_id, ts))
            _insert(factory,
                    "INSERT INTO project_assets (project_id, asset_id,"
                    " scene_no, usage_role, created_at) VALUES"
                    " (?, ?, 1, 'scene_visual', ?)", (pid, asset_id, ts))
            _insert(factory,
                    "INSERT INTO api_usage (id, provider, operation,"
                    " project_id, cost_micros, status, created_at)"
                    " VALUES (?, 'gemini', 'generate', ?, ?, 'ok', ?)",
                    (str(uuid.uuid4()), pid,
                     1_000 if label == "A" else 2_000, ts))

    for label, side in world["sides"].items():
        other = "B" if label == "A" else "A"
        other_pids = set(world["sides"][other]["projects"])
        for pid in side["projects"]:
            pubs = _rows(
                factory,
                "SELECT * FROM publish_records WHERE project_id = ?",
                (pid,))
            assert len(pubs) == 1 and pubs[0]["project_id"] == pid

            licences = _rows(
                factory,
                "SELECT l.* FROM asset_licences l "
                "JOIN project_assets pa ON pa.asset_id = l.asset_id "
                "WHERE pa.project_id = ?", (pid,))
            assert len(licences) == 1

            usage = _rows(
                factory,
                "SELECT * FROM api_usage WHERE project_id = ?", (pid,))
            assert len(usage) == 1
            assert usage[0]["cost_micros"] == (
                1_000 if label == "A" else 2_000)

        # Join-level: nothing owned by this channel's projects leaks
        # into the other channel's project set.
        leaked = _rows(
            factory,
            "SELECT pa.project_id FROM project_assets pa "
            "JOIN projects p ON p.id = pa.project_id "
            "WHERE p.channel_id = ?",
            (side["channel_id"],))
        assert {r["project_id"] for r in leaked} == set(side["projects"])
        assert not ({r["project_id"] for r in leaked} & other_pids)

    # Per-channel usage view: spend attribution follows the projects.
    # Expected spend is computed independently (per-project agent_runs
    # sums), NOT via a fixed stage-cost table — whether the stages ran
    # stub or real implementations, each channel's view must equal
    # exactly its own projects' runs and nothing of the other channel's.
    from app.execution.channel_context import month_window_ms

    start_ms, end_ms = month_window_ms()
    conn = factory()
    try:
        views = {v["channel_id"]: v for v in build_channel_usage_views(conn)}
        own_runs = {}
        for label, side in world["sides"].items():
            total = 0
            for pid in side["projects"]:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_micros), 0) FROM agent_runs "
                    "WHERE project_id = ? AND created_at >= ? "
                    "AND created_at < ?",
                    (pid, start_ms, end_ms),
                ).fetchone()
                total += int(row[0])
            own_runs[label] = total
    finally:
        conn.close()
    assert all(v > 0 for v in own_runs.values())
    # The two views together account for every run exactly once.
    assert sum(
        v["month_to_date_spend_micros"] for v in views.values()
    ) == sum(own_runs.values())
    for label, side in world["sides"].items():
        view = views[side["channel_id"]]
        assert view["project_count"] == 2
        assert view["month_to_date_api_usage_micros"] == (
            2_000 if label == "A" else 4_000)
        assert view["month_to_date_spend_micros"] == own_runs[label]
        assert view["monthly_budget_micros"] == (
            1_000_000 if label == "A" else 2_000_000)
        assert view["schedule_daily_caps_micros"] == [
            100_000 if label == "A" else 200_000]
