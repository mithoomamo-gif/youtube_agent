"""Tests for the image-generation layer (providers/images).

All HTTP is mocked with httpx.MockTransport — no network, no GPU.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.providers.images import (
    FLUX_1_DEV,
    FLUX_1_SCHNELL,
    FLUX_2_KLEIN_4B,
    FLUX_2_KLEIN_9B,
    ComfyUIAdapter,
    FluxApiAdapter,
    ImageRequest,
    ImageRouter,
    LicensingBlocked,
    PromptBlockedError,
    Purpose,
    StyleProfile,
    StyleRegistry,
    make_draft_style_profile,
    tier_for_vram,
    validate_model,
)
from app.providers.images.errors import DiegeticTextDisabled

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def run(coro):
    return asyncio.run(coro)


class FakeSecrets:
    def __init__(self, secrets: dict | None = None) -> None:
        self._secrets = secrets or {}

    async def get_secret(self, ref: str) -> str | None:
        return self._secrets.get(ref)


# ----------------------------------------------------------------------
# Fake transports
# ----------------------------------------------------------------------

def make_comfy_handler(captured_prompts: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"os": "win32"}})
        if path == "/queue":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        if path == "/prompt":
            captured_prompts.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"prompt_id": "p-1", "number": 1, "node_errors": {}},
            )
        if path == "/history/p-1":
            return httpx.Response(
                200,
                json={
                    "p-1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "flux_00001.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if path == "/view":
            return httpx.Response(200, content=PNG_BYTES)
        return httpx.Response(404, json={})

    return handler


def make_dead_comfy_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return handler


def make_bfl_handler(captured_payloads: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/flux"):
            captured_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "task-1"})
        if path == "/v1/get_result":
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "status": "Ready",
                    "result": {"sample": "https://cdn.example.test/sample.png"},
                },
            )
        if request.url.host == "cdn.example.test":
            return httpx.Response(200, content=PNG_BYTES)
        return httpx.Response(404, json={})

    return handler


def make_local(captured: list, **kwargs) -> ComfyUIAdapter:
    return ComfyUIAdapter(
        transport=httpx.MockTransport(make_comfy_handler(captured)),
        poll_interval_s=0,
        **kwargs,
    )


def make_api(captured: list, **kwargs) -> FluxApiAdapter:
    return FluxApiAdapter(
        FakeSecrets({"bfl/api_key": "test-key"}),
        transport=httpx.MockTransport(make_bfl_handler(captured)),
        poll_interval_s=0,
        **kwargs,
    )


def horror_profile() -> StyleProfile:
    return StyleProfile(
        profile_id="horror-main",
        style_block="dark cinematic digital painting, deep teal palette #0B2B2B",
        negative_block="watermark, bright daylight, cheerful colours",
        version=3,
        channel_id="chan-1",
    )


# ----------------------------------------------------------------------
# Allow-list / licensing
# ----------------------------------------------------------------------

def test_allowlist_rejects_flux1_dev_without_attestation():
    with pytest.raises(LicensingBlocked):
        validate_model(FLUX_1_DEV)
    # Display-name / free-text spellings are canonicalised and blocked too.
    with pytest.raises(LicensingBlocked):
        validate_model("FLUX.1 [dev]")


def test_allowlist_permits_flux1_dev_with_attestation():
    info = validate_model(FLUX_1_DEV, holds_bfl_commercial_licence=True)
    assert info.canonical_id == FLUX_1_DEV


def test_allowlist_schnell_permitted_by_default():
    info = validate_model(FLUX_1_SCHNELL)
    assert info.canonical_id == FLUX_1_SCHNELL
    assert info.commercial_safe_by_default is True
    assert validate_model("FLUX.1 [schnell]").canonical_id == FLUX_1_SCHNELL


def test_allowlist_klein_4b_permitted_klein_9b_blocked():
    assert validate_model(FLUX_2_KLEIN_4B).canonical_id == FLUX_2_KLEIN_4B
    assert validate_model("FLUX.2 [klein] 4B").canonical_id == FLUX_2_KLEIN_4B
    with pytest.raises(LicensingBlocked):
        validate_model(FLUX_2_KLEIN_9B)
    info = validate_model(FLUX_2_KLEIN_9B, holds_bfl_commercial_licence=True)
    assert info.canonical_id == FLUX_2_KLEIN_9B


def test_allowlist_unknown_model_blocked():
    with pytest.raises(LicensingBlocked):
        validate_model("some-random-community-checkpoint")


def test_router_rejects_dev_model_before_any_provider_call():
    comfy_calls: list = []
    api_calls: list = []
    router = ImageRouter(local=make_local(comfy_calls), api=make_api(api_calls))
    with pytest.raises(LicensingBlocked):
        run(router.generate(ImageRequest(
            prompt="a quiet forest at dawn", model_id=FLUX_1_DEV, seed=1,
        )))
    assert comfy_calls == []
    assert api_calls == []


# ----------------------------------------------------------------------
# Style system
# ----------------------------------------------------------------------

def test_style_block_appended_to_every_routed_request():
    comfy_calls: list = []
    profile = horror_profile()
    router = ImageRouter(
        local=make_local(comfy_calls),
        styles=StyleRegistry({profile.profile_id: profile}),
    )
    result = run(router.generate(ImageRequest(
        prompt="an abandoned lighthouse in a storm",
        negative_prompt="blurry",
        style_profile_id="horror-main",
        seed=42,
    )))
    assert result.provider == "comfyui-local"
    assert len(comfy_calls) == 1
    workflow = comfy_calls[0]["prompt"]
    positive = workflow["6"]["inputs"]["text"]
    negative = workflow["7"]["inputs"]["text"]
    # Layering order: scene prompt first, then the locked style block.
    assert positive.startswith("an abandoned lighthouse in a storm")
    assert "dark cinematic digital painting" in positive
    assert "#0B2B2B" in positive
    # Negatives merged: request negative + profile negative block.
    assert "blurry" in negative
    assert "watermark" in negative
    assert "bright daylight" in negative
    # Sampler lock + native 16:9 size reached the workflow.
    assert workflow["27"]["inputs"]["width"] == 1344
    assert workflow["27"]["inputs"]["height"] == 768
    assert workflow["31"]["inputs"]["seed"] == 42


def test_draft_profile_for_custom_niche():
    draft = make_draft_style_profile("underwater basket weaving stories")
    assert draft.status == "draft"
    assert draft.niche_idea == "underwater basket weaving stories"
    assert draft.style_block  # inferred, non-empty
    horror = make_draft_style_profile("horror stories from villages")
    assert "candlelight" in horror.style_block


def test_anchor_candidates_returns_four():
    api_calls: list = []
    router = ImageRouter(api=make_api(api_calls))
    results = run(router.generate_anchor_candidates(
        "cooking stories from a village kitchen", channel_id="chan-9",
    ))
    assert len(results) == 4
    assert len({r.seed for r in results}) == 4  # distinct deterministic seeds
    assert all(r.provenance.provenance == "GENERATED" for r in results)


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------

def test_content_hash_cache_returns_cached_without_second_provider_call():
    comfy_calls: list = []
    router = ImageRouter(local=make_local(comfy_calls))
    req = ImageRequest(prompt="misty mountains at sunrise", seed=7)
    first = run(router.generate(req))
    second = run(router.generate(ImageRequest(
        prompt="misty mountains at sunrise", seed=7,
    )))
    assert len(comfy_calls) == 1  # no second provider call
    assert second.image_bytes == first.image_bytes == PNG_BYTES
    assert second.provenance.cache_hit is True
    assert second.cost_usd == 0.0


# ----------------------------------------------------------------------
# Routing / overflow / premium budget
# ----------------------------------------------------------------------

def test_router_overflows_to_api_when_local_unavailable():
    api_calls: list = []
    dead_local = ComfyUIAdapter(
        transport=httpx.MockTransport(make_dead_comfy_handler()),
        poll_interval_s=0,
    )
    router = ImageRouter(local=dead_local, api=make_api(api_calls))
    result = run(router.generate(ImageRequest(
        prompt="a lighthouse keeper watches the storm", seed=5,
    )))
    assert result.provider == "bfl-flux-api"
    assert result.model_id == FLUX_1_SCHNELL
    assert result.cost_usd == pytest.approx(0.003)
    assert result.image_bytes == PNG_BYTES
    assert len(api_calls) == 1


def test_router_premium_pro_only_for_hero_within_budget():
    api_calls: list = []
    comfy_calls: list = []
    router = ImageRouter(
        local=make_local(comfy_calls),
        api=make_api(api_calls),
        premium_budget_usd_per_channel=1.0,
    )
    hero = run(router.generate(ImageRequest(
        prompt="the climactic reveal in the old mansion",
        purpose=Purpose.HERO, channel_id="chan-1", seed=3,
    )))
    assert hero.provider == "bfl-flux-api"
    assert hero.model_id == "flux-pro"
    assert hero.cost_usd == pytest.approx(0.05)
    assert comfy_calls == []  # premium path did not touch local

    # Budget exhausted (0.04 left < 0.05) -> hero falls back to local.
    tight = ImageRouter(
        local=make_local(comfy_calls),
        api=make_api(api_calls),
        premium_budget_usd_per_channel=0.04,
    )
    fallback = run(tight.generate(ImageRequest(
        prompt="another climactic reveal",
        purpose=Purpose.HERO, channel_id="chan-1", seed=4,
    )))
    assert fallback.provider == "comfyui-local"
    assert fallback.model_id == FLUX_1_SCHNELL


def test_router_thumbnail_background_uses_premium_when_affordable():
    api_calls: list = []
    router = ImageRouter(api=make_api(api_calls))
    result = run(router.generate(ImageRequest(
        prompt="shocked explorer silhouette, empty space in upper third, "
               "no text, no letters",
        purpose=Purpose.THUMBNAIL_BACKGROUND, channel_id="chan-2", seed=11,
    )))
    assert result.model_id == "flux-pro"
    assert api_calls[0]["prompt_upsampling"] is False


# ----------------------------------------------------------------------
# Safety layer
# ----------------------------------------------------------------------

def test_denylist_brand_prompt_blocked_before_any_http_call():
    comfy_calls: list = []
    api_calls: list = []
    router = ImageRouter(local=make_local(comfy_calls), api=make_api(api_calls))
    with pytest.raises(PromptBlockedError) as excinfo:
        run(router.generate(ImageRequest(
            prompt="a Nike advertisement poster in a stadium", seed=1,
        )))
    assert any("nike" in v for v in excinfo.value.violations)
    assert comfy_calls == []  # blocked BEFORE any provider/HTTP call
    assert api_calls == []


def test_denylist_character_and_celebrity_blocked():
    router = ImageRouter(local=make_local([]))
    for prompt in (
        "Mickey Mouse riding a train through the alps",
        "a portrait of Taylor Swift performing on stage",
    ):
        with pytest.raises(PromptBlockedError):
            run(router.generate(ImageRequest(prompt=prompt, seed=2)))


# ----------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------

def test_provenance_record_present_on_results():
    router = ImageRouter(local=make_local([]))
    result = run(router.generate(ImageRequest(
        prompt="an old library with towering shelves", seed=9,
    )))
    prov = result.provenance
    assert prov.provider == "comfyui-local"
    assert prov.model_id == FLUX_1_SCHNELL
    assert prov.provenance == "GENERATED"
    assert prov.prompt_hash and len(prov.prompt_hash) == 64
    assert prov.generated_at is not None
    assert prov.licence == "Apache-2.0"
    as_dict = prov.to_dict()
    assert as_dict["provenance"] == "GENERATED"
    assert as_dict["provider"] == result.provider


# ----------------------------------------------------------------------
# VRAM tiers + diegetic text exception path
# ----------------------------------------------------------------------

def test_vram_tiers():
    assert tier_for_vram(8).precision == "gguf-q4"
    assert tier_for_vram(8).lowvram is True
    assert tier_for_vram(12).precision == "fp8"
    assert tier_for_vram(24).precision == "fp16"


def test_diegetic_text_disabled_by_default():
    adapter = make_api([])
    with pytest.raises(DiegeticTextDisabled):
        run(adapter.generate_diegetic_text_image(
            scene_prompt="a village bakery at dawn", exact_text="Fresh Bread",
        ))


def test_diegetic_text_latin_only_and_fallback():
    api_calls: list = []
    adapter = make_api(api_calls, diegetic_text_enabled=True)
    with pytest.raises(ValueError):
        run(adapter.generate_diegetic_text_image(
            scene_prompt="a bookshop window", exact_text="کتابوں کی دکان",
        ))
    # OCR gate never matches -> 1 + 2 rerolls, then text-free fallback.
    result = run(adapter.generate_diegetic_text_image(
        scene_prompt="a village bakery at dawn",
        exact_text="Fresh Bread",
        ocr_check=lambda _b: "Fresh Bred",
    ))
    assert result.metadata["diegetic_fallback"] == "text-free"
    assert result.metadata["attempts"] == 3
