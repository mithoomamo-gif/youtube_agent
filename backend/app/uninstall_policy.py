"""Uninstall data policy (Round 6 — Windows packaging + hardening).

Policy (roadmap §26.7 / RELEASE.md uninstall matrix):

* Uninstalling the app **preserves user data by default**.  The data
  root (Windows: ``%APPDATA%\\YouTubeAgent``) — channels, projects and
  video history, Style Lab profiles, settings, the database and logs —
  survives an uninstall so a reinstall/upgrade picks up where the
  creator left off.
* Data is purged on uninstall **only** when the user explicitly opted
  in via the Settings toggle "Remove all my data on uninstall"
  (settings flag ``privacy.remove_data_on_uninstall``, default OFF).

Mechanism (agreed convention, Round 6): the backend owns a marker
file named ``uninstall-purge.marker`` in the data root.  The toggle
being ON writes the marker; turning it OFF removes it.  The NSIS
uninstaller hook (shell/packaging team, ``NSIS_HOOK_POSTUNINSTALL`` in
``src-tauri/windows/hooks.nsh``) checks for the marker's *existence*
at uninstall time and, only when present, purges the data directory
after the app itself has been removed.  Nothing in this module ever
deletes user data itself — it only writes/removes the marker.

Rendered/exported video files live outside the data root (default
``%USERPROFILE%\\Videos\\YouTubeAgent``) and are never covered by the
purge — see ``docs/BUILD.md`` / ``docs/RELEASE.md``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: Marker filename in the data root.  The NSIS uninstaller hook looks
#: for exactly this name — do not rename without the shell team.
MARKER_FILENAME = "uninstall-purge.marker"

#: Settings-table key for the opt-in flag.  Stored via the generic
#: settings store (non-secret key/value) and exposed typed via
#: ``GET|PUT /api/v1/settings/privacy``.
SETTINGS_KEY = "privacy.remove_data_on_uninstall"

_MARKER_BODY = """\
YouTube Agent — uninstall purge opt-in marker.

The user turned ON "Remove all my data on uninstall"
(setting: {settings_key}) in the app's Settings.

If you are the uninstaller: the presence of this file is the user's
explicit instruction to permanently delete this data folder
(channels, projects, Style Lab profiles, settings, database and
logs) after the app itself has been removed. If this file is absent,
PRESERVE the data folder — that is the default.

Written by the YouTube Agent backend at {written_at} (UTC).
"""


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    """Resolve the app data root.

    Priority: explicit argument -> ``app.config`` settings (which
    honour ``YTA_DATA_DIR`` / ``%APPDATA%\\YouTubeAgent``) -> the
    ``YTA_DATA_DIR`` env var -> platform default.  Mirrors the
    resolution used for the database path in ``app.db``.
    """
    if data_dir is not None:
        return Path(data_dir).expanduser()
    try:
        from app.config import get_settings

        return Path(get_settings().data_dir)
    except Exception:
        pass
    try:
        # Canonical Round-6 resolver (app.data_dir owns data-root
        # resolution for the whole app; keep in lockstep with it).
        from app.data_dir import resolve_data_dir as _canonical_resolve

        return Path(_canonical_resolve())
    except Exception:
        pass
    env_dir = os.environ.get("YTA_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "YouTubeAgent"
    return Path.home() / ".youtube-agent"


def marker_path(data_dir: Path | str | None = None) -> Path:
    """Absolute path of the uninstall-purge marker in the data root."""
    return resolve_data_dir(data_dir) / MARKER_FILENAME


def flag_enabled(settings_map: Mapping[str, Any] | None) -> bool:
    """True only when the persisted flag is exactly boolean ``True``.

    Anything else (missing key on a fresh profile, ``False``, a stray
    non-boolean value) means the default: preserve data, no purge.
    """
    if not settings_map:
        return False
    return settings_map.get(SETTINGS_KEY) is True


def marker_present(data_dir: Path | str | None = None) -> bool:
    try:
        return marker_path(data_dir).is_file()
    except OSError:
        return False


def sync_marker(
    enabled: bool, data_dir: Path | str | None = None
) -> Path | None:
    """Make the marker match ``enabled``.

    ON  -> write the marker into the data root (creating the root if
           needed) and return its path.
    OFF -> remove the marker if present and return ``None``.

    Filesystem failures are swallowed by callers at the API layer only
    in the sense that the *persisted flag* remains the record of the
    user's choice and startup reconciliation (see
    :func:`reconcile_marker`) re-applies it; this function itself
    raises so tests and direct callers see real errors.
    """
    path = marker_path(data_dir)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = _MARKER_BODY.format(
            settings_key=SETTINGS_KEY,
            written_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        path.write_text(body, encoding="utf-8")
        return path
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return None


def reconcile_marker(
    settings_map: Mapping[str, Any] | None,
    data_dir: Path | str | None = None,
) -> bool:
    """Re-apply the persisted flag to the filesystem (startup repair).

    Returns the effective enabled state.  Never raises: a broken data
    dir must not block app startup — the flag stays persisted and the
    next successful sync/reconcile restores the marker.
    """
    enabled = flag_enabled(settings_map)
    try:
        sync_marker(enabled, data_dir)
    except OSError:
        pass
    return enabled


__all__ = [
    "MARKER_FILENAME",
    "SETTINGS_KEY",
    "flag_enabled",
    "marker_path",
    "marker_present",
    "reconcile_marker",
    "resolve_data_dir",
    "sync_marker",
]
