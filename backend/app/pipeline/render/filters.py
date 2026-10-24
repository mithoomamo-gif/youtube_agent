"""Programmatic ffmpeg filter-graph construction for one segment.

Graphs are built here as strings, then written to a ``.txt`` file and passed
to ffmpeg via ``-/filter_complex`` — never inlined into the command
line (Windows command-line length limits; a long video's graphs are large
and machine-generated labels keep failures mappable to scene ids).

Chain per segment (roadmap Part 6.7)::

    source -> fps=30 -> scale/crop "cover" -> motion -> colour hygiene
           -> PNG text overlays -> in-segment fades -> frame-exact trim
           -> scale (range-normalise to tv) -> format=yuv420p, setsar=1,
              settb -> [vout]

There are NO cross-segment transitions anywhere in this module (D3): fades
live entirely inside a single segment's frame range.
"""

from __future__ import annotations

from .contract import FPS, HEIGHT, WIDTH, SceneSpec, SourceType

#: Stills are downscaled to at most this size BEFORE zoompan — zoompan on a
#: full-res still re-samples a huge input per frame and produces jitter.
KEN_BURNS_MAX_W = 3840
KEN_BURNS_MAX_H = 2160


def _cover_chain() -> str:
    """Scale/crop 'cover' to the timeline size — never stretch/pillarbox."""
    return (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT}"
    )


def build_filter_graph(spec: SceneSpec) -> str:
    """Return the ``-/filter_complex`` contents for one segment.

    Input layout (the renderer adds inputs in this order):
      * input 0 — the scene source (video clip / still image / lavfi colour)
      * inputs 1..N — text-overlay PNGs, in ``spec.text_overlays`` order
    Output label: ``[vout]``.
    """
    frames = spec.duration_frames
    dur = frames / FPS
    parts: list[str] = []

    if spec.source_type is SourceType.STILL and spec.ken_burns:
        # Ken Burns (roadmap recipe): downscale the still to <=3840x2160
        # BEFORE zoompan, total zoom <=1.15x, light unsharp AFTER zoompan.
        zoom_step = (
            (spec.zoom_end - spec.zoom_start) / max(frames - 1, 1)
            if frames > 1 else 0.0
        )
        zoom_expr = (
            f"min({spec.zoom_start:.4f}+{zoom_step:.6f}*on,{spec.zoom_end:.4f})"
            if zoom_step > 0 else f"{spec.zoom_start:.4f}"
        )
        chain = (
            f"scale={KEN_BURNS_MAX_W}:{KEN_BURNS_MAX_H}"
            f":force_original_aspect_ratio=increase,"
            f"crop={KEN_BURNS_MAX_W}:{KEN_BURNS_MAX_H},"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"unsharp=5:5:0.8:5:5:0.0"
        )
    elif spec.source_type is SourceType.STILL:
        chain = f"fps={FPS},{_cover_chain()}"
    elif spec.source_type is SourceType.SOLID:
        chain = f"fps={FPS},scale={WIDTH}:{HEIGHT}"
    else:  # VIDEO
        chain = f"fps={FPS},{_cover_chain()}"

    # Subtle colour hygiene (profile defaults from the roadmap).
    chain += ",eq=contrast=1.03:saturation=1.05"

    # In-segment fades only — cross-segment dissolves are forbidden (D3).
    if spec.fade_in_seconds > 0:
        chain += f",fade=t=in:st=0:d={spec.fade_in_seconds:.3f}"
    if spec.fade_out_seconds > 0:
        fade_start = max(dur - spec.fade_out_seconds, 0.0)
        chain += f",fade=t=out:st={fade_start:.3f}:d={spec.fade_out_seconds:.3f}"

    # Frame-exact duration + locked output format.  trim/setpts pins the
    # segment to exactly `frames` frames regardless of source length.
    #
    # Range normalisation comes BEFORE format=: the format filter alone
    # converts the pixel format but NOT the colour range, so a full-range
    # source (a yuvj420p stock clip, or a JPEG still — JPEG is inherently
    # full-range) used to emerge still tagged yuvj420p and fail the
    # stream contract.  A scale pass with out_range=tv performs the real
    # swscale level remapping (full-range 0-255 -> limited 16-235);
    # in_range=auto takes the source's own signalled range, so
    # limited-range sources pass through with levels unchanged.
    # (setrange=tv would only relabel the frames without remapping the
    # levels — verified experimentally; do not "simplify" to that.)
    chain += (
        f",trim=end_frame={frames},setpts=N/({FPS}*TB),"
        f"scale=in_range=auto:out_range=tv,"
        f"format=yuv420p,setsar=1,settb=1/30000"
    )

    if not spec.text_overlays:
        parts.append(f"[0:v]{chain}[vout]")
    else:
        parts.append(f"[0:v]{chain}[base]")
        current = "base"
        for idx, _overlay in enumerate(spec.text_overlays, start=1):
            nxt = "vout" if idx == len(spec.text_overlays) else f"ov{idx}"
            ov = spec.text_overlays[idx - 1]
            parts.append(
                f"[{current}][{idx}:v]overlay=x={ov.x}:y={ov.y}[{nxt}]"
            )
            current = nxt
    return ";\n".join(parts) + "\n"
