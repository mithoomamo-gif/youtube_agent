"""Launch-environment configuration for the sidecar.

The environment is the ONLY configuration channel into the backend
(roadmap §31, spawn contract). The Tauri shell generates these per launch:

============== ============================================================
Variable       Meaning
============== ============================================================
``YTA_PORT``   Port to bind on 127.0.0.1 (0/missing = pick a free port)
``YTA_TOKEN``  Per-launch token (REQUIRED — process exits 5 without it)
``YTA_BOOT_ID`` UUID for this backend process; stamps logs + WS events
``YTA_DATA_DIR`` Data dir (DB, logs, PID file). Resolved by the ONE
               canonical resolver, :func:`app.data_dir.resolve_data_dir`:
               the env override wins on every platform, else
               ``%APPDATA%\\YouTubeAgent`` on Windows, else
               ``~/.youtube-agent`` (dev default, unchanged).
``YTA_PARENT_PID`` Shell PID for the parent watchdog (None = no watchdog)
``YTA_LOG_LEVEL`` Default ``info``
``YTA_DETACHED`` 1 only for Task-Scheduler headless runs (no watchdog)
``YTA_ALLOW_DEV_ORIGINS`` 1/true only for local dev runs (set by
               ``tools/dev.ps1``): also allow the Vite dev UI origins
               ``http://localhost:1420`` / ``http://127.0.0.1:1420`` in
               the Origin/CORS guard. Unset/off everywhere else — the
               shipped sidecar never sets it, so its allowlist stays
               exactly the Tauri webview origins.
============== ============================================================
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from app.data_dir import resolve_data_dir
from app.redaction import register_secret

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


def _default_data_dir(env: Mapping[str, str]) -> Path:
    """Platform default WITHOUT the ``YTA_DATA_DIR`` override.

    Kept for backward compatibility; delegates to the canonical
    resolver (:func:`app.data_dir.resolve_data_dir`) by hiding the
    override variable, preserving this helper's historical semantics
    exactly (``APPDATA`` -> ``<APPDATA>/YouTubeAgent``, else
    ``~/.youtube-agent``).
    """
    without_override = {k: v for k, v in env.items() if k != "YTA_DATA_DIR"}
    return resolve_data_dir(without_override)


def _parse_bool(raw: str | None, name: str) -> bool:
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value (1/0/true/false), got {raw!r}")


def _parse_optional_int(raw: str | None, name: str) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _parse_int(raw: str | None, name: str, default: int) -> int:
    parsed = _parse_optional_int(raw, name)
    return default if parsed is None else parsed


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the launch environment."""

    port: int = 0
    token: str = ""
    boot_id: str = ""
    data_dir: Path = field(default_factory=resolve_data_dir)
    parent_pid: int | None = None
    log_level: str = "info"
    detached: bool = False
    allow_dev_origins: bool = False

    # -- derived paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        """SQLite database file: ``<data_dir>/app.db``."""
        return self.data_dir / "app.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def layout(self):  # -> app.data_dir.DataDirLayout
        """Full standard sub-path layout under ``data_dir``."""
        from app.data_dir import DataDirLayout

        return DataDirLayout(root=self.data_dir)

    @property
    def host(self) -> str:
        """Bind host — loopback only, by invariant (never configurable)."""
        return "127.0.0.1"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def require_token(self) -> str:
        if not self.token:
            raise ValueError("YTA_TOKEN is required but missing or empty")
        return self.token


def load_settings(env: Mapping[str, str] | None = None, *, require_token: bool = True) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    Raises :class:`ValueError` on a missing/empty ``YTA_TOKEN`` (when
    ``require_token``), or on malformed numeric/boolean values. ``app.main``
    maps the missing-token case to exit code 5.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    token = (source.get("YTA_TOKEN") or "").strip()
    if require_token and not token:
        raise ValueError("YTA_TOKEN is required but missing or empty")

    port = _parse_int(source.get("YTA_PORT"), "YTA_PORT", default=0)
    if not (0 <= port <= 65535):
        raise ValueError(f"YTA_PORT must be between 0 and 65535, got {port}")

    # The ONE canonical resolver: YTA_DATA_DIR override on every
    # platform, else %APPDATA%\YouTubeAgent on Windows, else the
    # historical dev default ~/.youtube-agent.
    data_dir = resolve_data_dir(source)

    settings = Settings(
        port=port,
        token=token,
        boot_id=(source.get("YTA_BOOT_ID") or "").strip() or uuid.uuid4().hex,
        data_dir=data_dir,
        parent_pid=_parse_optional_int(source.get("YTA_PARENT_PID"), "YTA_PARENT_PID"),
        log_level=(source.get("YTA_LOG_LEVEL") or "info").strip().lower() or "info",
        detached=_parse_bool(source.get("YTA_DETACHED"), "YTA_DETACHED"),
        allow_dev_origins=_parse_bool(
            source.get("YTA_ALLOW_DEV_ORIGINS"), "YTA_ALLOW_DEV_ORIGINS"
        ),
    )
    # Make the launch token an exact-match secret for the redaction choke point.
    register_secret(settings.token)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings, loaded from the real environment."""
    return load_settings()


def clear_settings_cache() -> None:
    """Drop the cached settings (tests / controlled restarts)."""
    get_settings.cache_clear()


__all__ = [
    "Settings",
    "clear_settings_cache",
    "get_settings",
    "load_settings",
    "resolve_data_dir",
]
