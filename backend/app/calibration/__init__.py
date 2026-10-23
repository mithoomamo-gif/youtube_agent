"""Calibration log + calibration error reporting (Round 5).

The grade sheet for the system's own predictions: per published
video, the expectation band issued at publish time is stored next to
the actuals measured at D+7 / D+28, and per-(channel, niche) error —
signed bias, MAPE against the band midpoint, and band coverage — is
reported with the raw rows attached.  If the system has been wrong,
the user sees it: no row is filtered and small samples are labelled
'insufficient evidence' instead of dressed up as statistics.

Modules:

* :mod:`app.calibration.schema` — ``calibration_log`` DDL + lazy
  creation (kept in sync with Alembic migration
  ``0009_calibration_log`` (renumbered from 0008 at Round 5
  closure, R5-001); 0005/0006/0007 belong to sibling Round 5
  teams — see that module's docstring for the number choice);
* :mod:`app.calibration.store` — :class:`CalibrationStore`, the
  idempotent recording seam (:func:`record_calibration_entry`,
  :func:`record_video_evidence`) the update engine / publish flow
  calls when a snapshot lands;
* :mod:`app.calibration.calibration_math` — the pure error math,
  with the definitions written down;
* :mod:`app.calibration.report` — :func:`build_calibration_report`,
  the read model a UI report renders.
"""

from __future__ import annotations

from app.calibration.calibration_math import MIN_EVIDENCE
from app.calibration.report import build_calibration_report
from app.calibration.schema import SNAPSHOT_POINTS, ensure_calibration_schema
from app.calibration.store import (
    CalibrationStore,
    record_calibration_entry,
    record_video_evidence,
)

__all__ = [
    "MIN_EVIDENCE",
    "SNAPSHOT_POINTS",
    "CalibrationStore",
    "build_calibration_report",
    "ensure_calibration_schema",
    "record_calibration_entry",
    "record_video_evidence",
]
