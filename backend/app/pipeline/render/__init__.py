"""Render engine — scene segments, stream-copy concat, tier outputs.

Public surface (roadmap Part 6.7 / research section 18):

  * :class:`SceneSpec`, :class:`SourceType`, :func:`segment_filename`,
    :func:`compute_segment_hash` — the locked segment contract + cache key.
  * :class:`SegmentRenderer` — per-scene rendering with atomic writes,
    cache reuse, progress callbacks and a ``segments.jsonl`` manifest.
  * :func:`concat_segments` — stream-copy join (NO dissolves, D3).
  * :func:`mux_final`, :func:`render_draft`, :func:`render_master` —
    final mux and the 720p Draft / 1440p Master output tiers.
  * :func:`render_final` + :func:`resolve_quality_tier` /
    :func:`quality_tier_spec` — the Round-5 quality-tier entry point
    (``draft`` / ``master_1080p`` / ``master_1440p_upscale``, default
    ``draft``) returning a :class:`FinalRenderResult` (tier, encode
    wall time, output file size).
  * :func:`probe_encoder` / :class:`EncoderChoice` — NVENC → QSV → AMF →
    libx264 selection via real test encodes.
  * :func:`verify_concat`, :func:`make_contact_sheet`,
    :func:`write_probe_report` — post-concat gates + QC by-products.
  * :func:`render_text_png` — Pillow text-overlay PNGs (never drawtext
    for Urdu/Arabic).
"""

from .contract import (
    FPS,
    HEIGHT,
    MASTER_HEIGHT,
    MASTER_WIDTH,
    TEMPLATE_VERSION,
    WIDTH,
    SceneSpec,
    SourceType,
    TextOverlay,
    check_narration_frame_gate,
    compute_segment_hash,
    frames_for_seconds,
    segment_filename,
)
from .encoder import (
    EncoderChoice,
    clamp_concurrency,
    encoder_args,
    probe_encoder,
    reset_probe_cache,
)
from .filters import build_filter_graph
from .qc import (
    VerificationError,
    VerificationReport,
    check_stream_contract,
    ffprobe,
    make_contact_sheet,
    probe_duration,
    verify_concat,
    write_probe_report,
)
from .renderer import (
    DEFAULT_QUALITY_TIER,
    QUALITY_TIERS,
    FinalRenderResult,
    QualityTierSpec,
    RenderConfig,
    RenderError,
    SegmentRenderer,
    SegmentResult,
    concat_segments,
    final_encode_command,
    mux_final,
    quality_tier_spec,
    render_draft,
    render_final,
    render_master,
    resolve_quality_tier,
)
from .text_overlay import find_default_font, render_text_png

__all__ = [
    "FPS", "WIDTH", "HEIGHT", "MASTER_WIDTH", "MASTER_HEIGHT",
    "TEMPLATE_VERSION",
    "SceneSpec", "SourceType", "TextOverlay",
    "check_narration_frame_gate", "compute_segment_hash",
    "frames_for_seconds", "segment_filename",
    "EncoderChoice", "clamp_concurrency", "encoder_args",
    "probe_encoder", "reset_probe_cache",
    "build_filter_graph",
    "VerificationError", "VerificationReport", "check_stream_contract",
    "ffprobe", "make_contact_sheet", "probe_duration", "verify_concat",
    "write_probe_report",
    "RenderConfig", "RenderError", "SegmentRenderer", "SegmentResult",
    "concat_segments", "mux_final", "render_draft", "render_master",
    "QUALITY_TIERS", "DEFAULT_QUALITY_TIER", "QualityTierSpec",
    "FinalRenderResult", "resolve_quality_tier", "quality_tier_spec",
    "final_encode_command", "render_final",
    "find_default_font", "render_text_png",
]
