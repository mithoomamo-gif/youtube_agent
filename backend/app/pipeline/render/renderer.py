"""Segment renderer, stream-copy concatenation and final mux (Part 6.7).

Hard rules honoured here:
  * D1 — scenes render as independent segments; one bad scene costs one
    segment, and an unchanged scene is never re-rendered (cache by hash).
  * D2 — segments are video-only; the mastered audio is attached once, by
    :func:`mux_final` / the tier builders, never inside a segment.
  * D3 — NO cross-segment dissolves.  Concatenation is pure stream copy.
  * Filter graphs go through a ``-/filter_complex`` FILE, never an
    inlined command-line graph.
  * Segments are written to ``<name>.tmp.ts`` and atomically renamed; a
    file under its final name is by definition complete (resume = skip +
    ffprobe-verify).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .contract import (
    DRAFT_HEIGHT,
    DRAFT_WIDTH,
    FPS,
    HEIGHT,
    MASTER_HEIGHT,
    MASTER_WIDTH,
    TEMPLATE_VERSION,
    WIDTH,
    SceneSpec,
    SourceType,
    segment_filename,
)
from .encoder import EncoderChoice, clamp_concurrency, probe_encoder
from .filters import build_filter_graph
from .qc import check_stream_contract
from ...ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

#: Progress callback: (fraction 0..1, ffmpeg progress key/values).
ProgressCallback = Callable[[float, dict[str, str]], None]


class RenderError(RuntimeError):
    """Raised when an ffmpeg render invocation fails."""


@dataclass
class RenderConfig:
    """Render settings.  ``concurrency`` is a config value (1 default, hard
    max 2); segment dispatch itself is the scheduler's concern."""

    concurrency: int = 1
    template_version: str = TEMPLATE_VERSION
    master_upscale: bool = True  # per-channel toggle: 1440p master on/off

    def __post_init__(self) -> None:
        self.concurrency = clamp_concurrency(self.concurrency)


@dataclass
class SegmentResult:
    path: Path
    cache_hit: bool
    duration_frames: int
    encoder: str


def _run_with_progress(
    cmd: list[str],
    *,
    expected_frames: int,
    progress: ProgressCallback | None,
    stderr_path: Path,
) -> None:
    """Run ffmpeg with ``-progress pipe:1``, streaming progress callbacks."""
    full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    with open(stderr_path, "wb") as stderr_file:
        proc = subprocess.Popen(
            full_cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
        assert proc.stdout is not None
        block: dict[str, str] = {}
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            block[key.strip()] = value.strip()
            if key.strip() == "progress":
                if progress is not None:
                    try:
                        frames_done = int(block.get("frame", "0"))
                    except ValueError:
                        frames_done = 0
                    fraction = (
                        min(frames_done / expected_frames, 1.0)
                        if expected_frames > 0 else 0.0
                    )
                    progress(fraction, dict(block))
                block = {}
        return_code = proc.wait()
    if return_code != 0:
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RenderError(f"ffmpeg failed (exit {return_code}):\n{tail}")
    if progress is not None:
        progress(1.0, {"progress": "end"})


class SegmentRenderer:
    """Renders :class:`SceneSpec`s to locked-contract ``.ts`` segments."""

    def __init__(
        self,
        output_dir: Path,
        *,
        config: RenderConfig | None = None,
        encoder_choice: EncoderChoice | None = None,
        ffmpeg: str = ffmpeg_executable(),
        ffprobe_bin: str = ffprobe_executable(),
        progress: ProgressCallback | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or RenderConfig()
        self._encoder_choice = encoder_choice
        self.ffmpeg = ffmpeg
        self.ffprobe_bin = ffprobe_bin
        self.progress = progress

    @property
    def encoder_choice(self) -> EncoderChoice:
        """Lazily probe (real test encodes) and cache the winning encoder."""
        if self._encoder_choice is None:
            self._encoder_choice = probe_encoder(self.ffmpeg)
        return self._encoder_choice

    # -- segment rendering -------------------------------------------------

    def segment_path(self, spec: SceneSpec) -> Path:
        return self.output_dir / segment_filename(
            spec, self.config.template_version)

    def render_segment(self, spec: SceneSpec) -> Path:
        return self.render_segment_detailed(spec).path

    def render_segment_detailed(self, spec: SceneSpec) -> SegmentResult:
        """Render one scene, or reuse the cached segment when it verifies."""
        final_path = self.segment_path(spec)
        encoder_name = self.encoder_choice.encoder

        if final_path.exists():
            try:
                check_stream_contract(final_path, ffprobe_bin=self.ffprobe_bin)
                return SegmentResult(final_path, True, spec.duration_frames,
                                     encoder_name)
            except Exception:
                # A final-name file that fails verification is not trusted
                # as complete — re-render it (atomically replaced below).
                final_path.unlink(missing_ok=True)

        graph = build_filter_graph(spec)
        graph_dir = self.output_dir / "graphs"
        graph_dir.mkdir(parents=True, exist_ok=True)
        graph_path = graph_dir / (final_path.stem + ".filter.txt")
        graph_path.write_text(graph, encoding="utf-8")

        tmp_path = self.output_dir / final_path.name.replace(".ts", ".tmp.ts")
        cmd = self._segment_command(spec, graph_path, tmp_path)
        stderr_path = graph_dir / (final_path.stem + ".stderr.log")
        _run_with_progress(
            cmd,
            expected_frames=spec.duration_frames,
            progress=self.progress,
            stderr_path=stderr_path,
        )
        # Verify BEFORE the atomic rename — only verified segments may
        # appear under their final (cache-key) name.
        check_stream_contract(tmp_path, ffprobe_bin=self.ffprobe_bin)
        os.replace(tmp_path, final_path)  # atomic on the same filesystem
        self._append_manifest(spec, final_path, encoder_name)
        return SegmentResult(final_path, False, spec.duration_frames,
                             encoder_name)

    def render_all(self, specs: list[SceneSpec]) -> list[Path]:
        """Render (or reuse) every scene's segment, in timeline order."""
        return [self.render_segment(spec) for spec in specs]

    def _segment_command(
        self, spec: SceneSpec, graph_path: Path, out_path: Path,
    ) -> list[str]:
        cmd: list[str] = [self.ffmpeg, "-y", "-v", "error"]
        if spec.source_type is SourceType.VIDEO:
            if spec.start_offset_seconds > 0:
                cmd += ["-ss", f"{spec.start_offset_seconds:.3f}"]
            cmd += ["-stream_loop", "-1", "-i", spec.source_path]
        elif spec.source_type is SourceType.STILL:
            cmd += ["-loop", "1", "-i", spec.source_path]
        else:  # SOLID — programmatic lavfi colour source
            cmd += ["-f", "lavfi", "-i",
                    f"color=c={spec.color}:s=1920x1080:r={FPS}"]
        for overlay in spec.text_overlays:
            cmd += ["-i", overlay.png_path]
        cmd += [
            "-/filter_complex", str(graph_path),
            "-map", "[vout]",
            "-an",  # D2: segments never carry audio
            *self.encoder_choice.args("segment"),
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            str(out_path),
        ]
        return cmd

    def _append_manifest(self, spec: SceneSpec, path: Path, encoder: str) -> None:
        entry = {
            "scene_id": spec.scene_id,
            "file": path.name,
            "duration_frames": spec.duration_frames,
            "encoder": encoder,
            "template_version": self.config.template_version,
            "source_type": spec.source_type.value,
            "source_asset": Path(spec.source_path).name if spec.source_path else "",
            "asset_hash": spec.asset_hash,
            "licence_id": spec.licence_id,
            "is_still_with_motion": bool(
                spec.source_type is SourceType.STILL and spec.ken_burns),
        }
        manifest = self.output_dir / "segments.jsonl"
        with open(manifest, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Concatenation + final mux / tiers
# ---------------------------------------------------------------------------

def concat_segments(
    segment_paths: list[Path],
    output: Path,
    *,
    ffmpeg: str = ffmpeg_executable(),
) -> Path:
    """Join segments with the concat demuxer — STREAM COPY ONLY.

    No re-encode, no xfade, no dissolves (D3): the segments are already
    byte-compatible by construction, so ``-c copy`` yields frame-exact
    joins.  Any transition belongs inside a segment (fades), never here.
    """
    if not segment_paths:
        raise ValueError("no segments to concatenate")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8",
        dir=output.parent,
    ) as list_file:
        for seg in segment_paths:
            escaped = str(Path(seg).resolve()).replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")
        list_path = Path(list_file.name)
    try:
        cmd = [
            ffmpeg, "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", str(output),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RenderError(f"concat failed: {proc.stderr.strip()[:1000]}")
    finally:
        list_path.unlink(missing_ok=True)
    return output


def mux_final(
    video: Path,
    audio: Path,
    output: Path,
    *,
    audio_bitrate: str = "320k",
    ffmpeg: str = ffmpeg_executable(),
) -> Path:
    """Mux a video-only file with the mastered audio into the final MP4.

    Video is stream-copied (never re-encoded after segment rendering);
    audio is encoded once to AAC-LC 48 kHz.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RenderError(f"mux failed: {proc.stderr.strip()[:1000]}")
    return output


# ---------------------------------------------------------------------------
# Output quality tiers (Round 5 — master encode)
# ---------------------------------------------------------------------------
#
# Public tier names — the per-channel setting (sibling plumbing) uses
# EXACTLY these strings:
#
#   ``draft``                — the historical behaviour, unchanged:
#                              720p downscale, fast preset, 128k AAC.
#   ``master_1080p``         — the 1080p timeline encoded at full
#                              1920x1080, master encoder settings,
#                              320k AAC.
#   ``master_1440p_upscale`` — the 1080p timeline upscaled to
#                              2560x1440, which earns YouTube's better
#                              (VP9/AV1) delivery encode.
#
# Segments are ALWAYS rendered once at the locked 1080p timeline
# (contract WIDTH x HEIGHT) regardless of tier — a tier changes only
# this final scale + encode.  Loudnorm mastering happens upstream on
# the voice mix (audio stage), subtitles stay a sidecar, and thumbnails
# are generated from the same assets, so all three are identical
# across tiers by construction.

#: The public quality-tier names, in increasing quality order.
QualityTier = Literal["draft", "master_1080p", "master_1440p_upscale"]

QUALITY_TIERS: tuple[str, ...] = (
    "draft", "master_1080p", "master_1440p_upscale")

#: Tier used everywhere unless a caller explicitly asks for another.
DEFAULT_QUALITY_TIER: str = "draft"


@dataclass(frozen=True)
class QualityTierSpec:
    """Resolved encode parameters for one public quality tier."""

    #: Public tier name (one of :data:`QUALITY_TIERS`).
    name: str
    #: Final output width in pixels.
    width: int
    #: Final output height in pixels.
    height: int
    #: Internal encoder tier (``encoder.Tier``) for quality/preset args.
    encoder_tier: str
    #: AAC bitrate for the mastered audio in the final encode.
    audio_bitrate: str


_QUALITY_TIER_SPECS: dict[str, QualityTierSpec] = {
    "draft": QualityTierSpec(
        name="draft", width=DRAFT_WIDTH, height=DRAFT_HEIGHT,
        encoder_tier="draft", audio_bitrate="128k"),
    "master_1080p": QualityTierSpec(
        name="master_1080p", width=WIDTH, height=HEIGHT,
        encoder_tier="master", audio_bitrate="320k"),
    "master_1440p_upscale": QualityTierSpec(
        name="master_1440p_upscale", width=MASTER_WIDTH,
        height=MASTER_HEIGHT, encoder_tier="master",
        audio_bitrate="320k"),
}


def resolve_quality_tier(quality_tier: str | None = None) -> str:
    """Normalise a requested quality tier to its canonical name.

    ``None`` / empty means "not asked for" and resolves to
    :data:`DEFAULT_QUALITY_TIER` (``"draft"``), so every existing
    caller keeps the historical behaviour.  Unknown names raise
    :class:`ValueError` — a misspelled tier must fail loud (R4-003),
    never silently render a draft.
    """
    if quality_tier is None:
        return DEFAULT_QUALITY_TIER
    cleaned = str(quality_tier).strip().lower()
    if not cleaned:
        return DEFAULT_QUALITY_TIER
    if cleaned not in _QUALITY_TIER_SPECS:
        raise ValueError(
            f"unknown quality_tier {quality_tier!r}; "
            f"expected one of {', '.join(QUALITY_TIERS)}"
        )
    return cleaned


def quality_tier_spec(quality_tier: str | None = None) -> QualityTierSpec:
    """The resolved encode parameters for ``quality_tier``."""
    return _QUALITY_TIER_SPECS[resolve_quality_tier(quality_tier)]


@dataclass
class FinalRenderResult:
    """Outcome of a tiered final render (:func:`render_final`).

    This is the render-level report record: which quality tier was
    used, how long the final encode took (wall time, seconds) and how
    big the final output file is (bytes) — the assemble stage mirrors
    the same three fields onto its ``AssembleResult``.
    """

    path: Path
    quality_tier: str
    width: int
    height: int
    render_wall_time_s: float
    output_file_size_bytes: int
    encoder_tier: str = ""
    audio_bitrate: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "final_mp4": str(self.path),
            "quality_tier": self.quality_tier,
            "width": self.width,
            "height": self.height,
            "render_wall_time_s": self.render_wall_time_s,
            "output_file_size_bytes": self.output_file_size_bytes,
            "encoder_tier": self.encoder_tier,
            "audio_bitrate": self.audio_bitrate,
        }


def _encode_command(
    source_video: Path,
    output: Path,
    *,
    width: int,
    height: int,
    encoder_tier: str,
    encoder_choice: EncoderChoice,
    audio: Path | None,
    audio_bitrate: str,
    ffmpeg: str,
) -> list[str]:
    """Build the final scale + encode command (no execution)."""
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(source_video)]
    if audio is not None:
        cmd += ["-i", str(audio)]
    # in_range/out_range: normalise to limited (tv) range while scaling —
    # inputs are concatenated segments (already tv-range by the segment
    # contract, so this is an identity for them), but the explicit range
    # pin keeps this re-encode from ever emitting full-range frames if
    # it is handed a full-range file directly.  A bare -pix_fmt cannot
    # do this: it converts format, not range.
    cmd += ["-vf", f"scale={width}:{height}:flags=lanczos"
                   f":in_range=auto:out_range=tv",
            "-map", "0:v:0"]
    if audio is not None:
        cmd += ["-map", "1:a:0"]
    cmd += [*encoder_choice.args(encoder_tier),  # type: ignore[arg-type]
            "-pix_fmt", "yuv420p", "-r", str(FPS)]
    if audio is not None:
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000",
                "-shortest"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(output)]
    return cmd


def final_encode_command(
    source_video: Path,
    output: Path,
    *,
    quality_tier: str | None = None,
    encoder_choice: EncoderChoice,
    audio: Path | None = None,
    ffmpeg: str = ffmpeg_executable(),
) -> list[str]:
    """The exact ffmpeg command :func:`render_final` would run for a
    tier — exposed so callers/tests can inspect scale + encoder
    arguments without encoding anything."""
    spec = quality_tier_spec(quality_tier)
    return _encode_command(
        source_video, output, width=spec.width, height=spec.height,
        encoder_tier=spec.encoder_tier, encoder_choice=encoder_choice,
        audio=audio, audio_bitrate=spec.audio_bitrate, ffmpeg=ffmpeg)


def _scale_and_encode(
    source_video: Path,
    output: Path,
    *,
    width: int,
    height: int,
    tier: str,
    encoder_choice: EncoderChoice,
    audio: Path | None,
    audio_bitrate: str,
    ffmpeg: str,
) -> Path:
    """Scale the concatenated 1080p timeline to a tier size and encode.

    This is the one sanctioned post-segment encode: the Master tier's
    1080p→1440p upscale (which earns YouTube's VP9/AV1 delivery encode)
    and the Draft tier's 720p downscale.  Segment rendering itself always
    happens once, at the locked 1080p timeline resolution.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = _encode_command(
        source_video, output, width=width, height=height,
        encoder_tier=tier, encoder_choice=encoder_choice, audio=audio,
        audio_bitrate=audio_bitrate, ffmpeg=ffmpeg)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RenderError(f"{tier} encode failed: {proc.stderr.strip()[:1000]}")
    return output


def render_final(
    segment_paths: list[Path],
    output: Path,
    *,
    quality_tier: str | None = "draft",
    audio: Path | None = None,
    encoder_choice: EncoderChoice | None = None,
    ffmpeg: str = ffmpeg_executable(),
) -> FinalRenderResult:
    """Render the final MP4 at ``quality_tier`` (default ``"draft"``).

    The tiered entry point: concatenates the (always 1080p) segments
    with stream copy, then runs the one sanctioned scale + encode for
    the resolved tier and muxes the mastered audio in the same pass.
    Returns a :class:`FinalRenderResult` recording the tier used, the
    encode wall time and the final output file size.
    """
    spec = quality_tier_spec(quality_tier)
    choice = encoder_choice or probe_encoder(ffmpeg)
    output = Path(output)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"{spec.name}-") as tmp:
        joined = concat_segments(segment_paths, Path(tmp) / "joined.ts",
                                 ffmpeg=ffmpeg)
        _scale_and_encode(
            joined, output, width=spec.width, height=spec.height,
            tier=spec.encoder_tier, encoder_choice=choice, audio=audio,
            audio_bitrate=spec.audio_bitrate, ffmpeg=ffmpeg)
    wall_time = time.monotonic() - started
    return FinalRenderResult(
        path=output,
        quality_tier=spec.name,
        width=spec.width,
        height=spec.height,
        render_wall_time_s=round(wall_time, 3),
        output_file_size_bytes=output.stat().st_size,
        encoder_tier=spec.encoder_tier,
        audio_bitrate=spec.audio_bitrate,
    )


def render_draft(
    segment_paths: list[Path],
    output: Path,
    *,
    audio: Path | None = None,
    encoder_choice: EncoderChoice | None = None,
    ffmpeg: str = ffmpeg_executable(),
) -> Path:
    """DRAFT tier: 720p, fast preset.  Video-only unless ``audio`` is given
    (when given, the audio is the FINAL mastered audio at 128k AAC —
    audio is never drafted)."""
    return render_final(
        segment_paths, output, quality_tier="draft", audio=audio,
        encoder_choice=encoder_choice, ffmpeg=ffmpeg).path


def render_master(
    segment_paths: list[Path],
    output: Path,
    *,
    audio: Path | None = None,
    upscale: bool = True,
    encoder_choice: EncoderChoice | None = None,
    ffmpeg: str = ffmpeg_executable(),
) -> Path:
    """MASTER tier: 2560x1440 upscale of the 1080p timeline (VP9/AV1
    delivery on YouTube), mastered audio at 320k AAC when provided.
    With ``upscale=False`` the master stays 1080p (per-channel toggle)."""
    return render_final(
        segment_paths, output,
        quality_tier="master_1440p_upscale" if upscale else "master_1080p",
        audio=audio, encoder_choice=encoder_choice, ffmpeg=ffmpeg).path
