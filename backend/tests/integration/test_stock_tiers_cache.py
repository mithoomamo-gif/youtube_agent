"""Unit tests for Round-4 stock_fetch hardening.

Covers (a) smallest-adequate download-tier selection and (b) the
content-cache size cap with LRU eviction in
``app.integration.stock_fetch``.  Fully mocked: a fake adapter returns
crafted candidates (with Pixabay-shaped ``download_tiers``) and an
injected downloader serves recorded payloads.  No network, no API key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integration.stock_fetch import (
    DEFAULT_CACHE_MAX_BYTES,
    StockFetcher,
    _normalise_candidate,
    _select_tier,
)
from app.providers.stock.models import Candidate, MediaType


# --- fakes ---------------------------------------------------------------------


class FakeAdapter:
    """Duck-types the Pixabay adapter (async search only).

    Deliberately has NO ``cache`` attribute, which also exercises the
    fetcher's 24h file search cache.  Downloads go through the
    fetcher's injected ``downloader``, never through this adapter.
    """

    name = "pixabay"

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.search_calls: list[tuple[str, str]] = []

    async def search(self, query, media_type, filters=None):
        kind = "video" if media_type == MediaType.VIDEO else "photo"
        self.search_calls.append((query, kind))
        return list(self.results.get((query, kind), []))


def tier(quality, width, height, size=None, asset="x"):
    t = {
        "quality": quality,
        "url": f"https://cdn.example.test/video/{asset}-{quality}.mp4",
        "width": width,
        "height": height,
        "fps": 30.0,
    }
    if size is not None:
        t["size"] = size
    return t


def make_video(asset_id, tiers=None, **kw) -> Candidate:
    """Candidate shaped like the real Pixabay adapter's output: tiers
    sorted widest-first and the LARGEST tier promoted to download_url."""
    tiers = list(tiers or [])
    base = dict(
        provider="pixabay",
        provider_asset_id=str(asset_id),
        media_type=MediaType.VIDEO,
        download_url=tiers[0]["url"] if tiers else f"https://cdn.example.test/video/{asset_id}.mp4",
        download_tiers=tiers,
        page_url=f"https://pixabay.com/videos/id-{asset_id}/",
        duration_s=30.0,
        width=tiers[0]["width"] if tiers else 1920,
        height=tiers[0]["height"] if tiers else 1080,
        fps=30.0,
        tags=["deep", "ocean"],
        downloads=100,
    )
    base.update(kw)
    return Candidate(**base)


MB = 1024 * 1024
ROUND3_TIERS = [  # widest-first, like the adapter emits
    tier("large", 3840, 2160, 135 * MB),
    tier("medium", 1920, 1080, 42 * MB),
    tier("small", 1280, 720, 15 * MB),
    tier("tiny", 640, 360, 3 * MB),
]


class RecordingDownloader:
    """Serves a distinct payload per URL and records bytes served."""

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.urls: list[str] = []
        self.bytes_served = 0

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        data = self.payloads[url]
        self.bytes_served += len(data)
        return data


def payloads_for(cand: Candidate, size_by_quality: dict[str, int]) -> dict[str, bytes]:
    out = {}
    for i, t in enumerate(cand.download_tiers):
        size = size_by_quality[t["quality"]]
        out[t["url"]] = bytes([i + 1]) * size
    if cand.download_url not in out:
        out[cand.download_url] = b"plain-bytes"
    return out


def fetch_one(tmp_path, cand: Candidate, payloads, **fetcher_kw):
    adapter = FakeAdapter({("q", "video"): [cand]})
    downloader = RecordingDownloader(payloads)
    fetcher = StockFetcher(
        adapter=adapter, cache_dir=tmp_path / "stock",
        downloader=downloader, **fetcher_kw,
    )
    asset = fetcher.fetch_for_scene(10.0, ["q"])
    return asset, downloader


# --- tier selection: _select_tier unit level ------------------------------------


def test_select_tier_only_uhd_picks_smallest_adequate():
    tiers = [tier("large", 3840, 2160, 135 * MB), tier("medium", 2560, 1440, 70 * MB)]
    picked, fallback = _select_tier(tiers)
    assert picked["width"] == 2560 and fallback is False


def test_select_tier_mixed_modest_1920_wins():
    picked, fallback = _select_tier(ROUND3_TIERS)
    # 42 MiB vs 15 MiB base: ratio 2.8 <= 3.0 and delta 27 MiB <= 32 MiB.
    assert picked["quality"] == "medium" and picked["width"] == 1920
    assert fallback is False


def test_select_tier_immodest_ratio_stays_at_1280():
    tiers = [
        tier("large", 3840, 2160, 200 * MB),
        tier("medium", 1920, 1080, 100 * MB),  # 6.7x the base, +85 MiB
        tier("small", 1280, 720, 15 * MB),
    ]
    picked, fallback = _select_tier(tiers)
    assert picked["quality"] == "small" and fallback is False


def test_select_tier_immodest_absolute_delta_stays_at_1280():
    tiers = [
        tier("medium", 1920, 1080, 55 * MB),  # ratio 2.75 ok, delta 35 MiB > 32 MiB
        tier("small", 1280, 720, 20 * MB),
    ]
    picked, _ = _select_tier(tiers)
    assert picked["quality"] == "small"


def test_select_tier_none_adequate_falls_back_to_largest():
    tiers = [tier("small", 854, 480, 5 * MB), tier("tiny", 640, 360, 2 * MB)]
    picked, fallback = _select_tier(tiers)
    assert picked["width"] == 854 and fallback is True


def test_select_tier_area_proxy_without_sizes():
    tiers = [  # no size keys at all -> width*height proxy (ratio 2.25)
        tier("large", 3840, 2160), tier("medium", 1920, 1080),
        tier("small", 1280, 720),
    ]
    picked, fallback = _select_tier(tiers)
    assert picked["quality"] == "medium" and fallback is False


def test_select_tier_empty_and_unusable():
    assert _select_tier([]) is None
    assert _select_tier([{"quality": "x", "url": "", "width": 1920}]) is None


def test_normalise_records_fallback_metadata():
    cand = _normalise_candidate(
        {
            "id": "lowres",
            "width": 854,
            "height": 480,
            "download_url": "https://cdn.example.test/video/lowres-small.mp4",
            "download_tiers": [
                tier("small", 854, 480, 5 * MB, asset="lowres"),
                tier("tiny", 640, 360, 2 * MB, asset="lowres"),
            ],
        },
        "video",
        0,
    )
    assert cand is not None
    assert cand.tier_fallback is True
    assert cand.download_url.endswith("-small.mp4")  # largest available
    assert cand.width == 854


# --- tier selection: end-to-end through the fetch path ---------------------------


def test_fetch_downloads_modest_1920_tier_not_uhd(tmp_path):
    cand = make_video("a1", tiers=[dict(t, url=t["url"].replace("/x-", "/a1-")) for t in ROUND3_TIERS])
    payloads = payloads_for(cand, {"large": 4000, "medium": 2000, "small": 700, "tiny": 100})
    asset, downloader = fetch_one(tmp_path, cand, payloads)
    assert asset is not None
    assert asset.width == 1920 and asset.tier_quality == "medium"
    assert asset.tier_fallback is False
    assert downloader.urls == [cand.download_tiers[1]["url"]]  # medium only
    assert Path(asset.local_path).read_bytes() == payloads[cand.download_tiers[1]["url"]]


def test_fetch_immodest_1920_downloads_1280_tier(tmp_path):
    tiers = [
        tier("large", 3840, 2160, 200 * MB, asset="b1"),
        tier("medium", 1920, 1080, 100 * MB, asset="b1"),
        tier("small", 1280, 720, 15 * MB, asset="b1"),
    ]
    cand = make_video("b1", tiers=tiers)
    payloads = payloads_for(cand, {"large": 4000, "medium": 3000, "small": 700})
    asset, downloader = fetch_one(tmp_path, cand, payloads)
    assert asset.width == 1280 and asset.tier_quality == "small"
    assert downloader.bytes_served == 700


def test_fetch_only_uhd_tiers_picks_smallest(tmp_path):
    tiers = [
        tier("large", 3840, 2160, 135 * MB, asset="c1"),
        tier("medium", 2560, 1440, 70 * MB, asset="c1"),
    ]
    cand = make_video("c1", tiers=tiers)
    payloads = payloads_for(cand, {"large": 4000, "medium": 2500})
    asset, downloader = fetch_one(tmp_path, cand, payloads)
    assert asset.width == 2560 and asset.tier_quality == "medium"
    assert downloader.bytes_served == 2500


def test_fetch_no_tiers_keeps_download_url(tmp_path):
    cand = make_video("d1")  # no download_tiers at all
    payloads = {cand.download_url: b"plain-bytes"}
    asset, _ = fetch_one(tmp_path, cand, payloads)
    assert asset is not None and asset.download_url == cand.download_url
    assert asset.tier_quality == "" and asset.tier_fallback is False


def test_byte_accounting_selected_tiers_total(tmp_path):
    """Unit-level byte accounting: 5 Round-3-shaped scenes must serve
    the 1920 tier's bytes (5 x 42 MiB), never the UHD tier's (5 x 135)."""
    adapter_results = {}
    payloads: dict[str, bytes] = {}
    expected = 0
    for i in range(5):
        aid = f"s{i}"
        tiers = [dict(t, url=t["url"].replace("/x-", f"/{aid}-")) for t in ROUND3_TIERS]
        # Payload lengths equal the declared tier sizes would be huge;
        # scale down 1:1024 — the accounting identity is what matters:
        # served bytes must equal the SELECTED tier's payload sum.
        scaled = {"large": 1350, "medium": 420, "small": 150, "tiny": 30}
        cand = make_video(aid, tiers=tiers)
        adapter_results[(f"q{i}", "video")] = [cand]
        payloads.update(payloads_for(cand, scaled))
        expected += scaled["medium"]
    downloader = RecordingDownloader(payloads)
    fetcher = StockFetcher(adapter=FakeAdapter(adapter_results),
                           cache_dir=tmp_path / "stock", downloader=downloader)
    for i in range(5):
        asset = fetcher.fetch_for_scene(10.0, [f"q{i}"])
        assert asset is not None and asset.width == 1920
    assert downloader.bytes_served == expected == 5 * 420


# --- cache cap / LRU eviction -----------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def sized_fetcher(tmp_path, clock, cap, sizes: dict[str, int]):
    """Fetcher whose downloader serves ``sizes[asset_id]`` distinct bytes."""
    results = {}
    payloads = {}
    for aid, size in sizes.items():
        cand = make_video(aid)
        results[(f"q-{aid}", "video")] = [cand]
        # Fill byte derived from the asset id (stable across fetcher
        # instances in this process) so distinct assets never collide
        # into one content-addressed file by accident.
        payloads[cand.download_url] = bytes([sum(aid.encode()) % 250 + 1]) * size
    downloader = RecordingDownloader(payloads)
    fetcher = StockFetcher(
        adapter=FakeAdapter(results), cache_dir=tmp_path / "stock",
        downloader=downloader, clock=clock, cache_max_bytes=cap,
    )
    return fetcher, downloader


def read_index(tmp_path) -> dict:
    return json.loads((tmp_path / "stock" / "index.json").read_text())


def test_cap_enforced_lru_evicted_first(tmp_path):
    clock = Clock()
    f1, _ = sized_fetcher(tmp_path, clock, cap=1500, sizes={"a": 600, "b": 600})
    clock.now = 1.0
    f1.fetch_for_scene(10.0, ["q-a"])
    clock.now = 2.0
    f1.fetch_for_scene(10.0, ["q-b"])
    # New fetcher (new "run"): a and b are no longer active/protected.
    f2, _ = sized_fetcher(tmp_path, clock, cap=1500, sizes={"c": 600})
    clock.now = 3.0
    f2.fetch_for_scene(10.0, ["q-c"])
    index = read_index(tmp_path)
    assert set(index) == {"b", "c"}  # a was least recently used
    assert Path(index["b"]["local_path"]).is_file()
    assert Path(index["c"]["local_path"]).is_file()
    total = sum(e["size_bytes"] for e in index.values())
    assert total <= 1500


def test_index_hit_refreshes_last_access(tmp_path):
    clock = Clock()
    f1, dl1 = sized_fetcher(tmp_path, clock, cap=1500, sizes={"a": 600, "b": 600})
    clock.now = 1.0
    f1.fetch_for_scene(10.0, ["q-a"])
    clock.now = 2.0
    f1.fetch_for_scene(10.0, ["q-b"])
    f2, dl2 = sized_fetcher(tmp_path, clock, cap=1500, sizes={"a": 600, "c": 600})
    clock.now = 3.0
    again = f2.fetch_for_scene(10.0, ["q-a"])  # index hit: no download
    assert again is not None and dl2.urls == []
    clock.now = 4.0
    f2.fetch_for_scene(10.0, ["q-c"])
    index = read_index(tmp_path)
    assert set(index) == {"a", "c"}  # b became the LRU after a's touch


def test_active_run_files_never_evicted(tmp_path):
    clock = Clock()
    fetcher, _ = sized_fetcher(tmp_path, clock, cap=1000, sizes={"a": 600, "b": 600})
    clock.now = 1.0
    fetcher.fetch_for_scene(10.0, ["q-a"])
    clock.now = 2.0
    fetcher.fetch_for_scene(10.0, ["q-b"])  # total 1200 > cap, both active
    index = read_index(tmp_path)
    assert set(index) == {"a", "b"}
    assert all(Path(e["local_path"]).is_file() for e in index.values())
    # A later run is not protected: adding c evicts a then b (LRU order).
    f2, _ = sized_fetcher(tmp_path, clock, cap=1000, sizes={"c": 600})
    clock.now = 3.0
    f2.fetch_for_scene(10.0, ["q-c"])
    assert set(read_index(tmp_path)) == {"c"}


def test_shared_content_file_counted_once_and_removed_with_last_entry(tmp_path):
    clock = Clock()
    # x and y return identical bytes -> one content-addressed file.
    results = {}
    payloads = {}
    for aid in ("x", "y", "z"):
        cand = make_video(aid)
        results[(f"q-{aid}", "video")] = [cand]
        payloads[cand.download_url] = b"S" * 600 if aid in ("x", "y") else b"Z" * 300
    dl = RecordingDownloader(payloads)
    f1 = StockFetcher(adapter=FakeAdapter(results), cache_dir=tmp_path / "stock",
                      downloader=dl, clock=clock, cache_max_bytes=1000)
    clock.now = 1.0
    ax = f1.fetch_for_scene(10.0, ["q-x"])
    clock.now = 2.0
    ay = f1.fetch_for_scene(10.0, ["q-y"])
    assert ax.local_path == ay.local_path  # shared file
    clock.now = 3.0
    f1.fetch_for_scene(10.0, ["q-z"])
    # Indexed total counts the shared file once: 600 + 300 <= 1000 cap,
    # so nothing is evicted.
    index = read_index(tmp_path)
    assert set(index) == {"x", "y", "z"}
    assert Path(ax.local_path).is_file()
    # Now force eviction of the shared file from a fresh run.
    f2, _ = sized_fetcher(tmp_path, clock, cap=1000, sizes={"w": 600})
    clock.now = 4.0
    f2.fetch_for_scene(10.0, ["q-w"])
    index = read_index(tmp_path)
    assert "x" not in index and "y" not in index  # both entries went together
    assert not Path(ax.local_path).is_file()  # file removed with last entry
    # Index stayed consistent: every remaining entry points at a real file.
    assert all(Path(e["local_path"]).is_file() for e in index.values())


def test_cap_disabled_with_zero(tmp_path):
    clock = Clock()
    fetcher, _ = sized_fetcher(tmp_path, clock, cap=0, sizes={"a": 600, "b": 600})
    fetcher.fetch_for_scene(10.0, ["q-a"])
    f2, _ = sized_fetcher(tmp_path, clock, cap=0, sizes={"c": 600})
    f2.fetch_for_scene(10.0, ["q-c"])
    assert set(read_index(tmp_path)) == {"a", "c"}


def test_index_file_survives_eviction_and_stays_valid_json(tmp_path):
    clock = Clock()
    f1, _ = sized_fetcher(tmp_path, clock, cap=700, sizes={"a": 600})
    f1.fetch_for_scene(10.0, ["q-a"])
    f2, _ = sized_fetcher(tmp_path, clock, cap=700, sizes={"b": 600})
    f2.fetch_for_scene(10.0, ["q-b"])
    index_path = tmp_path / "stock" / "index.json"
    assert index_path.is_file()
    assert set(json.loads(index_path.read_text())) == {"b"}


def test_cache_cap_config_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("YTA_STOCK_CACHE_MAX_BYTES", raising=False)
    f = StockFetcher(adapter=FakeAdapter(), cache_dir=tmp_path / "s")
    assert f.cache_max_bytes == DEFAULT_CACHE_MAX_BYTES == 2 * 1024**3
    monkeypatch.setenv("YTA_STOCK_CACHE_MAX_BYTES", "12345")
    f = StockFetcher(adapter=FakeAdapter(), cache_dir=tmp_path / "s")
    assert f.cache_max_bytes == 12345
    f = StockFetcher(adapter=FakeAdapter(), cache_dir=tmp_path / "s",
                     cache_max_bytes=999)
    assert f.cache_max_bytes == 999  # explicit arg beats env
    monkeypatch.setenv("YTA_STOCK_CACHE_MAX_BYTES", "not-a-number")
    f = StockFetcher(adapter=FakeAdapter(), cache_dir=tmp_path / "s")
    assert f.cache_max_bytes == DEFAULT_CACHE_MAX_BYTES
