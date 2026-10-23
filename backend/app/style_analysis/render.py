"""Style Lab — human-readable markdown breakdown renderer.

Renders a :class:`~app.style_analysis.schema.StyleLabAnalysis` as markdown
with the fixed section set: Overview, Scenes & Pacing, Visual Mix,
Speech & Structure, Audio & Editing Dressing, Thumbnail.

Honesty rules for this renderer (do not weaken them):
* Probabilistic labels are ALWAYS rendered with their confidence,
  e.g. ``likely stock-like (0.62)`` — never as bare facts. A label
  whose confidence is 0.0, or whose label is ``uncertain``, renders
  as "uncertain / not assessed".
* ``None`` measurements render as "not measured", never as 0.
* Thumbnail text is described by role/structure only — this schema
  holds no actual thumbnail wording, so none can leak here.
"""

from __future__ import annotations

from .schema import (
    PresenceAssessment,
    SceneVisualLabel,
    StyleLabAnalysis,
)

__all__ = ["render_breakdown", "render_markdown"]


def _fmt_num(value: float | int | None, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "not measured"
    return f"{float(value):.{digits}f}{suffix}"


def _fmt_conf(confidence: float) -> str:
    return f"{float(confidence):.2f}"


def _likely_label(label: str, confidence: float) -> str:
    """Render a probabilistic label honestly.

    >>> _likely_label("stock_like", 0.62)  ->  'likely stock-like (0.62)'
    >>> _likely_label("uncertain", 0.0)    ->  'uncertain / not assessed'
    """
    pretty = label.replace("_", "-")
    if label == "uncertain" or confidence <= 0.0:
        return "uncertain / not assessed"
    if confidence >= 0.8:
        return f"very likely {pretty} ({_fmt_conf(confidence)})"
    if confidence >= 0.5:
        return f"likely {pretty} ({_fmt_conf(confidence)})"
    return f"possibly {pretty} ({_fmt_conf(confidence)} — low confidence)"


def _presence_text(a: PresenceAssessment) -> str:
    if a.label == "uncertain" or a.confidence <= 0.0:
        base = "uncertain / not assessed"
    elif a.confidence >= 0.8:
        base = f"{a.label} (confidence {_fmt_conf(a.confidence)})"
    elif a.confidence >= 0.5:
        base = f"likely {a.label} (confidence {_fmt_conf(a.confidence)})"
    else:
        base = f"possibly {a.label} (confidence {_fmt_conf(a.confidence)} — low confidence)"
    return f"{base} — {a.notes}" if a.notes else base


def _scene_label_text(lbl: SceneVisualLabel) -> str:
    signals = f" — signals: {', '.join(lbl.signals)}" if lbl.signals else ""
    return f"scene {lbl.scene_index}: {_likely_label(lbl.label, lbl.confidence)}{signals}"


def _bool_text(value: bool | None, confidence: float | None = None) -> str:
    if value is None:
        return "uncertain / not assessed"
    base = "yes" if value else "no"
    if confidence is not None:
        return f"{base} (confidence {_fmt_conf(confidence)})"
    return base


def render_breakdown(analysis: StyleLabAnalysis) -> str:
    """Render the full markdown breakdown for one analysis result."""
    a = analysis
    v = a.video
    lines: list[str] = []

    # -- Overview ---------------------------------------------------------
    lines += ["# Style Lab — Video Breakdown", ""]
    if a.competitor_video_id:
        lines.append(f"- Competitor video ID: `{a.competitor_video_id}`")
    if a.channel_id:
        lines.append(f"- Attached to channel: `{a.channel_id}`")
    if a.source_url:
        lines.append(f"- Source: {a.source_url}")
    lines += [
        f"- Analysis ID: `{a.analysis_id}`",
        f"- Analyser: {a.analyzer_version} (schema {a.schema_version})",
        f"- Overall confidence: {_fmt_conf(a.overall_confidence)}"
        + (" — treat all findings as tentative" if a.overall_confidence < 0.5 else ""),
        "",
        "## Overview",
        "",
        f"- File: `{v.path}`",
        f"- Duration: {_fmt_num(v.duration_s, 1, 's')}",
        f"- Resolution: {v.resolution or 'not measured'}"
        + (f" ({v.width}x{v.height})" if v.width and v.height and v.resolution != f"{v.width}x{v.height}" else ""),
        f"- FPS: {_fmt_num(v.fps, 2)}",
    ]
    if v.codec:
        lines.append(f"- Codec: {v.codec}")
    for note in a.notes:
        lines.append(f"- Note: {note}")
    lines.append("")

    # -- Scenes & Pacing ----------------------------------------------------
    s = a.scenes
    lines += [
        "## Scenes & Pacing",
        "",
        f"- Scene count: {s.scene_count}",
        f"- Average shot length: {_fmt_num(s.avg_shot_length_s, 2, 's')}",
        f"- Cuts per minute: {_fmt_num(s.cuts_per_minute, 1)}",
        f"- Segmentation confidence: {_fmt_conf(s.confidence)}",
    ]
    for note in s.notes:
        lines.append(f"- Note: {note}")
    if s.scenes:
        lines += ["", "| # | Start (s) | End (s) | Duration (s) |", "|---|-----------|---------|--------------|"]
        for seg in s.scenes:
            lines.append(
                f"| {seg.index} | {seg.start_s:.1f} | {seg.end_s:.1f} | {seg.duration_s:.1f} |"
            )
    lines.append("")

    # -- Visual Mix ---------------------------------------------------------
    vm = a.visual_mix
    lines += [
        "## Visual Mix",
        "",
        "> Visual provenance is estimated from observable signals only. "
        "Labels below are likelihoods, not facts about how the video was made.",
        "",
        f"- Overall visual-mix confidence: {_fmt_conf(vm.confidence)}",
    ]
    pct = vm.percentages()
    for name in ("stock_like", "ai_generated_like", "raw_footage_like",
                 "screen_capture", "text_card", "other", "uncertain"):
        if pct[name] > 0:
            lines.append(f"- {name.replace('_', '-')}: {pct[name]:.1f}%")
    for note in vm.notes:
        lines.append(f"- Note: {note}")
    if vm.scene_labels:
        lines += ["", "Per-scene estimates:", ""]
        for lbl in vm.scene_labels:
            lines.append(f"- {_scene_label_text(lbl)}")
    lines.append("")

    # -- Speech & Structure ---------------------------------------------------
    sp = a.speech
    lines += [
        "## Speech & Structure",
        "",
        f"- Transcript language(s): {', '.join(sp.transcript_languages) if sp.transcript_languages else 'uncertain / not assessed'}"
        + (f" (confidence {_fmt_conf(sp.language_confidence)})" if sp.transcript_languages else ""),
        f"- Speaking pace: {_fmt_num(sp.wpm, 0, ' WPM')}"
        + (f" (confidence {_fmt_conf(sp.wpm_confidence)})" if sp.wpm is not None else ""),
        f"- Speech-analysis confidence: {_fmt_conf(sp.confidence)}",
        f"- Hook window: {_fmt_num(sp.hook.window_s, 1, 's')}",
        f"- Hook pattern: {_likely_label(sp.hook.pattern_label, sp.hook.confidence)}"
        if sp.hook.pattern_label != "uncertain"
        else "- Hook pattern: uncertain / not assessed",
    ]
    if sp.hook.pattern_notes:
        lines.append(f"- Hook notes (structure only): {sp.hook.pattern_notes}")
    if sp.transcript_ref:
        lines.append(f"- Transcript reference: `{sp.transcript_ref}` (full text stored separately)")
    for note in sp.notes:
        lines.append(f"- Note: {note}")
    if sp.beats:
        lines += ["", "Structure beats (summaries, not transcript excerpts):", ""]
        for beat in sp.beats:
            lines.append(
                f"- {beat.start_s:.1f}s–{beat.end_s:.1f}s: {beat.summary or 'no summary'}"
                f" (confidence {_fmt_conf(beat.confidence)})"
            )
    lines += [
        "",
        f"- Call to action: {_bool_text(sp.cta.present, sp.cta.confidence)}",
    ]
    for placement in sp.cta.placements:
        when = (
            f" at {_fmt_num(placement.start_s, 1, 's')}"
            if placement.start_s is not None else ""
        )
        lines.append(
            f"  - {placement.kind}{when} (confidence {_fmt_conf(placement.confidence)})"
            + (f" — {placement.notes}" if placement.notes else "")
        )
    lines.append("")

    # -- Audio & Editing Dressing --------------------------------------------
    d = a.dressing
    lines += [
        "## Audio & Editing Dressing",
        "",
        f"- Background music: {_presence_text(d.music_presence)}",
        f"- Burned-in captions: {_presence_text(d.caption_burn_in)}",
        f"- Text-overlay frequency: {_fmt_num(d.text_overlay_frequency, 1, ' per minute')}"
        + (f" (confidence {_fmt_conf(d.text_overlay_confidence)})" if d.text_overlay_frequency is not None else ""),
        f"- Camera/editing motion: {_likely_label(d.motion.label, d.motion.confidence)}"
        + (f" — {d.motion.notes}" if d.motion.notes else ""),
        f"- Dressing confidence: {_fmt_conf(d.confidence)}",
    ]
    for note in d.notes:
        lines.append(f"- Note: {note}")
    lines.append("")

    # -- Thumbnail ------------------------------------------------------------
    lines += ["## Thumbnail", ""]
    t = a.thumbnail
    if t is None:
        lines.append("No thumbnail was supplied or analysed for this video.")
    else:
        lines += [
            f"- Text word count: {_fmt_num(t.text_word_count, 0)}",
            f"- Face present: {_bool_text(t.face_present, t.face_confidence)}",
            f"- Layout: {_likely_label(t.layout_class, t.confidence) if t.layout_class != 'uncertain' else 'uncertain / not assessed'}",
            f"- Contrast: {t.contrast_class} (confidence {_fmt_conf(t.confidence)})"
            if t.contrast_class != "uncertain"
            else "- Contrast: uncertain / not assessed",
            f"- Dominant colours: {', '.join(t.dominant_colours) if t.dominant_colours else 'not measured'}",
            f"- Thumbnail confidence: {_fmt_conf(t.confidence)}",
        ]
        if t.text_role_notes:
            # Role/structure ONLY by schema contract — never the words.
            lines.append(f"- Text role (structure only): {t.text_role_notes}")
        for note in t.notes:
            lines.append(f"- Note: {note}")
    lines.append("")
    return "\n".join(lines)


#: Alias for callers that prefer the generic verb.
render_markdown = render_breakdown
