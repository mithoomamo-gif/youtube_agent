"""Retention-section analysis — persistence seam.

Storage decision (Round 5, Analytics Ingestion team)
-----------------------------------------------------
The analytics storage layer's contract is
``analytics_snapshots`` (``app.schema_ddl``): it stores the raw
metrics and the retention curve (``retention_curve_json``) but has
**no column for an analysis payload or a result location** — the
same gap the Style Lab intake tables had.  Following the Style Lab
precedent (``app.style_analysis.storage``), this module therefore OWNS a
file-based results seam, complementary to the snapshot row:

Layout (under a caller-supplied base dir, conventionally
``<data_dir>/analytics/retention_sections`` — see
:func:`default_retention_sections_dir`)::

    <base>/
        index.json                       # RetentionIndex (all entries)
        <key>/                           # slug of snapshot_id (else video_id)
            analysis.json                # RetentionSectionResult (schema JSON)
            annotations.md               # human-readable rendering

* The snapshot row is the identity: ``key`` IS the slugified
  ``analytics_snapshots.id`` when a snapshot id is known, else the
  ``video_id``.  Saving a new analysis for the same snapshot replaces
  its files and index entry (latest analysis wins — history is a
  future concern, as in Style Lab).
* ``index.json`` is rewritten atomically (tmp file + ``os.replace``)
  on every save and carries the lookup fields (ids, status, steepest
  drop summary, paths, timestamps).
* No database table is created here, so nothing can collide with the
  ingestion worker's schema.  A future migration may add an
  ``analysis_path`` column to ``analytics_snapshots`` pointing at
  ``analysis.json``; the signatures here (save/load/list keyed by
  snapshot id) would not need to change.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .mapping import RetentionSectionResult

__all__ = [
    "IndexEntry",
    "RetentionIndex",
    "RetentionSectionStore",
    "StoredRetentionAnalysis",
    "default_retention_sections_dir",
    "load_retention_analysis",
    "render_annotations_md",
    "save_retention_analysis",
    "slugify_key",
]

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify_key(raw: str) -> str:
    """Filesystem-safe key from a snapshot_id / video_id."""
    slug = _SLUG_RE.sub("-", raw.strip().lower()).strip("-.")
    return slug or "unknown"


def default_retention_sections_dir(data_dir: Path | str | None = None) -> Path:
    """Conventional base dir: ``<data_dir>/analytics/retention_sections``."""
    if data_dir is not None:
        return Path(data_dir) / "analytics" / "retention_sections"
    try:
        from app.config import load_settings

        return (
            load_settings(require_token=False).data_dir
            / "analytics"
            / "retention_sections"
        )
    except Exception:
        from app.data_dir import resolve_data_dir

        return resolve_data_dir() / "analytics" / "retention_sections"


def render_annotations_md(result: RetentionSectionResult) -> str:
    """Human-readable rendering of one analysis (the annotations.md)."""
    lines = [
        "# Retention by section",
        "",
        f"- Snapshot: {result.snapshot_id or '—'}",
        f"- Video: {result.video_id or '—'}",
        f"- Project: {result.project_id or '—'}",
        f"- Status: {result.status}",
        f"- Duration: {result.duration_s:.1f}s across {result.point_count} curve points",
    ]
    if result.timing_source:
        lines.append(f"- Section timings: {result.timing_source} — {result.timing_precision}")
    lines += ["", "## Annotations", ""]
    lines += [f"- {note}" for note in result.annotations] or ["- (none)"]
    if result.sections:
        lines += [
            "",
            "## Per-section stats",
            "",
            "| # | Section | Window (s) | Entry | Exit | Mean | Drop-off |",
            "|---|---------|-----------|-------|------|------|----------|",
        ]
        for s in result.sections:
            lines.append(
                f"| {s.index} | {s.title} | {s.start_s:.1f}–{s.end_s:.1f} "
                f"| {s.entry_ratio:.2f} | {s.exit_ratio:.2f} "
                f"| {s.mean_ratio:.2f} | {s.drop_off:+.2f} |"
            )
    if result.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in result.warnings]
    return "\n".join(lines) + "\n"


class IndexEntry(BaseModel):
    """One row of the JSON index (the file-based results table)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str
    analysis_id: str
    snapshot_id: str | None = None
    video_id: str | None = None
    project_id: str | None = None
    status: str
    timing_source: str | None = None
    steepest_drop_at_s: float | None = None
    steepest_drop_section: str | None = None
    created_at: datetime
    updated_at: datetime
    analysis_rel_path: str
    annotations_rel_path: str


class RetentionIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = 1
    entries: list[IndexEntry] = Field(default_factory=list)


class StoredRetentionAnalysis(BaseModel):
    """Paths returned by a save."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str
    analysis_path: Path
    annotations_path: Path
    index_path: Path


class RetentionSectionStore:
    """File-based store rooted at ``base_dir`` (see module docstring)."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    @property
    def index_path(self) -> Path:
        return self.base_dir / "index.json"

    def entry_dir(self, key: str) -> Path:
        return self.base_dir / slugify_key(key)

    @staticmethod
    def key_for(result: RetentionSectionResult) -> str:
        return slugify_key(result.snapshot_id or result.video_id or result.analysis_id)

    # -- index --------------------------------------------------------------
    def _load_index(self) -> RetentionIndex:
        if not self.index_path.exists():
            return RetentionIndex()
        return RetentionIndex.model_validate_json(
            self.index_path.read_text(encoding="utf-8")
        )

    def _write_index(self, index: RetentionIndex) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(index.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def list_entries(self) -> list[IndexEntry]:
        return list(self._load_index().entries)

    def get_entry(self, snapshot_id_or_key: str) -> IndexEntry | None:
        key = slugify_key(snapshot_id_or_key)
        for entry in self._load_index().entries:
            if entry.key == key or entry.snapshot_id == snapshot_id_or_key:
                return entry
        return None

    # -- save / load ----------------------------------------------------------
    def save(self, result: RetentionSectionResult) -> StoredRetentionAnalysis:
        """Persist analysis JSON + annotations.md; update the index."""
        key = self.key_for(result)
        edir = self.entry_dir(key)
        edir.mkdir(parents=True, exist_ok=True)
        analysis_path = edir / "analysis.json"
        annotations_path = edir / "annotations.md"
        analysis_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        annotations_path.write_text(render_annotations_md(result), encoding="utf-8")

        now = datetime.now(timezone.utc)
        index = self._load_index()
        existing = next((e for e in index.entries if e.key == key), None)
        drop = result.steepest_drop
        entry = IndexEntry(
            key=key,
            analysis_id=result.analysis_id,
            snapshot_id=result.snapshot_id,
            video_id=result.video_id,
            project_id=result.project_id,
            status=result.status,
            timing_source=result.timing_source,
            steepest_drop_at_s=drop.at_s if drop else None,
            steepest_drop_section=drop.section_title if drop else None,
            created_at=existing.created_at if existing else result.created_at,
            updated_at=now,
            analysis_rel_path=str(analysis_path.relative_to(self.base_dir)),
            annotations_rel_path=str(annotations_path.relative_to(self.base_dir)),
        )
        index.entries = [e for e in index.entries if e.key != key] + [entry]
        index.entries.sort(key=lambda e: e.key)
        self._write_index(index)
        return StoredRetentionAnalysis(
            key=key,
            analysis_path=analysis_path,
            annotations_path=annotations_path,
            index_path=self.index_path,
        )

    def load(self, snapshot_id_or_key: str) -> RetentionSectionResult | None:
        """Load the latest analysis for a snapshot, or None."""
        entry = self.get_entry(snapshot_id_or_key)
        if entry is None:
            return None
        path = self.base_dir / entry.analysis_rel_path
        if not path.exists():
            return None
        return RetentionSectionResult.model_validate_json(
            path.read_text(encoding="utf-8")
        )


# -- convenience functions ----------------------------------------------------


def save_retention_analysis(
    result: RetentionSectionResult, base_dir: Path | str | None = None
) -> StoredRetentionAnalysis:
    store = RetentionSectionStore(
        base_dir if base_dir is not None else default_retention_sections_dir()
    )
    return store.save(result)


def load_retention_analysis(
    snapshot_id_or_key: str, base_dir: Path | str | None = None
) -> RetentionSectionResult | None:
    store = RetentionSectionStore(
        base_dir if base_dir is not None else default_retention_sections_dir()
    )
    return store.load(snapshot_id_or_key)
