"""Retention curve -> script-section mapping (Round 5, Analytics team).

Turns one video's audience-retention curve into per-section
annotations such as::

    Steepest drop at 1:40 — 18s into 'Dabao aur Woh Log Jo Teh Tak
    Gaye' (near the opening); the section lost 25% of its starting
    audience.

Inputs (the contracts this module codes against)
-------------------------------------------------
* **Retention curve** — the sibling Analytics Ingestion worker's
  storage contract: ``analytics_snapshots.retention_curve_json``
  (``app.schema_ddl``) holds a JSON list of points, each
  ``{"elapsed_ratio": 0..1, "watch_ratio": >= 0}`` (YouTube Analytics'
  ``elapsedVideoTimeRatio`` / ``audienceWatchRatio`` pair; those API
  key names and ``[elapsed, watch]`` pairs are also accepted).
  ``watch_ratio`` may exceed 1.0 (re-watches) — it is never clamped.
* **Video duration** in seconds, known from the project / publish
  manifest (``duration_seconds``); the caller passes it explicitly.
* **Section map** — the project's script chapter boundaries in
  seconds (:class:`SectionMap`).  The produce pipeline does NOT store
  chapter timings in the content bundle (``bundle.json`` chapters are
  ``{index, title, text}`` — words only), so timings are derived from
  production artefacts by :mod:`app.retention_sections.section_maps`;
  see that module for the source precedence and precision of each
  derivation.  This module only consumes the finished map.

Definitions (chosen once, tested, and relied on by annotations)
--------------------------------------------------------------
* Curve points are converted ratio -> seconds (``t = elapsed_ratio *
  duration``), sorted, and duplicate timestamps averaged.
* ``watch_at(t)`` is piecewise-linear interpolation between samples,
  flat-extrapolated outside the sampled range.
* Per-section ``entry_ratio`` / ``exit_ratio`` are ``watch_at`` at the
  section's start / end; ``mean_ratio`` is the time-weighted
  (trapezoidal) mean of the interpolated curve over the section.
* ``drop_off`` = ``entry - exit``; ``relative_drop`` = ``drop_off /
  entry`` (None when entry is 0).
* **Steepest drop** = the consecutive-sample interval with the
  greatest drop *rate* (``drop / dt``, watch-ratio per second);
  ties break by larger absolute drop, then earliest interval.  On a
  uniformly sampled curve this is also the largest absolute drop.
  The reported timestamp is the interval's START — the last moment
  the audience was still at the higher level.
* **Boundary tie rule**: sections are half-open ``[start_s, end_s)``,
  except the final section, which also includes its ``end_s``.  A
  drop whose interval starts exactly on a boundary therefore belongs
  to the section that STARTS there (the drop unfolds inside it).

Honest states
-------------
* Curve present + section map present -> ``status == "ok"``.
* Curve present, NO section map -> ``status == "unmapped"``: overall
  curve stats and drop points are still computed, with NO section
  labels — labels are never fabricated.
* Curve missing / empty -> ``status == "no_data"``: no stats at all.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "SCHEMA_VERSION",
    "DropPoint",
    "OverallStats",
    "RetentionPoint",
    "RetentionSectionResult",
    "SectionMap",
    "SectionSpan",
    "SectionStats",
    "format_timestamp",
    "map_retention_to_sections",
    "map_snapshot_retention",
    "parse_retention_curve",
    "section_containing",
    "watch_at",
]

#: Bump on any breaking field change; stored in every persisted result.
SCHEMA_VERSION: str = "1.0"

#: How many runners-up accompany the steepest drop in overall stats.
TOP_DROPS_KEPT = 5

#: A steepest drop this close (seconds) to a section start is
#: annotated as happening at the section's opening.
OPENING_WINDOW_S = 15.0


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class RetentionPoint(BaseModel):
    """One retention-curve sample (ratios, as stored by ingestion).

    ``elapsed_ratio`` is nominally 0..1, but the YouTube provider can
    emit values marginally above 1.0; those are clamped to the video
    end when converted to seconds (see :func:`_samples`), not
    rejected.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    elapsed_ratio: float = Field(ge=0.0)
    watch_ratio: float = Field(ge=0.0)


class SectionSpan(BaseModel):
    """One script section / chapter with its timeline boundaries."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: Ordinal position in the map (0-based).
    index: int = Field(ge=0)
    title: str = Field(min_length=1)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0)

    @field_validator("end_s")
    @classmethod
    def _end_after_start(cls, value: float, info: Any) -> float:
        start = info.data.get("start_s")
        if start is not None and value <= start:
            raise ValueError(
                f"section end_s ({value}) must be greater than start_s ({start})"
            )
        return value

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class SectionMap(BaseModel):
    """Chapter boundaries in seconds + where the timings came from.

    ``source`` is one of the derivation ids documented in
    :mod:`app.retention_sections.section_maps` (``"scene_manifest"``,
    ``"voiceover_chapters"``, ``"srt_wordcount"``,
    ``"word_proportional"``) or ``"explicit"`` for a caller-supplied
    map.  ``precision`` is the human-readable precision note for that
    source — it travels with the result so a reader can judge how
    much to trust a boundary.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sections: list[SectionSpan] = Field(default_factory=list)
    source: str = "explicit"
    precision: str = "caller-supplied boundaries in seconds"
    duration_s: float | None = Field(default=None, gt=0.0)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class DropPoint(BaseModel):
    """A single observed drop interval of the retention curve."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: Interval start (seconds) — the reported drop timestamp.
    at_s: float
    interval_end_s: float
    at_text: str  # "3:12"
    from_ratio: float
    to_ratio: float
    #: Absolute audience lost over the interval (from - to, > 0).
    drop: float
    #: Drop rate, watch-ratio per second.
    slope_per_s: float
    #: Section containing ``at_s`` under the half-open tie rule;
    #: None when unmapped or the point falls outside every section.
    section_index: int | None = None
    section_title: str | None = None
    seconds_into_section: float | None = None


class SectionStats(BaseModel):
    """Retention statistics for one script section."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    index: int
    title: str
    start_s: float
    end_s: float
    duration_s: float
    entry_ratio: float
    exit_ratio: float
    mean_ratio: float
    #: entry - exit (may be negative if the section gained audience).
    drop_off: float
    #: drop_off / entry_ratio; None when entry_ratio == 0.
    relative_drop: float | None = None
    steepest_drop: DropPoint | None = None
    annotation: str = ""


class OverallStats(BaseModel):
    """Whole-video curve statistics (computed with or without a map)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entry_ratio: float
    exit_ratio: float
    mean_ratio: float
    total_drop: float
    steepest_drop: DropPoint | None = None
    top_drops: list[DropPoint] = Field(default_factory=list)


class RetentionSectionResult(BaseModel):
    """The full analysis output for one snapshot / video."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = SCHEMA_VERSION
    analysis_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    snapshot_id: str | None = None
    video_id: str | None = None
    project_id: str | None = None
    status: Literal["ok", "unmapped", "no_data"]
    duration_s: float = Field(gt=0.0)
    #: Section-map provenance (None when unmapped / no_data).
    timing_source: str | None = None
    timing_precision: str | None = None
    point_count: int = 0
    sections: list[SectionStats] = Field(default_factory=list)
    overall: OverallStats | None = None
    steepest_drop: DropPoint | None = None
    annotations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Curve parsing (the ingestion storage contract)
# ---------------------------------------------------------------------------

_ELAPSED_KEYS = ("elapsed_ratio", "elapsedVideoTimeRatio", "elapsed", "x")
_WATCH_KEYS = ("watch_ratio", "audienceWatchRatio", "watch", "y")


def _point_from(raw: Any) -> RetentionPoint:
    if isinstance(raw, RetentionPoint):
        return raw
    if isinstance(raw, dict):
        elapsed = next((raw[k] for k in _ELAPSED_KEYS if k in raw), None)
        watch = next((raw[k] for k in _WATCH_KEYS if k in raw), None)
        if elapsed is None or watch is None:
            raise ValueError(
                f"retention point {raw!r} lacks an elapsed/watch ratio pair; "
                f"expected keys {_ELAPSED_KEYS} / {_WATCH_KEYS}"
            )
        return RetentionPoint(elapsed_ratio=float(elapsed), watch_ratio=float(watch))
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return RetentionPoint(elapsed_ratio=float(raw[0]), watch_ratio=float(raw[1]))
    raise ValueError(f"unrecognised retention point shape: {raw!r}")


def parse_retention_curve(raw: Any) -> list[RetentionPoint]:
    """Parse a stored retention curve into validated points.

    Accepts the ingestion contract shapes: a JSON string or an
    already-parsed list of ``{"elapsed_ratio", "watch_ratio"}`` dicts
    (YouTube API key names and ``[elapsed, watch]`` pairs tolerated),
    or a ``{"points": [...]}`` wrapper.  ``None`` / empty -> ``[]``
    (the caller turns that into the ``no_data`` state).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        raw = json.loads(text)
    if isinstance(raw, dict):
        raw = raw.get("points") or raw.get("curve") or []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"retention curve must be a list of points, got {type(raw)}")
    return [_point_from(item) for item in raw]


# ---------------------------------------------------------------------------
# Curve maths (samples are (t_seconds, watch_ratio), sorted, deduped)
# ---------------------------------------------------------------------------


def _samples(points: Sequence[RetentionPoint], duration_s: float) -> list[tuple[float, float]]:
    by_t: dict[float, list[float]] = {}
    for point in points:
        t = min(max(point.elapsed_ratio * duration_s, 0.0), duration_s)
        by_t.setdefault(round(t, 6), []).append(point.watch_ratio)
    return sorted((t, sum(ws) / len(ws)) for t, ws in by_t.items())


def watch_at(samples: Sequence[tuple[float, float]], t: float) -> float:
    """Piecewise-linear watch ratio at ``t`` (flat outside the range)."""
    if not samples:
        raise ValueError("watch_at requires at least one sample")
    if t <= samples[0][0]:
        return samples[0][1]
    if t >= samples[-1][0]:
        return samples[-1][1]
    for (t0, w0), (t1, w1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return w1
            frac = (t - t0) / (t1 - t0)
            return w0 + frac * (w1 - w0)
    return samples[-1][1]  # pragma: no cover - unreachable after the guards


def _mean_watch(samples: Sequence[tuple[float, float]], a: float, b: float) -> float:
    """Time-weighted (trapezoidal) mean of the curve over ``[a, b]``."""
    if b <= a:
        return watch_at(samples, a)
    cuts = [a] + [t for t, _ in samples if a < t < b] + [b]
    area = 0.0
    for x0, x1 in zip(cuts, cuts[1:]):
        area += (watch_at(samples, x0) + watch_at(samples, x1)) / 2.0 * (x1 - x0)
    return area / (b - a)


def format_timestamp(seconds: float) -> str:
    """Seconds -> ``m:ss`` (``h:mm:ss`` at an hour or more)."""
    total = int(round(max(0.0, seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def section_containing(
    sections: Sequence[SectionSpan], t: float
) -> SectionSpan | None:
    """The section containing ``t`` under the boundary tie rule.

    Half-open ``[start_s, end_s)`` for every section except the last,
    which also includes its own ``end_s`` — so a timestamp exactly on
    a boundary belongs to the section STARTING there.
    """
    ordered = sorted(sections, key=lambda s: s.start_s)
    for i, section in enumerate(ordered):
        is_last = i == len(ordered) - 1
        if section.start_s <= t < section.end_s or (is_last and t == section.end_s):
            return section
    return None


class _DropCandidate:
    __slots__ = ("at_s", "end_s", "from_w", "to_w")

    def __init__(self, at_s: float, end_s: float, from_w: float, to_w: float):
        self.at_s, self.end_s, self.from_w, self.to_w = at_s, end_s, from_w, to_w

    @property
    def drop(self) -> float:
        return self.from_w - self.to_w

    @property
    def slope(self) -> float:
        dt = self.end_s - self.at_s
        return self.drop / dt if dt > 0 else 0.0


def _drop_candidates(samples: Sequence[tuple[float, float]]) -> list[_DropCandidate]:
    return [
        _DropCandidate(t0, t1, w0, w1)
        for (t0, w0), (t1, w1) in zip(samples, samples[1:])
        if w0 - w1 > 1e-12 and t1 > t0
    ]


def _steepest(candidates: Sequence[_DropCandidate]) -> _DropCandidate | None:
    if not candidates:
        return None
    # Greatest drop rate; ties -> larger absolute drop, then earliest.
    return max(candidates, key=lambda c: (c.slope, c.drop, -c.at_s))


def _to_drop_point(
    cand: _DropCandidate, sections: Sequence[SectionSpan]
) -> DropPoint:
    section = section_containing(sections, cand.at_s) if sections else None
    return DropPoint(
        at_s=round(cand.at_s, 3),
        interval_end_s=round(cand.end_s, 3),
        at_text=format_timestamp(cand.at_s),
        from_ratio=round(cand.from_w, 6),
        to_ratio=round(cand.to_w, 6),
        drop=round(cand.drop, 6),
        slope_per_s=round(cand.slope, 6),
        section_index=section.index if section else None,
        section_title=section.title if section else None,
        seconds_into_section=(
            round(cand.at_s - section.start_s, 3) if section else None
        ),
    )


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def _section_annotation(stats: SectionStats) -> str:
    span = f"{format_timestamp(stats.start_s)}–{format_timestamp(stats.end_s)}"
    if stats.relative_drop is not None and stats.entry_ratio > 0:
        change = (
            f"lost {stats.relative_drop * 100:.0f}% of its starting audience "
            f"({stats.entry_ratio:.2f} → {stats.exit_ratio:.2f})"
            if stats.drop_off >= 0
            else f"gained audience ({stats.entry_ratio:.2f} → {stats.exit_ratio:.2f})"
        )
    else:
        change = f"moved {stats.entry_ratio:.2f} → {stats.exit_ratio:.2f}"
    return (
        f"'{stats.title}' ({span}): mean retention {stats.mean_ratio:.2f}; {change}."
    )


def _steepest_annotation(
    drop: DropPoint, sections_by_index: dict[int, SectionStats]
) -> str:
    if drop.section_index is None or drop.section_title is None:
        return (
            f"Steepest drop at {drop.at_text} — watch ratio fell "
            f"{drop.from_ratio:.2f} → {drop.to_ratio:.2f} "
            f"(-{drop.drop:.2f}) over "
            f"{drop.interval_end_s - drop.at_s:.0f}s (no section label)."
        )
    into = drop.seconds_into_section or 0.0
    opening = " (near the opening)" if into <= OPENING_WINDOW_S else ""
    stats = sections_by_index.get(drop.section_index)
    tail = ""
    if stats is not None and stats.relative_drop is not None and stats.drop_off > 0:
        tail = (
            f"; the section lost {stats.relative_drop * 100:.0f}% of its "
            f"starting audience"
        )
    return (
        f"Steepest drop at {drop.at_text} — {into:.0f}s into "
        f"'{drop.section_title}'{opening}{tail}."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _validated_sections(
    section_map: SectionMap | None, duration_s: float, warnings: list[str]
) -> list[SectionSpan]:
    if section_map is None or not section_map.sections:
        return []
    ordered = sorted(section_map.sections, key=lambda s: s.start_s)
    for prev, curr in zip(ordered, ordered[1:]):
        if curr.start_s < prev.end_s - 1e-9:
            raise ValueError(
                f"section map overlaps: '{prev.title}' ends at {prev.end_s}s "
                f"but '{curr.title}' starts at {curr.start_s}s"
            )
    clipped: list[SectionSpan] = []
    for section in ordered:
        end = min(section.end_s, duration_s)
        if end <= section.start_s:
            warnings.append(
                f"section '{section.title}' lies outside the video "
                f"duration and was skipped"
            )
            continue
        if end < section.end_s:
            warnings.append(
                f"section '{section.title}' end clipped from "
                f"{section.end_s:.1f}s to the video duration {duration_s:.1f}s"
            )
            section = section.model_copy(update={"end_s": end})
        clipped.append(section)
    return clipped


def map_retention_to_sections(
    curve: Any,
    duration_s: float,
    section_map: SectionMap | None = None,
    *,
    snapshot_id: str | None = None,
    video_id: str | None = None,
    project_id: str | None = None,
) -> RetentionSectionResult:
    """Map a retention curve onto a project's script sections.

    ``curve`` is anything :func:`parse_retention_curve` accepts (the
    stored JSON string, a parsed list, or ``None``).  See the module
    docstring for the status semantics and the drop/tie definitions.
    """
    if duration_s is None or not math.isfinite(float(duration_s)) or float(duration_s) <= 0:
        raise ValueError(f"duration_s must be a positive number, got {duration_s!r}")
    duration_s = float(duration_s)

    base: dict[str, Any] = dict(
        snapshot_id=snapshot_id,
        video_id=video_id,
        project_id=project_id,
        duration_s=duration_s,
    )

    points = parse_retention_curve(curve)
    if not points:
        return RetentionSectionResult(
            **base,
            status="no_data",
            annotations=[
                "No retention curve available for this snapshot — "
                "nothing to map."
            ],
        )

    warnings: list[str] = []
    sections = _validated_sections(section_map, duration_s, warnings)
    samples = _samples(points, duration_s)
    candidates = _drop_candidates(samples)
    steepest_cand = _steepest(candidates)

    # -- per-section stats -------------------------------------------------
    section_stats: list[SectionStats] = []
    for section in sections:
        entry = watch_at(samples, section.start_s)
        exit_ = watch_at(samples, section.end_s)
        drop_off = entry - exit_
        in_section = [
            c for c in candidates if section_containing([section], c.at_s) is not None
        ]
        sec_steepest = _steepest(in_section)
        stats = SectionStats(
            index=section.index,
            title=section.title,
            start_s=section.start_s,
            end_s=section.end_s,
            duration_s=round(section.duration_s, 3),
            entry_ratio=round(entry, 6),
            exit_ratio=round(exit_, 6),
            mean_ratio=round(_mean_watch(samples, section.start_s, section.end_s), 6),
            drop_off=round(drop_off, 6),
            relative_drop=(
                round(drop_off / entry, 6) if entry > 0 else None
            ),
            steepest_drop=(
                _to_drop_point(sec_steepest, sections) if sec_steepest else None
            ),
        )
        stats.annotation = _section_annotation(stats)
        section_stats.append(stats)

    # -- overall ------------------------------------------------------------
    overall_entry = watch_at(samples, 0.0)
    overall_exit = watch_at(samples, duration_s)
    steepest_drop = (
        _to_drop_point(steepest_cand, sections) if steepest_cand else None
    )
    ranked = sorted(
        candidates, key=lambda c: (c.slope, c.drop, -c.at_s), reverse=True
    )
    overall = OverallStats(
        entry_ratio=round(overall_entry, 6),
        exit_ratio=round(overall_exit, 6),
        mean_ratio=round(_mean_watch(samples, 0.0, duration_s), 6),
        total_drop=round(overall_entry - overall_exit, 6),
        steepest_drop=steepest_drop,
        top_drops=[_to_drop_point(c, sections) for c in ranked[:TOP_DROPS_KEPT]],
    )

    # -- annotations ----------------------------------------------------------
    by_index = {s.index: s for s in section_stats}
    annotations: list[str] = []
    if steepest_drop is not None:
        annotations.append(_steepest_annotation(steepest_drop, by_index))
    annotations.extend(s.annotation for s in section_stats)
    if not sections:
        annotations.append(
            "No section map for this project — drop points are reported "
            "without section labels (status: unmapped)."
        )

    return RetentionSectionResult(
        **base,
        status="ok" if sections else "unmapped",
        timing_source=section_map.source if sections and section_map else None,
        timing_precision=(
            section_map.precision if sections and section_map else None
        ),
        point_count=len(points),
        sections=section_stats,
        overall=overall,
        steepest_drop=steepest_drop,
        annotations=annotations,
        warnings=warnings,
    )


def map_snapshot_retention(
    snapshot: Any,
    duration_s: float,
    section_map: SectionMap | None = None,
) -> RetentionSectionResult:
    """Map one stored analytics snapshot's curve onto sections.

    ``snapshot`` is a mapping in the Analytics Ingestion store's
    shape (``app.analytics.store`` row dicts): the curve is read from
    ``retention_curve`` (already parsed) or ``retention_curve_json``
    (raw JSON), identity from ``id`` / ``youtube_video_id`` (or its
    ``video_external_id`` alias) / ``project_id``.  ``duration_s`` is
    the video's known duration from the project / publish manifest —
    snapshots do not carry it.
    """
    if not isinstance(snapshot, dict):
        raise ValueError(f"snapshot must be a mapping, got {type(snapshot)}")
    curve = snapshot.get("retention_curve")
    if curve is None:
        curve = snapshot.get("retention_curve_json")
    return map_retention_to_sections(
        curve,
        duration_s,
        section_map,
        snapshot_id=snapshot.get("id") or snapshot.get("snapshot_id"),
        video_id=(
            snapshot.get("youtube_video_id")
            or snapshot.get("video_external_id")
            or snapshot.get("video_id")
        ),
        project_id=snapshot.get("project_id"),
    )
