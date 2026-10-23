"""ExecutionService — lifecycle wiring for the Round-2 execution layer.

Owns the three long-running pieces that turn queued jobs into finished
videos:

* :class:`JobRunner` (``app.execution.runner``, Worker 1) — claims jobs
  from :class:`app.queue.SQLiteJobQueue` and dispatches them to the
  handler registry.  ``runner.run()`` performs the startup lease sweep
  itself, so :meth:`ExecutionService.start` does not sweep separately.
* the handler registry (``app.execution.handlers.build_registry``,
  Worker 2) — stage handlers keyed by job kind, built over the same
  ``conn_factory``; defaults to the stub handlers until real providers
  land.
* :class:`SchedulerTick` (``app.execution.scheduler``, Worker 4) — one
  synchronous tick creates projects for due schedules; the service runs
  it on a 60 s loop (first tick after 5 s) via ``asyncio.to_thread``
  because the tick is blocking SQLite work.
* the Style Lab analysis claimer
  (:class:`app.style_analysis.claimer.CompetitorAnalysisClaimer`, SL-002) —
  polls for competitor videos queued for analysis
  (``competitor_videos.analysis_status = 'queued'``, see
  ``docs/STYLE_LAB.md`` §7) and runs the analysis engine on them.  The
  service runs its ``poll_once`` on a short loop via
  ``asyncio.to_thread`` (the engine's ffprobe/ffmpeg/Whisper work is
  blocking), exactly like the scheduler loop; the claimer itself
  honours the same persisted ``stop_all`` / ``pause_all`` flags as
  the JobRunner.
* the analytics sync poller
  (:class:`app.analytics_sync.service.AnalyticsSyncPoller`, Round 5)
  — the scheduler hook for per-channel YouTube Analytics ingestion.
  Hosted exactly like the claimer: its ``poll_once`` runs on its own
  loop via ``asyncio.to_thread`` (client HTTP + SQLite work is
  blocking) and the poller itself honours the same persisted
  ``stop_all`` / ``pause_all`` flags and throttles per channel
  (a channel is due only when its last sync attempt is older than
  the poller's minimum interval), so the loop cadence is a poll
  cadence, not a sync cadence.

Degradation contract (mirrors ``app.main``'s defensive-import style):
every sibling module is imported lazily inside try/except.  If a module
has not landed yet the service logs a warning and keeps going with what
it has — no runner -> scheduler-only mode, no scheduler -> runner-only
mode, no registry builder -> empty stage registry (the pipeline control
handlers below still work, so pause/cancel remain functional).  Nothing
in this module may raise at import time.

Pipeline control handlers
-------------------------
``app.routers.projects.pipeline_action`` has always enqueued
``pipeline.{start,pause,resume,cancel}`` jobs; this module supplies the
handlers that make those endpoints actually drive execution.  They are
merged *over* the stage registry in :class:`ExecutionService`, so a
stage handler can never shadow a control kind:

* ``pipeline.start`` — ensure a DAG exists for the project: if no
  stage jobs are queued/leased/running for it, (re)create the DAG via
  ``app.execution.dag.create_project_dag``.  Marks the project
  ``in_production`` when it was still a draft.
* ``pipeline.pause`` — cancel every *queued* stage job for the project
  (running jobs are left to finish; the queue refuses to cancel them).
  The project's ``status`` is deliberately left unchanged — a paused
  project keeps its production status and is resumed explicitly.
  Project-level pause only; the global execution pause flag belongs to
  the runner (Worker 1) and is not touched here.
* ``pipeline.resume`` — if no stage jobs are queued/leased/running for
  the project, restore the chain: cancelled stage jobs (from an
  earlier pause/cancel) are reset to ``queued`` directly — the DAG
  builder's per-stage idempotency keys would otherwise just return
  the cancelled ids — and when no chain exists yet the DAG is created
  via ``create_project_dag`` (its idempotency keys make that safe:
  already-finished stages are never duplicated).
* ``pipeline.cancel`` — cancel every queued (and leased, i.e. claimed
  but not yet running) stage job for the project and set
  ``projects.status = 'cancelled'``.

"Stage jobs" are all jobs for the project whose kind does not start
with ``pipeline.`` (control jobs never cancel each other).

Autostart policy
----------------
``create_app`` attaches the service with ``autostart=False`` by default
(env ``YTA_EXECUTION`` overrides; see ``app.main``), so tests and
embedders get a dormant service they can start manually.
``app.main.run()`` — the real process entry — sets
``YTA_EXECUTION=1`` when unset, so the shipped sidecar executes.
``YTA_EXECUTION_AUTOSTART=0`` is an explicit kill-switch: it forces
``autostart`` off even when the caller asked for it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from typing import Any, Callable, Optional

from app.logging_setup import get_logger

logger = get_logger("app.execution.service")

__all__ = [
    "ExecutionService",
    "build_service",
    "attach",
    "CONTROL_KINDS",
]

#: Job kinds handled by this module (merged over the stage registry).
CONTROL_KINDS = (
    "pipeline.start",
    "pipeline.pause",
    "pipeline.resume",
    "pipeline.cancel",
)

_SCHEDULER_INTERVAL_S = 60.0
_SCHEDULER_INITIAL_DELAY_S = 5.0
_STYLELAB_INTERVAL_S = 15.0
_STYLELAB_INITIAL_DELAY_S = 5.0
_ANALYTICS_INTERVAL_S = 900.0
_ANALYTICS_INITIAL_DELAY_S = 20.0

# Job statuses (section-15 naming; the queue normalises 'done').
_ACTIVE_STATUSES = ("queued", "leased", "running")
_SUCCESS_STATUSES = ("done", "succeeded")


# ---------------------------------------------------------------------------
# Small construction helpers — sibling constructors are other workers'
# code; accept whatever subset of arguments their signature supports.
# ---------------------------------------------------------------------------


def _filter_kwargs(func: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop kwargs ``func`` does not accept (unless it takes **kwargs)."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


def _positional_param_names(func: Any, count: int) -> set:
    """Names of the first ``count`` parameters of ``func`` (for dropping
    kwargs that would duplicate a positionally-bound argument)."""
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return set()
    names = {
        p.name
        for p in params[:count]
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    return names


def _construct(cls: Any, positional: tuple, kwargs: dict[str, Any]) -> Any:
    """Instantiate ``cls`` trying progressively simpler argument sets."""
    bound = _positional_param_names(cls, len(positional))
    clean_kwargs = {k: v for k, v in kwargs.items() if k not in bound}
    attempts: list[tuple[tuple, dict]] = [
        (positional, _filter_kwargs(cls, clean_kwargs)),
        (positional, {}),
    ]
    if positional:
        attempts.append(((), _filter_kwargs(cls, kwargs)))
        attempts.append(((), {}))
    last_exc: Optional[BaseException] = None
    for pos, kw in attempts:
        try:
            return cls(*pos, **kw)
        except TypeError as exc:  # signature mismatch -> try simpler
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def _call(func: Any, positional: tuple, kwargs: dict[str, Any]) -> Any:
    """Call ``func`` with signature-filtered kwargs, falling back to
    positional-only and then to no-arg on TypeError."""
    bound = _positional_param_names(func, len(positional))
    clean_kwargs = {k: v for k, v in kwargs.items() if k not in bound}
    attempts: list[tuple[tuple, dict]] = [
        (positional, _filter_kwargs(func, clean_kwargs)),
        (positional, {}),
    ]
    if positional:
        attempts.append(((), _filter_kwargs(func, kwargs)))
        attempts.append(((), {}))
    last_exc: Optional[BaseException] = None
    for pos, kw in attempts:
        try:
            return func(*pos, **kw)
        except TypeError as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Fallback event emitter (used only when app.execution.events is absent)
# ---------------------------------------------------------------------------


class _HubEmitter:
    """Minimal EventEmitter stand-in wrapping a WSHub (or nothing).

    Supports the shapes sibling code is likely to call — ``emit(...)``,
    ``publish(...)`` and direct call — and never raises: events are
    notifications, losing one must not fail a job.
    """

    def __init__(self, hub: Any = None) -> None:
        self.hub = hub

    def _send(self, event_type: str, payload: Any = None, **scope: Any) -> None:
        hub = self.hub
        if hub is None:
            return
        try:
            publish = getattr(hub, "publish", None)
            if callable(publish):
                publish(
                    event_type,
                    payload if isinstance(payload, dict) else {"value": payload},
                    project_id=scope.get("project_id"),
                    job_id=scope.get("job_id"),
                )
        except Exception:  # noqa: BLE001 — notifications are best-effort
            pass

    def emit(self, event_type: str, payload: Any = None, **scope: Any) -> None:
        self._send(event_type, payload, **scope)

    def publish(self, event_type: str, payload: Any = None, **scope: Any) -> None:
        self._send(event_type, payload, **scope)

    def __call__(self, event_type: str, payload: Any = None, **scope: Any) -> None:
        self._send(event_type, payload, **scope)


def _build_emitter(hub: Any) -> Any:
    """Real EventEmitter when available, hub-wrapping fallback otherwise."""
    try:
        from app.execution.events import EventEmitter
    except Exception as exc:  # module not landed yet
        logger.warning(
            "app.execution.events not available (%s); using fallback emitter", exc
        )
        return _HubEmitter(hub)
    try:
        return _construct(EventEmitter, (hub,), {"hub": hub})
    except Exception as exc:  # noqa: BLE001
        logger.warning("EventEmitter construction failed (%s); fallback", exc)
        return _HubEmitter(hub)


class _EventsFacade:
    """One events object satisfying both sibling conventions.

    ``handlers.build_registry`` expects ``events.publish(type, payload,
    project_id=, job_id=)`` (the WSHub signature), while
    ``gates_bridge`` expects a semantic
    :class:`~app.execution.events.EventEmitter`.  The facade exposes
    ``publish``/``emit`` (hub-style, via the wrapped hub) and delegates
    every other attribute (``gate_pending``, ``phase_changed``, ...) to
    the wrapped EventEmitter, so either consumer works unchanged.
    """

    def __init__(self, emitter: Any, hub: Any = None) -> None:
        self._emitter = emitter
        self._hub = hub if hub is not None else getattr(emitter, "hub", None)

    def publish(
        self,
        event_type: str,
        payload: Any = None,
        *,
        project_id: Any = None,
        job_id: Any = None,
        **scope: Any,
    ) -> Any:
        hub = self._hub
        if hub is not None and callable(getattr(hub, "publish", None)):
            try:
                return hub.publish(
                    event_type,
                    payload if isinstance(payload, dict) else {"value": payload},
                    project_id=project_id,
                    job_id=job_id,
                    **scope,
                )
            except Exception:  # noqa: BLE001 — notifications are best-effort
                return None
        emitter_publish = getattr(self._emitter, "publish", None)
        if callable(emitter_publish):
            try:
                return emitter_publish(
                    event_type, payload, project_id=project_id, job_id=job_id
                )
            except Exception:  # noqa: BLE001
                return None
        return None

    def emit(self, event_type: str, payload: Any = None, **scope: Any) -> Any:
        return self.publish(
            event_type,
            payload,
            project_id=scope.get("project_id"),
            job_id=scope.get("job_id"),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._emitter, name)


def _default_stage_registry(conn_factory: Callable, emitter: Any) -> dict:
    """Stage registry from handlers.build_registry, or {} with a warning."""
    try:
        from app.execution.handlers import build_registry
    except Exception as exc:
        logger.warning(
            "app.execution.handlers not available (%s); "
            "stage registry is empty (control handlers still active)",
            exc,
        )
        return {}
    try:
        registry = _call(
            build_registry,
            (conn_factory,),
            {"conn_factory": conn_factory, "events": emitter, "emitter": emitter},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_registry failed (%s); stage registry empty", exc)
        return {}
    if registry is None:
        return {}
    if isinstance(registry, dict):
        return dict(registry)
    try:
        return dict(registry)  # mapping-like
    except Exception:
        logger.warning(
            "build_registry returned a non-mapping registry (%s); "
            "wrapping it as an opaque registry",
            type(registry).__name__,
        )
        return {"__opaque_registry__": registry}


# ---------------------------------------------------------------------------
# ExecutionService
# ---------------------------------------------------------------------------


class ExecutionService:
    """Owns the JobRunner + scheduler loop lifecycle for one process."""

    def __init__(
        self,
        conn_factory: Callable,
        *,
        hub: Any = None,
        registry: Optional[dict] = None,
        runner_kwargs: Optional[dict[str, Any]] = None,
        scheduler_interval_s: float = _SCHEDULER_INTERVAL_S,
        scheduler_initial_delay_s: float = _SCHEDULER_INITIAL_DELAY_S,
        stylelab_interval_s: float = _STYLELAB_INTERVAL_S,
        stylelab_initial_delay_s: float = _STYLELAB_INITIAL_DELAY_S,
        stylelab_data_dir: Any = None,
        analytics_interval_s: float = _ANALYTICS_INTERVAL_S,
        analytics_initial_delay_s: float = _ANALYTICS_INITIAL_DELAY_S,
        analytics_client: Any = None,
        analytics_client_factory: Any = None,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory
        self.hub = hub
        # The EventEmitter required by the spec, wrapped in a facade so
        # both events conventions (hub-style publish / semantic emitter)
        # work for every consumer.  ``self.emitter`` is what the runner,
        # registry builder and scheduler receive.
        self.event_emitter = _build_emitter(hub)
        self.emitter = _EventsFacade(self.event_emitter, hub)

        # Registry: stage handlers (explicit or built) + control handlers
        # merged OVER them so pipeline.* always resolves here.
        if registry is None:
            stage_registry = _default_stage_registry(conn_factory, self.emitter)
        elif isinstance(registry, dict):
            stage_registry = dict(registry)
        else:
            stage_registry = {"__opaque_registry__": registry}
        self.control_handlers: dict[str, Callable] = self._make_control_handlers()
        merged = dict(stage_registry)
        merged.update(self.control_handlers)
        self.registry: dict = merged

        # Round 4 (Worker 6): terminal publish finalisation for the
        # scheduled path.  The DAG ends at gate.b; wrapping that one
        # handler is what moves an approved project into its (simulated)
        # publish state — see app.execution.publish_finaliser.  The
        # wrap is decision-driven and idempotent, and finaliser failures
        # are reported in the job result instead of failing the gate.
        _gate_b_handler = self.registry.get("gate.b")
        if callable(_gate_b_handler):
            self.registry["gate.b"] = self._wrap_gate_b_handler(_gate_b_handler)

        self._runner_kwargs: dict[str, Any] = dict(runner_kwargs or {})
        self._scheduler_interval_s = float(scheduler_interval_s)
        self._scheduler_initial_delay_s = float(scheduler_initial_delay_s)
        self._stylelab_interval_s = float(stylelab_interval_s)
        self._stylelab_initial_delay_s = float(stylelab_initial_delay_s)
        self._stylelab_data_dir = stylelab_data_dir
        self._analytics_interval_s = float(analytics_interval_s)
        self._analytics_initial_delay_s = float(analytics_initial_delay_s)
        self._analytics_client = analytics_client
        self._analytics_client_factory = analytics_client_factory

        self._queue = self._make_queue()
        self.runner: Any = self._make_runner()
        self._scheduler: Any = self._make_scheduler()
        self.stylelab_claimer: Any = self._make_claimer()
        self.analytics_poller: Any = self._make_analytics_poller()

        self._runner_task: Optional[asyncio.Task] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._claimer_task: Optional[asyncio.Task] = None
        self._analytics_task: Optional[asyncio.Task] = None
        self._running = False

    # -- construction of siblings (all defensive) ---------------------------

    def _make_queue(self) -> Any:
        try:
            from app.queue import SQLiteJobQueue

            return SQLiteJobQueue(self._conn_factory)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLiteJobQueue unavailable: %s", exc)
            return None

    def _make_runner(self) -> Any:
        try:
            from app.execution.runner import JobRunner
        except Exception as exc:
            logger.warning(
                "app.execution.runner not available (%s); "
                "service will run scheduler-only",
                exc,
            )
            return None
        kwargs: dict[str, Any] = {
            "registry": self.registry,
            "events": self.emitter,
            "emitter": self.emitter,
            "queue": self._queue,
            "conn_factory": self._conn_factory,
        }
        kwargs.update(self._runner_kwargs)
        try:
            return _construct(
                JobRunner, (self._conn_factory, self.registry), kwargs
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("JobRunner construction failed: %s", exc)
            return None

    def _make_scheduler(self) -> Any:
        try:
            from app.execution.scheduler import SchedulerTick
        except Exception as exc:
            logger.warning(
                "app.execution.scheduler not available (%s); "
                "scheduler loop will fall back to lease sweeping",
                exc,
            )
            return None
        try:
            return _construct(
                SchedulerTick,
                (self._conn_factory,),
                {
                    "conn_factory": self._conn_factory,
                    "queue": self._queue,
                    "events": self.emitter,
                    "emitter": self.emitter,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SchedulerTick construction failed: %s", exc)
            return None

    def _make_claimer(self) -> Any:
        """The Style Lab analysis claimer (SL-002), or None + warning.

        Defensive like every sibling construction: a missing/broken
        claimer module degrades the service (queued competitor
        analyses simply wait, as they did before SL-002) instead of
        failing ExecutionService construction.
        """
        try:
            from app.style_analysis.claimer import CompetitorAnalysisClaimer
        except Exception as exc:
            logger.warning(
                "app.style_analysis.claimer not available (%s); "
                "queued competitor analyses will not be claimed",
                exc,
            )
            return None
        try:
            return _construct(
                CompetitorAnalysisClaimer,
                (self._conn_factory,),
                {
                    "conn_factory": self._conn_factory,
                    "data_dir": self._stylelab_data_dir,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CompetitorAnalysisClaimer construction failed: %s", exc)
            return None

    def _make_analytics_poller(self) -> Any:
        """The analytics sync poller (Round 5), or None + warning.

        Defensive like every sibling construction: a missing/broken
        analytics module degrades the service (scheduled analytics
        syncs simply do not run; manual triggers still work through
        the router) instead of failing ExecutionService construction.
        With no client/client_factory injected, the poller's service
        resolves the production client per sync
        (``build_default_client`` — honest ``not_connected`` until a
        channel is connected).
        """
        try:
            from app.analytics_sync.service import AnalyticsSyncPoller
        except Exception as exc:
            logger.warning(
                "app.analytics_sync.service not available (%s); "
                "scheduled analytics syncs will not run",
                exc,
            )
            return None
        client_factory = self._analytics_client_factory
        if self._analytics_client is None and client_factory is None:
            try:
                from app.analytics_sync.service import build_default_client

                client_factory = build_default_client
            except Exception:  # noqa: BLE001 — poller records it per sync
                client_factory = None
        try:
            return _construct(
                AnalyticsSyncPoller,
                (self._conn_factory,),
                {
                    "conn_factory": self._conn_factory,
                    "client": self._analytics_client,
                    "client_factory": client_factory,
                    "events": self.emitter,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AnalyticsSyncPoller construction failed: %s", exc)
            return None

    # -- DB helpers (fresh short connections, like app.queue) ---------------

    def _connect(self):
        conn = self._conn_factory()
        try:
            import sqlite3

            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        return conn

    def _project_jobs(self, project_id: str) -> list[dict]:
        """All jobs belonging to a project (column or payload match)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_id = ? "
                "OR payload_json LIKE ?",
                (project_id, f'%"{project_id}"%'),
            ).fetchall()
        finally:
            conn.close()
        jobs: list[dict] = []
        for row in rows:
            job = dict(row)
            if job.get("project_id") != project_id:
                # LIKE is a coarse pre-filter; confirm via the payload.
                payload = job.get("payload_json")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = {}
                if not isinstance(payload, dict) or payload.get("project_id") != project_id:
                    continue
            jobs.append(job)
        return jobs

    def _stage_jobs(self, project_id: str) -> list[dict]:
        return [
            j
            for j in self._project_jobs(project_id)
            if not str(j.get("kind") or "").startswith("pipeline.")
        ]

    def _active_stage_jobs(self, project_id: str) -> list[dict]:
        return [
            j for j in self._stage_jobs(project_id) if j.get("status") in _ACTIVE_STATUSES
        ]

    def _set_project_status(self, project_id: str, status: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now_ms(), project_id),
            )
            try:
                conn.commit()
            except Exception:
                pass
            return (cur.rowcount or 0) > 0
        except Exception:  # project row may not exist (draft kept in-memory)
            return False
        finally:
            conn.close()

    def _project_field(self, project_id: str, field: str) -> Any:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {field} FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return row[0] if row is not None else None
        except Exception:
            return None
        finally:
            conn.close()

    def _requeue_cancelled_stage_jobs(self, project_id: str) -> list[str]:
        """Reset cancelled stage jobs back to 'queued' (direct SQL).

        Needed because the DAG builder's idempotency keys return the
        *existing* (cancelled) job ids on re-creation — re-enqueueing
        alone can never resurrect a paused/cancelled chain.  All
        cancelled stage jobs are reset in one statement so the
        dependency chain stays consistent (the queue's claim pass
        dead-letters queued jobs whose dependency finished badly, so a
        partially-dead chain still resolves correctly).  ``run_after``
        is zeroed so the first reset job is claimable immediately.
        """
        conn = self._connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            run_after_col = (
                "run_after" if "run_after" in cols else
                "run_after_ms" if "run_after_ms" in cols else None
            )
            updated_col = (
                "updated_at" if "updated_at" in cols else
                "updated_at_ms" if "updated_at_ms" in cols else None
            )
            sets = ["status = 'queued'", "lease_owner = NULL"]
            if run_after_col:
                sets.append(f"{run_after_col} = 0")
            if "lease_expires_at" in cols:
                sets.append("lease_expires_at = NULL")
            if "lease_expires_at_ms" in cols:
                sets.append("lease_expires_at_ms = NULL")
            if "error" in cols:
                sets.append("error = NULL")
            if "error_code" in cols:
                sets.append("error_code = NULL")
            if updated_col:
                sets.append(f"{updated_col} = {_now_ms()}")
            ids = [
                j["id"]
                for j in self._stage_jobs(project_id)
                if j.get("status") == "cancelled"
            ]
            if not ids:
                return []
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} "
                f"WHERE id IN ({placeholders}) AND status = 'cancelled'",
                ids,
            )
            try:
                conn.commit()
            except Exception:
                pass
            return ids
        except Exception as exc:  # noqa: BLE001
            logger.warning("requeue of cancelled jobs failed: %s", exc)
            return []
        finally:
            conn.close()

    def _ensure_dag(self, project_id: str) -> dict:
        """Make sure the project has a runnable DAG; return a result dict.

        * stage jobs already queued/leased/running -> nothing to do;
        * every stage job finished successfully -> chain complete;
        * cancelled stage jobs exist (paused/cancelled earlier) ->
          requeue them (idempotent re-creation would only return the
          cancelled ids);
        * otherwise -> create the DAG via ``create_project_dag``.
        """
        stage_jobs = self._stage_jobs(project_id)
        active = [j for j in stage_jobs if j.get("status") in _ACTIVE_STATUSES]
        if active:
            return {"dag": "already_active", "active_jobs": len(active)}
        if stage_jobs and all(
            j.get("status") in _SUCCESS_STATUSES for j in stage_jobs
        ):
            return {"dag": "complete", "jobs": len(stage_jobs)}
        if any(j.get("status") == "cancelled" for j in stage_jobs):
            requeued = self._requeue_cancelled_stage_jobs(project_id)
            if requeued:
                return {"dag": "resumed", "job_ids": requeued}
        try:
            from app.execution.dag import create_project_dag
        except Exception as exc:
            logger.warning("app.execution.dag not available: %s", exc)
            return {"dag": "unavailable"}
        if self._queue is None:
            return {"dag": "unavailable", "reason": "no_queue"}
        try:
            result = _call(
                create_project_dag,
                (self._queue, project_id),
                {"queue": self._queue, "project_id": project_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_project_dag failed for %s: %s", project_id, exc)
            return {"dag": "failed", "error": str(exc)}
        job_ids: list = []
        if isinstance(result, (list, tuple)):
            job_ids = list(result)
        elif isinstance(result, dict):
            for key in ("job_ids", "jobs", "ids"):
                if isinstance(result.get(key), (list, tuple)):
                    job_ids = list(result[key])
                    break
        return {"dag": "created", "job_ids": job_ids}

    def _cancel_stage_jobs(
        self, project_id: str, *, statuses: tuple[str, ...]
    ) -> int:
        if self._queue is None:
            return 0
        cancelled = 0
        for job in self._stage_jobs(project_id):
            if job.get("status") not in statuses:
                continue
            try:
                if self._queue.cancel(job["id"]):
                    cancelled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("cancel of job %s failed: %s", job.get("id"), exc)
        return cancelled

    # -- pipeline control handlers -------------------------------------------

    @staticmethod
    def _extract_project_id(*args: Any, **kwargs: Any) -> Optional[str]:
        """Find project_id in whatever shape the runner passes a handler:
        a job dict, a payload dict, a context object, or kwargs."""
        candidates: list[Any] = list(args) + list(kwargs.values())
        for obj in candidates:
            if isinstance(obj, dict):
                if obj.get("project_id"):
                    return str(obj["project_id"])
                for key in ("payload", "payload_json"):
                    payload = obj.get(key)
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except ValueError:
                            payload = None
                    if isinstance(payload, dict) and payload.get("project_id"):
                        return str(payload["project_id"])
            else:
                for attr in ("project_id",):
                    value = getattr(obj, attr, None)
                    if value:
                        return str(value)
                payload = getattr(obj, "payload", None)
                if isinstance(payload, dict) and payload.get("project_id"):
                    return str(payload["project_id"])
                job = getattr(obj, "job", None)
                if isinstance(job, dict):
                    found = ExecutionService._extract_project_id(job)
                    if found:
                        return found
        return None

    def _budget_pause(self, project_id: str) -> Optional[dict]:
        """Budget enforcement-lite hook (Round 3, budget_gate module).

        Returns the budget_gate result dict when the project's
        estimated total exceeds the persisted per-video cap — the
        project has been parked at Gate A with a ``budget_pending``
        approval and a ``budget.warning`` event — or ``None`` when the
        project may proceed.  Helper errors fail open (logged): an
        established over-cap result is not an exception and is never
        swallowed (see app.execution.budget_gate for full semantics).
        """
        try:
            from app.execution import budget_gate

            return budget_gate.check_budget_for_project(
                self._conn_factory, project_id, events=self.emitter
            )
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning(
                "budget check failed for %s (proceeding): %s", project_id, exc
            )
            return None

    def _release_budget(self, project_id: str) -> None:
        """Release the project's Budget Guard reservation (Round 4).

        Called when a run ends without settling (cancel): the
        unsettled remainder is freed so the daily/monthly budget is
        available to the next attempt.  Best-effort like the budget
        check — a store failure is logged, never raised.
        """
        try:
            from app.execution import budget_gate

            budget_gate.release_budget_for_project(
                self._conn_factory, project_id, reason="pipeline_cancelled"
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "budget reservation release failed for %s: %s", project_id, exc
            )

    # -- publish finalisation (Round 4, Worker 6) ----------------------------

    def _publish_finalise(
        self, project_id: str, decision: str, job_id: Optional[str] = None
    ) -> dict:
        """Run the publish finaliser for an approved Gate B (guarded).

        The finaliser converts its own expected failures (blocked pack
        Gate B, provider errors) into result dicts; anything unexpected
        is logged and reported here rather than raised, so a finaliser
        bug can never fail the already-decided gate job — the outcome
        is visible in the job result and, via the finaliser's blocked
        marking, in the project record on the paths it handles.
        """
        try:
            from app.execution import publish_finaliser

            return publish_finaliser.finalise_after_gate_b(
                self._conn_factory,
                project_id,
                decision=decision,
                events=self.emitter,
                gate_job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 — see docstring
            logger.warning(
                "publish finalisation failed for %s: %s", project_id, exc
            )
            return {"finalised": False, "error": f"{type(exc).__name__}: {exc}"}

    def _publish_note_blocked(self, project_id: str, reason: str) -> None:
        """Record a Gate B non-publication on the project (guarded)."""
        try:
            from app.execution import publish_finaliser

            publish_finaliser.record_non_publication(
                self._conn_factory,
                project_id,
                reason=reason,
                events=self.emitter,
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping only
            logger.warning(
                "publish non-publication note failed for %s: %s",
                project_id,
                exc,
            )

    def _wrap_gate_b_handler(self, handler: Callable) -> Callable:
        """Wrap the registry's ``gate.b`` handler with finalisation.

        * decision ``approved`` / ``auto_approved`` -> finalise
          (exactly once; the outcome is merged into the job result as
          ``publish_finalisation``);
        * any other returned decision -> visible non-publication note;
        * ``GateRejected`` -> non-publication note, exception re-raised
          (the runner's permanent-failure semantics are unchanged);
        * ``GatePending`` (review wait) -> untouched: no finalisation
          and no note — the gate has not completed yet.
        """
        service = self

        async def _handle_gate_b(job: Any) -> Any:
            try:
                outcome = handler(job)
                result = await outcome if inspect.isawaitable(outcome) else outcome
            except Exception as exc:
                if (
                    getattr(exc, "error_code", None) == "gate_rejected"
                    or type(exc).__name__ == "GateRejected"
                ):
                    project_id = service._extract_project_id(job)
                    if project_id:
                        service._publish_note_blocked(
                            project_id, "gate_b_rejected"
                        )
                raise
            if isinstance(result, dict):
                decision = str(result.get("decision") or "").strip().lower()
                project_id = result.get("project_id") or service._extract_project_id(job)
                job_id = job.get("id") if isinstance(job, dict) else None
                if project_id and decision in ("approved", "auto_approved"):
                    finalisation = service._publish_finalise(
                        str(project_id), decision, job_id
                    )
                    return {**result, "publish_finalisation": finalisation}
                if project_id and decision:
                    service._publish_note_blocked(
                        str(project_id), f"gate_b_decision_{decision}"
                    )
            return result

        _handle_gate_b.__name__ = "handle_gate_b_finalising"
        return _handle_gate_b

    def _make_control_handlers(self) -> dict[str, Callable]:
        """Async control handlers matching the registry convention
        (``async handler(job_dict) -> result_dict``, like handlers.py)."""
        service = self

        async def _handle_start(*args: Any, **kwargs: Any) -> dict:
            project_id = service._extract_project_id(*args, **kwargs)
            if not project_id:
                return {"ok": False, "error": "project_id_missing"}
            budget_result = service._budget_pause(project_id)
            if budget_result:
                return {
                    "ok": True,
                    "action": "start",
                    "project_id": project_id,
                    "dag": "budget_pending",
                    "budget_paused": True,
                    **budget_result,
                }
            result = service._ensure_dag(project_id)
            if service._project_field(project_id, "status") == "draft":
                service._set_project_status(project_id, "in_production")
            service._emit("pipeline.started", {"project_id": project_id}, project_id)
            return {"ok": True, "action": "start", "project_id": project_id, **result}

        async def _handle_pause(*args: Any, **kwargs: Any) -> dict:
            project_id = service._extract_project_id(*args, **kwargs)
            if not project_id:
                return {"ok": False, "error": "project_id_missing"}
            # Queued stage jobs only; running jobs finish. Project status
            # intentionally unchanged (see module docstring).
            cancelled = service._cancel_stage_jobs(project_id, statuses=("queued",))
            service._emit("pipeline.paused", {"project_id": project_id}, project_id)
            return {
                "ok": True,
                "action": "pause",
                "project_id": project_id,
                "cancelled": cancelled,
            }

        async def _handle_resume(*args: Any, **kwargs: Any) -> dict:
            project_id = service._extract_project_id(*args, **kwargs)
            if not project_id:
                return {"ok": False, "error": "project_id_missing"}
            # Same budget gate as start: resume re-parks while over cap,
            # and proceeds once the budget approval was approved (the
            # budget_gate bypass) or the cap was raised.
            budget_result = service._budget_pause(project_id)
            if budget_result:
                return {
                    "ok": True,
                    "action": "resume",
                    "project_id": project_id,
                    "dag": "budget_pending",
                    "budget_paused": True,
                    **budget_result,
                }
            result = service._ensure_dag(project_id)
            service._emit("pipeline.resumed", {"project_id": project_id}, project_id)
            return {"ok": True, "action": "resume", "project_id": project_id, **result}

        async def _handle_cancel(*args: Any, **kwargs: Any) -> dict:
            project_id = service._extract_project_id(*args, **kwargs)
            if not project_id:
                return {"ok": False, "error": "project_id_missing"}
            cancelled = service._cancel_stage_jobs(
                project_id, statuses=("queued", "leased")
            )
            service._set_project_status(project_id, "cancelled")
            service._release_budget(project_id)
            service._emit("pipeline.cancelled", {"project_id": project_id}, project_id)
            return {
                "ok": True,
                "action": "cancel",
                "project_id": project_id,
                "cancelled": cancelled,
            }

        return {
            "pipeline.start": _handle_start,
            "pipeline.pause": _handle_pause,
            "pipeline.resume": _handle_resume,
            "pipeline.cancel": _handle_cancel,
        }

    def _emit(self, event_type: str, payload: dict, project_id: Optional[str]) -> None:
        emitter = self.emitter
        if emitter is None:
            return
        try:
            publish = getattr(emitter, "publish", None)
            if callable(publish):
                publish(event_type, payload, project_id=project_id)
            elif callable(emitter):
                emitter(event_type, payload)
        except Exception:  # noqa: BLE001 — notifications are best-effort
            pass

    # -- lifecycle -------------------------------------------------------------

    async def _run_runner(self) -> None:
        runner = self.runner
        if runner is None:
            return
        run = getattr(runner, "run", None)
        if not callable(run):
            logger.warning("JobRunner has no run(); runner task is a no-op")
            return
        try:
            result = _call(run, (), dict(self._runner_kwargs))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("JobRunner.run() exited with error: %s", exc)

    def _tick_once(self) -> Any:
        """One scheduler tick (sync; called via asyncio.to_thread)."""
        scheduler = self._scheduler
        if scheduler is None:
            # Fallback tick: keep crashed workers' jobs recoverable even
            # before the real scheduler lands.
            if self._queue is not None:
                try:
                    return self._queue.sweep_expired_leases()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fallback lease sweep failed: %s", exc)
            return None
        for name in ("tick", "run_once", "run", "__call__"):
            method = getattr(scheduler, name, None)
            if callable(method):
                try:
                    result = _call(method, (), {})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("scheduler %s failed: %s", name, exc)
                    return None
                if inspect.isawaitable(result):
                    # Async tick invoked from a worker thread (no running
                    # loop there): drive it to completion locally.
                    try:
                        return asyncio.run(result)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("async scheduler tick failed: %s", exc)
                        return None
                return result
        logger.warning("SchedulerTick exposes no tick/run_once/run method")
        return None

    async def _scheduler_loop(self) -> None:
        """Scheduler loop: first tick after the initial delay, then every
        interval.  Each tick is sync SQLite work, so it runs in a worker
        thread via ``asyncio.to_thread`` and never blocks the loop."""
        await asyncio.sleep(self._scheduler_initial_delay_s)
        while self._running:
            try:
                await asyncio.to_thread(self._tick_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler tick failed: %s", exc)
            await asyncio.sleep(self._scheduler_interval_s)

    def _claimer_poll_once(self) -> Any:
        """One Style Lab claimer poll (sync; called via asyncio.to_thread).

        The claimer converts per-row failures into reports itself, so
        this wrapper only guards against a poll-level blow-up: it is
        logged, never raised — a broken poll must not kill the loop.
        """
        claimer = self.stylelab_claimer
        if claimer is None:
            return None
        try:
            return claimer.poll_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stylelab claimer poll failed: %s", exc)
            return None

    async def _claimer_loop(self) -> None:
        """Style Lab claimer loop (SL-002): first poll after the initial
        delay, then every interval, mirroring the scheduler loop.  Each
        poll is blocking engine work (ffprobe/ffmpeg/Whisper), so it
        runs in a worker thread via ``asyncio.to_thread``.  The loop
        ends on service stop (task cancellation / ``self._running``
        going False) or when the claimer observes the persisted
        ``stop_all`` flag — the same STOP ALL the JobRunner honours;
        the claimer additionally refuses to claim while ``stop_all``
        or ``pause_all`` is set."""
        await asyncio.sleep(self._stylelab_initial_delay_s)
        while self._running:
            try:
                await asyncio.to_thread(self._claimer_poll_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("stylelab claimer poll failed: %s", exc)
            claimer = self.stylelab_claimer
            if claimer is not None and getattr(claimer, "stop_requested", False):
                logger.info(
                    "stylelab claimer loop exiting: stop_all flag observed"
                )
                break
            await asyncio.sleep(self._stylelab_interval_s)

    def _analytics_poll_once(self) -> Any:
        """One analytics sync poll (sync; called via asyncio.to_thread).

        The poller converts per-channel failures into recorded sync
        runs itself, so this wrapper only guards against a poll-level
        blow-up: it is logged, never raised — a broken poll must not
        kill the loop.
        """
        poller = self.analytics_poller
        if poller is None:
            return None
        try:
            return poller.poll_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics sync poll failed: %s", exc)
            return None

    async def _analytics_loop(self) -> None:
        """Analytics sync loop (Round 5): first poll after the initial
        delay, then every interval, mirroring the claimer loop.  Each
        poll is blocking client/SQLite work, so it runs in a worker
        thread via ``asyncio.to_thread``.  The poller throttles actual
        syncs per channel (minimum interval between attempts) and
        honours the persisted ``stop_all`` / ``pause_all`` flags; the
        loop ends on service stop or when the poller latches
        ``stop_requested`` (STOP ALL observed)."""
        await asyncio.sleep(self._analytics_initial_delay_s)
        while self._running:
            try:
                await asyncio.to_thread(self._analytics_poll_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("analytics sync poll failed: %s", exc)
            poller = self.analytics_poller
            if poller is not None and getattr(poller, "stop_requested", False):
                logger.info(
                    "analytics sync loop exiting: stop_all flag observed"
                )
                break
            await asyncio.sleep(self._analytics_interval_s)

    async def start(self) -> None:
        """Start the runner task + scheduler loop (idempotent).

        The runner's own ``run()`` performs startup lease recovery
        (sweep) before claiming, per the Worker-1 contract.  When the
        runner module is absent the service degrades to scheduler-only
        and logs it — start() still succeeds.
        """
        if self._running:
            return
        self._running = True
        if self.runner is not None:
            self._runner_task = asyncio.create_task(
                self._run_runner(), name="yta-runner"
            )
        else:
            logger.warning(
                "ExecutionService starting scheduler-only (no JobRunner)"
            )
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="yta-scheduler"
        )
        if self.stylelab_claimer is not None:
            self._claimer_task = asyncio.create_task(
                self._claimer_loop(), name="yta-stylelab-claimer"
            )
        if self.analytics_poller is not None:
            self._analytics_task = asyncio.create_task(
                self._analytics_loop(), name="yta-analytics-sync"
            )
        logger.info(
            "ExecutionService started (runner=%s, scheduler=%s, stylelab_claimer=%s, analytics_poller=%s)",
            self.runner is not None,
            self._scheduler is not None,
            self.stylelab_claimer is not None,
            self.analytics_poller is not None,
        )

    async def stop(self) -> None:
        """Stop scheduler + runner and await their shutdown (idempotent)."""
        if (
            not self._running
            and self._runner_task is None
            and self._scheduler_task is None
            and self._claimer_task is None
            and self._analytics_task is None
        ):
            return
        self._running = False

        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._scheduler_task = None

        if self._claimer_task is not None:
            self._claimer_task.cancel()
            try:
                await self._claimer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._claimer_task = None

        if self._analytics_task is not None:
            self._analytics_task.cancel()
            try:
                await self._analytics_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._analytics_task = None

        runner = self.runner
        if runner is not None:
            stop = getattr(runner, "stop", None)
            if callable(stop):
                try:
                    result = _call(stop, (), {})
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.warning("JobRunner.stop() failed: %s", exc)

        if self._runner_task is not None:
            if not self._runner_task.done():
                self._runner_task.cancel()
            try:
                await self._runner_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._runner_task = None
        logger.info("ExecutionService stopped")

    def status(self) -> dict:
        """Liveness snapshot: {running, runner_alive, scheduler_alive}."""
        return {
            "running": bool(self._running),
            "runner_alive": bool(
                self._runner_task is not None and not self._runner_task.done()
            ),
            "scheduler_alive": bool(
                self._scheduler_task is not None and not self._scheduler_task.done()
            ),
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def build_service(conn_factory: Callable, hub: Any = None, **kwargs: Any) -> ExecutionService:
    """Convenience constructor: an ExecutionService over ``conn_factory``."""
    return ExecutionService(conn_factory, hub=hub, **kwargs)


def _env_autostart_kill_switch() -> bool:
    """True when YTA_EXECUTION_AUTOSTART=0 explicitly disables autostart."""
    raw = os.environ.get("YTA_EXECUTION_AUTOSTART")
    return raw is not None and raw.strip().lower() in ("0", "false", "no", "off", "")


def attach(app: Any, *, autostart: bool = False) -> Optional[ExecutionService]:
    """Attach an ExecutionService to ``app.state`` (defensive, never raises).

    * conn_factory is derived from ``app.state.db_path`` via
      ``app.db.get_connection(path)`` (default path when unset).
    * The WS hub is reused from ``app.state.ws_hub``; when unset, the
      ws router's ``get_hub(app)`` is asked for the shared instance
      (it creates + stores one on demand, exactly like a WS request
      would), so service events and sockets share one hub.
    * The service is stored as ``app.state.execution`` (None on total
      failure) and returned.
    * Startup/shutdown handlers are registered ONLY when ``autostart``
      is true (and the YTA_EXECUTION_AUTOSTART=0 kill-switch is not
      set): startup schedules ``service.start()`` as a task stored on
      ``app.state.execution_task``; shutdown awaits ``service.stop()``.
    """
    try:
        from app import db as db_module

        db_path = getattr(app.state, "db_path", None)
        if db_path is not None:
            conn_factory = lambda: db_module.get_connection(db_path)  # noqa: E731
        else:
            conn_factory = lambda: db_module.get_connection()  # noqa: E731
    except Exception as exc:  # noqa: BLE001
        logger.warning("execution attach: no conn_factory available: %s", exc)
        try:
            app.state.execution = None
        except Exception:
            pass
        return None

    hub = getattr(app.state, "ws_hub", None)
    if hub is None:
        try:
            from app.routers.ws import get_hub

            hub = get_hub(app)
        except Exception:
            hub = None

    try:
        service = ExecutionService(conn_factory, hub=hub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("execution attach: service construction failed: %s", exc)
        try:
            app.state.execution = None
        except Exception:
            pass
        return None

    try:
        app.state.execution = service
    except Exception:
        pass

    if autostart and _env_autostart_kill_switch():
        logger.info(
            "execution autostart suppressed by YTA_EXECUTION_AUTOSTART=0"
        )
        autostart = False

    if autostart:

        async def _start_execution() -> None:
            try:
                app.state.execution_task = asyncio.create_task(service.start())
            except Exception as exc:  # noqa: BLE001
                logger.warning("execution autostart failed: %s", exc)

        async def _stop_execution() -> None:
            try:
                await service.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("execution shutdown failed: %s", exc)

        for event, handler in (
            ("startup", _start_execution),
            ("shutdown", _stop_execution),
        ):
            try:
                app.add_event_handler(event, handler)
            except Exception:
                try:
                    hooks = getattr(app.router, f"on_{event}")
                    hooks.append(handler)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "could not register execution %s handler: %s", event, exc
                    )
    return service
