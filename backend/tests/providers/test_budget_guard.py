"""Budget Guard tests — pure logic, no network.

Covers roadmap Part 10.4: six nested caps, reservation ledger atomicity,
threshold ladder (70/90/100), downgrade ladder order, NO SILENT OVERRIDES,
quota projection, estimate-vs-actual variance, catalog staleness.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.providers.budget import (  # noqa: E402
    DOWNGRADE_LADDER,
    BudgetCaps,
    BudgetGuard,
    BudgetHardStop,
    CallSpec,
    InvalidLedgerState,
    L1LimitExceeded,
    NoSilentOverrideError,
    PriceCatalog,
    PriceEntry,
    QuotaAccount,
    ReservationNotFound,
    ReservationRefused,
    RetryCapExceeded,
    RetryLimitExceeded,
    usd_to_micros,
)

import pytest  # noqa: E402

PRICES_YAML = Path(__file__).resolve().parents[2] / "config" / "prices.yaml"
FIXED_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def make_guard(**cap_kwargs) -> BudgetGuard:
    return BudgetGuard(caps=BudgetCaps(**cap_kwargs), clock=lambda: FIXED_NOW)


def reserve(guard: BudgetGuard, **over):
    kw = dict(
        project_id="p1",
        video_id="v1",
        channel_id="ch1",
        agent_id="agent-script",
        step="script",
        provider="openai",
        model="gpt-5-mini",
        operation="chat.completion",
        estimated_cost_micros=100_000,
    )
    kw.update(over)
    return guard.reserve(**kw)


# --------------------------------------------------------------- L3 per-video
def test_reservation_refused_when_exceeding_per_video_cap():
    async def main():
        guard = make_guard()  # standard stack: $1.00 video cap
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.60"))
        with pytest.raises(ReservationRefused):
            await reserve(guard, estimated_cost_micros=usd_to_micros("0.50"))
        # The refused reservation committed nothing.
        assert guard.video_used_micros("v1") == usd_to_micros("0.60")

    asyncio.run(main())


def test_stack_caps_free_standard_premium():
    assert BudgetCaps(stack="free").video_cap() == usd_to_micros("0.25")
    assert BudgetCaps(stack="standard").video_cap() == usd_to_micros("1.00")
    assert BudgetCaps(stack="premium").video_cap() == usd_to_micros("5.00")


def test_concurrent_reservations_cannot_overcommit():
    async def main():
        guard = make_guard()
        results = await asyncio.gather(
            reserve(guard, video_id="v1", estimated_cost_micros=usd_to_micros("0.60")),
            reserve(guard, video_id="v2", project_id="p2", estimated_cost_micros=usd_to_micros("0.60")),
            return_exceptions=True,
        )
        # Global caps are large here; make the race about the SAME video pot:
        # both targeted different videos, so redo with a shared global-day cap.
        assert all(not isinstance(r, Exception) for r in results)

        guard2 = BudgetGuard(
            caps=BudgetCaps(global_daily_cap_micros=usd_to_micros("1.00")),
            clock=lambda: FIXED_NOW,
        )
        results2 = await asyncio.gather(
            reserve(guard2, video_id="v1", estimated_cost_micros=usd_to_micros("0.60")),
            reserve(guard2, video_id="v2", project_id="p2", estimated_cost_micros=usd_to_micros("0.60")),
            return_exceptions=True,
        )
        successes = [r for r in results2 if not isinstance(r, Exception)]
        failures = [r for r in results2 if isinstance(r, Exception)]
        assert len(successes) == 1  # the last dollar can only be promised once
        assert len(failures) == 1
        assert isinstance(failures[0], BudgetHardStop)
        assert guard2.global_day_used_micros() == usd_to_micros("0.60")

    asyncio.run(main())


# ------------------------------------------------------------ settle/release
def test_settle_releases_unspent_remainder():
    async def main():
        guard = make_guard()
        entry = await reserve(guard, estimated_cost_micros=usd_to_micros("0.80"))
        assert guard.video_used_micros("v1") == usd_to_micros("0.80")
        settled = await guard.settle(entry.reservation_id, actual_cost_micros=usd_to_micros("0.30"))
        assert settled.status == "settled"
        assert settled.cost_micros == usd_to_micros("0.30")
        assert settled.reserved_cost_micros == usd_to_micros("0.80")
        # Remainder released: another $0.60 now fits under the $1.00 cap.
        assert guard.video_used_micros("v1") == usd_to_micros("0.30")
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.60"), step="voice")
        assert guard.video_used_micros("v1") == usd_to_micros("0.90")

    asyncio.run(main())


def test_release_frees_the_full_reservation():
    async def main():
        guard = make_guard()
        entry = await reserve(guard, estimated_cost_micros=usd_to_micros("0.90"))
        await guard.release(entry.reservation_id, reason="step abandoned")
        assert entry.status == "released"
        assert guard.video_used_micros("v1") == 0
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.90"), step="voice")

    asyncio.run(main())


def test_settle_failures_propagate_write_before_accept():
    async def main():
        guard = make_guard()
        with pytest.raises(ReservationNotFound):
            await guard.settle("no-such-id", actual_cost_micros=1)
        entry = await reserve(guard)
        await guard.settle(entry.reservation_id, actual_cost_micros=50_000)
        with pytest.raises(InvalidLedgerState):
            await guard.settle(entry.reservation_id, actual_cost_micros=50_000)
        with pytest.raises(InvalidLedgerState):
            await guard.release(entry.reservation_id)

    asyncio.run(main())


# -------------------------------------------------------------------- L2
def test_retry_spend_capped_at_15_percent_of_video_cap():
    async def main():
        guard = make_guard()  # $1.00 cap -> retry sub-cap $0.15
        assert guard.retry_cap_micros("v1") == usd_to_micros("0.15")
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.50"))
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.10"), category="retry")
        with pytest.raises(RetryCapExceeded):
            await reserve(guard, estimated_cost_micros=usd_to_micros("0.10"), category="retry", step="voice")
        # Primary spend is unaffected by the retry sub-cap.
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.20"), step="voice")

    asyncio.run(main())


def test_retry_attempt_and_correction_round_limits():
    async def main():
        guard = make_guard()
        for _ in range(2):  # max 2 retries per step
            await reserve(guard, estimated_cost_micros=10_000, category="retry")
        with pytest.raises(RetryLimitExceeded):
            await reserve(guard, estimated_cost_micros=10_000, category="retry")
        for _ in range(2):  # max 2 correction rounds per step
            await reserve(guard, estimated_cost_micros=10_000, category="correction", step="qc")
        with pytest.raises(RetryLimitExceeded):
            await reserve(guard, estimated_cost_micros=10_000, category="correction", step="qc")

    asyncio.run(main())


# -------------------------------------------------------------------- L1
def test_l1_call_spec_rejected_before_sending_and_truncation():
    async def main():
        guard = make_guard()
        with pytest.raises(L1LimitExceeded):
            await reserve(guard, call_spec=CallSpec(output_tokens=5000))  # L1 max 4000
        with pytest.raises(L1LimitExceeded):
            await reserve(guard, call_spec=CallSpec(images=11))  # L1 max 10
        clamped = guard.truncate_call_spec(CallSpec(output_tokens=9999, images=99, tts_chars=99999))
        assert clamped == CallSpec(output_tokens=4000, images=10, tts_chars=9000)
        await reserve(guard, call_spec=CallSpec(output_tokens=4000, images=10))

    asyncio.run(main())


# --------------------------------------------------------------- L4/L5/L6
def test_l4_channel_day_cap_pauses_only_that_channel():
    async def main():
        guard = make_guard(videos_per_day=1)  # L4 = 1 x $1.00 x 1.2 = $1.20
        assert guard.channel_daily_cap_micros("ch1", "v1") == usd_to_micros("1.20")
        await reserve(guard, video_id="v1", estimated_cost_micros=usd_to_micros("1.00"))
        with pytest.raises(BudgetHardStop) as exc:
            await reserve(guard, video_id="v2", project_id="p2", estimated_cost_micros=usd_to_micros("0.30"))
        assert exc.value.scope == "channel_day"
        # Another channel's queue is untouched.
        await reserve(guard, video_id="v3", project_id="p3", channel_id="ch2",
                      estimated_cost_micros=usd_to_micros("0.30"))

    asyncio.run(main())


def test_l6_month_cap_turns_auto_mode_off_but_manual_approval_still_works():
    async def main():
        guard = make_guard(global_monthly_cap_micros=usd_to_micros("1.00"))
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.90"))
        assert guard.auto_mode_enabled is True
        with pytest.raises(BudgetHardStop) as exc:
            await reserve(guard, video_id="v2", project_id="p2", estimated_cost_micros=usd_to_micros("0.20"))
        assert exc.value.scope == "global_month"
        entry = await reserve(guard, video_id="v2", project_id="p2",
                              estimated_cost_micros=usd_to_micros("0.20"), manual_approved=True)
        assert entry.status == "reserved"
        assert guard.auto_mode_enabled is False
        assert guard.manual_approve_only is True

    asyncio.run(main())


# --------------------------------------------------------- threshold ladder
def test_thresholds_fire_in_order_warning_soft_hard():
    async def main():
        guard = make_guard()
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.70"))
        video_events = [e.level for e in guard.events if e.scope == "video:v1"]
        assert video_events == ["warning"]  # 70%: passive warning only
        assert guard.active_downgrades("v1") == []

        await reserve(guard, estimated_cost_micros=usd_to_micros("0.20"), step="voice")
        video_events = [e.level for e in guard.events if e.scope == "video:v1"]
        assert video_events == ["warning", "soft_stop"]  # 90%: downgrades engage
        assert [d.id for d in guard.active_downgrades("v1")] == [d.id for d in DOWNGRADE_LADDER]
        assert len(guard.downgrade_log) == 5  # recorded, never silent

        with pytest.raises(BudgetHardStop):  # 100%: hard stop
            await reserve(guard, estimated_cost_micros=usd_to_micros("0.20"), step="render")

    asyncio.run(main())


def test_hard_stop_exception_carries_spent_remaining_needed():
    async def main():
        guard = make_guard()
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.90"))
        with pytest.raises(BudgetHardStop) as exc:
            await reserve(guard, estimated_cost_micros=usd_to_micros("0.20"), step="voice")
        err = exc.value
        assert err.scope == "video"
        assert err.cap_micros == usd_to_micros("1.00")
        assert err.spent_micros == usd_to_micros("0.90")
        assert err.remaining_micros == usd_to_micros("0.10")
        assert err.requested_micros == usd_to_micros("0.20")
        assert err.needed_micros == usd_to_micros("0.10")  # exact shortfall
        assert err.payload["needed_micros"] == usd_to_micros("0.10")

    asyncio.run(main())


def test_downgrade_ladder_order_is_exactly_1_to_5():
    assert [d.id for d in DOWNGRADE_LADDER] == [
        "ai_scenes_to_stock",
        "premium_tts_to_edge",
        "t3_script_to_t2",
        "thumbnail_variants_3_to_1",
        "render_1440p_to_1080p",
    ]
    assert [d.order for d in DOWNGRADE_LADDER] == [1, 2, 3, 4, 5]


# ------------------------------------------------------ no silent overrides
def test_raise_cap_requires_user_action_marker_and_is_audit_logged():
    async def main():
        guard = make_guard()
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.90"))

        with pytest.raises(NoSilentOverrideError):
            guard.raise_cap("per_video", usd_to_micros("2.00"), video_id="v1")  # no marker
        with pytest.raises(NoSilentOverrideError):
            guard.raise_cap("per_video", usd_to_micros("2.00"), video_id="v1",
                            user_action=True, actor="supervisor")  # agents can never raise
        assert guard.audit_log == []

        entry = guard.raise_cap("per_video", usd_to_micros("2.00"), video_id="v1",
                                user_action=True, actor="user", reason="premium one-off")
        assert len(guard.audit_log) == 1
        assert entry.old_cap_micros == usd_to_micros("1.00")
        assert entry.new_cap_micros == usd_to_micros("2.00")
        assert entry.ts is not None and entry.actor == "user"
        # The raised cap is effective immediately.
        await reserve(guard, estimated_cost_micros=usd_to_micros("0.50"), step="voice")

    asyncio.run(main())


# -------------------------------------------------------------------- quota
def test_quota_account_burn_projection_and_downgrade_flag():
    account = QuotaAccount(
        provider="elevenlabs",
        plan_name="Creator",
        quota_total=121_000,
        cycle_start=date(2026, 10, 1),
        reset_date=date(2026, 11, 14),
    )
    account.burn(70_000, product="narration")
    account.burn(4_000, product="music")  # shared-pool drain stays visible
    assert account.by_product == {"narration": 70_000, "music": 4_000}
    assert account.quota_remaining == 47_000
    projected = account.projected_exhaustion_date(date(2026, 10, 23))
    assert projected is not None and projected < date(2026, 11, 14)
    assert account.flagged_for_downgrade(date(2026, 10, 23)) is True
    with pytest.raises(Exception):
        account.burn(100_000)  # would exceed the 121k pool


# ------------------------------------------------------- estimate vs actual
def test_variance_report_flags_recalibration_beyond_25_percent():
    guard = make_guard()
    guard.record_estimate("p1", usd_to_micros("1.00"), "2026-10-22.1")
    guard.complete_project("p1", actual_micros=usd_to_micros("1.10"))  # +10%
    guard.record_estimate("p2", usd_to_micros("1.00"), "2026-10-22.1")
    guard.complete_project("p2", actual_micros=usd_to_micros("1.40"))  # +40%
    guard.record_estimate("p3", usd_to_micros("1.00"), "2026-10-22.1")
    guard.complete_project("p3", actual_micros=usd_to_micros("1.50"))  # +50%
    report = guard.variance_report(last_n=10)
    assert report["count"] == 3
    assert report["flagged_project_ids"] == ["p2", "p3"]
    assert report["needs_recalibration"] is True


def test_complete_project_derives_actual_from_settled_ledger():
    async def main():
        guard = make_guard()
        guard.record_estimate("p1", usd_to_micros("1.00"), "2026-10-22.1")
        entry = await reserve(guard, estimated_cost_micros=usd_to_micros("0.50"))
        await guard.settle(entry.reservation_id, actual_cost_micros=usd_to_micros("0.80"))
        record = guard.complete_project("p1")
        assert record.actual_micros == usd_to_micros("0.80")
        assert record.variance_pct == -20.0
        assert record.flagged is False

    asyncio.run(main())


# ------------------------------------------------------------------ catalog
def test_prices_yaml_loads_with_verified_oct2026_figures():
    catalog = PriceCatalog.load(PRICES_YAML)
    assert catalog.version == "2026-10-22.1"
    # LLM spot checks (per-token prices from the per-1M figures).
    assert catalog.get("google", "gemini-2.5-flash-lite", "input_token").unit_price_usd == Decimal("0.0000001")
    assert catalog.get("openai", "gpt-5", "output_token").unit_price_usd == Decimal("0.00001")
    assert catalog.get("google", "gemini-3.1-flash-lite", "cached_input_token").unit_price_usd == Decimal("0.000000025")
    # 1M in + 1M out on Flash-Lite = $0.10 + $0.40 = $0.50 exactly.
    assert catalog.llm_cost_micros("google", "gemini-2.5-flash-lite",
                                   units_in=1_000_000, units_out=1_000_000) == usd_to_micros("0.50")
    # Images.
    assert catalog.cost_micros("black-forest-labs", "flux.1-schnell", "image", 1) == 3_000
    assert catalog.cost_micros("ideogram", "ideogram-api", "image", 1) == 60_000


def test_tts_pricing_uses_the_actual_billing_route():
    catalog = PriceCatalog.load(PRICES_YAML)
    chars = 9_000  # one 10-min video
    assert catalog.cost_micros("microsoft", "edge-tts", "tts_char", chars, "free") == 0
    assert catalog.cost_micros("openai", "tts-1", "tts_char", chars) == 135_000  # ~$0.14
    api = catalog.cost_micros("elevenlabs", "eleven-flash-turbo", "tts_char", chars, "api_payg")
    subs = catalog.cost_micros("elevenlabs", "eleven-flash-turbo", "tts_char", chars, "subscription_credits")
    assert api == 360_000  # $0.36 via API
    assert subs == 1_490_400  # ~$1.49 via subscription credits — same chars!
    assert subs > api


def test_stale_catalog_detection_over_60_days():
    fresh = PriceEntry(provider="x", model_or_service="m", unit="image",
                       unit_price_usd=Decimal("0.01"),
                       effective_from=date(2026, 10, 22), verified_at=date(2026, 10, 22))
    catalog = PriceCatalog(entries=[fresh], version="t")
    assert catalog.is_stale(date(2026, 10, 23)) is False
    assert catalog.is_stale(date(2026, 12, 21)) is False  # exactly 60 days: not stale yet
    assert catalog.is_stale(date(2026, 12, 22)) is True  # 61 days: stale
    loaded = PriceCatalog.load(PRICES_YAML)
    assert loaded.is_stale(date(2026, 11, 1)) is False
    assert loaded.is_stale(date(2027, 2, 1)) is True


def test_catalog_hot_reload(tmp_path):
    src = PRICES_YAML.read_text(encoding="utf-8")
    target = tmp_path / "prices.yaml"
    target.write_text(src, encoding="utf-8")
    catalog = PriceCatalog.load(target)
    assert catalog.reload() is False  # unchanged on disk
    target.write_text(src.replace('version: "2026-10-22.1"', 'version: "2099-01-01.9"'), encoding="utf-8")
    # Force a newer mtime so the change is observed deterministically.
    import os
    import time
    future = time.time() + 10
    os.utime(target, (future, future))
    assert catalog.reload() is True
    assert catalog.version == "2099-01-01.9"


def test_ledger_entry_carries_all_spec_fields():
    async def main():
        guard = make_guard()
        entry = await reserve(
            guard,
            units_in=1_000, units_out=200, units_cached=500,
            unit_price_snapshot="openai/gpt-5-mini:input_token[api_payg]@2026-10-22.1",
        )
        for attr in ("ts", "project_id", "video_id", "channel_id", "agent_id", "step",
                     "provider", "model", "operation", "units_in", "units_out",
                     "units_cached", "unit_price_snapshot", "cost_micros", "category", "status"):
            assert hasattr(entry, attr), attr
        assert entry.status == "reserved" and entry.category == "primary"

    asyncio.run(main())
