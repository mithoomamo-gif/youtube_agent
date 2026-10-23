"""Music stem preparation.

Prepares one music track to sit under the narration for the whole
programme: looped (or trimmed) to the exact video duration, set to the
bed level (volume ≈ 0.22, profile-driven starting point per roadmap §18)
and faded in / out so the bed never starts or stops abruptly.  Ducking
against the voice happens later, at mix time — this stem is *not* ducked.
"""

from __future__ import annotations

from pathlib import Path

from ._ffmpeg import ensure_parent, probe_duration, require_file, run_ffmpeg

SAMPLE_RATE = 48_000

#: Locked defaults (roadmap §18 music stem).
DEFAULT_VOLUME = 0.22
DEFAULT_FADE_IN_S = 1.5
DEFAULT_FADE_OUT_S = 3.0


def prepare_music(
    music_in: Path | str,
    out: Path | str,
    *,
    duration_s: float | None = None,
    target: Path | str | None = None,
    volume: float = DEFAULT_VOLUME,
    fade_in_s: float = DEFAULT_FADE_IN_S,
    fade_out_s: float = DEFAULT_FADE_OUT_S,
) -> Path:
    """Loop/trim ``music_in`` to the programme duration as a music stem.

    The duration comes from ``duration_s`` when given, else from
    ``target`` (typically the assembled voice stem — the narration is the
    master clock, roadmap §10), else from the music file itself (trim
    only, no loop extension).

    Fades are clamped to at most 40 % of the programme each so short
    programmes (tests, Shorts) still get a sane fade instead of an FFmpeg
    error or a bed that is all fade.

    Returns:
        The output path (48 kHz, stereo, 24-bit PCM WAV).
    """
    src = require_file(Path(music_in), "music")
    out_path = Path(out)
    ensure_parent(out_path)

    if duration_s is None and target is not None:
        duration_s = probe_duration(require_file(Path(target), "duration target"))
    if duration_s is None:
        duration_s = probe_duration(src)
    if duration_s <= 0:
        raise ValueError(f"duration_s must be > 0, got {duration_s}")
    if volume < 0:
        raise ValueError(f"volume must be >= 0, got {volume}")

    fade_in = max(0.0, min(fade_in_s, duration_s * 0.4))
    fade_out = max(0.0, min(fade_out_s, duration_s * 0.4))
    fade_out_start = max(0.0, duration_s - fade_out)

    chain = (
        f"aresample={SAMPLE_RATE},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={volume:g}"
    )
    if fade_in > 0:
        chain += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        chain += f",afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"

    run_ffmpeg([
        "-stream_loop", "-1", "-i", str(src),
        "-t", f"{duration_s:.3f}",
        "-af", chain,
        "-c:a", "pcm_s24le", "-ar", str(SAMPLE_RATE), "-ac", "2",
        str(out_path),
    ])
    return out_path
