"""Audio pipeline for the YouTube Agent app (worker 3).

Composable, file-in / file-out stages (FFmpeg via subprocess, no MoviePy):

    assemble_voice_stem(scene_wavs, gaps_ms, out)   -> voice stem WAV
    prepare_music(music_in, out, duration_s=...)    -> music stem WAV
    mix_stems(voice, music, out, sfx=None)          -> pre-master mix WAV
    loudnorm_two_pass(src, out)                     -> mastered WAV
    mix_and_master(voice, music, out, sfx=None)     -> MasterResult
    measure_loudness(path)                          -> LoudnessMeasurement
    detect_silence_gaps(path)                       -> list[SilenceGap]
    qc_check(path)                                  -> QCResult (PASS/FAIL)

Targets: −14 LUFS integrated, ≤ −1 dBTP, LRA 11, 48 kHz (roadmap §18/§10).
"""

from .mix import loudnorm_two_pass, mix_and_master, mix_stems
from .models import (
    DEFAULT_TARGETS,
    LoudnessMeasurement,
    LoudnessTargets,
    MasterResult,
    MixOutcome,
    QCResult,
    SilenceGap,
)
from .music import prepare_music
from .qc import detect_silence_gaps, measure_loudness, qc_check
from .voice import assemble_voice_stem

__all__ = [
    "DEFAULT_TARGETS",
    "LoudnessMeasurement",
    "LoudnessTargets",
    "MasterResult",
    "MixOutcome",
    "QCResult",
    "SilenceGap",
    "assemble_voice_stem",
    "detect_silence_gaps",
    "loudnorm_two_pass",
    "measure_loudness",
    "mix_and_master",
    "mix_stems",
    "prepare_music",
    "qc_check",
]
