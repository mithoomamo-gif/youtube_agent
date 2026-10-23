"""Env-backed secret provider tests — unit only, no network, no keyring.

Covers app.providers.secrets_env: the dev-only EnvSecretProvider /
EnvSecretStore, the tiny .env parser, and the guarantee that the
production keyring backend stays the default.
"""

import asyncio
import os
from pathlib import Path

import pytest

from app.providers.secrets_env import (
    EnvSecretProvider,
    EnvSecretStore,
    KeyringSecretProvider,
    create_secret_provider,
    load_dotenv_file,
    parse_dotenv,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def run(coro):
    return asyncio.run(coro)


def env_provider(values, **kw):
    kw.setdefault("dotenv_path", False)  # hermetic: no file discovery
    return EnvSecretProvider(values, **kw)


# ---------------------------------------------------------------------------
# EnvSecretProvider — values from the environment mapping
# ---------------------------------------------------------------------------

def test_returns_values_for_every_known_ref_spelling():
    provider = env_provider({
        "GEMINI_API_KEY": "gemini-value",
        "OPENAI_API_KEY": "openai-value",
        "ANTHROPIC_API_KEY": "anthropic-value",
        "PIXABAY_API_KEY": "pixabay-value",
        "PEXELS_API_KEY": "pexels-value",
        "BFL_API_KEY": "bfl-value",
        "ELEVENLABS_API_KEY": "eleven-value",
        "AZURE_SPEECH_KEY": "azure-value",
        "GOOGLE_API_KEY": "google-value",
        "YOUTUBE_CLIENT_ID": "yt-id-value",
        "YOUTUBE_CLIENT_SECRET": "yt-secret-value",
    })
    # Stock adapter default refs
    assert run(provider.get_secret("pixabay_api_key")) == "pixabay-value"
    assert run(provider.get_secret("pexels_api_key")) == "pexels-value"
    # FLUX API adapter default ref
    assert run(provider.get_secret("bfl/api_key")) == "bfl-value"
    # Keyring refs used by app.routers.providers (apikey/<provider>)
    assert run(provider.get_secret("apikey/gemini")) == "gemini-value"
    assert run(provider.get_secret("apikey/openai")) == "openai-value"
    assert run(provider.get_secret("apikey/anthropic")) == "anthropic-value"
    assert run(provider.get_secret("apikey/pixabay")) == "pixabay-value"
    assert run(provider.get_secret("apikey/pexels")) == "pexels-value"
    assert run(provider.get_secret("apikey/elevenlabs")) == "eleven-value"
    assert run(provider.get_secret("apikey/azure_tts")) == "azure-value"
    assert run(provider.get_secret("apikey/google_tts")) == "google-value"
    # Bare provider names + LLM test-style refs
    assert run(provider.get_secret("gemini")) == "gemini-value"
    assert run(provider.get_secret("openai_key")) == "openai-value"
    # YouTube OAuth credentials
    assert run(provider.get_secret("youtube_client_id")) == "yt-id-value"
    assert run(provider.get_secret("youtube_client_secret")) == "yt-secret-value"


def test_reads_process_environ_by_default(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "from-process-env")
    provider = EnvSecretProvider(dotenv_path=False)
    assert run(provider.get_secret("pixabay_api_key")) == "from-process-env"


def test_missing_key_returns_none_like_the_interface_requires():
    provider = env_provider({})
    assert run(provider.get_secret("pixabay_api_key")) is None
    assert run(provider.get_secret("apikey/gemini")) is None
    assert run(provider.get_secret("totally/unknown-ref")) is None


def test_empty_and_whitespace_values_count_as_missing():
    provider = env_provider({"PIXABAY_API_KEY": "", "PEXELS_API_KEY": "   "})
    assert run(provider.get_secret("pixabay_api_key")) is None
    assert run(provider.get_secret("pexels_api_key")) is None


def test_generic_ref_normalisation_fallback():
    provider = env_provider({"MY_CUSTOM_REF": "custom-value"})
    assert run(provider.get_secret("my/custom-ref")) == "custom-value"


def test_gemini_falls_back_to_google_api_key():
    provider = env_provider({"GOOGLE_API_KEY": "google-value"})
    assert run(provider.get_secret("apikey/gemini")) == "google-value"


def test_satisfies_every_packages_secret_provider_protocol():
    from app.providers.images.models import SecretProvider as ImageSecrets
    from app.providers.llm.models import SecretProvider as LLMSecrets
    from app.providers.stock.base import SecretProvider as StockSecrets
    from app.providers.tts.base import SecretProvider as TTSSecrets

    provider = env_provider({})
    assert isinstance(provider, LLMSecrets)
    assert isinstance(provider, StockSecrets)
    assert isinstance(provider, TTSSecrets)
    assert isinstance(provider, ImageSecrets)


def test_plugs_into_real_stock_adapter_without_network():
    from app.providers.stock import PixabayProvider

    provider = PixabayProvider(
        env_provider({"PIXABAY_API_KEY": "dev-key"}), "pixabay_api_key")
    assert run(provider.is_available()) is True
    missing = PixabayProvider(env_provider({}), "pixabay_api_key")
    assert run(missing.is_available()) is False


# ---------------------------------------------------------------------------
# .env parsing + file loading
# ---------------------------------------------------------------------------

def test_parse_dotenv_comments_blanks_quotes_and_export():
    text = (
        "# a full-line comment\n"
        "\n"
        "   \n"
        "GEMINI_API_KEY=plain-value\n"
        "OPENAI_API_KEY = spaced-value \n"
        'ANTHROPIC_API_KEY="double quoted # not a comment"\n'
        "PIXABAY_API_KEY='single quoted'\n"
        "export PEXELS_API_KEY=exported-value\n"
        "BFL_API_KEY=value-with-key # inline comment\n"
        "EMPTY_KEY=\n"
        "NO_EQUALS_LINE\n"
        "=no-key-here\n"
    )
    parsed = parse_dotenv(text)
    assert parsed["GEMINI_API_KEY"] == "plain-value"
    assert parsed["OPENAI_API_KEY"] == "spaced-value"
    assert parsed["ANTHROPIC_API_KEY"] == "double quoted # not a comment"
    assert parsed["PIXABAY_API_KEY"] == "single quoted"
    assert parsed["PEXELS_API_KEY"] == "exported-value"
    assert parsed["BFL_API_KEY"] == "value-with-key"
    assert parsed["EMPTY_KEY"] == ""
    assert "NO_EQUALS_LINE" not in parsed
    assert "" not in parsed


def test_parse_dotenv_double_quote_escapes():
    parsed = parse_dotenv('K="line1\\nline2"\n')
    assert parsed["K"] == "line1\nline2"


def test_dotenv_file_loading_and_env_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# dev keys\nPIXABAY_API_KEY=file-pixabay\nPEXELS_API_KEY=file-pexels\n",
        encoding="utf-8",
    )
    provider = EnvSecretProvider(
        {"PIXABAY_API_KEY": "env-pixabay"}, dotenv_path=env_file)
    # Process env wins over the file; file-only keys still resolve.
    assert run(provider.get_secret("pixabay_api_key")) == "env-pixabay"
    assert run(provider.get_secret("pexels_api_key")) == "file-pexels"


def test_load_dotenv_file_missing_path_is_empty(tmp_path):
    assert load_dotenv_file(tmp_path / "does-not-exist.env") == {}


def test_loading_dotenv_file_never_mutates_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("PIXABAY_API_KEY=file-only-value\n", encoding="utf-8")
    provider = EnvSecretProvider({}, dotenv_path=env_file)
    assert run(provider.get_secret("pixabay_api_key")) == "file-only-value"
    assert "PIXABAY_API_KEY" not in os.environ


# ---------------------------------------------------------------------------
# EnvSecretStore — sync YouTube SecretStore protocol
# ---------------------------------------------------------------------------

def test_env_secret_store_roundtrip_and_env_seed():
    from app.providers.youtube.secrets import SecretStore

    store = EnvSecretStore({"YOUTUBE_CLIENT_SECRET": "seeded"}, dotenv_path=False)
    assert isinstance(store, SecretStore)
    assert store.get("youtube_client_secret") == "seeded"
    assert store.get("youtube:channel:chan-1") is None
    store.put("youtube:channel:chan-1", "blob-1")
    assert store.get("youtube:channel:chan-1") == "blob-1"
    store.delete("youtube:channel:chan-1")
    assert store.get("youtube:channel:chan-1") is None
    # Deleting an env-seeded ref hides it for this instance only.
    store.delete("youtube_client_secret")
    assert store.get("youtube_client_secret") is None


# ---------------------------------------------------------------------------
# Selection: production keyring stays the default
# ---------------------------------------------------------------------------

def test_factory_defaults_to_keyring_without_env_opt_in(monkeypatch):
    monkeypatch.delenv("YTA_SECRET_BACKEND", raising=False)
    provider = create_secret_provider()
    assert isinstance(provider, KeyringSecretProvider)
    assert not isinstance(provider, EnvSecretProvider)


def test_factory_selects_env_only_when_explicitly_asked(monkeypatch):
    monkeypatch.setenv("YTA_SECRET_BACKEND", "env")
    provider = create_secret_provider(dotenv_path=False)
    assert isinstance(provider, EnvSecretProvider)
    explicit = create_secret_provider("env", dotenv_path=False)
    assert isinstance(explicit, EnvSecretProvider)
    # An explicit argument beats the env var.
    monkeypatch.setenv("YTA_SECRET_BACKEND", "env")
    assert isinstance(create_secret_provider("keyring"), KeyringSecretProvider)


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        create_secret_provider("vault-please")


def test_keyring_provider_delegates_to_secrets_store(monkeypatch):
    import app.secrets_store as secrets_store

    calls = []
    monkeypatch.setattr(
        secrets_store, "get_secret",
        lambda ref: calls.append(ref) or "keyring-value")
    provider = KeyringSecretProvider()
    assert run(provider.get_secret("apikey/gemini")) == "keyring-value"
    assert calls == ["apikey/gemini"]


# ---------------------------------------------------------------------------
# .env.example stays in sync and value-free
# ---------------------------------------------------------------------------

EXPECTED_ENV_NAMES = {
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY",
    "AZURE_SPEECH_REGION",
    "PIXABAY_API_KEY",
    "PEXELS_API_KEY",
    "BFL_API_KEY",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
}


def test_env_example_lists_all_keys_with_empty_values_only():
    example = REPO_ROOT / ".env.example"
    assert example.is_file(), ".env.example must exist at the repo root"
    parsed = parse_dotenv(example.read_text(encoding="utf-8"))
    assert EXPECTED_ENV_NAMES <= set(parsed)
    assert all(value == "" for value in parsed.values()), (
        ".env.example must never contain a value that looks like a real secret"
    )
    # Every listed name actually resolves through the env provider.
    seeded = {name: f"value-for-{name}" for name in EXPECTED_ENV_NAMES}
    provider = env_provider(seeded)
    assert run(provider.get_secret("apikey/gemini")) == "value-for-GEMINI_API_KEY"
    assert run(provider.get_secret("bfl/api_key")) == "value-for-BFL_API_KEY"
