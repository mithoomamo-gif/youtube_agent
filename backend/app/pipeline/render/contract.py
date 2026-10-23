"""Locked segment contract for the render engine (roadmap Part 6.7 / section 18).

Every segment in the app is byte-compatible *by construction*: one locked
parameter set, defined here and nowhere else.  Segments are later joined with
the concat demuxer using stream copy, which only produces clean joins when
codec / resolution / fps / pixel format match exactly across all segments.

Contract summary (decisions D1-D4 of the roadmap):
  * container ......... MPEG-TS (``.ts``) — tolerant of concat, no moov fragility
  * video codec ....... H.264, High profile
  * timeline size ..... 1920x1080, SAR 1:1
  * frame rate ........ constant 30 fps
  * pixel format ...... yuv420p
  * GOP ............... locked: ``-g 60`` (+ encoder-specific scene-cut lock),
                        so every segment starts with an IDR frame
  * audio ............. NONE in segments (D2 — one globally mastered audio
                        track is muxed once, at the very end)
  * transitions ....... NO cross-segment dissolves (D3 — hard rule).  Only
                        hard cuts plus optional fades *inside* a segment.

Segment filename = cache key::

    seg_<scene_id>_v<hash>.ts
    hash = sha256(canonical(scene spec + asset hash + template version))

An unchanged scene therefore always maps to the same filename and its
segment is reused, never re-rendered.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Locked constants — changing any of these is a template-version bump.
# ---------------------------------------------------------------------------

TEMPLATE_VERSION = "render-v1"

WIDTH = 1920
HEIGHT = 1080
FPS = 30
PIX_FMT = "yuv420p"
CODEC = "h264"
PROFILE = "High"
GOP_FRAMES = 60
TIMEBASE = "1/30000"  # filter-graph timebase intent (settb); MPEG-TS stores
# timestamps on its native 90 kHz clock, so ffprobe reports 1/90000 for .ts
# files — verification must not demand 1/30000 from the TS container itself.

DRAFT_WIDTH, DRAFT_HEIGHT = 1280, 720
MASTER_WIDTH, MASTER_HEIGHT = 2560, 1440

#: Tolerance for the post-concat duration gate (seconds).
CONCAT_DURATION_TOLERANCE_S = 0.5
#: Tolerance for the narration-vs-segments frame gate (frames).
NARRATION_FRAME_TOLERANCE = 1


class SourceType(str, Enum):
    """What a scene's picture comes from."""

    VIDEO = "video"  # stock video clip (trim / scale / crop to 1080p)
    STILL = "still"  # still image with Ken Burns motion
    SOLID = "solid"  # solid / programmatic background colour


@dataclass
class TextOverlay:
    """A pre-rendered transparent PNG composited over the scene.

    On-screen text is ALWAYS a Pillow-rendered PNG (see
    :mod:`.text_overlay`), never the ffmpeg ``drawtext`` filter — drawtext
    cannot shape Urdu/Arabic reliably.  ``png_path`` points at the rendered
    PNG; ``x``/``y`` are ffmpeg overlay expressions (ints or strings such as
    ``"(W-w)/2"``).
    """

    png_path: str
    x: str | int = "(W-w)/2"
    y: str | int = "(H-h)-80"


@dataclass
class SceneSpec:
    """Everything that determines one segment's pixels.

    The spec (plus the source asset's content hash and the template version)
    is hashed to form the segment cache key, so *every* render-affecting
    field belongs in this dataclass — if it changes the picture, it must
    change the hash.
    """

    scene_id: str
    source_type: SourceType
    duration_seconds: float
    #: Path to the source asset (video clip / still image).  Empty for SOLID.
    source_path: str = ""
    #: Content hash of the source asset (supplied by the asset layer).
    asset_hash: str = ""
    #: Licence-registry id of the source asset (manifest bookkeeping).
    licence_id: str = ""
    #: Trim start (seconds) inside a VIDEO source.
    start_offset_seconds: float = 0.0
    #: Hex colour for SOLID scenes, e.g. ``"0x101418"``.
    color: str = "0x000000"
    #: Ken Burns (STILL only): total zoom stays <= 1.15x per the spec.
    ken_burns: bool = True
    zoom_start: float = 1.0
    zoom_end: float = 1.12
    #: Optional fades *inside* this segment (never across segments — D3).
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0
    #: Pre-rendered PNG text overlays.
    text_overlays: list[TextOverlay] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if not (1.0 <= self.zoom_start <= 1.15 and 1.0 <= self.zoom_end <= 1.15):
            raise ValueError("Ken Burns zoom must stay within 1.0x..1.15x")
        if self.source_type is not SourceType.SOLID and not self.source_path:
            raise ValueError(f"source_path is required for {self.source_type}")

    @property
    def duration_frames(self) -> int:
        """Frame-exact duration: ``round(seconds x 30)`` (roadmap rule)."""
        return int(round(self.duration_seconds * FPS))

    def to_hash_dict(self) -> dict[str, Any]:
        """Canonical, render-affecting view of the spec used for hashing."""
        d = asdict(self)
        d["source_type"] = self.source_type.value
        # Only the asset's identity matters for the picture, not where a
        # cache happens to have stored it — hash the file name, not the
        # absolute path, so segments survive cache relocation.
        d["source_path"] = Path(self.source_path).name if self.source_path else ""
        d["duration_frames"] = self.duration_frames
        d.pop("duration_seconds", None)
        return d


def compute_segment_hash(
    spec: SceneSpec,
    template_version: str = TEMPLATE_VERSION,
) -> str:
    """Cache key: sha256 of canonical(scene spec + asset hash + template)."""
    payload = {
        "spec": spec.to_hash_dict(),
        "asset_hash": spec.asset_hash,
        "template_version": template_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def segment_filename(
    spec: SceneSpec,
    template_version: str = TEMPLATE_VERSION,
) -> str:
    """``seg_<scene_id>_v<hash>.ts`` — the hash *is* the cache key."""
    digest = compute_segment_hash(spec, template_version)
    return f"seg_{spec.scene_id}_v{digest}.ts"


def frames_for_seconds(seconds: float) -> int:
    return int(round(seconds * FPS))


def check_narration_frame_gate(
    segment_frames: list[int],
    narration_seconds: float,
) -> None:
    """Gate: sum of segment frames vs narration frames must match ±1 frame.

    Sub-second drift per join would otherwise accumulate into visible
    subtitle desync over a long video, so this is a hard failure.
    """
    total = sum(segment_frames)
    expected = frames_for_seconds(narration_seconds)
    if abs(total - expected) > NARRATION_FRAME_TOLERANCE:
        raise ValueError(
            f"narration frame gate FAILED: segments total {total} frames, "
            f"narration is {expected} frames "
            f"(diff {total - expected}, tolerance ±{NARRATION_FRAME_TOLERANCE})"
        )


def is_frame_aligned(seconds: float) -> bool:
    """True when ``seconds`` lands (within float noise) on a frame boundary."""
    return math.isclose(seconds * FPS, round(seconds * FPS), abs_tol=1e-6)
