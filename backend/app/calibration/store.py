"""SQLite store + recording seam for the calibration log.

The recording seam is :meth:`CalibrationStore.record_entry` (plus the
module-level :func:`record_calibration_entry` convenience wrapper):
the update engine / publish flow calls it when a D+7 or D+28
analytics snapshot lands for a published video, handing over the
expectation band that was issued at publish time and the actual the
snapshot measured.  Recording is **idempotent per video + snapshot
point + metric**: a second call for the same video/point/metric
returns the existing row with ``created == False`` and writes
nothing — the first recorded actual stands, rows are never updated
in place (a calibration log that quietly rewrites its misses would
defeat its purpose).

Store plumbing follows :mod:`app.style_lab.store` /
:mod:`app.learning.store`: constructed with a ``conn_factory``
returning a fresh ``sqlite3.Connection`` per call (e.g.
``functools.partial(app.db.connect, path)``); a bare connection may
also be passed, in which case it is used as-is and never closed.  The
schema is ensured lazily on every entry point, so the store works on
databases that have not run migration 0009.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Mapping, Optional

from app.calibration.schema import SNAPSHOT_POINTS, ensure_calibration_schema

__all__ = [
    "CalibrationStore",
    "CalibrationStoreError",
    "record_calibration_entry",
    "record_video_evidence",
]


class CalibrationStoreError(Exception):
    """Base class for calibration store errors."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _band_fields(
    expectation_band: Any,
    predicted_low: Any,
    predicted_mid: Any,
    predicted_high: Any,
    metric: Optional[str],
) -> tuple[float, float, float, str]:
    """Resolve (low, mid, high, metric) from explicit values and/or a
    band object.

    ``expectation_band`` may be a mapping or any object with
    ``low`` / ``high`` attributes (e.g.
    :class:`app.learning_loop.contracts.ExpectationBand`) and
    optionally ``mid`` / ``metric``.  Explicit arguments win.  A band
    without a midpoint uses the band centre ``(low + high) / 2`` as
    the issued midpoint — documented here because MAPE / bias are
    computed against the midpoint.
    """
    band_low = band_high = band_mid = band_metric = None
    if expectation_band is not None:
        if isinstance(expectation_band, Mapping):
            band_low = expectation_band.get("low")
            band_high = expectation_band.get("high")
            band_mid = expectation_band.get("mid")
            band_metric = expectation_band.get("metric")
        else:
            band_low = getattr(expectation_band, "low", None)
            band_high = getattr(expectation_band, "high", None)
            band_mid = getattr(expectation_band, "mid", None)
            band_metric = getattr(expectation_band, "metric", None)

    low_raw = predicted_low if predicted_low is not None else band_low
    high_raw = predicted_high if predicted_high is not None else band_high
    mid_raw = predicted_mid if predicted_mid is not None else band_mid
    if low_raw is None or high_raw is None:
        raise ValueError(
            "an expectation band is required: pass predicted_low / "
            "predicted_high or an expectation_band with low / high"
        )
    low = _finite_number(low_raw, "predicted_low")
    high = _finite_number(high_raw, "predicted_high")
    if mid_raw is None:
        mid = (low + high) / 2.0
    else:
        mid = _finite_number(mid_raw, "predicted_mid")
    if low < 0:
        raise ValueError(f"predicted_low must be >= 0, got {low}")
    if not (low <= mid <= high):
        raise ValueError(
            "expectation band must satisfy predicted_low <= predicted_mid "
            f"<= predicted_high, got low={low}, mid={mid}, high={high}"
        )
    resolved_metric = metric or band_metric or "views"
    return low, mid, high, str(resolved_metric)


class CalibrationStore:
    """Append-only, idempotent recording + scoped reads over
    ``calibration_log``."""

    def __init__(self, conn_factory: Any) -> None:
        """``conn_factory`` is a callable returning a fresh
        ``sqlite3.Connection``, or a single ``sqlite3.Connection`` to
        reuse (never closed by the store)."""
        # NOTE: sqlite3.Connection objects report callable() == True in
        # CPython, so the connection check must come first.
        if hasattr(conn_factory, "execute"):
            self._conn_factory = None
            self._shared_conn = conn_factory
        elif callable(conn_factory):
            self._conn_factory = conn_factory
            self._shared_conn: Optional[sqlite3.Connection] = None
        else:
            raise TypeError(
                "conn_factory must be callable or a sqlite3 connection"
            )

    # ------------------------------------------------------------------
    # connection / transaction plumbing (same style as app.style_lab.store)
    # ------------------------------------------------------------------
    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(conn, should_close)``."""
        if self._shared_conn is not None:
            conn = self._shared_conn
            should_close = False
        else:
            conn = self._conn_factory()  # type: ignore[misc]
            should_close = True
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        ensure_calibration_schema(conn)
        return conn, should_close

    @contextmanager
    def _write_tx(self, conn: sqlite3.Connection):
        """BEGIN IMMEDIATE transaction; a no-op if one is already open
        (shared-connection callers manage their own transaction)."""
        if conn.in_transaction:
            yield conn
            return
        previous_isolation = conn.isolation_level
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            conn.execute("COMMIT")
        finally:
            try:
                conn.isolation_level = previous_isolation
            except Exception:
                pass

    @staticmethod
    def _close(conn: sqlite3.Connection, should_close: bool) -> None:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # recording seam
    # ------------------------------------------------------------------
    def _find_existing(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: Optional[str],
        youtube_video_id: Optional[str],
        snapshot_point: str,
        metric: str,
    ) -> Optional[dict]:
        """Existing row for this video + point + metric, if any.

        A row matches when *either* supplied identifier matches, so a
        snapshot recorded first by project id and later by YouTube
        video id (or vice versa) is still recognised as the same
        entry.  The table's UNIQUE expression index backstops the
        exact-identifier case under concurrency.
        """
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(str(project_id))
        if youtube_video_id is not None:
            clauses.append("youtube_video_id = ?")
            params.append(str(youtube_video_id))
        if not clauses:
            return None
        row = conn.execute(
            "SELECT * FROM calibration_log WHERE ("
            + " OR ".join(clauses)
            + ") AND snapshot_point = ? AND metric = ?"
            " ORDER BY created_at ASC, rowid ASC LIMIT 1",
            (*params, snapshot_point, metric),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_entry(
        self,
        *,
        channel_id: str,
        niche: str,
        snapshot_point: str,
        actual_value: Any,
        project_id: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
        metric: Optional[str] = None,
        predicted_low: Any = None,
        predicted_mid: Any = None,
        predicted_high: Any = None,
        expectation_band: Any = None,
        snapshot_ref: Optional[str] = None,
        prediction_issued_at: Optional[int] = None,
        published_at: Optional[int] = None,
        recorded_at: Optional[int] = None,
    ) -> tuple[dict, bool]:
        """Record one calibration entry; return ``(row, created)``.

        The seam the update engine / publish flow calls when a
        snapshot lands.  ``channel_id`` + ``niche`` are the scope;
        the video is identified by ``project_id`` and/or
        ``youtube_video_id`` (at least one is required).  The issued
        band comes from ``predicted_low`` / ``predicted_mid`` /
        ``predicted_high`` and/or ``expectation_band`` (mapping or
        object with ``low`` / ``high`` [/ ``mid`` / ``metric``]);
        ``metric`` defaults to the band's metric, else ``'views'``.

        Idempotent per video + snapshot point + metric: if an entry
        already exists, it is returned unchanged with
        ``created == False`` and nothing is written.
        """
        if snapshot_point not in SNAPSHOT_POINTS:
            raise ValueError(
                f"snapshot_point must be one of {SNAPSHOT_POINTS}, "
                f"got {snapshot_point!r}"
            )
        if project_id is None and youtube_video_id is None:
            raise ValueError(
                "a video identity is required: pass project_id and/or "
                "youtube_video_id"
            )
        if not channel_id or not niche:
            raise ValueError("channel_id and niche scope are both required")
        low, mid, high, resolved_metric = _band_fields(
            expectation_band, predicted_low, predicted_mid, predicted_high,
            metric,
        )
        actual = _finite_number(actual_value, "actual_value")

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                existing = self._find_existing(
                    conn,
                    project_id=project_id,
                    youtube_video_id=youtube_video_id,
                    snapshot_point=snapshot_point,
                    metric=resolved_metric,
                )
                if existing is not None:
                    return existing, False
                now = int(recorded_at) if recorded_at is not None else _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO calibration_log ("
                    " id, project_id, youtube_video_id, channel_id, niche,"
                    " metric, snapshot_point, predicted_low, predicted_mid,"
                    " predicted_high, actual_value, snapshot_ref,"
                    " prediction_issued_at, published_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        str(project_id) if project_id is not None else None,
                        str(youtube_video_id)
                        if youtube_video_id is not None
                        else None,
                        str(channel_id),
                        str(niche),
                        resolved_metric,
                        snapshot_point,
                        low,
                        mid,
                        high,
                        actual,
                        str(snapshot_ref) if snapshot_ref is not None else None,
                        prediction_issued_at,
                        published_at,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM calibration_log WHERE id = ?", (row_id,)
                ).fetchone()
                assert row is not None
                return dict(row), True
        finally:
            self._close(conn, should_close)

    def record_video_evidence(
        self, evidence: Any, *, snapshot_ref: Optional[str] = None
    ) -> tuple[dict, bool]:
        """Record from an update-engine ``VideoEvidence``-shaped object.

        Accepts anything with ``video_id``, ``channel_id``, ``niche``,
        ``snapshot_point``, ``expectation_band`` and an ``outcome``
        property / ``views`` attribute (see
        :class:`app.learning_loop.contracts.VideoEvidence`); the
        evidence's YouTube video id is stored as ``youtube_video_id``
        and its band's metric grades the primary outcome (views).
        Raises ``ValueError`` when the evidence carries no expectation
        band — with nothing issued at publish time there is nothing
        to grade, and this seam never fabricates a band after the
        fact.
        """
        band = getattr(evidence, "expectation_band", None)
        if band is None:
            raise ValueError(
                "evidence carries no expectation_band; nothing was "
                "predicted for this video, so no calibration entry "
                "can be recorded"
            )
        outcome = getattr(evidence, "outcome", None)
        if outcome is None:
            outcome = getattr(evidence, "views", None)
        return self.record_entry(
            channel_id=str(evidence.channel_id),
            niche=str(evidence.niche),
            snapshot_point=str(evidence.snapshot_point),
            actual_value=outcome,
            youtube_video_id=str(evidence.video_id),
            expectation_band=band,
            snapshot_ref=snapshot_ref,
            published_at=getattr(evidence, "published_at_ms", None),
            recorded_at=getattr(evidence, "snapshot_at_ms", None),
        )

    # ------------------------------------------------------------------
    # reads (always scoped; used by the report surface)
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        channel_id: Optional[str] = None,
        niche: Optional[str] = None,
        metric: Optional[str] = None,
        snapshot_point: Optional[str] = None,
    ) -> list[dict]:
        """Calibration rows matching every supplied scope filter.

        Filters are exact matches on the stored scope columns, so a
        read for niche A can never return niche B's rows.  With no
        filters, all rows are returned (the report layer groups
        them); ordering is deterministic (created_at, rowid).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(str(channel_id))
        if niche is not None:
            clauses.append("niche = ?")
            params.append(str(niche))
        if metric is not None:
            clauses.append("metric = ?")
            params.append(str(metric))
        if snapshot_point is not None:
            clauses.append("snapshot_point = ?")
            params.append(str(snapshot_point))
        sql = "SELECT * FROM calibration_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, rowid ASC"
        conn, should_close = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            self._close(conn, should_close)


def _as_store(source: Any) -> CalibrationStore:
    if isinstance(source, CalibrationStore):
        return source
    return CalibrationStore(source)


def record_calibration_entry(source: Any, **kwargs: Any) -> tuple[dict, bool]:
    """Module-level recording seam.

    ``source`` is a :class:`CalibrationStore`, a ``conn_factory`` or a
    bare ``sqlite3.Connection``; ``kwargs`` are exactly
    :meth:`CalibrationStore.record_entry`'s arguments.  Returns
    ``(row, created)``.
    """
    return _as_store(source).record_entry(**kwargs)


def record_video_evidence(
    source: Any, evidence: Any, *, snapshot_ref: Optional[str] = None
) -> tuple[dict, bool]:
    """Module-level wrapper for
    :meth:`CalibrationStore.record_video_evidence`."""
    return _as_store(source).record_video_evidence(
        evidence, snapshot_ref=snapshot_ref
    )
