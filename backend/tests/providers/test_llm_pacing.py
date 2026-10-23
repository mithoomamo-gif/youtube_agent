"""Single-flight LLM dispatch queue + pacing tests (Round 4).

All provider calls are stub adapters — no network, no real keys.
Covers the Round 4 requirements:
  (a) N parallel same-key calls are serialised/paced and all succeed
  (b) different keys / providers are NOT serialised against each other
  (c) ledger rows == real dispatched calls (queued-never-dispatched
      calls write nothing; dispatched 429s are still ledgered)
  (d) max-wait timeout raises QueueTimeoutError cleanly
"""

import asyncio
import time
from pathlib import Path

import pytest

from app.providers.llm import (
    InMemoryLLMCallLedger,
    LLMRequest,
    LLMResult,
    LLMRouter,
    ModelRegistry,
    Tier,
    UsageBuckets,
)
from app.providers.llm.errors import RateLimitError
from app.providers.llm.pacing import (
    PacingConfig,
    ProviderPacing,
    QueueTimeoutError,
    SingleFlightPacer,
    get_default_pacer,
)

BACKEND = __file__.rsplit("/tests/", 1)[0]
REGISTRY_PATH = f"{BACKEND}/config/models.yaml"

FAST = ProviderPacing(rpm=100_000.0, burst=10_000, min_interval_s=0.0)


def run(coro):
    return asyncio.run(coro)


def fast_pacing(**kw) -> PacingConfig:
    kw.setdefault("providers", {})
    kw.setdefault("default", FAST)
    return PacingConfig(**kw)


class StubAdapter:
    """Records concurrency/intervals; optionally always raises `error`."""

    def __init__(
        self,
        key_ref: str = "gemini_key",
        *,
        delay: float = 0.05,
        error: Exception | None = None,
        tracker: dict | None = None,
    ) -> None:
        self._key_ref = key_ref
        self.delay = delay
        self.error = error
        self.tracker = tracker if tracker is not None else {"active": 0, "max": 0}
        self.calls = 0
        self.starts: list[float] = []
        self.intervals: list[tuple[float, float]] = []

    async def generate(self, req, entry=None, *, timeout_s: float = 60.0):
        self.calls += 1
        self.tracker["active"] += 1
        self.tracker["max"] = max(self.tracker["max"], self.tracker["active"])
        start = time.monotonic()
        self.starts.append(start)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return LLMResult(
                text=f"ok-{self.calls}",
                provider=entry.provider,
                model=entry.model_id,
                tier=req.tier,
                usage=UsageBuckets(input_uncached=10, output=5),
            )
        finally:
            self.tracker["active"] -= 1
            self.intervals.append((start, time.monotonic()))


def make_router(adapters, ledger=None, **kw) -> LLMRouter:
    kw.setdefault("backoff_base_s", 0.0)
    return LLMRouter(
        adapters,
        registry=ModelRegistry.load(Path(REGISTRY_PATH)),
        ledger=ledger or InMemoryLLMCallLedger(),
        **kw,
    )


# --------------------------------------------------------------------- (a)
def test_parallel_same_key_calls_are_serialised_and_paced():
    async def scenario():
        stub = StubAdapter(delay=0.02)
        ledger = InMemoryLLMCallLedger()
        pacing = PacingConfig(
            providers={"gemini": ProviderPacing(rpm=100_000.0, burst=10_000, min_interval_s=0.05)},
            default=FAST,
        )
        router = make_router({"gemini": stub}, ledger=ledger, pacing=pacing)
        results = await asyncio.gather(
            *(
                router.generate(LLMRequest(tier=Tier.T0, prompt=f"hello {i}"))
                for i in range(6)
            )
        )
        assert all(r.status == "ok" for r in results)
        assert stub.calls == 6
        # single-flight: never two provider calls in flight for this key
        assert stub.tracker["max"] == 1
        intervals = sorted(stub.intervals)
        for (_, prev_end), (next_start, _) in zip(intervals, intervals[1:]):
            assert next_start >= prev_end - 0.005  # no overlap
        starts = sorted(stub.starts)
        for a, b in zip(starts, starts[1:]):
            assert b - a >= 0.04  # min_interval_s=0.05 pacing (with jitter slack)
        # (c) every dispatched call ledgered exactly once
        assert len(ledger.entries) == stub.calls == 6
        assert all(e.status == "ok" for e in ledger.entries)

    run(scenario())


def test_same_key_across_two_routers_sharing_pacer_is_serialised():
    async def scenario():
        tracker = {"active": 0, "max": 0}
        pacer = SingleFlightPacer(fast_pacing())
        s1 = StubAdapter("shared_key", delay=0.3, tracker=tracker)
        s2 = StubAdapter("shared_key", delay=0.3, tracker=tracker)
        r1 = make_router({"gemini": s1}, pacer=pacer)
        r2 = make_router({"gemini": s2}, pacer=pacer)
        started = time.monotonic()
        results = await asyncio.gather(
            r1.generate(LLMRequest(tier=Tier.T0, prompt="one")),
            r2.generate(LLMRequest(tier=Tier.T0, prompt="two")),
        )
        elapsed = time.monotonic() - started
        assert all(r.status == "ok" for r in results)
        assert tracker["max"] == 1  # cross-router contention is serialised
        assert elapsed >= 0.55  # ~0.3 + ~0.3, not parallel

    run(scenario())


def test_default_routers_share_the_process_wide_pacer():
    r1 = make_router({"gemini": StubAdapter()})
    r2 = make_router({"gemini": StubAdapter()})
    assert r1._pacer is r2._pacer is get_default_pacer()


# --------------------------------------------------------------------- (b)
def test_different_providers_are_not_serialised():
    async def scenario():
        tracker = {"active": 0, "max": 0}
        pacer = SingleFlightPacer(fast_pacing())
        gem = StubAdapter("gemini_key", delay=0.3, tracker=tracker)
        oai = StubAdapter("openai_key", delay=0.3, tracker=tracker)
        r_gem = make_router({"gemini": gem}, pacer=pacer)
        r_oai = make_router({"openai": oai}, pacer=pacer)
        started = time.monotonic()
        results = await asyncio.gather(
            r_gem.generate(LLMRequest(tier=Tier.T0, prompt="g")),
            r_oai.generate(LLMRequest(tier=Tier.T0, prompt="o")),
        )
        elapsed = time.monotonic() - started
        assert all(r.status == "ok" for r in results)
        assert tracker["max"] == 2  # overlapped in flight
        assert elapsed < 0.55  # parallel, not 0.3 + 0.3

    run(scenario())


def test_different_keys_same_provider_are_not_serialised():
    async def scenario():
        tracker = {"active": 0, "max": 0}
        pacer = SingleFlightPacer(fast_pacing())
        s1 = StubAdapter("key_one", delay=0.3, tracker=tracker)
        s2 = StubAdapter("key_two", delay=0.3, tracker=tracker)
        r1 = make_router({"gemini": s1}, pacer=pacer)
        r2 = make_router({"gemini": s2}, pacer=pacer)
        started = time.monotonic()
        results = await asyncio.gather(
            r1.generate(LLMRequest(tier=Tier.T0, prompt="one")),
            r2.generate(LLMRequest(tier=Tier.T0, prompt="two")),
        )
        elapsed = time.monotonic() - started
        assert all(r.status == "ok" for r in results)
        assert tracker["max"] == 2
        assert elapsed < 0.55

    run(scenario())


# --------------------------------------------------------------------- (c)
def test_dispatched_429s_still_ledgered_and_surface_as_rate_limit():
    async def scenario():
        stub = StubAdapter(error=RateLimitError("429", provider="gemini"))
        ledger = InMemoryLLMCallLedger()
        router = make_router(
            {"gemini": stub}, ledger=ledger, pacing=fast_pacing()
        )
        with pytest.raises(RateLimitError):
            await router.generate(LLMRequest(tier=Tier.T0, prompt="boom"))
        # existing taxonomy preserved: 2 attempts on the provider, then
        # the RateLimitError surfaces — the queue does not swallow it
        assert stub.calls == 2
        assert len(ledger.entries) == stub.calls
        assert all(e.status == "failed" for e in ledger.entries)
        assert all(e.error_class == "RateLimit" for e in ledger.entries)

    run(scenario())


# --------------------------------------------------------------------- (d)
def test_max_wait_timeout_raises_and_writes_no_ledger_row():
    async def scenario():
        stub = StubAdapter(delay=0.6)
        ledger = InMemoryLLMCallLedger()
        router = make_router(
            {"gemini": stub},
            ledger=ledger,
            pacing=fast_pacing(max_wait_s=0.2),
        )
        first = asyncio.create_task(
            router.generate(LLMRequest(tier=Tier.T0, prompt="first"))
        )
        while stub.calls == 0:  # wait until call 1 is actually in flight
            await asyncio.sleep(0.01)
        with pytest.raises(QueueTimeoutError):
            await router.generate(LLMRequest(tier=Tier.T0, prompt="second"))
        result = await first
        assert result.status == "ok"
        # the timed-out call was never dispatched and fabricated no row
        assert stub.calls == 1
        assert len(ledger.entries) == 1
        assert ledger.entries[0].status == "ok"

    run(scenario())


def test_pacing_wait_beyond_max_wait_times_out_without_dispatch():
    async def scenario():
        stub = StubAdapter(delay=0.0)
        ledger = InMemoryLLMCallLedger()
        pacing = PacingConfig(
            max_wait_s=0.2,
            providers={
                "gemini": ProviderPacing(rpm=100_000.0, burst=10_000, min_interval_s=30.0)
            },
            default=FAST,
        )
        router = make_router({"gemini": stub}, ledger=ledger, pacing=pacing)
        ok = await router.generate(LLMRequest(tier=Tier.T0, prompt="first"))
        assert ok.status == "ok"
        with pytest.raises(QueueTimeoutError):
            await router.generate(LLMRequest(tier=Tier.T0, prompt="second"))
        assert stub.calls == 1
        assert len(ledger.entries) == 1

    run(scenario())


# ------------------------------------------------------- pacer unit checks
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_token_bucket_pacing_with_fake_clock():
    async def scenario():
        clock = FakeClock()
        pacer = SingleFlightPacer(
            PacingConfig(
                providers={"gemini": ProviderPacing(rpm=60.0, burst=1, min_interval_s=0.0)},
                default=FAST,
            ),
            sleep=clock.sleep,
            clock=clock,
        )
        dispatched_at = []
        for _ in range(3):
            async with pacer.slot("gemini:k", provider="gemini"):
                dispatched_at.append(clock.now)
        # burst of 1 immediate, then one per 60s/60rpm = 1.0s
        assert dispatched_at == [1000.0, 1001.0, 1002.0]
        assert clock.sleeps == [1.0, 1.0]

    run(scenario())


def test_pacing_config_defaults_are_conservative_for_gemini_free_tier():
    cfg = PacingConfig()
    gemini = cfg.for_provider("gemini")
    assert gemini.rpm <= 15.0  # free-tier Flash-class allowance
    assert gemini.min_interval_s >= 1.0
    assert cfg.max_wait_s > 0  # bounded waiting, never infinite


def test_pacing_config_from_env_overrides_and_ignores_garbage():
    cfg = PacingConfig.from_env(
        {
            "YTA_LLM_QUEUE_MAX_WAIT_S": "42",
            "YTA_LLM_GEMINI_RPM": "9",
            "YTA_LLM_GEMINI_MIN_INTERVAL_S": "2.5",
            "YTA_LLM_OPENAI_RPM": "banana",  # malformed -> default wins
        }
    )
    assert cfg.max_wait_s == 42.0
    assert cfg.for_provider("gemini").rpm == 9.0
    assert cfg.for_provider("gemini").min_interval_s == 2.5
    assert cfg.for_provider("gemini").burst == 4  # untouched default
    assert cfg.for_provider("openai").rpm == 60.0
