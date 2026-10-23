"""Logging setup: JSON-lines file log + human stdout log, both redacted.

Log management (Round 6):

* App log file: ``<log_dir>/backend.log``, JSON lines, rotating
  10 MB x 5 (one live file + five backups), via
  :class:`logging.handlers.RotatingFileHandler`.
* Stderr capture: ``<log_dir>/stderr.log``, plain text, same rotation
  (10 MB x 5). Once :func:`configure_logging` runs with a ``log_dir``,
  ``sys.stderr`` is tee'd through :class:`StderrCapture`: writes still go
  to the real stderr (dev console behaviour unchanged) while a redacted
  copy lands in the file — so tracebacks, uvicorn's fatal prints and any
  other raw stderr output are captured under the data root's logs area.
* Stdout: short human-readable lines (stdout is log text, never protocol —
  the single ``{"event": "listening", ...}`` line from ``app.main`` is the
  only structured stdout line, per roadmap §31).

Every handler carries :class:`app.redaction.RedactionFilter`, so a secret
can never reach either sink even if a caller logs it by mistake. The file
formatter redacts again, and the stderr mirror redacts line-by-line, so
redaction is active on every path that writes to a file. Uvicorn's own
loggers are covered two ways: ``app.main.run()`` passes ``log_config=None``
so uvicorn installs no handlers of its own (its records propagate to the
root handlers above), and :func:`configure_logging` attaches the redaction
filter to any pre-existing uvicorn handlers as a backstop.

``log_dir`` is supplied by the caller (``app.main`` passes
``Settings.log_dir`` = ``<data_dir>/logs``; the data dir itself resolves
from ``YTA_DATA_DIR`` / ``%APPDATA%\\YouTubeAgent`` in ``app.config`` — if
the canonical data-dir resolver lands, it only changes what ``log_dir``
the caller passes, not this module).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.redaction import REDACTED, RedactionFilter, redact_text  # noqa: F401  (REDACTED re-exported for convenience)

LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per log file
LOG_BACKUP_COUNT = 5  # plus the live file = 6 files max per log stream
_MAX_BYTES = LOG_MAX_BYTES
_BACKUP_COUNT = LOG_BACKUP_COUNT
_HANDLER_MARK = "_yta_handler"
_STDERR_FILENAME = "stderr.log"

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, message, boot_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "boot_id": os.environ.get("YTA_BOOT_ID", ""),
        }
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class _RotatingTextSink:
    """Append-only text file with RotatingFileHandler-style rollover.

    Backs the stderr mirror, which is a stream (not ``logging`` records)
    and so cannot use :class:`RotatingFileHandler` directly. Rollover
    keeps ``backup_count`` backups (``name.1`` … ``name.5``); the oldest
    is dropped. Never raises: a broken log file must not break the app.
    """

    def __init__(self, path: str | Path, max_bytes: int, backup_count: int) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._size = self.path.stat().st_size if self.path.exists() else 0
            self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        except Exception:
            self._fh = None

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._fh is None:
                return
            try:
                incoming = len(text.encode("utf-8", errors="replace"))
                if (
                    self.max_bytes > 0
                    and self._size > 0
                    and self._size + incoming > self.max_bytes
                ):
                    self._rotate_locked()
                self._fh.write(text)
                self._fh.flush()
                self._size += incoming
            except Exception:
                pass

    def _rotate_locked(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            if self.backup_count > 0:
                for index in range(self.backup_count - 1, 0, -1):
                    src = self.path.with_name(f"{self.path.name}.{index}")
                    dst = self.path.with_name(f"{self.path.name}.{index + 1}")
                    if src.exists():
                        os.replace(src, dst)
                if self.path.exists():
                    os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
                self._fh = open(self.path, "w", encoding="utf-8")  # noqa: SIM115
            else:
                self._fh = open(self.path, "w", encoding="utf-8")  # noqa: SIM115
        except Exception:
            self._fh = None
        self._size = 0

    def flush(self) -> None:
        with self._lock:
            try:
                if self._fh is not None:
                    self._fh.flush()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                if self._fh is not None:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None


class StderrCapture:
    """File-like tee installed as ``sys.stderr``.

    Every write passes through to the real stderr unchanged (resolved
    dynamically, so pytest/test harnesses that swap ``sys.stderr`` keep
    working), while complete lines are redacted via
    :func:`app.redaction.redact_text` and mirrored into a rotating
    ``stderr.log``. Line buffering means a secret split across two
    ``write()`` calls is still redacted as a whole line. Never raises.
    """

    def __init__(self, sink: _RotatingTextSink, original) -> None:
        self._sink = sink
        self._original = original
        self._buffer = ""
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._sink.path

    @property
    def original(self):
        return self._original

    def _target(self):
        current = sys.stderr
        return self._original if current is self else current

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        try:
            self._target().write(text)
        except Exception:
            pass
        try:
            with self._lock:
                self._buffer += text
                lines = self._buffer.split("\n")
                self._buffer = lines.pop()  # trailing partial line
                for line in lines:
                    self._sink.write(redact_text(line) + "\n")
        except Exception:
            pass
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        try:
            self._target().flush()
        except Exception:
            pass
        try:
            with self._lock:
                if self._buffer:
                    self._sink.write(redact_text(self._buffer) + "\n")
                    self._buffer = ""
        except Exception:
            pass
        self._sink.flush()

    def close_file(self) -> None:
        """Flush any partial line and close the mirror file (passthrough
        stderr is left open — it is not ours)."""
        try:
            with self._lock:
                if self._buffer:
                    self._sink.write(redact_text(self._buffer) + "\n")
                    self._buffer = ""
        except Exception:
            pass
        self._sink.close()

    def isatty(self) -> bool:
        try:
            return bool(self._target().isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._target().fileno()

    def __getattr__(self, name):
        # Delegate anything else (encoding, buffer, …) to the real stderr.
        original = self.__dict__.get("_original")
        if original is None:
            raise AttributeError(name)
        return getattr(original, name)


_active_capture: StderrCapture | None = None


def install_stderr_capture(
    log_dir: str | Path,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> StderrCapture:
    """Tee ``sys.stderr`` into ``<log_dir>/stderr.log`` (idempotent).

    Re-installing for the same file returns the live capture; installing
    for a different directory closes the old mirror and re-tees from the
    original stderr.
    """
    global _active_capture
    path = Path(log_dir) / _STDERR_FILENAME
    current = sys.stderr
    if isinstance(current, StderrCapture) and _active_capture is current:
        if current.path == path:
            return current
        uninstall_stderr_capture()
        current = sys.stderr
    sink = _RotatingTextSink(path, max_bytes, backup_count)
    capture = StderrCapture(sink, current)
    _active_capture = capture
    sys.stderr = capture
    return capture


def uninstall_stderr_capture() -> bool:
    """Restore the original ``sys.stderr`` and close the mirror file."""
    global _active_capture
    capture = _active_capture
    if capture is None:
        return False
    _active_capture = None
    try:
        capture.close_file()
    except Exception:
        pass
    if sys.stderr is capture:
        sys.stderr = capture.original
    return True


def _level_value(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(
    level: str | int = "info",
    log_dir: str | Path | None = None,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    capture_stderr: bool = True,
) -> logging.Logger:
    """Configure the root logger (idempotent; safe to call repeatedly).

    With ``log_dir``: app records go to a rotating JSON-lines file and
    stderr is captured to a rotating ``stderr.log`` in the same directory.
    Without it (bare dev console): stdout only, no capture — console
    logging keeps working exactly as before.
    """
    root = logging.getLogger()
    root.setLevel(_level_value(level))

    # Drop handlers from a previous configure_logging() call.
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    redaction = RedactionFilter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    stdout_handler.addFilter(redaction)
    setattr(stdout_handler, _HANDLER_MARK, True)
    root.addHandler(stdout_handler)

    # Backstop: if uvicorn (or anything else) already installed handlers
    # on its own loggers, make sure they redact too. The primary fix is
    # main.run() passing log_config=None so uvicorn installs none.
    for name in _UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactionFilter) for f in handler.filters):
                handler.addFilter(redaction)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "backend.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonLinesFormatter())
        file_handler.addFilter(redaction)
        setattr(file_handler, _HANDLER_MARK, True)
        root.addHandler(file_handler)

        if capture_stderr:
            install_stderr_capture(
                directory, max_bytes=max_bytes, backup_count=backup_count
            )
    elif _active_capture is not None:
        # Reconfigured without a log dir: do not keep mirroring stderr
        # into the previous run's directory.
        uninstall_stderr_capture()

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = [
    "LOG_BACKUP_COUNT",
    "LOG_MAX_BYTES",
    "JsonLinesFormatter",
    "StderrCapture",
    "configure_logging",
    "get_logger",
    "install_stderr_capture",
    "uninstall_stderr_capture",
]
