"""Style Lab speech tests — structure extraction + transcription.

Two layers:

* Deterministic unit tests over hand-built synthetic segment lists
  (timestamps + text) for hook / beats / CTA / WPM.  No model, no
  network, no audio — :func:`analyse_structure` is pure heuristics.
* Transcription wiring tests with an injected fake transcriber
  (sidecar writing, language collection, fail-loud on stub
  provenance), plus ONE real smoke test: a 60 s clip cut with FFmpeg
  from the Round-3 Phase-0 facts video, transcribed with the real
  ``base`` faster-whisper model through the project provider path.
  The smoke test skips cleanly if the source video or the library is
  unavailable, and prints its findings (language / WPM / hook / wall
  time) for the run report.
"""
from __future__ import annotations

import json
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.style_analysis.speech import (
    SpeechTranscriptionError,
    analyse_structure,
    transcribe,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SMOKE_SOURCE = REPO_ROOT / "outputs" / "round3" / "phase0" / "facts_ocean" / "final.mp4"


def seg(start: float, end: float, text: str, **kw) -> dict:
    return {"start": start, "end": end, "text": text, **kw}


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def test_hook_question():
    out = analyse_structure(
        [
            seg(0.0, 5.0, "What is hiding at the bottom of the ocean?"),
            seg(5.5, 12.0, "The answer begins with the sunlight zone."),
        ],
        duration_s=120.0,
    )
    assert out["hook"]["pattern"] == "question"
    assert out["hook"]["window_s"] == 30.0
    assert out["hook"]["pattern_notes"].startswith("question:")


def test_hook_question_roman_urdu():
    out = analyse_structure(
        [seg(0.0, 6.0, "Kya aap jante hain samundar ki gehrai mein kya hai?")],
        duration_s=90.0,
    )
    assert out["hook"]["pattern"] == "question"


def test_hook_preview():
    out = analyse_structure(
        [
            seg(0.0, 7.0, "In this video we will explore three hidden "
                          "places under the sea."),
            seg(7.5, 15.0, "Each one is stranger than the last."),
        ],
        duration_s=180.0,
    )
    assert out["hook"]["pattern"] == "preview"


def test_hook_bold_claim():
    out = analyse_structure(
        [seg(0.0, 6.0, "This is the deepest secret nobody knows about "
                        "the ocean floor.")],
        duration_s=180.0,
    )
    assert out["hook"]["pattern"] == "bold_claim"


def test_hook_direct_address():
    out = analyse_structure(
        [seg(0.0, 6.0, "Welcome back friends, you will hear a calm "
                        "story with me today.")],
        duration_s=180.0,
    )
    assert out["hook"]["pattern"] == "direct_address"


def test_hook_uncertain_is_not_forced():
    # "uncertain" is the shared schema's first-class no-claim value:
    # a neutral factual opening must NOT be forced into a label.
    out = analyse_structure(
        [seg(0.0, 6.0, "The ocean covers a large part of the planet "
                        "surface area.")],
        duration_s=180.0,
    )
    assert out["hook"]["pattern"] == "uncertain"
    assert out["hook"]["pattern_label"] == "uncertain"


def test_hook_no_speech_is_uncertain():
    out = analyse_structure([], duration_s=60.0)
    assert out["hook"]["pattern"] == "uncertain"
    assert "no speech" in out["hook"]["pattern_notes"]


def test_hook_notes_never_quote_transcript():
    # Schema copyright rule: pattern_notes are structural only.
    secret_sentence = "Zqxjkvb wobblefroth never appears anywhere else"
    out = analyse_structure(
        [seg(0.0, 6.0, secret_sentence + " in the deepest trench.")],
        duration_s=60.0,
    )
    assert "Zqxjkvb" not in out["hook"]["pattern_notes"]
    assert "wobblefroth" not in out["hook"]["pattern_notes"]


def test_hook_question_beats_direct_address():
    # Priority: question (rule 2) wins over direct address (rule 4)
    # even though "you" is present.
    out = analyse_structure(
        [seg(0.0, 5.0, "Have you ever wondered what lies below the waves?")],
        duration_s=120.0,
    )
    assert out["hook"]["pattern"] == "question"


# ---------------------------------------------------------------------------
# WPM
# ---------------------------------------------------------------------------


def test_wpm_uses_speech_time_not_total_duration():
    words = " ".join(["word"] * 120)
    out = analyse_structure(
        [seg(0.0, 30.0, " ".join(words.split()[:60])),
         seg(90.0, 120.0, " ".join(words.split()[60:]))],
        duration_s=120.0,
    )
    # 120 words over 60 s of actual speech (the 30–90 gap is silence)
    assert out["wpm"] == 120.0


def test_wpm_exact_simple():
    out = analyse_structure(
        [seg(0.0, 60.0, " ".join(["ocean"] * 90))], duration_s=60.0
    )
    assert out["wpm"] == 90.0


def test_wpm_no_segments_is_zero():
    assert analyse_structure([], duration_s=60.0)["wpm"] == 0.0


def test_analyse_accepts_transcript_segment_objects():
    from app.pipeline.subtitles.models import TranscriptSegment

    out = analyse_structure(
        [TranscriptSegment(text="hello world this is speech",
                           start=0.0, end=4.0)],
        duration_s=4.0,
    )
    assert out["wpm"] == 75.0  # 5 words / 4 s


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------


def test_beats_split_on_pause_gap_and_summarise():
    out = analyse_structure(
        [
            seg(0.0, 8.0, "Divers face a serious problem below "
                          "two hundred metres."),
            seg(8.4, 16.0, "The pressure becomes a real danger "
                           "for the human body."),
            seg(20.0, 28.0, "For example, a steel can is crushed "
                            "flat in seconds."),
        ],
        duration_s=28.0,
    )
    beats = out["beats"]
    assert len(beats) == 2
    assert beats[0]["start_s"] == 0.0 and beats[0]["end_s"] == 16.0
    assert beats[0]["summary"] == "problem statement"
    assert beats[1]["start_s"] == 20.0
    assert beats[1]["summary"] == "example"


def test_beats_split_on_topic_shift_without_gap():
    out = analyse_structure(
        [
            seg(0.0, 20.0, "The sunlight zone is full of life and "
                           "colour and movement everywhere."),
            seg(20.3, 40.0, "Now another mystery begins in the "
                            "twilight zone below it."),
        ],
        duration_s=40.0,
    )
    assert len(out["beats"]) == 2
    assert out["beats"][1]["start_s"] == 20.3


def test_beats_list_item_summary():
    out = analyse_structure(
        [
            seg(0.0, 15.0, "Three forces shape the deep ocean floor "
                           "and its hidden valleys."),
            seg(15.3, 30.0, "First, the pressure crushes everything "
                            "that descends without protection."),
        ],
        duration_s=30.0,
    )
    assert out["beats"][1]["summary"] == "list item"


def test_beats_tiny_beat_is_merged():
    out = analyse_structure(
        [
            seg(0.0, 10.0, "The trench is the deepest part of the "
                           "whole ocean system."),
            seg(12.0, 13.0, "Wow."),
            seg(20.0, 30.0, "Its floor has never seen sunlight "
                            "in its entire history."),
        ],
        duration_s=30.0,
    )
    assert len(out["beats"]) == 2
    assert out["beats"][1]["start_s"] == 12.0  # tiny beat merged forward
    assert out["beats"][1]["end_s"] == 30.0


def test_beats_long_monologue_split_at_max():
    segments = [
        seg(float(i * 30), float(i * 30 + 30),
            "The current moves slowly through the dark water column.")
        for i in range(4)
    ]
    out = analyse_structure(segments, duration_s=120.0)
    assert len(out["beats"]) == 2
    assert out["beats"][0]["end_s"] == 90.0


def test_beats_first_beat_defaults_to_opening_hook():
    out = analyse_structure(
        [seg(0.0, 20.0, "The canyon stretches far below the calm "
                         "surface waters here.")],
        duration_s=20.0,
    )
    assert out["beats"][0]["summary"] == "opening hook"


# ---------------------------------------------------------------------------
# CTA
# ---------------------------------------------------------------------------


def test_cta_subscribe_and_share_at_end():
    out = analyse_structure(
        [
            seg(0.0, 100.0, "A long calm narration about the sea."),
            seg(100.0, 110.0, "Please subscribe to the channel and "
                              "share this video with friends."),
        ],
        duration_s=120.0,
    )
    cta = out["cta"]
    assert cta["present"] is True
    kinds = {(p["kind"], p["position"]) for p in cta["placements"]}
    assert ("subscribe", "end") in kinds
    assert ("share", "end") in kinds
    sub = next(p for p in cta["placements"] if p["kind"] == "subscribe")
    assert sub["timestamp_s"] == 100.0


def test_cta_comment_in_middle():
    out = analyse_structure(
        [seg(60.0, 70.0, "Let me know in the comments below.")],
        duration_s=180.0,
    )
    assert out["cta"]["present"] is True
    assert out["cta"]["placements"][0]["kind"] == "comment"
    assert out["cta"]["placements"][0]["position"] == "middle"


def test_cta_roman_urdu_subscribe_and_bell():
    out = analyse_structure(
        [seg(170.0, 180.0, "Channel ko subscribe kar dein aur bell "
                           "icon daba dein.")],
        duration_s=180.0,
    )
    kinds = {p["kind"] for p in out["cta"]["placements"]}
    assert "subscribe" in kinds and "bell" in kinds
    assert all(p["position"] == "end" for p in out["cta"]["placements"])


def test_cta_urdu_script_subscribe():
    out = analyse_structure(
        [seg(170.0, 180.0, "براہ کرم چینل کو سبسکرائب کریں شکریہ")],
        duration_s=180.0,
    )
    assert out["cta"]["present"] is True
    assert out["cta"]["placements"][0]["kind"] == "subscribe"


def test_cta_link_in_description_at_start():
    out = analyse_structure(
        [seg(2.0, 8.0, "Link in the description for the full guide.")],
        duration_s=200.0,
    )
    placement = out["cta"]["placements"][0]
    assert placement["kind"] == "link_in_description"
    assert placement["position"] == "start"


def test_cta_absent():
    out = analyse_structure(
        [seg(0.0, 30.0, "Pure narration with no audience ask at all.")],
        duration_s=30.0,
    )
    assert out["cta"]["present"] is False
    assert out["cta"]["placements"] == []


def test_beat_with_cta_summarised_as_call_to_action():
    out = analyse_structure(
        [
            seg(0.0, 40.0, "The story of the trench and its strange "
                           "silent creatures unfolds slowly."),
            seg(45.0, 55.0, "Please subscribe to the channel for "
                            "more stories like this one."),
        ],
        duration_s=55.0,
    )
    assert out["beats"][1]["summary"] == "call to action"


# ---------------------------------------------------------------------------
# transcribe() wiring — fake transcriber, no model
# ---------------------------------------------------------------------------


def _silence_wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class _FakeTranscriber:
    """WhisperTranscriber-shaped double returning fixed segments."""

    def __init__(self, segments, provenance="whisper", error=None):
        self._segments = segments
        self.last_provenance = provenance
        self.last_error = error

    def transcribe(self, audio_path, language=None):
        return list(self._segments)


def test_transcribe_writes_sidecars_and_reports_language(tmp_path):
    from app.pipeline.subtitles.models import TranscriptSegment, WordTiming

    src = _silence_wav(tmp_path / "clip.wav")
    fake = _FakeTranscriber(
        [
            TranscriptSegment(
                text="yeh ek test hai",
                start=0.0,
                end=1.0,
                words=(WordTiming(word="yeh", start=0.0, end=0.25),),
                language="ur",
                source="whisper",
            )
        ]
    )
    result = transcribe(src, output_dir=tmp_path / "out", transcriber=fake)

    assert result["transcript_languages"] == ["ur"]
    assert result["provenance"] == "whisper"
    assert result["segment_count"] == 1
    sidecar = Path(result["transcript_ref"])
    assert sidecar.is_file() and sidecar.suffix == ".json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["segments"][0]["text"] == "yeh ek test hai"
    assert data["segments"][0]["words"][0]["word"] == "yeh"
    assert Path(result["srt_ref"]).is_file()
    # The analysis-facing payload references the transcript; the full
    # analysis structure must not be built by embedding it elsewhere.
    structure = analyse_structure(result["segments"], result["duration_s"])
    assert "yeh ek test hai" not in json.dumps(structure["beats"])


def test_transcribe_fails_loud_on_stub_provenance(tmp_path):
    from app.pipeline.subtitles.models import TranscriptSegment

    src = _silence_wav(tmp_path / "clip.wav")
    fake = _FakeTranscriber(
        [TranscriptSegment(text="fabricated", start=0.0, end=1.0,
                           source="alignment_stub")],
        provenance="alignment_stub",
        error="model missing",
    )
    with pytest.raises(SpeechTranscriptionError, match="stub"):
        transcribe(src, output_dir=tmp_path / "out", transcriber=fake)


def test_transcribe_missing_file():
    with pytest.raises(FileNotFoundError):
        transcribe("/nonexistent/video.mp4")


def test_output_converts_to_shared_schema():
    """The engine's adapter must turn my dicts into schema objects."""
    from app.style_analysis.engine import _to_schema_speech
    from app.style_analysis.schema import SpeechAnalysis

    structure = analyse_structure(
        [
            seg(0.0, 6.0, "What hides below the waves at midnight?"),
            seg(100.0, 110.0, "Please subscribe to the channel now."),
        ],
        duration_s=120.0,
    )
    speech = {
        **structure,
        "transcript_languages": ["ur"],
        "language_note": "note",
        "transcript_ref": "/tmp/x.transcript.json",
    }
    converted = _to_schema_speech(speech)
    assert isinstance(converted, SpeechAnalysis)
    assert converted.transcript_languages == ["ur"]
    assert converted.hook.pattern_label == "question"
    assert converted.beats and converted.beats[0].start_s == 0.0
    assert converted.cta.present is True
    assert converted.cta.placements[0].kind == "subscribe"
    assert converted.cta.placements[0].start_s == 100.0
    assert converted.cta.placements[0].notes == "position: end"
    assert converted.transcript_ref == "/tmp/x.transcript.json"


# ---------------------------------------------------------------------------
# Real smoke test — 60 s clip, real base model (skips when unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not SMOKE_SOURCE.is_file(),
    reason=f"smoke source video missing: {SMOKE_SOURCE}",
)
def test_smoke_real_transcription_60s_clip(tmp_path):
    pytest.importorskip(
        "faster_whisper", reason="faster-whisper not installed"
    )
    clip = tmp_path / "clip60.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "0", "-t", "60",
         "-i", str(SMOKE_SOURCE), "-c", "copy", str(clip)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0 and clip.is_file(), proc.stderr

    from app.style_analysis.speech import analyse_video_speech

    started = time.perf_counter()
    speech = analyse_video_speech(
        clip, output_dir=tmp_path / "out", model_size="base"
    )
    wall_s = time.perf_counter() - started

    print(
        f"\nSMOKE speech: wall={wall_s:.1f}s "
        f"duration={speech['duration_s']}s "
        f"languages={speech['transcript_languages']} "
        f"wpm={speech['wpm']} hook={speech['hook']['pattern']} "
        f"beats={len(speech['beats'])} cta={speech['cta']['present']} "
        f"provenance={speech['provenance']}"
    )
    assert speech["provenance"] == "whisper"
    assert speech["transcript_languages"], "Whisper reported no language"
    assert 30.0 <= speech["duration_s"] <= 75.0
    assert 20.0 <= speech["wpm"] <= 400.0
    assert speech["hook"]["pattern"] in {
        "question", "bold_claim", "preview", "direct_address", "uncertain",
    }
    assert speech["beats"], "no beats derived from a 60 s narration"
    assert Path(speech["transcript_ref"]).is_file()
    assert wall_s < 900.0
