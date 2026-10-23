"""Round 6 log management: rotation, redaction on every file path, stderr
capture, and sane dev (console-only) behaviour.

Proof obligations:
* a seeded FAKE secret emitted through the *real* logging configuration
  never appears in any produced log file, while plain content does;
* rollover happens at the configured size and the file count stays
  within the live + 5 backups bound, for both the app log and the
  stderr mirror;
* shipped defaults are exactly 10 MB x 5;
* every file-writing handler carries the redaction filter;
* console-only configuration (no log dir) still logs without error.
"""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app import redaction
from app.logging_setup import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    StderrCapture,
    configure_logging,
    uninstall_stderr_capture,
)
from app.redaction import RedactionFilter

FAKE_SECRET = "FAKE_SECRET_FOR_REDACTION_TEST_123"


@pytest.fixture()
def restore_logging():
    """Snapshot + restore root logger / sys.stderr around each test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_stderr = sys.stderr
    redaction.register_secret(FAKE_SECRET)
    try:
        yield
    finally:
        uninstall_stderr_capture()
        for handler in list(root.handlers):
            if getattr(handler, "_yta_handler", False):
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        if sys.stderr is not saved_stderr:
            sys.stderr = saved_stderr
        redaction._extra_secrets.discard(FAKE_SECRET)


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _log_files(log_dir: Path, stem: str) -> list[Path]:
    return sorted(log_dir.glob(f"{stem}*"))


# ---------------------------------------------------------------- redaction


def test_fake_secret_never_reaches_any_log_file(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir)

    logger = logging.getLogger("yta.test.logmgmt")
    logger.info("plain marker: pipeline started")
    logger.info("leaked value is %s", FAKE_SECRET)
    logger.warning("direct embed %s done", FAKE_SECRET)
    try:
        raise ValueError(f"boom carrying {FAKE_SECRET}")
    except ValueError:
        logger.exception("failure while using %s", FAKE_SECRET)

    # Raw stderr path (tracebacks, fatal prints, uvicorn): plain writes.
    print("stderr plain marker", file=sys.stderr)
    print(f"stderr leaks {FAKE_SECRET} here", file=sys.stderr)
    # Split across two writes: line buffering must still redact whole.
    sys.stderr.write("stderr split " + FAKE_SECRET[:10])
    sys.stderr.write(FAKE_SECRET[10:] + "\n")
    sys.stderr.flush()
    _flush_root_handlers()

    app_log = (log_dir / "backend.log").read_text(encoding="utf-8")
    err_log = (log_dir / "stderr.log").read_text(encoding="utf-8")

    assert FAKE_SECRET not in app_log
    assert FAKE_SECRET not in err_log
    assert "plain marker: pipeline started" in app_log
    assert "stderr plain marker" in err_log
    assert "***" in app_log  # redaction actually replaced something
    assert "***" in err_log

    # App log is valid JSON lines with the documented shape.
    lines = [ln for ln in app_log.splitlines() if ln.strip()]
    assert len(lines) >= 4
    for line in lines:
        payload = json.loads(line)
        assert {"ts", "level", "logger", "message", "boot_id"} <= set(payload)


def test_stderr_capture_is_a_passthrough_tee(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir)
    assert isinstance(sys.stderr, StderrCapture)
    # Passthrough target resolves dynamically to the harness's stderr,
    # and writing through the tee does not raise.
    written = sys.stderr.write("tee passthrough line\n")
    assert written == len("tee passthrough line\n")
    sys.stderr.flush()
    assert "tee passthrough line" in (log_dir / "stderr.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------- handlers


def test_defaults_are_10mb_times_5_and_every_file_handler_redacts(
    restore_logging, tmp_path
):
    assert LOG_MAX_BYTES == 10 * 1024 * 1024
    assert LOG_BACKUP_COUNT == 5

    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir)

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.maxBytes == LOG_MAX_BYTES
    assert handler.backupCount == LOG_BACKUP_COUNT
    assert Path(handler.baseFilename) == log_dir / "backend.log"

    # Every handler configure_logging installed (file AND stdout) carries
    # redaction. (pytest's own capture handlers also sit on the root and
    # are not ours to police.)
    ours = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_yta_handler", False)
    ]
    assert len(ours) == 2
    for h in ours:
        assert any(isinstance(f, RedactionFilter) for f in h.filters), h


def test_configure_logging_is_idempotent(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir)
    first_capture = sys.stderr
    configure_logging("info", log_dir)

    marked = [
        h for h in logging.getLogger().handlers if getattr(h, "_yta_handler", False)
    ]
    assert len(marked) == 2  # one stdout + one file handler, not duplicated
    assert sys.stderr is first_capture  # capture not stacked


def test_preexisting_uvicorn_handlers_gain_redaction(restore_logging, tmp_path):
    uvicorn_logger = logging.getLogger("uvicorn.error")
    bare = logging.StreamHandler(sys.stdout)
    uvicorn_logger.addHandler(bare)
    try:
        configure_logging("info", tmp_path / "logs")
        assert any(isinstance(f, RedactionFilter) for f in bare.filters)
    finally:
        uvicorn_logger.removeHandler(bare)


# ---------------------------------------------------------------- rotation


def test_app_log_rotation_bounds_file_count(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir, max_bytes=2048, backup_count=5)
    logger = logging.getLogger("yta.test.rotation")
    for i in range(400):
        logger.info("rotation filler record %04d %s", i, "x" * 80)
    _flush_root_handlers()

    files = _log_files(log_dir, "backend.log")
    assert (log_dir / "backend.log.1").exists()  # rollover actually happened
    assert len(files) == 6  # live file + exactly 5 backups, never more
    for path in files:
        assert path.stat().st_size <= 2048


def test_stderr_mirror_rotation_bounds_file_count(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("info", log_dir, max_bytes=2048, backup_count=5)
    for i in range(400):
        sys.stderr.write(f"stderr filler line {i:04d} " + "y" * 80 + "\n")
    sys.stderr.flush()

    files = _log_files(log_dir, "stderr.log")
    assert (log_dir / "stderr.log.1").exists()
    assert len(files) == 6
    for path in files:
        assert path.stat().st_size <= 2048 + 128  # one line of slack


# ---------------------------------------------------------------- dev mode


def test_console_only_mode_still_logs(restore_logging, tmp_path):
    configure_logging("info", tmp_path / "logs")
    assert isinstance(sys.stderr, StderrCapture)

    root = configure_logging("info", None)  # dev: no log dir
    assert not any(
        isinstance(h, RotatingFileHandler) for h in root.handlers
    )
    assert not isinstance(sys.stderr, StderrCapture)  # capture unwound
    # Console logging path works end to end without raising.
    logging.getLogger("yta.test.dev").info("dev console line %s", FAKE_SECRET)
    _flush_root_handlers()
