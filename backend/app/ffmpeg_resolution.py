"""FFmpeg / ffprobe runtime resolution — THE resolution contract.

Round 6 (Sidecar Freeze + Media Bundling). This module is the ONE place
the backend decides which ``ffmpeg`` / ``ffprobe`` binary to execute.
Every pipeline call site must obtain its binary from here (directly, or
via a default argument bound to :func:`ffmpeg_executable` /
:func:`ffprobe_executable`) — never hard-code a bare ``"ffmpeg"`` and
hope the user's PATH cooperates. That matters most in the frozen
(PyInstaller) product, where the sidecar may be launched with a minimal
or unrelated PATH.

RESOLUTION ORDER (per binary, first hit wins)
---------------------------------------------
1. **Explicit environment override**

   * ``YTA_FFMPEG_PATH``  — full path to an ``ffmpeg`` executable
   * ``YTA_FFPROBE_PATH`` — full path to an ``ffprobe`` executable

   (The master roadmap names no env var for this; the ``YTA_*`` launch
   namespace of ``app.config`` is the established convention, so these
   names extend it. A set-but-missing/non-file override is logged at
   WARNING and ignored — a typo must not brick rendering when a bundled
   binary exists.)

2. **Bundled binary, ``sys.executable``-relative** (NEVER CWD-relative —
   the frozen sidecar's working directory is arbitrary). Candidates, in
   order, for anchor ``<dir of sys.executable>``:

   * ``<anchor>/runtime/<name>``   — the roadmap Part 11 install layout:
     ``%LOCALAPPDATA%\\Programs\\YouTubeAgent\\agent-backend.exe`` with
     ``runtime\\ffmpeg.exe`` + ``runtime\\ffprobe.exe`` beside it.
   * ``<anchor>/<name>``           — flat sidecar layout (binary staged
     directly next to the sidecar executable).
   * ``<anchor>/../runtime/<name>`` and ``<anchor>/../../runtime/<name>``
     — versioned backend-release layouts (Part 11 backend channel:
     sidecar inside a versioned release dir, shared ``runtime\\`` one or
     two levels up, e.g. ``releases\\<ver>\\`` under the install root).

   On Windows ``<name>`` is ``ffmpeg.exe`` / ``ffprobe.exe``; elsewhere
   it is ``ffmpeg`` / ``ffprobe``.

3. **System PATH** via :func:`shutil.which`.

If nothing resolves, the ``*_executable()`` helpers fall back to the
bare name (``"ffmpeg"`` / ``"ffprobe"``) so callers keep their existing
"binary not found" error behaviour instead of a new exception type;
:func:`resolve_ffmpeg` / :func:`resolve_ffprobe` return ``None`` for
callers (probes, diagnostics) that need the honest answer.

PACKAGING CONTRACT (for the shell / packaging workers)
------------------------------------------------------
* ``packaging/fetch_ffmpeg.sh`` / ``fetch_ffmpeg.ps1`` download the
  pinned build (see ``packaging/README.md``) into
  ``packaging/vendor/ffmpeg/``.
* The installer / sidecar staging step copies those binaries to the
  ``runtime\\`` directory beside ``agent-backend.exe`` (candidate 1
  above). No registry entries, no PATH mutation, no CWD assumptions.
* The bundled build MUST ship the ``loudnorm`` filter — the render
  pipeline loudness-normalises to −14 LUFS with it (roadmap Part 6.7).
  :func:`probe_loudnorm` is the first-run capability probe that
  verifies this against the *resolved* binary and logs the result.

This module is stdlib-only and imports nothing from ``app`` — it sits
below every pipeline package and must stay import-cycle-free.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger("app.ffmpeg_resolution")

#: Environment override variable names (see module docstring, rule 1).
FFMPEG_ENV_VAR = "YTA_FFMPEG_PATH"
FFPROBE_ENV_VAR = "YTA_FFPROBE_PATH"

_ENV_VARS = {"ffmpeg": FFMPEG_ENV_VAR, "ffprobe": FFPROBE_ENV_VAR}

#: How long a capability probe may run before it counts as failed.
PROBE_TIMEOUT_S = 30.0


def _exe_name(kind: str, platform: str) -> str:
    return f"{kind}.exe" if platform.startswith("win") else kind


def bundled_candidates(
    kind: str,
    *,
    anchor: Optional[Path] = None,
    platform: Optional[str] = None,
) -> list[Path]:
    """Ordered bundled locations for ``kind`` relative to the executable.

    ``anchor`` defaults to the directory of ``sys.executable`` — in the
    frozen product that is the sidecar's own directory; under a dev venv
    it is the venv's ``bin/``, where (correctly) nothing is bundled and
    resolution falls through to PATH.
    """
    plat = sys.platform if platform is None else platform
    name = _exe_name(kind, plat)
    base = Path(sys.executable).resolve().parent if anchor is None else Path(anchor)
    return [
        base / "runtime" / name,
        base / name,
        base.parent / "runtime" / name,
        base.parent.parent / "runtime" / name,
    ]


def resolve_binary(
    kind: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    anchor: Optional[Path] = None,
    platform: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[Path]:
    """Resolve ``kind`` (``"ffmpeg"`` | ``"ffprobe"``) per the contract.

    Returns the winning path, or ``None`` when no source yields an
    existing file. ``env`` / ``anchor`` / ``which`` are injectable for
    tests and for the frozen-layout simulation.
    """
    if kind not in _ENV_VARS:
        raise ValueError(f"unknown media tool kind: {kind!r}")
    source: Mapping[str, str] = os.environ if env is None else env

    # 1. Explicit environment override.
    override = (source.get(_ENV_VARS[kind]) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
        logger.warning(
            "%s=%s does not point at an existing file; ignoring override",
            _ENV_VARS[kind],
            override,
        )

    # 2. Bundled, sys.executable-relative.
    for candidate in bundled_candidates(kind, anchor=anchor, platform=platform):
        if candidate.is_file():
            return candidate

    # 3. System PATH.
    plat = sys.platform if platform is None else platform
    on_path = which(_exe_name(kind, plat))
    if on_path:
        return Path(on_path)
    return None


def resolve_ffmpeg(**kwargs) -> Optional[Path]:
    """Resolved ``ffmpeg`` path, or ``None`` (see :func:`resolve_binary`)."""
    return resolve_binary("ffmpeg", **kwargs)


def resolve_ffprobe(**kwargs) -> Optional[Path]:
    """Resolved ``ffprobe`` path, or ``None`` (see :func:`resolve_binary`)."""
    return resolve_binary("ffprobe", **kwargs)


def ffmpeg_executable() -> str:
    """Resolved ``ffmpeg`` as a command string; bare ``"ffmpeg"`` if unresolved."""
    resolved = resolve_ffmpeg()
    return str(resolved) if resolved is not None else "ffmpeg"


def ffprobe_executable() -> str:
    """Resolved ``ffprobe`` as a command string; bare ``"ffprobe"`` if unresolved."""
    resolved = resolve_ffprobe()
    return str(resolved) if resolved is not None else "ffprobe"


# ---------------------------------------------------------------------------
# Loudnorm capability probe (first run)
# ---------------------------------------------------------------------------

_FILTER_LINE_RE = re.compile(r"^\s*\S+\s+(?P<name>[A-Za-z0-9_]+)\s+")


def parse_filter_names(filters_output: str) -> set[str]:
    """Filter names from ``ffmpeg -filters`` output (pure; testable)."""
    names: set[str] = set()
    for line in filters_output.splitlines():
        match = _FILTER_LINE_RE.match(line)
        if match:
            names.add(match.group("name"))
    return names


def has_loudnorm(filters_output: str) -> bool:
    """True iff the ``loudnorm`` filter appears in ``-filters`` output."""
    return "loudnorm" in parse_filter_names(filters_output)


@dataclass
class LoudnormProbeResult:
    """Recorded outcome of the first-run loudnorm capability probe."""

    ffmpeg: Optional[str]  # resolved binary that was probed (None = not found)
    found: bool  # a binary was resolved AND executed successfully
    loudnorm_present: bool
    version: Optional[str] = None  # first line of `ffmpeg -version`
    error: Optional[str] = None  # why the probe could not complete
    filters_seen: int = 0  # count of parsed filters (sanity signal)

    def as_dict(self) -> dict:
        return {
            "ffmpeg": self.ffmpeg,
            "found": self.found,
            "loudnorm_present": self.loudnorm_present,
            "version": self.version,
            "error": self.error,
            "filters_seen": self.filters_seen,
        }


def probe_loudnorm(
    ffmpeg: Optional[str | Path] = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = PROBE_TIMEOUT_S,
) -> LoudnormProbeResult:
    """Probe the resolved ffmpeg for the ``loudnorm`` filter; log the result.

    The render pipeline depends on loudnorm (−14 LUFS mastering, roadmap
    Part 6.7), so a build without it must be discovered at first run,
    not mid-render. Never raises: every failure mode is recorded in the
    returned result and logged (INFO when healthy, WARNING otherwise).
    """
    resolved = Path(ffmpeg) if ffmpeg is not None else resolve_ffmpeg()
    if resolved is None:
        result = LoudnormProbeResult(
            ffmpeg=None,
            found=False,
            loudnorm_present=False,
            error="ffmpeg not resolved (no env override, no bundled binary, not on PATH)",
        )
        logger.warning("ffmpeg capability probe: %s", result.error)
        return result

    binary = str(resolved)
    try:
        version_proc = runner(
            [binary, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        version_line = (version_proc.stdout or "").splitlines()[0].strip() if version_proc.stdout else None
        filters_proc = runner(
            [binary, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result = LoudnormProbeResult(
            ffmpeg=binary,
            found=False,
            loudnorm_present=False,
            error=f"ffmpeg probe execution failed: {exc}",
        )
        logger.warning("ffmpeg capability probe: %s", result.error)
        return result

    filters_output = filters_proc.stdout or ""
    names = parse_filter_names(filters_output)
    present = "loudnorm" in names
    result = LoudnormProbeResult(
        ffmpeg=binary,
        found=filters_proc.returncode == 0,
        loudnorm_present=present,
        version=version_line,
        error=None if filters_proc.returncode == 0 else f"ffmpeg -filters exited {filters_proc.returncode}",
        filters_seen=len(names),
    )
    if present:
        logger.info(
            "ffmpeg capability probe: loudnorm PRESENT (binary=%s, version=%s)",
            binary,
            version_line,
        )
    else:
        logger.warning(
            "ffmpeg capability probe: loudnorm MISSING (binary=%s, version=%s) "
            "— the render pipeline cannot loudness-normalise with this build",
            binary,
            version_line,
        )
    return result


__all__ = [
    "FFMPEG_ENV_VAR",
    "FFPROBE_ENV_VAR",
    "PROBE_TIMEOUT_S",
    "LoudnormProbeResult",
    "bundled_candidates",
    "ffmpeg_executable",
    "ffprobe_executable",
    "has_loudnorm",
    "parse_filter_names",
    "probe_loudnorm",
    "resolve_binary",
    "resolve_ffmpeg",
    "resolve_ffprobe",
]
