"""Backup export / restore for the YouTube Agent sidecar (Round 6).

A backup is a single user-facing zip bundle::

    youtube-agent-backup-<UTC timestamp>.zip
    ├── manifest.json          format/app/schema versions + in/out list
    ├── database/app.db        consistent SQLite snapshot (backup API)
    └── data/
        ├── channels.json      channel rows (secret-shaped fields stripped)
        ├── schedules.json     schedule rows
        ├── style_profiles.json  Style Lab style profiles
        └── competitors.json   competitor channels + video *metadata*

What is NEVER in a bundle
-------------------------
* **Raw secret values.**  API keys and OAuth tokens live in the secret
  store (OS credential store), never in the database — the DB only
  holds *references* (``provider_credentials.keyring_ref``), and a
  reference may travel with the backup while the value never does.
  The JSON snapshots additionally strip any secret-shaped field as
  defence in depth.
* **Competitor video files** (and thumbnails): large, re-attachable,
  and not needed to restore the app's state — only their metadata /
  analysis rows (inside the DB) are backed up.
* **Caches, logs, rendered videos and other derived artefacts.**

Schema-version / downgrade protection
-------------------------------------
The manifest records the bundle's DB schema version (the Alembic
revision number actually applied to the database, ``0001``..``0011``;
``schema_meta`` is pinned at the base-DDL version and does not advance
with migrations, so it is only a fallback).  Restoring a backup whose
schema is NEWER than this build supports is refused outright — a
backup made by a newer app is never partially applied.  Older backups
restore cleanly: after the file swap the app's idempotent schema
ensures (base DDL + lazy per-feature tables) run over the restored DB,
exactly as they do on a normal boot.

Restore is validate-first and atomic-ish
----------------------------------------
Everything (zip integrity, manifest, format + schema versions, DB
sha256, ``PRAGMA integrity_check``, the DB's own recorded schema
version vs the manifest's claim) is validated BEFORE the live database
is touched.  A safety copy of the pre-restore DB is then taken with
the SQLite backup API into ``<data_dir>/backups/pre-restore-*.db``,
and the new DB is installed with ``os.replace`` (atomic rename) after
removing stale WAL sidecars, so a failed restore leaves the live data
exactly as it was.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app import __version__ as APP_VERSION
from app import db as db_module

__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "CURRENT_DB_SCHEMA_VERSION",
    "BackupError",
    "build_export",
    "current_db_schema_version",
    "database_schema_version",
    "restore_backup",
    "validate_bundle",
]

#: Version of the zip bundle layout itself (manifest shape + entries).
BUNDLE_FORMAT_VERSION = 1

#: Fallback for the newest DB schema this build can restore, used only
#: when the Alembic migrations directory is not present (packaged
#: layouts).  Must equal the Alembic head revision — ``0011`` at
#: Round 6.  ``current_db_schema_version()`` prefers the real head.
_FALLBACK_SCHEMA_VERSION = 11

#: Entry names inside the bundle (POSIX separators, always).
MANIFEST_ENTRY = "manifest.json"
DATABASE_ENTRY = "database/app.db"
CHANNELS_ENTRY = "data/channels.json"
SCHEDULES_ENTRY = "data/schedules.json"
STYLE_PROFILES_ENTRY = "data/style_profiles.json"
COMPETITORS_ENTRY = "data/competitors.json"

#: Generous caps so a corrupt/hostile upload cannot fill the disk.
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024

#: Substrings that mark a field as secret-shaped in JSON snapshots.
#: (``testing_mode_token`` is a boolean *flag*, not a token value, and
#: is explicitly allowed — it says whether Google is expiring refresh
#: tokens early, it is not credential material.)
_SECRET_MARKERS = ("secret", "password", "api_key", "apikey")
_SECRET_SUFFIXES = ("_token", "_key")
_SECRET_ALLOWLIST = {"testing_mode_token"}


class BackupError(Exception):
    """A backup/restore failure that maps onto the API error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Schema version handling
# ---------------------------------------------------------------------------

_REVISION_RE = re.compile(r'^revision(?::\s*str)?\s*=\s*["\'](\d+)["\']', re.M)


def current_db_schema_version() -> int:
    """Newest DB schema version this build supports (Alembic head).

    Derived from the highest ``revision`` in
    ``backend/migrations/versions/`` so it cannot drift from the
    migrations that actually ship; falls back to
    :data:`_FALLBACK_SCHEMA_VERSION` when the directory is absent.
    """
    versions_dir = (
        Path(__file__).resolve().parent.parent / "migrations" / "versions"
    )
    try:
        heads = []
        for path in versions_dir.glob("*.py"):
            match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
            if match:
                heads.append(int(match.group(1)))
        if heads:
            return max(heads)
    except OSError:
        pass
    return _FALLBACK_SCHEMA_VERSION


#: Newest restorable DB schema version for this build.
CURRENT_DB_SCHEMA_VERSION = current_db_schema_version()


def database_schema_version(conn: sqlite3.Connection) -> int:
    """The schema version actually applied to an open database.

    Prefers ``alembic_version`` (the migration revision, which advances
    0001 -> 0011) because ``schema_meta.version`` is pinned at the
    base-DDL ``SCHEMA_VERSION`` by ``init_db`` and never advances.
    Falls back to :func:`app.db.schema_version` (schema_meta, then 0).
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "alembic_version" in tables:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is not None:
            try:
                return int(str(row[0]).split("_")[0])
            except (TypeError, ValueError):
                pass
    return db_module.schema_version(conn)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_secret_shaped(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_ALLOWLIST:
        return False
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return True
    return lowered.endswith(_SECRET_SUFFIXES)


def _sanitise(value: Any) -> Any:
    """Recursively strip secret-shaped fields from a snapshot value.

    Defence in depth: the database holds no secret values by design
    (only secret-store references), but a channel ``config_json`` is
    user-influenced JSON, so anything shaped like a credential is
    dropped from the human-readable snapshots rather than exported.
    """
    if isinstance(value, dict):
        return {
            k: _sanitise(v)
            for k, v in value.items()
            if not _is_secret_shaped(str(k))
        }
    if isinstance(value, list):
        return [_sanitise(item) for item in value]
    return value


def _table_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """All rows of ``table`` as sanitised dicts ([] when absent)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return []
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, str) and key.endswith("_json"):
                try:
                    row[key] = json.loads(value)
                except (TypeError, ValueError):
                    pass
    return _sanitise(rows)


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _snapshot_database(db_path: Path, dest_path: Path) -> None:
    """Consistent copy of the live DB via the SQLite backup API."""
    src = db_module.connect(db_path)
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _excluded_list() -> list[dict[str, str]]:
    """The manifest's explicit NOT-included list (what + why)."""
    return [
        {
            "what": "API keys and OAuth tokens (raw secret values)",
            "why": (
                "Secrets live in the OS credential store / secret store, "
                "never in the database or in backups. The database only "
                "holds references to them (e.g. provider_credentials."
                "keyring_ref); the reference is included, the value is "
                "not. Re-enter keys / reconnect channels after restoring "
                "on a different PC."
            ),
        },
        {
            "what": "Competitor video files and their thumbnails",
            "why": (
                "Excluded by default: they are large and can be "
                "re-attached. Only their metadata and analysis results "
                "(stored in the database) are backed up."
            ),
        },
        {
            "what": "Caches (stock downloads, model caches, HTTP caches)",
            "why": "Derived data — re-downloaded or rebuilt on demand.",
        },
        {
            "what": "Log files",
            "why": "Diagnostics only; not needed to restore app state.",
        },
        {
            "what": "Rendered videos and project media files",
            "why": (
                "Large derived artefacts. The database rows describing "
                "them are backed up; the media files themselves are not."
            ),
        },
    ]


def build_export(db_path: Path, dest_path: Path) -> dict:
    """Write a backup bundle for the DB at ``db_path`` to ``dest_path``.

    Returns ``{"path", "size_bytes", "manifest"}``. Raises
    :class:`BackupError` when there is no database to back up.
    """
    db_path = Path(db_path)
    dest_path = Path(dest_path)
    if not db_path.is_file():
        raise BackupError(
            404,
            "backup_no_database",
            "There is no database to back up yet — the app has not "
            "created its data store.",
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="yta-backup-export-"))
    try:
        snapshot_path = staging / "app.db"
        _snapshot_database(db_path, snapshot_path)

        conn = db_module.connect(snapshot_path)
        try:
            schema_version = database_schema_version(conn)
            channels = _table_rows(conn, "channels")
            schedules = _table_rows(conn, "schedules")
            style_profiles = _table_rows(conn, "style_profiles")
            competitors = {
                "competitor_channels": _table_rows(conn, "competitor_channels"),
                "competitor_videos": _table_rows(conn, "competitor_videos"),
            }
            counts = {
                "channels": len(channels),
                "schedules": len(schedules),
                "style_profiles": len(style_profiles),
                "competitor_channels": len(competitors["competitor_channels"]),
                "competitor_videos": len(competitors["competitor_videos"]),
            }
        finally:
            conn.close()

        db_sha256 = _sha256_file(snapshot_path)
        db_size = snapshot_path.stat().st_size
        created = datetime.now(timezone.utc)

        snapshots = {
            CHANNELS_ENTRY: channels,
            SCHEDULES_ENTRY: schedules,
            STYLE_PROFILES_ENTRY: style_profiles,
            COMPETITORS_ENTRY: competitors,
        }
        included: list[dict[str, Any]] = [
            {
                "path": DATABASE_ENTRY,
                "kind": "sqlite_database",
                "why": (
                    "The app's single source of truth: channels, "
                    "schedules, projects, Style Lab data (competitors, "
                    "style profiles), settings and ledgers."
                ),
                "sha256": db_sha256,
                "size_bytes": db_size,
            }
        ]
        snapshot_meta = {
            CHANNELS_ENTRY: (
                "channel_configs",
                "Channel configurations in readable form (the same rows "
                "as in the database; secret-shaped fields stripped).",
            ),
            SCHEDULES_ENTRY: (
                "schedules",
                "Automation schedules in readable form (same rows as "
                "in the database).",
            ),
            STYLE_PROFILES_ENTRY: (
                "style_profiles",
                "Style Lab style profiles in readable form (same rows "
                "as in the database).",
            ),
            COMPETITORS_ENTRY: (
                "competitor_metadata",
                "Competitor channels and video metadata (no video "
                "files — see the excluded list).",
            ),
        }
        for entry, (kind, why) in snapshot_meta.items():
            included.append(
                {"path": entry, "kind": kind, "why": why}
            )

        manifest = {
            "bundle_format_version": BUNDLE_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "db_schema_version": schema_version,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "created_at_ms": _now_ms(),
            "counts": counts,
            "included": included,
            "excluded": _excluded_list(),
        }

        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                MANIFEST_ENTRY,
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
            bundle.write(snapshot_path, DATABASE_ENTRY)
            for entry, payload in snapshots.items():
                bundle.writestr(
                    entry,
                    json.dumps(payload, indent=2, ensure_ascii=False),
                )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "path": dest_path,
        "size_bytes": dest_path.stat().st_size,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Validation (shared by validate + restore; touches nothing live)
# ---------------------------------------------------------------------------


def _check_entry_name(name: str) -> None:
    """Reject zip-slip / absolute entry names outright."""
    normalised = name.replace("\\", "/")
    if (
        normalised.startswith("/")
        or normalised.startswith("../")
        or "/../" in normalised
        or normalised.endswith("/..")
        or ":" in normalised.split("/")[0]
    ):
        raise BackupError(
            422,
            "backup_corrupt",
            "This backup file is damaged or is not a YouTube Agent "
            f"backup (unsafe entry name {name!r}). Nothing was changed.",
        )


def _newer_schema_error(bundle_schema: int) -> BackupError:
    return BackupError(
        409,
        "backup_from_newer_app",
        "This backup was made by a newer version of the app "
        f"(database schema {bundle_schema}) than this app supports "
        f"(schema {CURRENT_DB_SCHEMA_VERSION}). Update the app to "
        "restore it — nothing was changed.",
        details={
            "bundle_db_schema_version": bundle_schema,
            "supported_db_schema_version": CURRENT_DB_SCHEMA_VERSION,
        },
    )


def validate_bundle(bundle_path: Path) -> dict:
    """Validate a bundle file and return its manifest.

    Checks zip integrity, entry safety, the manifest's presence and
    shape, the bundle format version, and BOTH schema-version gates
    (the manifest's claim and the bundled database's own recorded
    version, which must agree). Raises :class:`BackupError` on any
    problem; newer-schema bundles get the downgrade-protection error.
    """
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        raise BackupError(
            422, "backup_corrupt", "No backup file was received."
        )
    if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
        raise BackupError(
            413,
            "backup_too_large",
            "This backup file is too large to restore safely. "
            "Nothing was changed.",
        )
    try:
        archive = zipfile.ZipFile(bundle_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(
            422,
            "backup_corrupt",
            "This file is not a valid backup bundle (it could not be "
            "opened as a zip archive). Nothing was changed.",
        ) from exc

    with archive:
        bad = archive.testzip()
        if bad is not None:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup file is damaged (a stored file failed its "
                "integrity check). Nothing was changed.",
            )
        names = archive.namelist()
        for name in names:
            _check_entry_name(name)
        if MANIFEST_ENTRY not in names:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup is missing its manifest, so the app "
                "cannot verify what it contains. Nothing was changed.",
            )
        if DATABASE_ENTRY not in names:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup does not contain a database. "
                "Nothing was changed.",
            )
        try:
            manifest = json.loads(
                archive.read(MANIFEST_ENTRY).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError, KeyError) as exc:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup's manifest could not be read. "
                "Nothing was changed.",
            ) from exc

    if not isinstance(manifest, dict):
        raise BackupError(
            422,
            "backup_corrupt",
            "This backup's manifest is not valid. Nothing was changed.",
        )

    format_version = manifest.get("bundle_format_version")
    if not isinstance(format_version, int) or format_version < 1:
        raise BackupError(
            422,
            "backup_corrupt",
            "This backup's manifest does not state a valid bundle "
            "format version. Nothing was changed.",
        )
    if format_version > BUNDLE_FORMAT_VERSION:
        raise BackupError(
            409,
            "backup_from_newer_app",
            "This backup was made by a newer version of the app "
            f"(bundle format {format_version}) than this app supports "
            f"(format {BUNDLE_FORMAT_VERSION}). Update the app to "
            "restore it — nothing was changed.",
            details={
                "bundle_format_version": format_version,
                "supported_format_version": BUNDLE_FORMAT_VERSION,
            },
        )

    bundle_schema = manifest.get("db_schema_version")
    if not isinstance(bundle_schema, int) or bundle_schema < 0:
        raise BackupError(
            422,
            "backup_corrupt",
            "This backup's manifest does not state a valid database "
            "schema version. Nothing was changed.",
        )
    if bundle_schema > CURRENT_DB_SCHEMA_VERSION:
        raise _newer_schema_error(bundle_schema)

    # The bundled database must agree with the manifest and pass its
    # own integrity check — a manifest that lies about the DB is
    # treated as corruption, never trusted.
    db_bytes_info = _manifest_db_claim(manifest)
    _verify_bundled_database(bundle_path, bundle_schema, db_bytes_info)
    return manifest


def _manifest_db_claim(manifest: dict) -> dict:
    """The manifest's {sha256, size_bytes} claim for the DB entry."""
    for item in manifest.get("included") or []:
        if isinstance(item, dict) and item.get("path") == DATABASE_ENTRY:
            return item
    return {}


def _extract_database(bundle_path: Path, dest_path: Path) -> None:
    """Stream the bundle's DB entry out to ``dest_path`` (size-capped)."""
    with zipfile.ZipFile(bundle_path) as archive:
        info = archive.getinfo(DATABASE_ENTRY)
        if info.file_size > MAX_DATABASE_BYTES:
            raise BackupError(
                413,
                "backup_too_large",
                "The database inside this backup is too large to "
                "restore safely. Nothing was changed.",
            )
        with archive.open(info) as src, dest_path.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)


def _verify_bundled_database(
    bundle_path: Path, claimed_schema: int, claim: dict
) -> None:
    staging = Path(tempfile.mkdtemp(prefix="yta-backup-verify-"))
    try:
        extracted = staging / "app.db"
        _extract_database(bundle_path, extracted)
        if claim.get("size_bytes") is not None and (
            extracted.stat().st_size != claim["size_bytes"]
        ):
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup's database does not match its manifest "
                "(size differs). Nothing was changed.",
            )
        if claim.get("sha256") and _sha256_file(extracted) != claim["sha256"]:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup's database does not match its manifest "
                "(checksum differs), so the file is damaged or was "
                "modified. Nothing was changed.",
            )
        try:
            conn = sqlite3.connect(f"file:{extracted}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise BackupError(
                422,
                "backup_corrupt",
                "The database inside this backup could not be opened. "
                "Nothing was changed.",
            ) from exc
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupError(
                    422,
                    "backup_corrupt",
                    "The database inside this backup failed its "
                    "integrity check. Nothing was changed.",
                )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "channels" not in tables:
                raise BackupError(
                    422,
                    "backup_corrupt",
                    "The database inside this backup is not a YouTube "
                    "Agent database (its core tables are missing). "
                    "Nothing was changed.",
                )
            actual_schema = database_schema_version(conn)
        finally:
            conn.close()
        if actual_schema > CURRENT_DB_SCHEMA_VERSION:
            raise _newer_schema_error(actual_schema)
        if actual_schema != claimed_schema:
            raise BackupError(
                422,
                "backup_corrupt",
                "This backup's manifest does not match the database "
                "inside it (schema versions differ), so it cannot be "
                "trusted. Nothing was changed.",
                details={
                    "manifest_db_schema_version": claimed_schema,
                    "database_schema_version": actual_schema,
                },
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def _safety_copy(db_path: Path, data_dir: Path) -> Optional[Path]:
    """Snapshot the pre-restore DB into ``<data_dir>/backups/``."""
    if not db_path.is_file():
        return None
    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"pre-restore-{_utc_stamp()}.db"
    counter = 1
    while target.exists():
        target = backups_dir / f"pre-restore-{_utc_stamp()}-{counter}.db"
        counter += 1
    _snapshot_database(db_path, target)
    return target


def _ensure_restored_schema(db_path: Path) -> None:
    """Run the app's idempotent schema ensures over a restored DB.

    Older backups may predate later tables; the base DDL (via
    :func:`app.db.init_db`) plus the lazy per-feature ensures recreate
    anything missing — the same pattern the app uses on a normal boot.
    """
    conn = db_module.init_db(db_path)
    try:
        for module_name, func_name in (
            ("app.style_lab.schema", "ensure_style_lab_schema"),
        ):
            try:
                import importlib

                module = importlib.import_module(module_name)
                getattr(module, func_name)(conn)
            except Exception:
                pass  # the owning feature ensures its own tables lazily
    finally:
        conn.close()


def restore_backup(
    bundle_path: Path,
    data_dir: Path,
    db_path: Path,
    *,
    before_install: Optional[Callable[[], None]] = None,
) -> dict:
    """Restore ``bundle_path`` over the live database.

    Validates EVERYTHING before touching live data, keeps a safety
    copy of the pre-restore DB, then installs the bundled DB with an
    atomic rename. ``before_install`` (optional) runs after validation
    and the safety copy, immediately before the swap — the API layer
    uses it to close the app's own long-lived DB connection so the
    rename also succeeds on Windows.

    Returns a summary dict; raises :class:`BackupError` on refusal.
    """
    bundle_path = Path(bundle_path)
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    manifest = validate_bundle(bundle_path)

    staging = Path(tempfile.mkdtemp(prefix="yta-backup-restore-"))
    try:
        extracted = staging / "app.db"
        _extract_database(bundle_path, extracted)

        safety_copy = _safety_copy(db_path, data_dir)

        if before_install is not None:
            before_install()

        # Checkpoint any WAL on the outgoing DB and drop its sidecars:
        # a stale -wal replayed onto the restored file would corrupt it.
        try:
            conn = db_module.connect(db_path)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except Exception:
            pass

        db_path.parent.mkdir(parents=True, exist_ok=True)
        install_tmp = db_path.parent / f".app.db.restore-{os.getpid()}.tmp"
        shutil.copyfile(extracted, install_tmp)
        with install_tmp.open("ab") as fh:
            os.fsync(fh.fileno())
        try:
            os.replace(install_tmp, db_path)
        except OSError as exc:
            raise BackupError(
                409,
                "backup_restore_failed",
                "The backup was valid, but the app could not replace "
                "its current database (it may be in use). Your "
                "existing data was not changed"
                + (
                    f"; a safety copy is at {safety_copy.name}."
                    if safety_copy is not None
                    else "."
                ),
            ) from exc
        finally:
            try:
                install_tmp.unlink(missing_ok=True)
            except OSError:
                pass
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _ensure_restored_schema(db_path)

    conn = db_module.connect(db_path)
    try:
        counts = {
            "channels": _row_count(conn, "channels"),
            "schedules": _row_count(conn, "schedules"),
            "style_profiles": _row_count(conn, "style_profiles"),
        }
    finally:
        conn.close()

    return {
        "restored": True,
        "bundle_format_version": manifest["bundle_format_version"],
        "db_schema_version": manifest["db_schema_version"],
        "supported_db_schema_version": CURRENT_DB_SCHEMA_VERSION,
        "safety_copy": safety_copy.name if safety_copy is not None else None,
        "counts": counts,
    }
