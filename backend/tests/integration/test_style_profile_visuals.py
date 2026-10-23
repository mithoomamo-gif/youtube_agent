"""Style Lab (mini-round, W3) — visuals-stage wiring tests.

When a style profile resolves for the spec, the visuals stage must:

* re-plan scene COUNT so the average shot length approaches the
  profile's ``avg_shot_len_s`` — merging sentences for longer shots,
  splitting them at word boundaries for shorter ones — while the
  total stays EXACTLY the measured narration duration (no fabricated
  time; sub-4 s scenes stay legal);
* plan per-scene source classes approximating the profile's
  ``stock_ratio`` / ``ai_ratio`` (±1 scene), spread through the video;
  "ai"-planned scenes use the existing local plate / generated-still
  path and never attempt stock, "stock"-planned scenes try stock
  first and honestly fall back to a plate on a miss (PLANNED vs
  ACTUAL is recorded per scene);
* record ``style_applied == {"visuals": {...}}`` on the result (and
  in the persisted manifest), with ledger rows still written for
  every scene — the real Pixabay rows for actual stock scenes.

With NO profile the behaviour is the pre-Style-Lab behaviour: no
planned classes, no ``style_applied``, every eligible scene attempts
stock exactly as before.

Resolution is exercised through the shared contract only:
``spec.style_profile_path`` first, then the channel-scoped
``<base_dir>/style_profiles/<channel_id>.json``, then the
``YTA_STYLE_PROFILE_PATH`` env var (raw JSON + resolver — W1's
``app.integration.style_profile`` when it has landed, the stage's
defensive fallback resolver otherwise).  All stock access is mocked;
plate rendering is stubbed with a tiny PNG (planning is the subject
here, not pixels — same pattern as test_visuals_short_scenes).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.integration import visuals_stage
from app.integration.visuals_stage import plan_and_generate_visuals

# Alpha-only word bank: no digits (DATA), no quote/list/explanatory/
# intensity markers — every sentence classifies as plain narration.
_BANK = [
    "samundar", "lehren", "kinara", "musafir", "chandni",
    "sitaare", "khamoshi", "manzar", "qissa", "safar",
    "dariya", "hawaein", "roshni", "parcham", "mohabbat",
    "duniya", "zamaana", "tasveer", "khushboo", "awaaz",
]


def _sentences(n: int, words_per: int) -> list[str]:
    return [
        " ".join(_BANK[(i * 3 + w) % len(_BANK)] for w in range(words_per)) + "."
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _fast_plates_and_clean_env(monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)

    def fake_render(out_path, *, palette, seed, phrase, scene_class, hero):
        Image.new("RGB", (8, 8), palette[2]).save(out_path, format="PNG")

    monkeypatch.setattr(visuals_stage, "_render_plate", fake_render)


class FakeFetcher:
    """Mocked StockFetcher contract: hit (unique asset) or miss."""

    def __init__(self, assets_dir: Path, *, hit: bool = True):
        self.assets_dir = Path(assets_dir)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.hit = hit
        self.calls: list[dict] = []

    def fetch_for_scene(self, scene_duration_s, queries, exclude_asset_ids=None):
        self.calls.append(
            {
                "duration_s": scene_duration_s,
                "queries": list(queries or []),
                "exclude": set(exclude_asset_ids or set()),
            }
        )
        if not self.hit:
            return None
        pid = f"px-{len(self.calls):03d}"
        path = self.assets_dir / f"{pid}.png"
        if not path.exists():
            Image.new("RGB", (64, 36), (18, 42, 92)).save(path, format="PNG")
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
            duration_s=None,
            kind="photo",
        )


def _write_profile(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _flat_profile(**overrides) -> dict:
    data = {
        "channel_id": "chan-style-1",
        "stock_ratio": 0.7,
        "ai_ratio": 0.3,
        "avg_shot_len_s": 6.0,
        "cuts_per_min": 10.0,
        "target_duration_s": 60.0,
    }
    data.update(overrides)
    return data


def _inputs(tmp_path, sentences, total_s, *, spec):
    narration = " ".join(sentences)
    bundle = SimpleNamespace(
        chapters=[SimpleNamespace(narration=narration)],
        niche_id="facts",
        video_id="style-test-video",
    )
    voice = SimpleNamespace(duration_s=total_s, chapter_files=[])
    profile = SimpleNamespace(profile_id="facts", visual_style="", taboo=[])
    ctx = SimpleNamespace(
        visuals_dir=str(tmp_path / "proj-1" / "visuals"),
        root=tmp_path / "proj-1",
        video_id="style-test-video",
        spec=spec,
    )
    return bundle, voice, profile, ctx


def _spec_with_profile(path: Path, channel_id: str = "chan-style-1"):
    return SimpleNamespace(
        style_profile_path=str(path), channel_id=channel_id
    )


def _avg_duration(result) -> float:
    durations = [s["duration_s"] for s in result.scene_manifest]
    return sum(durations) / len(durations)


# ---------------------------------------------------------------------------
# Planned mix + style_applied + ledger (explicit-path resolution)
# ---------------------------------------------------------------------------


def test_planned_mix_approximates_ratio_and_records_style_applied(tmp_path):
    sentences = _sentences(10, 10)
    profile_path = _write_profile(tmp_path / "profile.json", _flat_profile())
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    fetcher = FakeFetcher(tmp_path / "assets", hit=True)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=fetcher
    )

    # Scene count unchanged: 60 s / 6 s target = 10 scenes already.
    assert len(result.scene_manifest) == 10
    # Ratio 0.7/0.3 over 10 scenes -> 7 planned stock, 3 planned AI (±0).
    planned = [s.get("planned_class") for s in result.scene_manifest]
    assert planned.count("stock") == 7
    assert planned.count("ai") == 3
    # Only planned-stock scenes ever attempt stock.
    assert len(fetcher.calls) == 7
    # Actuals follow the plan when every fetch hits.
    assert result.stock_count == 7
    assert result.plate_count == 3
    for scene in result.scene_manifest:
        if scene["planned_class"] == "stock":
            assert scene["actual_class"] == "stock"
            assert scene["actual_source"].startswith("stock:pixabay:")
        else:
            assert scene["actual_class"] == "plate"
            assert scene["actual_source"] == "plate:local"
    # style_applied recorded on the result AND in the manifest.
    assert result.style_applied == {
        "visuals": {
            "stock_ratio_target": pytest.approx(0.7),
            "ai_ratio_target": pytest.approx(0.3),
            "avg_shot_len_target_s": pytest.approx(6.0),
            "scenes_planned": 10,
            "planned_stock": 7,
            "planned_ai": 3,
        }
    }
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["style_applied"]["visuals"]["planned_stock"] == 7
    assert manifest["style_applied"]["visuals"]["planned_ai"] == 3
    # Ledger covers every scene; actual stock scenes carry real rows.
    assert len(result.ledger_rows) == 10
    rows_by_scene = {r["scene_id"]: r for r in result.ledger_rows}
    for scene in result.scene_manifest:
        row = rows_by_scene[scene["scene_id"]]
        if scene["actual_class"] == "stock":
            assert row["provider"] == "pixabay"
            assert row["licence_name"] == "Pixabay Content License"
            assert len(row["file_sha256"]) == 64
        else:
            assert row["source"] == "own/generated (local Pillow plate)"


def test_planned_vs_actual_diverges_honestly_on_stock_miss(tmp_path):
    sentences = _sentences(10, 10)
    profile_path = _write_profile(tmp_path / "profile.json", _flat_profile())
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    fetcher = FakeFetcher(tmp_path / "assets", hit=False)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=fetcher
    )

    # Planned-stock scenes attempted stock (7 calls); AI scenes didn't.
    assert len(fetcher.calls) == 7
    # Every fetch missed -> every ACTUAL is a plate, plans unchanged.
    assert result.stock_count == 0
    assert result.plate_count == 10
    planned = [s.get("planned_class") for s in result.scene_manifest]
    assert planned.count("stock") == 7
    assert planned.count("ai") == 3
    assert all(s["actual_source"] == "plate:local" for s in result.scene_manifest)
    assert result.style_applied["visuals"]["planned_stock"] == 7
    assert result.style_applied["visuals"]["planned_ai"] == 3
    # Ledger still covers all scenes, all as generated plate rows.
    assert len(result.ledger_rows) == 10
    assert all(
        r["source"] == "own/generated (local Pillow plate)"
        for r in result.ledger_rows
    )


# ---------------------------------------------------------------------------
# Shot-length targets: splitting and merging within the measured total
# ---------------------------------------------------------------------------


def test_avg_shot_length_split_to_target(tmp_path):
    # 5 long sentences, 60 s measured, target 3 s -> 20 scenes.
    sentences = _sentences(5, 20)
    profile_path = _write_profile(
        tmp_path / "profile.json",
        _flat_profile(avg_shot_len_s=3.0, stock_ratio=0.5, ai_ratio=0.5),
    )
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=FakeFetcher(tmp_path / "a")
    )

    scenes = result.scene_manifest
    assert len(scenes) == 20
    # Average planned shot length within ±25% of the 3 s target, and
    # the timeline still sums to the MEASURED total exactly.
    assert _avg_duration(result) == pytest.approx(3.0, rel=0.25)
    assert sum(s["duration_s"] for s in scenes) == pytest.approx(60.0, abs=0.05)
    assert all(s["end_s"] > s["start_s"] for s in scenes)
    # No narration words were lost or invented by splitting.
    planned_words = sum(len(s["text"].split()) for s in scenes)
    assert planned_words == 100
    # Mix follows the new count: 10 planned stock / 10 planned AI (±1).
    planned = [s.get("planned_class") for s in scenes]
    assert abs(planned.count("stock") - 10) <= 1
    assert abs(planned.count("ai") - 10) <= 1


def test_avg_shot_length_merge_to_target(tmp_path):
    # 20 short sentences, 60 s measured, target 12 s -> 5 scenes.
    sentences = _sentences(20, 5)
    profile_path = _write_profile(
        tmp_path / "profile.json",
        _flat_profile(avg_shot_len_s=12.0, stock_ratio=0.6, ai_ratio=0.4),
    )
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=FakeFetcher(tmp_path / "a")
    )

    scenes = result.scene_manifest
    assert len(scenes) == 5
    assert _avg_duration(result) == pytest.approx(12.0, rel=0.25)
    assert sum(s["duration_s"] for s in scenes) == pytest.approx(60.0, abs=0.05)
    planned_words = sum(len(s["text"].split()) for s in scenes)
    assert planned_words == 100
    planned = [s.get("planned_class") for s in scenes]
    assert planned.count("stock") == 3
    assert planned.count("ai") == 2
    assert result.style_applied["visuals"]["scenes_planned"] == 5


# ---------------------------------------------------------------------------
# Resolution order: channel-scoped file, then env var
# ---------------------------------------------------------------------------


def test_channel_scoped_profile_resolution(tmp_path):
    sentences = _sentences(10, 10)
    # <base>/style_profiles/<channel_id>.json with base = ctx.root.parent
    _write_profile(
        tmp_path / "style_profiles" / "chan-scoped.json",
        _flat_profile(stock_ratio=0.5, ai_ratio=0.5),
    )
    spec = SimpleNamespace(style_profile_path=None, channel_id="chan-scoped")
    bundle, voice, profile, ctx = _inputs(tmp_path, sentences, 60.0, spec=spec)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=FakeFetcher(tmp_path / "a")
    )

    assert result.style_applied["visuals"]["stock_ratio_target"] == pytest.approx(0.5)
    planned = [s.get("planned_class") for s in result.scene_manifest]
    assert planned.count("stock") == 5
    assert planned.count("ai") == 5


def test_env_profile_resolution(tmp_path, monkeypatch):
    sentences = _sentences(10, 10)
    profile_path = _write_profile(
        tmp_path / "env-profile.json",
        _flat_profile(stock_ratio=0.8, ai_ratio=0.2),
    )
    monkeypatch.setenv("YTA_STYLE_PROFILE_PATH", str(profile_path))
    spec = SimpleNamespace(style_profile_path=None, channel_id="chan-unknown")
    bundle, voice, profile, ctx = _inputs(tmp_path, sentences, 60.0, spec=spec)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=FakeFetcher(tmp_path / "a")
    )

    planned = [s.get("planned_class") for s in result.scene_manifest]
    assert planned.count("stock") == 8
    assert planned.count("ai") == 2


def test_stylelab_shaped_profile_aggregates(tmp_path):
    # A profile persisted in Style Lab analysis shape (percent mix +
    # scene aggregates) must yield the same directives.
    sentences = _sentences(10, 10)
    profile_path = _write_profile(
        tmp_path / "stylelab.json",
        {
            "channel_id": "chan-style-1",
            "video": {"duration_s": 60.0},
            "scenes": {"avg_shot_length_s": 6.0, "cuts_per_minute": 10.0},
            "visual_mix": {"stock_like": 70.0, "ai_generated_like": 30.0},
        },
    )
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=FakeFetcher(tmp_path / "a")
    )
    applied = result.style_applied["visuals"]
    assert applied["stock_ratio_target"] == pytest.approx(0.7)
    assert applied["ai_ratio_target"] == pytest.approx(0.3)
    assert applied["avg_shot_len_target_s"] == pytest.approx(6.0)
    assert applied["planned_stock"] == 7
    assert applied["planned_ai"] == 3


def test_w1_schema_profile_resolution(tmp_path):
    # A profile file in the landed W1 schema (style_profile.v1):
    # ratios / pacing live under "visuals", the duration target under
    # "duration.target_s".  W1's resolver + style_directives drive
    # the stage (the local extraction agrees with them).
    sentences = _sentences(10, 10)
    profile_path = _write_profile(
        tmp_path / "w1-profile.json",
        {
            "schema_version": "style_profile.v1",
            "channel_id": "chan-style-1",
            "video_count": 3,
            "duration": {"target_s": 60.0},
            "visuals": {
                "stock_ratio": 0.7,
                "ai_ratio": 0.3,
                "avg_shot_len_s": 6.0,
                "cuts_per_min": 10.0,
            },
        },
    )
    bundle, voice, profile, ctx = _inputs(
        tmp_path, sentences, 60.0, spec=_spec_with_profile(profile_path)
    )
    fetcher = FakeFetcher(tmp_path / "assets", hit=True)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=fetcher
    )

    applied = result.style_applied["visuals"]
    assert applied["stock_ratio_target"] == pytest.approx(0.7)
    assert applied["ai_ratio_target"] == pytest.approx(0.3)
    assert applied["avg_shot_len_target_s"] == pytest.approx(6.0)
    assert applied["planned_stock"] == 7
    assert applied["planned_ai"] == 3
    assert len(fetcher.calls) == 7
    assert result.stock_count == 7


# ---------------------------------------------------------------------------
# No profile -> exact pre-Style-Lab behaviour
# ---------------------------------------------------------------------------


def test_no_profile_regression(tmp_path):
    sentences = _sentences(4, 10)
    spec = SimpleNamespace()  # no style fields at all
    bundle, voice, profile, ctx = _inputs(tmp_path, sentences, 20.0, spec=spec)
    fetcher = FakeFetcher(tmp_path / "assets", hit=True)

    result = plan_and_generate_visuals(
        bundle, voice, profile, ctx, stock_fetcher=fetcher
    )

    assert len(result.scene_manifest) == 4
    # Every scene attempted stock, exactly as before Style Lab.
    assert len(fetcher.calls) == 4
    assert result.stock_count == 4
    # No style artefacts anywhere.
    assert result.style_applied == {}
    for scene in result.scene_manifest:
        assert "planned_class" not in scene
        assert "actual_class" not in scene
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert "style_applied" not in manifest
