"""Backup export / restore endpoints (Round 6, roadmap: data root +
user-facing export under Settings -> Data & Privacy).

Routes (all under the launch-token middleware, like every ``/api/v1``
route):

* ``GET  /api/v1/settings/backup``         — what a backup contains +
  the versions this build can restore (no file created).
* ``GET  /api/v1/settings/backup/export`` — download one zip bundle
  (see :mod:`app.backup` for the bundle format and the manifest's
  explicit included/excluded list).  Secrets are never in a bundle:
  the database holds only secret-store *references*, never values.
* ``POST /api/v1/settings/backup/import`` — multipart upload (field
  ``file``) + restore.  Validation is total and happens before any
  live data is touched; a backup from a NEWER app/schema is refused
  with 409 ``backup_from_newer_app`` and the downgrade-protection
  message — it is never partially applied.  A safety copy of the
  pre-restore database is kept in ``<data_dir>/backups/``.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from app.backup import (
    BUNDLE_FORMAT_VERSION,
    CURRENT_DB_SCHEMA_VERSION,
    MAX_BUNDLE_BYTES,
    BackupError,
    build_export,
    restore_backup,
)
from app.errors import ApiError

router = APIRouter(prefix="/api/v1/settings/backup", tags=["backup"])


def _raise(err: BackupError) -> None:
    raise ApiError(err.status_code, err.code, err.message, details=err.details)


def _settings(request: Request):
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings
    from app.config import get_settings

    return get_settings()


def _export_filename() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"youtube-agent-backup-{stamp}.zip"


@router.get("")
async def backup_info(request: Request) -> dict:
    """Backup/restore capability summary for the Settings UI."""
    settings = _settings(request)
    return {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "supported_db_schema_version": CURRENT_DB_SCHEMA_VERSION,
        "database_present": Path(settings.db_path).is_file(),
        "includes": [
            "database (channels, schedules, projects, settings, ledgers)",
            "channel configurations",
            "style profiles and competitor metadata",
        ],
        "excludes": [
            "API keys and OAuth tokens (they stay in the credential store)",
            "competitor video files (metadata only)",
            "caches, logs and rendered videos",
        ],
    }


@router.get("/export")
async def export_backup(request: Request):
    """Build a backup bundle of the live data and download it as a zip."""
    settings = _settings(request)
    import os

    # Stage in the data dir's backups folder, not the OS temp dir —
    # a bundle mirrors the database and can be large.
    staging_dir = Path(settings.data_dir) / "backups"
    staging_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="yta-backup-", suffix=".zip", dir=staging_dir
    )
    os.close(fd)
    bundle_path = Path(tmp_name)
    try:
        result = build_export(Path(settings.db_path), bundle_path)
    except BackupError as err:
        bundle_path.unlink(missing_ok=True)
        _raise(err)

    from starlette.background import BackgroundTask

    return FileResponse(
        result["path"],
        media_type="application/zip",
        filename=_export_filename(),
        background=BackgroundTask(
            lambda: bundle_path.unlink(missing_ok=True)
        ),
    )


@router.post("/import")
async def import_backup(
    request: Request, file: UploadFile = File(...)
) -> dict:
    """Upload a backup bundle and restore it over the live data.

    The app's own long-lived DB connection is closed just before the
    file swap (and reopened right after) so the atomic rename also
    works on Windows, where an open file cannot be replaced.
    """
    settings = _settings(request)
    data_dir = Path(settings.data_dir)
    db_path = Path(settings.db_path)

    incoming_dir = data_dir / "backups"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="incoming-", suffix=".zip", dir=incoming_dir
    )
    tmp_path = Path(tmp_name)
    try:
        written = 0
        with open(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BUNDLE_BYTES:
                    raise ApiError(
                        413,
                        "backup_too_large",
                        "This backup file is too large to restore "
                        "safely. Nothing was changed.",
                    )
                out.write(chunk)

        app = request.app

        def _close_live_connection() -> None:
            conn = getattr(app.state, "db", None)
            if conn is not None:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                app.state.db = None

        try:
            summary = restore_backup(
                tmp_path,
                data_dir,
                db_path,
                before_install=_close_live_connection,
            )
        except BackupError as err:
            _raise(err)
        finally:
            # Reopen the app's DB handle on whatever database is live
            # now (restored on success, untouched on refusal).
            if getattr(app.state, "db", None) is None:
                try:
                    from app import db as db_module

                    app.state.db = db_module.connect(db_path)
                except Exception:
                    app.state.db = None
        return summary
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
