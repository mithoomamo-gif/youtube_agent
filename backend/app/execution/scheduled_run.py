"""One-shot scheduled run — the Task Scheduler wake path (Round 6).

Windows Task Scheduler launches the installed app executable with
``--scheduled-run`` (see ``packaging/register_scheduler.ps1``).  The
desktop shell forwards the flag to this sidecar entry point
(``app.main.run`` dispatches here before any server startup), and this
module performs exactly one scheduler pass and exits:

1. Read the persisted execution flags (``app.execution.flags``).
2. List the schedules that are due *now* (read-only).
3. Unless blocked or in dry-run mode, run one real
   :meth:`app.execution.scheduler.SchedulerTick.tick` — the same tick
   the resident ExecutionService runs every 60 s and the same tick the
   Schedules UI "Run now" action runs — so a headless wake materialises
   due schedules with identical ``catch_up_once`` semantics (several
   missed slots collapse into exactly one project, for the latest slot)
   and enqueues their production DAGs in the job queue.
4. Print one JSON report on stdout and exit 0.

No HTTP server is started, no window/session is involved, and no launch
token is required: this is local SQLite work against the app's own
database, exactly like the resident scheduler loop.

STOP ALL / pause semantics
--------------------------
The resident ``SchedulerTick`` is not itself flag-gated — in the
long-running app the flags gate the *JobRunner* (``pause_all`` stops it
claiming jobs, ``stop_all`` cancels in-flight work and ends its loop),
while the Style Lab claimer and the analytics poller refuse to start
new work while either flag is set.  A one-shot headless run has no
resident runner to defer execution to, so it honours the flags the
claimer/poller way: when ``stop_all`` **or** ``pause_all`` is set, the
run is *blocked* — nothing is listed as executed, no tick runs, no
project is materialised — and the report says so
(``status="blocked"``, ``blocked_by=...``).  Blocked is an operator's
deliberate state, not a failure: the exit code is still 0.

Dry-run
-------
``--scheduled-run --dry-run`` (or ``dry_run=True``) lists what *would*
run — every due schedule with its slot/collapse arithmetic — and
changes nothing: no tick, no ``schedule_runs`` rows, no advanced
``next_run_at``.  ``--dry-run`` without ``--scheduled-run`` is a usage
error (exit code 64, EX_USAGE).

Production-job execution
------------------------
Like the UI's "Run now", "running a schedule" here means the tick:
materialise + enqueue.  Executing the enqueued DAG jobs remains the
ExecutionService/JobRunner's job — after this one-shot exits, the
shell's supervisor spawns the normal server sidecar (window still
hidden), whose runner executes them.  This module never claims jobs
itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable, Optional, Sequence

from app.logging_setup import get_logger

logger = get_logger("app.execution.scheduled_run")

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "build_parser",
    "list_due_schedules",
    "run_scheduled_once",
    "main",
]

#: Ran (or was deliberately blocked / dry-run listed) without error.
EXIT_OK = 0
#: Bad flag combination (sysexits.h EX_USAGE) — e.g. --dry-run alone.
EXIT_USAGE = 64


def _now_ms_default() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Due-schedule listing (read-only; selection mirrors SchedulerTick.tick)
# ---------------------------------------------------------------------------


def list_due_schedules(
    conn_factory: Callable, now_ms: Optional[int] = None
) -> list[dict]:
    """Return the schedules due at ``now_ms`` (default: wall clock).

    The selection is exactly the one :meth:`SchedulerTick.tick` uses —
    enabled, not soft-deleted, ``next_run_at`` set and ``<= now``,
    oldest-due first.  Each entry additionally carries the catch-up
    arithmetic the tick *would* apply (``missed_slots`` including the
    pending slot, ``missed_collapsed`` slots that would be collapsed,
    ``slot_at_ms`` the latest missed slot that would materialise), so a
    dry-run listing shows precisely what a real run would do.

    Read-only: this function issues SELECTs only.
    """
    # _missed_occurrences is SchedulerTick's own catch-up walk; reusing
    # it (rather than re-implementing the loop) guarantees the listing
    # can never drift from the tick's catch_up_once semantics.
    from app.execution.scheduler import _missed_occurrences, parse_cadence

    now = int(now_ms) if now_ms is not None else _now_ms_default()
    conn = conn_factory()
    try:
        try:
            import sqlite3

            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        rows = conn.execute(
            "SELECT id, name, channel_id, niche_profile_id, cadence_json, "
            "topic_mode, target_minutes, publish_mode, next_run_at, "
            "last_run_at FROM schedules "
            "WHERE is_enabled = 1 AND deleted_at IS NULL "
            "AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "ORDER BY next_run_at ASC, id ASC",
            (now,),
        ).fetchall()
        due: list[dict] = []
        for row in rows:
            entry = dict(row)
            anchor = int(entry["next_run_at"])
            cadence = parse_cadence(entry.get("cadence_json"))
            occurrences = _missed_occurrences(cadence, anchor, now)
            entry["cadence"] = cadence
            entry["due_since_ms"] = anchor
            entry["overdue_ms"] = now - anchor
            entry["missed_slots"] = len(occurrences)
            entry["missed_collapsed"] = len(occurrences) - 1
            entry["slot_at_ms"] = occurrences[-1]
            due.append(entry)
        return due
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The one-shot run
# ---------------------------------------------------------------------------


def _default_tick(conn_factory: Callable) -> Any:
    """The production tick: a SchedulerTick over ``conn_factory``.

    Its job queue is built lazily by the tick itself over the same
    factory, exactly as in ``ExecutionService``'s scheduler-only
    fallback, so DAG creation / the ``pipeline.start`` fallback behave
    identically to the resident path.
    """
    from app.execution.scheduler import SchedulerTick

    return SchedulerTick(conn_factory)


def run_scheduled_once(
    conn_factory: Callable,
    *,
    now_ms: Optional[int] = None,
    dry_run: bool = False,
    tick_factory: Optional[Callable[[Callable], Any]] = None,
) -> dict:
    """Run (or, with ``dry_run``, only describe) one scheduled pass.

    Parameters
    ----------
    conn_factory:
        Zero-arg callable returning a fresh ``sqlite3.Connection``
        (e.g. ``functools.partial(app.db.get_connection, path)``).
    now_ms:
        The "now" for due-selection and the tick; wall clock when None.
        Injectable for deterministic tests.
    dry_run:
        List due schedules and change nothing.
    tick_factory:
        ``tick_factory(conn_factory) -> object with .tick(now_ms=...)``.
        Injectable so tests can mock the runner; defaults to a real
        :class:`SchedulerTick`.

    Returns the JSON-serialisable report dict that :func:`main` prints.
    Flag-blocked and dry-run reports carry ``summary=None`` and a
    ``status`` of ``"blocked"`` / ``"dry_run"``; a real run carries the
    tick's own summary (``checked`` / ``materialised`` / ``collapsed``
    / ``failed``) and ``status="completed"``.
    """
    from app.execution.flags import read_flags

    now = int(now_ms) if now_ms is not None else _now_ms_default()
    flags = read_flags(conn_factory)
    due = list_due_schedules(conn_factory, now_ms=now)

    report: dict[str, Any] = {
        "mode": "scheduled_run",
        "dry_run": bool(dry_run),
        "now_ms": now,
        "flags": {"pause_all": flags.pause_all, "stop_all": flags.stop_all},
        "due_count": len(due),
        "due": due,
        "summary": None,
    }

    # STOP ALL / pause: honoured up-front (see module docstring).  A
    # blocked wake is still a successful invocation — exit 0 — because
    # the operator asked for exactly this state.
    if flags.stop_all or flags.pause_all:
        report["status"] = "blocked"
        report["blocked_by"] = "stop_all" if flags.stop_all else "pause_all"
        logger.info(
            "scheduled run blocked by %s flag; %d schedule(s) left due",
            report["blocked_by"],
            len(due),
        )
        return report

    if dry_run:
        report["status"] = "dry_run"
        logger.info("scheduled run dry-run: %d schedule(s) due", len(due))
        return report

    factory = tick_factory if tick_factory is not None else _default_tick
    tick = factory(conn_factory)
    summary = tick.tick(now_ms=now)
    report["status"] = "completed"
    report["summary"] = summary
    logger.info("scheduled run completed: %s", summary)
    return report


# ---------------------------------------------------------------------------
# CLI entry (also the dispatch target for app.main's --scheduled-run)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-agent-backend --scheduled-run",
        description=(
            "One-shot scheduled run: materialise every due schedule "
            "once (catch_up_once) and exit. Invoked by Windows Task "
            "Scheduler via the app executable; see "
            "packaging/register_scheduler.ps1."
        ),
    )
    parser.add_argument(
        "--scheduled-run",
        action="store_true",
        help="Perform the one-shot scheduled run (required).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the due schedules that would run; change nothing.",
    )
    return parser


def _resolve_conn_factory(db_path: Any = None) -> Callable:
    """conn_factory for the app's real database.

    ``db_path`` wins when given (tests / tooling); otherwise the launch
    environment resolves it via ``app.config`` *without* requiring the
    launch token (a headless run starts no server), falling back to
    ``app.db.default_db_path()`` if the environment is malformed.  The
    schema is initialised idempotently first, exactly as the normal
    app boot does, so a first-ever wake on a fresh install works.
    """
    from app import db as db_module

    if db_path is None:
        try:
            from app.config import load_settings

            db_path = load_settings(require_token=False).db_path
        except Exception as exc:  # noqa: BLE001 — fall back, never crash a wake
            logger.warning(
                "scheduled run: settings unusable (%s); using default DB path",
                exc,
            )
            db_path = db_module.default_db_path()
    db_module.init_db(db_path).close()
    return lambda: db_module.get_connection(db_path)  # noqa: E731


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    conn_factory: Optional[Callable] = None,
    db_path: Any = None,
    now_ms: Optional[int] = None,
    tick_factory: Optional[Callable[[Callable], Any]] = None,
) -> int:
    """CLI entry point. Prints the JSON report; returns the exit code.

    ``argv`` includes the flags themselves (e.g. ``["--scheduled-run"]``
    or ``["--scheduled-run", "--dry-run"]``) — ``app.main.run`` passes
    ``sys.argv[1:]`` straight through.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.dry_run and not args.scheduled_run:
        print(
            "error: --dry-run is only meaningful together with --scheduled-run",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not args.scheduled_run:
        print("error: --scheduled-run is required", file=sys.stderr)
        return EXIT_USAGE

    if conn_factory is None:
        conn_factory = _resolve_conn_factory(db_path)

    report = run_scheduled_once(
        conn_factory,
        now_ms=now_ms,
        dry_run=bool(args.dry_run),
        tick_factory=tick_factory,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
