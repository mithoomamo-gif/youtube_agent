"""Always-runnable sanity tests (L1 smoke).

These tests deliberately depend on **nothing** beyond the stdlib and the
repository layout that exists *today*. They are the canary that the test
infrastructure itself works: if this file fails, fix the harness before
trusting any other result.

Rule from roadmap section 17: *a gate never seen to fail is assumed
broken* — ``test_suite_can_fail`` at the bottom is the positive control
proving assertions in this suite really execute.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
ROADMAP_ROOT = REPO_ROOT.parent / "youtube-windows-agent"


def test_python_version_is_312_or_newer():
    """The backend targets Python 3.12 (MASTER_ROADMAP stack decision)."""
    assert sys.version_info >= (3, 12), (
        f"YouTube Agent requires Python >= 3.12, running {platform.python_version()}"
    )


def test_tests_tree_self_check():
    """The tests/ tree has the structure the rest of the suite relies on."""
    assert (TESTS_DIR / "conftest.py").is_file()
    assert (TESTS_DIR / "pytest.ini").is_file()
    assert (TESTS_DIR / "README.md").is_file()
    for layer in ("unit", "contract", "integration"):
        assert (TESTS_DIR / layer).is_dir(), f"missing tests/{layer}/"


def test_repo_root_layout(repo_root):
    """Repo root is where conftest thinks it is, and tests/ lives in it."""
    assert repo_root == REPO_ROOT
    assert (repo_root / "tests").is_dir()
    # backend/ is being written by a parallel team and may not exist yet;
    # when it does land it must be a directory (not a stray file).
    backend = repo_root / "backend"
    if backend.exists():
        assert backend.is_dir()


def test_roadmap_spec_files_exist():
    """The spec sources this suite is built from are present (sibling tree)."""
    if not ROADMAP_ROOT.is_dir():
        # Clean checkouts (CI) contain only this repo; the roadmap tree is a
        # sibling in the dev workspace. Skip rather than fail there — the
        # check still bites on every dev machine where the sibling exists.
        pytest.skip("sibling roadmap tree (youtube-windows-agent/) not in this checkout")
    assert (ROADMAP_ROOT / "MASTER_ROADMAP.md").is_file()
    assert (ROADMAP_ROOT / "roadmap-sections" / "17_testing_qa.md").is_file()


def test_ffmpeg_presence_is_informational(ffmpeg_available, ffmpeg_path):
    """FFmpeg is expected on dev machines but its absence must not fail L1.

    Render/QC tests that truly need it use the ``ffmpeg`` marker or the
    ``require_ffmpeg`` fixture and skip instead (see conftest.py).
    """
    # Cross-check the fixture against a direct lookup; both must agree.
    assert ffmpeg_available == (shutil.which("ffmpeg") is not None)
    if ffmpeg_available:
        assert ffmpeg_path is not None and Path(ffmpeg_path).name.startswith("ffmpeg")


def test_fake_clock_advances_deterministically(fake_clock_unpatched):
    """The shared fake clock starts fixed and advances only on demand."""
    start = fake_clock_unpatched.time()
    assert fake_clock_unpatched.time() == start  # no drift between calls
    fake_clock_unpatched.advance(90)
    assert fake_clock_unpatched.time() == start + 90
    assert fake_clock_unpatched.now().timestamp() == start + 90


def test_secret_env_is_redacted_for_unit_tests(clean_env):
    """Hermetic runs must not see the developer's real provider keys."""
    import os

    for name in os.environ:
        # Our own fake values set inside a test are fine; the autouse
        # fixture guarantees nothing pre-existing survived into the test.
        assert not name.endswith("_API_KEY") or os.environ[name] == "test-only", (
            f"unexpected real-looking API key variable present: {name}"
        )
    # And a fake value can be set explicitly when a test needs one.
    clean_env.setenv("FAKE_API_KEY", "test-only")


def test_suite_can_fail():
    """Positive control: assertions here really run (section 17 rule)."""
    with pytest.raises(AssertionError):
        assert 1 == 2, "deliberate failure inside pytest.raises"
