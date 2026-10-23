"""ROUND 2 integration package: end-to-end production of one video.

The shared contract every stage module builds against lives here:

* :mod:`.models` — :class:`ProjectSpec` (the request),
  :class:`ProductionResult` (the outcome) and :class:`StageContext`
  plus :func:`make_context` (the per-project working directories).
* :mod:`.ledger` — :class:`LicenceLedger`, the per-project licence
  record written into the pack as ``licence_ledger.csv``.
* :mod:`.pack` — :func:`write_report_md` and :func:`write_json`, the
  pack file writers.
"""

from .ledger import CSV_COLUMNS, LicenceLedger, LicenceRow
from .models import ProductionResult, ProjectSpec, StageContext, make_context
from .pack import write_json, write_report_md
from .stage_bridge import (
    run_package,
    run_qc,
    run_render,
    run_research,
    run_script,
    run_thumbnail,
    run_visuals,
    run_voice,
)

__all__ = [
    "CSV_COLUMNS",
    "LicenceLedger",
    "LicenceRow",
    "ProductionResult",
    "ProjectSpec",
    "StageContext",
    "make_context",
    "run_package",
    "run_qc",
    "run_render",
    "run_research",
    "run_script",
    "run_thumbnail",
    "run_visuals",
    "run_voice",
    "write_json",
    "write_report_md",
]
