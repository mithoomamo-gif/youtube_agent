"""Development-only secret providers backed by environment variables / ``.env``.

Production secret storage is the OS keyring (Windows Credential Manager),
accessed through :mod:`app.secrets_store`. That path is UNCHANGED by this
module and remains the default everywhere (see :func:`create_secret_provider`).

This module exists so a developer can run the backend and construct any
provider adapter locally without a working keyring (e.g. headless Linux,
where no Secret Service / KWallet is running):

.. code-block:: python

    from app.providers.secrets_env import EnvSecretProvider

    secrets = EnvSecretProvider()  # process env + optional local .env file

    pixabay = PixabayProvider(secrets, "pixabay_api_key")   # -> PIXABAY_API_KEY
    pexels = PexelsProvider(secrets, "pexels_api_key")      # -> PEXELS_API_KEY
    gemini = GeminiAdapter(secrets, "apikey/gemini")        # -> GEMINI_API_KEY
    azure = AzureSpeechAdapter(secrets, "apikey/azure_tts") # -> AZURE_SPEECH_KEY
    eleven = ElevenLabsAdapter(secrets, "apikey/elevenlabs")
    flux = FluxApiAdapter(secrets)                          # -> BFL_API_KEY
    # YouTube (sync SecretStore protocol instead of async SecretProvider):
    tokens = TokenManager(EnvSecretStore(), client_id=..., client_secret=...)

Manual wiring is required on purpose: there is no composition-root factory
in the codebase yet, and this module deliberately does NOT install itself
anywhere. The only selection helper, :func:`create_secret_provider`,
returns the keyring-backed provider unless the caller (or the dev-only
``YTA_SECRET_BACKEND=env`` environment variable) explicitly asks for the
env backend.

Interface conformance
---------------------
Every provider package defines the same async protocol (duplicated per
package, all ``runtime_checkable``)::

    async def get_secret(self, ref: str) -> str | None: ...

(``app.providers.llm.models``, ``app.providers.stock.base``,
``app.providers.tts.base``, ``app.providers.images.models``.)
:class:`EnvSecretProvider` implements exactly that: it is async, takes the
adapter's opaque ``key_ref`` string, returns the value when present, and
returns ``None`` for a missing / empty / whitespace-only value — the same
missing-key behaviour as ``DictSecretProvider`` in
``app.providers.stock.base``. Adapters translate ``None`` into their own
"provider unavailable / no API key" error, exactly as with the keyring.

Ref -> env var mapping
----------------------
Known refs (every spelling used in the codebase: stock defaults
``pixabay_api_key`` / ``pexels_api_key``, the FLUX default ``bfl/api_key``,
and the keyring refs ``apikey/<provider>`` from
``app.routers.providers``) are mapped explicitly in :data:`REF_ENV_ALIASES`.
Any other ref falls back to a generic normalisation (upper-case,
non-alphanumeric -> ``_``), so ad-hoc refs like ``"my/custom-ref"`` resolve
from ``MY_CUSTOM_REF``.

``.env`` file loading
---------------------
No third-party dependency: :func:`parse_dotenv` is a small hand parser
(``python-dotenv`` is not a project dependency). Process environment
values always win over ``.env`` file values, and loading a file NEVER
mutates ``os.environ``. By default the provider looks for ``.env`` in the
current working directory, then at the repository root; pass
``dotenv_path=False`` to disable file loading or a path to pin the file.
Copy ``.env.example`` (repo root) to ``.env`` and fill in your own keys —
``.env`` is git-ignored and must never be committed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

__all__ = [
    "REF_ENV_ALIASES",
    "EnvSecretProvider",
    "EnvSecretStore",
    "KeyringSecretProvider",
    "create_secret_provider",
    "load_dotenv_file",
    "parse_dotenv",
]


# ---------------------------------------------------------------------------
# Ref -> env var aliases
# ---------------------------------------------------------------------------
#: Normalised ref (see :func:`_normalise_ref`) -> candidate env var names,
#: in priority order. Covers every key the app can consume.
REF_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    # LLM providers (app.providers.llm; keyring refs apikey/<provider>)
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "gemini_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "apikey_gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "openai_key": ("OPENAI_API_KEY",),
    "apikey_openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "anthropic_key": ("ANTHROPIC_API_KEY",),
    "apikey_anthropic": ("ANTHROPIC_API_KEY",),
    # Stock providers (defaults in app.providers.stock.pixabay / .pexels)
    "pixabay": ("PIXABAY_API_KEY",),
    "pixabay_api_key": ("PIXABAY_API_KEY",),
    "apikey_pixabay": ("PIXABAY_API_KEY",),
    "pexels": ("PEXELS_API_KEY",),
    "pexels_api_key": ("PEXELS_API_KEY",),
    "apikey_pexels": ("PEXELS_API_KEY",),
    # Image generation — Black Forest Labs FLUX API (default ref bfl/api_key)
    "bfl": ("BFL_API_KEY", "FLUX_API_KEY"),
    "bfl_api_key": ("BFL_API_KEY", "FLUX_API_KEY"),
    "apikey_bfl": ("BFL_API_KEY", "FLUX_API_KEY"),
    "black_forest_labs": ("BFL_API_KEY", "FLUX_API_KEY"),
    "flux": ("BFL_API_KEY", "FLUX_API_KEY"),
    "flux_api_key": ("BFL_API_KEY", "FLUX_API_KEY"),
    # TTS providers (app.providers.tts.adapters)
    "elevenlabs": ("ELEVENLABS_API_KEY",),
    "elevenlabs_key": ("ELEVENLABS_API_KEY",),
    "apikey_elevenlabs": ("ELEVENLABS_API_KEY",),
    "azure": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "azure_key": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "azure_tts": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "azure_speech": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "apikey_azure": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "apikey_azure_tts": ("AZURE_SPEECH_KEY", "AZURE_TTS_API_KEY", "AZURE_API_KEY"),
    "google": ("GOOGLE_API_KEY", "GOOGLE_TTS_API_KEY"),
    "google_key": ("GOOGLE_API_KEY", "GOOGLE_TTS_API_KEY"),
    "google_tts": ("GOOGLE_API_KEY", "GOOGLE_TTS_API_KEY"),
    "apikey_google_tts": ("GOOGLE_API_KEY", "GOOGLE_TTS_API_KEY"),
    # YouTube OAuth desktop-app credentials (app.providers.youtube.oauth)
    "youtube_client_id": ("YOUTUBE_CLIENT_ID",),
    "youtube_client_secret": ("YOUTUBE_CLIENT_SECRET",),
}


def _normalise_ref(ref: str) -> str:
    """``bfl/api_key`` -> ``bfl_api_key``; ``apikey/gemini`` -> ``apikey_gemini``."""
    return re.sub(r"[^A-Za-z0-9]+", "_", ref.strip()).strip("_").lower()


def _generic_env_name(ref: str) -> str:
    """Fallback env var name for an unlisted ref (``my/ref-x`` -> ``MY_REF_X``)."""
    return _normalise_ref(ref).upper()


# ---------------------------------------------------------------------------
# Tiny .env parser (no third-party dependency)
# ---------------------------------------------------------------------------
def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` content into a dict.

    Supports blank lines, ``#`` comments (full-line and, for unquoted
    values, inline after whitespace), an optional ``export `` prefix,
    whitespace around ``=``, and single- / double-quoted values (double
    quotes process ``\\n`` / ``\\t`` / ``\\"`` / ``\\\\`` escapes). Lines
    without ``=`` and empty keys are ignored; a key with no value maps to
    ``""`` (which callers treat as missing).
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value[:1] in ("'", '"') and len(value) >= 2:
            quote = value[0]
            end = value.find(quote, 1)
            inner = value[1:end] if end != -1 else value[1:]
            if quote == '"':
                inner = (
                    inner.replace("\\n", "\n")
                    .replace("\\t", "\t")
                    .replace('\\"', '"')
                    .replace("\\\\", "\\")
                )
            values[key] = inner
        else:
            # Unquoted: an inline comment starts at whitespace + '#'.
            comment = re.search(r"\s+#", value)
            if comment:
                value = value[: comment.start()].rstrip()
            values[key] = value
    return values


def load_dotenv_file(path: str | Path) -> dict[str, str]:
    """Read and parse one ``.env`` file; a missing file yields ``{}``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_dotenv(text)


def _discover_dotenv() -> Path | None:
    """First ``.env`` found: current working directory, then repo root."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[3] / ".env",  # repo root
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Env-backed providers
# ---------------------------------------------------------------------------
class _EnvResolver:
    """Shared merged view: ``.env`` file values under environment values."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        dotenv_path: str | Path | bool | None = None,
    ) -> None:
        file_values: dict[str, str] = {}
        if dotenv_path is not False:
            path = (
                Path(dotenv_path)
                if isinstance(dotenv_path, (str, Path))
                else _discover_dotenv()
            )
            if path is not None:
                file_values = load_dotenv_file(path)
        env_values = dict(os.environ) if environ is None else dict(environ)
        # Process environment wins over the .env file (dotenv convention).
        self._values: dict[str, str] = {**file_values, **env_values}

    def resolve(self, ref: str) -> str | None:
        """Return the value for ``ref``, or ``None`` when missing/empty."""
        candidates: list[str] = []
        normalised = _normalise_ref(ref)
        candidates.extend(REF_ENV_ALIASES.get(normalised, ()))
        generic = _generic_env_name(ref)
        if generic and generic not in candidates:
            candidates.append(generic)
        if ref not in candidates:
            candidates.append(ref)  # ref may already be an env var name
        for name in candidates:
            value = self._values.get(name)
            if value is not None and value.strip():
                return value.strip()
        return None


class EnvSecretProvider:
    """``SecretProvider`` backed by env vars / a local ``.env`` file (DEV ONLY).

    Implements the shared async protocol
    ``async def get_secret(self, ref: str) -> str | None`` used by the LLM,
    stock, TTS and image provider packages. Missing or empty values resolve
    to ``None`` — the interface's standard missing-key behaviour.
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        dotenv_path: str | Path | bool | None = None,
    ) -> None:
        self._resolver = _EnvResolver(environ, dotenv_path=dotenv_path)

    async def get_secret(self, ref: str) -> str | None:
        return self._resolver.resolve(ref)


class EnvSecretStore:
    """Sync ``SecretStore`` (YouTube) backed by env / ``.env`` (DEV ONLY).

    ``get`` resolves through the same ref mapping as
    :class:`EnvSecretProvider`. ``put`` / ``delete`` act on an in-memory
    overlay only — nothing is ever written back to the environment or to
    the ``.env`` file — so OAuth token persistence lasts for the process
    lifetime, which is all a dev run needs.
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        dotenv_path: str | Path | bool | None = None,
    ) -> None:
        self._resolver = _EnvResolver(environ, dotenv_path=dotenv_path)
        self._overlay: dict[str, str] = {}
        self._deleted: set[str] = set()

    def get(self, ref: str) -> str | None:
        if ref in self._overlay:
            return self._overlay[ref]
        if ref in self._deleted:
            return None
        return self._resolver.resolve(ref)

    def put(self, ref: str, value: str) -> None:
        self._overlay[ref] = value
        self._deleted.discard(ref)

    def delete(self, ref: str) -> None:
        self._overlay.pop(ref, None)
        self._deleted.add(ref)


# ---------------------------------------------------------------------------
# Production default + explicit selection
# ---------------------------------------------------------------------------
class KeyringSecretProvider:
    """``SecretProvider`` adapter over the production keyring store.

    Thin async wrapper around :mod:`app.secrets_store` (OS keyring /
    Windows Credential Manager). This is the production default; its
    behaviour — including ``ApiError`` 503 ``credential_store_unavailable``
    on backend failure — is exactly that of ``app.secrets_store``.
    """

    async def get_secret(self, ref: str) -> str | None:
        from app import secrets_store

        return secrets_store.get_secret(ref)


def create_secret_provider(
    backend: str | None = None, **env_kwargs
) -> EnvSecretProvider | KeyringSecretProvider:
    """Return a ``SecretProvider`` for ``backend`` (default: ``"keyring"``).

    ``backend`` falls back to the dev-only ``YTA_SECRET_BACKEND``
    environment variable and then to ``"keyring"``, so production runs
    keep using the OS credential store unless a developer explicitly opts
    into ``"env"``. ``env_kwargs`` are forwarded to
    :class:`EnvSecretProvider` when the env backend is selected.
    """
    chosen = (backend or os.environ.get("YTA_SECRET_BACKEND") or "keyring")
    chosen = chosen.strip().lower()
    if chosen == "keyring":
        return KeyringSecretProvider()
    if chosen == "env":
        return EnvSecretProvider(**env_kwargs)
    raise ValueError(
        f"unknown secret backend {chosen!r} (expected 'keyring' or 'env')"
    )
