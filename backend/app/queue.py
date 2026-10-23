"""SQLite-backed job queue (MASTER_ROADMAP Part 9.1 / section 08 + 15).

A single-file, no-Redis job queue built on the ``jobs`` table.  Design goals
(section 08 reliability): at-least-once delivery with leases + heartbeats,
effectively-once handlers via a UNIQUE ``idempotency_key``, exponential
retry backoff, a dead-letter state, DAG dependencies, and a crash-recovery
lease sweep.  Claims are atomic: one ``BEGIN IMMEDIATE`` transaction with an
``UPDATE ... RETURNING`` (section 15, query Q1).

Schema adaptation
-----------------
Section 15 names some columns/statuses slightly differently from the plain
task spec, so this module introspects the live ``jobs`` table once per
operation (``PRAGMA table_info`` + the CREATE TABLE sql) and adapts:

* dependency column: ``depends_on_job_id`` (s15) or ``depends_on``
* run-after column:  ``run_after`` (s15) or ``run_after_ms``
* lease expiry:      ``lease_expires_at`` (s15) or ``lease_expires_at_ms``
* heartbeat:         ``heartbeat_at`` or ``heartbeat_at_ms`` (optional)
* timestamps:        ``created_at``/``updated_at`` (s15) or ``*_ms``
* error storage:     single ``error`` column (s15) or split
  ``error_code`` + ``error_message``
* success status:    ``done`` (s15) or ``succeeded`` — detected from the
  table's CHECK/sql; both values count as "dependency satisfied".
* ``queue`` / ``agent_key`` columns are used when present, skipped when not.

Returned job dicts are normalised: regardless of the physical schema they
always carry ``depends_on``, ``run_after_ms``, ``lease_expires_at_ms``,
``heartbeat_at_ms``, ``created_at_ms``, ``updated_at_ms``, ``error_code``,
``error_message``, plus ``payload``/``result`` (and ``payload_json`` /
``result_json``) with the JSON already parsed.

Thread-safety: every operation opens a short connection via the supplied
``conn_factory`` (``app.db.connect``) and closes it; no connection is ever
shared between threads or kept between calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

__all__ = ["SQLiteJobQueue"]

#: Statuses that mean "this job finished successfully" (either naming).
SUCCESS_STATUSES = ("done", "succeeded")
#: Dependency statuses that doom a waiting dependent (section 08: a pipeline
#: may pause, never wait forever on a dead blocker).
TERMINAL_BAD_STATUSES = ("failed", "dead", "cancelled")
#: All statuses this queue writes.
ALL_STATUSES = (
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "dead",
    "cancelled",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_json(value):
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


class _Schema:
    """Physical layout of the live ``jobs`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        info = conn.execute("PRAGMA table_info(jobs)").fetchall()
        if not info:
            raise RuntimeError("jobs table does not exist (run app.db.init_db first)")
        # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
        self.columns = {row[1] for row in info}
        self.notnull = {row[1] for row in info if row[3]}

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        table_sql = (row[0] or "").lower() if row else ""

        self.success_status = (
            "succeeded" if "succeeded" in table_sql else
            "done" if "done" in table_sql else
            "succeeded"
        )

        self.queue = "queue" if "queue" in self.columns else None
        self.depends = self._pick("depends_on_job_id", "depends_on")
        self.run_after = self._pick("run_after_ms", "run_after")
        self.lease_expires = self._pick(
            "lease_expires_at_ms", "lease_expires_at"
        )
        self.heartbeat = self._pick("heartbeat_at_ms", "heartbeat_at")
        self.created = self._pick("created_at_ms", "created_at")
        self.updated = self._pick("updated_at_ms", "updated_at")
        self.has_error_split = (
            "error_code" in self.columns and "error_message" in self.columns
        )
        self.has_error_single = "error" in self.columns

    def _pick(self, *names):
        for name in names:
            if name in self.columns:
                return name
        return None

    # -- SQL fragments -----------------------------------------------------
    def error_set_sql(self) -> str:
        parts = []
        if self.has_error_split:
            parts.append("error_code = ?, error_message = ?")
        if self.has_error_single:
            parts.append("error = ?")
        return ", ".join(parts)

    def error_params(self, code, message) -> list:
        params = []
        if self.has_error_split:
            params.extend([code, message])
        if self.has_error_single:
            params.append(f"{code}: {message}" if code else message)
        return params

    def clear_error_sql(self) -> str:
        parts = []
        if self.has_error_split:
            parts.append("error_code = NULL, error_message = NULL")
        if self.has_error_single:
            parts.append("error = NULL")
        return ", ".join(parts)


class SQLiteJobQueue:
    """Lease-based job queue over the SQLite ``jobs`` table."""

    def __init__(self, conn_factory):
        """``conn_factory()`` must return a fresh ``sqlite3.Connection``
        (e.g. ``functools.partial(app.db.connect, path)``)."""
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory

    # ------------------------------------------------------------------
    # connection / transaction plumbing
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = self._conn_factory()
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        try:
            # Autocommit mode: transactions are managed explicitly with
            # BEGIN IMMEDIATE / COMMIT so claim stays atomic.
            conn.isolation_level = None
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
        except Exception:
            pass
        return conn

    @contextmanager
    def _tx(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # row -> public job dict
    # ------------------------------------------------------------------
    def _to_job(self, row, schema: _Schema) -> dict:
        job = dict(row)

        payload = _parse_json(job.get("payload_json"))
        result = _parse_json(job.get("result_json"))
        job["payload"] = payload
        job["payload_json"] = payload
        job["result"] = result
        job["result_json"] = result

        # Normalised aliases (stable regardless of physical column names).
        if schema.depends:
            job["depends_on"] = job.get(schema.depends)
        job.setdefault("depends_on", None)
        if schema.run_after:
            job["run_after_ms"] = job.get(schema.run_after)
        job.setdefault("run_after_ms", None)
        if schema.lease_expires:
            job["lease_expires_at_ms"] = job.get(schema.lease_expires)
        job.setdefault("lease_expires_at_ms", None)
        if schema.heartbeat:
            job["heartbeat_at_ms"] = job.get(schema.heartbeat)
        job.setdefault("heartbeat_at_ms", None)
        if schema.created:
            job["created_at_ms"] = job.get(schema.created)
        job.setdefault("created_at_ms", None)
        if schema.updated:
            job["updated_at_ms"] = job.get(schema.updated)
        job.setdefault("updated_at_ms", None)
        if schema.queue:
            job["queue"] = job.get(schema.queue)
        job.setdefault("queue", "default")

        # Normalised error fields.
        if not schema.has_error_split:
            code, message = None, job.get("error_message")
            raw = job.get("error")
            if isinstance(raw, str) and raw:
                if ": " in raw:
                    code, message = raw.split(": ", 1)
                else:
                    message = raw
            job["error_code"] = code
            job["error_message"] = message
        return job

    def _dependency_doomed_sql(self, schema: _Schema) -> str | None:
        """UPDATE fragment marking queued jobs dead when their dependency
        finished badly.  Returns None when there is no dependency column."""
        if not schema.depends:
            return None
        bad = ", ".join(f"'{s}'" for s in TERMINAL_BAD_STATUSES)
        sets = [f"status = 'dead'", f"{schema.updated} = ?"]
        err_sql = schema.error_set_sql()
        if err_sql:
            sets.append(err_sql)
        sets.append("lease_owner = NULL")
        if schema.lease_expires:
            sets.append(f"{schema.lease_expires} = NULL")
        return (
            f"UPDATE jobs SET {', '.join(sets)} "
            f"WHERE status = 'queued' AND {schema.depends} IN "
            f"(SELECT id FROM jobs WHERE status IN ({bad}))"
        )

    def _propagate_dependency_failures(self, conn, schema: _Schema, now: int):
        sql = self._dependency_doomed_sql(schema)
        if sql is None:
            return
        params = [now]
        params.extend(schema.error_params("dependency_failed",
                                           "dependency job failed or died"))
        conn.execute(sql, params)

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------
    def enqueue(
        self,
        kind,
        payload: dict,
        *,
        queue: str = "default",
        priority: int = 0,
        depends_on=None,
        run_after_ms=None,
        idempotency_key=None,
        max_attempts: int = 3,
        project_id=None,
    ) -> str:
        """Insert a queued job and return its id.

        Idempotency: if ``idempotency_key`` already exists, the EXISTING
        job's id is returned and no duplicate row is created.
        """
        conn = self._connect()
        try:
            schema = _Schema(conn)
            now = _now_ms()

            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return existing[0]

            job_id = str(uuid.uuid4())
            values: dict = {
                "id": job_id,
                "kind": kind,
                "status": "queued",
                "priority": priority,
                "attempts": 0,
                "max_attempts": max_attempts,
                "idempotency_key": idempotency_key,
                "payload_json": json.dumps(payload or {}),
                "result_json": None,
                "lease_owner": None,
                "project_id": project_id,
            }
            if schema.queue:
                values[schema.queue] = queue
            if "agent_key" in schema.columns:
                # Section-15 tables carry agent_key; the job kind is the
                # closest durable value until an agent claims the job.
                values["agent_key"] = kind
            if schema.depends:
                values[schema.depends] = depends_on
            if schema.run_after:
                # 0 = claimable immediately, whatever clock claim() uses.
                values[schema.run_after] = (
                    int(run_after_ms) if run_after_ms is not None else 0
                )
            if schema.lease_expires:
                values[schema.lease_expires] = None
            if schema.heartbeat:
                values[schema.heartbeat] = None
            if schema.has_error_split:
                values["error_code"] = None
                values["error_message"] = None
            if schema.has_error_single:
                values["error"] = None
            if schema.created:
                values[schema.created] = now
            if schema.updated:
                values[schema.updated] = now

            cols = [c for c in values if c in schema.columns]
            sql = (
                f"INSERT INTO jobs ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})"
            )
            try:
                with self._tx(conn):
                    conn.execute(sql, [values[c] for c in cols])
            except sqlite3.IntegrityError:
                # Lost a race on the UNIQUE idempotency key: return the
                # row that won instead of duplicating the job.
                if idempotency_key is not None:
                    existing = conn.execute(
                        "SELECT id FROM jobs WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        return existing[0]
                raise
            return job_id
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------
    def claim(
        self,
        owner: str,
        *,
        queues=("default",),
        kinds=None,
        lease_seconds: int = 60,
        now_ms=None,
    ) -> dict | None:
        """Atomically lease the highest-priority claimable job.

        Claimable = status 'queued', run_after reached, dependency (if any)
        finished successfully.  Queued jobs whose dependency failed / died /
        was cancelled are first dead-lettered with
        ``error_code='dependency_failed'``.  Returns the job dict or None.
        """
        now = int(now_ms) if now_ms is not None else _now_ms()
        lease_expires = now + int(lease_seconds) * 1000
        conn = self._connect()
        try:
            schema = _Schema(conn)
            with self._tx(conn):
                self._propagate_dependency_failures(conn, schema, now)

                where = ["status = 'queued'"]
                params: list = []
                if schema.run_after:
                    where.append(f"({schema.run_after} IS NULL OR {schema.run_after} <= ?)")
                    params.append(now)
                if schema.queue and queues:
                    where.append(
                        f"{schema.queue} IN ({', '.join('?' for _ in queues)})"
                    )
                    params.extend(queues)
                if kinds:
                    where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
                    params.extend(kinds)
                if schema.depends:
                    ok = ", ".join(f"'{s}'" for s in SUCCESS_STATUSES)
                    where.append(
                        f"({schema.depends} IS NULL OR EXISTS ("
                        f"SELECT 1 FROM jobs dep WHERE dep.id = jobs.{schema.depends} "
                        f"AND dep.status IN ({ok})))"
                    )
                created_order = schema.created or "rowid"
                candidate = conn.execute(
                    f"SELECT id FROM jobs WHERE {' AND '.join(where)} "
                    f"ORDER BY priority DESC, {created_order} ASC, rowid ASC "
                    f"LIMIT 1",
                    params,
                ).fetchone()
                if candidate is None:
                    return None

                sets = [
                    "status = 'leased'",
                    "lease_owner = ?",
                    "attempts = attempts + 1",
                    f"{schema.updated} = ?",
                ]
                upd_params: list = [owner, now]
                if schema.lease_expires:
                    sets.append(f"{schema.lease_expires} = ?")
                    upd_params.append(lease_expires)
                if schema.heartbeat:
                    sets.append(f"{schema.heartbeat} = ?")
                    upd_params.append(now)
                upd_params.append(candidate[0])
                row = conn.execute(
                    f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? RETURNING *",
                    upd_params,
                ).fetchone()
                if row is None:  # pragma: no cover - cannot happen under BEGIN IMMEDIATE
                    return None
                return self._to_job(row, schema)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # lease lifecycle
    # ------------------------------------------------------------------
    def heartbeat(self, job_id, owner, lease_seconds: int = 60, now_ms=None) -> bool:
        """Extend the lease; only the lease owner of a leased/running job
        may heartbeat.  Returns True when the lease was extended."""
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            sets = [f"{schema.updated} = ?"]
            params: list = [now]
            if schema.lease_expires:
                sets.append(f"{schema.lease_expires} = ?")
                params.append(now + int(lease_seconds) * 1000)
            if schema.heartbeat:
                sets.append(f"{schema.heartbeat} = ?")
                params.append(now)
            params.extend([job_id, owner])
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND lease_owner = ? AND status IN ('leased', 'running')",
                params,
            )
            return cur.rowcount > 0
        finally:
            conn.close()

    def start(self, job_id, owner, now_ms=None) -> bool:
        """Transition leased -> running (owner must hold the lease)."""
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            sets = ["status = 'running'", f"{schema.updated} = ?"]
            params: list = [now]
            if schema.heartbeat:
                sets.append(f"{schema.heartbeat} = ?")
                params.append(now)
            params.extend([job_id, owner])
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND lease_owner = ? AND status = 'leased'",
                params,
            )
            return cur.rowcount > 0
        finally:
            conn.close()

    def complete(self, job_id, owner, result: dict | None = None, now_ms=None) -> bool:
        """Mark a leased/running job succeeded and store its result."""
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            sets = [
                "status = ?",
                "result_json = ?",
                "lease_owner = NULL",
                f"{schema.updated} = ?",
            ]
            params: list = [
                schema.success_status,
                json.dumps(result) if result is not None else None,
                now,
            ]
            if schema.lease_expires:
                sets.append(f"{schema.lease_expires} = NULL")
            clear_err = schema.clear_error_sql()
            if clear_err:
                sets.append(clear_err)
            params.extend([job_id, owner])
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND lease_owner = ? AND status IN ('leased', 'running')",
                params,
            )
            return cur.rowcount > 0
        finally:
            conn.close()

    def fail(
        self,
        job_id,
        owner,
        error_code,
        error_message,
        *,
        retryable: bool = True,
        backoff_base_ms: int = 1000,
        now_ms=None,
    ) -> str | None:
        """Fail a leased/running job.

        Retryable with attempts left -> requeued with exponential backoff
        ``base * 2^(attempts-1)`` plus a small deterministic jitter derived
        from the job id hash.  Otherwise -> dead-letter ('dead').  Returns
        the new status, or None when the job/owner did not match.
        """
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            with self._tx(conn):
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    return None
                job = dict(row)
                if job.get("lease_owner") != owner or job.get("status") not in (
                    "leased",
                    "running",
                ):
                    return None

                attempts = int(job.get("attempts") or 0)
                max_attempts = int(job.get("max_attempts") or 3)
                sets = [f"{schema.updated} = ?", "lease_owner = NULL"]
                params: list = [now]
                if schema.lease_expires:
                    sets.append(f"{schema.lease_expires} = NULL")
                err_sql = schema.error_set_sql()
                if err_sql:
                    sets.append(err_sql)
                    params.extend(schema.error_params(error_code, error_message))

                if retryable and attempts < max_attempts:
                    backoff = int(backoff_base_ms) * (2 ** max(attempts - 1, 0))
                    jitter = (
                        int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16)
                        % max(int(backoff_base_ms), 1)
                    )
                    sets.append("status = 'queued'")
                    if schema.run_after:
                        sets.append(f"{schema.run_after} = ?")
                        params.append(now + backoff + jitter)
                    new_status = "queued"
                else:
                    sets.append("status = 'dead'")
                    new_status = "dead"

                params.append(job_id)
                conn.execute(
                    f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params
                )
                if new_status == "dead":
                    self._propagate_dependency_failures(conn, schema, now)
                return new_status
        finally:
            conn.close()

    def cancel(self, job_id, now_ms=None) -> bool:
        """Cancel a queued or leased job.  Returns True when cancelled."""
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            sets = ["status = 'cancelled'", "lease_owner = NULL",
                    f"{schema.updated} = ?"]
            if schema.lease_expires:
                sets.append(f"{schema.lease_expires} = NULL")
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? "
                f"AND status IN ('queued', 'leased')",
                [now, job_id],
            )
            return cur.rowcount > 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # crash recovery
    # ------------------------------------------------------------------
    def sweep_expired_leases(self, now_ms=None) -> int:
        """Requeue or dead-letter jobs whose lease has expired.

        Leased/running jobs with ``lease_expires < now`` go back to
        'queued' while attempts remain, else to 'dead' with
        ``error_code='lease_expired'``.  Returns the number of jobs swept.
        """
        now = int(now_ms) if now_ms is not None else _now_ms()
        conn = self._connect()
        try:
            schema = _Schema(conn)
            if not schema.lease_expires:
                return 0
            with self._tx(conn):
                base_sets = ["lease_owner = NULL",
                             f"{schema.lease_expires} = NULL",
                             f"{schema.updated} = ?"]
                # Requeue branch.
                requeue_sets = ["status = 'queued'"] + base_sets
                requeue_params: list = [now]
                if schema.run_after:
                    requeue_sets.append(f"{schema.run_after} = ?")
                    requeue_params.append(now)
                requeue_params.append(now)
                cur1 = conn.execute(
                    f"UPDATE jobs SET {', '.join(requeue_sets)} "
                    f"WHERE status IN ('leased', 'running') "
                    f"AND {schema.lease_expires} IS NOT NULL "
                    f"AND {schema.lease_expires} < ? "
                    f"AND attempts < max_attempts",
                    requeue_params,
                )
                # Dead-letter branch.
                dead_sets = ["status = 'dead'"] + base_sets
                dead_params: list = [now]
                err_sql = schema.error_set_sql()
                if err_sql:
                    dead_sets.append(err_sql)
                    dead_params.extend(schema.error_params(
                        "lease_expired",
                        "lease expired and attempts exhausted (worker presumed dead)",
                    ))
                if schema.run_after:
                    # run_after is meaningless for a dead job; keep ordering
                    # of params aligned with the SET clause.
                    pass
                dead_params.append(now)
                cur2 = conn.execute(
                    f"UPDATE jobs SET {', '.join(dead_sets)} "
                    f"WHERE status IN ('leased', 'running') "
                    f"AND {schema.lease_expires} IS NOT NULL "
                    f"AND {schema.lease_expires} < ? "
                    f"AND attempts >= max_attempts",
                    dead_params,
                )
                count = (cur1.rowcount or 0) + (cur2.rowcount or 0)
                if count:
                    self._propagate_dependency_failures(conn, schema, now)
                return count
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get(self, job_id) -> dict | None:
        conn = self._connect()
        try:
            schema = _Schema(conn)
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._to_job(row, schema) if row is not None else None
        finally:
            conn.close()

    def stats(self) -> dict:
        """Counts of jobs by status (only statuses present are returned)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            conn.close()
