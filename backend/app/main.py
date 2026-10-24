"""FastAPI application factory + process entry point for the sidecar.

Startup invariants (roadmap §31):

* Bind 127.0.0.1 ONLY — verified before serving; a non-loopback bind
  target is a fatal config error (exit code 2, do not auto-restart).
* Missing/invalid launch environment (no ``YTA_TOKEN``) -> exit code 5.
* Port bind failure -> exit code 3 (shell retries with a fresh port).
* Database schema newer than this build -> exit code 4 (downgrade
  protection; the shell routes to the updater, never auto-restarts).
* Clean shutdown -> exit code 0. Any other unhandled fatal propagates
  (Python exits non-zero, conventionally 1) = "crash" in the roadmap's
  restart policy — deliberately NOT remapped onto 2/3/4/5.
* On startup the process prints exactly one structured stdout line::

      {"event": "listening", "port": <port>, "boot_id": "<boot_id>"}

  Everything else on stdout is human log text, never protocol.

Other workers' modules (errors, middleware, routers) are imported
defensively: if one has not landed yet the app still boots and a warning
is logged, so parallel teams never block each other.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import sys
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings, load_settings
from app.logging_setup import configure_logging, get_logger

logger = get_logger("app.main")

HOST = "127.0.0.1"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

EXIT_OK = 0
EXIT_BIND_SCOPE = 2
EXIT_BIND_FAILURE = 3
EXIT_SCHEMA_TOO_NEW = 4
EXIT_BAD_ENV = 5


def _env_flag(name: str, default: bool = False) -> bool:
    """Boolean env flag: 1/true/yes/on -> True, 0/false/no/off/'' -> False,
    anything else (or unset) -> ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    return default


def _include_router(app: FastAPI, module_name: str, attr: str = "router") -> bool:
    """Include ``app.<module_name>:<attr>`` if it exists; warn otherwise."""
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.warning("router module %s not available yet: %s", module_name, exc)
        return False
    router = getattr(module, attr, None)
    if router is None:
        logger.warning("router module %s has no %r attribute", module_name, attr)
        return False
    app.include_router(router)
    return True


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. ``settings`` defaults to the launch env."""
    if settings is None:
        settings = get_settings()

    configure_logging(settings.log_level, settings.log_dir)

    app = FastAPI(title="YouTube Agent Backend", version=__version__)

    # -- state --------------------------------------------------------------
    app.state.settings = settings
    app.state.boot_id = settings.boot_id
    app.state.backend_version = __version__
    app.state.db = None  # set to the sqlite3 connection by the DB wiring below
    app.state.ws_hub = None  # placeholder — set by app.ws_hub wiring
    app.state.execution = None  # set by the execution wiring below
    app.state.ready = False  # placeholder readiness flag (health router refines)
    app.state.extra: dict = {}

    # -- database wiring ------------------------------------------------------
    # Initialise the SQLite schema (idempotent) so /health/ready can report
    # a real state. Failure must not kill the app factory: the health router
    # will report 503 not_ready and routers degrade individually.
    try:
        from app import db as db_module

        db_module.init_db(settings.db_path).close()
        app.state.db_path = settings.db_path
        app.state.db = db_module.connect(settings.db_path)
    except Exception as exc:  # noqa: BLE001 — boot must survive a broken DB
        logger.warning("database init failed, continuing degraded: %s", exc)

    # -- uninstall data policy ----------------------------------------------
    # Re-apply the persisted `privacy.remove_data_on_uninstall` flag to
    # the `uninstall-purge.marker` file in the data root, so the marker
    # the NSIS uninstaller checks always matches the user's saved
    # choice even if the file was removed/added out-of-band.  Default
    # (flag absent/false) = no marker = data preserved on uninstall.
    try:
        from app.routers.settings import _load as _load_app_settings
        from app.uninstall_policy import reconcile_marker

        reconcile_marker(_load_app_settings(), settings.data_dir)
    except Exception as exc:  # noqa: BLE001 — boot must survive this too
        logger.warning("uninstall marker reconcile failed: %s", exc)

    # -- execution wiring -----------------------------------------------------
    # Attach the Round-2 ExecutionService (job runner + scheduler loop).
    # Autostart defaults OFF here so tests/embedders get a dormant
    # service on app.state.execution that they can start manually; the
    # YTA_EXECUTION env flag overrides, and run() (the real process
    # entry) defaults it to '1' when unset — so only the shipped
    # sidecar executes automatically.  YTA_EXECUTION_AUTOSTART=0 is an
    # additional kill-switch honoured inside attach() itself.
    try:
        from app.execution.service import attach as _attach_execution

        _attach_execution(
            app, autostart=_env_flag("YTA_EXECUTION", default=False)
        )
    except ImportError as exc:
        logger.warning("app.execution.service not available yet: %s", exc)
    except Exception as exc:  # noqa: BLE001 — boot must survive a broken service
        logger.warning("execution attach failed, continuing degraded: %s", exc)

    # -- startup announcement -------------------------------------------------
    announced = {"done": False}

    def _announce_listening() -> None:
        """Print the single structured stdout line (exactly once per app)."""
        if announced["done"]:
            return
        announced["done"] = True
        app.state.ready = True
        line = json.dumps(
            {"event": "listening", "port": settings.port, "boot_id": settings.boot_id}
        )
        print(line, flush=True)

    # Exposed for run()/tests, and registered as a FastAPI startup event.
    app.state.on_startup = _announce_listening
    try:
        app.add_event_handler("startup", _announce_listening)
    except Exception:  # very new FastAPI: fall back to router hook list
        app.router.on_startup.append(_announce_listening)  # type: ignore[attr-defined]

    # -- shutdown: close the live DB connection ------------------------------
    # The app owns exactly one long-lived sqlite3 connection
    # (app.state.db, opened by the DB wiring above). Until now nothing
    # closed it at shutdown, so every app instance leaked its handle for
    # the life of the process — and on Windows a still-open handle blocks
    # replacing the database file, which is what made backup restore
    # fail in a full-suite process full of earlier tests' leaked handles.
    # This handler pairs the connect(): it runs on every graceful
    # shutdown (uvicorn stop, TestClient exit). Tolerant by design: the
    # restore flow may already have closed the handle and set it to None
    # (then there is nothing to do), and a failed close is logged, never
    # raised — shutdown must not turn into a crash.
    def _close_db_on_shutdown() -> None:
        conn = getattr(app.state, "db", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.warning(
                "shutdown: closing the app database connection failed",
                exc_info=True,
            )
        app.state.db = None

    # Exposed for run()/tests, and registered as a FastAPI shutdown event
    # (same on_event pattern as the startup announcement above: passing a
    # constructor lifespan instead would make Starlette skip the
    # on_startup handlers this app relies on).
    app.state.on_shutdown = _close_db_on_shutdown
    try:
        app.add_event_handler("shutdown", _close_db_on_shutdown)
    except Exception:  # very new FastAPI: fall back to router hook list
        app.router.on_shutdown.append(_close_db_on_shutdown)  # type: ignore[attr-defined]

    # -- error envelope -------------------------------------------------------
    try:
        from app.errors import register_exception_handlers

        register_exception_handlers(app)
    except ImportError as exc:
        logger.warning("app.errors not available yet: %s", exc)

    # -- auth / defence-in-depth middleware -----------------------------------
    try:
        from app.middleware import LaunchTokenMiddleware

        # NOTE: no constructor `settings=` override on purpose — the
        # middleware resolves the token per-request from
        # app.state.settings (set above), which keeps app.state the
        # single source of truth and lets tests/embedders swap it.
        app.add_middleware(LaunchTokenMiddleware)
    except ImportError as exc:
        logger.warning("app.middleware not available yet: %s", exc)

    # -- routers ----------------------------------------------------------------
    _include_router(app, "app.routers.health")
    # WebSocket side: REST ws-ticket router + the websocket route itself.
    _include_router(app, "app.routers.ws", attr="router")
    _include_router(app, "app.routers.ws", attr="ws_router")
    # Aggregate API router (worker 6) — app must still boot if it is late.
    try:
        from app.routers import api as api_module

        app.include_router(api_module.router)
    except ImportError as exc:
        logger.warning("app.routers.api not available yet: %s", exc)

    return app


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _supported_schema_version() -> int | None:
    """Newest DB schema version this build understands, or None.

    Prefers :func:`app.backup.current_db_schema_version` (the Alembic
    head, derived from the shipped migration files); falls back to
    scanning the migrations directory directly, then to the base-DDL
    ``SCHEMA_VERSION``. None = could not determine (the caller then
    skips the downgrade check rather than guessing).
    """
    try:
        from app.backup import current_db_schema_version

        return int(current_db_schema_version())
    except Exception:  # noqa: BLE001 — fall through to the local scan
        pass
    try:
        import re

        versions_dir = Path(__file__).resolve().parent.parent / "migrations" / "versions"
        heads: list[int] = []
        for path in versions_dir.glob("*.py"):
            match = re.search(
                r"^revision(?::\s*str)?\s*=\s*[\"'](\d+)[\"']",
                path.read_text(encoding="utf-8"),
                re.M,
            )
            if match:
                heads.append(int(match.group(1)))
        if heads:
            return max(heads)
    except Exception:  # noqa: BLE001 — fall through to the DDL version
        pass
    try:
        from app.schema_ddl import SCHEMA_VERSION

        return int(SCHEMA_VERSION)
    except Exception:  # noqa: BLE001
        return None


def _existing_db_schema_version(db_path: Path) -> int | None:
    """Schema version recorded in an existing DB file, or None.

    None when there is no DB file yet (first run — nothing to protect)
    or the version cannot be read (corrupt/locked DB: startup then
    proceeds and /health/ready reports the real state; exit 4 is only
    for a version that is *definitively* newer). The file is opened
    read-only so the check itself can never mutate or create it.
    """
    try:
        if not Path(db_path).exists():
            return None
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            try:
                from app.backup import database_schema_version

                return int(database_schema_version(conn))
            except Exception:  # noqa: BLE001 — use the base reader
                from app.db import schema_version

                return int(schema_version(conn))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable != newer
        return None


def _known_schema_revisions() -> frozenset[str] | None:
    """Revision ids this build ships (e.g. ``{"0001", ..., "0011"}``).

    Read from the ``revision`` declarations of the shipped migration
    files — the same source :func:`_supported_schema_version` and
    :func:`app.backup.current_db_schema_version` derive the numeric
    head from. None when the migrations directory cannot be read
    (packaged layouts): the caller then relies on the numeric
    comparison only, rather than refusing every database.
    """
    try:
        import re

        versions_dir = Path(__file__).resolve().parent.parent / "migrations" / "versions"
        known: set[str] = set()
        for path in versions_dir.glob("*.py"):
            match = re.search(
                r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']",
                path.read_text(encoding="utf-8"),
                re.M,
            )
            if match:
                known.add(match.group(1))
        return frozenset(known) if known else None
    except Exception:  # noqa: BLE001 — unknown set != refuse everything
        return None


def _existing_db_revision(db_path: Path) -> str | None:
    """Raw ``alembic_version.version_num`` recorded in a DB, or None.

    The numeric readers (:func:`app.backup.database_schema_version`)
    parse ``int(version_num.split("_")[0])`` and silently fall back
    for a non-numeric future revision (e.g. ``"future_xyz"`` written
    by a newer build with a different revision scheme) — such a DB
    would read as *old*, not new. This raw reader exists so the
    downgrade guard can apply the set-membership rule as well: a
    recorded revision that is not one of this build's known
    revisions is, by definition, not an ancestor-or-equal of the
    known head. None when there is no DB file, no
    ``alembic_version`` table/row, or the file cannot be read. The
    file is opened read-only, exactly like
    :func:`_existing_db_schema_version`.
    """
    try:
        if not Path(db_path).exists():
            return None
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "alembic_version" not in tables:
                return None
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            if row is None or row[0] is None:
                return None
            value = str(row[0]).strip()
            return value or None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable != newer
        return None


def _refuse_if_schema_too_new(settings: Settings) -> None:
    """Exit code 4 when the on-disk DB is newer than this build.

    Roadmap §4.6/§4.7: a backend that finds a newer schema refuses to
    start rather than corrupting data; the shell routes to the updater.
    Runs BEFORE create_app()/init_db so nothing touches the newer DB,
    and therefore before the ``{"event": "listening", ...}`` line can
    ever be printed. The stderr line carries the literal detail
    string ``database_newer_than_binary`` — the same detail the
    shell (``src-tauri/src/sidecar.rs``) attaches to exit code 4 —
    so log-based routing/diagnostics see it verbatim.

    "Newer" is decided two ways (either one refuses):

    * numeric: the recorded schema version (reusing
      :func:`app.backup.database_schema_version` via
      :func:`_existing_db_schema_version`) is greater than the
      binary's known head (:func:`_supported_schema_version`), or
    * set membership: the recorded Alembic revision string exists
      but is not in the binary's known revision set
      (:func:`_known_schema_revisions`) — an unknown/future
      revision, including non-numeric ones the numeric reader
      cannot rank.
    """
    supported = _supported_schema_version()
    found = _existing_db_schema_version(settings.db_path)
    revision = _existing_db_revision(settings.db_path)
    known = _known_schema_revisions()
    numeric_newer = (
        supported is not None and found is not None and found > supported
    )
    unknown_revision = (
        revision is not None and known is not None and revision not in known
    )
    if numeric_newer or unknown_revision:
        recorded = revision if revision is not None else found
        print(
            "fatal: database_newer_than_binary: database schema "
            f"version {recorded} is newer than this build supports "
            f"({supported}); refusing to start",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_SCHEMA_TOO_NEW)


def _install_clean_shutdown_signals() -> dict:
    """Map SIGTERM/SIGINT (and Windows SIGBREAK) to a clean exit 0.

    Roadmap §4.6: exit 0 = clean shutdown; anything else = crash. The
    shell's own shutdown path is POST /system/shutdown + the Job
    Object, but a POSIX supervisor's SIGTERM must ALSO read as clean,
    not as a crash. uvicorn (0.54) shuts down gracefully on these
    signals but then restores the previously-registered handler and
    re-raises the signal "to trigger the expected behaviour" — with
    the default disposition that means death-by-signal (143), which
    the shell would have to read as a crash. Pre-registering a handler
    that raises ``SystemExit(EXIT_OK)`` makes uvicorn's re-raise land
    here instead, so a graceful signal-driven stop exits 0. During
    serving, uvicorn's own handler displaces this one, so its graceful
    drain (and double-signal force-exit) is unaffected.
    """
    import signal as signal_module

    def _clean_exit(signum, frame):  # noqa: ANN001, ANN202 — signal handler shape
        raise SystemExit(EXIT_OK)

    previous: dict = {}
    names = ["SIGTERM", "SIGINT", "SIGBREAK"]  # SIGBREAK is Windows-only
    for name in names:
        sig = getattr(signal_module, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal_module.signal(sig, _clean_exit)
        except (ValueError, OSError):  # not in main thread / unsupported
            pass
    return previous


def _restore_signals(previous: dict) -> None:
    import signal as signal_module

    for sig, handler in previous.items():
        try:
            signal_module.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _assert_port_bindable(port: int) -> None:
    """Pre-flight bind probe: exit code 3 before uvicorn starts.

    Makes the bind-failure path deterministic (uvicorn otherwise runs
    the full app startup — including the listening announcement —
    before its own bind attempt fails) and guarantees no
    ``{"event": "listening", ...}`` line is ever printed for a port
    that could not be bound. The OSError catch around ``uvicorn.run``
    in :func:`run` stays as the backstop for the residual race.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, port))
    except OSError as exc:
        print(f"fatal: bind failure on {HOST}:{port}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BIND_FAILURE) from exc


def run() -> None:
    """Console entry point: ``youtube-agent-backend``.

    Argument handling (Round 6): when the process is launched with
    ``--scheduled-run`` (Windows Task Scheduler wakes the app this way;
    see ``packaging/register_scheduler.ps1``), the one-shot scheduled
    run in :mod:`app.execution.scheduled_run` performs a single
    catch-up scheduler tick and the process exits — no server, no
    window, no launch token required.  ``--scheduled-run --dry-run``
    lists the due schedules without running them.  A launch *without*
    the flag follows the normal serving path below, unchanged.
    """
    argv = list(sys.argv[1:])
    if "--scheduled-run" in argv or "--dry-run" in argv:
        from app.execution.scheduled_run import main as _scheduled_main

        raise SystemExit(_scheduled_main(argv))

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"fatal: invalid launch environment: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ENV) from exc

    configure_logging(settings.log_level, settings.log_dir)

    # Loopback-only invariant (exit code 2 = fatal bind-scope error).
    if HOST not in _LOOPBACK_HOSTS:
        print(f"fatal: refusing to bind non-loopback host {HOST!r}", file=sys.stderr)
        raise SystemExit(EXIT_BIND_SCOPE)

    # Downgrade protection (exit code 4): refuse a newer DB schema
    # before create_app()/init_db can touch it.
    _refuse_if_schema_too_new(settings)

    if settings.port == 0:
        settings = dataclasses.replace(settings, port=_pick_free_loopback_port())

    # Deterministic bind failure (exit code 3) before any startup work.
    _assert_port_bindable(settings.port)

    # Execution autostart defaults ON for the real process entry only
    # (see the execution wiring in create_app); an explicit YTA_EXECUTION
    # in the launch environment always wins.
    os.environ.setdefault("YTA_EXECUTION", "1")

    app = create_app(settings)

    import uvicorn

    # SIGTERM/SIGINT must read as a CLEAN shutdown (exit 0), not a
    # crash — see _install_clean_shutdown_signals. Handlers are
    # restored on the way out so embedders/tests are unaffected.
    _previous_handlers = _install_clean_shutdown_signals()
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=settings.port,
            # log_config=None: uvicorn must NOT install its default
            # handlers (they write to stderr/stdout with no redaction and
            # set propagate=False, bypassing the log files). With no
            # config, uvicorn's loggers propagate to the root handlers
            # installed by configure_logging() — rotating, redacted —
            # and uvicorn only applies the level (app.logging_setup).
            log_config=None,
            log_level=settings.log_level if settings.log_level != "warn" else "warning",
            access_log=False,
        )
    except OSError as exc:
        print(f"fatal: bind failure on {HOST}:{settings.port}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BIND_FAILURE) from exc
    finally:
        _restore_signals(_previous_handlers)


if __name__ == "__main__":
    run()
