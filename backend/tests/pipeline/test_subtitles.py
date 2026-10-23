"""Tests for the Subtitles worker (backend/app/pipeline/subtitles).

No test downloads a whisper model: the whisper path is mocked, and all
other tests use the alignment / stub paths.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# Make the backend package importable regardless of where pytest runs from.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.pipeline.subtitles import (  # noqa: E402
    MAX_LINE_CHARS,
    MAX_LINES_PER_CUE,
    MIN_CUE_DURATION_S,
    OFFSET_TOLERANCE_MS,
    SOURCE_ALIGNMENT_STUB,
    SOURCE_WHISPER,
    CaptionsProfile,
    Cue,
    WhisperTranscriber,
    build_cues,
    build_subtitles,
    check_cue_offsets,
    check_script_match,
    cues_for_burn_in,
    parse_srt,
    render_srt,
    script_match_ratio,
    write_srt_sidecar,
)
from app.pipeline.subtitles.whisper_wrapper import (  # noqa: E402
    STUB_ENV_VAR,
    segments_from_script_duration,
)

SCRIPT = "The valley was silent. A cold wind crossed the dark river slowly."
SENTENCES = [
    "The valley was silent.",
    "A cold wind crossed the dark river slowly.",
]


def synthetic_words(script: str = SCRIPT, word_duration: float = 0.4, gap: float = 0.05):
    """(word, start, end) tuples evenly spaced, one per script token."""
    words = []
    cursor = 0.0
    for token in script.split():
        words.append((token, cursor, cursor + word_duration))
        cursor += word_duration + gap
    return words


# ---------------------------------------------------------------------------
# SRT generation
# ---------------------------------------------------------------------------


def test_srt_generation_roundtrip_valid():
    result = build_subtitles(
        synthetic_words(), script_text=SCRIPT, profile=CaptionsProfile.ENGLISH
    )
    cues = parse_srt(result.srt_text)

    assert len(cues) == len(SENTENCES) == result.cue_count == 2
    assert cues[0].text == "The valley was silent."
    assert cues[1].text == "A cold wind crossed the dark river slowly."

    previous_end = 0.0
    for cue in cues:
        assert cue.start >= previous_end, "cues must be monotonic / non-overlapping"
        assert cue.end > cue.start
        assert len(cue.lines) <= MAX_LINES_PER_CUE
        for line in cue.lines:
            assert len(line) <= MAX_LINE_CHARS
        previous_end = cue.end


def test_long_sentence_splits_into_multiple_cues_within_limits():
    long_sentence = (
        "The old lighthouse keeper watched the storm roll across the harbour, "
        "and he counted every wave that broke against the black rocks below."
    )
    words = synthetic_words(long_sentence, word_duration=0.35)
    cues = build_cues(words, [long_sentence])

    assert len(cues) >= 2
    for cue in cues:
        assert len(cue.lines) <= MAX_LINES_PER_CUE
        assert all(len(line) <= MAX_LINE_CHARS for line in cue.lines)
        assert cue.duration <= 7.0 + 1e-6
    for first, second in zip(cues, cues[1:]):
        assert second.start >= first.end
    # No words lost or duplicated by the splitting.
    assert " ".join(cue.text for cue in cues) == long_sentence


def test_min_cue_duration_is_enforced_without_overlap():
    # Very fast speech: 0.1s words back to back.
    words = synthetic_words("One two three. Four five six.", word_duration=0.1, gap=0.6)
    cues = build_cues(words, ["One two three.", "Four five six."])
    assert len(cues) == 2
    assert cues[0].duration >= MIN_CUE_DURATION_S - 1e-9
    assert cues[1].start >= cues[0].end


def test_cues_use_script_wording_not_timed_wording():
    # Timings carry respelt/garbled tokens; the script must win.
    words = [(f"tok{i}", start, end) for i, (_, start, end) in enumerate(synthetic_words())]
    cues = build_cues(words, SENTENCES)
    assert " ".join(cue.text for cue in cues) == SCRIPT


def test_render_parse_roundtrip_preserves_timings():
    cues = build_cues(synthetic_words(), SENTENCES)
    parsed = parse_srt(render_srt(cues))
    assert len(parsed) == len(cues)
    for original, roundtripped in zip(cues, parsed):
        assert roundtripped.start == pytest.approx(original.start, abs=0.001)
        assert roundtripped.end == pytest.approx(original.end, abs=0.001)
        assert roundtripped.lines == original.lines


# ---------------------------------------------------------------------------
# Offset checker (200 ms gate)
# ---------------------------------------------------------------------------


def test_offset_checker_passes_good_cues():
    reference = synthetic_words()
    cues = build_cues(reference, SENTENCES)
    report = check_cue_offsets(cues, reference)
    assert report.passed
    assert report.status == "PASS"
    assert report.checked_cues == 2
    assert report.max_offset_ms <= OFFSET_TOLERANCE_MS
    assert report.violations == ()


def test_offset_checker_fails_cue_shifted_by_500ms():
    reference = synthetic_words()
    cues = build_cues(reference, SENTENCES)
    shifted = Cue(
        index=cues[1].index,
        start=cues[1].start + 0.5,
        end=cues[1].end + 0.5,
        lines=cues[1].lines,
    )
    report = check_cue_offsets([cues[0], shifted], reference)
    assert not report.passed
    assert report.status == "FAIL"
    assert report.max_offset_ms == pytest.approx(500.0, abs=1.0)
    assert [v.cue_index for v in report.violations] == [shifted.index]
    assert report.violations[0].offset_ms > OFFSET_TOLERANCE_MS


# ---------------------------------------------------------------------------
# Whisper wrapper: stub mode + mocked whisper path (no model downloads)
# ---------------------------------------------------------------------------


def test_stub_mode_forced_flag_uses_alignment_and_marks_provenance():
    alignment = synthetic_words()
    transcriber = WhisperTranscriber(stub_mode=True, alignment_words=alignment)
    segments = transcriber.transcribe("does-not-need-to-exist.wav", language="en")

    assert segments, "stub must produce segments from alignment words"
    assert all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)
    assert all(w.source == SOURCE_ALIGNMENT_STUB for s in segments for w in s.words)
    assert transcriber.last_provenance == SOURCE_ALIGNMENT_STUB
    # Timings come from the alignment data, wording from its tokens.
    assert segments[0].start == pytest.approx(0.0)
    assert segments[0].words[0].word == "The"


def test_stub_mode_env_var(monkeypatch):
    monkeypatch.setenv(STUB_ENV_VAR, "1")
    transcriber = WhisperTranscriber(alignment_words=synthetic_words())
    segments = transcriber.transcribe("missing.wav")
    assert segments and all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)


def test_stub_mode_when_model_loader_fails():
    def broken_loader():
        raise ImportError("No module named 'faster_whisper'")

    transcriber = WhisperTranscriber(
        stub_mode=False,
        model_loader=broken_loader,
        alignment_words=synthetic_words(),
        script_text=SCRIPT,
    )
    segments = transcriber.transcribe("missing.wav", language="en")
    assert segments and all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)
    assert transcriber.last_error and "faster_whisper" in transcriber.last_error


def test_stub_mode_from_script_and_duration():
    segments = segments_from_script_duration(SCRIPT, duration_s=9.0)
    assert segments
    assert all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)
    last_word = segments[-1].words[-1]
    assert last_word.end == pytest.approx(9.0)
    assert segments[0].words[0].start == pytest.approx(0.0)


@dataclass
class _FakeWord:
    word: str
    start: float
    end: float
    probability: float = 0.99


@dataclass
class _FakeSegment:
    text: str
    start: float
    end: float
    words: list


class _FakeModel:
    def transcribe(self, path, language=None, word_timestamps=True, vad_filter=True):
        words = [
            _FakeWord(" Hello", 0.0, 0.4),
            _FakeWord(" world.", 0.4, 0.9),
        ]
        segment = _FakeSegment(text="Hello world.", start=0.0, end=0.9, words=words)
        info = type("Info", (), {"language": language or "en"})()
        return iter([segment]), info


def test_whisper_path_with_mocked_model_marks_whisper_provenance(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"not real audio; the model is mocked")
    transcriber = WhisperTranscriber(stub_mode=False, model_loader=_FakeModel)
    segments = transcriber.transcribe(audio, language="en")

    assert len(segments) == 1
    assert segments[0].source == SOURCE_WHISPER
    assert transcriber.last_provenance == SOURCE_WHISPER
    assert [w.word for w in segments[0].words] == [" Hello", " world."]
    assert segments[0].words[1].start == pytest.approx(0.4)


def test_build_subtitles_from_stub_transcription_segments():
    transcriber = WhisperTranscriber(
        stub_mode=True, alignment_words=synthetic_words(), script_text=SCRIPT
    )
    segments = transcriber.transcribe("missing.wav", language="en")
    result = build_subtitles(
        transcription_segments=segments,
        script_text=SCRIPT,
        profile=CaptionsProfile.ENGLISH,
    )
    assert result.source == SOURCE_ALIGNMENT_STUB
    assert result.cue_count == 2
    assert any("stub" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Script-match helper + profile check
# ---------------------------------------------------------------------------


def test_script_match_ratio_identical_text_is_one():
    assert script_match_ratio(SCRIPT, SCRIPT) == pytest.approx(1.0)
    # Case / punctuation differences must not count against captions.
    assert script_match_ratio(SCRIPT, SCRIPT.upper()) == pytest.approx(1.0)


def test_script_match_ratio_mismatched_text_is_low():
    other = "Completely different words about cooking recipes and football scores today."
    assert script_match_ratio(SCRIPT, other) < 0.3
    assert script_match_ratio(SCRIPT, "") == 0.0


def test_roman_profile_requires_98_percent_match():
    result = build_subtitles(
        synthetic_words(), script_text=SCRIPT, profile=CaptionsProfile.ROMAN_URDU
    )
    assert result.profile is CaptionsProfile.ROMAN_URDU
    assert result.metadata["profile"] == "roman_urdu"
    assert result.script_match is not None
    assert result.script_match.threshold == pytest.approx(0.98)
    assert result.script_match_passed is True

    report = check_script_match(SCRIPT, "The valley was totally different.", "roman_urdu")
    assert report.passed is False
    assert report.ratio < 0.98


# ---------------------------------------------------------------------------
# Sidecar output + burn-in data
# ---------------------------------------------------------------------------


def test_write_srt_sidecar(tmp_path):
    result = build_subtitles(
        synthetic_words(), script_text=SCRIPT, profile=CaptionsProfile.ENGLISH
    )
    path = write_srt_sidecar(result, tmp_path / "episode1")
    assert path.suffix == ".srt"
    assert path.exists()
    parsed = parse_srt(path.read_text(encoding="utf-8"))
    assert len(parsed) == 2


def test_cues_for_burn_in_returns_plain_serialisable_data():
    result = build_subtitles(
        synthetic_words(), script_text=SCRIPT, profile=CaptionsProfile.ENGLISH
    )
    data = cues_for_burn_in(result)
    assert len(data) == 2
    assert data[0]["profile"] == "english"
    assert data[0]["lines"] == list(result.cues[0].lines)
    json.dumps(data)  # must not raise — render module consumes plain data
