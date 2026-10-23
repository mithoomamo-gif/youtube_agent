"""Retention -> section mapping (Round 5, Analytics Ingestion team).

Converts a video's audience-retention curve (the
``analytics_snapshots.retention_curve_json`` contract: elapsed_ratio /
watch_ratio points) into per-section annotations against the
project's script chapter boundaries.

* :mod:`app.retention_sections.mapping` — the pure mapping maths,
  result schema, and honest states (``ok`` / ``unmapped`` /
  ``no_data``).
* :mod:`app.retention_sections.section_maps` — deriving chapter
  boundaries (in seconds) from real production packs, with the
  timing source and precision of each derivation documented.
* :mod:`app.retention_sections.storage` — the file-based persistence
  seam (Style Lab precedent), keyed by snapshot id.
"""

from __future__ import annotations

from .mapping import (
    SCHEMA_VERSION,
    DropPoint,
    OverallStats,
    RetentionPoint,
    RetentionSectionResult,
    SectionMap,
    SectionSpan,
    SectionStats,
    format_timestamp,
    map_retention_to_sections,
    map_snapshot_retention,
    parse_retention_curve,
)
from .section_maps import load_section_map
from .storage import (
    RetentionSectionStore,
    default_retention_sections_dir,
    load_retention_analysis,
    save_retention_analysis,
)

__all__ = [
    "SCHEMA_VERSION",
    "DropPoint",
    "OverallStats",
    "RetentionPoint",
    "RetentionSectionResult",
    "RetentionSectionStore",
    "SectionMap",
    "SectionSpan",
    "SectionStats",
    "default_retention_sections_dir",
    "format_timestamp",
    "load_retention_analysis",
    "load_section_map",
    "map_retention_to_sections",
    "map_snapshot_retention",
    "parse_retention_curve",
    "save_retention_analysis",
]
