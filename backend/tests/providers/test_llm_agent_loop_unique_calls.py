"""Agent-loop cap counts UNIQUE logical calls (Round 4, Part B).

Round 3 shape: a cricket production died at chapter 4 because the
agent-loop cap counted ledger ROWS — every retry/fallback attempt row —
against the unique-call cap. The cap must count distinct logical calls
(one id per generate() invocation, or a caller-supplied stable
step/chapter key); retries of the same call share that identity and
must not consume budget. Distinct calls still hit the cap exactly.

All provider calls are stub adapters — no network, no keys.
"""

import asyncio
from pathlib import Path

import pytest

from app.providers.llm import (
    BudgetExceeded,
    InMemoryLLMCallLedger,
    LLMRequest,
    LLMResult,
    LLMRouter,
    ModelRegistry,
    Tier,
    UsageBuckets,
)
from app.providers.llm.errors import TransientProviderError
from app.providers.llm.ledger import LLMCallEntry, PriceSnapshot
from app.providers.llm.pacing import PacingConfig, ProviderPacing

BACKEND = Path(__file__).resolve().parents[2]
REGISTRY_PATH = BACKEND / "config" / "models.yaml"

FAST = ProviderPacing(rpm=100_000.0, burst=10_000, min_interval_s=0.0)


def run(coro):
    return asyncio.run(coro)


def fast_pacing() -> PacingConfig:
    return PacingConfig(providers={}, default=FAST)


class OkAdapter:
    def __init__(self, key_ref: str = "stub_key") -> None:
        self._key_ref = key_ref
        self.calls = 0

    async def generate(self, req, entry=None, *, timeout_s: float = 60.0):
        self.calls += 1
        return LLMResult(
            text="ok",
            provider=entry.provider,
            model=entry.model_id,
            tier=req.tier,
            usage=UsageBuckets(input_uncached=10, output=5),
        )


class FlakyAdapter(OkAdapter):
    """Fails transiently on attempts 1-2 of every 3, succeeds on the 3rd.

    Each logical generate() therefore needs exactly 3 provider attempts
    and writes 3 ledger rows (2 failed + 1 ok) — the Round 3 shape.
    """

    async def generate(self, req, entry=None, *, timeout_s: float = 60.0):
        self.calls += 1
        if self.calls % 3 != 0:
            raise TransientProviderError(
                "flaky 503", provider=entry.provider
            )
        return LLMResult(
            text="ok",
            provider=entry.provider,
            model=entry.model_id,
            tier=req.tier,
            usage=UsageBuckets(input_uncached=10, output=5),
        )


class AlwaysTransientAdapter(OkAdapter):
    async def generate(self, req, entry=None, *, timeout_s: float = 60.0):
        self.calls += 1
        raise TransientProviderError("down", provider=entry.provider)


def make_router(adapters, ledger=None, **router_kw) -> LLMRouter:
    router_kw.setdefault("backoff_base_s", 0.0)
    router_kw.setdefault("pacing", fast_pacing())
    router_kw.setdefault("agent_loop_cap_usd", 100.0)
    router_kw.setdefault("per_video_cap_usd", 100.0)
    return LLMRouter(
        adapters,
        registry=ModelRegistry.load(__import__("pathlib").Path(REGISTRY_PATH)),
        ledger=ledger,
        **router_kw,
    )


def chapter_req(prompt: str = "write the chapter", **kw) -> LLMRequest:
    kw.setdefault("agent_id", "7")
    kw.setdefault("project_id", "cricket-1")
    kw.setdefault("task", "script.chapter")
    return LLMRequest(tier=Tier.T0, prompt=prompt, **kw)


# ---------------------------------------------------------------------------
# Router-level regression: the Round 3 shape
# ---------------------------------------------------------------------------

def test_retries_do_not_consume_agent_loop_cap():
    flaky = FlakyAdapter()
    ledger = InMemoryLLMCallLedger()
    router = make_router({"gemini": flaky}, ledger=ledger,
                         agent_loop_max_calls=3)

    async def scenario():
        # 3 logical calls; each needs 3 attempts (9 attempts total,
        # far beyond the cap of 3). Pre-fix, the cap check before the
        # SECOND call already saw 3 ledger rows and killed the run.
        for i in range(3):
            result = await router.generate(chapter_req(f"chapter {i}"))
            assert result.status == "ok"
        assert flaky.calls == 9
        calls, _spent = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 3  # unique logical calls, not 9 rows
        assert len(ledger.entries) == 9  # every attempt still ledgered
        # Distinct-call runaway protection is intact: a 4th DISTINCT
        # call is refused by the cap before any provider dispatch.
        with pytest.raises(BudgetExceeded):
            await router.generate(chapter_req("one chapter too many"))
        assert flaky.calls == 9

    run(scenario())


def test_distinct_calls_still_hit_cap_exactly():
    ok = OkAdapter()
    router = make_router({"gemini": ok}, agent_loop_max_calls=3)

    async def scenario():
        for i in range(3):
            await router.generate(chapter_req(f"call {i}"))
        with pytest.raises(BudgetExceeded):
            await router.generate(chapter_req("call 4"))
        assert ok.calls == 3  # the capped call never dispatched

    run(scenario())


def test_cross_provider_fallback_counts_as_one_logical_call():
    failing = AlwaysTransientAdapter()
    ok = OkAdapter()
    ledger = InMemoryLLMCallLedger()
    router = make_router({"gemini": failing, "openai": ok}, ledger=ledger)

    async def scenario():
        result = await router.generate(chapter_req())
        assert result.provider == "openai"
        # 3 transient attempts on gemini + 1 success on openai = 4 rows.
        assert len(ledger.entries) == 4
        calls, _ = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 1

    run(scenario())


def test_explicit_logical_call_id_dedupes_across_invocations():
    ok = OkAdapter()
    ledger = InMemoryLLMCallLedger()
    router = make_router({"gemini": ok}, ledger=ledger,
                         agent_loop_max_calls=2)

    async def scenario():
        # Orchestration-level re-issue of the SAME logical call
        # (chapter 4) with a stable step/chapter key: counts once.
        key = "cricket-1:script.chapter:4"
        await router.generate(
            chapter_req("chapter 4, attempt A", logical_call_id=key)
        )
        await router.generate(
            chapter_req("chapter 4, attempt B", logical_call_id=key)
        )
        calls, _ = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 1
        # A genuinely different logical call counts separately...
        await router.generate(
            chapter_req(
                "chapter 5", logical_call_id="cricket-1:script.chapter:5"
            )
        )
        calls, _ = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 2
        # ...and the cap still stops the next distinct call.
        with pytest.raises(BudgetExceeded):
            await router.generate(chapter_req("chapter 6"))
        assert ok.calls == 3

    run(scenario())


# ---------------------------------------------------------------------------
# Ledger-level identity rules
# ---------------------------------------------------------------------------

def _entry(**kw) -> LLMCallEntry:
    kw.setdefault("agent_id", "7")
    kw.setdefault("project_id", "cricket-1")
    kw.setdefault("provider", "gemini")
    kw.setdefault("model", "gemini-3.1-flash-lite")
    kw.setdefault("tier", "T0")
    kw.setdefault(
        "price_snapshot",
        PriceSnapshot(
            registry_version="test",
            input_price_per_1m=0.25,
            output_price_per_1m=1.50,
            cached_input_price_per_1m=0.025,
        ),
    )
    return LLMCallEntry(**kw)


def test_ledger_stats_dedupe_by_logical_call_id_but_sum_all_cost():
    async def scenario():
        ledger = InMemoryLLMCallLedger()
        # One logical call, three attempt rows (2 failed + 1 ok).
        for status in ("failed", "failed", "ok"):
            await ledger.append(
                _entry(logical_call_id="L1", status=status, cost_usd=0.01)
            )
        await ledger.append(
            _entry(logical_call_id="L2", status="ok", cost_usd=0.02)
        )
        calls, spent = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 2
        # Cost cap integrity: retries really spent money — sum ALL rows.
        assert spent == pytest.approx(0.05)

    run(scenario())


def test_ledger_stats_legacy_rows_fall_back_to_prompt_hash_then_row():
    async def scenario():
        ledger = InMemoryLLMCallLedger()
        # Legacy rows (no logical_call_id): attempts of one call share
        # (task, prompt_hash) and dedupe; a different prompt is distinct.
        for _ in range(3):
            await ledger.append(
                _entry(task="script.chapter", prompt_hash="h-ch4")
            )
        await ledger.append(
            _entry(task="script.chapter", prompt_hash="h-ch5")
        )
        # Rows with no identity material at all count individually.
        await ledger.append(_entry())
        await ledger.append(_entry())
        calls, _ = await ledger.agent_loop_stats("7", "cricket-1")
        assert calls == 4  # ch4 + ch5 + 2 anonymous rows

    run(scenario())
