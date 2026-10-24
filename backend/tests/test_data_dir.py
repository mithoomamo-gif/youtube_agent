"""Tests for the ONE canonical data-dir resolver (Round 6).

``app.data_dir.resolve_data_dir`` owns data-root resolution for the
whole app:

* ``YTA_DATA_DIR`` override wins on EVERY platform branch;
* Windows -> ``%APPDATA%\\YouTubeAgent`` (Roaming reconstructed when
  ``APPDATA`` is absent);
* dev/Linux -> the historical ``~/.youtube-agent`` default, unchanged,
  so existing dev data and existing path expectations keep passing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings
from app.data_dir import (
    APP_DIR_NAME,
    DataDirLayout,
    data_dir_layout,
    is_windows_platform,
    resolve_data_dir,
)

HOME = Path("/home/tester")
APPDATA = "/appdata/roaming-sim"


# --------------------------------------------------------------------------
# Windows branch (simulated)
# --------------------------------------------------------------------------


def test_windows_uses_appdata() -> None:
    result = resolve_data_dir({"APPDATA": APPDATA}, platform="windows", home=HOME)
    assert result == Path(APPDATA) / "YouTubeAgent"


def test_windows_platform_spellings() -> None:
    for spelling in ("windows", "win32", "win", "nt", "Windows", "WIN32"):
        assert is_windows_platform(spelling) is True
        assert (
            resolve_data_dir({"APPDATA": APPDATA}, platform=spelling, home=HOME)
            == Path(APPDATA) / APP_DIR_NAME
        )


def test_windows_without_appdata_reconstructs_roaming() -> None:
    result = resolve_data_dir({}, platform="windows", home=HOME)
    assert result == HOME / "AppData" / "Roaming" / "YouTubeAgent"


def test_windows_via_load_settings_with_appdata_env() -> None:
    # The pre-Round-6 simulation route: APPDATA in the env mapping is
    # honoured by load_settings exactly as before.
    settings = load_settings(env={"YTA_TOKEN": "tok-12345678", "APPDATA": APPDATA})
    assert settings.data_dir == Path(APPDATA) / "YouTubeAgent"
    assert settings.db_path == Path(APPDATA) / "YouTubeAgent" / "app.db"
    assert settings.log_dir == Path(APPDATA) / "YouTubeAgent" / "logs"


# --------------------------------------------------------------------------
# YTA_DATA_DIR override — both platform branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["windows", "linux", "posix", "darwin"])
def test_override_wins_on_every_platform(platform: str) -> None:
    env = {"YTA_DATA_DIR": "/custom/yta-data", "APPDATA": APPDATA}
    assert resolve_data_dir(env, platform=platform, home=HOME) == Path("/custom/yta-data")


def test_override_wins_via_load_settings() -> None:
    settings = load_settings(
        env={"YTA_TOKEN": "tok-12345678", "YTA_DATA_DIR": "/custom/yta-data",
             "APPDATA": APPDATA}
    )
    assert settings.data_dir == Path("/custom/yta-data")


def test_blank_override_is_ignored() -> None:
    env = {"YTA_DATA_DIR": "   ", "APPDATA": APPDATA}
    assert resolve_data_dir(env, platform="windows", home=HOME) == Path(APPDATA) / "YouTubeAgent"
    assert resolve_data_dir({"YTA_DATA_DIR": ""}, platform="linux", home=HOME) == HOME / ".youtube-agent"


# --------------------------------------------------------------------------
# Dev / Linux branch — default UNCHANGED
# --------------------------------------------------------------------------


def test_linux_default_is_historical_home_dir() -> None:
    assert resolve_data_dir({}, platform="linux", home=HOME) == HOME / ".youtube-agent"
    assert resolve_data_dir({}, platform="posix", home=HOME) == HOME / ".youtube-agent"
    assert resolve_data_dir({}, platform="darwin", home=HOME) == HOME / ".youtube-agent"


@pytest.mark.skipif(
    is_windows_platform(),
    reason="Linux default via load_settings only applies on non-Windows platforms",
)
def test_linux_default_via_load_settings_unchanged() -> None:
    # Mirrors the pre-existing expectation in test_config.py.
    settings = load_settings(env={"YTA_TOKEN": "tok-12345678"})
    assert settings.data_dir == Path.home() / ".youtube-agent"


def test_linux_legacy_appdata_honoured_for_backcompat() -> None:
    # Rounds 1-5 config behaviour: APPDATA present -> APPDATA/YouTubeAgent
    # even when the platform branch is not Windows.  Preserved exactly.
    assert resolve_data_dir({"APPDATA": APPDATA}, platform="linux", home=HOME) == (
        Path(APPDATA) / "YouTubeAgent"
    )


@pytest.mark.skipif(
    is_windows_platform(),
    reason="Asserts the running platform is not Windows; only valid on non-Windows hosts",
)
def test_running_platform_is_not_windows_on_this_vm() -> None:
    assert is_windows_platform() is False
    assert resolve_data_dir({}, home=HOME) == HOME / ".youtube-agent"


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def test_layout_paths_under_root() -> None:
    layout = data_dir_layout(Path("/data/root"))
    assert isinstance(layout, DataDirLayout)
    assert layout.db_path == Path("/data/root/app.db")
    assert layout.log_dir == Path("/data/root/logs")
    assert layout.projects_dir == Path("/data/root/projects")
    assert layout.reports_dir == Path("/data/root/reports")
    assert layout.style_lab_dir == Path("/data/root/style_lab")
    assert layout.stylelab_dir == Path("/data/root/stylelab")
    assert layout.comment_intel_dir == Path("/data/root/comment_intel")
    assert layout.retention_sections_dir == Path(
        "/data/root/analytics/retention_sections"
    )
    assert layout.tmp_dir == Path("/data/root/tmp")


def test_settings_layout_matches_resolver_root() -> None:
    settings = load_settings(env={"YTA_TOKEN": "tok-12345678", "YTA_DATA_DIR": "/x"})
    assert settings.layout.root == settings.data_dir
    assert settings.layout.db_path == settings.db_path
    assert settings.layout.log_dir == settings.log_dir


# --------------------------------------------------------------------------
# Canonicalisation of the previously divergent fallbacks
# --------------------------------------------------------------------------


def test_db_fallback_uses_canonical_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """app.db.default_db_path's last-resort fallback goes through the
    resolver (previously it hand-rolled ~/.local/share/YouTubeAgent)."""
    from app import db as db_module
    from app.config import clear_settings_cache

    monkeypatch.setenv("YTA_DATA_DIR", "/canonical/data")
    clear_settings_cache()  # get_settings() must re-read the patched env
    try:
        assert db_module.default_db_path() == Path("/canonical/data/app.db")
    finally:
        clear_settings_cache()  # next caller reloads the conftest env


def test_comment_intel_default_uses_canonical_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.comment_intel.storage import default_comment_intel_dir

    monkeypatch.setenv("YTA_DATA_DIR", "/canonical/data")
    assert default_comment_intel_dir() == Path("/canonical/data/comment_intel")
    monkeypatch.delenv("YTA_DATA_DIR")
    assert default_comment_intel_dir() == resolve_data_dir() / "comment_intel"


def test_stylelab_and_retention_fallbacks_use_canonical_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.retention_sections.storage import default_retention_sections_dir
    from app.style_analysis.storage import default_stylelab_dir

    monkeypatch.setenv("YTA_DATA_DIR", "/canonical/data")
    assert default_stylelab_dir() == Path("/canonical/data/stylelab")
    assert default_retention_sections_dir() == Path(
        "/canonical/data/analytics/retention_sections"
    )
