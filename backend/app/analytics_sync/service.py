"""Analytics sync service + scheduler poller (Round 5, Analytics Ingestion).

Per-channel sync flow (:meth:`AnalyticsSyncService.sync_channel`)
-----------------------------------------------------------------
1. Control flags first (``app.execution.flags`` — the same persisted
   ``stop_all`` / ``pause_all`` the JobRunner and the Style Lab
   claimer honour): a flagged sync records a ``skipped`` run and
   returns; flags are re-read between API calls, so STOP ALL lands
   mid-sync too.
2. A run row (``analytics_sync_runs``) is opened as ``running`` and
   ALWAYS settled with a terminal status + reason — failures are
   recorded, never silent, and no exception escapes the sync.
3. Channel totals: ``get_channel_lifetime_stats`` (Data API
   ``channels.list``, 1 shared unit) -> ``channel_metric_snapshots``
   (lifetime counters), then ``get_channel_totals`` (Analytics
   reports, 1 unit; range = earliest known publish -> today PT)
   merges the period figures into the same day's row (same UTC
   ``capture_day`` — the store derives the idempotency day from the
   capture instant in UTC in both layers, R5-002; the Pacific dates
   used below are Analytics API query-range dates and the quota
   bucket's day, never snapshot identity). ALL storage
   writes go through :class:`app.analytics.store.AnalyticsStore` —
   the storage team's canonical shape (``app.analytics.schema``,
   migration 0007) is the single shape in the settled tree
   (R5-ANALYTICS-001); the period figures ride in the row's
   ``metrics_json`` under ``*_period`` keys, ``total_videos`` as a
   metrics key, and ordinary watch time in the ``watch_hours``
   column (the qualification figure is never written — the client
   does not provide one).
4. Known videos = the union of ``publish_records.youtube_video_id``
   and ``projects.youtube_video_id`` for the channel (publish is
   fake-provider today, so the mapping may be sparse — zero known
   videos is an honest *empty* sync, not an error). Metrics are
   fetched batched via ``get_videos_metrics`` (ONE query per chunk of
   50 videos) and each video with data is upserted into
   ``analytics_snapshots`` with an age-based ``snapshot_kind``
   (``d3``/``d7``/``d28``/``d90`` once the publish age reaches the
   milestone, else the trigger kind ``manual``/``scheduled``).
5. Retention: one ``get_retention_curve`` call per video with data,
   written into that snapshot's ``retention_curve_json``. Retention
   is the quota-hungry step, so it is gated per call (below) and a
   quota stop leaves the metric snapshots intact — partial progress
   is recorded as such.

Quota awareness (REUSED accounting — no parallel counter)
----------------------------------------------------------
The shared daily bucket (10,000 units, Pacific day, 90% safety
ceiling — :mod:`app.providers.youtube.quota`) is read through
:func:`app.quota_view.quota_snapshot_from_conn`, the same aggregation
the usage routers expose. Before every client call the service checks
``used + cost <= ceiling``; when the bucket would be exceeded it
stops cleanly with status ``quota_exhausted`` and the partial counts
recorded. Every client result reports ``quota_units``; the service
writes those into the ``api_usage`` ledger (``provider='youtube'``,
``operation=<quota_method>``) so the bucket accounting stays the
single source of truth — that is what makes the pre-flight check see
this sync's own spend between calls. A client result that itself
reports ``failed``/``quota_exceeded`` stops the sync the same way.

Client contract
---------------
The client is :class:`app.providers.youtube.analytics.
YouTubeAnalyticsClient` (async methods, typed ``AnalyticsResult``):
``status='not_connected'`` (no credential / channel not connected —
zero HTTP made) settles the run as ``not_connected`` with ZERO
snapshot writes; ``status='failed'`` carries a machine-readable
``reason``. All client methods take the *YouTube* channel id
(``channels.youtube_channel_id``), never the internal id. Injected
clients (tests) may be sync or async and may return duck-typed
results — both are normalised here.

Freshness
---------
``analytics_sync_state`` is maintained on every settled run:
``last_attempt_at`` / ``last_status`` / ``last_reason`` /
``last_run_id`` always move; ``last_synced_at`` moves only on
``success``/``partial`` (a channel that never connected has honestly
never synced). :meth:`AnalyticsSyncService.get_sync_status` is the
read surface for the read-API team and the manual-trigger router.

Scheduler hook
--------------
:class:`AnalyticsSyncPoller` is the claimer-shaped sibling of
``app.style_analysis.claimer``: ``poll_once()`` syncs every channel whose
last attempt is older than ``min_interval_ms`` (default 6 h),
re-checking the control flags before every channel and latching
``stop_requested`` on STOP ALL. It is hosted by
:class:`app.execution.service.ExecutionService` on its own loop —
no new runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from app.logging_setup import get_logger
from app.providers.youtube.quota import (
    BUCKET_LIMITS,
    BUCKET_SHARED,
    DEFAULT_SAFETY_CEILING,
    PACIFIC,
    classify_method,
)

from .schema import ensure_analytics_sync_schema

logger = get_logger("app.analytics_sync.service")

__all__ = [
    "AnalyticsSyncService",
    "AnalyticsSyncPoller",
    "snapshot_kind_for",
    "build_default_client",
    "build_default_service",
    "trigger_manual_sync",
    "get_channel_sync_status",
    "STATUS_SUCCESS",
    "STATUS_PARTIAL",
    "STATUS_NOT_CONNECTED",
    "STATUS_QUOTA_EXHAUSTED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "REASON_NOT_CONNECTED",
    "REASON_CLIENT_UNAVAILABLE",
    "REASON_QUOTA_EXHAUSTED",
]

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"

REASON_NOT_CONNECTED = "not_connected"
REASON_CLIENT_UNAVAILABLE = "analytics_client_unavailable"
REASON_QUOTA_EXHAUSTED = "quota_exceeded"
REASON_CHANNEL_NOT_FOUND = "channel_not_found"

#: Client method names (app.providers.youtube.analytics contract).
METHOD_LIFETIME = "channels.list"
METHOD_ANALYTICS = "youtubeAnalytics.reports.query"

#: Videos per get_videos_metrics call (one Analytics query each).
_VIDEO_BATCH_SIZE = 50

#: Default minimum spacing between scheduled sync attempts per channel.
DEFAULT_MIN_INTERVAL_MS = 6 * 3_600_000

_DAY_MS = 86_400_000
_MAX_DETAIL_CHARS = 500


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


def snapshot_kind_for(
    published_at_ms: Optional[int], now_ms: int, trigger: str
) -> str:
    """Age-based snapshot kind for one video.

    ``d3``/``d7``/``d28``/``d90`` once the publish age reaches that
    milestone (the highest milestone reached wins); younger videos —
    and videos with no known publish time — fall back to the trigger
    kind (``manual`` / ``scheduled``).
    """
    fallback = trigger if trigger in (TRIGGER_MANUAL, TRIGGER_SCHEDULED) else TRIGGER_SCHEDULED
    if published_at_ms is None:
        return fallback
    try:
        age_days = (int(now_ms) - int(published_at_ms)) // _DAY_MS
    except (TypeError, ValueError):
        return fallback
    if age_days >= 90:
        return "d90"
    if age_days >= 28:
        return "d28"
    if age_days >= 7:
        return "d7"
    if age_days >= 3:
        return "d3"
    return fallback


# ---------------------------------------------------------------------------
# Result / value normalisation (real AnalyticsResult + duck-typed doubles)
# ---------------------------------------------------------------------------


def _field(obj: Any, *names: str) -> Any:
    """First non-None attribute/key among ``names`` on a dict/object."""
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _result_status(result: Any) -> str:
    status = _field(result, "status")
    return str(status) if status else "failed"


def _result_reason(result: Any) -> Optional[str]:
    reason = _field(result, "reason")
    if reason:
        return str(reason)
    status = _result_status(result)
    if status == "not_connected":
        return REASON_NOT_CONNECTED
    if status == "failed":
        return "analytics_call_failed"
    return None


def _is_quota_reason(reason: Optional[str]) -> bool:
    return bool(reason) and (
        reason in ("quota_exceeded", "quotaExceeded", "quota_exhausted")
        or "quota" in reason
    )


def _await_if_needed(value: Any) -> Any:
    """Drive an awaitable to completion from synchronous code.

    The service is synchronous (hosted via ``asyncio.to_thread`` like
    the claimer, and the manual endpoint is a sync FastAPI handler),
    so normally there is no running loop and ``asyncio.run`` applies.
    When a loop IS running (embedded async callers), the coroutine is
    driven on a throwaway thread so it never deadlocks the loop.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(value)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _pacific_date_of_ms(ms: int) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, tz=PACIFIC).date()


# ---------------------------------------------------------------------------
# Quota gate over the shared bucket (api_usage ledger via app.quota_view)
# ---------------------------------------------------------------------------


class _QuotaGate:
    """Pre-flight checks against the shared daily bucket.

    Reads go through :func:`app.quota_view.quota_snapshot_from_conn`
    (the app's existing quota accounting over the ``api_usage``
    ledger, Pacific day) — there is deliberately no second counter.
    """

    def __init__(self, conn_factory: Callable, safety_ceiling: float) -> None:
        self._conn_factory = conn_factory
        self._ceiling = float(safety_ceiling)

    def used_shared(self) -> int:
        conn = self._conn_factory()
        try:
            from app.quota_view import quota_snapshot_from_conn

            snapshot = quota_snapshot_from_conn(conn)
            return int(snapshot.get("buckets", {}).get("shared", {}).get("used", 0))
        finally:
            _close(conn)

    def would_exceed(self, method: str) -> Optional[str]:
        """Reason string when one more ``method`` call would cross the
        safety ceiling of the shared bucket, else ``None``."""
        _bucket, cost = classify_method(method)
        limit = BUCKET_LIMITS[BUCKET_SHARED]
        ceiling_units = int(limit * self._ceiling)
        try:
            used = self.used_shared()
        except Exception as exc:  # noqa: BLE001 — a broken ledger read
            # must not silently authorise spend; fail closed as quota.
            logger.warning("analytics sync: quota read failed: %s", exc)
            return REASON_QUOTA_EXHAUSTED
        if used + cost > ceiling_units:
            return REASON_QUOTA_EXHAUSTED
        return None


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _commit(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The sync service
# ---------------------------------------------------------------------------


class _SyncOutcome:
    """Mutable counters threaded through one sync run."""

    def __init__(self) -> None:
        self.videos_seen = 0
        self.videos_with_data = 0
        self.snapshots_written = 0
        self.channel_snapshots_written = 0
        self.retention_written = 0
        self.quota_units_charged = 0
        self.errors: list[dict[str, Any]] = []
        self.stop_status: Optional[str] = None  # terminal stop (quota / not_connected)
        self.stop_reason: Optional[str] = None

    def error(self, step: str, reason: str, detail: str = "") -> None:
        self.errors.append(
            {"step": step, "reason": reason, "detail": detail[:_MAX_DETAIL_CHARS]}
        )

    def stop(self, status: str, reason: str, step: str, detail: str = "") -> None:
        self.stop_status = status
        self.stop_reason = reason
        self.error(step, reason, detail)


class AnalyticsSyncService:
    """Per-channel analytics sync over an injected analytics client.

    Parameters
    ----------
    conn_factory:
        Zero-arg callable returning a fresh ``sqlite3.Connection``
        (the contract the queue / stores / claimer all use).
    client:
        The analytics client (real
        :class:`~app.providers.youtube.analytics.YouTubeAnalyticsClient`
        or a duck-typed double). ``None`` -> every sync settles as
        ``not_connected`` / ``analytics_client_unavailable`` with zero
        writes. A zero-arg ``client_factory`` may be supplied instead
        (resolved per sync, so late wiring is picked up).
    events:
        Optional hub/emitter with ``publish(type, payload, ...)``;
        ``analytics.synced`` / ``analytics.sync_failed`` are emitted
        best-effort — a broken hub never breaks a sync.
    record_usage:
        Write client-reported quota units into the ``api_usage``
        ledger (default True — this is what keeps the shared-bucket
        accounting, and therefore the pre-flight gate, truthful).
    """

    def __init__(
        self,
        conn_factory: Callable,
        *,
        client: Any = None,
        client_factory: Optional[Callable] = None,
        events: Any = None,
        record_usage: bool = True,
        safety_ceiling: float = DEFAULT_SAFETY_CEILING,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory
        self._client = client
        self._client_factory = client_factory
        self._events = events
        self._record_usage_enabled = bool(record_usage)
        self._quota = _QuotaGate(conn_factory, safety_ceiling)

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = self._conn_factory()
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        ensure_analytics_sync_schema(conn)
        return conn

    def _flags(self):
        try:
            from app.execution.flags import read_flags

            return read_flags(self._conn_factory)
        except Exception:  # noqa: BLE001 — tolerant like the claimer
            from app.execution.flags import Flags

            return Flags()

    def _blocked_reason(self) -> Optional[str]:
        flags = self._flags()
        if flags.stop_all:
            return "stop_all"
        if flags.pause_all:
            return "pause_all"
        return None

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            try:
                return self._client_factory()
            except Exception as exc:  # noqa: BLE001 — recorded downstream
                logger.warning("analytics client factory failed: %s", exc)
                return None
        return None

    def _emit(self, event_type: str, payload: dict) -> None:
        events = self._events
        if events is None:
            return
        try:
            publish = getattr(events, "publish", None)
            if callable(publish):
                publish(event_type, payload)
            elif callable(events):
                events(event_type, payload)
        except Exception:  # noqa: BLE001 — notifications are best-effort
            pass

    # -- channel / video discovery ----------------------------------------

    def resolve_channel(self, channel_ref: str) -> Optional[dict]:
        """Resolve an internal id OR YouTube channel id to the channel
        row dict (``id`` + ``youtube_channel_id`` + ...), else None."""
        conn = self._connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
            if not cols:
                return None
            where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
            row = conn.execute(
                "SELECT * FROM channels WHERE (id = ? OR youtube_channel_id = ?)"
                f" {where_deleted} LIMIT 1",
                (str(channel_ref), str(channel_ref)),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            _close(conn)

    def list_known_videos(self, channel_id: str) -> list[dict]:
        """Videos known to the app for ``channel_id`` (internal id).

        Union of ``publish_records`` (authoritative publish mapping)
        and ``projects.youtube_video_id``, deduped by YouTube video
        id. Each entry: ``{youtube_video_id, project_id,
        published_at}`` (epoch ms or None), oldest publish first.
        Missing tables degrade to an empty list — an honest empty
        sync, never an error.
        """
        conn = self._connect()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            known: dict[str, dict] = {}
            if "publish_records" in tables:
                try:
                    rows = conn.execute(
                        "SELECT pr.youtube_video_id AS vid, pr.project_id AS pid,"
                        " pr.published_at AS pub FROM publish_records pr"
                        " JOIN projects p ON p.id = pr.project_id"
                        " WHERE p.channel_id = ? AND pr.youtube_video_id IS NOT NULL",
                        (channel_id,),
                    ).fetchall()
                    for row in rows:
                        known[str(row["vid"])] = {
                            "youtube_video_id": str(row["vid"]),
                            "project_id": row["pid"],
                            "published_at": row["pub"],
                        }
                except Exception as exc:  # noqa: BLE001 — degrade honestly
                    logger.warning("analytics sync: publish_records read failed: %s", exc)
            if "projects" in tables:
                rows = conn.execute(
                    "SELECT id, youtube_video_id, published_at FROM projects"
                    " WHERE channel_id = ? AND youtube_video_id IS NOT NULL"
                    " AND deleted_at IS NULL",
                    (channel_id,),
                ).fetchall()
                for row in rows:
                    vid = str(row["youtube_video_id"])
                    existing = known.get(vid)
                    if existing is None:
                        known[vid] = {
                            "youtube_video_id": vid,
                            "project_id": row["id"],
                            "published_at": row["published_at"],
                        }
                    elif existing.get("published_at") is None:
                        existing["published_at"] = row["published_at"]
            videos = list(known.values())
            videos.sort(key=lambda v: (v.get("published_at") is None, v.get("published_at") or 0))
            return videos
        finally:
            _close(conn)

    # -- usage accounting -----------------------------------------------------

    def _record_usage(self, method: Optional[str], units: int, *, ok: bool, now: int) -> None:
        if not self._record_usage_enabled or not method or not units:
            return
        conn = self._connect()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "api_usage" not in tables:
                return
            conn.execute(
                "INSERT INTO api_usage (id, provider, operation, units,"
                " cost_micros, status, created_at) VALUES (?, 'youtube', ?, ?,"
                " 0, ?, ?)",
                (_new_id(), str(method), int(units), "ok" if ok else "error", now),
            )
            _commit(conn)
        except Exception as exc:  # noqa: BLE001 — accounting must not kill a sync
            logger.warning("analytics sync: api_usage write failed: %s", exc)
        finally:
            _close(conn)

    # -- storage writes (through AnalyticsStore — canonical shape) ------
    #
    # R5-ANALYTICS-001 (settled): the ONLY writer of analytics_snapshots
    # / channel_metric_snapshots rows in this service is the storage
    # team's AnalyticsStore (app.analytics.store over the canonical
    # app.analytics.schema shape, migration 0007). No raw SQL writes
    # to those tables remain here.

    def _store(self):
        """The storage layer's store over this service's conn_factory
        (constructed lazily; the store ensures the canonical schema
        on every call)."""
        store = getattr(self, "_store_instance", None)
        if store is None:
            from app.analytics.store import AnalyticsStore

            store = AnalyticsStore(self._conn_factory)
            self._store_instance = store
        return store

    def _store_call(self, outcome: _SyncOutcome, step: str, fn, *args, **kwargs):
        """One AnalyticsStore call; failures are recorded on the
        outcome (never raised) and reported as ``None``."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — recorded, not raised
            outcome.error(step, type(exc).__name__, str(exc))
            return None

    # -- run / state bookkeeping ---------------------------------------------

    def _open_run(self, channel_id: str, trigger: str, now: int) -> str:
        run_id = _new_id()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO analytics_sync_runs (id, channel_id, trigger,"
                " status, started_at, created_at) VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, channel_id, trigger, now, now),
            )
            _commit(conn)
            return run_id
        finally:
            _close(conn)

    def _settle_run(
        self,
        run_id: str,
        channel_id: str,
        status: str,
        reason: Optional[str],
        outcome: _SyncOutcome,
        now: int,
    ) -> None:
        detail = {
            "videos_with_data": outcome.videos_with_data,
            "errors": outcome.errors,
            "empty": outcome.videos_seen == 0,
        }
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE analytics_sync_runs SET status = ?, reason = ?,"
                " videos_seen = ?, snapshots_written = ?,"
                " channel_snapshots_written = ?, retention_written = ?,"
                " quota_units_charged = ?, detail_json = ?, finished_at = ?"
                " WHERE id = ?",
                (
                    status,
                    reason,
                    outcome.videos_seen,
                    outcome.snapshots_written,
                    outcome.channel_snapshots_written,
                    outcome.retention_written,
                    outcome.quota_units_charged,
                    json.dumps(detail),
                    now,
                    run_id,
                ),
            )
            conn.execute(
                "INSERT INTO analytics_sync_state (channel_id, last_synced_at,"
                " last_attempt_at, last_status, last_reason, last_run_id,"
                " last_snapshots_written, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET"
                " last_synced_at = COALESCE(excluded.last_synced_at,"
                "   analytics_sync_state.last_synced_at),"
                " last_attempt_at = excluded.last_attempt_at,"
                " last_status = excluded.last_status,"
                " last_reason = excluded.last_reason,"
                " last_run_id = excluded.last_run_id,"
                " last_snapshots_written = excluded.last_snapshots_written,"
                " updated_at = excluded.updated_at",
                (
                    channel_id,
                    now if status in (STATUS_SUCCESS, STATUS_PARTIAL) else None,
                    now,
                    status,
                    reason,
                    run_id,
                    outcome.snapshots_written,
                    now,
                ),
            )
            _commit(conn)
        finally:
            _close(conn)

    def get_sync_status(self, channel_ref: str) -> Optional[dict]:
        """Freshness/status surface for one channel (read-API seam).

        Returns ``{channel_id, youtube_channel_id, last_synced_at,
        last_attempt_at, last_status, last_reason, last_run_id,
        last_snapshots_written}`` — state fields are None/never_synced
        when the channel has never been attempted. None when the
        channel itself does not exist.
        """
        channel = self.resolve_channel(channel_ref)
        if channel is None:
            return None
        channel_id = str(channel["id"])
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM analytics_sync_state WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            state = dict(row) if row is not None else {}
            return {
                "channel_id": channel_id,
                "youtube_channel_id": channel.get("youtube_channel_id"),
                "last_synced_at": state.get("last_synced_at"),
                "last_attempt_at": state.get("last_attempt_at"),
                "last_status": state.get("last_status") or "never_synced",
                "last_reason": state.get("last_reason"),
                "last_run_id": state.get("last_run_id"),
                "last_snapshots_written": state.get("last_snapshots_written") or 0,
            }
        finally:
            _close(conn)

    def last_attempt_at(self, channel_id: str) -> Optional[int]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_attempt_at FROM analytics_sync_state WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            return int(row[0]) if row is not None and row[0] is not None else None
        finally:
            _close(conn)


    # -- client invocation ---------------------------------------------------

    def _invoke(
        self, client: Any, method_name: str, default_method: str,
        outcome: _SyncOutcome, now: int, **kwargs: Any,
    ) -> Any:
        """Call one client method; ALWAYS returns a result-like object.

        Exceptions are converted into synthetic typed results
        (``QuotaExhausted`` -> failed/quota_exceeded, ``NeedsReauth``
        -> not_connected) so the caller has exactly one verdict path.
        Reported quota units are charged into the outcome and into the
        ``api_usage`` ledger.
        """
        fn = getattr(client, method_name, None)
        if not callable(fn):
            return _SyntheticResult(
                "failed", reason="client_method_missing",
                detail=f"analytics client has no {method_name}()",
            )
        try:
            raw = _await_if_needed(fn(**kwargs))
        except Exception as exc:  # noqa: BLE001 — converted, never raised
            name = type(exc).__name__
            if name == "QuotaExhausted":
                return _SyntheticResult(
                    "failed", reason=REASON_QUOTA_EXHAUSTED,
                    detail=str(exc)[:_MAX_DETAIL_CHARS],
                )
            if name in ("NeedsReauth", "NotConnectedError"):
                return _SyntheticResult(
                    "not_connected", reason="oauth_token_unavailable",
                    detail=str(exc)[:_MAX_DETAIL_CHARS],
                )
            return _SyntheticResult(
                "failed", reason=name,
                detail=str(exc)[:_MAX_DETAIL_CHARS],
            )
        units = _field(raw, "quota_units")
        try:
            units = int(units) if units is not None else 0
        except (TypeError, ValueError):
            units = 0
        if units:
            outcome.quota_units_charged += units
            self._record_usage(
                str(_field(raw, "quota_method") or default_method),
                units, ok=_result_status(raw) == "ok", now=now,
            )
        return raw

    def _verdict(self, result: Any, outcome: _SyncOutcome, step: str) -> str:
        """Classify one client result: 'ok' | 'error' | 'stop'.

        Terminal conditions (not_connected, quota) latch the outcome's
        stop state; ordinary failures are recorded and the sync
        continues with the next step.
        """
        status = _result_status(result)
        if status == "ok":
            return "ok"
        reason = _result_reason(result) or "analytics_call_failed"
        detail = str(_field(result, "detail") or "")
        if status == "not_connected":
            outcome.stop(STATUS_NOT_CONNECTED, reason, step, detail)
            return "stop"
        if _is_quota_reason(reason):
            outcome.stop(STATUS_QUOTA_EXHAUSTED, REASON_QUOTA_EXHAUSTED, step, detail)
            return "stop"
        outcome.error(step, reason, detail)
        return "error"

    def _pre_call(self, outcome: _SyncOutcome, method: str, step: str) -> bool:
        """Gate one API call: prior stop, control flags, quota bucket."""
        if outcome.stop_status is not None:
            return False
        blocked = self._blocked_reason()
        if blocked is not None:
            outcome.stop(STATUS_SKIPPED, blocked, step)
            return False
        quota_reason = self._quota.would_exceed(method)
        if quota_reason is not None:
            outcome.stop(
                STATUS_QUOTA_EXHAUSTED, quota_reason, step,
                "shared daily quota bucket would exceed its safety ceiling",
            )
            return False
        return True

    # -- the sync itself ------------------------------------------------------

    def sync_channel(
        self, channel_ref: str, *, trigger: str = TRIGGER_SCHEDULED,
        now_ms: Optional[int] = None,
    ) -> dict:
        """Sync one channel end to end. NEVER raises.

        ``channel_ref`` is the internal channel id or the YouTube
        channel id. Returns the settled run summary dict (also
        persisted in ``analytics_sync_runs`` /
        ``analytics_sync_state``). An unknown channel returns a
        ``failed``/``channel_not_found`` summary with no run row (the
        router 404s before reaching here).
        """
        now = int(now_ms) if now_ms is not None else _now_ms()
        trigger = trigger if trigger in (TRIGGER_MANUAL, TRIGGER_SCHEDULED) else TRIGGER_SCHEDULED
        channel = self.resolve_channel(channel_ref)
        if channel is None:
            return {
                "channel_id": str(channel_ref),
                "youtube_channel_id": None,
                "trigger": trigger,
                "status": STATUS_FAILED,
                "reason": REASON_CHANNEL_NOT_FOUND,
                "videos_seen": 0,
                "snapshots_written": 0,
                "channel_snapshots_written": 0,
                "retention_written": 0,
                "quota_units_charged": 0,
                "errors": [],
                "empty": True,
                "run_id": None,
                "started_at": now,
                "finished_at": now,
            }
        channel_id = str(channel["id"])
        run_id = self._open_run(channel_id, trigger, now)
        outcome = _SyncOutcome()
        try:
            blocked = self._blocked_reason()
            if blocked is not None:
                outcome.stop(STATUS_SKIPPED, blocked, "preflight")
            else:
                client = self._resolve_client()
                if client is None:
                    outcome.stop(
                        STATUS_NOT_CONNECTED, REASON_CLIENT_UNAVAILABLE,
                        "client",
                        "no analytics client is configured; no API call was made",
                    )
                else:
                    self._run_sync(client, channel, trigger, now, outcome)
        except Exception as exc:  # noqa: BLE001 — a sync must never escape
            logger.error(
                "analytics sync for channel %s blew up: %s", channel_id, exc
            )
            outcome.error("sync", type(exc).__name__, str(exc))

        if outcome.stop_status is not None:
            status, reason = outcome.stop_status, outcome.stop_reason
        elif outcome.errors and outcome.snapshots_written == 0 \
                and outcome.channel_snapshots_written == 0:
            status, reason = STATUS_FAILED, outcome.errors[0]["reason"]
        elif outcome.errors:
            status, reason = STATUS_PARTIAL, outcome.errors[0]["reason"]
        else:
            status, reason = STATUS_SUCCESS, None

        finished = _now_ms()
        try:
            self._settle_run(run_id, channel_id, status, reason, outcome, finished)
        except Exception as exc:  # noqa: BLE001 — settling is bookkeeping
            logger.error("analytics sync: could not settle run %s: %s", run_id, exc)

        result = {
            "run_id": run_id,
            "channel_id": channel_id,
            "youtube_channel_id": channel.get("youtube_channel_id"),
            "trigger": trigger,
            "status": status,
            "reason": reason,
            "videos_seen": outcome.videos_seen,
            "videos_with_data": outcome.videos_with_data,
            "snapshots_written": outcome.snapshots_written,
            "channel_snapshots_written": outcome.channel_snapshots_written,
            "retention_written": outcome.retention_written,
            "quota_units_charged": outcome.quota_units_charged,
            "errors": outcome.errors,
            "empty": outcome.videos_seen == 0,
            "started_at": now,
            "finished_at": finished,
        }
        self._emit(
            "analytics.synced" if status in (STATUS_SUCCESS, STATUS_PARTIAL)
            else "analytics.sync_failed",
            result,
        )
        return result

    def _run_sync(
        self, client: Any, channel: dict, trigger: str, now: int,
        outcome: _SyncOutcome,
    ) -> None:
        channel_id = str(channel["id"])
        yt_id = str(channel.get("youtube_channel_id") or "")
        videos = self.list_known_videos(channel_id)
        outcome.videos_seen = len(videos)
        end_day = _pacific_date_of_ms(now)
        end_date = end_day.isoformat()
        published = [v["published_at"] for v in videos if v.get("published_at")]
        if published:
            period_start = _pacific_date_of_ms(min(published)).isoformat()
        else:
            period_start = (end_day - timedelta(days=28)).isoformat()

        channel_written = False
        store = self._store()

        # 1) Lifetime channel stats -> channel_metric_snapshots.
        if self._pre_call(outcome, METHOD_LIFETIME, "lifetime_stats"):
            result = self._invoke(
                client, "get_channel_lifetime_stats", METHOD_LIFETIME,
                outcome, now, channel_id=yt_id,
            )
            if self._verdict(result, outcome, "lifetime_stats") == "ok":
                data = _field(result, "data")
                if data is not None:
                    written_row = self._store_call(
                        outcome, "lifetime_stats_write",
                        store.upsert_channel_metrics,
                        channel_id,
                        subscribers=_field(data, "subscriber_count"),
                        total_views=_field(data, "view_count", "total_views"),
                        metrics={
                            k: v for k, v in {
                                "total_videos": _field(
                                    data, "video_count", "total_videos"
                                )
                            }.items() if v is not None
                        },
                        snapshot_kind=trigger,
                        captured_at_ms=now,
                    )
                    if written_row is not None:
                        channel_written = True

        # 2) Period channel totals -> merged into the same day's row.
        if self._pre_call(outcome, METHOD_ANALYTICS, "channel_totals"):
            result = self._invoke(
                client, "get_channel_totals", METHOD_ANALYTICS,
                outcome, now, channel_id=yt_id,
                start_date=period_start, end_date=end_date,
            )
            if self._verdict(result, outcome, "channel_totals") == "ok":
                data = _field(result, "data")
                # has_data False = the API returned no row for the
                # range: absence is stored as absence (period figures
                # stay absent in metrics_json), never fabricated zeroes.
                if data is not None and _field(data, "has_data") is not False:
                    watch_minutes = _field(data, "watch_time_minutes")
                    watch_hours = (
                        watch_minutes / 60.0
                        if isinstance(watch_minutes, (int, float))
                        else None
                    )
                    gained = _field(data, "subscribers_gained")
                    lost = _field(data, "subscribers_lost")
                    net = (
                        gained - lost
                        if isinstance(gained, int) and isinstance(lost, int)
                        else gained
                    )
                    period_metrics = {
                        k: v for k, v in {
                            "views_period": _field(data, "views"),
                            "watch_hours_period": watch_hours,
                            "watch_minutes_period": watch_minutes,
                            "subs_delta_period": net,
                            "subscribers_gained_period": gained,
                            "subscribers_lost_period": lost,
                            "likes_period": _field(data, "likes"),
                            "comments_period": _field(data, "comment_count"),
                            "shares_period": _field(data, "shares"),
                            "avg_view_duration_sec_period": _field(
                                data, "average_view_duration_seconds"
                            ),
                            "period_start_date": period_start,
                            "period_end_date": end_date,
                        }.items() if v is not None
                    }
                    written_row = self._store_call(
                        outcome, "channel_totals_write",
                        store.upsert_channel_metrics,
                        channel_id,
                        watch_hours=watch_hours,
                        metrics=period_metrics,
                        snapshot_kind=trigger,
                        captured_at_ms=now,
                    )
                    if written_row is not None:
                        channel_written = True
        if channel_written:
            outcome.channel_snapshots_written = 1

        # 3) Per-video metrics, batched (one query per chunk).
        by_id = {v["youtube_video_id"]: v for v in videos}
        written: list[tuple[dict, str]] = []
        for start in range(0, len(videos), _VIDEO_BATCH_SIZE):
            chunk = videos[start:start + _VIDEO_BATCH_SIZE]
            if outcome.stop_status is not None:
                break
            metrics_list = self._fetch_chunk_metrics(
                client, yt_id, chunk, end_date, now, outcome
            )
            if metrics_list is None:
                continue  # error/stop already recorded by the fetch
            for metrics in metrics_list:
                vid = str(_field(metrics, "video_id") or "")
                video = by_id.get(vid)
                if video is None or not _field(metrics, "has_data"):
                    continue
                outcome.videos_with_data += 1
                kind = snapshot_kind_for(
                    video.get("published_at"), now, trigger
                )
                snap = self._store_call(
                    outcome, "videos_metrics_write",
                    store.upsert_snapshot,
                    channel_id,
                    video_external_id=vid,
                    snapshot_kind=kind,
                    metrics=_video_metrics_mapping(metrics),
                    project_id=video.get("project_id"),
                    captured_at_ms=now,
                )
                if snap is not None:
                    outcome.snapshots_written += 1
                    written.append((video, kind))

        # 4) Retention curves, one gated call per written snapshot.
        # The store replaces the curve on the same (channel, video,
        # kind, UTC day) row when a new one is supplied.
        for video, kind in written:
            if not self._pre_call(outcome, METHOD_ANALYTICS, "retention"):
                break
            result = self._invoke(
                client, "get_retention_curve", METHOD_ANALYTICS,
                outcome, now, channel_id=yt_id,
                video_id=video["youtube_video_id"],
                start_date=(
                    _pacific_date_of_ms(video["published_at"]).isoformat()
                    if video.get("published_at") else period_start
                ),
                end_date=end_date,
            )
            if self._verdict(result, outcome, "retention") != "ok":
                if outcome.stop_status is not None:
                    break
                continue
            data = _field(result, "data")
            points = _field(data, "points") if data is not None else None
            curve = [
                {
                    "elapsed_ratio": _field(p, "elapsed_ratio"),
                    "watch_ratio": _field(p, "watch_ratio"),
                }
                for p in (points or [])
            ]
            curve = [p for p in curve if p["elapsed_ratio"] is not None]
            if not curve:
                continue
            updated = self._store_call(
                outcome, "retention_write",
                store.upsert_snapshot,
                channel_id,
                video_external_id=video["youtube_video_id"],
                snapshot_kind=kind,
                metrics={},
                retention_curve=curve,
                project_id=video.get("project_id"),
                captured_at_ms=now,
            )
            if updated is not None:
                outcome.retention_written += 1

    def _fetch_chunk_metrics(
        self, client: Any, yt_id: str, chunk: list[dict], end_date: str,
        now: int, outcome: _SyncOutcome,
    ) -> Optional[list]:
        """Metrics for one chunk; None when the call failed/stopped
        (the verdict is already recorded on ``outcome``)."""
        published = [v["published_at"] for v in chunk if v.get("published_at")]
        start_date = (
            _pacific_date_of_ms(min(published)).isoformat()
            if published else (date.fromisoformat(end_date) - timedelta(days=28)).isoformat()
        )
        if callable(getattr(client, "get_videos_metrics", None)):
            if not self._pre_call(outcome, METHOD_ANALYTICS, "videos_metrics"):
                return None
            result = self._invoke(
                client, "get_videos_metrics", METHOD_ANALYTICS,
                outcome, now, channel_id=yt_id,
                video_ids=[v["youtube_video_id"] for v in chunk],
                start_date=start_date, end_date=end_date,
            )
            if self._verdict(result, outcome, "videos_metrics") != "ok":
                return None
            data = _field(result, "data")
            return list(data) if isinstance(data, (list, tuple)) else []
        # Fallback: per-video calls (each gated + charged individually).
        collected: list = []
        for video in chunk:
            if not self._pre_call(outcome, METHOD_ANALYTICS, "videos_metrics"):
                return None
            result = self._invoke(
                client, "get_video_metrics", METHOD_ANALYTICS,
                outcome, now, channel_id=yt_id,
                video_id=video["youtube_video_id"],
                start_date=start_date, end_date=end_date,
            )
            if self._verdict(result, outcome, "videos_metrics") != "ok":
                if outcome.stop_status is not None:
                    return None
                continue
            data = _field(result, "data")
            if data is not None:
                collected.append(data)
        return collected

    def sync_all_channels(
        self, *, trigger: str = TRIGGER_SCHEDULED, now_ms: Optional[int] = None
    ) -> list[dict]:
        """Sync every live channel once; never raises."""
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
            if not cols:
                return []
            where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
            rows = conn.execute(
                f"SELECT id FROM channels WHERE 1 = 1 {where_deleted} ORDER BY created_at ASC"
            ).fetchall()
            channel_ids = [str(row[0]) for row in rows]
        finally:
            _close(conn)
        results: list[dict] = []
        for channel_id in channel_ids:
            if self._blocked_reason() is not None:
                break
            results.append(self.sync_channel(channel_id, trigger=trigger, now_ms=now))
        return results


class _SyntheticResult:
    """Result-shaped stand-in for a client call that never returned."""

    def __init__(self, status: str, *, reason: str, detail: str = "") -> None:
        self.status = status
        self.reason = reason
        self.detail = detail
        self.data = None
        self.quota_units = 0
        self.quota_method = None


def _video_metrics_mapping(metrics: Any) -> dict[str, Any]:
    """Canonical metrics mapping (the storage layer's shape) for
    ``analytics_snapshots.metrics_json`` — non-None values only."""
    mapping = {
        "views": _field(metrics, "views"),
        "watch_minutes": _field(metrics, "watch_time_minutes"),
        "avg_view_duration_sec": _field(metrics, "average_view_duration_seconds"),
        "avg_view_percentage": _field(metrics, "average_view_percentage"),
        "likes": _field(metrics, "likes"),
        "comments": _field(metrics, "comment_count"),
        "shares": _field(metrics, "shares"),
        "subs_gained": _field(metrics, "subscribers_gained"),
        "ctr": _field(metrics, "ctr"),
        "revenue_micros": _field(metrics, "revenue_micros"),
    }
    return {k: v for k, v in mapping.items() if v is not None}


# ---------------------------------------------------------------------------
# Scheduler hook — the claimer-shaped poller hosted by ExecutionService
# ---------------------------------------------------------------------------


class AnalyticsSyncPoller:
    """Claims due channels and syncs them (the scheduler hook).

    Mirrors :class:`app.style_analysis.claimer.CompetitorAnalysisClaimer`:
    owned by :class:`app.execution.service.ExecutionService`, driven
    via ``poll_once()`` on the service's loop in a worker thread,
    honouring the persisted ``stop_all`` / ``pause_all`` flags
    (re-read before every channel; ``stop_all`` latches
    :attr:`stop_requested` so the hosting loop exits). A channel is
    due when its last sync *attempt* is older than ``min_interval_ms``
    (or it has never been attempted).
    """

    def __init__(
        self,
        conn_factory: Callable,
        *,
        service: Optional[AnalyticsSyncService] = None,
        client: Any = None,
        client_factory: Optional[Callable] = None,
        events: Any = None,
        min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS,
        max_channels_per_poll: int = 20,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory
        self.service = service or AnalyticsSyncService(
            conn_factory, client=client, client_factory=client_factory,
            events=events,
        )
        self._min_interval_ms = max(0, int(min_interval_ms))
        self._max_channels = max(1, int(max_channels_per_poll))
        #: Latched when a poll observes the persisted ``stop_all`` flag.
        self.stop_requested = False

    def _blocked(self) -> Optional[str]:
        flags = self.service._flags()  # same tolerant read as the sync
        if flags.stop_all:
            self.stop_requested = True
            return "stop_all"
        if flags.pause_all:
            return "pause_all"
        return None

    def due_channel_ids(self, now: int) -> list[str]:
        conn = self.service._connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
            if not cols:
                return []
            where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
            rows = conn.execute(
                f"SELECT id FROM channels WHERE 1 = 1 {where_deleted}"
                " ORDER BY created_at ASC"
            ).fetchall()
            channel_ids = [str(row[0]) for row in rows]
        finally:
            _close(conn)
        due: list[str] = []
        for channel_id in channel_ids:
            last = self.service.last_attempt_at(channel_id)
            if last is None or now - last >= self._min_interval_ms:
                due.append(channel_id)
            if len(due) >= self._max_channels:
                break
        return due

    def poll_once(self, now_ms: Optional[int] = None) -> list[dict]:
        """Sync every due channel once; one report per synced channel.

        An empty list means nothing was due or the control flags are
        set. Never raises — per-channel failures are the sync's own
        recorded outcomes.
        """
        now = int(now_ms) if now_ms is not None else _now_ms()
        if self._blocked() is not None:
            return []
        reports: list[dict] = []
        for channel_id in self.due_channel_ids(now):
            if self._blocked() is not None:
                break
            reports.append(
                self.service.sync_channel(
                    channel_id, trigger=TRIGGER_SCHEDULED, now_ms=now
                )
            )
        return reports


# ---------------------------------------------------------------------------
# Default wiring (production client) + API-facing helpers for the router
# ---------------------------------------------------------------------------


class _KeyringSecretStore:
    """Production SecretStore adapter over ``app.secrets_store``
    (mirrors ``app.routers.channels._KeyringSecretStore``)."""

    def get(self, ref: str) -> Optional[str]:
        from app import secrets_store

        return secrets_store.get_secret(ref)

    def put(self, ref: str, value: str) -> None:
        from app import secrets_store

        secrets_store.set_secret(ref, value)

    def delete(self, ref: str) -> None:
        from app import secrets_store

        secrets_store.delete_secret(ref)


def build_default_client() -> Any:
    """The production analytics client.

    Builds a :class:`TokenManager` from the stored OAuth client pair
    (secret refs ``youtube/client_id`` / ``youtube/client_secret``,
    the channels-router convention; ``YTA_SECRET_BACKEND=env`` selects
    the dev env store). When the pair is absent — or anything in the
    wiring fails — the client is built with NO token manager, which
    is the client's honest ``not_connected`` mode: every method
    returns the typed result with zero HTTP. Either way the sync
    layer records the truth.
    """
    from app.providers.youtube.analytics import YouTubeAnalyticsClient

    try:
        import os

        if os.environ.get("YTA_SECRET_BACKEND", "").strip().lower() == "env":
            from app.providers.secrets_env import EnvSecretStore

            store: Any = EnvSecretStore()
        else:
            store = _KeyringSecretStore()
        client_id = store.get("youtube/client_id")
        client_secret = store.get("youtube/client_secret")
        if not client_id or not client_secret:
            return YouTubeAnalyticsClient(token_manager=None)
        from app.providers.youtube.tokens import TokenManager

        manager = TokenManager(
            store, client_id=str(client_id), client_secret=str(client_secret)
        )
        return YouTubeAnalyticsClient(token_manager=manager)
    except Exception as exc:  # noqa: BLE001 — degrade to not_connected
        logger.warning("analytics client wiring failed (%s); not_connected mode", exc)
        return YouTubeAnalyticsClient(token_manager=None)


def _default_conn_factory() -> Callable:
    from app import db as db_module

    return db_module.get_connection


def build_default_service(**kwargs: Any) -> AnalyticsSyncService:
    """A service over the app DB with the production client factory."""
    kwargs.setdefault("client_factory", build_default_client)
    return AnalyticsSyncService(_default_conn_factory(), **kwargs)


def trigger_manual_sync(
    channel_ref: str, *, service: Optional[AnalyticsSyncService] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """Manual trigger (router seam): 404s unknown channels, else syncs
    with ``trigger='manual'`` and returns the settled run summary."""
    from app.errors import ApiError

    svc = service or build_default_service()
    if svc.resolve_channel(channel_ref) is None:
        raise ApiError(
            404, "channel_not_found", f"No channel with id '{channel_ref}'."
        )
    return svc.sync_channel(channel_ref, trigger=TRIGGER_MANUAL, now_ms=now_ms)


def get_channel_sync_status(
    channel_ref: str, *, service: Optional[AnalyticsSyncService] = None
) -> dict:
    """Sync-status read (router seam): 404s unknown channels."""
    from app.errors import ApiError

    svc = service or build_default_service()
    status = svc.get_sync_status(channel_ref)
    if status is None:
        raise ApiError(
            404, "channel_not_found", f"No channel with id '{channel_ref}'."
        )
    return status
