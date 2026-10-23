"""Round 3 — live Gemini wiring for the content stage.

All network is faked: the SecretProvider is a dict-backed stub, the
LLM is either a recording fake implementing the ``LLMProvider``
protocol or the REAL ``LLMRouter`` + ``GeminiAdapter`` over an
``httpx.MockTransport``.  These tests pin the contract:

* no resolvable key  -> deterministic stub bundle, provenance
  ``"deterministic-stub"``, zero live calls;
* key resolvable     -> live generation through the prompt-assembly
  agent layer, provenance ``"gemini-live"``, real models / usage /
  cost aggregated on the bundle;
* tier design         -> outline T1, chapters T1, final pass T2
  (the Gemini Pro-class slot in config/models.yaml);
* Budget Guard        -> the router's ledger records every live call
  (caps/ledger are exercised, never bypassed);
* orchestrator        -> ``produce`` surfaces the bundle's provenance
  in ``paths_used["llm"]``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.integration import content as content_mod
from app.integration import produce
from app.integration.content import (
    GEMINI_KEY_REF,
    LLM_PATH_GEMINI_LIVE,
    LLM_PATH_STUB,
    build_content,
    build_content_live,
    build_gemini_router,
    resolve_llm_path,
)
from app.integration.models import ProjectSpec
from app.niches.builtins import get_profile
from app.providers.llm import InMemoryLLMCallLedger
from app.providers.llm.models import LLMResult, Tier, UsageBuckets


def run(coro):
    return asyncio.run(coro)


def _spec(**overrides) -> ProjectSpec:
    data = dict(niche="facts", topic="Samundar ki gehrai ke raaz", minutes=2.0)
    data.update(overrides)
    return ProjectSpec(**data)


class FakeSecrets:
    """SecretProvider stub: records refs asked, returns a fixed value."""

    def __init__(self, value: str | None):
        self.value = value
        self.refs: list[str] = []

    async def get_secret(self, ref: str) -> str | None:
        self.refs.append(ref)
        return self.value


class RecordingLLM:
    """LLMProvider fake: records requests, returns chapter-shaped text."""

    def __init__(self):
        self.requests = []

    async def generate(self, req) -> LLMResult:
        self.requests.append(req)
        if req.task == "outline":
            text = "Outline: hook, chapters, climax, conclusion."
            model = "gemini-3.8-flash"
        elif req.task == "script.final_pass":
            # Echo a parseable full script: reuse the draft embedded in
            # the prompt so the chapter count matches the plan.
            draft = req.prompt.split("Draft script:\n", 1)[-1]
            text = draft
            model = "gemini-3.1-pro-preview"
        else:
            text = (
                "Ye chapter Samundar ki gehrai ke baare mein ek hairan kun "
                "baat batata hai jo samajh ko gehra karti hai aur aage ki "
                "kahani ke liye sawal khara karti hai."
            )
            model = "gemini-3.8-flash"
        return LLMResult(
            text=text,
            provider="gemini",
            model=model,
            tier=req.tier,
            usage=UsageBuckets(input_uncached=100, output=40),
            cost_usd=0.001,
            prompt_version=req.prompt_version,
        )


# ---------------------------------------------------------------------------
# Switch: key availability decides the path
# ---------------------------------------------------------------------------

def test_no_key_selects_deterministic_stub():
    secrets = FakeSecrets(None)
    spec, profile = _spec(), get_profile("facts")
    bundle = run(build_content_live(spec, profile, secret_provider=secrets))
    assert bundle.llm_path == LLM_PATH_STUB == "deterministic-stub"
    assert bundle.llm_models == []
    assert bundle.llm_cost_usd == 0.0
    # Identical to the pure offline builder.
    assert bundle.narration_text == build_content(spec, profile).narration_text
    assert secrets.refs and all(r == GEMINI_KEY_REF for r in secrets.refs)


def test_empty_key_counts_as_no_key():
    bundle = run(
        build_content_live(_spec(), get_profile("facts"), secret_provider=FakeSecrets("  "))
    )
    assert bundle.llm_path == "deterministic-stub"


def test_resolve_llm_path_strings():
    assert run(resolve_llm_path(FakeSecrets(None))) == "deterministic-stub"
    assert run(resolve_llm_path(FakeSecrets("a-key"))) == "gemini-live"


def test_key_available_selects_gemini_live_with_provenance():
    llm = RecordingLLM()
    bundle = run(
        build_content_live(
            _spec(),
            get_profile("facts"),
            secret_provider=FakeSecrets("a-key"),
            llm=llm,
        )
    )
    assert bundle.llm_path == LLM_PATH_GEMINI_LIVE == "gemini-live"
    assert bundle.llm_models == ["gemini-3.8-flash", "gemini-3.1-pro-preview"]
    assert bundle.llm_usage["calls"] == len(llm.requests)
    assert bundle.llm_usage["input_tokens"] == 100 * len(llm.requests)
    assert bundle.llm_usage["output_tokens"] == 40 * len(llm.requests)
    assert bundle.llm_cost_usd == pytest.approx(0.001 * len(llm.requests))
    assert bundle.chapters and all(c.text for c in bundle.chapters)
    assert "Samundar" in bundle.narration_text


def test_present_key_without_key_means_no_live_calls():
    """Key check governs even when an llm is injected (no silent live)."""
    llm = RecordingLLM()
    bundle = run(
        build_content_live(
            _spec(),
            get_profile("facts"),
            secret_provider=FakeSecrets(None),
            llm=llm,
        )
    )
    assert bundle.llm_path == "deterministic-stub"
    assert llm.requests == []


# ---------------------------------------------------------------------------
# Tier design: router receives tiers as designed
# ---------------------------------------------------------------------------

def test_router_receives_designed_tiers_and_tasks():
    llm = RecordingLLM()
    spec, profile = _spec(), get_profile("facts")
    bundle = run(
        build_content_live(spec, profile, secret_provider=FakeSecrets("k"), llm=llm)
    )
    tasks = [r.task for r in llm.requests]
    tiers = [r.tier for r in llm.requests]
    n_chapters = len(bundle.chapters)
    # outline first, one call per chapter, final pass last.
    assert tasks[0] == "outline"
    assert tasks[-1] == "script.final_pass"
    assert tasks[1:-1] == ["script.chapter"] * n_chapters
    assert tiers[0] == Tier.T1  # outline at T1
    assert all(t == Tier.T1 for t in tiers[1:-1])  # chapter writing at T1
    assert tiers[-1] == Tier.T2  # flagship final pass: Gemini Pro-class slot
    # Agent attribution follows the agent contracts (6 outline, 7 writing).
    assert llm.requests[0].agent_id == "6"
    assert all(r.agent_id == "7" for r in llm.requests[1:])
    # Prompts came through the assembly layer (versioned prompt files).
    assert all(r.prompt_version for r in llm.requests)
    assert all("Samundar ki gehrai ke raaz" in r.prompt for r in llm.requests)


# ---------------------------------------------------------------------------
# Real router + adapter (mocked HTTP): ledger + Budget Guard intact
# ---------------------------------------------------------------------------

GEMINI_OK = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {
                        "text": (
                            "Samundar ki gehrai mein chhupa raaz hairan kun "
                            "hai aur har nayi tafseel tasveer ko aur wazeh "
                            "karti hai."
                        )
                    }
                ]
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 120,
        "candidatesTokenCount": 30,
        "cachedContentTokenCount": 0,
        "thoughtsTokenCount": 0,
    },
}


def test_real_router_records_ledger_and_models():
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=GEMINI_OK)

    ledger = InMemoryLLMCallLedger()
    router = build_gemini_router(
        FakeSecrets("a-key"), ledger=ledger, transport=httpx.MockTransport(handler)
    )
    spec, profile = _spec(), get_profile("facts")
    bundle = run(
        build_content_live(spec, profile, secret_provider=FakeSecrets("a-key"), llm=router)
    )
    assert bundle.llm_path == "gemini-live"
    # Models actually used, per models.yaml tiers.
    assert set(bundle.llm_models) == {"gemini-3.8-flash", "gemini-3.1-pro-preview"}
    assert any("gemini-3.8-flash" in u for u in seen_urls)  # T1 calls
    assert any("gemini-3.1-pro-preview" in u for u in seen_urls)  # T2 final pass
    # Ledger (Budget Guard bookkeeping) recorded every call as gemini.
    assert ledger.entries, "router must write ledger rows for live calls"
    assert len(ledger.entries) == bundle.llm_usage["calls"]
    assert all(e.provider == "gemini" for e in ledger.entries)
    assert {e.tier for e in ledger.entries} == {"T1", "T2"}
    assert all(e.status == "ok" for e in ledger.entries)
    ledger_cost = sum(e.cost_usd for e in ledger.entries)
    assert ledger_cost > 0
    assert bundle.llm_cost_usd == pytest.approx(ledger_cost)
    assert bundle.llm_usage["input_tokens"] == 120 * len(ledger.entries)
    assert bundle.llm_usage["output_tokens"] == 30 * len(ledger.entries)


def test_invalid_key_error_propagates_not_stubbed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    router = build_gemini_router(
        FakeSecrets("bad-key"), transport=httpx.MockTransport(handler)
    )
    from app.providers.llm import ProviderConfigError

    with pytest.raises(ProviderConfigError):
        run(
            build_content_live(
                _spec(), get_profile("facts"),
                secret_provider=FakeSecrets("bad-key"), llm=router,
            )
        )


# ---------------------------------------------------------------------------
# Orchestrator provenance surfacing
# ---------------------------------------------------------------------------

def test_produce_surfaces_bundle_llm_provenance(monkeypatch, stage_fakes, tmp_path):
    stage_fakes.bundle.llm_path = "gemini-live"
    stage_fakes.bundle.llm_cost_usd = 0.0042

    def rec(ret):
        return lambda *args: ret

    monkeypatch.setattr(produce, "build_content", rec(stage_fakes.bundle))
    monkeypatch.setattr(produce, "run_gate_a", rec((True, [])))
    monkeypatch.setattr(produce, "synthesize_voice", rec(stage_fakes.voice))
    monkeypatch.setattr(produce, "plan_and_generate_visuals", rec(stage_fakes.visuals))
    monkeypatch.setattr(produce, "assemble_project", rec(stage_fakes.assemble))
    monkeypatch.setattr(produce, "finalize_publish", rec(stage_fakes.publish))
    result = produce.produce_video(_spec(minutes=1.0), tmp_path / "out")
    assert result.paths_used["llm"] == "gemini-live"
    assert result.cost_usd == pytest.approx(0.0042)
