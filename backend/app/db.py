"""SQLite connection handling for the YouTube Agent sidecar.

One database file (``app.db``), WAL mode, single source of truth.
Section 15 pragmas are applied on **every** connection:

    journal_mode=WAL, foreign_keys=ON, synchronous=NORMAL,
    busy_timeout=5000, cache_size=-64000

Connections run with ``isolation_level=None`` (autocommit) so write
transactions are explicit — use :func:`transaction`, which issues
``BEGIN IMMEDIATE`` (required for the atomic job-claim path, Q1).
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from app.schema_ddl import SCHEMA_DDL, SCHEMA_VERSION

PathLike = Union[str, os.PathLike]

# Pragmas applied on every connection (section 15 §Global conventions).
PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA cache_size=-64000",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def default_db_path() -> Path:
    """Resolve the default database path.

    Priority: ``app.config`` settings (``db_path``) -> the ONE canonical
    resolver (:func:`app.data_dir.resolve_data_dir`), which honours
    ``YTA_DATA_DIR`` on every platform, then ``%APPDATA%\\YouTubeAgent``
    on Windows, then the dev default ``~/.youtube-agent``.

    Round 6 note: the previous last-resort fallback hand-rolled its own
    platform logic and diverged (``~/.local/share/YouTubeAgent`` on
    Linux).  It now goes through the canonical resolver; that fallback
    only ever ran when settings loading itself had failed.
    """
    try:  # app.config is owned by another module; import defensively.
        from app.config import get_settings  # type: ignore

        settings = get_settings()
        candidate = getattr(settings, "db_path", None)
        if candidate:
            return Path(candidate)
    except Exception:
        pass

    from app.data_dir import resolve_data_dir

    return resolve_data_dir() / "app.db"


def connect(db_path: PathLike) -> sqlite3.Connection:
    """Open a connection with row factory and all section-15 pragmas set."""
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


def get_connection(db_path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Return a configured connection (explicit path or configured default)."""
    return connect(db_path if db_path is not None else default_db_path())


def init_db(path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Create (if needed) and initialise the database; idempotent.

    Creates the parent directory, applies the full schema DDL
    (``CREATE ... IF NOT EXISTS`` throughout) and records the schema
    version in ``schema_meta``. Returns the open connection.
    """
    db_path = Path(path) if path is not None else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA_DDL)
    conn.execute(
        "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
        "updated_at = excluded.updated_at",
        (SCHEMA_VERSION, _now_ms()),
    )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Current schema version: schema_meta, else alembic_version, else 0."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "schema_meta" in tables:
        row = conn.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()
        if row is not None:
            return int(row[0])
    if "alembic_version" in tables:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is not None:
            try:
                return int(str(row[0]).split("_")[0])
            except ValueError:
                return 0
    return 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction using ``BEGIN IMMEDIATE``.

    Takes the write lock up front (queue claiming, lease updates) instead
    of upgrading mid-transaction. Commits on success, rolls back on error.
    If the connection is already inside a transaction, yields without
    nesting (SQLite has no nested transactions without savepoints).
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
