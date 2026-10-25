"""Tests for app.config (launch-env loading) and app.redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.data_dir import is_windows_platform
from app.redaction import REDACTED, redact_obj, redact_text

FULL_ENV = {
    "YTA_PORT": "49152",
    "YTA_TOKEN": "launch-token-abcdefghij",
    "YTA_BOOT_ID": "boot-123",
    "YTA_DATA_DIR": "/tmp/yta-unit-data",
    "YTA_PARENT_PID": "4242",
    "YTA_LOG_LEVEL": "debug",
    "YTA_DETACHED": "1",
}


# --------------------------------------------------------------------------
# config: env loading
# --------------------------------------------------------------------------


def test_load_settings_from_env() -> None:
    settings = load_settings(env=dict(FULL_ENV))
    assert isinstance(settings, Settings)
    assert settings.port == 49152
    assert settings.token == "launch-token-abcdefghij"
    assert settings.boot_id == "boot-123"
    assert settings.data_dir == Path("/tmp/yta-unit-data")
    assert settings.parent_pid == 4242
    assert settings.log_level == "debug"
    assert settings.detached is True
    # Derived paths
    assert settings.db_path == Path("/tmp/yta-unit-data") / "app.db"
    assert settings.host == "127.0.0.1"


def test_load_settings_defaults() -> None:
    settings = load_settings(env={"YTA_TOKEN": "tok-12345678"})
    assert settings.port == 0  # 0 = pick a free loopback port in run()
    assert settings.parent_pid is None
    assert settings.log_level == "info"
    assert settings.detached is False
    assert settings.boot_id  # generated when not supplied
    # No YTA_DATA_DIR and no APPDATA in the mapping -> platform default:
    # %APPDATA%\YouTubeAgent (Roaming reconstructed) on Windows,
    # otherwise ~/.youtube-agent
    if is_windows_platform():
        assert settings.data_dir == Path.home() / "AppData" / "Roaming" / "YouTubeAgent"
    else:
        assert settings.data_dir == Path.home() / ".youtube-agent"


def test_load_settings_detached_false_values() -> None:
    for raw in ("0", "false", "no", ""):
        settings = load_settings(env={"YTA_TOKEN": "tok-12345678", "YTA_DETACHED": raw})
        assert settings.detached is False


def test_load_settings_allow_dev_origins() -> None:
    # Default (unset) is off — the shipped sidecar never sets the env.
    settings = load_settings(env={"YTA_TOKEN": "tok-12345678"})
    assert settings.allow_dev_origins is False
    for raw in ("1", "true"):
        settings = load_settings(
            env={"YTA_TOKEN": "tok-12345678", "YTA_ALLOW_DEV_ORIGINS": raw}
        )
        assert settings.allow_dev_origins is True
    for raw in ("0", "false", ""):
        settings = load_settings(
            env={"YTA_TOKEN": "tok-12345678", "YTA_ALLOW_DEV_ORIGINS": raw}
        )
        assert settings.allow_dev_origins is False
    with pytest.raises(ValueError, match="YTA_ALLOW_DEV_ORIGINS"):
        load_settings(
            env={"YTA_TOKEN": "tok-12345678", "YTA_ALLOW_DEV_ORIGINS": "maybe"}
        )


def test_load_settings_missing_token_raises() -> None:
    with pytest.raises(ValueError, match="YTA_TOKEN"):
        load_settings(env={})
    with pytest.raises(ValueError, match="YTA_TOKEN"):
        load_settings(env={"YTA_TOKEN": "   "})


def test_load_settings_token_optional_when_not_required() -> None:
    settings = load_settings(env={}, require_token=False)
    assert settings.token == ""


def test_load_settings_invalid_port_raises() -> None:
    with pytest.raises(ValueError, match="YTA_PORT"):
        load_settings(env={"YTA_TOKEN": "tok-12345678", "YTA_PORT": "not-a-port"})
    with pytest.raises(ValueError, match="YTA_PORT"):
        load_settings(env={"YTA_TOKEN": "tok-12345678", "YTA_PORT": "70000"})


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

GOOGLE_KEY = "AIza" + "Ab1_-" * 7  # AIza + 35 chars
SK_KEY = "sk-proj-" + "a1B2c3D4" * 4


def test_redact_google_api_key() -> None:
    out = redact_text(f"failed with key {GOOGLE_KEY} in request")
    assert GOOGLE_KEY not in out
    assert REDACTED in out


def test_redact_bearer_token() -> None:
    secret = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
    out = redact_text(f"Authorization: Bearer {secret}")
    assert secret not in out
    assert f"Bearer {REDACTED}" in out


def test_redact_sk_key() -> None:
    out = redact_text(f"openai key: {SK_KEY}")
    assert SK_KEY not in out
    assert REDACTED in out


def test_redact_x_api_key_and_refresh_token() -> None:
    out = redact_text("x-api-key: supersecretvalue123")
    assert "supersecretvalue123" not in out
    out = redact_text('{"refresh_token": "1//refresh-value-xyz"}')
    assert "refresh-value-xyz" not in out


def test_redact_launch_token_header_and_exact_value() -> None:
    settings = load_settings(env=dict(FULL_ENV))
    out = redact_text("X-Local-Token: launch-token-abcdefghij")
    assert settings.token not in out
    # The exact token value is scrubbed wherever it appears after loading.
    out = redact_text(f"leaked {settings.token} in a log line")
    assert settings.token not in out
    assert REDACTED in out


def test_redact_obj_recursive() -> None:
    payload = {
        "message": f"key {GOOGLE_KEY}",
        "refresh_token": "should-be-hidden",
        "nested": {"items": [f"Bearer abcdefghij123456", {"api_key": "hidden-too"}]},
        "count": 3,
    }
    out = redact_obj(payload)
    assert GOOGLE_KEY not in out["message"]
    assert out["refresh_token"] == REDACTED
    assert "abcdefghij123456" not in out["nested"]["items"][0]
    assert out["nested"]["items"][1]["api_key"] == REDACTED
    assert out["count"] == 3
