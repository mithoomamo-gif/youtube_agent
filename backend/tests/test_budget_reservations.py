"""Unit tests for the Round 4 Budget Guard reservation store.

Owned by the Budget Guard enforcement-core worker (the sibling suite
``test_budget_guard_full.py`` covers the gate end-to-end).  This file
covers :mod:`app.execution.budget_reservations` itself:

* reserve / ensure / get / list shapes (micros + USD mirrors, Pacific
  bucket stamps, channel attribution incl. the NULL bucket),
* settle arithmetic (partial settle shrinks the remainder without
  double-counting; reaching the estimate settles; ``final=True``
  frees the unspent remainder; over-settles count truthfully),
* release arithmetic (remainder freed, settled portion stays
  committed; unknown / non-active ids are safe no-ops),
* Pacific day/month bucket boundaries via explicit ``now=`` and via
  the ``now_pacific`` monkeypatch seam,
* per-channel sums agreeing exactly with the global total,
* persistence across fresh connections (restart survival),

plus a light gate integration pass: daily/monthly parks through
``budget_gate.check_budget_for_project``, reservation creation on an
under-cap start (idempotent across the gate's two seams), research
stage settling, and ``pipeline.cancel`` releasing the remainder.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.execution import budget_gate, budget_reservations as br
from app.providers.budget.policy import save_budget_policy
from app.providers.youtube.quota import PACIFIC
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

# Fixed Pacific moments (Oct 2026 is PDT; Nov 1 is the DST fallback day,
# 00:30 local is still unambiguous for bucket purposes).
DAY1_LATE = datetime(2026, 10, 23, 23, 30, tzinfo=PACIFIC)
DAY2_EARLY = datetime(2026, 10, 24, 0, 30, tzinfo=PACIFIC)
OCT_LATE = datetime(2026, 10, 31, 23, 30, tzinfo=PACIFIC)
NOV_EARLY = datetime(2026, 11, 1, 0, 30, tzinfo=PACIFIC)
MID_OCT = datetime(2026, 10, 15, 12, 0, tzinfo=PACIFIC)


@pytest.fixture()
def env(tmp_path):
    _, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    project_id = seed_project(conn_factory, channel_id, niche_id)
    return {
        "conn_factory": conn_factory,
        "channel_id": channel_id,
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Reserve / shape / persistence
# ---------------------------------------------------------------------------


def test_reserve_creates_active_row_with_contract_fields(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 78_000, now=MID_OCT)
    assert row["project_id"] == env["project_id"]
    assert row["channel_id"] == env["channel_id"]
    assert row["status"] == "active"
    assert row["reserved_micros"] == 78_000
    assert row["reserved_usd"] == pytest.approx(0.078)
    assert row["settled_micros"] == 0
    assert row["settled_usd"] == 0.0
    assert row["remaining_micros"] == 78_000
    assert row["committed_micros"] == 78_000
    assert row["pacific_day"] == "2026-10-15"
    assert row["pacific_month"] == "2026-10"
    assert row["created_at"] is not None
    assert row["settled_at"] is None


def test_reserve_without_channel_is_unattributed(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], None, 10_000, now=MID_OCT)
    assert row["channel_id"] is None


def test_reservation_survives_fresh_connections(env, tmp_path):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 50_000, now=MID_OCT)
    # A brand-new factory over the same file == a process restart.
    from tests.execution_fixtures import conn_factory_for

    db_path = tmp_path / "app.db"
    fresh = conn_factory_for(db_path)
    loaded = br.get_reservation(fresh, row["id"])
    assert loaded is not None
    assert loaded["reserved_micros"] == 50_000
    assert loaded["status"] == "active"


def test_ensure_reservation_is_idempotent_per_project(env):
    cf = env["conn_factory"]
    first = br.ensure_reservation(cf, env["project_id"], env["channel_id"], 40_000, now=MID_OCT)
    second = br.ensure_reservation(cf, env["project_id"], env["channel_id"], 40_000, now=MID_OCT)
    assert first["id"] == second["id"]
    assert len(br.list_reservations(cf, project_id=env["project_id"])) == 1


def test_clock_seam_now_pacific_monkeypatch(env, monkeypatch):
    """Patching budget_reservations.now_pacific steers bucket stamps."""
    cf = env["conn_factory"]
    monkeypatch.setattr(br, "now_pacific", lambda: DAY1_LATE)
    row = br.reserve(cf, env["project_id"], env["channel_id"], 10_000)
    assert row["pacific_day"] == "2026-10-23"
    assert br.daily_spend_micros(cf) == 10_000
    monkeypatch.setattr(br, "now_pacific", lambda: DAY2_EARLY)
    assert br.daily_spend_micros(cf) == 0
    assert br.monthly_spend_micros(cf) == 10_000  # same Pacific month


# ---------------------------------------------------------------------------
# Settle arithmetic
# ---------------------------------------------------------------------------


def test_partial_settle_shrinks_remainder_without_double_count(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 100_000, now=MID_OCT)
    settled = br.settle_reservation(cf, row["id"], 30_000, now=MID_OCT)
    assert settled["status"] == "active"
    assert settled["settled_micros"] == 30_000
    assert settled["remaining_micros"] == 70_000
    # Bucket spend is unchanged: settled slice replaced remainder slice.
    assert br.daily_spend_micros(cf, now=MID_OCT) == 100_000


def test_settle_reaching_estimate_marks_settled_and_stamps(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 50_000, now=MID_OCT)
    br.settle_reservation(cf, row["id"], 20_000, now=MID_OCT)
    done = br.settle_reservation(cf, row["id"], 30_000, now=MID_OCT)
    assert done["status"] == "settled"
    assert done["settled_at"] is not None
    assert done["remaining_micros"] == 0
    assert br.daily_spend_micros(cf, now=MID_OCT) == 50_000


def test_final_settle_frees_unspent_remainder(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 100_000, now=MID_OCT)
    done = br.settle_reservation(cf, row["id"], 25_000, final=True, now=MID_OCT)
    assert done["status"] == "settled"
    assert done["committed_micros"] == 25_000
    assert br.daily_spend_micros(cf, now=MID_OCT) == 25_000


def test_oversettle_counts_truthful_actual(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 40_000, now=MID_OCT)
    done = br.settle_reservation(cf, row["id"], 55_000, now=MID_OCT)
    assert done["status"] == "settled"
    assert done["committed_micros"] == 55_000
    assert br.daily_spend_micros(cf, now=MID_OCT) == 55_000


def test_settle_unknown_and_non_active_are_safe(env):
    cf = env["conn_factory"]
    assert br.settle_reservation(cf, "no-such-id", 1_000, now=MID_OCT) is None
    row = br.reserve(cf, env["project_id"], env["channel_id"], 10_000, now=MID_OCT)
    br.release_reservation(cf, row["id"], now=MID_OCT)
    after = br.settle_reservation(cf, row["id"], 5_000, now=MID_OCT)
    assert after["status"] == "released"
    assert after["settled_micros"] == 0


def test_settle_for_project_targets_active_reservation(env):
    cf = env["conn_factory"]
    assert br.settle_for_project(cf, env["project_id"], 5_000, now=MID_OCT) is None
    br.reserve(cf, env["project_id"], env["channel_id"], 20_000, now=MID_OCT)
    settled = br.settle_for_project(cf, env["project_id"], 12_000, now=MID_OCT)
    assert settled["settled_micros"] == 12_000


# ---------------------------------------------------------------------------
# Release arithmetic
# ---------------------------------------------------------------------------


def test_release_frees_remainder_keeps_settled_spend(env):
    cf = env["conn_factory"]
    row = br.reserve(cf, env["project_id"], env["channel_id"], 100_000, now=MID_OCT)
    br.settle_reservation(cf, row["id"], 30_000, now=MID_OCT)
    released = br.release_reservation(cf, row["id"], reason="cancelled", now=MID_OCT)
    assert released["status"] == "released"
    assert released["released_at"] is not None
    assert released["release_reason"] == "cancelled"
    assert released["remaining_micros"] == 0
    # Only the real (settled) spend stays committed.
    assert br.daily_spend_micros(cf, now=MID_OCT) == 30_000


def test_release_for_project_and_unknown_ids(env):
    cf = env["conn_factory"]
    assert br.release_for_project(cf, env["project_id"]) is None
    assert br.release_reservation(cf, "no-such-id") is None
    br.reserve(cf, env["project_id"], env["channel_id"], 10_000, now=MID_OCT)
    released = br.release_for_project(cf, env["project_id"], reason="failed")
    assert released["status"] == "released"
    assert br.daily_spend_micros(cf, now=MID_OCT) == 0
    # Releasing again is a no-op returning the row.
    again = br.release_for_project(cf, env["project_id"])
    assert again is None  # no active reservation remains


# ---------------------------------------------------------------------------
# Bucket boundaries + per-channel agreement
# ---------------------------------------------------------------------------


def test_daily_bucket_boundary_is_pacific(env):
    cf = env["conn_factory"]
    br.reserve(cf, env["project_id"], env["channel_id"], 10_000, now=DAY1_LATE)
    br.reserve(cf, "other-project", env["channel_id"], 20_000, now=DAY2_EARLY)
    assert br.daily_spend_micros(cf, now=DAY1_LATE) == 10_000
    assert br.daily_spend_micros(cf, now=DAY2_EARLY) == 20_000
    # Both are inside the same Pacific month.
    assert br.monthly_spend_micros(cf, now=DAY2_EARLY) == 30_000


def test_monthly_bucket_boundary_is_pacific(env):
    cf = env["conn_factory"]
    br.reserve(cf, env["project_id"], env["channel_id"], 10_000, now=OCT_LATE)
    br.reserve(cf, "other-project", env["channel_id"], 20_000, now=NOV_EARLY)
    assert br.monthly_spend_micros(cf, now=OCT_LATE) == 10_000
    assert br.monthly_spend_micros(cf, now=NOV_EARLY) == 20_000
    assert br.daily_spend_micros(cf, now=NOV_EARLY) == 20_000


def test_spend_by_channel_sums_exactly_to_total(env):
    cf = env["conn_factory"]
    other_channel, _ = seed_channel_niche(cf)
    br.reserve(cf, env["project_id"], env["channel_id"], 10_000, now=MID_OCT)
    br.reserve(cf, "p2", other_channel, 20_000, now=MID_OCT)
    br.reserve(cf, "p3", None, 30_000, now=MID_OCT)
    by_channel = br.spend_by_channel(cf, "daily", now=MID_OCT)
    assert by_channel[env["channel_id"]] == 10_000
    assert by_channel[other_channel] == 20_000
    assert by_channel[None] == 30_000
    assert sum(by_channel.values()) == br.daily_spend_micros(cf, now=MID_OCT) == 60_000
    # Channel-filtered bucket reads agree with the split.
    assert br.daily_spend_micros(cf, now=MID_OCT, channel_id=other_channel) == 20_000
    assert br.daily_spend_micros(cf, now=MID_OCT, channel_id=None) == 30_000
    totals = br.bucket_totals(cf, now=MID_OCT)
    assert totals["daily"]["total_micros"] == 60_000
    assert totals["daily"]["pacific_day"] == "2026-10-15"
    assert totals["monthly"]["pacific_month"] == "2026-10"


# ---------------------------------------------------------------------------
# Gate integration (light — the full matrix lives in the sibling suite)
# ---------------------------------------------------------------------------


def _set_policy(cf, **overrides):
    policy = {
        "per_video_cap_usd": 1.0,
        "daily_cap_usd": 10.0,
        "monthly_cap_usd": 30.0,
        "warn_pct": 70,
        "soft_pct": 90,
    }
    policy.update(overrides)
    save_budget_policy(policy, cf)


def test_gate_parks_on_daily_cap_and_reserves_nothing(env, monkeypatch):
    cf, pid = env["conn_factory"], env["project_id"]
    _set_policy(cf, daily_cap_usd=0.05)
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    hub = RecordingHub()
    result = budget_gate.check_budget_for_project(cf, pid, events=hub)
    assert result["paused"] is True
    assert result["cap_scope"] == "daily"
    assert result["cap_micros"] == 50_000
    assert result["daily_spent_micros"] == 0
    assert result["needed_micros"] == 28_000
    assert br.get_active_reservation(cf, pid) is None
    assert len(hub.by_type("budget.warning")) == 1
    # Repeat poll while parked: still paused, no second warning.
    again = budget_gate.check_budget_for_project(cf, pid, events=hub)
    assert again["paused"] is True
    assert len(hub.by_type("budget.warning")) == 1


def test_gate_parks_on_monthly_cap(env, monkeypatch):
    cf, pid = env["conn_factory"], env["project_id"]
    _set_policy(cf, monthly_cap_usd=0.05)
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    result = budget_gate.check_budget_for_project(cf, pid, events=RecordingHub())
    assert result["paused"] is True
    assert result["cap_scope"] == "monthly"
    assert result["monthly_needed_micros"] == 28_000


def test_gate_counts_prior_reservations_against_daily_cap(env, monkeypatch):
    cf, pid = env["conn_factory"], env["project_id"]
    _set_policy(cf, daily_cap_usd=0.10)
    # Another project already holds $0.06 of today's budget.
    br.reserve(cf, "prior-project", env["channel_id"], 60_000)
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    result = budget_gate.check_budget_for_project(cf, pid, events=RecordingHub())
    assert result["paused"] is True
    assert result["cap_scope"] == "daily"
    assert result["daily_spent_micros"] == 60_000


def test_gate_under_all_caps_reserves_once_across_seams(env, monkeypatch):
    cf, pid = env["conn_factory"], env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    assert budget_gate.check_budget_for_project(cf, pid, events=RecordingHub()) is None
    active = br.get_active_reservation(cf, pid)
    assert active is not None
    assert active["reserved_micros"] == 78_000
    assert active["channel_id"] == env["channel_id"]
    # Second seam (stage.research re-check): no double reservation.
    assert budget_gate.check_budget_for_project(cf, pid, events=RecordingHub()) is None
    assert len(br.list_reservations(cf, project_id=pid)) == 1


def test_gate_counts_legacy_api_usage_spend(env, monkeypatch):
    """Settled spend seeded only in the api_usage ledger (a pre-Round-4
    project with no reservation) counts against the daily cap."""
    import time as _time
    import uuid as _uuid

    cf, pid = env["conn_factory"], env["project_id"]
    _set_policy(cf, daily_cap_usd=0.10)
    _ch2, _ni2 = seed_channel_niche(cf)
    other = seed_project(cf, _ch2, _ni2)
    conn = cf()
    try:
        conn.execute(
            "INSERT INTO api_usage (id, provider, operation, project_id,"
            " cost_micros, status, created_at)"
            " VALUES (?, 'gemini', 'chat.completion', ?, 60000, 'ok', ?)",
            (_uuid.uuid4().hex, other, int(_time.time() * 1000)),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    result = budget_gate.check_budget_for_project(cf, pid, events=RecordingHub())
    assert result["paused"] is True
    assert result["cap_scope"] == "daily"
    assert result["daily_spent_micros"] == 60_000


def test_gate_legacy_rollup_and_ledger_never_double_count(env, monkeypatch):
    """A project whose rollup AND ledger both record the same $0.06 is
    counted once ($0.06), so $0.06 + $0.078 fits a $0.15 daily cap."""
    import time as _time
    import uuid as _uuid

    cf, pid = env["conn_factory"], env["project_id"]
    _set_policy(cf, daily_cap_usd=0.15)
    _ch2, _ni2 = seed_channel_niche(cf)
    other = seed_project(cf, _ch2, _ni2)
    conn = cf()
    try:
        conn.execute(
            "UPDATE projects SET total_cost_micros = 60000 WHERE id = ?",
            (other,),
        )
        conn.execute(
            "INSERT INTO api_usage (id, provider, operation, project_id,"
            " cost_micros, status, created_at)"
            " VALUES (?, 'gemini', 'chat.completion', ?, 60000, 'ok', ?)",
            (_uuid.uuid4().hex, other, int(_time.time() * 1000)),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    assert budget_gate.check_budget_for_project(cf, pid, events=RecordingHub()) is None
    assert br.get_active_reservation(cf, pid) is not None


def test_research_stage_settles_against_reservation(env, monkeypatch):
    from app.execution.handlers import build_registry

    cf, pid = env["conn_factory"], env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    registry = build_registry(cf)
    job = {"id": "job-res-1", "kind": "stage.research",
           "project_id": pid, "payload": {"project_id": pid}}
    result = asyncio.run(registry["stage.research"](job))
    assert result["stage"] == "research"
    active = br.get_active_reservation(cf, pid)
    assert active is not None
    assert active["settled_micros"] == 12_000  # STAGE_COST_MICROS research
    assert active["remaining_micros"] == 78_000 - 12_000


def test_pipeline_cancel_releases_reservation_remainder(env, monkeypatch):
    from app.execution.service import ExecutionService

    cf, pid = env["conn_factory"], env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf_, pid_, project=None: 78_000,
    )
    service = ExecutionService(cf, hub=RecordingHub())

    async def _start_then_cancel():
        start = await service.registry["pipeline.start"](
            {"kind": "pipeline.start", "project_id": pid,
             "payload": {"project_id": pid}}
        )
        assert start["dag"] == "created"
        assert br.get_active_reservation(cf, pid) is not None
        return await service.registry["pipeline.cancel"](
            {"kind": "pipeline.cancel", "project_id": pid,
             "payload": {"project_id": pid}}
        )

    result = asyncio.run(_start_then_cancel())
    assert result["ok"] is True
    assert br.get_active_reservation(cf, pid) is None
    rows = br.list_reservations(cf, project_id=pid)
    assert len(rows) == 1
    assert rows[0]["status"] == "released"
    assert rows[0]["release_reason"] == "pipeline_cancelled"
