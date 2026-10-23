"""LLM layer tests — all HTTP mocked via httpx MockTransport, no network."""

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from app.providers.llm import (
    AnthropicAdapter,
    BudgetExceeded,
    GeminiAdapter,
    InMemoryLLMCallLedger,
    InputTooLargeError,
    LLMRequest,
    LLMRouter,
    ModelRegistry,
    OpenAIAdapter,
    ProviderConfigError,
    Tier,
)
from app.providers.llm.ledger import LLMCallEntry, PriceSnapshot

BACKEND = __file__.rsplit("/tests/", 1)[0]
REGISTRY_PATH = f"{BACKEND}/config/models.yaml"


class FakeSecrets:
    async def get_secret(self, ref: str) -> str | None:
        return "test-key"  # never logged, never asserted on beyond presence


def run(coro):
    return asyncio.run(coro)


GEMINI_OK = {
    "candidates": [
        {"content": {"parts": [{"text": "gemini says hi"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {
        "promptTokenCount": 100,
        "candidatesTokenCount": 20,
        "cachedContentTokenCount": 10,
        "thoughtsTokenCount": 5,
    },
}

OPENAI_OK = {
    "status": "completed",
    "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "openai hi"}]}
    ],
    "usage": {
        "input_tokens": 1000,
        "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 400},
        "output_tokens_details": {"reasoning_tokens": 30},
    },
}

ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "claude hi"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 500,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
    },
}


def make_router(handlers: dict[str, callable], **router_kw) -> LLMRouter:
    """handlers: provider name -> MockTransport handler fn."""
    adapters = {}
    for name, handler in handlers.items():
        transport = httpx.MockTransport(handler)
        cls = {
            "gemini": GeminiAdapter,
            "openai": OpenAIAdapter,
            "anthropic": AnthropicAdapter,
        }[name]
        adapters[name] = cls(FakeSecrets(), f"{name}_key", transport=transport)
    router_kw.setdefault("backoff_base_s", 0.0)
    return LLMRouter(
        adapters,
        registry=ModelRegistry.load(__import__("pathlib").Path(REGISTRY_PATH)),
        **router_kw,
    )


def ok_handler(payload):
    return lambda request: httpx.Response(200, json=payload)


def test_tier_routing_picks_configured_model():
    router = make_router({"gemini": ok_handler(GEMINI_OK)})
    result = run(router.generate(LLMRequest(tier=Tier.T0, prompt="hello")))
    assert result.provider == "gemini"
    assert result.model == "gemini-3.1-flash-lite"  # T0 primary in models.yaml
    assert result.text == "gemini says hi"
    assert result.status == "ok"
    # exclusive buckets from Gemini's inclusive counts
    assert result.usage.input_uncached == 90
    assert result.usage.input_cached == 10
    assert result.usage.output == 15
    assert result.usage.reasoning == 5
    # cost: 90*.25 + 10*.025 + 20*1.50 per 1M
    assert result.cost_usd == pytest.approx((90 * 0.25 + 10 * 0.025 + 20 * 1.50) / 1e6)


def test_within_tier_fallback_on_429_after_two_attempts():
    calls = {"gemini": 0, "openai": 0}

    def gemini_429(request):
        calls["gemini"] += 1
        return httpx.Response(429, json={"error": "slow down"})

    def openai_ok(request):
        calls["openai"] += 1
        return httpx.Response(200, json=OPENAI_OK)

    router = make_router({"gemini": gemini_429, "openai": openai_ok})
    result = run(router.generate(LLMRequest(tier=Tier.T0, prompt="hello")))
    assert calls["gemini"] == 2  # exactly 2 attempts, then fall back
    assert calls["openai"] == 1
    assert result.provider == "openai"
    assert result.model == "gpt-6-luna"  # same tier T0, cross-provider
    assert result.fell_back_from == "gemini/gemini-3.1-flash-lite"


def test_401_raises_immediately_no_retry_no_fallback():
    calls = {"gemini": 0, "openai": 0}

    def gemini_401(request):
        calls["gemini"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    def openai_ok(request):
        calls["openai"] += 1
        return httpx.Response(200, json=OPENAI_OK)

    router = make_router({"gemini": gemini_401, "openai": openai_ok})
    with pytest.raises(ProviderConfigError):
        run(router.generate(LLMRequest(tier=Tier.T0, prompt="hello")))
    assert calls == {"gemini": 1, "openai": 0}


def test_openai_inclusive_cached_tokens_converted_to_exclusive():
    # only the OpenAI key configured -> router uses the T0 OpenAI entry
    router = make_router({"openai": ok_handler(OPENAI_OK)})
    result = run(router.generate(LLMRequest(tier=Tier.T0, prompt="hello")))
    assert result.model == "gpt-6-luna"
    u = result.usage
    assert u.input_cached == 400
    assert u.input_uncached == 600  # 1000 inclusive - 400 cached, NOT 1000
    assert u.reasoning == 30
    assert u.output == 70  # 100 - 30 reasoning, exclusive
    expected = (600 * 0.10 + 400 * 0.01 + 70 * 0.50 + 30 * 0.50) / 1e6
    assert result.cost_usd == pytest.approx(expected)


def test_100k_ceiling_rejected():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=GEMINI_OK)

    router = make_router({"gemini": handler})
    req = LLMRequest(tier=Tier.T1, prompt="x", input_token_estimate=100_001)
    with pytest.raises(InputTooLargeError):
        run(router.generate(req))
    assert calls["n"] == 0  # rejected before any provider call


def test_per_video_cap_raises_budget_exceeded():
    async def scenario():
        ledger = InMemoryLLMCallLedger()
        await ledger.append(
            LLMCallEntry(
                agent_id="writer",
                project_id="video-1",
                provider="gemini",
                model="gemini-3.1-flash-lite",
                tier="T0",
                price_snapshot=PriceSnapshot(
                    registry_version="test",
                    input_price_per_1m=0.25,
                    output_price_per_1m=1.50,
                    cached_input_price_per_1m=0.025,
                ),
                cost_usd=0.60,  # over the $0.50 per-video cap
            )
        )
        router = make_router({"gemini": ok_handler(GEMINI_OK)}, ledger=ledger)
        with pytest.raises(BudgetExceeded):
            await router.generate(
                LLMRequest(tier=Tier.T0, prompt="hi", project_id="video-1")
            )
        # a different video is unaffected
        ok = await router.generate(
            LLMRequest(tier=Tier.T0, prompt="hi", project_id="video-2")
        )
        assert ok.status == "ok"

    run(scenario())


class Outline(BaseModel):
    title: str
    sections: int


def test_structured_output_repair_retry_succeeds():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            payload = dict(GEMINI_OK)
            payload["candidates"] = [
                {
                    "content": {"parts": [{"text": "not json at all"}]},
                    "finishReason": "STOP",
                }
            ]
            return httpx.Response(200, json=payload)
        payload = dict(GEMINI_OK)
        payload["candidates"] = [
            {
                "content": {
                    "parts": [
                        {"text": json.dumps({"title": "T", "sections": 3})}
                    ]
                },
                "finishReason": "STOP",
            }
        ]
        return httpx.Response(200, json=payload)

    router = make_router({"gemini": flaky})
    req = LLMRequest(
        tier=Tier.T1, prompt="outline please", output_schema=Outline
    )
    result = run(router.generate(req))
    assert calls["n"] == 2  # one repair retry, same model
    assert isinstance(result.parsed, Outline)
    assert result.parsed.title == "T"
    assert result.parsed.sections == 3


GEMINI_MODELS_PAGE_1 = {
    "models": [
        {"name": "models/gemini-3.1-flash-lite"},
        {"name": "models/gemini-3.8-flash"},
    ],
    "nextPageToken": "page-2",
}

GEMINI_MODELS_PAGE_2 = {
    "models": [{"name": "models/gemini-3.1-pro-preview"}],
}


def make_gemini_adapter(handler) -> GeminiAdapter:
    transport = httpx.MockTransport(handler)
    return GeminiAdapter(FakeSecrets(), "gemini_key", transport=transport)


def test_gemini_list_models_returns_names_and_sends_key_header():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["has_key_header"] = "x-goog-api-key" in request.headers
        return httpx.Response(200, json=GEMINI_MODELS_PAGE_2)

    names = run(make_gemini_adapter(handler).list_models())
    assert names == ["models/gemini-3.1-pro-preview"]
    assert seen == {
        "method": "GET",
        "path": "/v1beta/models",
        "has_key_header": True,
    }


def test_gemini_list_models_follows_pagination():
    def handler(request):
        if request.url.params.get("pageToken") == "page-2":
            return httpx.Response(200, json=GEMINI_MODELS_PAGE_2)
        return httpx.Response(200, json=GEMINI_MODELS_PAGE_1)

    names = run(make_gemini_adapter(handler).list_models())
    assert names == [
        "models/gemini-3.1-flash-lite",
        "models/gemini-3.8-flash",
        "models/gemini-3.1-pro-preview",
    ]


def test_gemini_list_models_auth_error_maps_to_config_error():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(ProviderConfigError) as excinfo:
        run(make_gemini_adapter(handler).list_models())
    assert excinfo.value.status_code == 401


def test_registry_seed_prices():
    handle = ModelRegistry.load(__import__("pathlib").Path(REGISTRY_PATH))
    reg = handle.registry
    assert reg.verified_at == "2026-10-22"
    t3 = reg.tier(Tier.T3).primary
    assert (t3.provider, t3.model_id) == ("anthropic", "claude-opus-5.5")
    assert (t3.input_price_per_1m, t3.output_price_per_1m) == (4.00, 20.00)
    t1_gemini = reg.tier(Tier.T1).primary
    assert t1_gemini.price_expires is not None  # intro rate expiry tracked
    assert t1_gemini.prices_on(__import__("datetime").date(2027, 2, 1))[:2] == (
        1.50,
        7.50,
    )
