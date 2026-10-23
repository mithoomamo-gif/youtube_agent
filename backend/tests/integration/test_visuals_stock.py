"""Round 3 unit tests for the stock-first visuals stage.

All stock access goes through FAKE fetchers — no live calls, and the
real ``PIXABAY_API_KEY`` is never read (tests clear it, or set a dummy
placeholder purely to exercise the enablement branch against fake
parallel modules injected into ``sys.modules``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from app.integration import produce
from app.integration.models import ProjectSpec
from app.integration.visuals_stage import plan_and_generate_visuals

SENTENCES_3 = (
    "The ocean hides deep secrets beneath dark waves today. "
    "Divers found ancient ruins near the silent shore yesterday. "
    "Strange lights appeared above the cold water at midnight."
)
SENTENCES_4 = SENTENCES_3 + (
    " The crew recorded everything on that quiet night carefully."
)


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("DISABLE_STOCK", raising=False)


def _inputs(tmp_path, narration=SENTENCES_3, duration_s=15.0, spec=None):
    bundle = SimpleNamespace(
        chapters=[SimpleNamespace(narration=narration)], niche_id="facts"
    )
    voice = SimpleNamespace(duration_s=duration_s, chapter_files=[])
    profile = SimpleNamespace(profile_id="facts", visual_style="", taboo=[])
    ctx = SimpleNamespace(
        visuals_dir=str(tmp_path / "visuals"),
        video_id="stock-test",
        spec=spec if spec is not None else SimpleNamespace(),
    )
    return bundle, voice, profile, ctx


def _make_asset(stock_dir: Path, pid: str, kind: str = "photo"):
    stock_dir.mkdir(parents=True, exist_ok=True)
    if kind == "video":
        path = stock_dir / f"{pid}.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42-fake-stock-clip")
        media_kind, is_video = "video", True
    else:
        path = stock_dir / f"{pid}.jpg"
        Image.new("RGB", (640, 360), (12, 44, 92)).save(path)
        media_kind, is_video = "photo", False
    return SimpleNamespace(
        local_path=str(path),
        provider="pixabay",
        provider_asset_id=pid,
        asset_page_url=f"https://pixabay.com/media/{pid}/",
        download_url=f"https://cdn.example.test/{pid}",
        licence_name="Pixabay Content License",
        terms_hash="terms-hash-1",
        file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        width=640,
        height=360,
        duration_s=6.0 if is_video else None,
        kind=media_kind,
    )


class RecordingFetcher:
    """Fake StockFetcher: delegates each call to ``behaviour``."""

    def __init__(self, stock_dir: Path, behaviour):
        self.stock_dir = stock_dir
        self.behaviour = behaviour
        self.calls: list[dict] = []

    def fetch_for_scene(self, scene_duration_s, queries, exclude_asset_ids=None):
        exclude = set(exclude_asset_ids or set())
        self.calls.append(
            {
                "duration_s": scene_duration_s,
                "queries": list(queries),
                "exclude": set(exclude),
            }
        )
        return self.behaviour(len(self.calls), list(queries), exclude)


def _unique_photo_fetcher(tmp_path):
    def behaviour(n, queries, exclude):
        return _make_asset(tmp_path / "downloads", f"pix-{n:03d}")

    return RecordingFetcher(tmp_path / "downloads", behaviour)


# ---------------------------------------------------------------------------
# Stock hit path
# ---------------------------------------------------------------------------

def test_stock_hit_path_all_scenes(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    fetcher = _unique_photo_fetcher(tmp_path)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)

    assert result.stock_enabled is True
    assert result.stock_share == 1.0
    assert result.stock_count == 3
    assert result.plate_count == 0
    assert result.counts == {"stock": 3, "plate": 0, "total": 3}
    assert result.path_used == "stock-first"
    assert result.scene_sources == [
        "stock:pixabay:pix-001",
        "stock:pixabay:pix-002",
        "stock:pixabay:pix-003",
    ]
    assert result.sources == result.scene_sources
    for scene in result.scene_manifest:
        assert scene["source"].startswith("stock:pixabay:")
        assert scene["provider"] == "pixabay"
        assert scene["chosen_source"] == "stock"
        assert scene["source_decision"] == "STOCK_MATCH"
        assert scene["is_still"] is True  # photo -> plate-like still + motion
        assert Path(scene["asset_path"]).is_file()
        assert result.asset_files[scene["scene_id"]] == scene["asset_path"]
    # Real ledger rows for the stock scenes.
    assert len(result.ledger_rows) == 3
    for row in result.ledger_rows:
        assert row["licence_name"] == "Pixabay Content License"
        assert row["provider"] == "pixabay"
        # CSV ``source`` is the provider per the stock_ledger contract
        # ("pixabay"); the full provenance string lives on the scene.
        assert "pixabay" in row["source"]
        assert row["asset_page_url"].startswith("https://pixabay.com/")
        assert row["terms_hash"] == "terms-hash-1"
        assert len(row["file_sha256"]) == 64
    # Fetcher contract: duration + non-empty queries per scene.
    assert len(fetcher.calls) == 3
    for call in fetcher.calls:
        assert call["duration_s"] > 0
        assert call["queries"] and all(call["queries"])
    # Manifest on disk carries the reporting data.
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["stock_share"] == 1.0
    assert manifest["stock_count"] == 3
    assert manifest["plate_count"] == 0
    assert manifest["scene_sources"] == result.scene_sources


def test_stock_video_asset_becomes_video_scene(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)

    def behaviour(n, queries, exclude):
        return _make_asset(tmp_path / "downloads", f"vid-{n:03d}", kind="video")

    fetcher = RecordingFetcher(tmp_path / "downloads", behaviour)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    assert result.stock_share == 1.0
    for scene in result.scene_manifest:
        assert scene["is_still"] is False
        assert scene["kind"] == "video"
        assert scene["asset_path"].endswith(".mp4")
    assert result.ledger_rows[0]["kind"] == "video"


# ---------------------------------------------------------------------------
# Fallback paths (never a placeholder / blank)
# ---------------------------------------------------------------------------

def test_fallback_to_plates_when_fetcher_returns_none(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    fetcher = RecordingFetcher(tmp_path, lambda n, q, e: None)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)

    assert result.stock_enabled is True  # mode was on; fetches missed
    assert result.stock_share == 0.0
    assert result.stock_count == 0
    assert result.plate_count == 3
    assert result.path_used == "local-plates"
    assert result.scene_sources == ["plate:local"] * 3
    for scene in result.scene_manifest:
        assert scene["source"] == "plate:local"
        path = Path(scene["asset_path"])
        assert path.is_file() and path.suffix == ".png" and path.stat().st_size > 0
        assert any("stock_fallback" in note for note in scene["notes"])
    for row in result.ledger_rows:
        assert row["source"] == "own/generated (local Pillow plate)"


def test_fallback_to_plates_when_fetcher_raises(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)

    def boom(n, queries, exclude):
        raise RuntimeError("network down")

    fetcher = RecordingFetcher(tmp_path, boom)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    assert result.stock_share == 0.0
    assert result.scene_sources == ["plate:local"] * 3
    assert all(
        Path(s["asset_path"]).is_file() for s in result.scene_manifest
    )
    assert any(
        "stock_fallback" in note and "RuntimeError" in note
        for s in result.scene_manifest
        for note in s["notes"]
    )


# ---------------------------------------------------------------------------
# Anti-repetition
# ---------------------------------------------------------------------------

def test_exclude_asset_ids_grow_per_scene(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    fetcher = _unique_photo_fetcher(tmp_path)
    plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    assert fetcher.calls[0]["exclude"] == set()
    assert fetcher.calls[1]["exclude"] == {"pix-001"}
    assert fetcher.calls[2]["exclude"] == {"pix-001", "pix-002"}


def test_repeated_id_never_reused_even_if_fetcher_ignores_exclusion(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)

    def same_id(n, queries, exclude):
        # Distinct file per call, SAME provider asset id every time.
        return _make_asset(tmp_path / "downloads", "dup-001") if n == 1 else _other_file(tmp_path, n)

    def _other_file(base, n):
        asset = _make_asset(base / "downloads", f"file-{n}")
        asset.provider_asset_id = "dup-001"  # fetcher misbehaves on purpose
        return asset

    fetcher = RecordingFetcher(tmp_path / "downloads", same_id)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)

    assert result.scene_sources[0] == "stock:pixabay:dup-001"
    assert result.scene_sources[1:] == ["plate:local", "plate:local"]
    assert result.stock_count == 1
    stock_ids = [s for s in result.scene_sources if s.startswith("stock:")]
    assert len(stock_ids) == len(set(stock_ids)) == 1
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["used_stock_asset_ids"] == ["dup-001"]


# ---------------------------------------------------------------------------
# Share computation
# ---------------------------------------------------------------------------

def test_stock_share_mixed_hits_and_misses(tmp_path):
    bundle, voice, profile, ctx = _inputs(
        tmp_path, narration=SENTENCES_4, duration_s=20.0
    )

    def half(n, queries, exclude):
        if n <= 2:
            return _make_asset(tmp_path / "downloads", f"mix-{n:03d}")
        return None

    fetcher = RecordingFetcher(tmp_path / "downloads", half)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    assert result.stock_count == 2
    assert result.plate_count == 2
    assert result.stock_share == pytest.approx(0.5)
    assert result.scene_sources[:2] == [
        "stock:pixabay:mix-001",
        "stock:pixabay:mix-002",
    ]
    assert result.scene_sources[2:] == ["plate:local", "plate:local"]
    assert result.path_used == "stock-first"


# ---------------------------------------------------------------------------
# Enablement / stock-off flags
# ---------------------------------------------------------------------------

def test_no_stock_without_fetcher_or_key_is_round2(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx)
    assert result.stock_enabled is False
    assert result.stock_share == 0.0
    assert result.scene_sources == ["plate:local"] * 3
    assert result.path_used == "local-plates"
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["generator"] == "visuals_stage local Pillow plates"
    assert manifest["stock_mode"] == "local-plates"


def test_stock_off_via_spec_use_stock_false(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "dummy-test-value-not-a-real-key")
    bundle, voice, profile, ctx = _inputs(
        tmp_path, spec=SimpleNamespace(use_stock=False)
    )
    fetcher = _unique_photo_fetcher(tmp_path)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    assert fetcher.calls == []  # never even attempted
    assert result.stock_enabled is False
    assert result.scene_sources == ["plate:local"] * 3


def test_stock_off_via_spec_force_flag_and_mode_string(tmp_path):
    for spec in (
        SimpleNamespace(force_stock_off=True),
        SimpleNamespace(stock_mode="off"),
        SimpleNamespace(disable_stock=True),
    ):
        bundle, voice, profile, ctx = _inputs(tmp_path, spec=spec)
        fetcher = _unique_photo_fetcher(tmp_path)
        result = plan_and_generate_visuals(
            bundle, voice, profile, ctx, stock_fetcher=fetcher
        )
        assert fetcher.calls == []
        assert result.scene_sources == ["plate:local"] * 3


def test_stock_off_via_call_kwarg(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    fetcher = _unique_photo_fetcher(tmp_path)
    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=fetcher, use_stock=False
    )
    assert fetcher.calls == []
    assert result.stock_enabled is False
    assert result.scene_sources == ["plate:local"] * 3


def test_fetcher_injected_via_ctx(tmp_path):
    bundle, voice, profile, ctx = _inputs(tmp_path)
    fetcher = _unique_photo_fetcher(tmp_path)
    ctx.stock_fetcher = fetcher
    result = plan_and_generate_visuals(bundle, voice, profile, ctx)
    assert result.stock_share == 1.0
    assert len(fetcher.calls) == 3


# ---------------------------------------------------------------------------
# Env-key enablement through the parallel modules (faked in sys.modules)
# ---------------------------------------------------------------------------

def test_env_key_constructs_fetcher_and_uses_parallel_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "dummy-test-value-not-a-real-key")
    downloads = tmp_path / "downloads"
    constructed: dict = {}

    class FakeStockFetcher:
        def __init__(self, api_key=None, download_dir=None):
            constructed["api_key_present"] = bool(api_key)
            constructed["download_dir"] = download_dir
            self.n = 0

        def fetch_for_scene(self, scene_duration_s, queries, exclude_asset_ids=None):
            constructed.setdefault("queries_seen", []).append(list(queries))
            self.n += 1
            return _make_asset(downloads, f"env-{self.n:03d}")

    query_mod = ModuleType("app.integration.stock_query")
    query_mod.build_query_ladder = lambda scene, profile: [
        f"ladder:{scene['scene_id']}"
    ]
    fetch_mod = ModuleType("app.integration.stock_fetch")
    fetch_mod.StockFetcher = FakeStockFetcher
    ledger_mod = ModuleType("app.integration.stock_ledger")

    def fake_ledger_row(asset, scene):
        return {
            "licence_ref": f"lic-{scene['scene_id']}-fake",
            "scene_id": scene["scene_id"],
            "asset_id": asset.provider_asset_id,
            "source": f"stock:pixabay:{asset.provider_asset_id}",
            "licence_name": "Fake Ledger Licence",
            "licence": "Fake Ledger Licence",
            "kind": "image",
            "local_path": asset.local_path,
            "asset_path": asset.local_path,
            "content_hash": asset.file_sha256,
            "file_sha256": asset.file_sha256,
            "asset_page_url": asset.asset_page_url,
            "download_url": asset.download_url,
            "terms_hash": "fake-terms-hash",
            "width_px": asset.width,
            "height_px": asset.height,
        }

    ledger_mod.ledger_row_for_stock_asset = fake_ledger_row
    monkeypatch.setitem(sys.modules, "app.integration.stock_query", query_mod)
    monkeypatch.setitem(sys.modules, "app.integration.stock_fetch", fetch_mod)
    monkeypatch.setitem(sys.modules, "app.integration.stock_ledger", ledger_mod)

    bundle, voice, profile, ctx = _inputs(tmp_path)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx)

    assert constructed["api_key_present"] is True  # key handed to ctor
    assert result.stock_enabled is True
    assert result.stock_share == 1.0
    # Queries came from the parallel query ladder, not the fallback.
    assert constructed["queries_seen"][0] == ["ladder:scene_0001"]
    # The ledger helper's row was used (and normalised).
    assert result.ledger_rows[0]["licence_name"] == "Fake Ledger Licence"
    assert result.ledger_rows[0]["licence_ref"] == "lic-scene_0001-fake"
    assert result.ledger_rows[0]["terms_hash"] == "fake-terms-hash"


def test_env_key_present_but_stock_module_missing_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "dummy-test-value-not-a-real-key")
    # Simulate the parallel stock modules not existing yet (a None
    # sys.modules entry makes the import fail): the stage must
    # degrade to plates rather than crash.
    for name in (
        "app.integration.stock_fetch",
        "app.integration.stock_query",
        "app.integration.stock_ledger",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    bundle, voice, profile, ctx = _inputs(tmp_path)
    result = plan_and_generate_visuals(bundle, voice, profile, ctx)
    assert result.scene_sources == ["plate:local"] * 3
    assert all(Path(s["asset_path"]).is_file() for s in result.scene_manifest)


# ---------------------------------------------------------------------------
# produce.py report plumbing
# ---------------------------------------------------------------------------

def test_produce_report_shows_visual_sources(monkeypatch, stage_fakes, tmp_path):
    stage_fakes.visuals = SimpleNamespace(
        scene_manifest=[
            {"scene_id": "scene_0001", "source": "stock:pixabay:pix-001"},
            {"scene_id": "scene_0002", "source": "plate:local"},
        ],
        asset_files={},
        ledger_rows=[
            {
                "asset_id": "pix-001", "scene_id": "scene_0001",
                "source": "stock:pixabay:pix-001",
                "licence_name": "Pixabay Content License",
                "kind": "image", "local_path": "/nonexistent/stock.jpg",
            }
        ],
        total_duration_s=55.0,
        path_used="stock-first",
        scene_sources=["stock:pixabay:pix-001", "plate:local"],
        stock_share=0.5,
        stock_count=1,
        plate_count=1,
    )

    def rec(ret):
        return lambda *args: ret

    monkeypatch.setattr(produce, "build_content", rec(stage_fakes.bundle))
    monkeypatch.setattr(produce, "run_gate_a", rec((True, [])))
    monkeypatch.setattr(produce, "synthesize_voice", rec(stage_fakes.voice))
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", rec(stage_fakes.visuals)
    )
    monkeypatch.setattr(produce, "assemble_project", rec(stage_fakes.assemble))
    monkeypatch.setattr(produce, "finalize_publish", rec(stage_fakes.publish))

    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    result = produce.produce_video(spec, tmp_path / "out")
    report = Path(result.report_md).read_text(encoding="utf-8")
    assert "## Visuals Sources" in report
    assert "stock:pixabay:pix-001" in report
    assert "plate:local" in report
    assert "Stock share" in report
    assert result.paths_used["visuals"] == "stock-first"
