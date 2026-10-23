"""LLMRouter — the LLMProvider agents actually talk to.

Agents request TIERS only. The router maps tier -> ordered registry chain,
enforces the 100K ceiling and the hard spend caps, applies per-provider
rate limiting / circuit breaking, serialises + paces real dispatches per
provider+key through the single-flight queue (see ``.pacing``), falls
back WITHIN TIER across providers
(never across tiers; sole exception: a T3 final pass may degrade to T2 with
``degraded_from_t3=True``), validates structured output with one repair
retry then one tier escalation, and writes EVERY attempt to the ledger
before any result reaches an agent.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import random
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from .cache import LocalResultCache
from .caps import (
    AGENT_LOOP_CAP_USD,
    AGENT_LOOP_MAX_CALLS,
    PER_VIDEO_CAP_USD,
    CapChecker,
    default_cap_checker,
)
from .errors import (
    AbortedPartialError,
    BudgetExceeded,
    CircuitOpenError,
    InputTooLargeError,
    LLMError,
    ModelRegistryError,
    PolicyRefusal,
    ProviderConfigError,
    RateLimitError,
    SchemaValidationError,
    TransientProviderError,
)
from .ledger import InMemoryLLMCallLedger, LLMCallEntry, LLMCallLedger, PriceSnapshot
from .model_validation import ensure_model_registered_and_priced
from .models import MAX_INPUT_TOKENS, LLMRequest, LLMResult, Message, Tier
from .pacing import (
    PacingConfig,
    QueueTimeoutError,
    SingleFlightPacer,
    get_default_pacer,
)
from .registry import ModelEntry, ModelRegistry, RegistryHandle
from .reliability import ProviderRuntime

DEFAULT_CONCURRENCY = {"gemini": 8, "openai": 6, "anthropic": 6}
RATE_LIMIT_ATTEMPTS = 2  # 429: fall back after 2 attempts on one provider
TRANSIENT_ATTEMPTS = 3  # 5xx/timeout: max 3 attempts on one provider
_TIER_ORDER = [Tier.T0, Tier.T1, Tier.T2, Tier.T3]

#: Logical-call identity of the generate() invocation currently running
#: in this task/context (Round 4, see ledger.py). Set once per
#: generate() in LLMRouter.generate and stamped on every ledger row the
#: invocation writes, so retries/fallbacks/repairs of one logical call
#: share one id and the agent-loop cap can count UNIQUE calls. A
#: ContextVar keeps concurrent generate() calls on one shared router
#: from cross-stamping each other.
_CURRENT_LOGICAL_CALL_ID: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("yta_llm_logical_call_id", default=None)
)


class _Exhausted(Exception):
    """One provider entry used up its retry budget; fall back."""

    def __init__(self, error: LLMError) -> None:
        self.error = error


class _SchemaFailed(Exception):
    """Structured output failed validation even after the repair retry."""


class LLMRouter:
    def __init__(
        self,
        adapters: dict[str, object],
        registry: RegistryHandle | ModelRegistry | Path | None = None,
        ledger: LLMCallLedger | None = None,
        *,
        cap_checker: CapChecker | None = None,
        per_video_cap_usd: float = PER_VIDEO_CAP_USD,
        agent_loop_max_calls: int = AGENT_LOOP_MAX_CALLS,
        agent_loop_cap_usd: float = AGENT_LOOP_CAP_USD,
        backoff_base_s: float = 1.0,
        sleep=asyncio.sleep,
        pacer: SingleFlightPacer | None = None,
        pacing: PacingConfig | None = None,
        price_catalog: object | None = None,
    ) -> None:
        self.adapters = dict(adapters)  # provider name -> adapter
        if isinstance(registry, RegistryHandle) or registry is None:
            self._handle = registry or ModelRegistry.load()
            self._static: ModelRegistry | None = None
        elif isinstance(registry, ModelRegistry):
            self._handle = None
            self._static = registry
        else:  # Path
            self._handle = ModelRegistry.load(Path(registry))
            self._static = None
        self.ledger: LLMCallLedger = ledger or InMemoryLLMCallLedger()
        self._cap_checker = cap_checker
        self.per_video_cap_usd = per_video_cap_usd
        self.agent_loop_max_calls = agent_loop_max_calls
        self.agent_loop_cap_usd = agent_loop_cap_usd
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep
        # Single-flight dispatch queue + pacing (app.providers.llm.pacing).
        # An injected pacer wins; a bare PacingConfig gets a private pacer
        # (tests / isolated callers); otherwise the PROCESS-WIDE default
        # pacer is shared — parallel productions each build their own
        # router, and only a shared pacer stops them contending on one
        # provider key.
        if pacer is not None:
            self._pacer = pacer
        elif pacing is not None:
            self._pacer = SingleFlightPacer(pacing, sleep=sleep)
        else:
            self._pacer = get_default_pacer()
        self.cache = LocalResultCache()
        self._runtimes: dict[str, ProviderRuntime] = {}
        # Price catalog for named-model validation (model_validation.py).
        # Injected catalog wins (tests); otherwise the default
        # config/prices.yaml catalog is loaded lazily on first use and
        # cached — and ONLY consulted when a request names a model, so
        # tier-only routing never depends on it.
        self._price_catalog_injected = price_catalog
        self._price_catalog_cache: object | None = None
        self._price_catalog_loaded = False

    # ------------------------------------------------------------------ util
    def _registry(self) -> ModelRegistry:
        return self._handle.reload_if_changed() if self._handle else self._static

    def _price_catalog(self) -> object | None:
        """The price catalog for named-model checks (None if unloadable)."""
        if self._price_catalog_injected is not None:
            return self._price_catalog_injected
        if not self._price_catalog_loaded:
            self._price_catalog_loaded = True
            try:
                from ..budget.catalog import PriceCatalog

                self._price_catalog_cache = PriceCatalog.load()
            except Exception:
                self._price_catalog_cache = None
        return self._price_catalog_cache

    def _validate_named_model(self, req: LLMRequest) -> None:
        """Refuse a caller-named model that is not registered AND priced.

        Runs before cache, caps and any provider call (see
        model_validation.py for the Round 3 incident this closes).
        """
        assert req.model_id is not None
        ensure_model_registered_and_priced(
            self._registry(),
            self._price_catalog(),
            model_id=req.model_id,
            provider=req.model_provider,
            tier=req.tier,
        )

    def _runtime(self, provider: str) -> ProviderRuntime:
        return self._runtimes.setdefault(
            provider,
            ProviderRuntime(DEFAULT_CONCURRENCY.get(provider, 4)),
        )

    def _queue_key(self, provider: str) -> str:
        """Dispatch-queue identity for one provider credential.

        ``"<provider>:<key_ref>"`` when the adapter carries a key
        reference (a ref string like ``"apikey/gemini"`` — never the key
        value), else just the provider name.  Same provider + same key
        serialises; a different key_ref gets an independent queue.
        """
        adapter = self.adapters.get(provider)
        key_ref = getattr(adapter, "_key_ref", None) or getattr(
            adapter, "key_ref", None
        )
        return f"{provider}:{key_ref}" if key_ref else provider

    def _timeout_for(self, req: LLMRequest) -> float:
        if req.timeout_s is not None:
            return req.timeout_s
        if req.tier == Tier.T0:
            return 15.0
        if req.is_script_class:
            return 120.0
        return 60.0

    async def _check_caps(self, req: LLMRequest) -> None:
        if self._cap_checker is not None:
            await self._cap_checker(req, self.ledger)
        else:
            await default_cap_checker(
                req,
                self.ledger,
                per_video_cap_usd=self.per_video_cap_usd,
                agent_loop_max_calls=self.agent_loop_max_calls,
                agent_loop_cap_usd=self.agent_loop_cap_usd,
            )

    async def _ledger_row(
        self,
        req: LLMRequest,
        entry: ModelEntry,
        *,
        status: str,
        usage=None,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        attempts: int = 1,
        error_class: str | None = None,
        fell_back_from: str | None = None,
        degraded: bool = False,
    ) -> None:
        inp, out, cached = entry.prices_on()
        u = usage
        await self.ledger.append(
            LLMCallEntry(
                logical_call_id=(
                    _CURRENT_LOGICAL_CALL_ID.get() or req.logical_call_id
                ),
                agent_id=req.agent_id,
                project_id=req.effective_project_id,
                video_id=req.video_id,
                channel_id=req.channel_id,
                task=req.task,
                provider=entry.provider,
                model=entry.model_id,
                tier=req.tier.value,
                input_uncached=u.input_uncached if u else 0,
                input_cached=u.input_cached if u else 0,
                input_cache_write=u.input_cache_write if u else 0,
                output=u.output if u else 0,
                reasoning=u.reasoning if u else 0,
                price_snapshot=PriceSnapshot(
                    registry_version=self._registry().registry_version,
                    input_price_per_1m=inp,
                    output_price_per_1m=out,
                    cached_input_price_per_1m=cached,
                ),
                cost_usd=cost_usd,
                prompt_version=req.prompt_version,
                prompt_hash=req.prompt_hash(),
                fell_back_from=fell_back_from,
                status=status,
                latency_ms=latency_ms,
                attempts=attempts,
                error_class=error_class,
                degraded_from_t3=degraded,
            )
        )

    # -------------------------------------------------------------- generate
    async def generate(self, req: LLMRequest) -> LLMResult:
        # Named-model validation FIRST: a request naming a model that is
        # not in BOTH the registry and the price catalog is refused
        # before cache, caps and any provider HTTP call (Round 4).
        if req.model_id is not None:
            self._validate_named_model(req)
        # One logical-call id for this whole invocation: every ledger row
        # written below (retries, fallbacks, repair, escalation) carries
        # it, so the agent-loop cap counts this as ONE call. A caller
        # supplied req.logical_call_id (stable step/chapter key) wins,
        # which also dedupes orchestration-level re-issues of the call.
        token = _CURRENT_LOGICAL_CALL_ID.set(
            req.logical_call_id or str(uuid.uuid4())
        )
        try:
            return await self._generate_inner(req)
        finally:
            _CURRENT_LOGICAL_CALL_ID.reset(token)

    async def _generate_inner(self, req: LLMRequest) -> LLMResult:
        if req.estimated_input_tokens() > MAX_INPUT_TOKENS:
            raise InputTooLargeError(
                f"input estimate {req.estimated_input_tokens()} tokens "
                f"exceeds {MAX_INPUT_TOKENS} ceiling; chunk (map-reduce) first"
            )

        cached = self.cache.get(req)
        if cached is not None:
            return cached

        await self._check_caps(req)

        try:
            result = await self._run_chain(req, degraded=False)
        except _SchemaFailed:
            # schema failure: one tier escalation, then agent-level failure
            if req.tier == Tier.T3:
                raise SchemaValidationError(
                    "structured output failed validation at T3 after repair"
                ) from None
            escalated = _TIER_ORDER[_TIER_ORDER.index(req.tier) + 1]
            try:
                # The named-model demand applied to the requested tier;
                # the router's own escalation uses the escalated tier's
                # registered chain, so the pin is dropped here.
                result = await self._run_chain(
                    req.model_copy(
                        update={
                            "tier": escalated,
                            "model_id": None,
                            "model_provider": None,
                        }
                    ),
                    degraded=False,
                )
            except _SchemaFailed:
                raise SchemaValidationError(
                    "structured output failed validation after repair + "
                    f"escalation to {escalated.value}"
                ) from None
        except _Exhausted as exc:
            # T3 final-pass degrade exception -> T2, manifest-flagged
            if req.tier == Tier.T3 and (
                req.allow_degrade_from_t3 or req.task == "script.final_pass"
            ):
                result = await self._run_chain(
                    req.model_copy(
                        update={
                            "tier": Tier.T2,
                            "model_id": None,
                            "model_provider": None,
                        }
                    ),
                    degraded=True,
                )
            else:
                raise exc.error

        self.cache.put(req, result)
        return result

    # ----------------------------------------------------------------- chain
    async def _run_chain(self, req: LLMRequest, *, degraded: bool) -> LLMResult:
        cfg = self._registry().tier(req.tier)
        entries = [e for e in cfg.models if e.provider in self.adapters]
        if req.model_id is not None:
            # Named-model resolution (validated in generate()): the
            # chain is narrowed to exactly the named registry entry —
            # no other model in the tier may serve this call.
            entries = [
                e
                for e in entries
                if e.model_id == req.model_id
                and (req.model_provider is None or e.provider == req.model_provider)
            ]
        if not entries:
            raise ProviderConfigError(
                f"no configured provider for tier {req.tier.value} "
                "(no API keys set)",
                status_code=401,
            )
        attempts = {"n": 0}
        first_failed: str | None = None
        last_error: LLMError | None = None
        all_open = True
        for entry in entries:
            runtime = self._runtime(entry.provider)
            if runtime.breaker_for(entry.model_id).is_open:
                first_failed = first_failed or f"{entry.provider}/{entry.model_id}"
                continue
            all_open = False
            try:
                result = await self._attempt_entry(
                    req, entry, attempts, degraded=degraded
                )
            except _Exhausted as exc:
                first_failed = first_failed or f"{entry.provider}/{entry.model_id}"
                last_error = exc.error
                continue
            if first_failed is not None:
                result.fell_back_from = first_failed
            result.attempts = attempts["n"]
            result.degraded_from_t3 = degraded
            return result
        if last_error is not None:
            raise _Exhausted(last_error)
        if all_open:
            raise CircuitOpenError(
                f"all models for tier {req.tier.value} are circuit-broken"
            )
        raise CircuitOpenError(f"tier {req.tier.value} chain exhausted")

    async def _attempt_entry(
        self,
        req: LLMRequest,
        entry: ModelEntry,
        attempts: dict,
        *,
        degraded: bool,
    ) -> LLMResult:
        """One provider entry: retries + schema repair. Raises _Exhausted
        when this entry's budget is spent, fatal errors directly."""
        current_req = req
        repaired = False
        while True:
            result = await self._call_with_retries(
                current_req, entry, attempts, degraded=degraded
            )
            # authoritative cost from registry prices at call time
            result.cost_usd = entry.cost_usd(result.usage)
            if req.output_schema is None:
                await self._ledger_row(
                    req, entry, status="ok", usage=result.usage,
                    cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                    attempts=attempts["n"], degraded=degraded,
                )
                return result
            try:
                self._parse_structured(req, result)
            except _SchemaFailed:
                await self._ledger_row(
                    req, entry, status="failed", usage=result.usage,
                    cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                    attempts=attempts["n"], error_class="SchemaValidation",
                    degraded=degraded,
                )
                if repaired:
                    raise _SchemaFailed() from None
                repaired = True
                current_req = self._repair_request(req, result)
                continue
            await self._ledger_row(
                req, entry, status="ok", usage=result.usage,
                cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                attempts=attempts["n"], degraded=degraded,
            )
            return result

    @staticmethod
    def _parse_structured(req: LLMRequest, result: LLMResult) -> None:
        schema = req.output_schema
        assert schema is not None
        try:
            data = json.loads(result.text)
            result.parsed = schema.model_validate(data)
        except (ValueError, ValidationError) as exc:
            raise _SchemaFailed() from exc

    @staticmethod
    def _repair_request(req: LLMRequest, bad: LLMResult) -> LLMRequest:
        msgs = req.normalised_messages() + [
            Message(role="assistant", content=bad.text),
            Message(
                role="user",
                content=(
                    "Your previous output failed schema validation. Return "
                    "ONLY corrected JSON matching the required schema, no "
                    "commentary."
                ),
            ),
        ]
        return req.model_copy(update={"messages": msgs, "prompt": None})

    # ------------------------------------------------------------ one entry
    async def _call_with_retries(
        self,
        req: LLMRequest,
        entry: ModelEntry,
        attempts: dict,
        *,
        degraded: bool,
    ) -> LLMResult:
        adapter = self.adapters[entry.provider]
        runtime = self._runtime(entry.provider)
        breaker = runtime.breaker_for(entry.model_id)
        timeout_s = self._timeout_for(req)
        queue_key = self._queue_key(entry.provider)
        rate_attempts = 0
        transient_attempts = 0
        while True:
            started = time.monotonic()
            try:
                # Single-flight + pacing (llm.pacing): serialise per
                # provider+key and space dispatch starts so one shared
                # key cannot 429 itself.  The slot covers exactly one
                # dispatch attempt — retries re-queue like everyone else.
                # QueueTimeoutError is raised BEFORE dispatch (see the
                # except clause below).  A 429 raised by adapter.generate
                # itself is a REAL provider response: it is ledgered
                # below and surfaces as RateLimitError exactly as before
                # — the queue prevents self-inflicted contention, it
                # does not hide genuine quota exhaustion.
                async with self._pacer.slot(queue_key, provider=entry.provider):
                    attempts["n"] += 1  # counts real dispatches only
                    started = time.monotonic()
                    async with runtime.semaphore:
                        await runtime.limiter.acquire()
                        result = await adapter.generate(
                            req, entry, timeout_s=timeout_s
                        )
                breaker.record_success()
                return result
            except QueueTimeoutError:
                # Never dispatched (queue wait exceeded max_wait_s):
                # write NO ledger row — nothing was spent or attempted
                # against the provider — do not feed the breaker, and
                # do not retry here; the caller decides whether to
                # re-queue the whole request at a higher level.
                raise
            except RateLimitError as exc:
                breaker.record_failure()
                runtime.limiter.on_rate_limited()  # AIMD halving
                rate_attempts += 1
                await self._ledger_row(
                    req, entry, status="failed",
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts["n"], error_class="RateLimit",
                    degraded=degraded,
                )
                if rate_attempts >= RATE_LIMIT_ATTEMPTS:
                    raise _Exhausted(exc) from exc
                delay = (
                    exc.retry_after_s
                    if exc.retry_after_s is not None
                    else self._backoff(rate_attempts)
                )
                await self._sleep(delay)
            except TransientProviderError as exc:
                breaker.record_failure()
                transient_attempts += 1
                await self._ledger_row(
                    req, entry, status="failed",
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts["n"], error_class="TransientProvider",
                    degraded=degraded,
                )
                if transient_attempts >= TRANSIENT_ATTEMPTS:
                    raise _Exhausted(exc) from exc
                await self._sleep(self._backoff(transient_attempts))
            except PolicyRefusal as exc:
                await self._ledger_row(
                    req, entry, status="refused",
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts["n"], error_class="PolicyRefusal",
                    degraded=degraded,
                )
                raise
            except AbortedPartialError as exc:
                # no duplicate spend: never restarted elsewhere
                usage = exc.usage
                cost = entry.cost_usd(usage) if usage else 0.0
                await self._ledger_row(
                    req, entry, status="aborted_partial", usage=usage,
                    cost_usd=cost,
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts["n"], error_class="AbortedPartial",
                    degraded=degraded,
                )
                raise
            except (ProviderConfigError, ModelRegistryError) as exc:
                # 400/401/403/404: NEVER retry, NEVER fall back
                await self._ledger_row(
                    req, entry, status="failed",
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts["n"],
                    error_class=type(exc).__name__,
                    degraded=degraded,
                )
                raise

    def _backoff(self, attempt: int) -> float:
        cap = min(60.0, self.backoff_base_s * (2 ** (attempt - 1)))
        return random.uniform(0, cap) if cap > 0 else 0.0
