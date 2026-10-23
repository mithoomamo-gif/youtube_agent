"""Calibration report read model — what the UI report renders.

:func:`build_calibration_report` returns the per-(channel, niche,
metric) calibration summary plus **every** per-video row in scope,
enriched with its signed / percentage error and band verdict.
Honesty rules, enforced structurally here:

* No row is ever filtered out of a scope it belongs to — misses,
  blowouts and insufficient-evidence scopes all appear.
* D+7 and D+28 are summarised separately (see
  :mod:`app.calibration.calibration_math` for the definitions of
  signed bias, MAPE and band coverage, and the small-n rule).
* Aggregates are computed per (channel, niche, **metric**):
  percentage errors across different metrics (views vs retention %)
  are not commensurable, so pooling them would manufacture a
  meaningless number.  Scopes are still keyed visibly by channel +
  niche, with the metric named on every scope.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from app.calibration.calibration_math import summarise_rows
from app.calibration.store import CalibrationStore

__all__ = ["build_calibration_report"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_calibration_report(
    source: Any,
    *,
    channel_id: Optional[str] = None,
    niche: Optional[str] = None,
) -> dict:
    """Build the calibration report for a scope.

    ``source`` is a :class:`CalibrationStore`, a ``conn_factory`` or
    a bare ``sqlite3.Connection``.  ``channel_id`` / ``niche``
    optionally narrow the report to one channel and/or niche; omitted
    filters include every scope (never a best-of selection).

    Returns::

        {
          "generated_at": <epoch ms>,
          "filters": {"channel_id": ..., "niche": ...},
          "scopes": [
            {
              "channel_id": ..., "niche": ..., "metric": ...,
              "video_count": <distinct videos in scope>,
              "entry_count": <rows in scope>,
              "points": {"D+7": {...stats...}, "D+28": {...stats...}},
              "rows": [ {...row + signed_error / pct_error /
                         abs_pct_error / within_band / verdict...} ],
            }, ...
          ],
          "rows": [ ...every enriched row, all scopes... ],
        }
    """
    store = (
        source
        if isinstance(source, CalibrationStore)
        else CalibrationStore(source)
    )
    rows = store.list_entries(channel_id=channel_id, niche=niche)

    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (str(row["channel_id"]), str(row["niche"]), str(row["metric"]))
        grouped.setdefault(key, []).append(row)

    scopes: list[dict] = []
    all_rows: list[dict] = []
    for (scope_channel, scope_niche, scope_metric), scope_rows in sorted(
        grouped.items()
    ):
        summary = summarise_rows(scope_rows)
        video_ids = {
            r.get("project_id") or r.get("youtube_video_id")
            for r in scope_rows
        }
        scopes.append(
            {
                "channel_id": scope_channel,
                "niche": scope_niche,
                "metric": scope_metric,
                "video_count": len(video_ids),
                "entry_count": len(scope_rows),
                "points": summary["points"],
                "rows": summary["rows"],
            }
        )
        all_rows.extend(summary["rows"])

    return {
        "generated_at": _now_ms(),
        "filters": {"channel_id": channel_id, "niche": niche},
        "scopes": scopes,
        "rows": all_rows,
    }
