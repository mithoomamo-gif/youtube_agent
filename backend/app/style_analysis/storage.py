"""Style Lab — persistence seam for analysis results.

Storage decision (2026-10-23, Style Lab mini-round)
---------------------------------------------------
Searched ``backend/app`` + ``backend/migrations`` for a competitor /
stylelab intake schema or table at assignment start: none existed
(only the pre-existing ``competitor_analysis`` agent prompt).

DURING this round the intake team landed its contract in a sibling
package — note the underscore — ``app.style_lab`` (migration
``0004_style_lab``): tables ``competitor_channels``,
``competitor_videos`` and ``style_profiles``, with the DB store at
``app.style_lab.store.StyleLabStore``.  That contract is ADOPTED
here as the identity/scope layer:

* :attr:`StyleLabAnalysis.competitor_video_id` IS the
  ``competitor_videos.id`` (uuid4 hex) of the analysed video.
* :attr:`StyleLabAnalysis.channel_id` IS the owning user's channel
  (``competitor_channels.channel_id`` / ``style_profiles.channel_id``
  scope).

However ``competitor_videos`` has NO column for an analysis payload
or breakdown location — it only carries the lifecycle field
``analysis_status`` ('not_queued'/'queued'/'analysing'/'analysed'/
'analysis_failed') plus media metadata.  So this module still OWNS
the results seam, file-based, complementary to the intake tables:

Layout (under a caller-supplied base dir, conventionally
``<data_dir>/stylelab/`` — see :func:`default_stylelab_dir`)::

    <base>/
        index.json                       # StyleLabIndex (all entries)
        <competitor_key>/                # slug of competitor_video_id
            analysis.json                # StyleLabAnalysis (schema JSON)
            breakdown.md                 # render.render_breakdown output

* ``competitor_key`` = slugified ``competitor_video_id`` when present,
  else the ``analysis_id``. One directory per competitor video; saving
  a new analysis for the same video replaces that video's files and
  updates its index entry (latest analysis wins — history is a future
  intake-team concern, noted here deliberately).
* ``index.json`` is the "small results table": a JSON index with one
  entry per analysed video (ids, channel scope, paths, confidence,
  dominant visual label, timestamps). It is rewritten atomically
  (tmp file + ``os.replace``) on every save.
* No database table is created by this module, so it cannot collide
  with the intake migration. The caller/engine, after a successful
  :meth:`StyleAnalysisStore.save`, should flip the intake row via
  ``app.style_lab.store.StyleLabStore.set_analysis_status(
  video_id, 'analysed')`` (or ``'analysis_failed'`` when only an
  all-uncertain fallback could be produced and the engine decides
  that counts as failure) — this module deliberately does not write
  to the intake tables itself, keeping the file seam usable in tests
  and offline tooling without a DB. A future reconciliation may add
  ``analysis_json``/breakdown-path columns to ``competitor_videos``;
  the signatures here (save/load/list keyed by competitor_video_id)
  would not need to change.
* NAMING (SL-009, resolved pre-ship): this analysis package is
  ``app.style_analysis`` while intake is ``app.style_lab``.  The two
  stores are distinctly named: the file store here is
  :class:`StyleAnalysisStore`, while the intake DB store remains
  ``app.style_lab.store.StyleLabStore`` (different classes: DB store
  there, file store here).

Nothing here stores competitor transcript text or thumbnail wording:
only the structural :class:`StyleLabAnalysis` schema is persisted.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .render import render_breakdown
from .schema import StyleLabAnalysis

__all__ = [
    "IndexEntry",
    "StoredAnalysis",
    "StyleLabIndex",
    "StyleAnalysisStore",
    "default_stylelab_dir",
    "load_analysis",
    "save_analysis",
    "slugify_key",
]

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify_key(raw: str) -> str:
    """Filesystem-safe key from a competitor_video_id / analysis_id."""
    slug = _SLUG_RE.sub("-", raw.strip().lower()).strip("-.")
    return slug or "unknown"


def default_stylelab_dir(data_dir: Path | str | None = None) -> Path:
    """Conventional base dir: ``<data_dir>/stylelab``.

    ``data_dir`` should be ``app.config.Settings.data_dir`` at call
    sites; when omitted, ``YTA_DATA_DIR`` / the config default is used
    lazily (imported here so this module stays import-light in tests).
    """
    if data_dir is not None:
        return Path(data_dir) / "stylelab"
    try:
        from app.config import load_settings

        return load_settings(require_token=False).data_dir / "stylelab"
    except Exception:
        from app.data_dir import resolve_data_dir

        return resolve_data_dir() / "stylelab"


class IndexEntry(BaseModel):
    """One row of the JSON index (the file-based results table)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    competitor_key: str
    analysis_id: str
    competitor_video_id: str | None = None
    channel_id: str | None = None
    source_url: str | None = None
    video_path: str
    created_at: datetime
    updated_at: datetime
    overall_confidence: float = 0.0
    #: Dominant visual class name (a likelihood label, see schema).
    dominant_visual_label: str = "uncertain"
    analysis_rel_path: str  # relative to the stylelab base dir
    breakdown_rel_path: str


class StyleLabIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = 1
    entries: list[IndexEntry] = Field(default_factory=list)


class StoredAnalysis(BaseModel):
    """Paths returned by a save."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    competitor_key: str
    analysis_path: Path
    breakdown_path: Path
    index_path: Path


class StyleAnalysisStore:
    """File-based store rooted at ``base_dir`` (see module docstring)."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    # -- paths ------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.base_dir / "index.json"

    def video_dir(self, competitor_key: str) -> Path:
        return self.base_dir / slugify_key(competitor_key)

    @staticmethod
    def key_for(analysis: StyleLabAnalysis) -> str:
        return slugify_key(analysis.competitor_video_id or analysis.analysis_id)

    # -- index --------------------------------------------------------------
    def _load_index(self) -> StyleLabIndex:
        if not self.index_path.exists():
            return StyleLabIndex()
        return StyleLabIndex.model_validate_json(
            self.index_path.read_text(encoding="utf-8")
        )

    def _write_index(self, index: StyleLabIndex) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(index.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def list_entries(self) -> list[IndexEntry]:
        return list(self._load_index().entries)

    def get_entry(
        self, competitor_video_id_or_key: str
    ) -> IndexEntry | None:
        key = slugify_key(competitor_video_id_or_key)
        for entry in self._load_index().entries:
            if entry.competitor_key == key or entry.competitor_video_id == competitor_video_id_or_key:
                return entry
        return None

    # -- save / load ----------------------------------------------------------
    def save(self, analysis: StyleLabAnalysis) -> StoredAnalysis:
        """Persist analysis JSON + breakdown.md; update the index."""
        key = self.key_for(analysis)
        vdir = self.video_dir(key)
        vdir.mkdir(parents=True, exist_ok=True)
        analysis_path = vdir / "analysis.json"
        breakdown_path = vdir / "breakdown.md"
        analysis_path.write_text(
            analysis.model_dump_json(indent=2), encoding="utf-8"
        )
        breakdown_path.write_text(render_breakdown(analysis), encoding="utf-8")

        now = datetime.now(timezone.utc)
        index = self._load_index()
        existing = next(
            (e for e in index.entries if e.competitor_key == key), None
        )
        entry = IndexEntry(
            competitor_key=key,
            analysis_id=analysis.analysis_id,
            competitor_video_id=analysis.competitor_video_id,
            channel_id=analysis.channel_id,
            source_url=analysis.source_url,
            video_path=analysis.video.path,
            created_at=existing.created_at if existing else analysis.created_at,
            updated_at=now,
            overall_confidence=analysis.overall_confidence,
            dominant_visual_label=analysis.visual_mix.dominant_label(),
            analysis_rel_path=str(analysis_path.relative_to(self.base_dir)),
            breakdown_rel_path=str(breakdown_path.relative_to(self.base_dir)),
        )
        index.entries = [
            e for e in index.entries if e.competitor_key != key
        ] + [entry]
        index.entries.sort(key=lambda e: e.competitor_key)
        self._write_index(index)
        return StoredAnalysis(
            competitor_key=key,
            analysis_path=analysis_path,
            breakdown_path=breakdown_path,
            index_path=self.index_path,
        )

    def load(self, competitor_video_id_or_key: str) -> StyleLabAnalysis | None:
        """Load the latest analysis for a competitor video, or None."""
        entry = self.get_entry(competitor_video_id_or_key)
        if entry is None:
            return None
        path = self.base_dir / entry.analysis_rel_path
        if not path.exists():
            return None
        return StyleLabAnalysis.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def load_breakdown(self, competitor_video_id_or_key: str) -> str | None:
        entry = self.get_entry(competitor_video_id_or_key)
        if entry is None:
            return None
        path = self.base_dir / entry.breakdown_rel_path
        return path.read_text(encoding="utf-8") if path.exists() else None


# -- convenience functions ----------------------------------------------------


def save_analysis(
    analysis: StyleLabAnalysis, base_dir: Path | str | None = None
) -> StoredAnalysis:
    """Save via a store at ``base_dir`` (default: conventional dir)."""
    store = StyleAnalysisStore(base_dir if base_dir is not None else default_stylelab_dir())
    return store.save(analysis)


def load_analysis(
    competitor_video_id_or_key: str, base_dir: Path | str | None = None
) -> StyleLabAnalysis | None:
    store = StyleAnalysisStore(base_dir if base_dir is not None else default_stylelab_dir())
    return store.load(competitor_video_id_or_key)
