"""Real-FFmpeg tests for the audio pipeline (worker 3).

No mocks: every fixture is generated with FFmpeg (``aevalsrc`` tones at
known amplitude / ``anullsrc`` silence), run through the real pipeline
(assembly -> music prep -> sidechain mix -> two-pass loudnorm), and the
finished master is verified with an independent loudness re-measurement.

Clips are kept short (2-5 s) so the suite stays fast.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# The repo has no installed package; make ``app.pipeline.audio`` importable
# straight from the backend tree (namespace packages, no build step).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.audio import (  # noqa: E402
    assemble_voice_stem,
    detect_silence_gaps,
    measure_loudness,
    mix_and_master,
    mix_stems,
    prepare_music,
    qc_check,
)

SR = 48_000


# ---------------------------------------------------------------------------
# FFmpeg fixture helpers
# ---------------------------------------------------------------------------

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def make_tone(path: Path, *, freq: float, duration_s: float, amp: float = 0.45,
              channels: int = 1) -> Path:
    """Deterministic sine tone: aevalsrc amplitude is exact (amp 0.5 -> -6 dBFS)."""
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"aevalsrc={amp}*sin(2*PI*{freq}*t):s={SR}:d={duration_s}",
        "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", str(channels),
        str(path),
    ])
    return path


def make_silence(path: Path, *, duration_s: float, channels: int = 1) -> Path:
    _ffmpeg([
        "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl={'mono' if channels == 1 else 'stereo'}",
        "-t", str(duration_s), "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", str(channels),
        str(path),
    ])
    return path


def probe(path: Path, entries: str) -> str:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[0]


def duration_s(path: Path) -> float:
    return float(probe(path, "format=duration"))


def max_volume_db(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        if "max_volume" in line:
            return float(line.rsplit(":", 1)[1].strip().split()[0])
    raise AssertionError(f"volumedetect gave no max_volume for {path}")


# ---------------------------------------------------------------------------
# Shared end-to-end build (module-scoped: the loudnorm passes are the
# expensive part, build once and assert many times)
# ---------------------------------------------------------------------------

SCENE_DUR_S = 2.0
GAP_MS = 800.0
EXPECTED_PROGRAMME_S = SCENE_DUR_S * 2 + GAP_MS / 1000.0  # 4.8 s


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("audio_pipeline")
    scene1 = make_tone(work / "scene1.wav", freq=440, duration_s=SCENE_DUR_S)
    scene2 = make_tone(work / "scene2.wav", freq=550, duration_s=SCENE_DUR_S)
    # Music source is deliberately *shorter* than the programme (3 s < 4.8 s)
    # so prepare_music must loop it.
    music_raw = make_tone(work / "music_raw.wav", freq=220, duration_s=3.0,
                          amp=0.5, channels=2)

    voice = assemble_voice_stem([scene1, scene2], [GAP_MS], work / "voice_stem.wav")
    music = prepare_music(music_raw, work / "music_stem.wav", target=voice)
    master = work / "master.wav"
    result = mix_and_master(voice, music, master)
    assert result.output_path == master
    return {"scene1": scene1, "scene2": scene2, "music_raw": music_raw,
            "voice": voice, "music": music, "master": master}


# ---------------------------------------------------------------------------
# Voice stem assembly
# ---------------------------------------------------------------------------

def test_voice_stem_duration_includes_gap(built: dict[str, Path]) -> None:
    assert duration_s(built["voice"]) == pytest.approx(EXPECTED_PROGRAMME_S, abs=0.15)


def test_voice_stem_format_is_house_standard(built: dict[str, Path]) -> None:
    assert probe(built["voice"], "stream=sample_rate") == str(SR)
    assert probe(built["voice"], "stream=channels") == "1"  # mono


def test_voice_stem_single_scene_no_gaps(tmp_path: Path) -> None:
    scene = make_tone(tmp_path / "only.wav", freq=330, duration_s=1.5)
    out = assemble_voice_stem([scene], None, tmp_path / "stem.wav")
    assert duration_s(out) == pytest.approx(1.5, abs=0.1)


def test_assemble_rejects_mismatched_gap_list(tmp_path: Path) -> None:
    scene = make_tone(tmp_path / "s.wav", freq=330, duration_s=1.0)
    with pytest.raises(ValueError):
        assemble_voice_stem([scene, scene, scene], [500.0], tmp_path / "x.wav")


def test_assemble_rejects_no_scenes(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        assemble_voice_stem([], None, tmp_path / "x.wav")


# ---------------------------------------------------------------------------
# Music stem
# ---------------------------------------------------------------------------

def test_music_stem_loops_to_programme_duration(built: dict[str, Path]) -> None:
    assert duration_s(built["music"]) == pytest.approx(EXPECTED_PROGRAMME_S, abs=0.15)
    assert probe(built["music"], "stream=channels") == "2"  # stereo


def test_music_stem_is_quiet_bed(built: dict[str, Path]) -> None:
    # Raw tone was 0.5 amp (-6 dBFS peak); the 0.22 bed volume plus fades
    # must put the stem's peak well below the narration register.
    assert max_volume_db(built["music"]) < -15.0


# ---------------------------------------------------------------------------
# Mix + two-pass loudnorm mastering (the headline spec test)
# ---------------------------------------------------------------------------

def test_master_loudness_within_tolerance(built: dict[str, Path]) -> None:
    m = measure_loudness(built["master"])  # independent re-measurement
    assert m.integrated_lufs == pytest.approx(-14.0, abs=1.5)
    assert m.true_peak_dbtp <= -0.5


def test_master_passes_qc_gate(built: dict[str, Path]) -> None:
    qc = qc_check(built["master"])  # gate tolerance is the tight ±1 LUFS
    assert qc.passed, qc.failures
    assert qc.loudness_ok and qc.peak_ok
    assert qc.measurement.integrated_lufs == pytest.approx(-14.0, abs=1.0)
    assert qc.measurement.true_peak_dbtp <= -1.0


def test_master_is_48k_stereo_not_loudnorms_192k(built: dict[str, Path]) -> None:
    # Guards the mandatory aresample=48000 after loudnorm (which otherwise
    # leaves the WAV at loudnorm's internal 192 kHz).
    assert probe(built["master"], "stream=sample_rate") == str(SR)
    assert probe(built["master"], "stream=channels") == "2"
    assert duration_s(built["master"]) == pytest.approx(EXPECTED_PROGRAMME_S, abs=0.2)


def test_mix_with_sfx_stem_follows_voice_duration(tmp_path: Path) -> None:
    voice = make_tone(tmp_path / "v.wav", freq=440, duration_s=2.5)
    music = make_tone(tmp_path / "m.wav", freq=220, duration_s=2.5, amp=0.3, channels=2)
    sfx = make_tone(tmp_path / "s.wav", freq=880, duration_s=1.0, amp=0.2, channels=2)
    outcome = mix_stems(voice, music, tmp_path / "premix.wav", sfx=sfx)
    assert outcome.output_path.is_file()
    assert outcome.sidechain_used  # sidechain path must work for 3 stems
    assert duration_s(outcome.output_path) == pytest.approx(2.5, abs=0.15)


# ---------------------------------------------------------------------------
# amix normalize=0 (mandatory) — indirect level test
# ---------------------------------------------------------------------------

def test_amix_normalize_zero_preserves_programme_level(tmp_path: Path) -> None:
    """With a silent voice the sidechain does not duck, so the premix peak
    must equal the music stem's own measured peak.

    With amix's default normalize=1 the same mix would come out ~6 dB
    lower (divided by the 2 inputs) — we build that reference mix with
    plain FFmpeg here and require the pipeline mix to be clearly louder.
    """
    voice = make_silence(tmp_path / "silent_voice.wav", duration_s=3.0)
    music = make_tone(tmp_path / "music.wav", freq=220, duration_s=3.0,
                      amp=0.3, channels=2)
    outcome = mix_stems(voice, music, tmp_path / "premix.wav")
    pipeline_peak = max_volume_db(outcome.output_path)
    music_peak = max_volume_db(music)

    reference = tmp_path / "reference_normalize1.wav"
    _ffmpeg([
        "-i", str(voice), "-i", str(music),
        "-filter_complex",
        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[v];"
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[m];"
        "[v][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=1[x]",
        "-map", "[x]", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2",
        str(reference),
    ])
    reference_peak = max_volume_db(reference)

    # normalize=0: the premix peak is the music stem's own peak, untouched.
    # (Expected value is read off the generated stem itself: FFmpeg's
    # mono->stereo upmix in make_tone applies a -3 dB pan law, so the
    # stem's absolute peak is a fixture property, not a pipeline one.)
    assert pipeline_peak == pytest.approx(music_peak, abs=1.0)
    assert pipeline_peak - reference_peak > 4.0  # ~6 dB = the normalize penalty


# ---------------------------------------------------------------------------
# Silence-gap detector (suspected failed TTS chunk)
# ---------------------------------------------------------------------------

def test_detects_four_second_gap(tmp_path: Path) -> None:
    head = make_tone(tmp_path / "head.wav", freq=440, duration_s=1.0)
    gap = make_silence(tmp_path / "gap.wav", duration_s=4.0)
    tail = make_tone(tmp_path / "tail.wav", freq=440, duration_s=1.0)
    joined = tmp_path / "joined.wav"
    _ffmpeg([
        "-i", str(head), "-i", str(gap), "-i", str(tail),
        "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[x]",
        "-map", "[x]", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "1",
        str(joined),
    ])
    gaps = detect_silence_gaps(joined)
    assert len(gaps) == 1
    assert gaps[0].duration_s == pytest.approx(4.0, abs=0.3)
    assert gaps[0].start_s == pytest.approx(1.0, abs=0.2)


def test_short_chapter_pause_is_not_flagged(built: dict[str, Path]) -> None:
    # The assembled stem contains an intentional 800 ms chapter pause —
    # far under the 3 s suspected-failed-TTS threshold.
    assert detect_silence_gaps(built["voice"]) == []
