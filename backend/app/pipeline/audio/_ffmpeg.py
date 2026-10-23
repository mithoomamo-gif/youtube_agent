"""Thin subprocess helpers around the ``ffmpeg`` / ``ffprobe`` CLIs.

Everything in the audio pipeline shells out to FFmpeg (no MoviePy, per
roadmap §18): filters do the DSP, Python only builds graphs and parses the
loudnorm JSON that FFmpeg prints to stderr.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from ...ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

#: Generous per-invocation ceiling; audio stages are faster than realtime
#: for programme-length material, this only guards against a hung process.
FFMPEG_TIMEOUT_S = 1800


class FFmpegError(RuntimeError):
    """An FFmpeg/ffprobe invocation exited non-zero (stderr tail attached)."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        tail = stderr.strip().splitlines()[-15:]
        super().__init__(message + ("\n--- ffmpeg stderr (tail) ---\n" + "\n".join(tail) if tail else ""))
        self.stderr = stderr


def run_ffmpeg(args: list[str], *, timeout: int = FFMPEG_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run ``ffmpeg -y -hide_banner <args>``; raise :class:`FFmpegError` on failure."""
    cmd = [ffmpeg_executable(), "-y", "-hide_banner", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}", stderr=proc.stderr)
    return proc


def run_ffmpeg_capture(args: list[str], *, timeout: int = FFMPEG_TIMEOUT_S) -> str:
    """Like :func:`run_ffmpeg` but returns stderr (where loudnorm prints JSON)."""
    return run_ffmpeg(args, timeout=timeout).stderr


def probe_duration(path: Path) -> float:
    """Duration of a media file in seconds, via ffprobe."""
    return _probe_float(path, "format=duration")


def probe_channels(path: Path) -> int:
    """Channel count of the first audio stream, via ffprobe (0 if unknown)."""
    out = _probe_raw(
        path,
        ["-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0"],
    )
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _probe_float(path: Path, entries: str) -> float:
    out = _probe_raw(path, ["-show_entries", entries, "-of", "csv=p=0"])
    try:
        return float(out.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise FFmpegError(f"ffprobe could not read {entries} from {path}", stderr=out) from exc


def _probe_raw(path: Path, extra: list[str]) -> str:
    if not Path(path).is_file():
        raise FileNotFoundError(f"media file not found: {path}")
    cmd = [ffprobe_executable(), "-v", "error", *extra, str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed ({proc.returncode}): {' '.join(cmd)}", stderr=proc.stderr)
    return proc.stdout


_LOUDNORM_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)


def parse_loudnorm_json(stderr: str) -> dict[str, str]:
    """Extract the (last) loudnorm ``print_format=json`` block from stderr."""
    matches = _LOUDNORM_JSON_RE.findall(stderr)
    if not matches:
        raise FFmpegError("loudnorm JSON block not found in ffmpeg output", stderr=stderr)
    data = json.loads(matches[-1])
    return {str(k): str(v) for k, v in data.items()}


def loudnorm_value(raw: dict[str, str], key: str) -> float:
    """Read one numeric loudnorm field, rejecting non-finite values."""
    try:
        value = float(raw[key])
    except (KeyError, ValueError) as exc:
        raise FFmpegError(f"loudnorm field {key!r} missing/non-numeric: {raw.get(key)!r}") from exc
    if not math.isfinite(value):
        # e.g. digital silence measures -inf LUFS; there is nothing to
        # normalise and any downstream gain math would be meaningless.
        raise FFmpegError(f"loudnorm field {key!r} is not finite ({value}); input may be silent")
    return value


def ensure_parent(path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def require_file(path: Path, what: str = "input") -> Path:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"{what} file not found: {p}")
    return p
