"""Round 6 — first-run wizard system-check tests.

Honesty contract under test: every check performs the real probe and
reports the real state — ffmpeg missing is a FAILURE (never a green
tick), WebView2 on a non-Windows OS is "unverified" (never pass, never
fail), low disk is a WARNING carrying the real free-space number, a
stored-but-untested provider key is "saved, not yet verified", and the
credential-store selection pins Windows Credential Manager on Windows.

No real key is used anywhere: fake strings + an in-memory keyring only.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app import secrets_store, system_check

FAKE_GEMINI_KEY = "fake-gemini-key-for-tests-0000"
FAKE_PIXABAY_KEY = "fake-pixabay-key-for-tests-1111"

LOUDNORM_STDERR = """
[Parsed_loudnorm_0 @ 0x123] 
{
	"input_i" : "-14.02",
	"input_tp" : "-1.20",
	"input_lra" : "0.90",
	"input_thresh" : "-24.11",
	"output_i" : "-14.00",
	"target_offset" : "0.02",
	"linear" : "true",
	"mode" : "measurement"
}
"""


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets_store, "keyring", fake)
    return fake


def _fake_runner(*, loudnorm_stderr: str = LOUDNORM_STDERR, version_rc: int = 0):
    def run(cmd, capture_output=True, text=True, timeout=None):
        if "-version" in cmd:
            return SimpleNamespace(
                returncode=version_rc,
                stdout="ffmpeg version 8.1.2-test\nbuilt with gcc",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr=loudnorm_stderr)

    return run


# ---------------------------------------------------------------------------
# WebView2
# ---------------------------------------------------------------------------
def test_webview2_unverified_on_non_windows():
    result = system_check.check_webview2(platform="linux", environ={})
    assert result["id"] == "webview2"
    assert result["status"] == "unverified"
    assert "Cannot verify on this OS" in result["detail"]


def test_webview2_unverified_on_macos_too():
    assert system_check.check_webview2(platform="darwin")["status"] == "unverified"


def test_webview2_pass_on_windows_when_registry_reports_version():
    result = system_check.check_webview2(
        platform="win32",
        environ={},
        registry_version=lambda: "141.0.3537.99",
    )
    assert result["status"] == "pass"
    assert result["measured"]["version"] == "141.0.3537.99"
    assert result["measured"]["source"] == "registry"


def test_webview2_fail_on_windows_when_absent(tmp_path):
    result = system_check.check_webview2(
        platform="win32",
        environ={
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "ProgramFiles": str(tmp_path / "pf"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
        registry_version=lambda: None,
    )
    assert result["status"] == "fail"
    assert "NOT found" in result["detail"]


def test_webview2_pass_on_windows_via_filesystem(tmp_path):
    app_dir = (
        tmp_path / "pf" / "Microsoft" / "EdgeWebView" / "Application" / "141.0.1.2"
    )
    app_dir.mkdir(parents=True)
    (app_dir / "msedgewebview2.exe").write_bytes(b"fake")
    result = system_check.check_webview2(
        platform="win32",
        environ={"ProgramFiles": str(tmp_path / "pf")},
        registry_version=lambda: None,
    )
    assert result["status"] == "pass"
    assert result["measured"]["source"] == "filesystem"
    assert result["measured"]["version"] == "141.0.1.2"


# ---------------------------------------------------------------------------
# FFmpeg + loudnorm
# ---------------------------------------------------------------------------
def test_ffmpeg_missing_is_a_real_failure():
    result = system_check.check_ffmpeg(which=lambda name: None)
    assert result["status"] == "fail"
    assert "not found on PATH" in result["detail"]
    assert result["measured"]["path"] is None


def test_ffmpeg_loudnorm_probe_passes_with_real_measurement():
    result = system_check.check_ffmpeg(
        which=lambda name: "/usr/bin/ffmpeg", runner=_fake_runner()
    )
    assert result["status"] == "pass"
    assert result["measured"]["loudnorm_input_i_lufs"] == pytest.approx(-14.02)
    assert result["measured"]["loudnorm_target_lufs"] == -14.0
    assert "8.1.2" in result["measured"]["version"]


def test_ffmpeg_present_but_loudnorm_silent_is_failure():
    result = system_check.check_ffmpeg(
        which=lambda name: "/usr/bin/ffmpeg",
        runner=_fake_runner(loudnorm_stderr="no json here"),
    )
    assert result["status"] == "fail"
    assert "loudnorm" in result["detail"]


def test_ffmpeg_broken_binary_is_failure():
    result = system_check.check_ffmpeg(
        which=lambda name: "/usr/bin/ffmpeg", runner=_fake_runner(version_rc=1)
    )
    assert result["status"] == "fail"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_real_probe_on_this_machine():
    """The genuine article: real ffmpeg, real loudnorm JSON."""
    result = system_check.check_ffmpeg()
    assert result["status"] == "pass"
    assert result["measured"]["loudnorm_input_i_lufs"] < 0


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def _usage(free: int, total: int = 100 * system_check.GIB):
    return lambda path: SimpleNamespace(total=total, used=total - free, free=free)


def test_disk_pass_with_plenty_free(tmp_path):
    result = system_check.check_disk(
        data_dir=tmp_path, disk_usage=_usage(50 * system_check.GIB)
    )
    assert result["status"] == "pass"
    assert result["measured"]["free_bytes"] == 50 * system_check.GIB


def test_disk_low_space_warns_with_the_real_number(tmp_path):
    free = 5 * system_check.GIB + 12345
    result = system_check.check_disk(data_dir=tmp_path, disk_usage=_usage(free))
    assert result["status"] == "warn"
    # The warning must carry the REAL measured number, not a guess.
    assert result["measured"]["free_bytes"] == free
    assert str(result["measured"]["free_gb"]) in result["detail"]


def test_disk_critically_low_is_failure(tmp_path):
    result = system_check.check_disk(
        data_dir=tmp_path, disk_usage=_usage(512 * 1024 * 1024)
    )
    assert result["status"] == "fail"
    assert result["measured"]["free_bytes"] == 512 * 1024 * 1024


def test_disk_real_measurement_on_this_machine(tmp_path):
    result = system_check.check_disk(data_dir=tmp_path)
    assert result["status"] in ("pass", "warn")
    assert result["measured"]["free_bytes"] > 0
    real_free = shutil.disk_usage(str(tmp_path)).free
    assert abs(result["measured"]["free_bytes"] - real_free) < system_check.GIB


# ---------------------------------------------------------------------------
# Sidecar health
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sidecar_pass_when_probe_reports_live_and_ready():
    async def probe(app, headers):
        return True, {"http_status": 200, "body": {"status": "ready", "db": "ok"}}

    result = await system_check.check_sidecar(app=object(), probe=probe)
    assert result["status"] == "pass"
    assert result["measured"]["live_ok"] is True


@pytest.mark.asyncio
async def test_sidecar_fail_when_endpoint_down():
    def probe(app, headers):
        raise ConnectionError("connection refused")

    result = await system_check.check_sidecar(app=object(), probe=probe)
    assert result["status"] == "fail"
    assert "did not respond" in result["detail"]


@pytest.mark.asyncio
async def test_sidecar_fail_when_db_not_ready():
    async def probe(app, headers):
        return True, {"http_status": 503, "body": {"error": "not_ready"}}

    result = await system_check.check_sidecar(app=object(), probe=probe)
    assert result["status"] == "fail"


@pytest.mark.asyncio
async def test_sidecar_no_app_handle_is_honest_failure():
    result = await system_check.check_sidecar(app=None)
    assert result["status"] == "fail"


# ---------------------------------------------------------------------------
# Credential-store selection seam
# ---------------------------------------------------------------------------
class _FakeKrModule:
    """Minimal stand-in for the keyring module (selection seam only)."""

    def __init__(self, backend):
        self._backend = backend
        self.set_calls = []

    def get_keyring(self):
        return self._backend

    def set_keyring(self, backend):
        self.set_calls.append(backend)
        self._backend = backend


class _FailBackend:
    pass


def test_select_backend_on_windows_pins_credential_manager():
    fake = _FakeKrModule(_FailBackend())
    name = secrets_store.select_keyring_backend(fake, platform="win32")
    assert fake.set_calls, "Windows selection must install a backend explicitly"
    assert "WinVaultKeyring" in type(fake.set_calls[0]).__name__
    assert "WinVaultKeyring" in name


def test_select_backend_off_windows_leaves_selection_alone():
    fake = _FakeKrModule(_FailBackend())
    secrets_store.select_keyring_backend(fake, platform="linux")
    assert fake.set_calls == []


def test_credential_store_info_windows_flag_is_honest(monkeypatch):
    fake = _FakeKrModule(_FailBackend())
    monkeypatch.setattr(secrets_store, "keyring", fake)
    win = secrets_store.credential_store_info(platform="win32")
    assert win["windows_credential_manager"] is True
    linux = secrets_store.credential_store_info(platform="linux")
    assert linux["windows_credential_manager"] is False


# ---------------------------------------------------------------------------
# API surface (full app via conftest client)
# ---------------------------------------------------------------------------
def test_system_check_endpoint_shape_and_honesty(client):
    resp = client.get("/api/v1/onboarding/system-check")
    assert resp.status_code == 200
    body = resp.json()
    ids = [c["id"] for c in body["checks"]]
    assert ids == ["webview2", "ffmpeg", "disk", "sidecar"]
    by_id = {c["id"]: c for c in body["checks"]}
    # WebView2 is Windows-only: off Windows the honest result is
    # "unverified" (never pass, never fail); on win32 the app performs
    # the real registry/filesystem lookup and a machine with the
    # runtime installed (e.g. the Windows CI runner) reports "pass".
    if sys.platform.startswith("win"):
        assert by_id["webview2"]["status"] == "pass"
    else:
        assert by_id["webview2"]["status"] == "unverified"
    # Disk reports a real number.
    assert by_id["disk"]["measured"]["free_bytes"] > 0
    assert by_id["disk"]["status"] in ("pass", "warn")
    # The sidecar really answered its own health endpoints.
    assert by_id["sidecar"]["status"] == "pass"
    # Credential store selection is reported honestly: Windows
    # Credential Manager is pinned only on win32 (the first-failure
    # webview2 assert above previously masked this line on Windows).
    assert body["credential_store"]["windows_credential_manager"] is (
        sys.platform.startswith("win")
    )
    assert body["overall"] in ("pass_with_unverified", "warn", "pass")


def test_system_check_endpoint_ffmpeg_missing_is_failure(client, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "ffmpeg" else real_which(name)
    )
    resp = client.get("/api/v1/onboarding/system-check")
    assert resp.status_code == 200
    by_id = {c["id"]: c for c in resp.json()["checks"]}
    assert by_id["ffmpeg"]["status"] == "fail"
    assert resp.json()["overall"] == "fail"


@pytest.fixture()
def no_preexisting_channels(client):
    """Isolate the status test's YouTube leg from other suites' rows.

    The backend suite shares ONE session DB (see tests/conftest.py),
    and suites collected earlier seed realistic channel rows into it —
    including OAuth-state rows (connect_status in config_json) from
    the channels-connect suite. The status test's premise is a fresh
    install ("nothing saved yet"), so soft-delete every live channel
    row for its duration and restore exactly those rows afterwards.
    Assertions are untouched; this is isolation, not leniency.
    """
    from app import db as db_module

    def _live_ids(conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        if "deleted_at" not in cols:
            return None
        return [
            row[0]
            for row in conn.execute(
                "SELECT id FROM channels WHERE deleted_at IS NULL"
            )
        ]

    conn = db_module.get_connection()
    try:
        live_ids = _live_ids(conn)
        if live_ids:
            now = int(time.time() * 1000)
            conn.execute(
                "UPDATE channels SET deleted_at = ? "
                "WHERE deleted_at IS NULL",
                (now,),
            )
    finally:
        conn.close()
    yield
    if live_ids:
        conn = db_module.get_connection()
        try:
            for channel_id in live_ids:
                conn.execute(
                    "UPDATE channels SET deleted_at = NULL WHERE id = ?",
                    (channel_id,),
                )
        finally:
            conn.close()


def test_status_endpoint_honest_key_states(
    client, fake_keyring, no_preexisting_channels
):
    # Nothing saved yet.
    body = client.get("/api/v1/onboarding/status").json()
    assert body["completed"] is False
    assert body["keys"]["gemini"]["state"] == "missing"
    assert body["keys"]["pixabay"]["state"] == "missing"
    assert body["youtube"]["state"] in ("not_configured", "ready_to_connect")
    assert body["youtube"]["connected"] is False

    # Save fake keys through the EXISTING providers endpoint (secret layer).
    for provider, value in (("gemini", FAKE_GEMINI_KEY), ("pixabay", FAKE_PIXABAY_KEY)):
        resp = client.put(f"/api/v1/providers/{provider}/key", json={"value": value})
        assert resp.status_code == 200
        assert value not in resp.text  # never echoed back

    body = client.get("/api/v1/onboarding/status").json()
    for provider in ("gemini", "pixabay"):
        entry = body["keys"][provider]
        # Saved but never tested => NOT verified. This is the honesty core.
        assert entry["state"] == "saved_unverified"
        assert entry["configured"] is True
        assert entry["verified"] is False
        assert "not yet verified" in entry["detail"]
    # No key material anywhere in the status payload.
    import json as _json

    assert FAKE_GEMINI_KEY not in _json.dumps(body)
    assert FAKE_PIXABAY_KEY not in _json.dumps(body)


def test_complete_with_skip_is_first_class(client):
    resp = client.post("/api/v1/onboarding/complete", json={"test_video": "skip"})
    assert resp.status_code == 200
    assert resp.json()["completed"] is True
    assert resp.json()["test_video"] == "skip"
    body = client.get("/api/v1/onboarding/status").json()
    assert body["completed"] is True
    assert body["test_video_choice"] == "skip"


def test_complete_rejects_bad_choice(client):
    resp = client.post("/api/v1/onboarding/complete", json={"test_video": "maybe"})
    assert resp.status_code == 422


def test_test_video_requires_channel_and_niche(client):
    resp = client.post(
        "/api/v1/onboarding/test-video",
        json={"channel_id": "", "niche_profile_id": ""},
    )
    assert resp.status_code == 422


def test_test_video_creates_project_via_produce_path(client):
    resp = client.post(
        "/api/v1/onboarding/test-video",
        json={
            "channel_id": "chan-test",
            "niche_profile_id": "niche-facts",
            "topic": "Test topic",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["target_minutes"] == 1
    assert body["estimated_duration_s"] == 60
    assert body["status"] in ("queued", "created_not_queued")
    if body["status"] == "created_not_queued":
        assert "NOT started" in body["detail"]
    # The project really exists through the normal projects surface.
    got = client.get(f"/api/v1/projects/{body['project_id']}")
    assert got.status_code == 200
    assert got.json()["target_minutes"] == 1
