"""Stock brightness selection filter (Round 5, SL-006).

A *selection-time* filter for stock candidates: it measures a
candidate clip's mean luma (ffmpeg ``signalstats`` ``YAVG``) and
rejects near-black candidates BEFORE they can reach the render /
QC stages.

Why this exists
---------------
The Style Lab v1 proof (``outputs/stylelab/proof/``) was BLOCKED at
Gate B by technical QC: one Pixabay stock clip of the Moon's far
side (provider asset 25613, a 10.7 s scene) is near-black, so QC's
``BLACK_SEGMENT_TOO_LONG`` fired.  QC was right — the clip should
never have been *selected*.  This module is that missing selection
filter.  It changes NO QC threshold and NO gate rule: technical QC
in ``app.pipeline.qc.technical`` (black >= 2.0 s -> BLOCK, etc.)
runs exactly as before and remains the backstop for anything the
filter cannot measure or that darkens only in part.

Floor calibration (measured 2026-10-23 on this machine, ffmpeg 8.1)
-------------------------------------------------------------------
``signalstats`` YAVG is the per-frame mean luma (limited-range
16..235 for typical yuv420p stock):

* pure black (lavfi ``color=black``)                      YAVG = 16.0
* the actual v1 blocking clip (Pixabay 25613)             YAVG ~= 18.2
  (10 keyframe samples, range 17.8..18.5)
* a normal, QC-passing Moon clip from the same v1 run     YAVG ~= 56
  (Pixabay 138739, 9 keyframe samples, range 54.1..59.0)
* lavfi ``color=0x404040`` mid-dark grey                   YAVG = 71

:data:`BRIGHTNESS_FLOOR_YAVG` = 30.0 sits between the near-black
cluster (16..18) and every observed QC-passing clip (>= ~54): a
candidate whose sampled mean luma is BELOW the floor is rejected
as too dark; anything at or above it passes this filter (and still
faces all the usual stock filters + QC downstream).

Measurement method
------------------
:func:`measure_brightness_yavg` runs ONE ffmpeg pass per file with
``-skip_frame nokey`` (only keyframes are decoded — cheap even for
UHD tiers), scales frames to 320 px wide, and averages the
``lavfi.signalstats.YAVG`` metadata values across the sampled
keyframes.  When a file yields no keyframe samples (unusual
encodings), it falls back to a short plain decode (``fps=1`` for
the first :data:`_FALLBACK_SAMPLE_S` seconds).  Stills (photos /
plates) are measured from their single frame.

A file that cannot be measured at all (missing, corrupt, not a
media file, ffmpeg failure) returns ``None`` — *unmeasurable is not
"dark"*: the filter abstains and the candidate proceeds to the
existing fallback chain / QC exactly as it did before this filter
existed.  Only a positively measured below-floor mean rejects.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from app.ffmpeg_resolution import ffmpeg_executable

__all__ = [
    "BRIGHTNESS_FLOOR_YAVG",
    "BrightnessVerdict",
    "StockBrightnessFilter",
    "is_too_dark",
    "measure_brightness_yavg",
]

logger = logging.getLogger(__name__)

#: The selection floor: a stock candidate whose sampled mean luma
#: (``signalstats`` YAVG, 16..235 limited range) is BELOW this value
#: is rejected as near-black before selection.  See the module
#: docstring for the measured calibration behind 30.0.
BRIGHTNESS_FLOOR_YAVG = 30.0

#: Suffixes measured as still images (single frame, no seeking).
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

#: Fallback sampling window (seconds) when keyframe sampling yields
#: nothing.
_FALLBACK_SAMPLE_S = 6.0

_FFMPEG_TIMEOUT_S = 120

_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")


def _parse_yavg(stderr_text: str) -> list[float]:
    return [float(m) for m in _YAVG_RE.findall(stderr_text or "")]


def _run_ffmpeg(args: list[str]) -> Optional[str]:
    """Run ffmpeg, returning its stderr text (None on any failure)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S,
        )
    except Exception as exc:  # missing binary, timeout, OSError...
        logger.warning(
            "brightness: ffmpeg run failed (%s): %s",
            type(exc).__name__, exc,
        )
        return None
    if proc.returncode != 0:
        return None
    return proc.stderr or ""


def measure_brightness_yavg(
    path: str | Path,
    *,
    ffmpeg: str = ffmpeg_executable(),
) -> Optional[float]:
    """Mean sampled luma (``signalstats`` YAVG) of a media file.

    Returns the mean YAVG across sampled frames, or ``None`` when
    the file cannot be measured (missing / unreadable / no video
    stream / ffmpeg failure).  ``None`` means "abstain", never
    "dark" — see the module docstring.
    """
    media = Path(path)
    try:
        if not media.is_file() or media.stat().st_size == 0:
            return None
    except OSError:
        return None

    vf = (
        "scale=320:-2,signalstats,"
        "metadata=print:key=lavfi.signalstats.YAVG"
    )
    if media.suffix.lower() in _IMAGE_SUFFIXES:
        stderr = _run_ffmpeg([
            ffmpeg, "-v", "info", "-i", str(media),
            "-frames:v", "1", "-vf", vf, "-f", "null", "-",
        ])
        values = _parse_yavg(stderr) if stderr is not None else []
        return round(sum(values) / len(values), 3) if values else None

    # Primary: keyframe-only sampling (one pass, cheap for UHD).
    stderr = _run_ffmpeg([
        ffmpeg, "-v", "info", "-skip_frame", "nokey",
        "-i", str(media), "-vf", vf, "-fps_mode", "vfr",
        "-f", "null", "-",
    ])
    values = _parse_yavg(stderr) if stderr is not None else []
    if not values:
        # Fallback: plain decode of a short head window at 1 fps.
        stderr = _run_ffmpeg([
            ffmpeg, "-v", "info", "-i", str(media),
            "-t", f"{_FALLBACK_SAMPLE_S:g}",
            "-vf", f"fps=1,{vf}", "-f", "null", "-",
        ])
        values = _parse_yavg(stderr) if stderr is not None else []
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def is_too_dark(
    yavg: Optional[float],
    *,
    floor: float = BRIGHTNESS_FLOOR_YAVG,
) -> bool:
    """True only for a *measured* mean luma strictly below ``floor``."""
    return yavg is not None and yavg < floor


@dataclass
class BrightnessVerdict:
    """The filter's decision for one candidate file."""

    path: str
    yavg: Optional[float]
    floor: float
    too_dark: bool
    measured: bool  # False = abstained (unmeasurable), treated as pass


@dataclass
class StockBrightnessFilter:
    """Stateful selection filter: measures candidates, tracks stats.

    One instance per selection run (the stock fetcher owns one).
    ``stats`` is the run-level record the production report renders:
    how many candidates were checked and how many were rejected as
    too dark (with per-rejection asset/path/YAVG detail).
    """

    floor: float = BRIGHTNESS_FLOOR_YAVG
    checked: int = 0
    rejected: int = 0
    unmeasurable: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)
    _cache: dict[str, Optional[float]] = field(default_factory=dict)

    def check(
        self,
        path: str | Path,
        *,
        asset_id: str = "",
    ) -> BrightnessVerdict:
        """Measure ``path`` (cached per path) and return the verdict."""
        key = str(path)
        if key not in self._cache:
            self._cache[key] = measure_brightness_yavg(path)
        yavg = self._cache[key]
        measured = yavg is not None
        too_dark = is_too_dark(yavg, floor=self.floor)
        self.checked += 1
        if not measured:
            self.unmeasurable += 1
        if too_dark:
            self.rejected += 1
            self.rejections.append({
                "asset_id": str(asset_id or ""),
                "path": key,
                "yavg": yavg,
                "floor": self.floor,
            })
            logger.info(
                "brightness filter: rejecting near-black stock "
                "candidate asset=%s yavg=%s floor=%s path=%s",
                asset_id or "?", yavg, self.floor, key,
            )
        return BrightnessVerdict(
            path=key, yavg=yavg, floor=self.floor,
            too_dark=too_dark, measured=measured,
        )

    def stats(self) -> dict[str, Any]:
        """Run-level filter record (JSON-safe) for reports/manifests."""
        return {
            "floor_yavg": self.floor,
            "checked": self.checked,
            "rejected_too_dark": self.rejected,
            "unmeasurable": self.unmeasurable,
            "rejections": list(self.rejections),
        }
