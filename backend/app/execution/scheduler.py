"""Scheduler tick: turn due ``schedules`` rows into projects (Round 2).

The backend owns scheduling (Windows Task Scheduler only wakes the app).
:class:`SchedulerTick` is invoked periodically — the ExecutionService is
expected to call :meth:`SchedulerTick.tick` roughly every 60 s, either
directly (all work here is short, synchronous SQLite I/O) or via a
thread/executor so the event loop is never blocked.  ``tick`` is therefore
a plain synchronous ``def``, *not* a coroutine.

Cadence semantics (``schedules.cadence_json``)
----------------------------------------------
``{"type": "interval", "interval_hours": N}``
    Occurrences are ``anchor + k * N hours``.  :func:`next_occurrence`
    returns ``after_ms + N hours`` (one step forward from ``after_ms``).
``{"type": "daily", "time": "HH:MM"}`` (UTC)
    The next calendar day-boundary occurrence at HH:MM UTC *strictly
    after* ``after_ms`` — an ``after_ms`` exactly on HH:MM rolls to the
    next day.
``{"type": "weekly", "weekday": 0..6, "time": "HH:MM"}`` (UTC, Mon=0)
    The next occurrence on that weekday at HH:MM UTC strictly after
    ``after_ms`` — an exact hit rolls a full week forward.

Unknown types, missing fields, out-of-range values or unparseable
``cadence_json`` yield ``None`` from :func:`next_occurrence` (the schedule
materialises at most its already-due slot and then stops advancing).

Catch-up policy: ``catch_up_once``
-----------------------------------
When a schedule is several slots behind (app was closed, machine asleep),
``tick`` walks *every* missed occurrence from ``next_run_at`` up to
``now`` but creates exactly **one** project — for the *latest* missed
slot.  The earlier slots are collapsed: they are counted in the returned
summary and in the run's ``detail_json`` (``missed_collapsed``) but never
materialised, so a week offline produces one video, not seven.  The
schedule is then advanced to the first occurrence strictly after ``now``
and ``last_run_at`` is set to ``now``.

Topic selection (``schedules.topic_mode``)
------------------------------------------
``queue``
    Highest-priority ``pending`` row of ``topic_queue`` for the schedule's
    channel/niche (a NULL ``channel_id`` counts as channel-agnostic);
    the row is marked ``used`` with ``used_in_project_id`` pointing at the
    new project.  An empty queue fails the slot (see below) rather than
    inventing a topic.
``fixed_list``
    ``cadence_json["topics"]`` is a list; the topic is chosen by rotation:
    ``topics[count(prior *created* schedule_runs) % len(topics)]``.  Only
    runs that actually materialised a project advance the rotation —
    ``failed`` / ``skipped_*`` runs consumed no topic, so counting them
    would shift the rotation by one after any failed slot (bug R2-004).
``auto``
    The research stage generates the topic later.  ``projects.topic`` is
    NOT NULL, so a sentinel ``"auto:<niche_profile_id>"`` is stored; the
    research stage recognises the ``auto:`` prefix and replaces it.
    ``topic_source`` is ``'schedule'`` in all modes.

Failure handling
----------------
A schedule whose channel or niche row is missing (the project INSERT
would fail its FK), an empty topic queue, or an empty fixed list does not
abort the tick: a ``schedule_runs`` row with status ``'failed'`` and a
``reason`` is written, the schedule is advanced exactly as on success
(so a broken schedule cannot spam a failure every 60 s), and the tick
continues with the remaining schedules.  Disabled or not-yet-due
schedules are never selected and are left untouched.

Guardrail (Round 4, D3): the selected topic is screened against the
niche profile's taboo list (``niche_profiles.taboo_json`` — the same
list the scene-safety layer enforces, matched with the pipeline's own
``prompt_matches_taboo``) after topic selection and before the project
INSERT.  A match raises :class:`_GuardrailBlocked`: no project is
created, no queued topic is consumed, and the slot is recorded through
the same failure path with reason ``guardrail_taboo: ...`` and a
``detail_json`` carrying the machine-readable ``reason_code`` +
matched term — a visible refusal, never a silent drop.  The research
stage re-screens fail-closed (see ``app.execution.guardrail_gate``).

After the project + run rows commit, the production DAG is built via
``app.execution.dag.create_project_dag(queue, project_id)`` (lazy import:
the DAG module is owned by another worker).  When it is unavailable — or
raises — the scheduler falls back to enqueueing a single
``pipeline.start`` job for the project, so scheduling works standalone.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

__all__ = ["next_occurrence", "parse_cadence", "SchedulerTick"]

_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 86_400_000
_MS_PER_WEEK = 7 * _MS_PER_DAY

#: Upper bound on the catch-up walk so a pathological cadence (e.g. a
#: one-millisecond interval a year behind) can never spin forever.
_MAX_CATCHUP_STEPS = 10_000

#: Fallback words-per-minute when the niche profile carries none
#: (niche profiles clamp their WPM to 110..160; 150 is the fast end).
_DEFAULT_WPM = 150


# ---------------------------------------------------------------------------
# Cadence parsing
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_hhmm(value: Any) -> Optional[int]:
    """``"HH:MM"`` -> milliseconds after UTC midnight, or None."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return (hh * 3600 + mm * 60) * 1000


def next_occurrence(cadence: dict, after_ms: int) -> Optional[int]:
    """First occurrence of ``cadence`` strictly after ``after_ms``.

    All arithmetic is integer epoch-ms in UTC (the epoch itself is a UTC
    midnight, so day/week boundaries need no calendar library).  Returns
    None for unknown or invalid cadences.
    """
    if not isinstance(cadence, dict):
        return None
    try:
        after = int(after_ms)
    except (TypeError, ValueError):
        return None

    ctype = cadence.get("type")

    if ctype == "interval":
        hours = cadence.get("interval_hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)):
            return None
        if hours <= 0:
            return None
        step = int(hours * _MS_PER_HOUR)
        if step <= 0:
            return None
        return after + step

    if ctype == "daily":
        time_ms = _parse_hhmm(cadence.get("time"))
        if time_ms is None:
            return None
        day_start = (after // _MS_PER_DAY) * _MS_PER_DAY
        candidate = day_start + time_ms
        if candidate <= after:
            candidate += _MS_PER_DAY
        return candidate

    if ctype == "weekly":
        weekday = cadence.get("weekday")
        if not _is_int(weekday) or not (0 <= weekday <= 6):
            return None
        time_ms = _parse_hhmm(cadence.get("time"))
        if time_ms is None:
            return None
        days = after // _MS_PER_DAY
        # Epoch day 0 (1970-01-01) was a Thursday; with Mon=0, Thu=3.
        current_weekday = (days + 3) % 7
        delta_days = (weekday - current_weekday) % 7
        candidate = (days + delta_days) * _MS_PER_DAY + time_ms
        if candidate <= after:
            candidate += _MS_PER_WEEK
        return candidate

    return None


def parse_cadence(raw: Any) -> Optional[dict]:
    """Tolerantly decode a ``cadence_json`` value into a dict or None.

    Accepts an already-decoded dict, a JSON string, or None/garbage
    (invalid JSON, JSON scalars/arrays -> None).
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _ScheduleFailed(Exception):
    """A single schedule slot cannot materialise; recorded, not fatal."""


class _GuardrailBlocked(_ScheduleFailed):
    """The selected topic matches the niche profile's taboo list (D3).

    Raised inside :meth:`SchedulerTick._materialise` *after* topic
    selection and *before* the project INSERT, so the transaction
    rolls back: no project is created and no queued topic is consumed.
    ``tick`` records the slot through the normal failure path — a
    ``schedule_runs`` row with status ``'failed'``, this message as
    ``reason`` and ``detail_json`` carrying the machine-readable
    ``reason_code='guardrail_taboo'`` + matched term — and emits a
    ``guardrail.blocked`` event.  Never a silent drop.
    """

    def __init__(self, topic: str, matched_term: str, niche_id: Any) -> None:
        self.topic = topic
        self.matched_term = matched_term
        self.niche_id = niche_id
        super().__init__(
            "guardrail_taboo: blocked topic refused by guardrail — "
            f"topic {topic!r} matches niche taboo blocked term "
            f"{matched_term!r} (niche_profile_id={niche_id})"
        )


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction (mirrors app.db.transaction semantics).

    If the connection is already inside a transaction, yields without
    nesting — the outer owner keeps commit/rollback responsibility.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    else:
        conn.execute("COMMIT")


def _missed_occurrences(
    cadence: Optional[dict], anchor_ms: int, now_ms: int
) -> list[int]:
    """Every occurrence from ``anchor_ms`` up to ``now_ms``, inclusive.

    ``anchor_ms`` itself is always the first (already-due) slot, even when
    the cadence is invalid — a due schedule materialises its pending slot
    once and then stops (see :func:`_first_after`).
    """
    occurrences = [anchor_ms]
    current = anchor_ms
    for _ in range(_MAX_CATCHUP_STEPS):
        nxt = next_occurrence(cadence, current) if cadence else None
        if nxt is None or nxt <= current or nxt > now_ms:
            break
        occurrences.append(nxt)
        current = nxt
    return occurrences


def _first_after(
    cadence: Optional[dict], latest_ms: int, now_ms: int
) -> Optional[int]:
    """First occurrence strictly after ``now_ms`` (None = never again)."""
    if not cadence:
        return None
    nxt = next_occurrence(cadence, latest_ms)
    steps = 0
    while nxt is not None and nxt <= now_ms and steps < _MAX_CATCHUP_STEPS:
        following = next_occurrence(cadence, nxt)
        if following is None or following <= nxt:
            return None
        nxt = following
        steps += 1
    return nxt


# ---------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------

class SchedulerTick:
    """Materialises due schedules into projects, one tick at a time.

    ``conn_factory()`` must return a fresh ``sqlite3.Connection`` (e.g.
    ``functools.partial(app.db.get_connection, path)``).  ``queue`` is a
    ``SQLiteJobQueue`` over the same factory (one is built lazily when
    omitted).  ``events`` is an optional hub with a
    ``publish(type, payload, project_id=...)`` method (``app.ws_hub.WSHub``);
    a ``schedule.materialised`` event is emitted per created project and
    publishing is fully guarded — a broken hub never breaks scheduling.
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        queue: Any = None,
        events: Any = None,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory
        self._queue = queue
        self._events = events

    # -- plumbing ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = self._conn_factory()
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        return conn

    def _get_queue(self):
        if self._queue is None:
            from app.queue import SQLiteJobQueue

            self._queue = SQLiteJobQueue(self._conn_factory)
        return self._queue

    # -- main entry point ----------------------------------------------

    def tick(self, now_ms: Optional[int] = None) -> dict:
        """Run one scheduler tick (synchronous; see module docstring).

        Returns ``{"checked": int, "materialised": int, "collapsed": int,
        "failed": int}`` — due schedules examined, projects created,
        missed slots collapsed by catch_up_once, and slots that failed.
        """
        now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        summary = {"checked": 0, "materialised": 0, "collapsed": 0, "failed": 0}

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM schedules "
                "WHERE is_enabled = 1 AND deleted_at IS NULL "
                "AND next_run_at IS NOT NULL AND next_run_at <= ? "
                "ORDER BY next_run_at ASC, id ASC",
                (now,),
            ).fetchall()
            due = [dict(r) for r in rows]

            for schedule in due:
                summary["checked"] += 1
                try:
                    info = self._materialise(conn, schedule, now)
                except _GuardrailBlocked as exc:
                    summary["failed"] += 1
                    self._record_failure(
                        conn,
                        schedule,
                        now,
                        str(exc),
                        detail_extra={
                            "reason_code": "guardrail_taboo",
                            "guardrail": {
                                "matched_taboo_term": exc.matched_term,
                                "topic": exc.topic,
                                "niche_profile_id": exc.niche_id,
                            },
                        },
                    )
                    self._emit_guardrail_event(schedule, exc)
                    continue
                except _ScheduleFailed as exc:
                    summary["failed"] += 1
                    self._record_failure(conn, schedule, now, str(exc))
                    continue
                except Exception as exc:  # one bad schedule must not kill the tick
                    summary["failed"] += 1
                    self._record_failure(
                        conn, schedule, now, f"{type(exc).__name__}: {exc}"
                    )
                    continue
                summary["materialised"] += 1
                summary["collapsed"] += info["missed_collapsed"]
                self._build_dag(info["project_id"])
                self._emit_event(info)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return summary

    # -- per-schedule materialisation -----------------------------------

    def _materialise(
        self, conn: sqlite3.Connection, schedule: dict, now: int
    ) -> dict:
        schedule_id = schedule["id"]
        channel_id = schedule["channel_id"]
        niche_id = schedule["niche_profile_id"]
        cadence = parse_cadence(schedule.get("cadence_json"))
        anchor = int(schedule["next_run_at"])

        occurrences = _missed_occurrences(cadence, anchor, now)
        slot_at = occurrences[-1]
        missed_collapsed = len(occurrences) - 1
        next_run_at = _first_after(cadence, slot_at, now)

        project_id = str(uuid.uuid4())

        with _tx(conn):
            # FK pre-check: a missing parent would fail the project INSERT
            # anyway; checking first yields a precise failure reason.
            if conn.execute(
                "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
            ).fetchone() is None:
                raise _ScheduleFailed(f"channel not found: {channel_id}")
            niche_row = conn.execute(
                "SELECT * FROM niche_profiles WHERE id = ?", (niche_id,)
            ).fetchone()
            if niche_row is None:
                raise _ScheduleFailed(f"niche profile not found: {niche_id}")
            niche = dict(niche_row)

            topic, queued_topic_id = self._select_topic(
                conn, schedule, cadence, niche
            )

            # Guardrail (D3): screen the selected topic against the
            # niche profile's taboo list BEFORE the project is
            # created.  The matcher is the pipeline's own
            # prompt_matches_taboo, resolved via guardrail_gate the
            # same way the scene-safety layer resolves its profile.
            # The 'auto:' sentinel is not a topic and is not screened
            # here — research screens its resolved topic instead.
            if not topic.startswith("auto:"):
                from app.execution.guardrail_gate import match_taboo_term

                matched_term = match_taboo_term(topic, niche, niche_id=niche_id)
                if matched_term is not None:
                    raise _GuardrailBlocked(topic, matched_term, niche_id)

            target_minutes = schedule.get("target_minutes")
            target_words = self._target_words(niche, target_minutes)

            conn.execute(
                "INSERT INTO projects ("
                "id, channel_id, niche_profile_id, topic, topic_source, "
                "target_minutes, target_words, status, current_phase, "
                "publish_mode, schedule_id, total_cost_micros, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'schedule', ?, ?, 'in_production', "
                "'research', ?, ?, 0, ?, ?)",
                (
                    project_id,
                    channel_id,
                    niche_id,
                    topic,
                    target_minutes,
                    target_words,
                    schedule.get("publish_mode") or "review_first",
                    schedule_id,
                    now,
                    now,
                ),
            )

            if queued_topic_id is not None:
                conn.execute(
                    "UPDATE topic_queue SET status = 'used', "
                    "used_in_project_id = ? WHERE id = ?",
                    (project_id, queued_topic_id),
                )

            detail = {"missed_collapsed": missed_collapsed, "slot_at": slot_at}
            conn.execute(
                "INSERT INTO schedule_runs ("
                "id, schedule_id, project_id, status, reason, detail_json, "
                "started_at, finished_at, created_at"
                ") VALUES (?, ?, ?, 'created', NULL, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    schedule_id,
                    project_id,
                    json.dumps(detail),
                    now,
                    now,
                    now,
                ),
            )

            conn.execute(
                "UPDATE schedules SET next_run_at = ?, last_run_at = ?, "
                "updated_at = ? WHERE id = ?",
                (next_run_at, now, now, schedule_id),
            )

        return {
            "schedule_id": schedule_id,
            "project_id": project_id,
            "slot_at": slot_at,
            "missed_collapsed": missed_collapsed,
            "next_run_at": next_run_at,
        }

    def _select_topic(
        self,
        conn: sqlite3.Connection,
        schedule: dict,
        cadence: Optional[dict],
        niche: dict,
    ) -> tuple[str, Optional[str]]:
        """Return ``(topic, topic_queue_row_id_or_None)`` for this slot."""
        mode = schedule.get("topic_mode") or "auto"
        niche_id = schedule["niche_profile_id"]

        if mode == "queue":
            row = conn.execute(
                "SELECT * FROM topic_queue "
                "WHERE niche_profile_id = ? AND status = 'pending' "
                "AND (channel_id IS NULL OR channel_id = ?) "
                "ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                (niche_id, schedule["channel_id"]),
            ).fetchone()
            if row is None:
                raise _ScheduleFailed(
                    "topic_queue empty for channel/niche "
                    f"{schedule['channel_id']}/{niche_id}"
                )
            topic_row = dict(row)
            return topic_row["topic"], topic_row["id"]

        if mode == "fixed_list":
            topics = cadence.get("topics") if cadence else None
            if not isinstance(topics, list):
                topics = []
            topics = [t for t in topics if isinstance(t, str) and t.strip()]
            if not topics:
                raise _ScheduleFailed(
                    "fixed_list mode but cadence_json carries no topics"
                )
            # Count only 'created' runs (R2-004): a 'failed' run — written
            # by _record_failure for a broken slot — produced no project
            # and consumed no topic.  The old COUNT(*) over all runs made
            # the rotation index depend on failure history, so one failed
            # tick shifted every later topic by one (the observed
            # ["Second topic", "First topic"] swap).  Counting created
            # runs makes the index exactly "topics used so far".
            runs_count = conn.execute(
                "SELECT COUNT(*) FROM schedule_runs "
                "WHERE schedule_id = ? AND status = 'created'",
                (schedule["id"],),
            ).fetchone()[0]
            return topics[int(runs_count) % len(topics)], None

        # 'auto' (and any unexpected value): the research stage generates
        # the real topic later; store the documented sentinel (projects
        # .topic is NOT NULL, so "" is not an option).
        return f"auto:{niche_id}", None

    @staticmethod
    def _target_words(niche: dict, target_minutes: Any) -> Optional[int]:
        if target_minutes is None:
            return None
        try:
            minutes = int(target_minutes)
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        wpm = niche.get("words_per_minute") or _DEFAULT_WPM
        try:
            wpm = int(wpm)
        except (TypeError, ValueError):
            wpm = _DEFAULT_WPM
        return minutes * wpm

    def _record_failure(
        self,
        conn: sqlite3.Connection,
        schedule: dict,
        now: int,
        reason: str,
        detail_extra: Optional[dict] = None,
    ) -> None:
        """Write a 'failed' schedule_runs row and advance the schedule.

        Best-effort: a failure to record the failure must never mask the
        original error or abort the tick.  Advancing ``next_run_at`` (same
        computation as the success path) keeps a broken schedule from
        failing again on every 60 s tick.  ``detail_extra`` is merged
        into ``detail_json`` (the guardrail path uses it for its
        machine-readable ``reason_code`` + matched term).
        """
        try:
            cadence = parse_cadence(schedule.get("cadence_json"))
            anchor = schedule.get("next_run_at")
            if anchor is not None:
                occurrences = _missed_occurrences(cadence, int(anchor), now)
                next_run_at = _first_after(cadence, occurrences[-1], now)
            else:
                next_run_at = None
            detail = {"error": reason}
            if detail_extra:
                detail.update(detail_extra)
            with _tx(conn):
                conn.execute(
                    "INSERT INTO schedule_runs ("
                    "id, schedule_id, project_id, status, reason, "
                    "detail_json, started_at, finished_at, created_at"
                    ") VALUES (?, ?, NULL, 'failed', ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        schedule["id"],
                        reason,
                        json.dumps(detail),
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE schedules SET next_run_at = ?, last_run_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (next_run_at, now, now, schedule["id"]),
                )
        except Exception:
            pass

    # -- DAG / events ----------------------------------------------------

    def _build_dag(self, project_id: str) -> list:
        """Build the production DAG, falling back to one pipeline.start job.

        The DAG module belongs to another worker and is lazy-imported; if
        it is missing or raises, a single ``pipeline.start`` job keeps the
        scheduler fully functional standalone.
        """
        try:
            queue = self._get_queue()
        except Exception:
            return []
        create_project_dag = None
        try:
            from app.execution.dag import create_project_dag as _dag_fn

            create_project_dag = _dag_fn
        except Exception:
            create_project_dag = None
        if create_project_dag is not None:
            try:
                result = create_project_dag(queue, project_id)
                return list(result) if result is not None else []
            except Exception:
                pass  # fall through to the standalone fallback
        try:
            job_id = queue.enqueue(
                "pipeline.start",
                {"project_id": project_id},
                project_id=project_id,
                idempotency_key=f"pipeline.start:{project_id}",
            )
            return [job_id]
        except Exception:
            return []

    def _emit_guardrail_event(self, schedule: dict, exc: _GuardrailBlocked) -> None:
        """Emit ``guardrail.blocked`` for a taboo-refused slot; guarded."""
        if self._events is None:
            return
        payload = {
            "schedule_id": schedule["id"],
            "reason_code": "guardrail_taboo",
            "topic": exc.topic,
            "matched_taboo_term": exc.matched_term,
            "niche_profile_id": exc.niche_id,
        }
        try:
            self._events.publish("guardrail.blocked", payload)
        except Exception:
            pass

    def _emit_event(self, info: dict) -> None:
        if self._events is None:
            return
        payload = {
            "schedule_id": info["schedule_id"],
            "project_id": info["project_id"],
            "slot_at": info["slot_at"],
            "missed_collapsed": info["missed_collapsed"],
        }
        try:
            self._events.publish(
                "schedule.materialised", payload, project_id=info["project_id"]
            )
        except TypeError:
            try:
                self._events.publish("schedule.materialised", payload)
            except Exception:
                pass
        except Exception:
            pass
