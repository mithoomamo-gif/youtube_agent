"""Stock media layer tests (§24) — mocked HTTP, no network.

Covers: Pixabay-first default order; Pexels absent key -> Pixabay alone;
0 results advances the ladder then failover; hard filters; freshness
ranking de-ranks most-downloaded; ledger render gate; L1 search cache
24h hit; L3 negative cache; Pexels quota headers; circuit breaker.
"""
from __future__ import annotations

import asyncio
import io

import httpx
import pytest

from app.providers.stock import (
    Candidate,
    DecisionStatus,
    DictSecretProvider,
    DownloadedAsset,
    InMemoryLicenceLedger,
    LicenceLedgerError,
    MediaType,
    NegativeCache,
    SceneRequest,
    SearchCache,
    SearchFilters,
    StockSourcingService,
    StubAIImageProvider,
    assert_renderable,
)
from app.providers.stock.ai_image import NullAIImageProvider
from app.providers.stock.ledger import LedgerRow, build_row
from app.providers.stock.licences import pixabay_licence
from app.providers.stock.manual import ManualAsset, ManualLibraryProvider
from app.providers.stock.pexels import PexelsProvider
from app.providers.stock.pixabay import PixabayProvider
from app.providers.stock.ranking import filter_candidates, rank_candidates

PIXABAY_KEY_REF = "pixabay_api_key"
PEXELS_KEY_REF = "pexels_api_key"


def run(coro):
    return asyncio.run(coro)


# --- payload factories -----------------------------------------------------------

def pixabay_video_hit(asset_id, *, width=1920, height=1080, duration=20, fps=30,
                      downloads=100, tags="deep ocean, underwater, dark water"):
    return {
        "id": asset_id,
        "pageURL": f"https://pixabay.com/videos/id-{asset_id}/",
        "tags": tags,
        "duration": duration,
        "user": "oceanographer",
        "user_id": 42,
        "views": downloads * 10,
        "downloads": downloads,
        "videos": {
            "large": {"url": f"https://cdn.pixabay.com/video/{asset_id}.mp4",
                      "width": width, "height": height, "fps": fps, "size": 1000},
            "small": {"url": f"https://cdn.pixabay.com/video/{asset_id}-small.mp4",
                      "width": 640, "height": 360, "fps": fps, "size": 100},
        },
    }


def pixabay_photo_hit(asset_id, *, width=1920, height=1080, downloads=10):
    return {
        "id": asset_id,
        "pageURL": f"https://pixabay.com/photos/id-{asset_id}/",
        "tags": "ocean, underwater",
        "user": "shooter",
        "user_id": 7,
        "downloads": downloads,
        "previewURL": f"https://cdn.pixabay.com/photo/{asset_id}_150.jpg",
        "webformatURL": f"https://cdn.pixabay.com/photo/{asset_id}_640.jpg",
        "largeImageURL": f"https://cdn.pixabay.com/photo/{asset_id}_1280.jpg",
        "imageWidth": width,
        "imageHeight": height,
        "webformatWidth": 640,
        "webformatHeight": 360,
    }


def pexels_video(asset_id, *, width=1920, height=1080, duration=20, fps=30):
    return {
        "id": asset_id,
        "width": width,
        "height": height,
        "duration": duration,
        "url": f"https://www.pexels.com/video/{asset_id}/",
        "image": f"https://images.pexels.com/videos/{asset_id}/preview.jpg",
        "user": {"name": "pex user", "url": "https://www.pexels.com/@pex"},
        "video_files": [
            {"link": f"https://videos.pexels.com/{asset_id}.mp4", "quality": "hd",
             "width": width, "height": height, "fps": fps, "file_type": "video/mp4"}
        ],
    }


def good_pixabay_hits(start=1, count=3, **kw):
    return [pixabay_video_hit(i, **kw) for i in range(start, start + count)]


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def pixabay_provider(handler, *, secrets=None, cache=None):
    client = make_client(handler)
    return PixabayProvider(
        DictSecretProvider(secrets if secrets is not None else {PIXABAY_KEY_REF: "test-pixabay-key"}),
        PIXABAY_KEY_REF,
        http_client=client,
        cache=cache or SearchCache(),
    )


def pexels_provider(handler, *, secrets=None, cache=None):
    client = make_client(handler)
    return PexelsProvider(
        DictSecretProvider(secrets if secrets is not None else {PEXELS_KEY_REF: "test-pexels-key"}),
        PEXELS_KEY_REF,
        http_client=client,
        cache=cache or SearchCache(),
    )


def scene(**overrides) -> SceneRequest:
    base = dict(
        scene_id="scene-1",
        project_id="proj-1",
        niche="facts",
        exact_query="deep ocean dark water",
        synonym_query="ocean underwater",
        mood_query="darkness mystery slow motion",
        niche_query="ocean underwater",
        scene_duration_s=10.0,
    )
    base.update(overrides)
    return SceneRequest(**base)


# --- 1. Pixabay tried before Pexels by default order -----------------------------

def test_pixabay_tried_before_pexels_by_default_order():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "pixabay" in request.url.host:
            calls.append("pixabay")
            return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})
        calls.append("pexels")
        return httpx.Response(200, json={"videos": [pexels_video(i) for i in range(1, 4)]})

    async def go():
        service = StockSourcingService(
            providers=[pexels_provider(handler), pixabay_provider(handler)]  # reversed input order
        )
        # Default order is config: pixabay first regardless of list order.
        assert [p.name for p in service.ordered_providers] == ["pixabay", "pexels"]
        return await service.resolve_scene(scene())

    decision = run(go())
    assert decision.status == DecisionStatus.FOUND
    assert decision.chosen is not None and decision.chosen.provider == "pixabay"
    assert calls[0] == "pixabay"
    # >=3 candidates passed at step 1, so the ladder stopped and Pexels
    # was never billed for this scene.
    assert "pexels" not in calls
    assert decision.ladder_step == 1
    assert len(decision.alternates) == 2


def test_provider_order_is_configurable():
    def handler(request):
        return httpx.Response(200, json={"hits": [], "videos": []})

    service = StockSourcingService(
        providers=[pixabay_provider(handler), pexels_provider(handler)],
        provider_order=["pexels", "pixabay"],
    )
    assert [p.name for p in service.ordered_providers] == ["pexels", "pixabay"]


# --- 2. Pexels absent key -> chain proceeds on Pixabay alone ---------------------

def test_pexels_absent_key_chain_proceeds_on_pixabay_alone():
    pexels_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "pexels" in request.url.host:
            pexels_calls.append(1)
            return httpx.Response(200, json={"videos": [pexels_video(i) for i in range(1, 4)]})
        return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})

    async def go():
        pexels = pexels_provider(handler, secrets={})  # no key configured
        assert await pexels.is_available() is False
        assert await pexels.search("deep ocean", MediaType.VIDEO, SearchFilters()) == []
        service = StockSourcingService(providers=[pexels, pixabay_provider(handler)])
        return await service.resolve_scene(scene())

    decision = run(go())
    assert decision.status == DecisionStatus.FOUND
    assert decision.chosen.provider == "pixabay"
    assert pexels_calls == []  # no HTTP call was ever made without a key


# --- 3. 0 results advances ladder, then failover ---------------------------------

def test_zero_results_advances_ladder_to_next_step():
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        if q == "ocean underwater":
            return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async def go():
        service = StockSourcingService(providers=[pixabay_provider(handler)])
        return await service.resolve_scene(scene())

    decision = run(go())
    assert decision.status == DecisionStatus.FOUND
    assert decision.ladder_step == 2  # synonym/subject step succeeded


def test_zero_results_everywhere_falls_over_to_ai_image():
    def handler(request):
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async def go():
        ai = StubAIImageProvider(enabled=True)
        service = StockSourcingService(providers=[pixabay_provider(handler)], ai_provider=ai)
        decision = await service.resolve_scene(scene())
        assert ai.calls, "AI image provider should have been invoked"
        return decision

    decision = run(go())
    assert decision.status == DecisionStatus.AI_GENERATED
    assert decision.chosen is not None and decision.chosen.is_generated  # flagged GENERATED


def test_zero_results_everywhere_without_ai_is_blocked_with_escalation():
    def handler(request):
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async def go():
        service = StockSourcingService(
            providers=[pixabay_provider(handler)], ai_provider=NullAIImageProvider()
        )
        return await service.resolve_scene(scene())

    decision = run(go())
    assert decision.status == DecisionStatus.BLOCKED
    assert decision.chosen is None  # NEVER a silent black/placeholder scene
    assert decision.escalation_required is True


def test_manual_library_failover_requires_complete_licence():
    def handler(request):
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    complete_candidate = Candidate(
        provider="manual", provider_asset_id="m1", media_type=MediaType.VIDEO,
        width=1920, height=1080, duration_s=30, fps=30,
        tags=["deep", "ocean"], page_url="https://example.test/m1",
    )
    incomplete_candidate = Candidate(
        provider="manual", provider_asset_id="m2", media_type=MediaType.VIDEO,
        width=1920, height=1080, duration_s=30, fps=30, tags=["deep", "ocean"],
    )
    complete_row = LedgerRow(
        project_id="old", scene_id="s0", provider="manual", provider_asset_id="m1",
        asset_page_url="https://example.test/m1", licence_name="Mixkit Free License",
        licence_snapshot_date="2026-10-01", licence_terms_hash="abc123",
        retrieved_at="2026-10-01T00:00:00+00:00", file_sha256="deadbeef",
        local_file_path="/library/m1.mp4",
    )
    assert complete_row.is_complete()
    manual = ManualLibraryProvider([
        ManualAsset(candidate=incomplete_candidate, licence_row=None),
        ManualAsset(candidate=complete_candidate, licence_row=complete_row),
    ])

    async def go():
        service = StockSourcingService(
            providers=[pixabay_provider(handler)], manual_provider=manual,
            ai_provider=NullAIImageProvider(),
        )
        return await service.resolve_scene(scene())

    decision = run(go())
    assert decision.status == DecisionStatus.MANUAL
    assert decision.chosen.provider_asset_id == "m1"  # incomplete-licence asset skipped


# --- 4. Hard filters ----------------------------------------------------------------

def _candidate(asset_id, **kw) -> Candidate:
    base = dict(
        provider="pixabay", provider_asset_id=str(asset_id), media_type=MediaType.VIDEO,
        width=1920, height=1080, duration_s=20, fps=30, tags=["ocean"],
    )
    base.update(kw)
    return Candidate(**base)


def test_hard_filters_reject_portrait_short_and_low_fps():
    filters = SearchFilters(media_type=MediaType.VIDEO, scene_duration_s=10.0)
    candidates = [
        _candidate("good"),
        _candidate("portrait", width=720, height=1280),
        _candidate("short", duration_s=3),          # shorter than the scene
        _candidate("lowfps", fps=12),
        _candidate("narrow", width=854, height=480),  # below 1280 wide
        _candidate("logo", tags=["ocean", "logo"]),  # brand/logo metadata
    ]
    passed = filter_candidates(candidates, filters)
    assert [c.provider_asset_id for c in passed] == ["good"]


def test_hard_filters_photo_min_width_and_reuse_exclusions():
    filters = SearchFilters(media_type=MediaType.PHOTO)
    photo = Candidate(provider="pixabay", provider_asset_id="p1", media_type=MediaType.PHOTO,
                      width=1280, height=720, tags=["ocean"])
    big_photo = Candidate(provider="pixabay", provider_asset_id="p2", media_type=MediaType.PHOTO,
                          width=2400, height=1350, tags=["ocean"])
    assert filter_candidates([photo], filters) == []  # photos need >=1920 wide
    assert filter_candidates([big_photo], filters) == [big_photo]
    # Already used in this project / channel's last 20 videos -> excluded.
    used = filter_candidates(
        [big_photo], filters, used_keys={"pixabay:p2"}
    )
    assert used == []


# --- 5. Ranking de-ranks the most-downloaded -------------------------------------

def test_ranking_deranks_most_downloaded_vs_equal_relevance_fresher():
    filters = SearchFilters(media_type=MediaType.VIDEO, scene_duration_s=10.0)
    popular = _candidate("popular", downloads=100_000, tags=["deep", "ocean"])
    fresh = _candidate("fresh", downloads=5, tags=["deep", "ocean"])
    ranked = rank_candidates([popular, fresh], "deep ocean", filters)
    assert ranked[0].provider_asset_id == "fresh"
    assert ranked[1].provider_asset_id == "popular"


# --- 6. Licence ledger gate --------------------------------------------------------

def test_ledger_gate_raises_for_asset_with_no_ledger_row():
    ledger = InMemoryLicenceLedger()
    candidate = _candidate("99")
    asset = DownloadedAsset(candidate=candidate, local_path="/tmp/99.mp4", file_sha256="sha-99")
    with pytest.raises(LicenceLedgerError):
        assert_renderable(ledger, asset)
    with pytest.raises(LicenceLedgerError):
        ledger.assert_renderable(candidate)

    row = build_row(
        project_id="proj-1", scene_id="scene-1", candidate=candidate,
        licence=pixabay_licence(), retrieved_by_key_id=PIXABAY_KEY_REF,
        local_file_path="/tmp/99.mp4", file_sha256="sha-99",
    )
    # The key REF is recorded, never a key value.
    assert row.retrieved_by_key_id == PIXABAY_KEY_REF
    ledger.write_row(row)
    assert ledger.assert_renderable(asset, project_id="proj-1").ledger_id == row.ledger_id

    # CSV export is licence_ledger.csv-compatible.
    out = io.StringIO()
    ledger.export_csv(out)
    text = out.getvalue()
    assert "licence_terms_hash" in text.splitlines()[0]
    assert "pixabay" in text and "sha-99" in text

    # Ledger rows are never deleted.
    with pytest.raises(LicenceLedgerError):
        ledger.delete_row(row.ledger_id)


# --- 7. Search cache hit avoids a second HTTP call within 24h ----------------------

def test_search_cache_hit_avoids_second_http_call_within_24h():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})

    async def go():
        provider = pixabay_provider(handler)
        filters = SearchFilters(media_type=MediaType.VIDEO)
        first = await provider.search("Deep  Ocean", MediaType.VIDEO, filters)
        # Normalised query (case/whitespace) hits the same cache entry.
        second = await provider.search("deep ocean", MediaType.VIDEO, filters)
        return first, second

    first, second = run(go())
    assert len(calls) == 1
    assert [c.provider_asset_id for c in first] == [c.provider_asset_id for c in second]


# --- 8. Negative cache records an empty query --------------------------------------

def test_negative_cache_records_empty_query():
    def handler(request):
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async def go():
        negative = NegativeCache()
        service = StockSourcingService(
            providers=[pixabay_provider(handler)], negative_cache=negative,
            ai_provider=NullAIImageProvider(),
        )
        decision = await service.resolve_scene(scene())
        return decision, negative

    decision, negative = run(go())
    assert decision.status == DecisionStatus.BLOCKED
    assert negative.is_negative("pixabay", "deep ocean dark water", "facts", media_type="video")
    assert negative.is_negative("pixabay", "Deep Ocean Dark Water", "facts", media_type="video")
    # Direct unit behaviour: unknown queries are not negative.
    assert NegativeCache().is_negative("pixabay", "never seen", "facts") is False


# --- Extras: Pexels quota headers + circuit breaker ---------------------------------

def test_pexels_quota_read_from_success_headers_and_not_on_429():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                200,
                json={"videos": [pexels_video(1)]},
                headers={"X-Ratelimit-Limit": "200", "X-Ratelimit-Remaining": "199",
                         "X-Ratelimit-Reset": "1890000000"},
            )
        return httpx.Response(429, json={})  # headers absent on 429

    async def go():
        provider = pexels_provider(handler)
        await provider.search("ocean", MediaType.VIDEO, SearchFilters())
        quota_after_success = (provider.quota.limit, provider.quota.remaining, provider.quota.reset)
        # Different query -> cache miss -> real 429 response.
        await provider.search("volcano", MediaType.VIDEO, SearchFilters())
        return provider, quota_after_success

    provider, quota_after_success = run(go())
    assert quota_after_success == (200, 199, 1890000000)
    # The 429 must not clobber the last known quota state.
    assert (provider.quota.limit, provider.quota.remaining, provider.quota.reset) == quota_after_success


def test_circuit_breaker_trips_after_429_storm():
    def handler(request):
        return httpx.Response(429, json={})

    async def go():
        provider = pixabay_provider(handler)
        for i in range(3):
            # Distinct queries bypass the L1 cache; each is a real 429.
            result = await provider.search(f"query {i}", MediaType.VIDEO, SearchFilters())
            assert result == []  # outage is an outcome, not an exception
        return provider

    provider = run(go())
    assert provider.circuit_breaker.is_open is True

    async def available():
        return await provider.is_available()

    assert run(available()) is False


# --- Round 3 hardening: proxy fix, filter forwarding, likes, key hygiene ---------

def test_adapter_built_clients_ignore_malformed_proxy_env(monkeypatch):
    """Regression: sandbox proxy env (httpx InvalidURL ':1]') must never
    break the adapters' own HTTP clients — they are built trust_env=False."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://proxy.invalid:1]")
    pix = PixabayProvider(DictSecretProvider({PIXABAY_KEY_REF: "k"}), PIXABAY_KEY_REF)
    pex = PexelsProvider(DictSecretProvider({PEXELS_KEY_REF: "k"}), PEXELS_KEY_REF)
    for provider in (pix, pex):
        client = provider._ensure_client()  # must not raise InvalidURL
        assert client.trust_env is False
        run(client.aclose())


def test_pixabay_search_forwards_integration_filters():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/videos/" in request.url.path:
            return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})
        return httpx.Response(200, json={"hits": [pixabay_photo_hit(9)], "totalHits": 1})

    async def go():
        provider = pixabay_provider(handler)
        video_filters = SearchFilters(media_type=MediaType.VIDEO, orientation="landscape",
                                      min_width=1600, per_page=50, safesearch=True)
        await provider.search("deep ocean", MediaType.VIDEO, video_filters)
        photo_filters = SearchFilters(media_type=MediaType.PHOTO, orientation="landscape")
        await provider.search("reef", MediaType.PHOTO, photo_filters)
        # per_page above Pixabay's max clamps to 200; below its min clamps to 3.
        await provider.search("storm", MediaType.VIDEO,
                              SearchFilters(media_type=MediaType.VIDEO, per_page=999))
        await provider.search("calm", MediaType.VIDEO,
                              SearchFilters(media_type=MediaType.VIDEO, per_page=1))

    run(go())
    video_params = seen[0].url.params
    assert video_params["orientation"] == "horizontal"  # Pixabay vocabulary, not "landscape"
    assert video_params["min_width"] == "1600"
    assert video_params["per_page"] == "50"
    assert video_params["safesearch"] == "true"
    photo_params = seen[1].url.params
    assert photo_params["image_type"] == "photo"
    assert photo_params["orientation"] == "horizontal"
    assert photo_params["min_width"] == "1920"  # photo effective minimum
    assert seen[2].url.params["per_page"] == "200"
    assert seen[3].url.params["per_page"] == "3"


def test_pixabay_candidates_carry_likes_and_freshness_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/videos/" in request.url.path:
            hit = pixabay_video_hit(5, downloads=321)
            hit["likes"] = 77
            return httpx.Response(200, json={"hits": [hit], "totalHits": 1})
        hit = pixabay_photo_hit(6, downloads=12)
        hit["likes"] = 34
        hit["views"] = 999
        return httpx.Response(200, json={"hits": [hit], "totalHits": 1})

    async def go():
        provider = pixabay_provider(handler)
        video = (await provider.search("ocean", MediaType.VIDEO, SearchFilters()))[0]
        photo = (await provider.search("reef", MediaType.PHOTO,
                                         SearchFilters(media_type=MediaType.PHOTO)))[0]
        return video, photo

    video, photo = run(go())
    assert video.provider_asset_id == "5"
    assert video.page_url and video.download_url and video.preview_url
    assert (video.width, video.height) == (1920, 1080)
    assert video.duration_s == 20
    assert "underwater" in video.tags
    assert video.downloads == 321 and video.likes == 77 and video.views == 3210
    assert photo.provider_asset_id == "6"
    assert photo.page_url and photo.download_url and photo.preview_url
    assert (photo.width, photo.height) == (1920, 1080)
    assert photo.duration_s is None
    assert photo.downloads == 12 and photo.likes == 34 and photo.views == 999


def test_pixabay_key_comes_from_secret_provider_and_never_leaks():
    secret_value = "test-pixabay-key"  # the mocked-suite placeholder, not a real key
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.url.params.get("key", ""))
        return httpx.Response(200, json={"hits": good_pixabay_hits(), "totalHits": 3})

    async def go():
        provider = pixabay_provider(handler)
        return await provider.search("ocean", MediaType.VIDEO, SearchFilters())

    candidates = run(go())
    assert seen_keys == [secret_value]  # key reached Pixabay, from SecretProvider
    for cand in candidates:
        blob = repr(cand)
        assert secret_value not in blob  # ... and nowhere in returned candidates
    assert PixabayProvider.key_ref is not None  # ref-based, never a value


def test_pexels_search_clamps_per_page_and_forwards_video_min_width():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"videos": [pexels_video(1)]})

    async def go():
        provider = pexels_provider(handler)
        await provider.search("ocean", MediaType.VIDEO,
                              SearchFilters(media_type=MediaType.VIDEO, per_page=500))

    run(go())
    params = seen[0].url.params
    assert params["per_page"] == "80"  # Pexels maximum
    assert params["min_width"] == "1280"
    assert params["orientation"] == "landscape"  # Pexels vocabulary passes through


def test_pixabay_licence_mapping():
    licence = pixabay_licence()
    assert licence.name == "Pixabay Content License"
    assert licence.commercial_use_allowed is True
    assert licence.attribution_required is False
    assert licence.standalone_use_prohibited is True
    assert licence.terms_hash  # snapshot hash present for the ledger
