"""Shared pytest fixtures for the YouTube Agent test suite.

Design rules (see tests/README.md and roadmap section 17):

* This module must import cleanly with **only pytest installed** — stdlib
  + pytest only. No fastapi, httpx, pydantic, langgraph, etc. at import
  time. Backend modules are imported lazily / via ``importorskip``.
* The default run is *hermetic*: no network, no real provider keys.
  The autouse fixture below strips ``*_API_KEY``-style variables from the
  environment for every non-``live`` test so a developer's real keys can
  never leak into a unit/contract/integration run (roadmap L9).
* Tests for backend modules that are still landing must skip, not fail.
  Use the ``requires_module`` fixture (or ``pytest.importorskip`` at
  module level) for anything under ``backend/``.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
# Sibling checkout that holds the master roadmap / spec sections.
ROADMAP_ROOT = REPO_ROOT.parent / "youtube-windows-agent"

# Make the backend importable under either layout the backend team lands:
#   <repo>/backend/__init__.py          -> import backend.*
#   <repo>/backend/app/__init__.py      -> import app.*  (backend/ on path)
# Inserting a not-yet-existing path is harmless.
for _candidate in (REPO_ROOT, REPO_ROOT / "backend"):
    _s = str(_candidate)
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root (parent of ``tests/``)."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def tests_dir() -> Path:
    """Absolute path to this ``tests/`` directory."""
    return TESTS_DIR


@pytest.fixture(scope="session")
def roadmap_root() -> Path:
    """Absolute path to the sibling ``youtube-windows-agent`` roadmap tree."""
    return ROADMAP_ROOT


# ---------------------------------------------------------------------------
# requires_module — graceful skips for backend modules still landing
# ---------------------------------------------------------------------------


@pytest.fixture
def requires_module():
    """Return a helper that importorskips a module by name.

    Usage::

        def test_jobs(requires_module):
            jobs = requires_module("backend.jobs")

    Equivalent to ``pytest.importorskip(name)`` but reads better when the
    module is needed mid-test (e.g. after trying several candidate paths).
    """

    def _require(modname: str, reason: str | None = None):
        return pytest.importorskip(
            modname,
            reason=reason or f"backend module {modname!r} not landed yet",
        )

    return _require


# ---------------------------------------------------------------------------
# Redaction-safe environment (hermetic by default)
# ---------------------------------------------------------------------------

_SECRET_ENV_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_SECRET",
    "_SECRET_KEY",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_PASSWORD",
    "_PRIVATE_KEY",
)
_SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "YOUTUBE_API_KEY",
}


def _is_secret_env(name: str) -> bool:
    upper = name.upper()
    return upper in _SECRET_ENV_NAMES or upper.endswith(_SECRET_ENV_SUFFIXES)


@pytest.fixture(autouse=True)
def _redaction_safe_env(request, monkeypatch):
    """Strip provider secrets from the environment for non-live tests.

    ``live``-marked tests are the only ones allowed to see real keys, and
    they are excluded from the default run (see tests/pytest.ini).
    ``monkeypatch`` restores the original environment after each test.
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    for name in list(os.environ):
        if _is_secret_env(name):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def clean_env(monkeypatch):
    """Explicit handle on the redacted environment for tests that assert it.

    Returns the ``monkeypatch`` fixture after secret variables have been
    cleared (the autouse fixture above has already done the clearing; this
    fixture exists so a test can state the dependency out loud and set
    fake values, e.g. ``monkeypatch.setenv("FAKE_API_KEY", "test-only")``).
    """
    return monkeypatch


# ---------------------------------------------------------------------------
# Fake clock — deterministic time for quota / scheduling / retry logic
# ---------------------------------------------------------------------------


class FakeClock:
    """A manually-advanced clock with time.time / monotonic / now faces.

    Starts at a fixed instant so tests are reproducible:
    2026-01-01T00:00:00Z (epoch 1767225600.0).
    """

    START_EPOCH = 1767225600.0  # 2026-01-01T00:00:00Z

    def __init__(self, start: float | None = None) -> None:
        self._now = self.START_EPOCH if start is None else float(start)

    # -- epoch-style faces -------------------------------------------------
    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    __call__ = time

    # -- datetime face ------------------------------------------------------
    def now(self, tz=timezone.utc) -> datetime:
        return datetime.fromtimestamp(self._now, tz=tz)

    def utcnow(self) -> datetime:
        return self.now(timezone.utc).replace(tzinfo=None)

    # -- control ------------------------------------------------------------
    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def advance_delta(self, delta: timedelta) -> float:
        return self.advance(delta.total_seconds())

    def set(self, when: float | datetime) -> float:
        if isinstance(when, datetime):
            self._now = when.timestamp()
        else:
            self._now = float(when)
        return self._now

    def sleep(self, seconds: float) -> None:  # drop-in for time.sleep in fakes
        self.advance(seconds)


@pytest.fixture
def fake_clock(monkeypatch):
    """A :class:`FakeClock`, also patched over ``time.time``/``time.monotonic``.

    Patching is scoped to the test via ``monkeypatch`` and undone after.
    Tests that need the real clock should not request this fixture.
    """
    clock = FakeClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    return clock


@pytest.fixture
def fake_clock_unpatched():
    """A :class:`FakeClock` without patching the global ``time`` module."""
    return FakeClock()


# ---------------------------------------------------------------------------
# Temporary project directories
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project_dir(tmp_path):
    """Factory creating throwaway project directories.

    Usage::

        proj = tmp_project_dir()            # <tmp>/project
        proj2 = tmp_project_dir("other")    # <tmp>/other

    Each project dir gets the sub-layout the pipeline expects
    (``assets/``, ``renders/``, ``audio/``, ``subtitles/``) so early
    pipeline code can write into it without setup boilerplate.
    """
    counter = {"n": 0}

    def _make(name: str | None = None) -> Path:
        counter["n"] += 1
        dirname = name or (f"project-{counter['n']}" if counter["n"] > 1 else "project")
        proj = tmp_path / dirname
        for sub in ("assets", "renders", "audio", "subtitles"):
            (proj / sub).mkdir(parents=True, exist_ok=True)
        return proj

    return _make


@pytest.fixture
def tmp_project(tmp_project_dir) -> Path:
    """A single ready-made temporary project directory."""
    return tmp_project_dir()


# ---------------------------------------------------------------------------
# FFmpeg availability
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ffmpeg_path() -> str | None:
    """Path to the ``ffmpeg`` binary, or ``None`` when not installed."""
    return shutil.which("ffmpeg")


@pytest.fixture(scope="session")
def ffprobe_path() -> str | None:
    """Path to the ``ffprobe`` binary, or ``None`` when not installed."""
    return shutil.which("ffprobe")


@pytest.fixture
def ffmpeg_available(ffmpeg_path) -> bool:
    """True when a local ffmpeg binary is on PATH (informational)."""
    return ffmpeg_path is not None


@pytest.fixture
def require_ffmpeg(ffmpeg_path):
    """Skip the requesting test when ffmpeg is not installed."""
    if ffmpeg_path is None:
        pytest.skip("ffmpeg binary not found on PATH")
    return ffmpeg_path


@pytest.fixture
def require_ffprobe(ffprobe_path):
    """Skip the requesting test when ffprobe is not installed."""
    if ffprobe_path is None:
        pytest.skip("ffprobe binary not found on PATH")
    return ffprobe_path


def pytest_runtest_setup(item):
    """Auto-skip ``ffmpeg``-marked tests when no ffmpeg binary exists.

    This is what makes the ``ffmpeg`` marker safe to use anywhere: on a
    machine (or CI image) without FFmpeg the test reports as *skipped*
    with a clear reason instead of failing.
    """
    if item.get_closest_marker("ffmpeg") is not None:
        if shutil.which("ffmpeg") is None:
            pytest.skip("marked 'ffmpeg' but no ffmpeg binary on PATH")
