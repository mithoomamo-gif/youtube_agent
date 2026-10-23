"""Diagnostics bundle builder ("Copy diagnostics").

Assembles a single JSON payload a user can preview and then share with
support. The bundle is built for *preview-before-share*: there is exactly
one builder (:func:`build_diagnostics_bundle`) and one serialiser
(:func:`serialize_bundle`), and the API endpoint returns the serialised
bytes verbatim — the bytes a user previews are byte-identical to the
bytes that would be copied/sent. There is no second, hidden bundle.

Included (all safe, aggregate, or redacted):

* ``app`` — app / backend / API versions.
* ``runtime`` — OS/platform and Python version/implementation.
* ``system`` — CPU count, disk free/total for the data volume, and
  ffmpeg presence + version line (cheap ``ffmpeg -version`` probe).
* ``health`` — DB reachability + schema/migration state, using the same
  probe as ``GET /health/ready`` (``SELECT 1`` +
  :func:`app.db.schema_version` + the ``alembic_version`` head when the
  migrations table exists).
* ``counts`` — row *counts only* (channels, projects, jobs, schedules,
  registered credentials). Never row contents.
* ``config_state`` — non-secret launch state only (log level, detached
  flag, loopback invariant, whether the data dir exists). Never values
  from the settings table, never paths.
* ``logs`` — the last N lines of ``<data_dir>/logs/backend.log``
  (N configurable, default 200), each passed through the redaction
  choke point.

Excluded — by construction, not by filtering after the fact:

* secret values, OAuth tokens and API keys: no secret is ever read;
  additionally every string in the finished bundle passes through
  :func:`app.redaction.redact_text` / :func:`app.redaction.redact_obj`
  (the single redaction choke point, which also scrubs every value
  registered via :func:`app.redaction.register_secret`), so a secret
  that leaked into a log line is replaced with ``***``.
* user content: no script text, no comment text, no channel titles, no
  project topics — the builder only ever runs ``COUNT(*)`` queries.
* identifying file paths: the absolute data-dir / DB / log paths are
  never emitted, and any home-directory prefix appearing inside a log
  line is rewritten to ``~`` (the data-dir prefix to ``<data_dir>``).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.redaction import REDACTED, redact_obj, redact_text

BUNDLE_VERSION = 1
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 1000

_COUNT_TABLES = ("channels", "projects", "jobs", "schedules", "provider_credentials")


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------


def _path_prefixes(settings: Any = None) -> list[tuple[str, str]]:
    """(absolute prefix, replacement) pairs scrubbed from every string."""
    pairs: list[tuple[str, str]] = []
    try:
        home = str(Path.home())
        if home and home != ".":
            pairs.append((home, "~"))
    except Exception:
        pass
    data_dir = getattr(settings, "data_dir", None)
    if data_dir:
        raw = str(data_dir)
        if raw and raw not in {p for p, _ in pairs}:
            pairs.append((raw, "<data_dir>"))
    # Longest first so the data-dir (usually under home) wins over home.
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def _scrub_text(text: str, prefixes: list[tuple[str, str]]) -> str:
    out = redact_text(text)
    for prefix, replacement in prefixes:
        if prefix in out:
            out = out.replace(prefix, replacement)
    return out


def _scrub_obj(obj: Any, prefixes: list[tuple[str, str]]) -> Any:
    """redact_obj + path-prefix scrubbing, recursively."""
    cleaned = redact_obj(obj)
    if isinstance(cleaned, str):
        return _scrub_text(cleaned, prefixes)
    if isinstance(cleaned, dict):
        return {key: _scrub_obj(value, prefixes) for key, value in cleaned.items()}
    if isinstance(cleaned, list):
        return [_scrub_obj(item, prefixes) for item in cleaned]
    if isinstance(cleaned, tuple):
        return tuple(_scrub_obj(item, prefixes) for item in cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# probes (each best-effort: a failed probe reports None/"unknown", never raises)
# ---------------------------------------------------------------------------


def _probe_ffmpeg() -> dict[str, Any]:
    path = shutil.which("ffmpeg")
    if not path:
        return {"available": False, "version": None}
    version: Optional[str] = None
    try:
        proc = subprocess.run(
            [path, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        first = (proc.stdout or "").splitlines()
        if first:
            version = first[0].strip() or None
    except Exception:
        version = None
    return {"available": True, "version": version}


def _probe_disk(data_dir: Any) -> dict[str, Any]:
    target = str(data_dir) if data_dir else str(Path.home())
    try:
        usage = shutil.disk_usage(target)
        return {"free_bytes": int(usage.free), "total_bytes": int(usage.total)}
    except Exception:
        return {"free_bytes": None, "total_bytes": None}


def _open_db(settings: Any = None, db_path: Any = None) -> Optional[sqlite3.Connection]:
    try:
        from app import db as db_module

        if db_path is not None:
            return db_module.connect(db_path)
        settings_db = getattr(settings, "db_path", None)
        if settings_db is not None:
            return db_module.connect(settings_db)
        return db_module.get_connection()
    except Exception:
        return None


def _collect_health_and_counts(
    settings: Any = None, db_path: Any = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: dict[str, Any] = {name: None for name in _COUNT_TABLES}
    health: dict[str, Any] = {
        "db": "unknown",
        "schema_version": None,
        "migration_head": None,
    }
    conn = _open_db(settings, db_path)
    if conn is None:
        health["db"] = "down"
        return health, counts
    try:
        conn.execute("SELECT 1").fetchone()
        health["db"] = "ok"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for name in _COUNT_TABLES:
            if name in tables:
                try:
                    counts[name] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    )
                except Exception:
                    counts[name] = None
        try:
            from app import db as db_module

            schema_version = getattr(db_module, "schema_version", None)
            if callable(schema_version):
                health["schema_version"] = int(schema_version(conn))
        except Exception:
            health["schema_version"] = None
        if "alembic_version" in tables:
            try:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
                health["migration_head"] = str(row[0]) if row is not None else None
            except Exception:
                health["migration_head"] = None
    except Exception:
        health["db"] = "down"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return health, counts


def read_log_tail(log_dir: Any, limit: int) -> list[str]:
    """Last ``limit`` raw lines of ``<log_dir>/backend.log`` ([] if absent)."""
    if limit <= 0 or log_dir is None:
        return []
    try:
        log_file = Path(log_dir) / "backend.log"
        if not log_file.is_file():
            return []
        # Read only a bounded tail so a huge log stays cheap.
        size = log_file.stat().st_size
        with log_file.open("rb") as handle:
            if size > 4 * 1024 * 1024:
                handle.seek(size - 4 * 1024 * 1024)
            raw = handle.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def _collect_log_lines(settings: Any, limit: int) -> list[str]:
    log_dir = getattr(settings, "log_dir", None)
    if log_dir is None:
        try:
            from app.config import get_settings

            log_dir = get_settings().log_dir
        except Exception:
            log_dir = None
    return read_log_tail(log_dir, limit)


# ---------------------------------------------------------------------------
# builder + serialiser (the single code path behind preview AND share)
# ---------------------------------------------------------------------------


def build_diagnostics_bundle(
    *,
    settings: Any = None,
    log_line_limit: int = DEFAULT_LOG_LINES,
    log_lines: Optional[Iterable[str]] = None,
    db_path: Any = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build the diagnostics bundle dict (see module docstring).

    ``log_lines`` overrides the log-file source (tests / embedders);
    either way every line passes through the redactor. ``settings``
    defaults to no launch settings: versions/runtime/system probes
    still work and DB/log sections degrade to honest empty states.
    """
    limit = max(0, min(int(log_line_limit), MAX_LOG_LINES))
    prefixes = _path_prefixes(settings)

    if settings is None:
        try:
            from app.config import get_settings

            settings = get_settings()
        except Exception:
            settings = None

    try:
        from app import __version__ as app_version
    except Exception:
        app_version = "unknown"
    try:
        from app.routers.system import BACKEND_VERSION

        backend_version = str(BACKEND_VERSION)
    except Exception:
        backend_version = str(app_version)

    health, counts = _collect_health_and_counts(settings, db_path)

    raw_lines = (
        [str(line) for line in log_lines]
        if log_lines is not None
        else _collect_log_lines(settings, limit)
    )
    cleaned_lines = [_scrub_text(line, prefixes) for line in raw_lines[-limit:]] if limit else []

    data_dir = getattr(settings, "data_dir", None)
    disk = _probe_disk(data_dir)
    ffmpeg = _probe_ffmpeg()
    if ffmpeg.get("version"):
        ffmpeg["version"] = _scrub_text(str(ffmpeg["version"]), prefixes)

    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "generated_at": generated_at or _utc_now_iso(),
        "app": {
            "app_version": str(app_version),
            "backend_version": backend_version,
            "api_version": 1,
        },
        "runtime": {
            "platform": platform.platform(),
            "os": platform.system(),
            "os_release": platform.release(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "system": {
            "cpu_count": os.cpu_count(),
            "disk_free_bytes": disk["free_bytes"],
            "disk_total_bytes": disk["total_bytes"],
            "ffmpeg": ffmpeg,
        },
        "health": health,
        "counts": {
            "channels": counts["channels"],
            "projects": counts["projects"],
            "jobs": counts["jobs"],
            "schedules": counts["schedules"],
        },
        "config_state": {
            "log_level": getattr(settings, "log_level", None),
            "detached": bool(getattr(settings, "detached", False)),
            "loopback_only": True,
            "data_dir_present": bool(data_dir and Path(data_dir).exists()),
            "credentials_registered": counts["provider_credentials"],
        },
        "logs": {
            "limit": limit,
            "count": len(cleaned_lines),
            "lines": cleaned_lines,
        },
    }
    # Final belt-and-braces pass: the ENTIRE bundle goes through the
    # redaction choke point + path scrubbing, whatever a probe returned.
    return _scrub_obj(bundle, prefixes)


def serialize_bundle(bundle: dict[str, Any]) -> bytes:
    """Serialise ``bundle`` deterministically — the exact share bytes."""
    return json.dumps(
        bundle, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")


__all__ = [
    "BUNDLE_VERSION",
    "DEFAULT_LOG_LINES",
    "MAX_LOG_LINES",
    "REDACTED",
    "build_diagnostics_bundle",
    "read_log_tail",
    "serialize_bundle",
]
