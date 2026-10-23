"""Tests for the Style Lab competitor analyser (W6).

The main fixture runs :func:`app.integration.competitor_analyse.analyse_pack`
ONCE against the REAL Round 3 Phase-0 facts pack
(``outputs/round3/phase0/facts_ocean/``: ``final.mp4`` 185.73 s,
``publish_manifest.json``, ``captions.srt``, ``licence_ledger.csv``
with 25 Pixabay rows, ``thumbnail_1.png``) and every test asserts
against that single measured analysis.

Runtime note: the analyser's ffmpeg scene detection decodes the full
185 s 720p video (measured ~107 s on this VM for the
``select='gt(scene,0.4)'`` pass) — under the 120 s budget the task
allows, so the test does NOT sample/truncate the video; the whole
module takes roughly two minutes, dominated by that one fixture.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.integration.competitor_analyse import (
    PALETTE_CLASSES,
    analyse_pack,
    build_analysis_model,
    summarise_analysis,
    to_contract_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "outputs" / "round3" / "phase0" / "facts_ocean"
VIDEO = PACK_DIR / "final.mp4"
MANIFEST = PACK_DIR / "publish_manifest.json"
CAPTIONS = PACK_DIR / "captions.srt"
LEDGER = PACK_DIR / "licence_ledger.csv"
THUMBNAIL = PACK_DIR / "thumbnail_1.png"
ASSET_ID = "own-round3-facts-ocean"

pytestmark = pytest.mark.skipif(
    not all(p.is_file() for p in (VIDEO, MANIFEST, CAPTIONS, LEDGER, THUMBNAIL)),
    reason="Round 3 facts pack artefacts not present in this checkout",
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def analysis(manifest) -> dict:
    return analyse_pack(
        VIDEO,
        title=manifest["title"],
        description=manifest["description"],
        captions_srt=CAPTIONS,
        licence_ledger_csv=LEDGER,
        thumbnail_path=THUMBNAIL,
        asset_id=ASSET_ID,
    )


def test_duration_matches_pack(analysis):
    # ffprobe-measured; the manifest froze 185.733333 s for this file.
    assert analysis["duration_s"] == pytest.approx(185.7, abs=1.0)


def test_speech_wpm_in_sane_band(analysis):
    speech = analysis["speech"]
    assert speech["word_count"] and speech["word_count"] > 100
    assert speech["wpm"] is not None
    assert 100 <= speech["wpm"] <= 260
    # Language comes from the pack manifest when readable (the method
    # actually used is recorded in the notes, not assumed here).
    assert speech["language"]
    assert "language_method" in analysis["analysis_notes"]["speech"]


def test_shots_detected_and_well_formed(analysis):
    shots = analysis["shots"]
    assert len(shots) >= 10
    assert shots[0]["start_s"] == 0.0
    assert shots[-1]["end_s"] == pytest.approx(analysis["duration_s"], abs=0.5)
    for prev, nxt in zip(shots, shots[1:]):
        assert prev["end_s"] == pytest.approx(nxt["start_s"], abs=0.01)
        assert nxt["end_s"] > nxt["start_s"]


def test_visual_stock_share_from_ledger(analysis):
    summary = summarise_analysis(analysis)
    # All 25 scenes of this pack are ledgered Pixabay stock; the
    # ledger-backed timing overlap must classify >80% of shot time.
    assert summary["stock_share"] > 0.8
    stock_shots = [
        s for s in analysis["shots"] if s["visual_class"] == "stock"]
    assert stock_shots, "expected ledger-backed stock shots"
    assert all(s["confidence"] >= 0.8 for s in stock_shots)
    note = analysis["analysis_notes"]["visual_class"]
    assert note["ledger_stock_rows"] == 25
    assert note["timed_scenes_found"] >= 25


def test_seo_texts_carried_into_analysis(analysis, manifest):
    # Analysis-side ONLY: synthesis (style_profile.synthesize) is what
    # strips these from any profile — the analyser must carry them
    # verbatim so pattern extraction can see them.
    assert analysis["seo"]["title_text"] == manifest["title"]
    assert analysis["seo"]["description_text"] == manifest["description"]


def test_chapters_equal_split_estimate_flagged(analysis):
    chapters = analysis["chapters"]
    # The pack description lists 5 chapter lines without timestamps.
    assert len(chapters) == 5
    assert chapters[0]["start_s"] == 0.0
    assert chapters[-1]["end_s"] == pytest.approx(
        analysis["duration_s"], abs=0.5)
    note = analysis["analysis_notes"]["chapters"]
    assert note["estimated"] is True
    assert "EQUAL-SPLIT" in note["method"]
    # Hook = the first chapter's window.
    hook = analysis["hook"]
    assert hook["window_s"] == pytest.approx(
        chapters[0]["end_s"] - chapters[0]["start_s"], abs=0.01)
    assert hook["type"]


def test_music_and_captions_from_pack(analysis):
    # The ledger carries a kind == "music" row (music_bed).
    assert analysis["music"]["present"] is True
    assert analysis["captions"]["present"] is True
    assert analysis["captions"]["burned_in"] is False


def test_thumbnail_heuristics_honest(analysis):
    thumb = analysis["thumbnail"]
    assert thumb["palette_class"] in PALETTE_CLASSES
    # No face detector exists in the repo: False + the explicit note.
    assert thumb["face_present"] is False
    face_note = analysis["analysis_notes"]["thumbnail"]["face_present"]
    assert "not detectable with current tooling" in face_note
    # No OCR in the repo: the word count is null, never guessed.
    assert thumb["text_word_count"] is None


def test_contract_shape_and_provenance(analysis):
    assert analysis["schema_version"] == "competitor_analysis.v1"
    source = analysis["source"]
    assert source["asset_id"] == ASSET_ID
    digest = hashlib.sha256(VIDEO.read_bytes()).hexdigest()
    assert source["sha256"] == digest
    # The contract-pure dict validates as the shared W1 model.
    model = build_analysis_model(analysis)
    assert model.duration_s == pytest.approx(analysis["duration_s"])
    assert len(model.shots) == len(analysis["shots"])
    assert "analysis_notes" not in to_contract_dict(analysis)


def test_synthesised_profile_contains_no_source_text(analysis, manifest):
    # The seam end-to-end: analyser -> contract model -> W1 synthesis.
    # Synthesis itself asserts no-copy; this test additionally checks
    # the serialised profile for the source title/description.
    from app.integration.style_profile import synthesize_style_profile

    model = build_analysis_model(analysis)
    channel_id = manifest.get("channel_id") or "channel"
    profile = synthesize_style_profile([model], channel_id=channel_id)
    blob = profile.model_dump_json().lower()
    assert manifest["title"].lower() not in blob
    assert manifest["description"].lower() not in blob
    assert profile.video_count == 1
    assert profile.visuals.stock_ratio is not None
    assert profile.visuals.stock_ratio > 0.8


def test_missing_video_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyse_pack(
            tmp_path / "nope.mp4",
            title="t", description="d", asset_id="missing-1")
