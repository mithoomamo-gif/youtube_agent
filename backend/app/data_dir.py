"""Canonical data-directory resolution (Round 6, Windows packaging).

ONE function owns the question "where does this app keep its data?":
:func:`resolve_data_dir`.  Every other module that needs the data root
(``app.config``, ``app.db``, the file-based storage seams, the Style
Lab claimer, reporting, …) must derive it from here — directly or via
``app.config.Settings.data_dir`` — instead of re-implementing
platform checks, ``APPDATA`` lookups or ``Path.home()`` fallbacks.

Contract (see ``docs/WINDOWS_DATA.md`` for the full layout spec):

1. ``YTA_DATA_DIR`` — explicit override, honoured on EVERY platform,
   verbatim (after ``~`` expansion).  The packaged Windows runtime
   (Tauri shell / freeze) sets this; tests set it; nothing else may
   second-guess it.
2. Windows (``os.name == "nt"`` / ``win32``, or an explicit
   ``platform="windows"``): ``%APPDATA%\\YouTubeAgent``.  When
   ``APPDATA`` is absent from the environment the roaming profile is
   reconstructed as ``<home>/AppData/Roaming/YouTubeAgent``.
3. Everywhere else (dev / Linux / macOS): the historical default
   ``~/.youtube-agent`` — UNCHANGED from Rounds 1–5, so existing dev
   data stays exactly where it is.  For backward compatibility an
   ``APPDATA`` value present in the environment is still honoured on
   non-Windows platforms (that is what ``app.config`` did before this
   module existed, and it is how Windows behaviour is simulated in
   tests on this Linux VM).

Nothing here creates directories, moves data, or reads the filesystem;
resolution is a pure function of the environment mapping, the platform
name and the home directory, all injectable for tests.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "APP_DIR_NAME",
    "DATA_DIR_ENV_VAR",
    "DEV_DIR_NAME",
    "DataDirLayout",
    "data_dir_layout",
    "explicit_data_dir",
    "is_windows_platform",
    "resolve_data_dir",
]

#: Environment variable that overrides the data root on every platform.
DATA_DIR_ENV_VAR = "YTA_DATA_DIR"

#: Directory name under ``%APPDATA%`` (and the layout's display name).
APP_DIR_NAME = "YouTubeAgent"

#: Historical dev/Linux directory name under the home directory.
DEV_DIR_NAME = ".youtube-agent"

_WINDOWS_TOKENS = {"windows", "win32", "win", "nt"}


def is_windows_platform(platform: str | None = None) -> bool:
    """True when ``platform`` (default: the running interpreter) is Windows.

    ``platform`` accepts the spellings callers/tests naturally use:
    ``"windows"``, ``"win32"``, ``"win"``, ``"nt"`` (case-insensitive).
    Anything else (``"linux"``, ``"posix"``, ``"darwin"``, …) is not
    Windows.  With no argument the running platform decides
    (``os.name`` / ``sys.platform``).
    """
    if platform is None:
        return os.name == "nt" or sys.platform.startswith("win")
    return platform.strip().lower() in _WINDOWS_TOKENS


def _clean(value: str | None) -> str:
    return (value or "").strip()


def resolve_data_dir(
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | str | None = None,
) -> Path:
    """Resolve the app's data root — THE canonical resolver.

    Parameters
    ----------
    env:
        Environment mapping to read (default: ``os.environ``).  Pass an
        explicit mapping in tests; values are read from the mapping
        only, never mixed with the real environment.
    platform:
        Platform override for simulation/testing (see
        :func:`is_windows_platform`).  Default: the running platform.
    home:
        Home-directory override for simulation/testing.  Default:
        ``Path.home()``.

    Resolution order: ``YTA_DATA_DIR`` override → Windows
    ``%APPDATA%\\YouTubeAgent`` → dev default ``~/.youtube-agent``
    (with the legacy ``APPDATA`` honouring described in the module
    docstring).
    """
    source: Mapping[str, str] = os.environ if env is None else env

    override = _clean(source.get(DATA_DIR_ENV_VAR))
    if override:
        return Path(override).expanduser()

    home_path = Path(home) if home is not None else Path.home()
    appdata = _clean(source.get("APPDATA"))

    if is_windows_platform(platform):
        if appdata:
            return Path(appdata).expanduser() / APP_DIR_NAME
        return home_path / "AppData" / "Roaming" / APP_DIR_NAME

    # Non-Windows (dev/Linux/macOS): preserve the Rounds 1–5 behaviour
    # exactly — APPDATA, when present, wins; otherwise ~/.youtube-agent.
    if appdata:
        return Path(appdata).expanduser() / APP_DIR_NAME
    return home_path / DEV_DIR_NAME


def explicit_data_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The ``YTA_DATA_DIR`` override ONLY, or ``None`` when not set.

    Some call sites (caches and model stores that historically lived
    inside the repo checkout) must distinguish "the operator/packaged
    shell explicitly chose a data root" from "dev default": when the
    override is set they root under it; when it is not, their
    historical dev location is preserved byte-for-byte.  This helper
    is that distinction — the ONE place it is made.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    override = _clean(source.get(DATA_DIR_ENV_VAR))
    if override:
        return Path(override).expanduser()
    return None


@dataclass(frozen=True)
class DataDirLayout:
    """The standard sub-paths under the data root.

    Mirrors ``docs/WINDOWS_DATA.md``.  Pure path arithmetic — creating
    these directories stays the caller's job, exactly as before.
    """

    root: Path

    @property
    def db_path(self) -> Path:
        """Main SQLite database: ``<root>/app.db``."""
        return self.root / "app.db"

    @property
    def idempotency_db_path(self) -> Path:
        return self.root / "idempotency.db"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def projects_dir(self) -> Path:
        """Per-project working trees (pipeline stage checkpoints)."""
        return self.root / "projects"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def style_lab_dir(self) -> Path:
        """Style Lab intake attachments (underscore package)."""
        return self.root / "style_lab"

    @property
    def stylelab_dir(self) -> Path:
        """Style Lab analysis results (no-underscore package)."""
        return self.root / "stylelab"

    @property
    def comment_intel_dir(self) -> Path:
        return self.root / "comment_intel"

    @property
    def analytics_dir(self) -> Path:
        return self.root / "analytics"

    @property
    def retention_sections_dir(self) -> Path:
        return self.analytics_dir / "retention_sections"

    @property
    def tmp_dir(self) -> Path:
        """App-private scratch/spool space under the data root."""
        return self.root / "tmp"

    @property
    def cache_dir(self) -> Path:
        """Downloadable/regenerable caches under the data root."""
        return self.root / "cache"

    @property
    def stock_cache_dir(self) -> Path:
        """Stock-footage cache: ``<root>/cache/stock``."""
        return self.cache_dir / "stock"

    @property
    def models_dir(self) -> Path:
        """Downloaded model weights under the data root."""
        return self.root / "models"

    @property
    def whisper_model_dir(self) -> Path:
        """faster-whisper weights: ``<root>/models/whisper``."""
        return self.models_dir / "whisper"


def data_dir_layout(
    root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | str | None = None,
) -> DataDirLayout:
    """Layout for ``root`` (default: :func:`resolve_data_dir`)."""
    resolved = Path(root) if root is not None else resolve_data_dir(
        env, platform=platform, home=home
    )
    return DataDirLayout(root=resolved)
