"""Single-flight dispatch queue with token-bucket pacing for LLM calls.

Why this exists (Round 3 finding)
---------------------------------
Parallel video productions sharing ONE provider API key fired their
generation calls concurrently.  The router's per-provider semaphore
allowed up to 8 in flight, so a burst of near-simultaneous calls hit the
provider together and produced 15+ failed HTTP 429 responses — the key
was contending with *itself*.

What this module does
---------------------
* **Single-flight per provider+key.**  Calls for the same
  ``(provider, key_ref)`` pair are serialised through a FIFO flight
  lock: at most one real provider call for that key is in flight at any
  moment.  Different providers, and the same provider under a different
  key, proceed fully independently.
* **Token-bucket pacing on top.**  Even serialised, back-to-back calls
  can exceed a free-tier RPM allowance, so dispatches for a key are
  additionally spaced by a token bucket (``rpm`` calls per minute with a
  small ``burst`` allowance) plus a minimum inter-call gap
  (``min_interval_s``).  Callers *wait their turn* instead of failing.
* **Bounded waiting.**  A call that cannot be dispatched within
  ``max_wait_s`` raises :class:`QueueTimeoutError` — it never hangs
  forever, and because it was never dispatched it writes **no** ledger
  row (the ledger records real provider calls exactly once; see the
  router).

Process-wide sharing
--------------------
The pacer is shared process-wide by default
(:func:`get_default_pacer`): each production builds its own
``LLMRouter`` (see ``app.integration.content.build_gemini_router``), so
a per-router queue could not stop cross-production contention on a
shared key.  One sidecar process = one event loop in production, and
the flight locks are kept per event loop so test suites that drive the
router through successive ``asyncio.run()`` loops never trip
loop-binding errors; the pacing bookkeeping (monotonic timestamps and
token counts) is loop-independent and shared regardless.

What this does NOT do
---------------------
The queue prevents *self-inflicted* contention only.  A 429 that still
arrives after dispatch means the provider's real quota is exhausted (or
shared with another process/account user): it is ledgered exactly like
before and surfaces through the existing taxonomy as
``RateLimitError`` — the router's retry/fallback policy decides what
happens next.  Pacing never swallows, converts, or retries-away a real
provider error on its own.

Configuration
-------------
Defaults are conservative for free-tier-class keys (Gemini free tier
is ~15 RPM for Flash-class models).  Overrides follow the repo's
existing patterns — router constructor arguments first, ``YTA_LLM_*``
environment variables for the process-wide default pacer:

==================================== ==================================
Variable                             Meaning (defaults in brackets)
==================================== ==================================
``YTA_LLM_QUEUE_MAX_WAIT_S``         max seconds a call may queue [300]
``YTA_LLM_<PROVIDER>_RPM``           token-bucket refill, calls/min
``YTA_LLM_<PROVIDER>_BURST``         bucket capacity (instant burst)
``YTA_LLM_<PROVIDER>_MIN_INTERVAL_S`` min gap between dispatch starts
``YTA_LLM_DEFAULT_RPM`` etc.         same, for unlisted providers
==================================== ==================================

``<PROVIDER>`` is the upper-cased provider name (``GEMINI``, ``OPENAI``,
``ANTHROPIC``).  Malformed values are ignored (the default wins) rather
than crashing call dispatch.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .errors import LLMError

__all__ = [
    "PacingConfig",
    "ProviderPacing",
    "QueueTimeoutError",
    "SingleFlightPacer",
    "get_default_pacer",
    "reset_default_pacer",
]


class QueueTimeoutError(LLMError):
    """A generate call waited in the dispatch queue past ``max_wait_s``.

    Raised *before* dispatch: the provider was never called for this
    attempt, so no ledger row is written and the provider circuit
    breaker is not fed.  This is deliberately distinct from
    :class:`~app.providers.llm.errors.RateLimitError`, which means a
    real call was dispatched and the provider answered 429.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        queue_key: str | None = None,
        max_wait_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.queue_key = queue_key
        self.max_wait_s = max_wait_s


@dataclass(frozen=True)
class ProviderPacing:
    """Pacing parameters for one provider (or the fallback default)."""

    rpm: float = 30.0  # token-bucket refill: calls per minute
    burst: int = 5  # bucket capacity: calls allowed back-to-back
    min_interval_s: float = 0.5  # min gap between dispatch starts

    @property
    def refill_per_s(self) -> float:
        return max(0.0, self.rpm) / 60.0


#: Conservative per-provider defaults.  Gemini is set for free-tier-class
#: keys (~15 RPM Flash-class allowance): a burst of 4 covers a short
#: sequential chain, then calls pace out at the bucket rate with at
#: least one second between dispatch starts.
DEFAULT_PROVIDER_PACING: dict[str, ProviderPacing] = {
    "gemini": ProviderPacing(rpm=15.0, burst=4, min_interval_s=1.0),
    "openai": ProviderPacing(rpm=60.0, burst=10, min_interval_s=0.2),
    "anthropic": ProviderPacing(rpm=60.0, burst=10, min_interval_s=0.2),
}

DEFAULT_PACING = ProviderPacing(rpm=30.0, burst=5, min_interval_s=0.5)
DEFAULT_MAX_WAIT_S = 300.0


def _env_float(env: Mapping[str, str], name: str) -> float | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(env: Mapping[str, str], name: str) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class PacingConfig:
    """Resolved pacing configuration for a :class:`SingleFlightPacer`."""

    max_wait_s: float = DEFAULT_MAX_WAIT_S
    providers: Mapping[str, ProviderPacing] = field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_PACING)
    )
    default: ProviderPacing = DEFAULT_PACING

    def for_provider(self, provider: str) -> ProviderPacing:
        return self.providers.get(provider, self.default)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **overrides
    ) -> "PacingConfig":
        """Build a config from ``YTA_LLM_*`` env vars (+ kwarg overrides).

        Keyword overrides (``max_wait_s=``, ``providers=``, ``default=``)
        win over the environment, which wins over the built-in defaults.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        max_wait = _env_float(source, "YTA_LLM_QUEUE_MAX_WAIT_S")
        providers: dict[str, ProviderPacing] = dict(DEFAULT_PROVIDER_PACING)
        names = set(providers) | {"default"}
        # Also honour overrides for providers not in the default table:
        # any YTA_LLM_<NAME>_RPM variable defines that provider's row.
        for var in source:
            if var.startswith("YTA_LLM_") and var.endswith("_RPM"):
                names.add(var[len("YTA_LLM_") : -len("_RPM")].lower())
        for name in names:
            prefix = f"YTA_LLM_{name.upper()}"
            base = providers.get(name, DEFAULT_PACING)
            rpm = _env_float(source, f"{prefix}_RPM")
            burst = _env_int(source, f"{prefix}_BURST")
            gap = _env_float(source, f"{prefix}_MIN_INTERVAL_S")
            if rpm is None and burst is None and gap is None:
                continue
            providers[name] = ProviderPacing(
                rpm=base.rpm if rpm is None else max(0.0, rpm),
                burst=base.burst if burst is None else max(0, burst),
                min_interval_s=(
                    base.min_interval_s if gap is None else max(0.0, gap)
                ),
            )
        cfg = cls(
            max_wait_s=DEFAULT_MAX_WAIT_S if max_wait is None else max_wait,
            providers=providers,
            default=providers.get("default", DEFAULT_PACING),
        )
        if overrides:
            cfg = cls(
                max_wait_s=overrides.get("max_wait_s", cfg.max_wait_s),
                providers=overrides.get("providers", cfg.providers),
                default=overrides.get("default", cfg.default),
            )
        return cfg


class _KeyState:
    """Mutable per-queue-key pacing state (loop-independent fields)."""

    __slots__ = ("tokens", "last_refill", "last_dispatch", "locks", "inflight", "waiting")

    def __init__(self, burst: int, now: float) -> None:
        self.tokens = float(max(0, burst))
        self.last_refill = now
        self.last_dispatch: float | None = None
        # Flight locks are per event loop: production has exactly one
        # loop; per-loop locks keep asyncio.run()-per-test suites safe.
        self.locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self.inflight = 0
        self.waiting = 0


class _Slot:
    """Async context manager returned by :meth:`SingleFlightPacer.slot`."""

    def __init__(self, pacer: "SingleFlightPacer", queue_key: str, provider: str) -> None:
        self._pacer = pacer
        self._queue_key = queue_key
        self._provider = provider
        self._state: _KeyState | None = None

    async def __aenter__(self) -> "_Slot":
        self._state = await self._pacer._acquire(self._queue_key, self._provider)
        return self

    async def __aexit__(self, *exc) -> bool:
        if self._state is not None:
            self._pacer._release(self._state)
            self._state = None
        return False


class SingleFlightPacer:
    """Serialises + paces real provider dispatches per provider+key.

    Usage (from the router's dispatch path)::

        async with pacer.slot(queue_key, provider=provider):
            result = await adapter.generate(...)

    ``queue_key`` identifies one credential: ``"<provider>:<key_ref>"``
    (a key *reference*, never the key value).  The slot covers exactly
    one dispatch attempt: enter = the call may go out now, exit = the
    provider call has finished (success or error).
    """

    def __init__(
        self,
        config: PacingConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or PacingConfig()
        self._sleep = sleep
        self._clock = clock
        self._states: dict[str, _KeyState] = {}

    # ------------------------------------------------------------------ API
    def slot(self, queue_key: str, *, provider: str) -> _Slot:
        return _Slot(self, queue_key, provider)

    def snapshot(self, queue_key: str) -> dict:
        """Introspection for tests/diagnostics (no state mutation)."""
        state = self._states.get(queue_key)
        if state is None:
            return {"exists": False, "inflight": 0, "waiting": 0, "tokens": 0.0}
        return {
            "exists": True,
            "inflight": state.inflight,
            "waiting": state.waiting,
            "tokens": state.tokens,
            "last_dispatch": state.last_dispatch,
        }

    # ------------------------------------------------------------- internals
    def _state_for(self, queue_key: str, provider: str) -> _KeyState:
        state = self._states.get(queue_key)
        if state is None:
            state = _KeyState(self.config.for_provider(provider).burst, self._clock())
            self._states[queue_key] = state
        return state

    async def _acquire(self, queue_key: str, provider: str) -> _KeyState:
        pacing = self.config.for_provider(provider)
        max_wait = max(0.0, self.config.max_wait_s)
        deadline = self._clock() + max_wait
        state = self._state_for(queue_key, provider)
        loop = asyncio.get_running_loop()
        lock = state.locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            state.locks[loop] = lock

        # 1) Single-flight: wait for the previous call on this key.
        state.waiting += 1
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise QueueTimeoutError(
                    f"LLM dispatch queue timeout: no slot for provider "
                    f"{provider!r} within {max_wait:.1f}s (queue busy)",
                    provider=provider,
                    queue_key=queue_key,
                    max_wait_s=max_wait,
                )
            try:
                await asyncio.wait_for(lock.acquire(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                raise QueueTimeoutError(
                    f"LLM dispatch queue timeout: waited {max_wait:.1f}s "
                    f"for the in-flight call on provider {provider!r} to "
                    "finish; call was not dispatched",
                    provider=provider,
                    queue_key=queue_key,
                    max_wait_s=max_wait,
                ) from None
        finally:
            state.waiting -= 1

        # 2) Pacing: token bucket + min inter-call gap, still bounded
        #    by the same deadline.  The flight lock is held throughout,
        #    which keeps dispatch order FIFO and the bookkeeping
        #    race-free on the (single) production loop.
        try:
            await self._wait_for_pacing(state, pacing, deadline, queue_key, provider)
        except BaseException:
            lock.release()
            raise
        state.inflight += 1
        return state

    async def _wait_for_pacing(
        self,
        state: _KeyState,
        pacing: ProviderPacing,
        deadline: float,
        queue_key: str,
        provider: str,
    ) -> None:
        now = self._clock()
        rate = pacing.refill_per_s
        if rate > 0:
            state.tokens = min(
                float(pacing.burst),
                state.tokens + (now - state.last_refill) * rate,
            )
        state.last_refill = now

        wait = 0.0
        if rate > 0 and state.tokens < 1.0:
            wait = max(wait, (1.0 - state.tokens) / rate)
        elif rate <= 0 and state.tokens < 1.0:
            wait = float("inf")  # rpm=0 with an empty bucket: never refills
        if state.last_dispatch is not None and pacing.min_interval_s > 0:
            wait = max(wait, state.last_dispatch + pacing.min_interval_s - now)

        if wait > 0:
            if wait == float("inf") or self._clock() + wait > deadline:
                raise QueueTimeoutError(
                    f"LLM dispatch queue timeout: pacing for provider "
                    f"{provider!r} would delay dispatch past the "
                    f"{self.config.max_wait_s:.1f}s max wait; call was "
                    "not dispatched",
                    provider=provider,
                    queue_key=queue_key,
                    max_wait_s=self.config.max_wait_s,
                )
            await self._sleep(wait)
            now = self._clock()
            if rate > 0:
                state.tokens = min(
                    float(pacing.burst),
                    state.tokens + (now - state.last_refill) * rate,
                )
                state.last_refill = now
        # Reserve this dispatch: consume one token, stamp the start.
        state.tokens -= 1.0
        state.last_dispatch = now

    def _release(self, state: _KeyState) -> None:
        state.inflight -= 1
        loop = asyncio.get_running_loop()
        lock = state.locks.get(loop)
        if lock is not None and lock.locked():
            lock.release()


# ------------------------------------------------------------ shared default
_default_pacer: SingleFlightPacer | None = None


def get_default_pacer() -> SingleFlightPacer:
    """Process-wide pacer (env-configured), shared by all routers that
    do not inject their own — this sharing across router instances is
    what stops parallel productions from contending on one key."""
    global _default_pacer
    if _default_pacer is None:
        _default_pacer = SingleFlightPacer(PacingConfig.from_env())
    return _default_pacer


def reset_default_pacer() -> None:
    """Drop the shared pacer (tests / controlled restarts)."""
    global _default_pacer
    _default_pacer = None
