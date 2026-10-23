"""Round-3 tests: assemble-stage subtitles use REAL whisper by default.

The integration subtitles step (``app.integration.assemble_stage``)
must prefer the real faster-whisper transcriber (provider factory
``create_transcriber``) when no TTS word timings exist, and keep the
alignment stub ONLY as an explicit, provenance-marked fallback.

All fast tests mock the transcriber factory — the normal suite never
downloads or loads a model.  The one real-model check is ``slow`` +
env-gated (``YTA_LIVE_WHISPER``), mirroring tests/providers_live/.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration import assemble_stage
from app.pipeline.gates.tokens import Verdict
from app.pipeline.subtitles.models import (
    SOURCE_ALIGNMENT,
    SOURCE_ALIGNMENT_STUB,
    SOURCE_WHISPER,
    CaptionsProfile,
    TranscriptSegment,
    WordTiming,
)
from app.pipeline.subtitles.whisper_wrapper import segments_from_script_duration

SCRIPT = "Hello world. This is a test."


def _whisper_segments() -> list[TranscriptSegment]:
    words = [
        WordTiming("Hello", 0.0, 0.5, source=SOURCE_WHISPER),
        WordTiming("world.", 0.5, 1.0, source=SOURCE_WHISPER),
        WordTiming("This", 1.2, 1.6, source=SOURCE_WHISPER),
        WordTiming("is", 1.6, 1.8, source=SOURCE_WHISPER),
        WordTiming("a", 1.8, 1.9, source=SOURCE_WHISPER),
        WordTiming("test.", 1.9, 2.5, source=SOURCE_WHISPER),
    ]
    return [
        TranscriptSegment("Hello world.", 0.0, 1.0, words=tuple(words[:2]),
                          source=SOURCE_WHISPER),
        TranscriptSegment("This is a test.", 1.2, 2.5, words=tuple(words[2:]),
                          source=SOURCE_WHISPER),
    ]


class FakeTranscriber:
    """Stand-in for WhisperTranscriber with canned transcribe() output."""

    def __init__(self, segments, provenance=SOURCE_WHISPER, error=None,
                 raises: Exception | None = None):
        self._segments = segments
        self.last_provenance = provenance
        self.last_error = error
        self._raises = raises
        self.calls: list[dict] = []

    def transcribe(self, audio_path, language=None, **kwargs):
        self.calls.append({"audio_path": audio_path, "language": language, **kwargs})
        if self._raises is not None:
            raise self._raises
        return list(self._segments)


# ---------------------------------------------------------------------------
# _transcribe_for_subtitles: real-first, stub only as explicit fallback
# ---------------------------------------------------------------------------

def test_prefers_real_transcriber(monkeypatch, tmp_path):
    fake = FakeTranscriber(_whisper_segments())
    factory_calls: list[dict] = []

    def factory(model_size, **kwargs):
        factory_calls.append({"model_size": model_size, **kwargs})
        return fake

    monkeypatch.setattr(assemble_stage, "create_transcriber", factory)
    monkeypatch.delenv(assemble_stage.WHISPER_MODEL_SIZE_ENV_VAR, raising=False)
    notes: dict = {}
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"fake")

    segments = assemble_stage._transcribe_for_subtitles(
        audio, language="ur", script_text=SCRIPT, duration_s=4.0, notes=notes)

    assert segments and all(s.source == SOURCE_WHISPER for s in segments)
    assert notes["subtitle_transcriber_provenance"] == SOURCE_WHISPER
    assert "subtitle_whisper_error" not in notes
    # Factory was asked for the code default model, non-stub (stub_mode
    # explicitly None so the YTA_SUBTITLES_STUB env kill-switch applies),
    # with the script handed over for the internal fallback.
    assert factory_calls[0]["model_size"] == assemble_stage.DEFAULT_WHISPER_MODEL_SIZE
    assert factory_calls[0]["stub_mode"] is None
    assert factory_calls[0]["script_text"] == SCRIPT
    assert fake.calls[0]["language"] == "ur"


def test_model_size_env_override(monkeypatch, tmp_path):
    seen: dict = {}

    def factory(model_size, **kwargs):
        seen["model_size"] = model_size
        return FakeTranscriber(_whisper_segments())

    monkeypatch.setattr(assemble_stage, "create_transcriber", factory)
    monkeypatch.setenv(assemble_stage.WHISPER_MODEL_SIZE_ENV_VAR, "tiny")
    notes: dict = {}
    assemble_stage._transcribe_for_subtitles(
        tmp_path / "a.wav", language="en", script_text=SCRIPT,
        duration_s=4.0, notes=notes)
    assert seen["model_size"] == "tiny"
    assert notes["subtitle_whisper_model"] == "tiny"


def test_factory_failure_falls_back_to_explicit_stub(monkeypatch, tmp_path):
    def factory(model_size, **kwargs):
        raise RuntimeError("faster-whisper unavailable")

    monkeypatch.setattr(assemble_stage, "create_transcriber", factory)

    class StubTranscriber:
        def __init__(self, stub_mode=None, script_text=None, **kwargs):
            assert stub_mode is True  # fallback must be EXPLICIT stub mode
            self.last_provenance = SOURCE_ALIGNMENT_STUB
            self.last_error = "stub mode forced"

        def transcribe(self, audio_path, language=None, **kwargs):
            return segments_from_script_duration(SCRIPT, 4.0)

    monkeypatch.setattr(assemble_stage, "WhisperTranscriber", StubTranscriber)
    notes: dict = {}
    segments = assemble_stage._transcribe_for_subtitles(
        tmp_path / "a.wav", language="en", script_text=SCRIPT,
        duration_s=4.0, notes=notes)

    assert segments and all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)
    assert notes["subtitle_transcriber_provenance"] == SOURCE_ALIGNMENT_STUB
    assert "factory failed" in notes["subtitle_whisper_error"]


def test_transcriber_internal_fallback_is_surfaced(monkeypatch, tmp_path):
    """Real path attempted, model failed, wrapper fell back internally:
    segments keep alignment_stub provenance and the error is recorded."""
    stub_segments = segments_from_script_duration(SCRIPT, 4.0)
    fake = FakeTranscriber(
        stub_segments, provenance=SOURCE_ALIGNMENT_STUB,
        error="ImportError: No module named 'faster_whisper'")
    monkeypatch.setattr(
        assemble_stage, "create_transcriber", lambda *a, **k: fake)
    notes: dict = {}
    segments = assemble_stage._transcribe_for_subtitles(
        tmp_path / "a.wav", language="en", script_text=SCRIPT,
        duration_s=4.0, notes=notes)
    assert segments and all(s.source == SOURCE_ALIGNMENT_STUB for s in segments)
    assert notes["subtitle_transcriber_provenance"] == SOURCE_ALIGNMENT_STUB
    assert "faster_whisper" in notes["subtitle_whisper_error"]


def test_no_timing_source_returns_empty(monkeypatch, tmp_path):
    from app.pipeline.subtitles.whisper_wrapper import SubtitlesStubError

    fake = FakeTranscriber([], raises=SubtitlesStubError("no timing source"))
    monkeypatch.setattr(
        assemble_stage, "create_transcriber", lambda *a, **k: fake)
    notes: dict = {}
    segments = assemble_stage._transcribe_for_subtitles(
        tmp_path / "a.wav", language="en", script_text="", duration_s=None,
        notes=notes)
    assert segments == []
    assert "subtitle_stub_error" in notes


# ---------------------------------------------------------------------------
# Language normalisation + transcription-audio resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,profile,expected", [
    ("roman_urdu", CaptionsProfile.ROMAN_URDU, "ur"),
    ("Urdu", CaptionsProfile.URDU_SCRIPT, "ur"),
    ("ur-PK", CaptionsProfile.URDU_SCRIPT, "ur"),
    ("english", CaptionsProfile.ENGLISH, "en"),
    ("en", CaptionsProfile.ENGLISH, "en"),
    (None, CaptionsProfile.URDU_SCRIPT, "ur"),
    (None, CaptionsProfile.ENGLISH, "en"),
    ("fr", CaptionsProfile.ENGLISH, "fr"),
])
def test_whisper_language_normalisation(raw, profile, expected):
    assert assemble_stage._whisper_language(raw, profile) == expected


def test_transcription_audio_prefers_narration_stem(tmp_path):
    master = tmp_path / "master.wav"
    master.write_bytes(b"m")
    narration = tmp_path / "narration.wav"
    narration.write_bytes(b"n")
    voice = SimpleNamespace(narration_wav=str(narration))
    assert assemble_stage._resolve_transcription_audio(voice, master) == narration
    missing = SimpleNamespace(narration_wav=str(tmp_path / "gone.wav"))
    assert assemble_stage._resolve_transcription_audio(missing, master) == master
    assert assemble_stage._resolve_transcription_audio(None, master) == master


# ---------------------------------------------------------------------------
# assemble_project level: heavy stages faked, subtitles wiring real
# ---------------------------------------------------------------------------

class _FakeRenderer:
    def __init__(self, out_dir, encoder_choice=None):
        self.out_dir = Path(out_dir)

    def render_all(self, specs):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "seg_0001.ts"
        path.write_bytes(b"ts")
        return [path]


def _fake_render_draft(segment_paths, out, audio=None, encoder_choice=None):
    Path(out).write_bytes(b"mp4")


def _fake_qc(*args, **kwargs):
    return SimpleNamespace(
        verdict=Verdict.PASS,
        loudness=SimpleNamespace(integrated_lufs=-14.0, true_peak_dbtp=-1.0,
                                 lra_lu=2.0, source="fake"),
        checks=[],
        detections={},
    )


def _patch_heavy_stages(monkeypatch, tmp_path):
    monkeypatch.setattr(assemble_stage, "probe_encoder",
                        lambda: SimpleNamespace(encoder="fake"))
    monkeypatch.setattr(assemble_stage, "SegmentRenderer", _FakeRenderer)
    monkeypatch.setattr(assemble_stage, "render_draft", _fake_render_draft)
    monkeypatch.setattr(assemble_stage, "probe_duration", lambda p: 4.0)
    monkeypatch.setattr(assemble_stage, "run_technical_qc", _fake_qc)
    thumbs = []
    for i in range(3):
        t = tmp_path / f"variant_{i}.png"
        t.write_bytes(b"png")
        thumbs.append(SimpleNamespace(
            path=str(t), layout_id="fake", headline="Ocean",
            legibility=None, warnings=[]))
    monkeypatch.setattr(
        assemble_stage, "generate_variants",
        lambda *a, **k: SimpleNamespace(variants=thumbs))


def _assemble_inputs(tmp_path, **voice_overrides):
    audio = tmp_path / "master.wav"
    audio.write_bytes(b"fake-audio")
    voice_data = dict(mixed_master_wav=str(audio), duration_s=4.0)
    voice_data.update(voice_overrides)
    bundle = SimpleNamespace(
        script_text=SCRIPT, title="Ocean Test", thumbnail_text="Ocean")
    ctx = SimpleNamespace(pack_dir=str(tmp_path / "pack"),
                          work_dir=str(tmp_path / "work"))
    profile = SimpleNamespace(language="english")
    return bundle, SimpleNamespace(**voice_data), [], profile, ctx


def test_assemble_uses_real_whisper_by_default(monkeypatch, tmp_path):
    _patch_heavy_stages(monkeypatch, tmp_path)
    fake = FakeTranscriber(_whisper_segments())
    monkeypatch.setattr(
        assemble_stage, "create_transcriber", lambda *a, **k: fake)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    assert result.subtitle_source == SOURCE_WHISPER
    assert result.subtitle_path_used == "whisper"
    provenance = result.notes["subtitle_provenance"]
    assert provenance["source"] == SOURCE_WHISPER
    assert provenance["whisper_model"] == assemble_stage.DEFAULT_WHISPER_MODEL_SIZE
    assert provenance["transcriber_provenance"] == SOURCE_WHISPER
    assert "Hello world" in result.captions_srt
    srt_on_disk = Path(result.pack_dir, "captions.srt").read_text(encoding="utf-8")
    assert "Hello world" in srt_on_disk


def test_assemble_falls_back_to_provenance_marked_stub(monkeypatch, tmp_path):
    _patch_heavy_stages(monkeypatch, tmp_path)

    def factory(model_size, **kwargs):
        raise RuntimeError("no whisper here")

    monkeypatch.setattr(assemble_stage, "create_transcriber", factory)

    class StubTranscriber:
        def __init__(self, stub_mode=None, script_text=None, **kwargs):
            self.last_provenance = SOURCE_ALIGNMENT_STUB
            self.last_error = "stub mode forced"

        def transcribe(self, audio_path, language=None, **kwargs):
            return segments_from_script_duration(SCRIPT, 4.0)

    monkeypatch.setattr(assemble_stage, "WhisperTranscriber", StubTranscriber)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    assert result.subtitle_source == SOURCE_ALIGNMENT_STUB
    assert result.subtitle_path_used == "alignment-stub"
    provenance = result.notes["subtitle_provenance"]
    assert provenance["source"] == SOURCE_ALIGNMENT_STUB
    assert "factory failed" in provenance["whisper_error"]
    assert "Hello world" in result.captions_srt


def test_assemble_alignment_timings_still_win_and_skip_whisper(
        monkeypatch, tmp_path):
    _patch_heavy_stages(monkeypatch, tmp_path)

    def factory(*a, **k):
        raise AssertionError("whisper must not run when TTS timings exist")

    monkeypatch.setattr(assemble_stage, "create_transcriber", factory)
    timings = [
        WordTiming("Hello", 0.0, 0.5), WordTiming("world.", 0.5, 1.0),
        WordTiming("This", 1.2, 1.6), WordTiming("is", 1.6, 1.8),
        WordTiming("a", 1.8, 1.9), WordTiming("test.", 1.9, 2.5),
    ]
    bundle, voice, visuals, profile, ctx = _assemble_inputs(
        tmp_path, word_timings=timings)

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)
    assert result.subtitle_source == SOURCE_ALIGNMENT
    assert result.subtitle_path_used == "alignment"


# ---------------------------------------------------------------------------
# Slow / live: the REAL model on the Round-2 facts narration (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_real_whisper_on_round2_facts_narration():
    if os.environ.get("YTA_LIVE_WHISPER", "").strip().lower() not in {
            "1", "true", "yes", "on"}:
        pytest.skip("YTA_LIVE_WHISPER not set — real-model check is opt-in")
    pytest.importorskip("faster_whisper")
    repo_root = Path(__file__).resolve().parents[3]
    audio = (repo_root / "outputs" / "round2" / "facts_ocean"
             / "samundar-ki-gehrai-ke-raaz-1952544f" / "audio" / "narration.wav")
    if not audio.is_file():
        pytest.skip("Round-2 facts narration.wav not present")
    notes: dict = {}
    segments = assemble_stage._transcribe_for_subtitles(
        audio, language="ur", script_text="", duration_s=None, notes=notes)
    assert segments, "real whisper returned no segments"
    assert notes["subtitle_transcriber_provenance"] == SOURCE_WHISPER
    assert all(s.source == SOURCE_WHISPER for s in segments)
    assert all(s.words for s in segments), "word timestamps expected"
