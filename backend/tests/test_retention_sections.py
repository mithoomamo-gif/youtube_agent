"""Tests for app.retention_sections (retention curve -> section mapping).

Coverage:
* a synthetic curve with a known cliff mapped to the correct section,
  with hand-computed per-section stats;
* the boundary tie rule (a drop starting exactly on a section edge
  belongs to the section that starts there);
* the REAL Round 3 facts pack (its actual scene-manifest chapter
  boundaries) with a synthetic-but-labelled retention curve;
* honest states: unmapped (curve, no map) and no_data (no curve);
* section-map derivations (scene manifest, word-proportional,
  SRT word-count alignment, voiceover WAV durations) and the pack
  loader's precedence / fallbacks;
* the file-based storage seam (Style Lab precedent) round-trip.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from app.retention_sections import (
    SectionMap,
    SectionSpan,
    RetentionSectionStore,
    load_section_map,
    map_retention_to_sections,
    map_snapshot_retention,
    parse_retention_curve,
)
from app.retention_sections.section_maps import (
    section_map_from_chapter_durations,
    section_map_from_scene_manifest,
    section_map_from_srt_wordcount,
    section_map_from_voiceover_dir,
    section_map_from_word_counts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FACTS_PROJECT = (
    REPO_ROOT
    / "outputs/round3/phase0/facts_ocean/samundar-ki-gehrai-ke-raaz-r3"
)


def _curve(times_watches: list[tuple[float, float]], duration: float):
    return [
        {"elapsed_ratio": t / duration, "watch_ratio": w} for t, w in times_watches
    ]


def _two_section_map() -> SectionMap:
    return SectionMap(
        sections=[
            SectionSpan(index=0, title="Intro", start_s=0.0, end_s=100.0),
            SectionSpan(index=1, title="Main Event", start_s=100.0, end_s=200.0),
        ]
    )


def _cliff_curve():
    """200s video, flat 0.90, cliff 110s->120s (0.90 -> 0.50), slow tail."""
    times = list(range(0, 201, 10))
    watches = []
    for t in times:
        if t <= 110:
            watches.append(0.90)
        elif t == 120:
            watches.append(0.50)
        else:
            watches.append(0.50 - 0.01 * ((t - 120) // 10))
    return _curve(list(zip(times, watches)), 200.0)


# ---------------------------------------------------------------------------
# Core mapping
# ---------------------------------------------------------------------------


def test_known_cliff_maps_to_correct_section():
    result = map_retention_to_sections(_cliff_curve(), 200.0, _two_section_map())
    assert result.status == "ok"
    drop = result.steepest_drop
    assert drop is not None
    assert drop.at_s == 110.0
    assert drop.at_text == "1:50"
    assert drop.drop == pytest.approx(0.40)
    assert drop.section_index == 1
    assert drop.section_title == "Main Event"
    assert drop.seconds_into_section == pytest.approx(10.0)
    # The headline annotation names the section and the timestamp.
    assert any(
        "Steepest drop at 1:50" in note and "Main Event" in note
        for note in result.annotations
    )


def test_per_section_stats_hand_computed():
    result = map_retention_to_sections(_cliff_curve(), 200.0, _two_section_map())
    intro, main = result.sections
    assert intro.entry_ratio == pytest.approx(0.90)
    assert intro.exit_ratio == pytest.approx(0.90)
    assert intro.mean_ratio == pytest.approx(0.90)
    assert intro.drop_off == pytest.approx(0.0)
    assert main.entry_ratio == pytest.approx(0.90)
    assert main.exit_ratio == pytest.approx(0.42)
    # Trapezoidal mean over [100, 200] computed by hand = 52.8 / 100.
    assert main.mean_ratio == pytest.approx(0.528)
    assert main.drop_off == pytest.approx(0.48)
    assert main.relative_drop == pytest.approx(0.48 / 0.90)
    assert main.steepest_drop is not None
    assert main.steepest_drop.at_s == 110.0
    assert intro.steepest_drop is None  # flat section: no drop interval
    overall = result.overall
    assert overall is not None
    assert overall.mean_ratio == pytest.approx(142.8 / 200.0)
    assert overall.top_drops[0].at_s == 110.0


def test_interpolation_between_samples():
    curve = _curve([(0.0, 1.0), (100.0, 0.0)], 100.0)
    section_map = SectionMap(
        sections=[SectionSpan(index=0, title="Only", start_s=0.0, end_s=100.0)]
    )
    result = map_retention_to_sections(curve, 100.0, section_map)
    only = result.sections[0]
    assert only.entry_ratio == pytest.approx(1.0)
    assert only.exit_ratio == pytest.approx(0.0)
    assert only.mean_ratio == pytest.approx(0.5)  # linear ramp trapezoid


def test_boundary_tie_rule_drop_on_edge_belongs_to_starting_section():
    # Cliff interval starts exactly at the 100s boundary.
    times = list(range(0, 201, 10))
    watches = [0.90 if t <= 100 else 0.50 if t == 110 else 0.49 for t in times]
    # fix the tail: flat-ish after 110
    watches = []
    for t in times:
        if t <= 100:
            watches.append(0.90)
        elif t == 110:
            watches.append(0.50)
        else:
            watches.append(0.49)
    result = map_retention_to_sections(
        _curve(list(zip(times, watches)), 200.0), 200.0, _two_section_map()
    )
    drop = result.steepest_drop
    assert drop is not None and drop.at_s == 100.0
    assert drop.section_title == "Main Event"  # NOT "Intro"
    assert drop.seconds_into_section == pytest.approx(0.0)


def test_curve_parsing_shapes():
    as_json = json.dumps([{"elapsed_ratio": 0.0, "watch_ratio": 1.0}])
    assert len(parse_retention_curve(as_json)) == 1
    api_names = [{"elapsedVideoTimeRatio": 0.5, "audienceWatchRatio": 0.8}]
    points = parse_retention_curve(api_names)
    assert points[0].elapsed_ratio == 0.5 and points[0].watch_ratio == 0.8
    pairs = parse_retention_curve([[0.25, 0.9]])
    assert pairs[0].watch_ratio == 0.9
    wrapped = parse_retention_curve({"points": pairs})
    assert len(wrapped) == 1


def test_overlapping_sections_rejected():
    bad = SectionMap(
        sections=[
            SectionSpan(index=0, title="A", start_s=0.0, end_s=120.0),
            SectionSpan(index=1, title="B", start_s=100.0, end_s=200.0),
        ]
    )
    with pytest.raises(ValueError, match="overlaps"):
        map_retention_to_sections(_cliff_curve(), 200.0, bad)


def test_invalid_duration_rejected():
    with pytest.raises(ValueError):
        map_retention_to_sections(_cliff_curve(), 0.0, _two_section_map())


def test_elapsed_ratio_marginally_above_one_is_clamped_not_rejected():
    # The YouTube provider documents elapsed_ratio as "0..1+".
    curve = [
        {"elapsed_ratio": 0.0, "watch_ratio": 1.0},
        {"elapsed_ratio": 1.02, "watch_ratio": 0.40},
    ]
    result = map_retention_to_sections(curve, 200.0, _two_section_map())
    assert result.status == "ok"
    assert result.overall is not None
    assert result.overall.exit_ratio == pytest.approx(0.40)


def test_map_snapshot_retention_reads_ingestion_store_shape():
    snapshot = {
        "id": "snap-77",
        "youtube_video_id": "yt-abc",
        "project_id": "proj-1",
        "retention_curve_json": json.dumps(_cliff_curve()),
    }
    result = map_snapshot_retention(snapshot, 200.0, _two_section_map())
    assert result.snapshot_id == "snap-77"
    assert result.video_id == "yt-abc"
    assert result.project_id == "proj-1"
    assert result.status == "ok"
    # Parsed-curve variant + the store's external-id alias.
    snapshot2 = dict(snapshot, retention_curve=_cliff_curve())
    del snapshot2["retention_curve_json"]
    snapshot2["video_external_id"] = snapshot2.pop("youtube_video_id")
    result2 = map_snapshot_retention(snapshot2, 200.0, _two_section_map())
    assert result2.video_id == "yt-abc" and result2.status == "ok"


# ---------------------------------------------------------------------------
# Honest states
# ---------------------------------------------------------------------------


def test_unmapped_curve_still_gets_overall_stats():
    result = map_retention_to_sections(_cliff_curve(), 200.0, None)
    assert result.status == "unmapped"
    assert result.sections == []
    assert result.timing_source is None
    assert result.overall is not None
    assert result.overall.entry_ratio == pytest.approx(0.90)
    assert result.overall.exit_ratio == pytest.approx(0.42)
    drop = result.steepest_drop
    assert drop is not None and drop.at_s == 110.0
    assert drop.section_title is None  # never fabricated
    assert drop.seconds_into_section is None
    assert any("unmapped" in note for note in result.annotations)


def test_no_data_states():
    for empty in (None, [], "", '{"points": []}'):
        result = map_retention_to_sections(empty, 200.0, _two_section_map())
        assert result.status == "no_data"
        assert result.overall is None
        assert result.sections == []
        assert result.steepest_drop is None


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


def test_word_proportional_derivation():
    result = section_map_from_word_counts(["A", "B"], [100, 300], 80.0)
    assert result is not None and result.source == "word_proportional"
    assert [s.start_s for s in result.sections] == [0.0, 20.0]
    assert result.sections[-1].end_s == 80.0


def test_chapter_durations_derivation():
    result = section_map_from_chapter_durations(["A", "B"], [30.0, 50.0])
    assert result is not None
    assert [(s.start_s, s.end_s) for s in result.sections] == [(0.0, 30.0), (30.0, 80.0)]


def test_scene_manifest_derivation():
    manifest = {
        "total_duration_s": 30.0,
        "scenes": [
            {"chapter_index": 0, "start_s": 0.0, "end_s": 5.0},
            {"chapter_index": 0, "start_s": 5.0, "end_s": 12.0},
            {"chapter_index": 1, "start_s": 12.0, "end_s": 30.0},
        ],
    }
    result = section_map_from_scene_manifest(manifest, ["One", "Two"])
    assert result is not None and result.source == "scene_manifest"
    assert [(s.start_s, s.end_s) for s in result.sections] == [(0.0, 12.0), (12.0, 30.0)]
    assert result.duration_s == 30.0


def test_srt_wordcount_derivation():
    chapters = [
        {"title": "First", "text": " ".join(["w"] * 10)},
        {"title": "Second", "text": " ".join(["w"] * 10)},
    ]
    srt = (
        "1\n00:00:00,000 --> 00:00:10,000\n"
        "one two three four five six seven eight nine ten\n\n"
        "2\n00:00:10,000 --> 00:00:20,000\n"
        "one two three four five six seven eight nine ten\n"
    )
    result = section_map_from_srt_wordcount(chapters, srt)
    assert result is not None and result.source == "srt_wordcount"
    assert result.sections[0].end_s == pytest.approx(10.0, abs=0.01)
    assert result.sections[1].end_s == pytest.approx(20.0)


def test_voiceover_wav_derivation(tmp_path):
    for name, seconds in (("chapter_01_aaaa.wav", 1.0), ("chapter_02_bbbb.wav", 2.0)):
        path = tmp_path / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    result = section_map_from_voiceover_dir(tmp_path, ["One", "Two"])
    assert result is not None and result.source == "voiceover_chapters"
    assert [(s.start_s, s.end_s) for s in result.sections] == [(0.0, 1.0), (1.0, 3.0)]


def test_loader_word_proportional_fallback(tmp_path):
    bundle = {
        "chapters": [
            {"index": 1, "title": "A", "text": " ".join(["w"] * 50)},
            {"index": 2, "title": "B", "text": " ".join(["w"] * 50)},
        ]
    }
    (tmp_path / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    (tmp_path / "publish_manifest.json").write_text(
        json.dumps({"duration_seconds": 100.0}), encoding="utf-8"
    )
    result = load_section_map(tmp_path)
    assert result is not None and result.source == "word_proportional"
    assert result.sections[0].end_s == pytest.approx(50.0)


def test_loader_empty_dir_returns_none(tmp_path):
    assert load_section_map(tmp_path) is None
    assert load_section_map(tmp_path / "does-not-exist") is None


# ---------------------------------------------------------------------------
# Real Round 3 facts pack
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FACTS_PROJECT.is_dir(), reason="Round 3 facts pack not present")
def test_real_facts_pack_section_map():
    section_map = load_section_map(FACTS_PROJECT)
    assert section_map is not None
    assert section_map.source == "scene_manifest"
    assert len(section_map.sections) == 5
    boundaries = [section_map.sections[0].start_s] + [
        s.end_s for s in section_map.sections
    ]
    assert boundaries == pytest.approx(
        [0.0, 43.573, 82.124, 124.678, 155.49, 185.744], abs=0.01
    )
    titles = [s.title for s in section_map.sections]
    assert titles[0].startswith("Hook")
    assert titles[2] == "Dabao aur Woh Log Jo Teh Tak Gaye"


@pytest.mark.skipif(not FACTS_PROJECT.is_dir(), reason="Round 3 facts pack not present")
def test_real_facts_pack_labelled_curve_cliff_in_chapter_3():
    section_map = load_section_map(FACTS_PROJECT)
    assert section_map is not None
    duration = section_map.duration_s
    assert duration == pytest.approx(185.744)

    def watch(t: float) -> float:
        base = 0.90 - 0.01 * (t / 5.0)
        if t >= 105.0:
            base -= 0.14  # the labelled cliff, realised over 100s -> 105s
        return base

    times = [float(t) for t in range(0, 186, 5)] + [duration]
    curve = _curve([(t, watch(t)) for t in times], duration)
    result = map_retention_to_sections(
        curve, duration, section_map, video_id="facts-ocean-r3"
    )
    assert result.status == "ok"
    assert result.timing_source == "scene_manifest"
    drop = result.steepest_drop
    assert drop is not None
    assert drop.at_s == pytest.approx(100.0)
    assert drop.at_text == "1:40"
    assert drop.section_index == 2
    assert drop.section_title == "Dabao aur Woh Log Jo Teh Tak Gaye"
    assert drop.seconds_into_section == pytest.approx(100.0 - 82.124, abs=0.01)
    chapter3 = result.sections[2]
    assert chapter3.start_s == pytest.approx(82.124)
    assert chapter3.end_s == pytest.approx(124.678)
    assert chapter3.drop_off > 0.2  # cliff + baseline decline inside the chapter
    assert any("Dabao aur Woh Log Jo Teh Tak Gaye" in n for n in result.annotations)


# ---------------------------------------------------------------------------
# Storage seam
# ---------------------------------------------------------------------------


def test_storage_round_trip(tmp_path):
    result = map_retention_to_sections(
        _cliff_curve(), 200.0, _two_section_map(),
        snapshot_id="snap-123", video_id="vid-9",
    )
    store = RetentionSectionStore(tmp_path)
    stored = store.save(result)
    assert stored.key == "snap-123"
    assert stored.analysis_path.is_file()
    assert stored.annotations_path.is_file()
    assert "Main Event" in stored.annotations_path.read_text(encoding="utf-8")

    loaded = store.load("snap-123")
    assert loaded is not None
    assert loaded.status == "ok"
    assert loaded.steepest_drop is not None
    assert loaded.steepest_drop.section_title == "Main Event"

    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].snapshot_id == "snap-123"
    assert entries[0].steepest_drop_section == "Main Event"
    assert entries[0].steepest_drop_at_s == pytest.approx(110.0)

    # Re-saving the same snapshot replaces, never duplicates.
    store.save(result)
    assert len(store.list_entries()) == 1
    assert store.load("missing") is None
