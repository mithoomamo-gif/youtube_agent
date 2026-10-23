"""SQLite store for analytics snapshots + channel metric snapshots.

A small store class over the tables managed by
:mod:`app.analytics.schema` (``analytics_snapshots``,
``channel_metric_snapshots``), following the codebase patterns of
:mod:`app.style_lab.store` / :mod:`app.execution.budget_reservations`:

* constructed with a ``conn_factory`` returning a fresh
  ``sqlite3.Connection`` per call (e.g.
  ``functools.partial(app.db.connect, path)``); a bare connection
  may also be passed, in which case it is used as-is and never
  closed;
* ids are ``uuid4().hex``; timestamps are epoch milliseconds;
* the schema is ensured lazily on every entry point, so the store
  works on databases that have not run migrations.

Idempotency (the sync contract)
-------------------------------
A video snapshot is identified by ``(channel_id, youtube_video_id,
snapshot_kind, capture_day)`` where ``capture_day`` is the **UTC**
calendar day of the capture instant, derived at write time
(:func:`app.analytics.schema.capture_day_for`) and stored on the
row.  Re-upserting the same key updates the one row in place (same
``id``, same ``created_at``; ``updated_at`` and the capture instant
move to the latest write) — a re-sync never duplicates.  Metrics
are cumulative point-in-time figures from the YouTube Analytics
API, not deltas: on update the supplied metrics are *merged* over
the stored ones (new keys win, unreported keys keep their previous
values), the retention curve is replaced only when a new one is
supplied, and ``project_id`` is filled in when newly known but
never cleared.  Snapshots with no video id (``youtube_video_id``
NULL) are exempt from day-uniqueness (SQLite unique indexes treat
NULLs as distinct) and always insert a fresh row.

Channel metric snapshots are identified by ``(channel_id,
snapshot_kind, capture_day)`` with the same update-in-place rules;
for the value columns (``subscribers`` / ``total_views`` /
``watch_hours`` / ``qualified_watch_hours``) a ``None`` argument
means "not reported this sync" and keeps the stored value.

Metrics shape
-------------
``metrics`` is a plain mapping.  Canonical keys: ``views``,
``watch_minutes``, ``watch_hours`` (either watch key; minutes win
when both are given), ``avg_view_duration_sec``,
``avg_view_percentage``, ``likes``, ``comments`` (alias
``comments_count``), ``shares``, ``subs_gained`` (alias
``subs_delta``), ``ctr`` (a 0..1 fraction, nullable — alias
``ctr_bps`` in basis points), ``revenue_micros``.  Unknown keys are
preserved verbatim in ``metrics_json``.  On every write the
canonical keys are mirrored into the legacy discrete columns
(``views``, ``watch_hours_x100``, ``likes``, ``comments_count``,
``shares``, ``ctr_bps``, ``avg_view_duration_sec``, ``subs_delta``,
``revenue_micros``) so section-15 readers (e.g. the learning
loop's ``SqliteAnalyticsSource``) keep working.  Returned dicts
always carry a parsed ``metrics`` mapping — synthesised from the
discrete columns for legacy rows that predate ``metrics_json`` —
plus the aliases ``video_external_id`` (= ``youtube_video_id``)
and ``captured_at`` (= ``snapshot_at``).

Owner scoping: every read/write is keyed by ``channel_id``; a read
for another channel's row looks exactly like "not found".
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Mapping, Optional

from app.analytics.schema import (
    CHANNEL_SNAPSHOT_KINDS,
    SNAPSHOT_KINDS,
    capture_day_for,
)

__all__ = [
    "AnalyticsStore",
    "AnalyticsStoreError",
    "SNAPSHOT_KINDS",
    "CHANNEL_SNAPSHOT_KINDS",
]


class AnalyticsStoreError(Exception):
    """Base class for analytics store errors."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


def _parse_json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(round(float(value)))


def _mirrors_from_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Legacy discrete-column values derived from a metrics mapping.

    A key absent from ``metrics`` maps to ``None`` here; callers
    decide whether ``None`` means the column default (insert) or
    "keep the stored value" (update).
    """
    watch_x100: Optional[int] = None
    if metrics.get("watch_minutes") is not None:
        watch_x100 = int(round(float(metrics["watch_minutes"]) * 100.0 / 60.0))
    elif metrics.get("watch_hours") is not None:
        watch_x100 = int(round(float(metrics["watch_hours"]) * 100.0))

    ctr_bps: Optional[int] = None
    if metrics.get("ctr_bps") is not None:
        ctr_bps = _as_int(metrics["ctr_bps"])
    elif metrics.get("ctr") is not None:
        ctr_bps = int(round(float(metrics["ctr"]) * 10_000))

    comments = metrics.get("comments", metrics.get("comments_count"))
    subs = metrics.get("subs_gained", metrics.get("subs_delta"))
    return {
        "views": _as_int(metrics.get("views")),
        "watch_hours_x100": watch_x100,
        "likes": _as_int(metrics.get("likes")),
        "comments_count": _as_int(comments),
        "shares": _as_int(metrics.get("shares")),
        "ctr_bps": ctr_bps,
        "avg_view_duration_sec": _as_int(metrics.get("avg_view_duration_sec")),
        "subs_delta": _as_int(subs),
        "revenue_micros": _as_int(metrics.get("revenue_micros")),
    }


def _synthesise_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    """Metrics mapping for a legacy row (no ``metrics_json``)."""
    watch_x100 = row.get("watch_hours_x100") or 0
    ctr_bps = row.get("ctr_bps")
    return {
        "views": row.get("views") or 0,
        "watch_minutes": float(watch_x100) * 60.0 / 100.0,
        "watch_hours": float(watch_x100) / 100.0,
        "avg_view_duration_sec": row.get("avg_view_duration_sec"),
        "likes": row.get("likes") or 0,
        "comments": row.get("comments_count") or 0,
        "shares": row.get("shares") or 0,
        "subs_gained": row.get("subs_delta"),
        "ctr": (float(ctr_bps) / 10_000.0) if ctr_bps is not None else None,
    }


class AnalyticsStore:
    """Idempotent upserts + scoped reads over the analytics tables."""

    def __init__(self, conn_factory: Any) -> None:
        """``conn_factory`` is a callable returning a fresh
        ``sqlite3.Connection``, or a single ``sqlite3.Connection``
        to reuse (never closed by the store)."""
        # NOTE: sqlite3.Connection objects report callable() == True
        # in CPython, so the connection check must come first
        # (same note as StyleLabStore).
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
    # connection / transaction plumbing (same style as StyleLabStore)
    # ------------------------------------------------------------------
    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(conn, should_close)`` with the schema ensured."""
        from app.analytics.schema import ensure_analytics_schema

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
        ensure_analytics_schema(conn)
        return conn, should_close

    @contextmanager
    def _write_tx(self, conn: sqlite3.Connection):
        """BEGIN IMMEDIATE transaction; a no-op if one is already
        open (shared-connection callers manage their own)."""
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
    # row shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        metrics = _parse_json(data.get("metrics_json"))
        data["metrics"] = (
            metrics if isinstance(metrics, dict) else _synthesise_metrics(data)
        )
        data["retention_curve"] = _parse_json(data.get("retention_curve_json"))
        data["video_external_id"] = data.get("youtube_video_id")
        captured = data.get("captured_at")
        data["captured_at"] = captured if captured is not None else data.get(
            "snapshot_at"
        )
        return data

    @staticmethod
    def _channel_metrics_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        metrics = _parse_json(data.get("metrics_json"))
        data["metrics"] = metrics if isinstance(metrics, dict) else {}
        return data

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _check_kind(kind: str, allowed: tuple, what: str) -> str:
        if kind not in allowed:
            raise ValueError(
                f"{what} snapshot_kind must be one of {allowed}, got {kind!r}"
            )
        return kind

    @staticmethod
    def _resolve_captured(
        captured_at_ms: Optional[int], captured_at: Optional[int]
    ) -> int:
        value = captured_at_ms if captured_at_ms is not None else captured_at
        if value is None:
            return _now_ms()
        value = int(value)
        if value < 0:
            raise ValueError("captured_at must be epoch milliseconds >= 0")
        return value

    # ==================================================================
    # Video snapshots
    # ==================================================================
    def upsert_snapshot(
        self,
        channel_id: str,
        video_external_id: Optional[str] = None,
        snapshot_kind: str = "scheduled",
        *,
        metrics: Optional[Mapping[str, Any]] = None,
        retention_curve: Any = None,
        project_id: Optional[str] = None,
        captured_at_ms: Optional[int] = None,
        captured_at: Optional[int] = None,
        source: str = "youtube_analytics_api",
        youtube_video_id: Optional[str] = None,
    ) -> dict:
        """Insert or update-in-place one video analytics snapshot.

        The identity is ``(channel_id, video_external_id,
        snapshot_kind, UTC capture day)``: a second upsert with the
        same identity returns the same row (same ``id`` /
        ``created_at``) with merged metrics and a fresh
        ``updated_at``.  ``video_external_id`` is the YouTube video
        id, stored in the ``youtube_video_id`` column (the repo
        name); ``youtube_video_id=`` is accepted as an alias.
        ``retention_curve`` is a list of elapsed/watch ratio points,
        stored verbatim as JSON.
        """
        if youtube_video_id is not None:
            if (
                video_external_id is not None
                and str(youtube_video_id) != str(video_external_id)
            ):
                raise ValueError(
                    "video_external_id and youtube_video_id disagree: "
                    f"{video_external_id!r} vs {youtube_video_id!r}"
                )
            video_external_id = youtube_video_id
        kind = self._check_kind(snapshot_kind, SNAPSHOT_KINDS, "analytics")
        if retention_curve is not None and not isinstance(
            retention_curve, (list, tuple)
        ):
            raise ValueError("retention_curve must be a list of points or None")
        captured = self._resolve_captured(captured_at_ms, captured_at)
        capture_day = capture_day_for(captured)
        new_metrics: dict[str, Any] = dict(metrics or {})
        video_id = (
            str(video_external_id) if video_external_id is not None else None
        )

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                existing = None
                if video_id is not None:
                    row = conn.execute(
                        "SELECT * FROM analytics_snapshots"
                        " WHERE channel_id = ? AND youtube_video_id = ?"
                        " AND snapshot_kind = ? AND capture_day = ?",
                        (str(channel_id), video_id, kind, capture_day),
                    ).fetchone()
                    existing = dict(row) if row is not None else None

                now = _now_ms()
                if existing is not None:
                    old_metrics = _parse_json(existing.get("metrics_json"))
                    merged = (
                        dict(old_metrics)
                        if isinstance(old_metrics, dict)
                        else _synthesise_metrics(existing)
                    )
                    merged.update(new_metrics)
                    mirrors = _mirrors_from_metrics(merged)
                    # A mirror with no source key in the merged
                    # metrics keeps the stored column value.
                    for col, val in mirrors.items():
                        if val is None and existing.get(col) is not None:
                            mirrors[col] = existing[col]
                    conn.execute(
                        "UPDATE analytics_snapshots SET"
                        " project_id = ?, snapshot_at = ?, views = ?,"
                        " watch_hours_x100 = ?, likes = ?,"
                        " comments_count = ?, shares = ?, ctr_bps = ?,"
                        " avg_view_duration_sec = ?,"
                        " retention_curve_json = ?, subs_delta = ?,"
                        " revenue_micros = ?, source = ?,"
                        " metrics_json = ?, captured_at = ?,"
                        " updated_at = ? WHERE id = ?",
                        (
                            project_id
                            if project_id is not None
                            else existing.get("project_id"),
                            captured,
                            mirrors["views"] or 0,
                            mirrors["watch_hours_x100"] or 0,
                            mirrors["likes"] or 0,
                            mirrors["comments_count"] or 0,
                            mirrors["shares"] or 0,
                            mirrors["ctr_bps"],
                            mirrors["avg_view_duration_sec"],
                            json.dumps(list(retention_curve))
                            if retention_curve is not None
                            else existing.get("retention_curve_json"),
                            mirrors["subs_delta"],
                            mirrors["revenue_micros"],
                            source,
                            json.dumps(merged),
                            captured,
                            now,
                            existing["id"],
                        ),
                    )
                    result = self._fetch_snapshot(conn, existing["id"])
                    assert result is not None
                    return result

                mirrors = _mirrors_from_metrics(new_metrics)
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO analytics_snapshots ("
                    " id, project_id, channel_id, youtube_video_id,"
                    " snapshot_at, views, watch_hours_x100, likes,"
                    " comments_count, shares, ctr_bps,"
                    " avg_view_duration_sec, retention_curve_json,"
                    " subs_delta, revenue_micros, source, created_at,"
                    " snapshot_kind, capture_day, metrics_json,"
                    " captured_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        str(project_id) if project_id is not None else None,
                        str(channel_id),
                        video_id,
                        captured,
                        mirrors["views"] or 0,
                        mirrors["watch_hours_x100"] or 0,
                        mirrors["likes"] or 0,
                        mirrors["comments_count"] or 0,
                        mirrors["shares"] or 0,
                        mirrors["ctr_bps"],
                        mirrors["avg_view_duration_sec"],
                        json.dumps(list(retention_curve))
                        if retention_curve is not None
                        else None,
                        mirrors["subs_delta"],
                        mirrors["revenue_micros"],
                        source,
                        now,
                        kind,
                        capture_day,
                        json.dumps(new_metrics),
                        captured,
                        now,
                    ),
                )
                result = self._fetch_snapshot(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_snapshot(self, conn, snapshot_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM analytics_snapshots WHERE id = ?",
            (str(snapshot_id),),
        ).fetchone()
        return self._snapshot_dict(row)

    def get_snapshot(
        self, snapshot_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One snapshot by id, or ``None`` (cross-channel = None)."""
        conn, should_close = self._connect()
        try:
            snap = self._fetch_snapshot(conn, snapshot_id)
            if snap is None:
                return None
            if channel_id is not None and str(snap["channel_id"]) != str(
                channel_id
            ):
                return None
            return snap
        finally:
            self._close(conn, should_close)

    def list_snapshots(
        self,
        channel_id: str,
        *,
        video_external_id: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
        snapshot_kind: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Snapshots owned by ``channel_id`` (always scoped),
        oldest capture first, optionally filtered by video / kind /
        project."""
        video_id = (
            youtube_video_id if youtube_video_id is not None else video_external_id
        )
        clauses = ["channel_id = ?"]
        params: list[Any] = [str(channel_id)]
        if video_id is not None:
            clauses.append("youtube_video_id = ?")
            params.append(str(video_id))
        if snapshot_kind is not None:
            clauses.append("snapshot_kind = ?")
            params.append(
                self._check_kind(snapshot_kind, SNAPSHOT_KINDS, "analytics")
            )
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(str(project_id))
        sql = (
            "SELECT * FROM analytics_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at ASC, created_at ASC, rowid ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        conn, should_close = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._snapshot_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def list_video_snapshots(
        self, channel_id: str, video_external_id: str, **kwargs: Any
    ) -> list[dict]:
        """All snapshots for one video on one channel."""
        return self.list_snapshots(
            channel_id, video_external_id=video_external_id, **kwargs
        )

    def latest_snapshot(
        self,
        channel_id: str,
        video_external_id: str,
        *,
        snapshot_kind: Optional[str] = None,
    ) -> Optional[dict]:
        """The most recently captured snapshot for a video, or None."""
        rows = self.list_snapshots(
            channel_id,
            video_external_id=video_external_id,
            snapshot_kind=snapshot_kind,
        )
        return rows[-1] if rows else None

    # ==================================================================
    # Channel metric snapshots (YPP progress)
    # ==================================================================
    def upsert_channel_metrics(
        self,
        channel_id: str,
        *,
        subscribers: Optional[int] = None,
        total_views: Optional[int] = None,
        watch_hours: Optional[float] = None,
        qualified_watch_hours: Optional[float] = None,
        snapshot_kind: str = "scheduled",
        metrics: Optional[Mapping[str, Any]] = None,
        captured_at_ms: Optional[int] = None,
        captured_at: Optional[int] = None,
    ) -> dict:
        """Insert or update-in-place one channel-metrics snapshot.

        Identity: ``(channel_id, snapshot_kind, UTC capture day)``.
        ``watch_hours`` is ordinary (all) watch time;
        ``qualified_watch_hours`` is the Analytics qualification
        figure for YPP — keep it ``None`` when qualification data
        is unavailable (it is never derived from ordinary hours).
        A ``None`` value argument keeps the stored value on update.
        """
        kind = self._check_kind(
            snapshot_kind, CHANNEL_SNAPSHOT_KINDS, "channel metrics"
        )
        captured = self._resolve_captured(captured_at_ms, captured_at)
        capture_day = capture_day_for(captured)
        new_metrics: dict[str, Any] = dict(metrics or {})

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                row = conn.execute(
                    "SELECT * FROM channel_metric_snapshots"
                    " WHERE channel_id = ? AND snapshot_kind = ?"
                    " AND capture_day = ?",
                    (str(channel_id), kind, capture_day),
                ).fetchone()
                existing = dict(row) if row is not None else None
                now = _now_ms()
                if existing is not None:
                    old_metrics = _parse_json(existing.get("metrics_json"))
                    merged = dict(old_metrics) if isinstance(old_metrics, dict) else {}
                    merged.update(new_metrics)
                    conn.execute(
                        "UPDATE channel_metric_snapshots SET"
                        " captured_at = ?, subscribers = ?,"
                        " total_views = ?, watch_hours = ?,"
                        " qualified_watch_hours = ?, metrics_json = ?,"
                        " updated_at = ? WHERE id = ?",
                        (
                            captured,
                            subscribers
                            if subscribers is not None
                            else existing.get("subscribers"),
                            total_views
                            if total_views is not None
                            else existing.get("total_views"),
                            watch_hours
                            if watch_hours is not None
                            else existing.get("watch_hours"),
                            qualified_watch_hours
                            if qualified_watch_hours is not None
                            else existing.get("qualified_watch_hours"),
                            json.dumps(merged),
                            now,
                            existing["id"],
                        ),
                    )
                    result = self._fetch_channel_metrics(conn, existing["id"])
                    assert result is not None
                    return result

                row_id = _new_id()
                conn.execute(
                    "INSERT INTO channel_metric_snapshots ("
                    " id, channel_id, snapshot_kind, captured_at,"
                    " capture_day, subscribers, total_views,"
                    " watch_hours, qualified_watch_hours, metrics_json,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        str(channel_id),
                        kind,
                        captured,
                        capture_day,
                        subscribers,
                        total_views,
                        watch_hours,
                        qualified_watch_hours,
                        json.dumps(new_metrics),
                        now,
                        now,
                    ),
                )
                result = self._fetch_channel_metrics(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_channel_metrics(self, conn, row_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM channel_metric_snapshots WHERE id = ?",
            (str(row_id),),
        ).fetchone()
        return self._channel_metrics_dict(row)

    def list_channel_metrics(
        self,
        channel_id: str,
        *,
        snapshot_kind: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Channel metric snapshots for ``channel_id`` (always
        scoped), oldest capture first."""
        clauses = ["channel_id = ?"]
        params: list[Any] = [str(channel_id)]
        if snapshot_kind is not None:
            clauses.append("snapshot_kind = ?")
            params.append(
                self._check_kind(
                    snapshot_kind, CHANNEL_SNAPSHOT_KINDS, "channel metrics"
                )
            )
        sql = (
            "SELECT * FROM channel_metric_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at ASC, created_at ASC, rowid ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        conn, should_close = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                d for d in (self._channel_metrics_dict(r) for r in rows) if d
            ]
        finally:
            self._close(conn, should_close)

    def latest_channel_metrics(
        self, channel_id: str, *, snapshot_kind: Optional[str] = None
    ) -> Optional[dict]:
        """The most recently captured channel metrics, or ``None``."""
        rows = self.list_channel_metrics(
            channel_id, snapshot_kind=snapshot_kind
        )
        return rows[-1] if rows else None

    # ==================================================================
    # Sync bookkeeping
    # ==================================================================
    def last_synced_at(self, channel_id: str) -> Optional[int]:
        """Epoch ms of the channel's most recent capture across
        both analytics tables, or ``None`` when the channel has
        never been synced."""
        conn, should_close = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(v) FROM ("
                " SELECT COALESCE(captured_at, snapshot_at) AS v"
                " FROM analytics_snapshots WHERE channel_id = ?"
                " UNION ALL"
                " SELECT captured_at AS v FROM channel_metric_snapshots"
                " WHERE channel_id = ?"
                ")",
                (str(channel_id), str(channel_id)),
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        finally:
            self._close(conn, should_close)
