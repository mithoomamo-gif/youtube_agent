"""Engine orchestration tests (Style Lab mini-round).

All stage functions are fakes (monkeypatched onto the engine module),
so these tests pin the ORCHESTRATION contract only:

* stages run in order and their schema objects land on the result,
* a failing stage degrades to the schema's uncertain state with the
  error class recorded in provenance — the analysis never crashes,
* a missing stage is stubbed with an explicit ``stage_unavailable``
  provenance entry,
* persistence writes analysis.json + breakdown.md (via storage when
  available, via the engine fallback otherwise),
* caller metadata (competitor / channel / source) maps onto the
  schema result.

No network, no real video analysis, no API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.style_analysis import engine, schema


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_probe(_path):
    return {
        "path": str(_path),
        "exists": True,
        "size_bytes": 1234,
        "duration_s": 60.0,
        "width": 1280,
        "height": 720,
        "video_codec": "h264",
        "fps": 30.0,
        "has_audio": True,
        "has_video": True,
    }


def _fake_scenes(_path, _duration):
    segs = [
        schema.SceneSegment(index=0, start_s=0.0, end_s=30.0),
        schema.SceneSegment(index=1, start_s=30.0, end_s=60.0),
    ]
    return schema.SceneAnalysis.from_segments(segs, total_duration_s=60.0)


def _fake_visual(_path, _scenes):
    labels = [
        schema.SceneVisualLabel(scene_index=0, label="stock_like", confidence=0.8),
        schema.SceneVisualLabel(scene_index=1, label="uncertain", confidence=0.3),
    ]
    return schema.VisualMix.from_scene_labels(
        labels, list(_scenes.scenes), confidence=0.55
    )


def _fake_speech(_path, _scenes, _work):
    return schema.SpeechAnalysis(
        transcript_languages=["ur"], language_confidence=0.9, wpm=150.0
    )


def _fake_dressing(_path, _scenes, _speech):
    return schema.DressingAnalysis(
        music_presence=schema.PresenceAssessment(label="present", confidence=0.7)
    )


def _fake_thumbnail(_path):
    return schema.ThumbnailAnalysis(
        text_word_count=3, layout_class="mixed", contrast_class="high",
        confidence=0.6,
    )


@pytest.fixture()
def patched_stages(monkeypatch):
    monkeypatch.setattr(engine, "probe_video", _fake_probe)
    monkeypatch.setattr(engine, "_stage_scenes", _fake_scenes)
    monkeypatch.setattr(engine, "_stage_visual", _fake_visual)
    monkeypatch.setattr(engine, "_stage_speech", _fake_speech)
    monkeypatch.setattr(engine, "_stage_dressing", _fake_dressing)
    monkeypatch.setattr(engine, "_stage_thumbnail", _fake_thumbnail)
    return monkeypatch


@pytest.fixture()
def video(tmp_path) -> Path:
    p = tmp_path / "video.mp4"
    p.write_bytes(b"fake")
    return p


@pytest.fixture()
def thumb(tmp_path) -> Path:
    p = tmp_path / "thumb.png"
    p.write_bytes(b"fake")
    return p


def _prov(result, stage: str) -> dict:
    for entry in result.provenance:
        if entry["stage"] == stage:
            return entry
    raise AssertionError(f"no provenance entry for stage {stage!r}")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_assembles_schema_result(patched_stages, video, thumb):
    result = engine.analyse_video(video, thumbnail_path=thumb)
    assert isinstance(result, schema.StyleLabAnalysis)
    assert result.video.path == str(video)
    assert result.video.duration_s == 60.0
    assert result.video.resolution == "1280x720"
    assert result.scenes.scene_count == 2
    assert result.visual_mix.scene_labels[0].label == "stock_like"
    assert result.speech.transcript_languages == ["ur"]
    assert result.dressing.music_presence.label == "present"
    assert result.thumbnail is not None
    assert result.thumbnail.text_word_count == 3
    for stage in ("probe", "scenes", "visual_classify", "speech", "dressing", "thumbnail"):
        assert _prov(result, stage)["status"] == "ok", stage
        assert stage in result.stage_timings


def test_us_spelling_alias_exists():
    assert engine.analyze_video is engine.analyse_video


def test_no_thumbnail_is_skipped_not_failed(patched_stages, video):
    result = engine.analyse_video(video)
    assert result.thumbnail is None
    assert _prov(result, "thumbnail")["status"] == "skipped"


def test_metadata_maps_onto_result(patched_stages, video):
    result = engine.analyse_video(
        video,
        metadata={
            "competitor_video_id": "comp-42",
            "channel_id": "chan-7",
            "source_url": "https://example.invalid/watch?v=x",
            "title": "kept in payload only",
        },
    )
    assert result.competitor_video_id == "comp-42"
    assert result.channel_id == "chan-7"
    assert result.source_url == "https://example.invalid/watch?v=x"
    payload = engine.result_payload(result)
    assert payload["source_metadata"]["title"] == "kept in payload only"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_stage_error_degrades_to_uncertain(patched_stages, video, monkeypatch):
    def boom(_path, _scenes):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(engine, "_stage_visual", boom)
    result = engine.analyse_video(video)
    assert isinstance(result, schema.StyleLabAnalysis)
    assert result.visual_mix.uncertain == 100.0
    assert result.visual_mix.confidence == 0.0
    prov = _prov(result, "visual_classify")
    assert prov["status"] == "error"
    assert prov["error_class"] == "RuntimeError"
    # Other stages are unaffected.
    assert _prov(result, "scenes")["status"] == "ok"
    assert _prov(result, "speech")["status"] == "ok"


def test_stage_unavailable_is_explicit(patched_stages, video, monkeypatch):
    def missing(_path, _scenes, _work):
        raise engine._StageUnavailable("speech", ImportError("no module"))

    monkeypatch.setattr(engine, "_stage_speech", missing)
    result = engine.analyse_video(video)
    prov = _prov(result, "speech")
    assert prov["status"] == "stage_unavailable"
    assert prov["error_class"] == "ImportError"
    assert "stage_unavailable" in (prov["detail"] or "")
    assert result.speech.transcript_languages == []
    assert result.speech.confidence == 0.0


def test_all_stages_fail_still_returns_result(patched_stages, video, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("nope")

    for name in ("_stage_scenes", "_stage_visual", "_stage_speech", "_stage_dressing"):
        monkeypatch.setattr(engine, name, boom)
    result = engine.analyse_video(video)
    assert isinstance(result, schema.StyleLabAnalysis)
    assert result.scenes.scene_count == 0
    assert result.visual_mix.uncertain == 100.0
    assert result.overall_confidence == 0.0
    assert result.notes  # degradation is surfaced, not hidden


def test_real_missing_stage_module_is_stage_unavailable(
    patched_stages, video, monkeypatch
):
    """Un-patch dressing: if the sibling module cannot be imported the
    engine must record stage_unavailable (not crash). Skipped when the
    real dressing module is present and working."""
    monkeypatch.undo()
    monkeypatch.setattr(engine, "probe_video", _fake_probe)
    monkeypatch.setattr(engine, "_stage_scenes", _fake_scenes)
    monkeypatch.setattr(engine, "_stage_visual", _fake_visual)
    monkeypatch.setattr(engine, "_stage_speech", _fake_speech)
    monkeypatch.setattr(engine, "_stage_thumbnail", _fake_thumbnail)
    real_import = engine._import_sibling

    def guarded(name):
        if name == "dressing":
            return None, ImportError("simulated missing dressing")
        return real_import(name)

    monkeypatch.setattr(engine, "_import_sibling", guarded)
    # _stage_dressing resolves the sibling at call time via the patched
    # importer, so the genuine adapter raises _StageUnavailable.
    result = engine.analyse_video(video)
    prov = _prov(result, "dressing")
    assert prov["status"] == "stage_unavailable"
    assert result.dressing.confidence == 0.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persistence_via_storage(patched_stages, video, thumb, tmp_path):
    out = tmp_path / "store"
    result = engine.analyse_video(
        video,
        thumbnail_path=thumb,
        metadata={"competitor_video_id": "comp-42"},
        out_dir=out,
    )
    payload = engine.result_payload(result)
    assert payload["persistence"]["via"] == "storage"
    analysis_json = out / "comp-42" / "analysis.json"
    breakdown = out / "comp-42" / "breakdown.md"
    assert analysis_json.is_file()
    assert breakdown.is_file()
    assert (out / "index.json").is_file()
    saved = json.loads(analysis_json.read_text(encoding="utf-8"))
    assert saved["competitor_video_id"] == "comp-42"
    assert saved["video"]["path"] == str(video)
    assert "Visual Mix" in breakdown.read_text(encoding="utf-8")


def test_persistence_fallback_when_storage_unavailable(
    patched_stages, video, tmp_path, monkeypatch
):
    real_import = engine._import_sibling

    def guarded(name):
        if name == "storage":
            return None, ImportError("simulated missing storage")
        return real_import(name)

    monkeypatch.setattr(engine, "_import_sibling", guarded)
    out = tmp_path / "store2"
    result = engine.analyse_video(video, out_dir=out)
    payload = engine.result_payload(result)
    assert payload["persistence"]["via"] == "engine_fallback"
    saved = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert saved["video_path"] == str(video)
    assert saved["provenance"]  # fallback JSON keeps provenance
    assert (out / "breakdown.md").is_file()


def test_no_out_dir_no_persistence(patched_stages, video):
    result = engine.analyse_video(video)
    payload = engine.result_payload(result)
    assert "persistence" not in payload


# ---------------------------------------------------------------------------
# compute_visual_mix helper
# ---------------------------------------------------------------------------


def test_compute_visual_mix_counts_and_percentages():
    scenes = [
        {"label": "stock_like", "duration_s": 10.0},
        {"label": "stock_like", "duration_s": 30.0},
        {"label": "uncertain", "duration_s": 10.0},
        {"duration_s": 10.0},  # unlabelled — counted, never dropped
    ]
    mix = engine.compute_visual_mix(scenes)
    assert mix["scene_count"] == 4
    assert mix["counts"] == {"stock_like": 2, "uncertain": 1, "unlabelled": 1}
    assert mix["pct_by_scene"]["stock_like"] == 50.0
    assert mix["pct_by_duration"]["stock_like"] == pytest.approx(66.67, abs=0.01)


def test_compute_visual_mix_empty():
    mix = engine.compute_visual_mix([])
    assert mix["scene_count"] == 0
    assert mix["counts"] == {}


def test_probe_missing_file_degrades(tmp_path):
    info = engine.probe_video(tmp_path / "does-not-exist.mp4")
    assert info["exists"] is False
    assert info["path"].endswith("does-not-exist.mp4")
