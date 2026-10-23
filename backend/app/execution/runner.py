"""JobRunner: lease-based execution loop over :class:`SQLiteJobQueue`.

The runner claims queued jobs whose kind has a registered handler *and*
whose concurrency pool still has a free slot, starts them, and babysits
each one to a terminal queue transition:

* handler returns        -> ``queue.complete(result)``
* handler raises
  :class:`GatePending`   -> job requeued for a later poll **without**
  consuming an attempt (a gate wait is not a failure — see
  :meth:`JobRunner._requeue_for_gate` for why this is a direct row update)
* handler raises
  :class:`JobCancelled`  -> ``queue.cancel`` (with a direct-update fallback
  for jobs already in ``running``, which ``cancel()`` does not cover)
* any other exception    -> ``queue.fail`` under the failure policy
  (:mod:`app.execution.failure_policy`): DAG jobs retry exactly once,
  a second failure dead-letters the job and parks the project as
  ``failed`` (error_summary persisted, ``project.failed`` emitted
  persisted + live); a gate rejection is never retried
* task cancelled (stop) -> job cancelled, cancellation re-raised

While a handler runs, a heartbeat task extends its lease every
``lease_seconds / 3`` so long jobs are never swept as crashed.

Control surface: the ``settings``-table flags (see
:mod:`app.execution.flags`) are re-read every poll — ``pause_all`` stops
claiming, ``stop_all`` cancels in-flight work and ends the loop.  The
:meth:`pause` / :meth:`resume` / :meth:`stop_all` methods set the internal
flag *and* persist it via :func:`app.execution.flags.set_flag`.

All queue/DB calls are synchronous SQLite operations on short-lived
connections (the queue's own design); they are fast local calls and keep
the runner deterministic.  Event emission is fully guarded: a broken
events hub can never kill the runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import time
import uuid
from typing import Any, Callable, Optional

from app.queue import SQLiteJobQueue

from . import failure_policy
from .flags import Flags, read_flags, set_flag
from .pools import PoolManager, pool_for_kind

__all__ = ["GatePending", "JobCancelled", "JobRunner"]


class GatePending(Exception):
    """Raised by a handler when its job must wait for a human gate.

    The runner requeues the job (delayed by ``gate_requeue_delay_ms``)
    without consuming an attempt and without recording a failure.
    """


class JobCancelled(Exception):
    """Raised by a handler to cancel its own job (terminal, no retry)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _table_columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _pick(columns: set, *names: str) -> Optional[str]:
    for name in names:
        if name in columns:
            return name
    return None


class JobRunner:
    """Claim-and-execute loop for registered job kinds."""

    def __init__(
        self,
        conn_factory,
        registry: dict[str, Callable],
        *,
        owner: Optional[str] = None,
        pools: Optional[PoolManager] = None,
        events=None,
        poll_interval: float = 0.05,
        lease_seconds: int = 60,
        gate_requeue_delay_ms: int = 15_000,
        error_backoff_base_ms: int = 1000,
        stop_timeout: float = 10.0,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self.conn_factory = conn_factory
        self.registry: dict[str, Callable] = dict(registry or {})
        self.owner = owner or f"runner-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.pools = pools if pools is not None else PoolManager()
        self.events = events
        self.poll_interval = float(poll_interval)
        self.lease_seconds = lease_seconds
        self.gate_requeue_delay_ms = int(gate_requeue_delay_ms)
        self.error_backoff_base_ms = int(error_backoff_base_ms)
        self.stop_timeout = float(stop_timeout)

        self.queue = SQLiteJobQueue(conn_factory)

        self._inflight: dict[str, asyncio.Task] = {}
        self._inflight_jobs: dict[str, dict] = {}
        self._paused = False
        self._stop_requested = False
        self._stop_all_requested = False
        self._running = False

    # ------------------------------------------------------------------
    # introspection helpers (used by tests / UI)
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    # ------------------------------------------------------------------
    # control methods (sync: set internal flag + persist)
    # ------------------------------------------------------------------
    def pause(self) -> None:
        """Stop claiming new jobs (in-flight jobs run to completion)."""
        self._paused = True
        self._persist_flag("pause_all", True)

    def resume(self) -> None:
        """Resume claiming jobs."""
        self._paused = False
        self._persist_flag("pause_all", False)

    def stop_all(self) -> None:
        """Persist the stop flag; the loop cancels in-flight work and exits."""
        self._stop_all_requested = True
        self._persist_flag("stop_all", True)

    def _persist_flag(self, name: str, value: bool) -> None:
        try:
            set_flag(self.conn_factory, name, value)
        except Exception:
            pass  # the internal flag still applies to this runner

    def _read_flags(self) -> Flags:
        try:
            return read_flags(self.conn_factory)
        except Exception:
            return Flags()

    def _release_budget_on_permanent_failure(self, project_id: Any) -> None:
        """Free the project's Budget Guard reservation on permanent failure.

        Mirrors the ``pipeline.cancel`` release in
        :mod:`app.execution.service` (``_release_budget``): same
        delegate (:func:`app.execution.budget_gate.release_budget_for_project`),
        same conn_factory/project arguments, with ``reason='run_failed'``
        naming this end path.  The store only releases an *active*
        reservation, so calling this twice is a safe no-op — the
        settled slice (real spend) stays committed and only the
        unsettled remainder is freed.  Best-effort and guarded like
        parking: a broken budget store must never kill the runner.
        Deliberately NOT called from the pause paths (runner
        :meth:`pause`, ``GatePending`` requeue, ``pipeline.pause``):
        a paused run keeps its reservation by design.
        """
        if not project_id:
            return
        try:
            from . import budget_gate

            budget_gate.release_budget_for_project(
                self.conn_factory, project_id, reason="run_failed"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Crash-recover, then claim/execute until stopped.

        Returns when :meth:`stop` is called or the persisted ``stop_all``
        flag is observed (in-flight tasks are cancelled in the latter
        case).
        """
        self._running = True
        # FIRST: crash recovery — requeue/dead-letter jobs whose worker
        # died holding a lease, so they become claimable again below.
        # The failure-policy wrapper also parks any project whose DAG
        # job died in the sweep (or was left dead-but-unparked by a
        # crash), so a restart can neither lose a failure silently nor
        # reset a job's persisted retry budget.
        recovered: Any = None
        try:
            recovered = failure_policy.recover_and_park(
                self.conn_factory, self.queue, events=self.events
            )
        except Exception:
            try:
                self.queue.sweep_expired_leases()
            except Exception:
                pass
        # Projects the recovery sweep just parked as permanently
        # failed free their budget reservations immediately too —
        # the same terminal transition as the dead-letter path in
        # _run_job, reached without a handler ever running.
        for parked_id in (recovered or {}).get("parked") or []:
            self._release_budget_on_permanent_failure(parked_id)
        try:
            while not self._stop_requested:
                flags = self._read_flags()
                if flags.stop_all or self._stop_all_requested:
                    await self._cancel_all_inflight()
                    break
                self._reap()
                if flags.pause_all or self._paused:
                    await asyncio.sleep(self.poll_interval)
                    continue

                kinds = self._available_kinds()
                if not kinds:
                    await asyncio.sleep(self.poll_interval)
                    continue
                try:
                    job = self.queue.claim(
                        self.owner,
                        queues=("default",),
                        kinds=kinds,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    await asyncio.sleep(self.poll_interval)
                    continue
                if job is None:
                    await asyncio.sleep(self.poll_interval)
                    continue

                job_id = job["id"]
                try:
                    self.queue.start(job_id, self.owner)
                except Exception:
                    pass
                self._inflight_jobs[job_id] = job
                await self._emit(
                    "agent.status_changed",
                    {
                        "job_id": job_id,
                        "kind": job.get("kind"),
                        "status": "running",
                        "project_id": job.get("project_id"),
                    },
                    project_id=job.get("project_id"),
                    job_id=job_id,
                )
                self._inflight[job_id] = asyncio.create_task(self._run_job(job))
                # Loop immediately: claim more jobs while pools have room.
        finally:
            self._running = False

    async def stop(self, graceful: bool = True, timeout: Optional[float] = None) -> None:
        """Stop claiming; optionally let in-flight jobs finish.

        With ``graceful=True`` in-flight tasks are awaited up to
        ``timeout`` seconds (default ``self.stop_timeout``) before being
        cancelled; with ``graceful=False`` they are cancelled at once.
        """
        self._stop_requested = True
        tasks = [t for t in self._inflight.values() if not t.done()]
        if not tasks:
            return
        if graceful:
            wait = self.stop_timeout if timeout is None else float(timeout)
            await asyncio.wait(tasks, timeout=wait)
            tasks = [t for t in tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reap()

    # ------------------------------------------------------------------
    # claiming helpers
    # ------------------------------------------------------------------
    def _pool_inflight_count(self, pool: str) -> int:
        return sum(
            1
            for job in self._inflight_jobs.values()
            if pool_for_kind(str(job.get("kind") or "")) == pool
        )

    def _pool_has_capacity(self, pool: str) -> bool:
        # A claimed-but-not-yet-started job will hold a slot imminently, so
        # count in-flight jobs as well as slots already acquired; the
        # semaphore inside _run_job remains the hard guarantee.
        used = max(self.pools.in_use(pool), self._pool_inflight_count(pool))
        return used < self.pools.limit(pool)

    def _available_kinds(self) -> list[str]:
        return [
            kind
            for kind in self.registry
            if self._pool_has_capacity(pool_for_kind(kind))
        ]

    def _reap(self) -> None:
        for job_id, task in list(self._inflight.items()):
            if task.done():
                self._inflight.pop(job_id, None)
                self._inflight_jobs.pop(job_id, None)
                if not task.cancelled():
                    # Retrieve (and drop) any stray exception so asyncio
                    # never logs "exception was never retrieved".  _run_job
                    # handles all expected outcomes itself.
                    task.exception()

    async def _cancel_all_inflight(self) -> None:
        tasks = [t for t in self._inflight.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reap()

    # ------------------------------------------------------------------
    # per-job execution
    # ------------------------------------------------------------------
    async def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        kind = job.get("kind")
        project_id = job.get("project_id")
        pool = pool_for_kind(str(kind or ""))
        handler = self.registry.get(kind)  # type: ignore[arg-type]
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id))
        try:
            if handler is None:
                raise LookupError(f"no handler registered for kind {kind!r}")
            async with self.pools.acquire(pool):
                outcome = handler(job)
                result = await outcome if inspect.isawaitable(outcome) else outcome
        except GatePending:
            # Not a failure: park the job for a later poll, attempt refunded.
            self._requeue_for_gate(job_id)
            await self._emit(
                "agent.status_changed",
                {
                    "job_id": job_id,
                    "kind": kind,
                    "status": "queued",
                    "project_id": project_id,
                },
                project_id=project_id,
                job_id=job_id,
            )
        except JobCancelled:
            self._cancel_job(job_id)
            await self._emit(
                "agent.status_changed",
                {
                    "job_id": job_id,
                    "kind": kind,
                    "status": "cancelled",
                    "project_id": project_id,
                },
                project_id=project_id,
                job_id=job_id,
            )
        except asyncio.CancelledError:
            # stop_all / stop(graceful=False): mark the job cancelled so it
            # is not left leased/running, then let the cancellation fly.
            self._cancel_job(job_id)
            raise
        except Exception as exc:  # handler failure -> failure policy
            # Failure policy (app.execution.failure_policy): DAG jobs
            # get exactly ONE automatic retry (persisted attempts cap
            # the retry even for legacy rows with max_attempts=3);
            # a gate rejection is permanent (no retry) — the gates
            # bridge has already parked that project as 'blocked', so
            # it is not re-parked as 'failed' below.
            permanent = failure_policy.is_permanent_failure(exc)
            new_status: Optional[str] = None
            try:
                new_status = self.queue.fail(
                    job_id,
                    self.owner,
                    getattr(exc, "error_code", None) or type(exc).__name__,
                    str(exc) or type(exc).__name__,
                    retryable=failure_policy.retryable_for(job, exc),
                    backoff_base_ms=self.error_backoff_base_ms,
                )
            except Exception:
                pass
            await self._emit(
                "agent.status_changed",
                {
                    "job_id": job_id,
                    "kind": kind,
                    "status": new_status or "failed",
                    "project_id": project_id,
                },
                project_id=project_id,
                job_id=job_id,
            )
            if new_status == "dead" and not permanent and project_id:
                # Second failure (or non-retryable death): park the
                # project terminally — status/error_summary persisted,
                # downstream chain dead-lettered, project.failed
                # emitted persisted + live.  Guarded: parking must
                # never kill the runner loop.
                try:
                    dead_job = self.queue.get(job_id) or job
                except Exception:
                    dead_job = job
                try:
                    failure_policy.park_project_failed(
                        self.conn_factory,
                        project_id=project_id,
                        job=dead_job,
                        error_code=getattr(exc, "error_code", None)
                        or type(exc).__name__,
                        error_message=str(exc) or type(exc).__name__,
                        events=self.events,
                    )
                except Exception:
                    pass
                # Terminal failure frees the budget reservation
                # immediately (not at Pacific-day rollover): the
                # unsettled remainder returns to the daily/monthly
                # buckets for the next attempt.  Transient retries
                # never reach here — queue.fail only returns 'dead'
                # once the retry budget is spent — and gate parks /
                # pauses take their own branches above, so neither
                # releases.
                self._release_budget_on_permanent_failure(project_id)
        else:
            try:
                self.queue.complete(job_id, self.owner, result)
            except Exception:
                pass
            status = "succeeded"
            try:
                after = self.queue.get(job_id)
                if after is not None and after.get("status"):
                    status = str(after["status"])
            except Exception:
                pass
            await self._emit(
                "agent.status_changed",
                {
                    "job_id": job_id,
                    "kind": kind,
                    "status": status,
                    "project_id": project_id,
                },
                project_id=project_id,
                job_id=job_id,
            )
            await self._emit(
                "pipeline.phase_changed",
                {"project_id": project_id, "phase": kind},
                project_id=project_id,
                job_id=job_id,
            )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, job_id: str) -> None:
        """Extend the job's lease every ``lease_seconds / 3`` until done."""
        interval = max(float(self.lease_seconds) / 3.0, 0.01)
        while True:
            await asyncio.sleep(interval)
            try:
                self.queue.heartbeat(job_id, self.owner, self.lease_seconds)
            except Exception:
                pass  # a missed heartbeat is retried next tick

    # ------------------------------------------------------------------
    # direct row updates (queue has no primitive for these two cases)
    # ------------------------------------------------------------------
    def _requeue_for_gate(self, job_id: str) -> bool:
        """Requeue a gate-blocked job WITHOUT consuming an attempt.

        Why direct SQL: the queue's public API cannot express this.
        ``queue.cancel()`` is terminal (status ``cancelled`` — the job
        would never run again) and ``queue.fail()`` records an error and
        consumes the attempt that ``claim()`` already incremented.  A gate
        wait is neither, so this helper flips the row back to ``queued``
        itself: lease cleared, ``run_after`` pushed out by
        ``gate_requeue_delay_ms`` (the gate poll delay, 15 s by default),
        and ``attempts`` decremented to refund the claim.  Column names are
        introspected because the physical schema differs between the
        section-15 names (``run_after``) and the ``*_ms`` spellings.
        """
        conn = None
        try:
            conn = self.conn_factory()
            columns = _table_columns(conn, "jobs")
            if not columns:
                return False
            run_after = _pick(columns, "run_after", "run_after_ms")
            lease_expires = _pick(
                columns, "lease_expires_at", "lease_expires_at_ms"
            )
            heartbeat = _pick(columns, "heartbeat_at", "heartbeat_at_ms")
            updated = _pick(columns, "updated_at", "updated_at_ms")
            now = _now_ms()
            sets = ["status = 'queued'", "lease_owner = NULL"]
            params: list = []
            if lease_expires:
                sets.append(f"{lease_expires} = NULL")
            if heartbeat:
                sets.append(f"{heartbeat} = NULL")
            if "attempts" in columns:
                sets.append("attempts = MAX(attempts - 1, 0)")
            if run_after:
                sets.append(f"{run_after} = ?")
                params.append(now + self.gate_requeue_delay_ms)
            if updated:
                sets.append(f"{updated} = ?")
                params.append(now)
            params.append(job_id)
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND status IN ('leased', 'running')",
                params,
            )
            try:
                conn.commit()
            except Exception:
                pass
            return (cur.rowcount or 0) > 0
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _cancel_job(self, job_id: str) -> bool:
        """Cancel a job, including one already in ``running`` state.

        ``queue.cancel()`` only covers ``queued``/``leased`` rows (per its
        contract), but the runner starts jobs before executing them, so a
        running job needs the direct-update fallback below.  Terminal jobs
        are untouched (the status guard makes this idempotent).
        """
        try:
            if self.queue.cancel(job_id):
                return True
        except Exception:
            pass
        conn = None
        try:
            conn = self.conn_factory()
            columns = _table_columns(conn, "jobs")
            if not columns:
                return False
            lease_expires = _pick(
                columns, "lease_expires_at", "lease_expires_at_ms"
            )
            updated = _pick(columns, "updated_at", "updated_at_ms")
            sets = ["status = 'cancelled'", "lease_owner = NULL"]
            params: list = []
            if lease_expires:
                sets.append(f"{lease_expires} = NULL")
            if updated:
                sets.append(f"{updated} = ?")
                params.append(_now_ms())
            params.append(job_id)
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND status IN ('leased', 'running')",
                params,
            )
            try:
                conn.commit()
            except Exception:
                pass
            return (cur.rowcount or 0) > 0
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # events (guarded: a broken hub never kills the runner)
    # ------------------------------------------------------------------
    async def _emit(
        self,
        event_type: str,
        payload: dict,
        *,
        project_id: Any = None,
        job_id: Any = None,
    ) -> None:
        if self.events is None:
            return
        try:
            result = self.events.publish(
                event_type, payload, project_id=project_id, job_id=job_id
            )
        except TypeError:
            # A minimal hub that only accepts (type, payload).
            try:
                result = self.events.publish(event_type, payload)
            except Exception:
                return
        except Exception:
            return
        try:
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
