"""Budget view tests (Round 4) — ``app.budget_view`` + endpoint.

The view mirrors the enforcement accounting of
``app.execution.budget_gate`` / ``app.execution.budget_reservations``
exactly:

    spent(bucket) = reservation settled slices in the bucket
                  + active reservations' remaining in the bucket
                  + legacy actuals (projects.total_cost_micros of
                    projects created in the bucket with NO reservation)

Covers:
- empty ledger -> honest zeros with full remaining at the policy caps;
- Pacific day edge: 23:59 vs 00:01 Pacific land in different day
  buckets (both inside the same month bucket);
- month edge: last day vs first day of the Pacific month;
- DST transition days (spring-forward 23-hour day, fall-back 25-hour
  day) — window membership and reset math, mirroring quota_view;
- per-channel figures sum EXACTLY to the app totals, with unattributed
  spend under the ``"unassigned"`` (null) channel key;
- the reservation lifecycle in the view: partial settle shrinks the
  remainder without double-counting, release frees the remainder but
  keeps settled spend, a reservation-backed project is never also
  counted as legacy;
- cross-check: app totals equal ``budget_gate.bucket_committed_micros``
  for the same buckets (the gate is the enforcement ground truth);
- ``GET /api/v1/usage/budget`` response shape.

Reservations are seeded through the store's own API
(``app.execution.budget_reservations``) so the tests exercise the
real persisted shape.  Isolated tmp-path DBs throughout
(tests.execution_fixtures.make_db), so no test touches the shared
session DB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as app_db
from app.budget_view import budget_snapshot_from_conn, get_budget_snapshot
from app.errors import register_exception_handlers
from app.execution import budget_gate, budget_reservations as store
from app.providers.youtube.quota import PACIFIC
from app.routers import api as api_module
from tests.execution_fixtures import (
    conn_factory_for,
    make_db,
    seed_channel_niche,
    seed_project,
)

# Fixed clocks (Pacific).
JAN_NOW = datetime(2026, 1, 15, 10, 30, tzinfo=PACIFIC)
JAN_NOON = datetime(2026, 1, 15, 12, 0, tzinfo=PACIFIC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture()
def env(tmp_path):
    """Isolated DB + conn_factory + a caller-owned connection."""
    db_path, conn_factory = make_db(tmp_path)
    conn = conn_factory()
    yield {"db_path": db_path, "conn_factory": conn_factory, "conn": conn}
    conn.close()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _update_project(factory, project_id, *, total_cost_micros=None, created_at_ms=None):
    conn = factory()
    try:
        if total_cost_micros is not None:
            conn.execute(
                "UPDATE projects SET total_cost_micros = ? WHERE id = ?",
                (total_cost_micros, project_id),
            )
        if created_at_ms is not None:
            conn.execute(
                "UPDATE projects SET created_at = ?, updated_at = ? WHERE id = ?",
                (created_at_ms, created_at_ms, project_id),
            )
    finally:
        conn.close()


def _seed_legacy_project(factory, channel_id, niche_id, *, total_cost_micros, created_at):
    """A pre-Round-4 project: actuals, no reservation row."""
    pid = seed_project(factory, channel_id, niche_id)
    _update_project(
        factory, pid,
        total_cost_micros=total_cost_micros, created_at_ms=_ms(created_at),
    )
    return pid


def _seed_policy(conn: sqlite3.Connection, policy: dict) -> None:
    conn.execute(
        "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        ("budget_policy", json.dumps(policy), int(time.time() * 1000)),
    )


# ---------------------------------------------------------------------------
# Empty ledger -> honest zeros
# ---------------------------------------------------------------------------


def test_empty_ledger_honest_zeros_full_remaining(env) -> None:
    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    assert snap["pacific_date"] == "2026-01-15"
    assert snap["pacific_month"] == "2026-01"
    assert snap["day"] == {
        "cap_usd": 10.0, "settled_usd": 0.0, "reserved_usd": 0.0,
        "spent_usd": 0.0, "remaining_usd": 10.0,
        "cap_micros": 10_000_000, "settled_micros": 0, "reserved_micros": 0,
        "spent_micros": 0, "remaining_micros": 10_000_000,
    }
    assert snap["month"]["cap_usd"] == 30.0
    assert snap["month"]["settled_usd"] == 0.0
    assert snap["month"]["reserved_usd"] == 0.0
    assert snap["month"]["spent_usd"] == 0.0
    assert snap["month"]["remaining_usd"] == 30.0
    assert snap["channels"] == {}
    # Reset math agrees with the quota view's Pacific discipline:
    # next midnight PST = 08:00 UTC, 13.5 h after 10:30.
    assert snap["resets_at"] == "2026-01-16T08:00:00Z"
    assert snap["seconds_until_reset"] == 48_600
    assert snap["month_resets_at"] == "2026-02-01T08:00:00Z"


def test_missing_tables_return_honest_zeros(tmp_path) -> None:
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        snap = budget_snapshot_from_conn(bare, now=JAN_NOW)
    finally:
        bare.close()
    assert snap["day"]["cap_usd"] == 10.0
    assert snap["day"]["remaining_usd"] == 10.0
    assert snap["month"]["remaining_usd"] == 30.0
    assert snap["channels"] == {}


def test_custom_policy_caps_are_used(env) -> None:
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_policy(env["conn"], {
        "per_video_cap_usd": 2.0, "daily_cap_usd": 4.0,
        "monthly_cap_usd": 12.0, "warn_pct": 70, "soft_pct": 90,
    })
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=1_500_000, created_at=JAN_NOON
    )
    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    assert snap["day"]["cap_usd"] == 4.0
    assert snap["day"]["settled_usd"] == 1.5
    assert snap["day"]["remaining_usd"] == 2.5
    assert snap["month"]["cap_usd"] == 12.0
    assert snap["month"]["remaining_usd"] == 10.5


# ---------------------------------------------------------------------------
# Pacific day edge: 23:59 vs 00:01 (legacy projects bucket by creation)
# ---------------------------------------------------------------------------


def test_pacific_day_edge_2359_vs_0001(env) -> None:
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=1_000_000,
        created_at=datetime(2026, 3, 10, 23, 59, tzinfo=PACIFIC),
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=2_000_000,
        created_at=datetime(2026, 3, 11, 0, 1, tzinfo=PACIFIC),
    )

    on_10th = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 3, 10, 12, 0, tzinfo=PACIFIC)
    )
    assert on_10th["pacific_date"] == "2026-03-10"
    assert on_10th["day"]["settled_micros"] == 1_000_000
    # Same Pacific month: the month bucket sees both projects.
    assert on_10th["month"]["settled_micros"] == 3_000_000

    on_11th = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 3, 11, 12, 0, tzinfo=PACIFIC)
    )
    assert on_11th["pacific_date"] == "2026-03-11"
    assert on_11th["day"]["settled_micros"] == 2_000_000
    assert on_11th["month"]["settled_micros"] == 3_000_000


def test_utc_evening_is_still_pacific_daytime(env) -> None:
    """A project created 20:00 UTC was created 12:00 Pacific — same day."""
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=500_000,
        created_at=datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc),
    )
    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    assert snap["day"]["settled_micros"] == 500_000


# ---------------------------------------------------------------------------
# Month edge: last day vs first day
# ---------------------------------------------------------------------------


def test_pacific_month_edge_last_vs_first_day(env) -> None:
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=1_000_000,
        created_at=datetime(2026, 1, 31, 23, 59, tzinfo=PACIFIC),
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=4_000_000,
        created_at=datetime(2026, 2, 1, 0, 1, tzinfo=PACIFIC),
    )

    in_jan = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 1, 31, 12, 0, tzinfo=PACIFIC)
    )
    assert in_jan["pacific_month"] == "2026-01"
    assert in_jan["month"]["settled_micros"] == 1_000_000
    # The 23:59 row is later that same Pacific day -> in the day window.
    assert in_jan["day"]["settled_micros"] == 1_000_000

    in_feb = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 2, 1, 12, 0, tzinfo=PACIFIC)
    )
    assert in_feb["pacific_month"] == "2026-02"
    assert in_feb["month"]["settled_micros"] == 4_000_000
    assert in_feb["day"]["settled_micros"] == 4_000_000
    assert in_feb["month_resets_at"] == "2026-03-01T08:00:00Z"


# ---------------------------------------------------------------------------
# DST transition days
# ---------------------------------------------------------------------------


def test_dst_spring_forward_day_window(env) -> None:
    """2026-03-08 is the 23-hour spring-forward day in Pacific time.

    Midnight Mar 8 is PST (UTC-8); midnight Mar 9 is PDT (UTC-7), so the
    day window is 23 h and the reset lands at 07:00 UTC.
    """
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=1_000_000,
        created_at=datetime(2026, 3, 8, 0, 30, tzinfo=PACIFIC),
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=2_000_000,
        created_at=datetime(2026, 3, 8, 23, 30, tzinfo=PACIFIC),
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=4_000_000,
        created_at=datetime(2026, 3, 9, 0, 30, tzinfo=PACIFIC),
    )

    snap = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 3, 8, 12, 0, tzinfo=PACIFIC)
    )
    assert snap["pacific_date"] == "2026-03-08"
    assert snap["day"]["settled_micros"] == 3_000_000
    assert snap["month"]["settled_micros"] == 7_000_000
    assert snap["resets_at"] == "2026-03-09T07:00:00Z"
    # Noon PDT -> midnight PDT is exactly 12 h on the short day.
    assert snap["seconds_until_reset"] == 12 * 3600


def test_dst_fall_back_day_window(env) -> None:
    """2026-11-01 is the 25-hour fall-back day: 01:30 happens twice
    (once PDT, once PST) and both occurrences are the same Pacific day.
    """
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=1_000_000,
        created_at=datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),   # 01:30 PDT
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=2_000_000,
        created_at=datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),   # 01:30 PST
    )
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=4_000_000,
        created_at=datetime(2026, 10, 31, 23, 30, tzinfo=PACIFIC),
    )

    snap = budget_snapshot_from_conn(
        env["conn"], now=datetime(2026, 11, 1, 12, 0, tzinfo=PACIFIC)
    )
    assert snap["pacific_date"] == "2026-11-01"
    assert snap["day"]["settled_micros"] == 3_000_000
    assert snap["month"]["settled_micros"] == 3_000_000
    assert snap["resets_at"] == "2026-11-02T08:00:00Z"


# ---------------------------------------------------------------------------
# Reservation lifecycle in the view
# ---------------------------------------------------------------------------


def test_partial_settle_shrinks_remainder_without_double_count(env) -> None:
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    pid = seed_project(factory, ch, niche)
    row = store.reserve(factory, pid, ch, 2_000_000, now=JAN_NOON)
    store.settle_reservation(factory, row["id"], 500_000, now=JAN_NOON)

    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    # Settled slice counts as settled; the rest stays reserved; the
    # total committed is still the original $2.00 — never $2.50.
    assert snap["day"]["settled_micros"] == 500_000
    assert snap["day"]["reserved_micros"] == 1_500_000
    assert snap["day"]["spent_micros"] == 2_000_000


def test_release_frees_remainder_keeps_settled_spend(env) -> None:
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    pid = seed_project(factory, ch, niche)
    row = store.reserve(factory, pid, ch, 2_000_000, now=JAN_NOON)
    store.settle_reservation(factory, row["id"], 500_000, now=JAN_NOON)
    store.release_reservation(factory, row["id"], reason="cancelled", now=JAN_NOON)

    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    assert snap["day"]["settled_micros"] == 500_000
    assert snap["day"]["reserved_micros"] == 0
    assert snap["day"]["spent_micros"] == 500_000


def test_reservation_buckets_by_reserve_day_not_settle_day(env) -> None:
    """A reservation made Jan 3 counts in January's month bucket and in
    Jan 3's day bucket — a later snapshot day does not pull it in."""
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    pid = seed_project(factory, ch, niche)
    jan3 = datetime(2026, 1, 3, 12, 0, tzinfo=PACIFIC)
    store.reserve(factory, pid, ch, 500_000, now=jan3)

    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)  # Jan 15
    assert snap["day"]["reserved_micros"] == 0
    assert snap["month"]["reserved_micros"] == 500_000


def test_no_reservation_store_legacy_only(env) -> None:
    """A pre-Round-4 DB (no budget_reservations table): reserved = 0,
    settled comes from legacy project actuals alone."""
    factory = env["conn_factory"]
    ch, niche = seed_channel_niche(factory)
    _seed_legacy_project(
        factory, ch, niche, total_cost_micros=250_000, created_at=JAN_NOON
    )
    snap = budget_snapshot_from_conn(env["conn"], now=JAN_NOW)
    assert snap["day"]["reserved_micros"] == 0
    assert snap["day"]["settled_micros"] == 250_000


def test_get_budget_snapshot_opens_configured_db(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "via_settings.db"
    app_db.init_db(str(db_file)).close()
    monkeypatch.setattr(
        app_db, "get_connection", lambda db_path=None: app_db.connect(str(db_file))
    )
    snap = get_budget_snapshot(now=JAN_NOW)
    assert snap["day"]["cap_usd"] == 10.0
    assert snap["day"]["remaining_usd"] == 10.0
    assert snap["seconds_until_reset"] == 48_600


# ---------------------------------------------------------------------------
# Per-channel sums == app totals (incl. the unassigned bucket),
# cross-checked against the enforcement gate itself
# ---------------------------------------------------------------------------


def test_per_channel_sums_equal_app_totals_and_gate(env) -> None:
    factory = env["conn_factory"]
    conn = env["conn"]
    ch1, niche1 = seed_channel_niche(factory)
    ch2, niche2 = seed_channel_niche(factory)
    jan3 = datetime(2026, 1, 3, 12, 0, tzinfo=PACIFIC)

    # Legacy actuals (no reservation rows): ch1 today + earlier month,
    # ch2 today, ch1 last month (excluded everywhere).
    _seed_legacy_project(factory, ch1, niche1, total_cost_micros=1_000_000, created_at=JAN_NOON)
    _seed_legacy_project(factory, ch2, niche2, total_cost_micros=2_000_000, created_at=JAN_NOON)
    _seed_legacy_project(factory, ch1, niche1, total_cost_micros=700_000, created_at=jan3)
    _seed_legacy_project(
        factory, ch1, niche1, total_cost_micros=9_000_000,
        created_at=datetime(2025, 12, 20, 12, 0, tzinfo=PACIFIC),
    )

    # pA (ch1): active reservation today.
    pa = seed_project(factory, ch1, niche1)
    store.reserve(factory, pa, ch1, 1_250_000, now=JAN_NOON)
    # pB (ch2): reservation fully settled today.
    pb = seed_project(factory, ch2, niche2)
    rb = store.reserve(factory, pb, ch2, 3_000_000, now=JAN_NOON)
    store.settle_reservation(factory, rb["id"], 3_000_000, now=JAN_NOON)
    # pC (ch1): partially settled, then released (settled stays).
    pc = seed_project(factory, ch1, niche1)
    rc = store.reserve(factory, pc, ch1, 2_000_000, now=JAN_NOON)
    store.settle_reservation(factory, rc["id"], 500_000, now=JAN_NOON)
    store.release_reservation(factory, rc["id"], reason="failed", now=JAN_NOON)
    # pD (ch2): active reservation from Jan 3 (month bucket only).
    pd = seed_project(factory, ch2, niche2)
    store.reserve(factory, pd, ch2, 500_000, now=jan3)
    # pE (ch1's project, reservation unattributed): active today.  The
    # project ALSO carries $5 of actuals created today — with a
    # reservation row present it must never be legacy-counted.
    pe = seed_project(factory, ch1, niche1)
    _update_project(factory, pe, total_cost_micros=5_000_000, created_at_ms=_ms(JAN_NOON))
    store.reserve(factory, pe, None, 750_000, now=JAN_NOON)

    snap = budget_snapshot_from_conn(conn, now=JAN_NOW)

    # App totals (micros, exact).
    assert snap["day"]["settled_micros"] == 6_500_000
    assert snap["day"]["reserved_micros"] == 2_000_000
    assert snap["day"]["spent_micros"] == 8_500_000
    assert snap["day"]["remaining_micros"] == 1_500_000
    assert snap["month"]["settled_micros"] == 7_200_000
    assert snap["month"]["reserved_micros"] == 2_500_000
    assert snap["month"]["spent_micros"] == 9_700_000
    assert snap["month"]["remaining_micros"] == 20_300_000

    # The enforcement gate agrees to the micro — the view IS the gate's
    # accounting, split into settled/reserved.
    assert budget_gate.bucket_committed_micros(factory, "daily", JAN_NOW) == 8_500_000
    assert budget_gate.bucket_committed_micros(factory, "monthly", JAN_NOW) == 9_700_000

    # Per-channel breakdown, incl. the unassigned (null) key.
    channels = snap["channels"]
    assert set(channels) == {ch1, ch2, "unassigned"}
    assert channels[ch1]["day"]["settled_micros"] == 1_500_000
    assert channels[ch1]["day"]["reserved_micros"] == 1_250_000
    assert channels[ch1]["month"]["settled_micros"] == 2_200_000
    assert channels[ch2]["day"]["settled_micros"] == 5_000_000
    assert channels[ch2]["day"]["reserved_micros"] == 0
    assert channels[ch2]["month"]["reserved_micros"] == 500_000
    assert channels["unassigned"]["day"]["settled_micros"] == 0
    assert channels["unassigned"]["day"]["reserved_micros"] == 750_000
    # Per-channel caps are undefined by policy -> None, never invented.
    assert channels[ch1]["day"]["cap_usd"] is None
    assert channels[ch1]["day"]["remaining_usd"] is None

    # The invariant: per-channel micros sum EXACTLY to the app totals.
    for window in ("day", "month"):
        for field in ("settled_micros", "reserved_micros", "spent_micros"):
            assert (
                sum(c[window][field] for c in channels.values())
                == snap[window][field]
            ), (window, field)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    db_file = tmp_path / "endpoint.db"
    app_db.init_db(str(db_file)).close()
    monkeypatch.setattr(
        app_db, "get_connection", lambda db_path=None: app_db.connect(str(db_file))
    )
    app = FastAPI(title="budget endpoint tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return TestClient(app), db_file


def test_usage_budget_endpoint_empty_zeros(api_env) -> None:
    client, _ = api_env
    resp = client.get("/api/v1/usage/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert body["day"]["cap_usd"] == 10.0
    assert body["day"]["settled_usd"] == 0.0
    assert body["day"]["reserved_usd"] == 0.0
    assert body["day"]["remaining_usd"] == 10.0
    assert body["month"]["cap_usd"] == 30.0
    assert body["month"]["remaining_usd"] == 30.0
    assert body["channels"] == {}
    assert body["pacific_date"]
    assert body["pacific_month"]
    assert body["resets_at"]
    assert body["month_resets_at"]


def test_usage_budget_endpoint_seeded_totals(api_env) -> None:
    client, db_file = api_env
    factory = conn_factory_for(db_file)
    ch, niche = seed_channel_niche(factory)
    now = datetime.now(PACIFIC)
    _seed_legacy_project(factory, ch, niche, total_cost_micros=1_250_000, created_at=now)
    pid = seed_project(factory, ch, niche)
    store.reserve(factory, pid, None, 750_000, now=now)

    resp = client.get("/api/v1/usage/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert body["day"]["settled_micros"] == 1_250_000
    assert body["day"]["reserved_micros"] == 750_000
    assert body["day"]["spent_micros"] == 2_000_000
    assert body["channels"][ch]["day"]["settled_micros"] == 1_250_000
    # The unattributed reservation sits under the unassigned key.
    assert body["channels"]["unassigned"]["day"]["reserved_micros"] == 750_000
