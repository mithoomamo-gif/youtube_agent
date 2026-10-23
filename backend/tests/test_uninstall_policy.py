"""Uninstall data policy (Round 6) — marker + settings flag tests.

Policy under test (roadmap §26.7): uninstalling PRESERVES user data
by default; data is purged on uninstall only when the user opted in
via the flag ``privacy.remove_data_on_uninstall`` (default OFF), which
the backend records as a marker file named ``uninstall-purge.marker``
in the data root for the NSIS uninstaller hook to check.

Covered:
* unit — marker write/remove/no-op, exact filename + location,
  flag parsing (only boolean True counts), reconcile;
* API — fresh-profile default (flag OFF, no marker), typed
  ``GET|PUT /api/v1/settings/privacy`` round-trip with marker
  side-effects, and marker sync via the generic ``PUT /settings``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.uninstall_policy import (
    MARKER_FILENAME,
    SETTINGS_KEY,
    flag_enabled,
    marker_path,
    marker_present,
    reconcile_marker,
    sync_marker,
)

EXPECTED_MARKER_NAME = "uninstall-purge.marker"
EXPECTED_SETTINGS_KEY = "privacy.remove_data_on_uninstall"


# ---------------------------------------------------------------------------
# convention constants
# ---------------------------------------------------------------------------


def test_agreed_marker_name_and_flag_key() -> None:
    # The names the NSIS uninstaller hook (shell team) checks for.
    assert MARKER_FILENAME == EXPECTED_MARKER_NAME
    assert SETTINGS_KEY == EXPECTED_SETTINGS_KEY


# ---------------------------------------------------------------------------
# unit: marker file behaviour
# ---------------------------------------------------------------------------


def test_sync_marker_on_writes_marker_in_data_root(tmp_path: Path) -> None:
    result = sync_marker(True, tmp_path)
    expected = tmp_path / EXPECTED_MARKER_NAME
    assert result == expected
    assert expected.is_file()
    # The marker explains itself to a human / the uninstaller author.
    body = expected.read_text(encoding="utf-8")
    assert EXPECTED_SETTINGS_KEY in body


def test_sync_marker_off_removes_marker(tmp_path: Path) -> None:
    sync_marker(True, tmp_path)
    assert marker_present(tmp_path)
    result = sync_marker(False, tmp_path)
    assert result is None
    assert not marker_present(tmp_path)
    assert not (tmp_path / EXPECTED_MARKER_NAME).exists()


def test_sync_marker_off_without_marker_is_a_noop(tmp_path: Path) -> None:
    assert sync_marker(False, tmp_path) is None
    assert not marker_present(tmp_path)


def test_sync_marker_on_creates_missing_data_root(tmp_path: Path) -> None:
    nested = tmp_path / "fresh" / "data-root"
    sync_marker(True, nested)
    assert (nested / EXPECTED_MARKER_NAME).is_file()


def test_marker_path_is_directly_in_data_root(tmp_path: Path) -> None:
    assert marker_path(tmp_path) == tmp_path / EXPECTED_MARKER_NAME


# ---------------------------------------------------------------------------
# unit: flag parsing + reconcile (the fresh-profile default)
# ---------------------------------------------------------------------------


def test_flag_enabled_only_for_boolean_true() -> None:
    assert flag_enabled(None) is False
    assert flag_enabled({}) is False  # fresh profile: key absent
    assert flag_enabled({SETTINGS_KEY: False}) is False
    assert flag_enabled({SETTINGS_KEY: "true"}) is False  # not a bool
    assert flag_enabled({SETTINGS_KEY: 1}) is False  # not a bool
    assert flag_enabled({SETTINGS_KEY: True}) is True


def test_reconcile_fresh_profile_leaves_no_marker(tmp_path: Path) -> None:
    assert reconcile_marker({}, tmp_path) is False
    assert not marker_present(tmp_path)


def test_reconcile_applies_persisted_flag_both_ways(tmp_path: Path) -> None:
    assert reconcile_marker({SETTINGS_KEY: True}, tmp_path) is True
    assert marker_present(tmp_path)
    assert reconcile_marker({}, tmp_path) is False
    assert not marker_present(tmp_path)


# ---------------------------------------------------------------------------
# API: typed privacy endpoint + generic settings sync
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_data_dir() -> Path:
    """The data root the running test app actually uses.

    NOTE: this is deliberately NOT the conftest ``test_data_dir``
    fixture.  ``test_routers.py`` reassigns ``YTA_DATA_DIR`` at import
    time (pre-existing repo behaviour), so once the full suite is
    collected the app resolves its data root from the *live* env var,
    which may differ from the conftest's original session dir.  The
    app (``load_settings``) reads the env at app-creation time, so
    reading it here matches the app in both standalone and combined
    runs.
    """
    return Path(os.environ["YTA_DATA_DIR"])


@pytest.fixture()
def privacy_api(client: TestClient, app_data_dir: Path):
    """API client with the flag reset OFF before and after the test,
    so marker state never leaks into other test files (the session
    data dir / DB is shared)."""
    client.put(
        "/api/v1/settings/privacy", json={"remove_data_on_uninstall": False}
    )
    yield client
    client.put(
        "/api/v1/settings/privacy", json={"remove_data_on_uninstall": False}
    )


def test_api_default_state_is_off_and_no_marker(
    client: TestClient, app_data_dir: Path
) -> None:
    # Runs before any opt-in in this file: a fresh profile must report
    # the preserve-by-default state and have NO marker on disk.
    resp = client.get("/api/v1/settings/privacy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["remove_data_on_uninstall"] is False
    assert body["marker_present"] is False
    assert body["marker_filename"] == EXPECTED_MARKER_NAME
    assert body["settings_key"] == EXPECTED_SETTINGS_KEY
    assert body["data_preserved_by_default"] is True
    assert not (app_data_dir / EXPECTED_MARKER_NAME).exists()


def test_api_toggle_on_writes_marker_and_roundtrips(
    privacy_api: TestClient, app_data_dir: Path
) -> None:
    marker = app_data_dir / EXPECTED_MARKER_NAME
    assert not marker.exists()

    resp = privacy_api.put(
        "/api/v1/settings/privacy", json={"remove_data_on_uninstall": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["remove_data_on_uninstall"] is True
    assert body["marker_present"] is True
    assert marker.is_file()

    # Round-trip: typed GET and the generic settings map agree.
    got = privacy_api.get("/api/v1/settings/privacy")
    assert got.json()["remove_data_on_uninstall"] is True
    assert got.json()["marker_present"] is True
    all_settings = privacy_api.get("/api/v1/settings")
    assert all_settings.json()[EXPECTED_SETTINGS_KEY] is True


def test_api_toggle_off_removes_marker(
    privacy_api: TestClient, app_data_dir: Path
) -> None:
    marker = app_data_dir / EXPECTED_MARKER_NAME
    privacy_api.put(
        "/api/v1/settings/privacy", json={"remove_data_on_uninstall": True}
    )
    assert marker.is_file()

    resp = privacy_api.put(
        "/api/v1/settings/privacy", json={"remove_data_on_uninstall": False}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["remove_data_on_uninstall"] is False
    assert body["marker_present"] is False
    assert not marker.exists()


def test_api_generic_settings_put_syncs_marker(
    privacy_api: TestClient, app_data_dir: Path
) -> None:
    marker = app_data_dir / EXPECTED_MARKER_NAME

    resp = privacy_api.put(
        "/api/v1/settings", json={"values": {EXPECTED_SETTINGS_KEY: True}}
    )
    assert resp.status_code == 200
    assert marker.is_file()

    resp = privacy_api.put(
        "/api/v1/settings", json={"values": {EXPECTED_SETTINGS_KEY: False}}
    )
    assert resp.status_code == 200
    assert not marker.exists()


def test_api_generic_settings_put_rejects_non_bool_flag(
    privacy_api: TestClient, app_data_dir: Path
) -> None:
    resp = privacy_api.put(
        "/api/v1/settings", json={"values": {EXPECTED_SETTINGS_KEY: "yes"}}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "privacy_setting_invalid"
    # A rejected write must not change the marker either way.
    assert not (app_data_dir / EXPECTED_MARKER_NAME).exists()


def test_api_privacy_put_requires_boolean_field(
    privacy_api: TestClient,
) -> None:
    resp = privacy_api.put("/api/v1/settings/privacy", json={})
    assert resp.status_code == 422
