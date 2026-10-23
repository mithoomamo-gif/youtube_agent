"""Mix (voice + ducked music + optional SFX) and loudnorm mastering.

Pipeline (roadmap §18, order fixed):
    1. mix      — :func:`mix_stems`: sidechain-ducked music under the voice,
                  optional SFX stem, summed with ``amix``.
    2. loudnorm — :func:`loudnorm_two_pass` on the *mix* (never per stem,
                  never per segment): pass 1 measures, pass 2 applies the
                  measured values, ``linear=true`` whenever dynamics allow,
                  then an explicit ``aresample=48000`` (loudnorm internally
                  upsamples to 192 kHz; without the resample the WAV would
                  ship at 192 kHz).

:func:`mix_and_master` composes the two and re-measures its own output
with the independent QC measurement, so a ``MasterResult`` always carries
fresh final numbers, not pass-2's claims.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ._ffmpeg import (
    FFmpegError,
    ensure_parent,
    loudnorm_value,
    parse_loudnorm_json,
    require_file,
    run_ffmpeg,
    run_ffmpeg_capture,
)
from .models import (
    DEFAULT_TARGETS,
    LoudnessMeasurement,
    LoudnessTargets,
    MasterResult,
    MixOutcome,
)
from .qc import measure_loudness

SAMPLE_RATE = 48_000

#: Sidechain ducking defaults (roadmap §18): the music dips ≈ 8–12 dB
#: under narration and recovers in pauses (release 350 ms).
DUCK_THRESHOLD = 0.02
DUCK_RATIO = 8.0
DUCK_ATTACK_MS = 25.0
DUCK_RELEASE_MS = 350.0
#: Fallback envelope duck: constant ≈ −9.5 dB music attenuation for the
#: narration span (20·log10(0.335) ≈ −9.5 dB, mid-range of the 8–12 dB
#: duck window) when sidechain compression is unavailable / fails.
FALLBACK_DUCK_VOLUME = 0.335


def _stereo(label_in: str, label_out: str) -> str:
    return f"[{label_in}]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo[{label_out}]"


def mix_stems(
    voice: Path | str,
    music: Path | str,
    out: Path | str,
    sfx: Path | str | None = None,
    *,
    sfx_volume: float = 1.0,
    duck_threshold: float = DUCK_THRESHOLD,
    duck_ratio: float = DUCK_RATIO,
    duck_attack_ms: float = DUCK_ATTACK_MS,
    duck_release_ms: float = DUCK_RELEASE_MS,
) -> MixOutcome:
    """Mix voice + music (+ optional SFX) into one pre-master WAV.

    Ducking: the voice is split; one copy feeds the sidechain of a
    compressor on the music (``sidechaincompress`` threshold 0.02,
    ratio 8, attack 25 ms, release 350 ms, makeup 1 → ≈ 8–12 dB dip under
    narration).  If that graph fails for any reason, the fallback path
    applies a pre-computed volume envelope instead: the music is attenuated
    by a fixed ≈ 9.5 dB for the narration span (the envelope the sidechain
    would average to on dense narration) and mixed the same way.  Which
    path produced the mix is reported in ``MixOutcome.sidechain_used`` —
    the fallback is never silent.

    The mix length follows the voice (``duration=first``): the narration
    is the master clock.
    """
    voice_path = require_file(Path(voice), "voice stem")
    music_path = require_file(Path(music), "music stem")
    sfx_path = require_file(Path(sfx), "SFX stem") if sfx is not None else None
    out_path = Path(out)
    ensure_parent(out_path)

    inputs: list[str] = ["-i", str(voice_path), "-i", str(music_path)]
    if sfx_path is not None:
        inputs += ["-i", str(sfx_path)]

    # ------------------------------------------------------------------
    # amix normalize=0 is MANDATORY here (and in the fallback below).
    # amix's default (normalize=1) divides the sum by the number of
    # inputs, silently dropping the whole programme by ~6 dB (2 inputs)
    # or ~9.5 dB (3 inputs) *after* the stems were levelled on purpose.
    # The two-pass loudnorm stage would then over-correct a level error
    # that was never in the stems, and any stem-level QC comparison
    # (voice stem vs. mix) becomes meaningless.  Levels in this mix are
    # set deliberately per stem (music bed volume, ducking, SFX volume);
    # amix must sum them verbatim: normalize=0.
    # ------------------------------------------------------------------
    n_inputs = 3 if sfx_path is not None else 2

    def tail_graph(music_label: str) -> list[str]:
        parts = [f"[vmix][{music_label}]"]
        if sfx_path is not None:
            parts.append(f"[2:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,volume={sfx_volume:g}[sfx]")
            mix_in = "[vmix][" + music_label + "][sfx]"
        else:
            mix_in = f"[vmix][{music_label}]"
        graph = ";".join(parts[1:]) + (";" if len(parts) > 1 else "")
        graph += (
            f"{mix_in}amix=inputs={n_inputs}:duration=first"
            f":dropout_transition=0:normalize=0[mixed]"
        )
        return [graph]

    sidechain_graph = ";".join([
        _stereo("0:a", "vraw"),
        "[vraw]asplit=2[vmix][vsc]",
        _stereo("1:a", "mraw"),
        f"[mraw][vsc]sidechaincompress=threshold={duck_threshold:g}:ratio={duck_ratio:g}"
        f":attack={duck_attack_ms:g}:release={duck_release_ms:g}:makeup=1[ducked]",
        *tail_graph("ducked"),
    ])

    try:
        run_ffmpeg([
            *inputs, "-filter_complex", sidechain_graph,
            "-map", "[mixed]", "-c:a", "pcm_f32le", "-ar", str(SAMPLE_RATE), "-ac", "2",
            str(out_path),
        ])
        return MixOutcome(output_path=out_path, sidechain_used=True,
                          details={"mix_path": "sidechaincompress"})
    except FFmpegError as exc:
        sidechain_error = str(exc)

    # Fallback: volume-envelope ducking (fixed ≈9.5 dB envelope over the
    # narration span) — same amix contract, normalize=0 as documented above.
    fallback_graph = ";".join([
        _stereo("0:a", "vmix"),
        _stereo("1:a", "mraw"),
        f"[mraw]volume={FALLBACK_DUCK_VOLUME:g}:eval=frame[ducked]",
        *tail_graph("ducked"),
    ])
    run_ffmpeg([
        *inputs, "-filter_complex", fallback_graph,
        "-map", "[mixed]", "-c:a", "pcm_f32le", "-ar", str(SAMPLE_RATE), "-ac", "2",
        str(out_path),
    ])
    return MixOutcome(
        output_path=out_path,
        sidechain_used=False,
        details={"mix_path": "volume_envelope_fallback", "sidechain_error": sidechain_error},
    )


def loudnorm_two_pass(
    src: Path | str,
    out: Path | str,
    *,
    targets: LoudnessTargets = DEFAULT_TARGETS,
) -> tuple[LoudnessMeasurement, bool, dict[str, str]]:
    """Two-pass loudnorm of ``src`` onto ``out``; returns (pass-1
    measurement, linear_used, pass-2 raw JSON).

    Pass 1 measures with ``print_format=json``.  Pass 2 re-applies with
    the measured I/TP/LRA/thresh + target offset and ``linear=true``:
    loudnorm then uses a single linear gain when the measured dynamics
    allow it and falls back to dynamic normalisation internally only when
    true-peak headroom forces it (single-pass dynamic processing audibly
    pumps narration, which is why two-pass linear is specified).  The
    actual mode is read back from pass 2's JSON (``normalization_type``).
    If the linear attempt itself errors, it is retried once with
    ``linear=false``.
    """
    src_path = require_file(Path(src), "loudnorm source")
    out_path = Path(out)
    ensure_parent(out_path)
    t = targets

    # ---- Pass 1: measure ---------------------------------------------
    stderr = run_ffmpeg_capture([
        "-i", str(src_path), "-vn",
        "-af", f"loudnorm=I={t.integrated_lufs:g}:TP={t.true_peak_dbtp:g}:LRA={t.lra:g}:print_format=json",
        "-f", "null", "-",
    ])
    raw1 = parse_loudnorm_json(stderr)
    measured = LoudnessMeasurement(
        integrated_lufs=loudnorm_value(raw1, "input_i"),
        true_peak_dbtp=loudnorm_value(raw1, "input_tp"),
        lra=loudnorm_value(raw1, "input_lra"),
        threshold_lufs=loudnorm_value(raw1, "input_thresh"),
        target_offset=loudnorm_value(raw1, "target_offset"),
        normalization_type=raw1.get("normalization_type"),
        raw=raw1,
    )

    # ---- Pass 2: apply ------------------------------------------------
    def apply(linear: bool) -> dict[str, str]:
        af = (
            f"loudnorm=I={t.integrated_lufs:g}:TP={t.true_peak_dbtp:g}:LRA={t.lra:g}"
            f":measured_I={measured.integrated_lufs:.2f}"
            f":measured_TP={measured.true_peak_dbtp:.2f}"
            f":measured_LRA={measured.lra:.2f}"
            f":measured_thresh={measured.threshold_lufs:.2f}"
            f":offset={measured.target_offset:.2f}"
            f":linear={'true' if linear else 'false'}"
            f":print_format=json"
            # loudnorm upsamples internally (192 kHz); resample back to
            # the house 48 kHz or the WAV/AAC encode inherits 192 kHz.
            f",aresample={t.sample_rate}"
        )
        proc_stderr = run_ffmpeg_capture([
            "-i", str(src_path), "-vn", "-af", af,
            "-c:a", "pcm_s16le", "-ar", str(t.sample_rate), "-ac", "2",
            str(out_path),
        ])
        try:
            return parse_loudnorm_json(proc_stderr)
        except FFmpegError:
            return {}

    try:
        raw2 = apply(linear=True)
        linear_used = raw2.get("normalization_type", "linear") == "linear"
    except FFmpegError:
        raw2 = apply(linear=False)
        linear_used = False
    return measured, linear_used, raw2


def mix_and_master(
    voice: Path | str,
    music: Path | str,
    out: Path | str,
    sfx: Path | str | None = None,
    *,
    targets: LoudnessTargets = DEFAULT_TARGETS,
    sfx_volume: float = 1.0,
) -> MasterResult:
    """Full mix + master: ducked mix of the stems, then two-pass loudnorm.

    ``music`` should already be the prepared stem (see
    :func:`app.pipeline.audio.music.prepare_music`); the mix still follows
    the voice's duration.  The finished master is re-measured independently
    (:func:`app.pipeline.audio.qc.measure_loudness`) and those fresh values
    — not pass 2's — are what ``MasterResult.final_measurement`` reports.

    Returns a :class:`MasterResult`; the master itself is a 48 kHz stereo
    16-bit PCM WAV at ``out`` (roadmap §18 master_audio.wav contract).
    """
    out_path = Path(out)
    ensure_parent(out_path)

    with tempfile.TemporaryDirectory(prefix="premix_", dir=str(out_path.parent)) as tmp:
        premix = Path(tmp) / "premix.wav"
        mix = mix_stems(voice, music, premix, sfx=sfx, sfx_volume=sfx_volume)
        measured, linear_used, raw2 = loudnorm_two_pass(premix, out_path, targets=targets)

    final = measure_loudness(out_path, targets=targets)
    return MasterResult(
        output_path=out_path,
        premaster_measurement=measured,
        final_measurement=final,
        linear_used=linear_used,
        sidechain_used=mix.sidechain_used,
        details={"mix": mix.details, "loudnorm_pass2": raw2},
    )
