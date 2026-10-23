"""Named-model validation tests (Round 4, Part A).

Round 3 incident: unregistered model 'gemini-3.6-flash' ran in
production and could only be priced by analogy to 'gemini-3.8-flash'.
The router must now REFUSE any call naming a model that is not present
in BOTH config/models.yaml (registry) and config/prices.yaml (price
catalog), raising ProviderConfigError BEFORE any provider call.

All provider calls are stub adapters — no network, no keys.
"""

import asyncio
from pathlib import Path

import pytest

from app.providers.llm import (
    InMemoryLLMCallLedger,
    LLMRequest,
    LLMResult,
    LLMRouter,
    ModelRegistry,
    ProviderConfigError,
    Tier,
    UsageBuckets,
    audit_registry_against_catalog,
    catalog_has_llm_price,
)
from app.providers.llm.pacing import PacingConfig, ProviderPacing
from app.providers.budget.catalog import PriceCatalog

BACKEND = __file__.rsplit("/tests/", 1)[0]
REGISTRY_PATH = f"{BACKEND}/config/models.yaml"

FAST = ProviderPacing(rpm=100_000.0, burst=10_000, min_interval_s=0.0)


def run(coro):
    return asyncio.run(coro)


def fast_pacing() -> PacingConfig:
    return PacingConfig(providers={}, default=FAST)


class StubAdapter:
    """Records every (provider, model) it was asked to generate with."""

    def __init__(self, key_ref: str = "stub_key") -> None:
        self._key_ref = key_ref
        self.calls: list[tuple[str, str]] = []

    async def generate(self, req, entry=None, *, timeout_s: float = 60.0):
        self.calls.append((entry.provider, entry.model_id))
        return LLMResult(
            text="ok",
            provider=entry.provider,
            model=entry.model_id,
            tier=req.tier,
            usage=UsageBuckets(input_uncached=10, output=5),
        )


class EmptyCatalog:
    """Price-catalog double with no rows at all."""

    def find(self, provider, model_or_service, unit=None, billing_route=None):
        return []


def make_router(adapters, **router_kw) -> LLMRouter:
    router_kw.setdefault("backoff_base_s", 0.0)
    router_kw.setdefault("pacing", fast_pacing())
    return LLMRouter(
        adapters,
        registry=ModelRegistry.load(Path(REGISTRY_PATH)),
        **router_kw,
    )


# ---------------------------------------------------------------------------
# Refusals — zero provider calls, error names the offending model
# ---------------------------------------------------------------------------

def test_unregistered_model_refused_before_any_provider_call():
    gemini = StubAdapter()
    ledger = InMemoryLLMCallLedger()
    router = make_router({"gemini": gemini}, ledger=ledger)
    req = LLMRequest(
        tier=Tier.T1, prompt="hello", model_id="gemini-3.6-flash"
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        run(router.generate(req))
    assert "gemini-3.6-flash" in str(excinfo.value)
    assert gemini.calls == []  # NO provider HTTP call was made
    assert ledger.entries == []  # and nothing was ledgered as an attempt


def test_registry_model_missing_price_refused():
    # gpt-6-luna IS in models.yaml (T0) but has NO row in prices.yaml.
    openai = StubAdapter()
    router = make_router({"openai": openai})
    req = LLMRequest(tier=Tier.T0, prompt="hello", model_id="gpt-6-luna")
    with pytest.raises(ProviderConfigError) as excinfo:
        run(router.generate(req))
    assert "gpt-6-luna" in str(excinfo.value)
    assert openai.calls == []


def test_model_registered_for_other_tier_refused():
    # gemini-3.8-flash is registered (and priced) — but for T1, not T0.
    # A named model must not silently substitute tiers.
    gemini = StubAdapter()
    router = make_router({"gemini": gemini})
    req = LLMRequest(
        tier=Tier.T0, prompt="hello", model_id="gemini-3.8-flash"
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        run(router.generate(req))
    assert "gemini-3.8-flash" in str(excinfo.value)
    assert gemini.calls == []


def test_named_model_refused_when_catalog_has_no_prices_at_all():
    # Proves the price catalog is genuinely consulted: with an empty
    # injected catalog even the fully-registered flash-lite is refused.
    gemini = StubAdapter()
    router = make_router({"gemini": gemini}, price_catalog=EmptyCatalog())
    req = LLMRequest(
        tier=Tier.T0, prompt="hello", model_id="gemini-3.1-flash-lite"
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        run(router.generate(req))
    assert "gemini-3.1-flash-lite" in str(excinfo.value)
    assert gemini.calls == []


# ---------------------------------------------------------------------------
# Allowed paths
# ---------------------------------------------------------------------------

def test_registered_and_priced_model_still_works():
    gemini = StubAdapter()
    router = make_router({"gemini": gemini})
    result = run(
        router.generate(
            LLMRequest(
                tier=Tier.T0,
                prompt="hello",
                model_id="gemini-3.1-flash-lite",
            )
        )
    )
    assert result.status == "ok"
    assert result.model == "gemini-3.1-flash-lite"
    assert gemini.calls == [("gemini", "gemini-3.1-flash-lite")]


def test_named_model_pins_chain_to_exactly_that_model(tmp_path):
    # Custom registry whose T0 primary is 3.8-flash, flash-lite second.
    # A tier-only call uses the primary; a call NAMING flash-lite must
    # be served by flash-lite and nothing else.
    registry_yaml = tmp_path / "models.yaml"
    registry_yaml.write_text(
        'registry_version: "test"\n'
        'verified_at: "2026-10-22"\n'
        "tiers:\n"
        "  T0:\n"
        "    models:\n"
        "      - provider: gemini\n"
        "        model_id: gemini-3.8-flash\n"
        "        input_price_per_1m: 0.75\n"
        "        output_price_per_1m: 3.75\n"
        "        cached_input_price_per_1m: 0.075\n"
        "      - provider: gemini\n"
        "        model_id: gemini-3.1-flash-lite\n"
        "        input_price_per_1m: 0.25\n"
        "        output_price_per_1m: 1.50\n"
        "        cached_input_price_per_1m: 0.025\n",
        encoding="utf-8",
    )
    gemini = StubAdapter()
    router = LLMRouter(
        {"gemini": gemini},
        registry=ModelRegistry.load(registry_yaml),
        backoff_base_s=0.0,
        pacing=fast_pacing(),
    )
    tier_only = run(router.generate(LLMRequest(tier=Tier.T0, prompt="hi")))
    assert tier_only.model == "gemini-3.8-flash"

    named = run(
        router.generate(
            LLMRequest(
                tier=Tier.T0,
                prompt="hi",
                model_id="gemini-3.1-flash-lite",
            )
        )
    )
    assert named.model == "gemini-3.1-flash-lite"
    assert gemini.calls == [
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "gemini-3.1-flash-lite"),
    ]


def test_tier_only_calls_never_consult_price_catalog():
    # Tier-only routing (the normal agent path) must be unaffected by
    # the named-model hardening — even with an empty catalog injected.
    gemini = StubAdapter()
    router = make_router({"gemini": gemini}, price_catalog=EmptyCatalog())
    result = run(router.generate(LLMRequest(tier=Tier.T0, prompt="hello")))
    assert result.status == "ok"
    assert result.model == "gemini-3.1-flash-lite"


# ---------------------------------------------------------------------------
# Catalog / audit helpers — exact matching only, no analogy
# ---------------------------------------------------------------------------

def test_catalog_price_lookup_is_exact_no_analogy():
    catalog = PriceCatalog.load()
    # Registry provider 'gemini' maps to catalog provider 'google'.
    assert catalog_has_llm_price(catalog, "gemini", "gemini-3.1-flash-lite")
    assert catalog_has_llm_price(catalog, "gemini", "gemini-3.8-flash")
    # The Round 3 model: no price, and 3.8-flash prices must NOT cover it.
    assert not catalog_has_llm_price(catalog, "gemini", "gemini-3.6-flash")
    # '-preview' is a different model: gemini-3.1-pro must NOT price it.
    assert not catalog_has_llm_price(
        catalog, "gemini", "gemini-3.1-pro-preview"
    )
    # Registry-only model with no catalog rows at all.
    assert not catalog_has_llm_price(catalog, "openai", "gpt-6-luna")


def test_audit_lists_registry_models_missing_prices():
    registry = ModelRegistry.load(Path(REGISTRY_PATH)).registry
    gaps = audit_registry_against_catalog(registry, PriceCatalog.load())
    joined = "\n".join(gaps)
    assert "gpt-6-luna" in joined
    assert "claude-opus-5.5" in joined
    assert "gemini-3.1-flash-lite" not in joined
    assert "gemini-3.8-flash" not in joined
