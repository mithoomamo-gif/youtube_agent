"""Budget Guard FULL enforcement behavioural tests (Round 4).

Sibling implementation under test (landed during this task):

* ``app.execution.budget_gate.check_budget_for_project`` — at DAG
  start the estimate is reserved against the per-video, **daily**
  and **monthly** caps (policy in ``app.providers.budget.policy``).
  Bucket spend = settled actuals + active reservation remainders
  (reservation store) + legacy ``projects.total_cost_micros`` for
  projects with no reservation row.  Breach of any cap parks the
  project at Gate A: ``budget_pending`` approval,
  ``status='awaiting_review'``, no DAG jobs, exactly one
  ``budget.warning`` event, and **no** reservation is made.
* ``app.execution.budget_reservations`` — the persisted store
  (table ``budget_reservations``: ``project_id``, nullable
  ``channel_id``, ``reserved_micros`` / ``settled_micros``,
  ``status`` active/settled/released, ``pacific_day`` /
  ``pacific_month`` bucket stamps).  Clock seam:
  ``budget_reservations.now_pacific`` (the gate delegates to it).
  Settle replaces reservation slices with actuals (no double
  count); release frees the unsettled remainder while settled
  actuals stay committed.
* ``app.execution.service`` — ``pipeline.cancel`` releases the
  project's reservation; stage handlers settle stage costs
  (final settle on the package stage).
* Per-channel agreement is tested against BOTH read models the
  implementers exposed:
  (1) the store-level aggregation
      ``budget_reservations.spend_by_channel`` /
      ``bucket_spend_micros`` / ``bucket_totals``, and
  (2) the view-level ``app.budget_view.budget_snapshot_from_conn``
      (same accounting model as the gate: settled = reservation
      rows' settled micros + legacy ``projects.total_cost_micros``,
      reserved = active reservations' remainders, unattributed
      spend under the ``"unassigned"`` key),
  asserting per-channel micros sum EXACTLY to the app totals.

Behaviour is driven through the public seams wherever possible:
``ExecutionService`` ``pipeline.start`` / ``pipeline.cancel``
handlers, ``save_budget_policy`` for small caps, the
``budget_gate.estimate_project_cost_micros`` monkeypatch seam for
crisp estimates, the ``budget_reservations.now_pacific`` clock
seam, and ``gates_bridge.apply_decision`` for the human override.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.execution import budget_gate, budget_reservations, gates_bridge
from app.providers.budget.policy import save_budget_policy
from app.providers.youtube.quota import PACIFIC
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

# Fixed Pacific clocks (noon — clear of every DST edge).
OCT_05 = datetime(2026, 10, 5, 12, 0, tzinfo=PACIFIC)
OCT_10 = datetime(2026, 10, 10, 12, 0, tzinfo=PACIFIC)
OCT_20 = datetime(2026, 10, 20, 12, 0, tzinfo=PACIFIC)
OCT_23 = datetime(2026, 10, 23, 12, 0, tzinfo=PACIFIC)
NOV_02 = datetime(2026, 11, 2, 12, 0, tzinfo=PACIFIC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """Mutable stand-in for ``budget_reservations.now_pacific``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    holder = _Clock(OCT_23)
    # The gate delegates to the store's seam; patch both symbols so
    # either lookup path sees the simulated clock.
    monkeypatch.setattr(budget_reservations, "now_pacific", holder)
    monkeypatch.setattr(budget_gate, "now_pacific", holder)
    return holder


def _env(tmp_path):
    _, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    return conn_factory, channel_id, niche_id


def _set_policy(conn_factory, *, per_video=5.0, daily=10.0, monthly=30.0):
    return save_budget_policy(
        {
            "per_video_cap_usd": per_video,
            "daily_cap_usd": daily,
            "monthly_cap_usd": monthly,
            "warn_pct": 70,
            "soft_pct": 90,
        },
        conn_factory,
    )


def _patch_estimate(monkeypatch, estimates):
    """Patch the gate's estimate seam.

    ``estimates`` is an int (same estimate for every project) or a
    ``{project_id: micros}`` mapping (default: the real DAG table
    sum for unlisted projects).
    """
    if isinstance(estimates, int):
        fn = lambda cf, pid, project=None: estimates  # noqa: E731
    else:
        default = budget_gate.DAG_RESERVED_ESTIMATE_MICROS

        def fn(cf, pid, project=None, _m=dict(estimates), _d=default):
            return _m.get(str(pid), _d)

    monkeypatch.setattr(budget_gate, "estimate_project_cost_micros", fn)


def _run_control(conn_factory, project_id, action="start", hub=None):
    from app.execution.service import ExecutionService

    service = ExecutionService(conn_factory, hub=hub)
    handler = service.registry[f"pipeline.{action}"]
    job = {
        "kind": f"pipeline.{action}",
        "project_id": project_id,
        "payload": {"project_id": project_id},
    }
    return asyncio.run(handler(job))


def _project_row(conn_factory, project_id):
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _approvals(conn_factory, project_id):
    conn = conn_factory()
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE project_id = ?", (project_id,)
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _job_count(conn_factory, project_id):
    conn = conn_factory()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _settle(conn_factory, project_id, actual_micros, *, final=False):
    return budget_gate.settle_budget_for_project(
        conn_factory, project_id, actual_micros, final=final
    )


def _daily(conn_factory, now=None):
    return budget_gate.bucket_committed_micros(conn_factory, "daily", now=now)


def _monthly(conn_factory, now=None):
    return budget_gate.bucket_committed_micros(conn_factory, "monthly", now=now)


def _assert_parked(result, project_id, conn_factory, hub, *, cap_scope):
    """The shared parking contract for a budget breach."""
    assert result["ok"] is True
    assert result["budget_paused"] is True
    assert result["dag"] == "budget_pending"
    assert result["reason"] == "budget_pending"
    assert result["cap_scope"] == cap_scope
    # Parked at Gate A, no DAG, no reservation committed.
    assert _project_row(conn_factory, project_id)["status"] == "awaiting_review"
    assert _job_count(conn_factory, project_id) == 0
    assert budget_reservations.get_active_reservation(conn_factory, project_id) is None
    assert budget_reservations.list_reservations(
        conn_factory, project_id=project_id
    ) == []
    approvals = _approvals(conn_factory, project_id)
    assert len(approvals) == 1
    assert approvals[0]["id"] == result["approval_id"]
    assert approvals[0]["gate"] == "a"
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["reason_code"] == "budget_pending"
    # Exactly one budget.warning, carrying this project's figures.
    warnings = hub.by_type("budget.warning")
    assert len(warnings) == 1
    assert warnings[0]["payload"]["project_id"] == project_id
    assert warnings[0]["payload"]["approval_id"] == approvals[0]["id"]


# ---------------------------------------------------------------------------
# (a) DAILY CAP — video #1 fits and settles, video #2 same day parks
# ---------------------------------------------------------------------------


def test_daily_cap_second_video_parks_after_first_settles(
    tmp_path, monkeypatch, clock
):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="video 1")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="video 2")
    _patch_estimate(monkeypatch, 60_000)  # $0.06 per video

    # Video #1 fits the $0.10 daily cap and proceeds.
    hub1 = RecordingHub()
    first = _run_control(conn_factory, p1, hub=hub1)
    assert first.get("budget_paused") is not True
    assert first["dag"] == "created"
    assert _job_count(conn_factory, p1) == 10
    reservation = budget_reservations.get_active_reservation(conn_factory, p1)
    assert reservation is not None
    assert reservation["reserved_micros"] == 60_000
    assert reservation["channel_id"] == channel_id
    assert reservation["pacific_day"] == "2026-10-23"
    assert reservation["pacific_month"] == "2026-10"
    assert _daily(conn_factory) == 60_000

    # Video #1 settles at its actual cost ($0.06): the reservation is
    # replaced by settled actuals, the bucket total is unchanged.
    settled = _settle(conn_factory, p1, 60_000, final=True)
    assert settled is not None and settled["status"] == "settled"
    assert settled["settled_micros"] == 60_000
    assert settled["committed_micros"] == 60_000
    assert _daily(conn_factory) == 60_000
    assert _monthly(conn_factory) == 60_000

    # Video #2 the same Pacific day: 60k settled + 60k new = 120k
    # breaches the 100k daily cap (monthly is nowhere near its cap).
    hub2 = RecordingHub()
    second = _run_control(conn_factory, p2, hub=hub2)
    _assert_parked(second, p2, conn_factory, hub2, cap_scope="daily")
    assert second["spent_micros"] == 60_000
    assert second["cap_micros"] == 100_000
    assert second["remaining_micros"] == 40_000
    assert second["needed_micros"] == 20_000
    assert second["daily_spent_micros"] == 60_000
    assert second["daily_cap_micros"] == 100_000
    assert second["monthly_spent_micros"] == 60_000


# ---------------------------------------------------------------------------
# (b) MONTHLY CAP ACROSS DAYS — earlier days of the month block a later
#     day even with a fresh daily bucket; next month is a control pass
# ---------------------------------------------------------------------------


def test_monthly_cap_blocks_later_day_same_month_next_month_control(
    tmp_path, monkeypatch, clock
):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    _set_policy(conn_factory, per_video=5.0, daily=10.0, monthly=0.10)
    p_early = seed_project(conn_factory, channel_id, niche_id, topic="early")
    p_late = seed_project(conn_factory, channel_id, niche_id, topic="late")
    p_next = seed_project(conn_factory, channel_id, niche_id, topic="next mo")
    _patch_estimate(monkeypatch, 60_000)

    # Oct 5: first video fits the $0.10 monthly cap and settles.
    clock.now = OCT_05
    first = _run_control(conn_factory, p_early, hub=RecordingHub())
    assert first.get("budget_paused") is not True
    assert first["dag"] == "created"
    _settle(conn_factory, p_early, 60_000, final=True)
    early_res = budget_reservations.list_reservations(
        conn_factory, project_id=p_early
    )
    assert len(early_res) == 1
    assert early_res[0]["pacific_day"] == "2026-10-05"
    assert early_res[0]["pacific_month"] == "2026-10"

    # Oct 20: the daily bucket is fresh (nothing committed that day)
    # but the month bucket still carries the Oct 5 spend.
    clock.now = OCT_20
    assert _daily(conn_factory) == 0
    assert _monthly(conn_factory) == 60_000

    hub = RecordingHub()
    blocked = _run_control(conn_factory, p_late, hub=hub)
    _assert_parked(blocked, p_late, conn_factory, hub, cap_scope="monthly")
    assert blocked["spent_micros"] == 60_000
    assert blocked["cap_micros"] == 100_000
    assert blocked["needed_micros"] == 20_000
    assert blocked["daily_spent_micros"] == 0
    assert blocked["monthly_spent_micros"] == 60_000

    # Control: the same kind of start in the FOLLOWING month proceeds
    # — both buckets are fresh in November.
    clock.now = NOV_02
    assert _daily(conn_factory) == 0
    assert _monthly(conn_factory) == 0
    control = _run_control(conn_factory, p_next, hub=RecordingHub())
    assert control.get("budget_paused") is not True
    assert control["dag"] == "created"
    assert _job_count(conn_factory, p_next) == 10
    next_res = budget_reservations.get_active_reservation(conn_factory, p_next)
    assert next_res is not None
    assert next_res["pacific_day"] == "2026-11-02"
    assert next_res["pacific_month"] == "2026-11"


# ---------------------------------------------------------------------------
# (c) SETTLEMENT / RELEASE FREES BUDGET
# ---------------------------------------------------------------------------


def test_pipeline_cancel_releases_reservation_and_unblocks_next_start(
    tmp_path, monkeypatch, clock
):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="cancel me")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="waiting")
    _patch_estimate(monkeypatch, 60_000)

    first = _run_control(conn_factory, p1, hub=RecordingHub())
    assert first["dag"] == "created"
    assert _daily(conn_factory) == 60_000

    # While p1's reservation is live, p2 cannot start (60k + 60k > 100k).
    hub_block = RecordingHub()
    blocked = _run_control(conn_factory, p2, hub=hub_block)
    _assert_parked(blocked, p2, conn_factory, hub_block, cap_scope="daily")

    # Cancelling p1 through the public pipeline.cancel seam releases
    # its unsettled reservation: the whole 60k is freed (nothing had
    # settled against it).
    cancelled = _run_control(conn_factory, p1, action="cancel", hub=RecordingHub())
    assert cancelled["ok"] is True
    assert _project_row(conn_factory, p1)["status"] == "cancelled"
    released = budget_reservations.list_reservations(
        conn_factory, project_id=p1
    )
    assert len(released) == 1
    assert released[0]["status"] == "released"
    assert released[0]["release_reason"] == "pipeline_cancelled"
    assert released[0]["committed_micros"] == 0
    assert _daily(conn_factory) == 0

    # The previously blocked start now proceeds, with no new warning.
    hub_retry = RecordingHub()
    retry = _run_control(conn_factory, p2, hub=hub_retry)
    assert retry.get("budget_paused") is not True
    assert retry["dag"] == "created"
    assert _job_count(conn_factory, p2) == 10
    assert hub_retry.by_type("budget.warning") == []
    assert budget_reservations.get_active_reservation(conn_factory, p2) is not None


def test_failed_run_release_frees_unsettled_remainder(
    tmp_path, monkeypatch, clock
):
    """A run that ends without settling (failure) frees its hold via
    the gate's release delegate — the store path service/runner end
    paths use for failed/stopped runs."""
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="fails")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="next")
    _patch_estimate(monkeypatch, 60_000)

    assert _run_control(conn_factory, p1, hub=RecordingHub())["dag"] == "created"
    assert _daily(conn_factory) == 60_000

    released = budget_gate.release_budget_for_project(
        conn_factory, p1, reason="run_failed"
    )
    assert released is not None and released["status"] == "released"
    assert released["release_reason"] == "run_failed"
    assert _daily(conn_factory) == 0

    nxt = _run_control(conn_factory, p2, hub=RecordingHub())
    assert nxt.get("budget_paused") is not True
    assert nxt["dag"] == "created"


def test_partial_settle_no_double_count_then_release_frees_remainder(
    tmp_path, monkeypatch, clock
):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="partial")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="fits after")
    _patch_estimate(monkeypatch, {p1: 90_000, p2: 60_000})

    started = _run_control(conn_factory, p1, hub=RecordingHub())
    assert started["dag"] == "created"
    assert _daily(conn_factory) == 90_000

    # Partial settle: $0.04 of actuals land.  The settled slice
    # REPLACES an equal slice of the reservation — committed spend
    # stays 90k, it does not become 90k + 40k = 130k (double count),
    # and the still-active remainder is 50k.
    partial = _settle(conn_factory, p1, 40_000)
    assert partial is not None and partial["status"] == "active"
    assert partial["settled_micros"] == 40_000
    assert partial["remaining_micros"] == 50_000
    assert partial["committed_micros"] == 90_000
    assert _daily(conn_factory) == 90_000

    # The run now fails: release frees the unsettled 50k remainder;
    # only the truthfully settled 40k stays committed.
    released = budget_gate.release_budget_for_project(
        conn_factory, p1, reason="run_failed"
    )
    assert released is not None and released["status"] == "released"
    assert released["settled_micros"] == 40_000
    assert released["committed_micros"] == 40_000
    assert _daily(conn_factory) == 40_000

    # 40k committed + 60k new = exactly the 100k cap -> proceeds.
    # (Had the remainder not been freed: 90k + 60k > cap -> parked.)
    nxt = _run_control(conn_factory, p2, hub=RecordingHub())
    assert nxt.get("budget_paused") is not True
    assert nxt["dag"] == "created"


def test_final_settle_below_estimate_frees_remainder(
    tmp_path, monkeypatch, clock
):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="cheap run")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="next run")
    _patch_estimate(monkeypatch, {p1: 60_000, p2: 55_000})

    assert _run_control(conn_factory, p1, hub=RecordingHub())["dag"] == "created"

    # The run finishes cheaper than estimated: final settle at 45k
    # frees the unspent 15k of the reservation.
    settled = _settle(conn_factory, p1, 45_000, final=True)
    assert settled is not None and settled["status"] == "settled"
    assert settled["reserved_micros"] == 60_000
    assert settled["settled_micros"] == 45_000
    assert settled["committed_micros"] == 45_000
    assert _daily(conn_factory) == 45_000

    # 45k + 55k = exactly the cap -> proceeds (with the reservation
    # still held at 60k it would have parked: 60k + 55k > 100k).
    nxt = _run_control(conn_factory, p2, hub=RecordingHub())
    assert nxt.get("budget_paused") is not True
    assert nxt["dag"] == "created"


# ---------------------------------------------------------------------------
# (d) PER-CHANNEL AGREEMENT — store aggregation AND budget view
# ---------------------------------------------------------------------------


def test_per_channel_store_aggregation_sums_to_totals(tmp_path, clock):
    """Against the store-level aggregation the implementer exposes:
    ``budget_reservations.spend_by_channel`` / ``bucket_spend_micros``
    / ``bucket_totals`` over the persisted reservation rows."""
    conn_factory, channel_id, niche_id = _env(tmp_path)
    ch2, niche2 = seed_channel_niche(conn_factory)
    p_ch1 = seed_project(conn_factory, channel_id, niche_id, topic="ch1 today")
    p_ch1_early = seed_project(conn_factory, channel_id, niche_id, topic="ch1 early")
    p_ch2 = seed_project(conn_factory, ch2, niche2, topic="ch2 today")
    p_none = seed_project(conn_factory, ch2, niche2, topic="unattributed")

    # ch1 today: active 30k.  ch2 today: reserved 20k, settled 12k
    # (final) -> committed 12k.  Unattributed (NULL channel): active
    # 15k.  ch1 earlier this month (Oct 10): active 10k, month only.
    budget_reservations.reserve(conn_factory, p_ch1, channel_id, 30_000, now=OCT_23)
    budget_reservations.reserve(conn_factory, p_ch2, ch2, 20_000, now=OCT_23)
    budget_reservations.settle_for_project(conn_factory, p_ch2, 12_000, final=True)
    budget_reservations.reserve(conn_factory, p_none, None, 15_000, now=OCT_23)
    budget_reservations.reserve(conn_factory, p_ch1_early, channel_id, 10_000, now=OCT_10)

    clock.now = OCT_23
    daily_by_channel = budget_reservations.spend_by_channel(
        conn_factory, "daily", now=OCT_23
    )
    assert daily_by_channel == {channel_id: 30_000, ch2: 12_000, None: 15_000}
    daily_total = budget_reservations.bucket_spend_micros(
        conn_factory, "daily", now=OCT_23
    )
    assert daily_total == 57_000
    assert sum(daily_by_channel.values()) == daily_total

    monthly_by_channel = budget_reservations.spend_by_channel(
        conn_factory, "monthly", now=OCT_23
    )
    assert monthly_by_channel == {channel_id: 40_000, ch2: 12_000, None: 15_000}
    monthly_total = budget_reservations.bucket_spend_micros(
        conn_factory, "monthly", now=OCT_23
    )
    assert monthly_total == 67_000
    assert sum(monthly_by_channel.values()) == monthly_total

    # bucket_totals (the view worker's read of the same store) agrees.
    totals = budget_reservations.bucket_totals(conn_factory, now=OCT_23)
    assert totals["daily"]["total_micros"] == daily_total
    assert totals["daily"]["by_channel"] == daily_by_channel
    assert totals["monthly"]["total_micros"] == monthly_total
    assert totals["monthly"]["by_channel"] == monthly_by_channel

    # The gate's committed-spend read (store + legacy) agrees too:
    # every project here either has a reservation or zero actuals,
    # so the legacy slice contributes nothing.
    assert _daily(conn_factory) == daily_total
    assert _monthly(conn_factory) == monthly_total

    # Per-channel filtered bucket reads match the split exactly.
    assert budget_reservations.bucket_spend_micros(
        conn_factory, "daily", now=OCT_23, channel_id=channel_id
    ) == 30_000
    assert budget_reservations.bucket_spend_micros(
        conn_factory, "daily", now=OCT_23, channel_id=None
    ) == 15_000


def test_per_channel_budget_view_sums_to_totals(tmp_path):
    """Against ``app.budget_view.budget_snapshot_from_conn`` (final
    Round 4 accounting model, identical to the gate's): settled =
    reservation rows' ``settled_micros`` + legacy actuals
    (``projects.total_cost_micros`` of reservation-less projects
    created in the window); reserved = active rows' remainders;
    unattributed spend sits under ``UNASSIGNED_CHANNEL``
    (``"unassigned"``).  Per-channel micros must sum EXACTLY to
    the app totals for day and month, and the view's spent figures
    must equal the gate's ``bucket_committed_micros``."""
    from app.budget_view import UNASSIGNED_CHANNEL, budget_snapshot_from_conn

    conn_factory, channel_id, niche_id = _env(tmp_path)
    ch2, niche2 = seed_channel_niche(conn_factory)
    p_ch1 = seed_project(conn_factory, channel_id, niche_id, topic="view ch1")
    p_ch2 = seed_project(conn_factory, ch2, niche2, topic="view ch2")
    p_legacy = seed_project(conn_factory, ch2, niche2, topic="view legacy")

    def _reserve_and_settle(project_id, channel, amount, when, actual):
        row = budget_reservations.reserve(
            conn_factory, project_id, channel, amount, now=when
        )
        budget_reservations.settle_reservation(
            conn_factory, row["id"], actual, final=True
        )

    # Settled (fully settled reservation rows): ch1 11k today +
    # 5k earlier this month, ch2 22k today, unattributed 7k today.
    _reserve_and_settle(p_ch1, channel_id, 11_000, OCT_23, 11_000)
    _reserve_and_settle(p_ch1, channel_id, 5_000, OCT_05, 5_000)
    _reserve_and_settle(p_ch2, ch2, 22_000, OCT_23, 22_000)
    _reserve_and_settle("unattributed-project", None, 7_000, OCT_23, 7_000)

    # Reserved (active rows): ch1 30k today, ch2 20k today + 9k
    # earlier this month, unattributed 15k today.
    budget_reservations.reserve(conn_factory, p_ch1, channel_id, 30_000, now=OCT_23)
    budget_reservations.reserve(conn_factory, p_ch2, ch2, 20_000, now=OCT_23)
    budget_reservations.reserve(conn_factory, p_ch2, ch2, 9_000, now=OCT_05)
    budget_reservations.reserve(
        conn_factory, "unattributed-project", None, 15_000, now=OCT_23
    )

    # Legacy actuals: a reservation-less project on ch2 created
    # today (Pacific) with 3k of accrued cost.
    conn = conn_factory()
    try:
        conn.execute(
            "UPDATE projects SET total_cost_micros = 3000, created_at = ?"
            " WHERE id = ?",
            (int(OCT_23.timestamp() * 1000), p_legacy),
        )
    finally:
        conn.close()

    conn = conn_factory()
    try:
        snap = budget_snapshot_from_conn(conn, now=OCT_23)
    finally:
        conn.close()

    # App totals, exact micros.
    assert snap["day"]["settled_micros"] == 43_000
    assert snap["day"]["reserved_micros"] == 65_000
    assert snap["day"]["spent_micros"] == 108_000
    assert snap["month"]["settled_micros"] == 48_000
    assert snap["month"]["reserved_micros"] == 74_000
    assert snap["month"]["spent_micros"] == 122_000

    # The view's spent equals the gate's committed spend, by design.
    assert snap["day"]["spent_micros"] == _daily(conn_factory, now=OCT_23)
    assert snap["month"]["spent_micros"] == _monthly(conn_factory, now=OCT_23)

    channels = snap["channels"]
    assert set(channels) == {channel_id, ch2, UNASSIGNED_CHANNEL}
    assert channels[channel_id]["day"]["settled_micros"] == 11_000
    assert channels[channel_id]["day"]["reserved_micros"] == 30_000
    assert channels[channel_id]["month"]["settled_micros"] == 16_000
    assert channels[channel_id]["month"]["reserved_micros"] == 30_000
    assert channels[ch2]["day"]["settled_micros"] == 25_000  # 22k + 3k legacy
    assert channels[ch2]["day"]["reserved_micros"] == 20_000
    assert channels[ch2]["month"]["settled_micros"] == 25_000
    assert channels[ch2]["month"]["reserved_micros"] == 29_000
    assert channels[UNASSIGNED_CHANNEL]["day"]["settled_micros"] == 7_000
    assert channels[UNASSIGNED_CHANNEL]["day"]["reserved_micros"] == 15_000

    # The invariant, for both windows and every figure.
    for window in ("day", "month"):
        for field in ("settled_micros", "reserved_micros", "spent_micros"):
            assert (
                sum(c[window][field] for c in channels.values())
                == snap[window][field]
            ), (window, field)


# ---------------------------------------------------------------------------
# (e) OVERRIDE — approving the budget_pending approval unblocks a
#     daily park and a monthly park (Round 3 semantics, extended)
# ---------------------------------------------------------------------------


def test_approved_override_releases_daily_park(tmp_path, monkeypatch, clock):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="first")
    p2 = seed_project(conn_factory, channel_id, niche_id, topic="override me")
    _patch_estimate(monkeypatch, 60_000)

    assert _run_control(conn_factory, p1, hub=RecordingHub())["dag"] == "created"
    _settle(conn_factory, p1, 60_000, final=True)

    hub = RecordingHub()
    parked = _run_control(conn_factory, p2, hub=hub)
    _assert_parked(parked, p2, conn_factory, hub, cap_scope="daily")

    # Human approves the budget approval (the approvals router's call).
    applied = gates_bridge.apply_decision(
        conn_factory, p2, "a", "approved",
        approval_id=parked["approval_id"], reason_code="budget_pending",
    )
    assert applied in (True, False)  # no live gate job on this path

    # Next start bypasses the daily cap and proceeds; a tracking
    # reservation is still recorded so spend stays attributed.
    retry = _run_control(conn_factory, p2, hub=RecordingHub())
    assert retry.get("budget_paused") is not True
    assert retry["dag"] == "created"
    assert _job_count(conn_factory, p2) == 10
    tracking = budget_reservations.get_active_reservation(conn_factory, p2)
    assert tracking is not None
    assert tracking["reserved_micros"] == 60_000
    assert tracking["channel_id"] == channel_id


def test_approved_override_releases_monthly_park(tmp_path, monkeypatch, clock):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    _set_policy(conn_factory, per_video=5.0, daily=10.0, monthly=0.10)
    p_early = seed_project(conn_factory, channel_id, niche_id, topic="early")
    p_late = seed_project(conn_factory, channel_id, niche_id, topic="monthly override")
    _patch_estimate(monkeypatch, 60_000)

    clock.now = OCT_05
    assert _run_control(conn_factory, p_early, hub=RecordingHub())["dag"] == "created"
    _settle(conn_factory, p_early, 60_000, final=True)

    clock.now = OCT_20
    hub = RecordingHub()
    parked = _run_control(conn_factory, p_late, hub=hub)
    _assert_parked(parked, p_late, conn_factory, hub, cap_scope="monthly")

    gates_bridge.apply_decision(
        conn_factory, p_late, "a", "approved",
        approval_id=parked["approval_id"], reason_code="budget_pending",
    )
    retry = _run_control(conn_factory, p_late, hub=RecordingHub())
    assert retry.get("budget_paused") is not True
    assert retry["dag"] == "created"
    assert _job_count(conn_factory, p_late) == 10
    tracking = budget_reservations.get_active_reservation(conn_factory, p_late)
    assert tracking is not None
    assert tracking["pacific_month"] == "2026-10"


# ---------------------------------------------------------------------------
# Extra: the gate's two seams (pipeline.start + stage.research) must
# reserve exactly once — a second check for an already-reserved
# project is a no-op, never a double reservation / double count.
# ---------------------------------------------------------------------------


def test_second_gate_check_does_not_double_reserve(tmp_path, monkeypatch, clock):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    clock.now = OCT_23
    _set_policy(conn_factory, per_video=5.0, daily=0.10, monthly=30.0)
    p1 = seed_project(conn_factory, channel_id, niche_id, topic="one reserve")
    _patch_estimate(monkeypatch, 60_000)

    assert _run_control(conn_factory, p1, hub=RecordingHub())["dag"] == "created"
    # Second seam (stage.research calls the same check directly).
    again = budget_gate.check_budget_for_project(
        conn_factory, p1, events=RecordingHub()
    )
    assert again is None
    rows = budget_reservations.list_reservations(conn_factory, project_id=p1)
    assert len(rows) == 1
    assert rows[0]["reserved_micros"] == 60_000
    assert _daily(conn_factory) == 60_000
    assert _approvals(conn_factory, p1) == []
