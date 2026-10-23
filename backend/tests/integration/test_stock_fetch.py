"""Unit tests for app.integration.stock_fetch (Round 3, §24).

Fully mocked: a fake adapter returns crafted candidate lists and fake
download bytes. No network, no live Pixabay calls, no API key involved.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.integration.stock_fetch import (
    LICENCE_NAME,
    PIXABAY_TERMS_URL,
    TERMS_HASH,
    StockFetcher,
    StockSceneAsset,
)
from app.providers.stock.models import Candidate, MediaType, SensitiveFlags


# --- fake adapter --------------------------------------------------------------


class FakeAdapter:
    """Duck-types the Pixabay adapter: async search + async download.

    ``results`` maps (query, "video"|"photo") -> list[Candidate].
    Records every search and download so tests can assert the ladder
    order and that cached fetches perform no download. Deliberately has
    NO ``cache`` attribute, which also exercises the fetcher's 24h file
    search cache.
    """

    name = "pixabay"

    def __init__(self, results=None, *, error: Exception | None = None):
        self.results = dict(results or {})
        self.error = error
        self.search_calls: list[tuple[str, str]] = []
        self.download_calls: list[str] = []

    async def search(self, query, media_type, filters=None):
        kind = "video" if media_type == MediaType.VIDEO else "photo"
        self.search_calls.append((query, kind))
        if self.error is not None:
            raise self.error
        # Return copies so cache serialisation in one fetch cannot
        # mutate the fixture for the next.
        return list(self.results.get((query, kind), []))

    async def download(self, candidate):
        self.download_calls.append(str(candidate.provider_asset_id))
        return f"fake-bytes-for-{candidate.provider_asset_id}".encode()


def make_video(asset_id, **kw) -> Candidate:
    base = dict(
        provider="pixabay",
        provider_asset_id=str(asset_id),
        media_type=MediaType.VIDEO,
        download_url=f"https://cdn.example.test/video/{asset_id}.mp4",
        page_url=f"https://pixabay.com/videos/id-{asset_id}/",
        duration_s=30.0,
        width=1920,
        height=1080,
        fps=30.0,
        tags=["deep", "ocean"],
        downloads=100,
    )
    base.update(kw)
    return Candidate(**base)


def make_photo(asset_id, **kw) -> Candidate:
    base = dict(
        provider="pixabay",
        provider_asset_id=str(asset_id),
        media_type=MediaType.PHOTO,
        download_url=f"https://cdn.example.test/photo/{asset_id}.jpg",
        page_url=f"https://pixabay.com/photos/id-{asset_id}/",
        duration_s=None,
        width=2400,
        height=1350,
        tags=["deep", "ocean"],
        downloads=100,
    )
    base.update(kw)
    return Candidate(**base)


def fetcher_for(tmp_path, adapter) -> StockFetcher:
    return StockFetcher(adapter=adapter, cache_dir=tmp_path / "stock")


# --- happy path / contract -----------------------------------------------------


def test_video_preferred_over_photo(tmp_path):
    adapter = FakeAdapter({
        ("deep ocean", "video"): [make_video("v1")],
        ("deep ocean", "photo"): [make_photo("p1")],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["deep ocean"])
    assert isinstance(asset, StockSceneAsset)
    assert asset.kind == "video"
    assert asset.provider == "pixabay"
    assert asset.provider_asset_id == "v1"
    assert asset.duration_s == 30.0
    assert asset.width == 1920 and asset.height == 1080
    # Photo search never happened: the video ladder succeeded first.
    assert adapter.search_calls == [("deep ocean", "video")]
    # Local file is real, content-addressed, and matches file_sha256.
    path = Path(asset.local_path)
    assert path.is_file()
    assert path.parent == tmp_path / "stock"
    expected = hashlib.sha256(b"fake-bytes-for-v1").hexdigest()
    assert asset.file_sha256 == expected
    assert path.name == f"{expected}.mp4"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def test_asset_licence_fields(tmp_path):
    adapter = FakeAdapter({("q", "video"): [make_video("v9")]})
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(5.0, ["q"])
    assert asset.licence_name == "Pixabay Content License" == LICENCE_NAME
    expected_hash = hashlib.sha256(
        (LICENCE_NAME + PIXABAY_TERMS_URL).encode("utf-8")
    ).hexdigest()
    assert asset.terms_hash == expected_hash == TERMS_HASH
    assert asset.asset_page_url == "https://pixabay.com/videos/id-v9/"
    assert asset.download_url == "https://cdn.example.test/video/v9.mp4"


def test_photo_fallback_when_no_video_passes(tmp_path):
    adapter = FakeAdapter({
        ("deep ocean", "photo"): [make_photo("p1")],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["deep ocean"])
    assert asset is not None
    assert asset.kind == "photo"
    assert asset.provider_asset_id == "p1"
    assert asset.duration_s is None
    # Videos were tried first, across the ladder, before photos.
    assert adapter.search_calls == [("deep ocean", "video"), ("deep ocean", "photo")]


def test_photo_preferred_width_1920(tmp_path):
    adapter = FakeAdapter({
        ("q", "photo"): [
            make_photo("narrow-ok", width=1600, height=900, downloads=1),
            make_photo("wide", width=2400, height=1350, downloads=9999),
        ],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(5.0, ["q"])
    assert asset.provider_asset_id == "wide"  # >=1920 preferred over >=1280


def test_photo_min_width_1280_still_usable(tmp_path):
    adapter = FakeAdapter({
        ("q", "photo"): [make_photo("p1280", width=1280, height=720)],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(5.0, ["q"])
    assert asset is not None and asset.provider_asset_id == "p1280"


# --- ladder order ----------------------------------------------------------------


def test_ladder_order_second_query_wins(tmp_path):
    adapter = FakeAdapter({
        ("specific query", "video"): [],
        ("broader query", "video"): [make_video("v2")],
        ("mood query", "video"): [make_video("v3")],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(
        10.0, ["specific query", "broader query", "mood query"]
    )
    assert asset.provider_asset_id == "v2"
    # Queries were tried in order and the ladder stopped at the first hit.
    assert adapter.search_calls == [
        ("specific query", "video"),
        ("broader query", "video"),
    ]


def test_ladder_all_video_queries_before_any_photo(tmp_path):
    adapter = FakeAdapter({
        ("q2", "photo"): [make_photo("p7")],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q1", "q2"])
    assert asset.provider_asset_id == "p7"
    assert adapter.search_calls == [
        ("q1", "video"), ("q2", "video"),  # whole video ladder first
        ("q1", "photo"), ("q2", "photo"),  # then the photo ladder
    ]


# --- hard filters ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_candidate",
    [
        pytest.param(make_video("portrait", width=720, height=1280), id="portrait"),
        pytest.param(make_video("square", width=1280, height=1280), id="not-landscape"),
        pytest.param(make_video("narrow", width=854, height=480), id="too-narrow"),
        pytest.param(make_video("short", duration_s=3.0), id="too-short"),
        pytest.param(
            make_video("brandtag", tags=["deep", "ocean", "logo"]), id="brand-tag"
        ),
        pytest.param(
            make_video(
                "flagged",
                sensitive_flags=SensitiveFlags(trademark_or_logo=True),
            ),
            id="trademark-flag",
        ),
        pytest.param(make_video("nsfw", is_nsfw=True), id="nsfw-safesearch"),
    ],
)
def test_hard_filters_reject_bad_video_candidates(tmp_path, bad_candidate):
    adapter = FakeAdapter({("q", "video"): [bad_candidate]})
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q"])
    assert asset is None
    assert adapter.download_calls == []


def test_excluded_asset_id_rejected(tmp_path):
    adapter = FakeAdapter({
        ("q", "video"): [make_video("used1"), make_video("fresh2")],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(
        10.0, ["q"], exclude_asset_ids={"used1"}
    )
    assert asset.provider_asset_id == "fresh2"


def test_excluded_asset_id_pixabay_prefixed_form(tmp_path):
    adapter = FakeAdapter({("q", "video"): [make_video("used1")]})
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(
        10.0, ["q"], exclude_asset_ids={"pixabay:used1"}
    )
    assert asset is None


def test_filter_rejection_falls_through_to_next_candidate(tmp_path):
    adapter = FakeAdapter({
        ("q", "video"): [
            make_video("portrait", width=720, height=1280),
            make_video("good"),
        ],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q"])
    assert asset.provider_asset_id == "good"


# --- ranking: freshness ------------------------------------------------------------


def test_freshness_deranks_most_downloaded(tmp_path):
    adapter = FakeAdapter({
        ("deep ocean", "video"): [
            make_video("popular", downloads=100_000),  # adapter order #1
            make_video("fresh", downloads=5),           # equal relevance
        ],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["deep ocean"])
    assert asset.provider_asset_id == "fresh"


def test_relevance_beats_freshness_for_better_match(tmp_path):
    adapter = FakeAdapter({
        ("deep ocean", "video"): [
            make_video("offtopic-fresh", tags=["mountain", "snow"], downloads=1),
            make_video("ontopic-popular", tags=["deep", "ocean"], downloads=100_000),
        ],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["deep ocean"])
    assert asset.provider_asset_id == "ontopic-popular"


def test_ranking_deterministic_tiebreak_by_asset_id(tmp_path):
    adapter = FakeAdapter({
        ("deep ocean", "video"): [
            make_video("b-id", downloads=50),
            make_video("a-id", downloads=50),
        ],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["deep ocean"])
    assert asset.provider_asset_id == "a-id"


# --- caching ------------------------------------------------------------------------


def test_cache_reuse_second_fetch_performs_no_download(tmp_path):
    adapter = FakeAdapter({("deep ocean", "video"): [make_video("v1")]})
    fetcher = fetcher_for(tmp_path, adapter)
    first = fetcher.fetch_for_scene(10.0, ["deep ocean"])
    second = fetcher.fetch_for_scene(10.0, ["deep ocean"])
    assert adapter.download_calls == ["v1"]  # exactly one download, ever
    assert second.local_path == first.local_path
    assert second.file_sha256 == first.file_sha256
    # The asset-id -> hash index exists in the cache dir and points at v1.
    index = json.loads((tmp_path / "stock" / "index.json").read_text())
    assert index["v1"]["file_sha256"] == first.file_sha256
    assert index["v1"]["local_path"] == first.local_path


def test_index_reuse_across_fetcher_instances(tmp_path):
    adapter1 = FakeAdapter({("deep ocean", "video"): [make_video("v1")]})
    first = fetcher_for(tmp_path, adapter1).fetch_for_scene(10.0, ["deep ocean"])
    # A brand-new fetcher + adapter over the same cache dir: the index
    # (plus the 24h file search cache) means no search and no download.
    adapter2 = FakeAdapter({("deep ocean", "video"): [make_video("v1")]})
    second = fetcher_for(tmp_path, adapter2).fetch_for_scene(10.0, ["deep ocean"])
    assert second.local_path == first.local_path
    assert adapter2.download_calls == []
    assert adapter2.search_calls == []  # served by the file search cache


def test_same_bytes_from_different_asset_reuse_content_file(tmp_path):
    class SameBytesAdapter(FakeAdapter):
        async def download(self, candidate):
            self.download_calls.append(str(candidate.provider_asset_id))
            return b"identical-bytes"

    adapter = SameBytesAdapter({
        ("q", "video"): [make_video("one")],
        ("q2", "video"): [make_video("two")],
    })
    fetcher = fetcher_for(tmp_path, adapter)
    a = fetcher.fetch_for_scene(10.0, ["q"])
    b = fetcher.fetch_for_scene(10.0, ["q2"])
    assert a.file_sha256 == b.file_sha256
    assert a.local_path == b.local_path  # one content-addressed file


# --- None outcomes ---------------------------------------------------------------------


def test_none_on_empty_results(tmp_path):
    adapter = FakeAdapter({})
    assert fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q1", "q2"]) is None
    assert adapter.download_calls == []


def test_none_on_adapter_exception(tmp_path):
    adapter = FakeAdapter(error=RuntimeError("provider outage"))
    assert fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q"]) is None


def test_none_on_download_failure_then_ladder_continues(tmp_path):
    class FlakyDownloadAdapter(FakeAdapter):
        async def download(self, candidate):
            self.download_calls.append(str(candidate.provider_asset_id))
            if candidate.provider_asset_id == "broken":
                raise RuntimeError("cdn 404")
            return b"good-bytes"

    adapter = FlakyDownloadAdapter({
        ("q", "video"): [make_video("broken", downloads=1),
                         make_video("works", downloads=2)],
    })
    asset = fetcher_for(tmp_path, adapter).fetch_for_scene(10.0, ["q"])
    assert asset is not None and asset.provider_asset_id == "works"


def test_none_on_empty_queries(tmp_path):
    adapter = FakeAdapter({("q", "video"): [make_video("v1")]})
    fetcher = fetcher_for(tmp_path, adapter)
    assert fetcher.fetch_for_scene(10.0, []) is None
    assert fetcher.fetch_for_scene(10.0, ["", "  "]) is None
    assert adapter.search_calls == []


# --- async core -------------------------------------------------------------------------


def test_async_core_matches_sync_contract(tmp_path):
    import asyncio

    adapter = FakeAdapter({("q", "video"): [make_video("v1")]})
    fetcher = fetcher_for(tmp_path, adapter)
    asset = asyncio.run(fetcher.fetch_for_scene_async(10.0, ["q"]))
    assert asset is not None and asset.provider_asset_id == "v1"
