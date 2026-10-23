"""Narration (voice) stem assembly.

Concatenates per-scene narration WAVs (48 kHz mono, the house standard the
TTS stage delivers) into a single voice stem, inserting the configured
silence gap at every join (scene / chapter pauses are *assembly* joins
inserted here — never baked into TTS audio, roadmap §10).

Fixed processing order (roadmap §18, order is significant and locked):
    concat  →  highpass 70 Hz  →  compressor  →  presence EQ

The chain runs once over the assembled timeline rather than per scene so
the compressor sees programme-level dynamics and every scene is treated
identically; highpass/EQ are linear so their position relative to the
concat makes no audible difference, while the compressor's does.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

from ._ffmpeg import ensure_parent, require_file, run_ffmpeg

#: Join pause used when the caller does not specify gaps.  Chapter pauses
#: are passed explicitly by the caller (roadmap §10 pause profile:
#: sentence 250–350 ms, paragraph 500–700 ms, chapter ≈ 1200 ms; the
#: pipeline spec's working range for chapter gaps is 600–900 ms).
DEFAULT_GAP_MS = 400.0

#: Locked voice-chain defaults (roadmap §18).
HIGHPASS_HZ = 70
COMP_THRESHOLD_DB = -18.0
COMP_RATIO = 3.0
COMP_ATTACK_MS = 5.0
COMP_RELEASE_MS = 120.0
PRESENCE_FREQ_HZ = 3000
PRESENCE_WIDTH_OCT = 1.2
PRESENCE_GAIN_DB = 1.5

SAMPLE_RATE = 48_000


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def voice_chain_filter(
    *,
    highpass_hz: float = HIGHPASS_HZ,
    comp_threshold_db: float = COMP_THRESHOLD_DB,
    comp_ratio: float = COMP_RATIO,
    comp_attack_ms: float = COMP_ATTACK_MS,
    comp_release_ms: float = COMP_RELEASE_MS,
    presence_freq_hz: float = PRESENCE_FREQ_HZ,
    presence_width_oct: float = PRESENCE_WIDTH_OCT,
    presence_gain_db: float = PRESENCE_GAIN_DB,
) -> str:
    """The locked voice processing chain as an FFmpeg filter string.

    ``acompressor`` takes its threshold as a *linear* amplitude, so the
    −18 dB spec value is converted (0.126).  Presence EQ is a peaking
    ``equalizer`` at 3 kHz, 1.2 octaves wide, +1.5 dB (profile-driven in
    the roadmap; the defaults here are the locked starting point).
    """
    threshold = _db_to_linear(comp_threshold_db)
    return (
        f"highpass=f={highpass_hz:g},"
        f"acompressor=threshold={threshold:.4f}:ratio={comp_ratio:g}"
        f":attack={comp_attack_ms:g}:release={comp_release_ms:g},"
        f"equalizer=f={presence_freq_hz:g}:width_type=o"
        f":width={presence_width_oct:g}:gain={presence_gain_db:g}"
    )


def _resolve_gaps(gaps_ms: float | Sequence[float | None] | None, joins: int) -> list[float]:
    """Normalise the ``gaps_ms`` argument to one gap (ms) per join."""
    if joins == 0:
        return []
    if gaps_ms is None:
        return [DEFAULT_GAP_MS] * joins
    if isinstance(gaps_ms, (int, float)):
        return [float(gaps_ms)] * joins
    gaps = [DEFAULT_GAP_MS if g is None else float(g) for g in gaps_ms]
    if len(gaps) != joins:
        raise ValueError(
            f"gaps_ms has {len(gaps)} entries but {joins} joins exist "
            f"between the given scenes (expected len(scene_wavs) - 1)"
        )
    if any(g < 0 for g in gaps):
        raise ValueError("gap durations must be >= 0 ms")
    return gaps


def assemble_voice_stem(
    scene_wavs: Sequence[Path | str],
    gaps_ms: float | Sequence[float | None] | None = None,
    out: Path | str = "voice_stem.wav",
    *,
    chain: str | None = None,
) -> Path:
    """Assemble per-scene narration WAVs into one processed voice stem.

    Args:
        scene_wavs: Scene WAVs in narration order (48 kHz mono expected;
            anything else is resampled / downmixed to that standard).
        gaps_ms: Silence inserted at each join, in milliseconds — either a
            single value used for every join, a per-join sequence of length
            ``len(scene_wavs) - 1`` (``None`` entries use
            :data:`DEFAULT_GAP_MS`), or ``None`` for the default everywhere.
            Chapter pauses are typically 600–900 ms.
        out: Destination WAV path (48 kHz, mono, 24-bit PCM).
        chain: Override for the processing chain; defaults to the locked
            :func:`voice_chain_filter` chain.  Exists for tests/profiles —
            production callers should not pass it.

    Returns:
        The output path.
    """
    scenes = [require_file(Path(p), "scene WAV") for p in scene_wavs]
    if not scenes:
        raise ValueError("scene_wavs must contain at least one WAV")
    out_path = Path(out)
    ensure_parent(out_path)
    gaps = _resolve_gaps(gaps_ms, len(scenes) - 1)

    # Build the input list: scene, silence, scene, silence, ...  Silence is
    # a lavfi anullsrc input trimmed to the exact gap, so gap durations are
    # sample-accurate and need no temporary files.
    args: list[str] = []
    segments: list[tuple[str, float | None]] = []  # (kind, gap_ms)
    for idx, scene in enumerate(scenes):
        args += ["-i", str(scene)]
        segments.append(("scene", None))
        if idx < len(gaps) and gaps[idx] > 0:
            args += ["-f", "lavfi", "-t", f"{gaps[idx] / 1000.0:.3f}", "-i",
                     f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
            segments.append(("silence", gaps[idx]))

    # Normalise every segment to the concat filter's required common format
    # (48 kHz, mono, float-planar), concat, then run the locked chain once.
    parts: list[str] = []
    labels: list[str] = []
    for i in range(len(segments)):
        parts.append(
            f"[{i}:a]aresample={SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=mono[seg{i}]"
        )
        labels.append(f"[seg{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=0:a=1[cat]")
    parts.append(f"[cat]{chain if chain is not None else voice_chain_filter()}[voice]")
    graph = ";".join(parts)

    run_ffmpeg([
        *args,
        "-filter_complex", graph,
        "-map", "[voice]",
        "-c:a", "pcm_s24le", "-ar", str(SAMPLE_RATE), "-ac", "1",
        str(out_path),
    ])
    return out_path


def gaps_from_durations(durations_s: Sequence[float], *, default_ms: float = DEFAULT_GAP_MS) -> list[float]:
    """Convenience helper kept for callers that think in scene durations.

    Not part of the core contract — assembly gaps are explicit inputs; this
    simply returns a flat default gap per join for a duration list.
    """
    if len(durations_s) < 2 or any(not math.isfinite(d) or d < 0 for d in durations_s):
        return []
    return [default_ms] * (len(durations_s) - 1)
