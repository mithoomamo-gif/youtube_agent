"""Gemini LIVE smoke tests — real network, real key, on demand only.

These tests are the key-ready harness for Round 2 integration. They are
NOT part of the normal (mocked) provider suite in ``tests/providers/``:

* Every test here is marked ``live`` (marker registered in
  ``backend/pytest.ini``; same marker name/semantics as the repo-root
  ``tests/pytest.ini``).
* Every test SKIPS unless the ``GEMINI_API_KEY`` environment variable is
  present and non-empty. No key is hardcoded, committed, or logged here.

How the harness plugs into the existing Round-1 code
----------------------------------------------------
Construction matches the mocked tests in ``tests/providers/test_llm_layer.py``
exactly — the adapter is built as::

    GeminiAdapter(secret_provider, key_ref, transport=None, base_url=None)

with the adapter defaults doing the rest:

* base URL: ``GeminiAdapter`` falls back to
  ``app.providers.llm.gemini.DEFAULT_BASE``
  (``https://generativelanguage.googleapis.com/v1beta``);
* model: no model ID is hardcoded here — the entry is the T0 Gemini entry
  loaded from ``backend/config/models.yaml`` via ``ModelRegistry``
  (currently ``gemini-3.1-flash-lite``), passed to
  ``GeminiAdapter.generate(req, entry)`` exactly as the router passes it;
* key: the adapter obtains it only via ``SecretProvider.get_secret(key_ref)``
  (``BaseAdapter._api_key``) and sends it as the ``x-goog-api-key`` header.

Key wiring (dev only — see README.md in this directory)
-------------------------------------------------------
This harness builds NO secret loader of its own. It uses the Round-2
env-backed ``SecretProvider`` built by a sibling worker,
``app.providers.secrets_env.EnvSecretProvider``, whose ref mapping
resolves the production keyring reference ``apikey/gemini`` (the same ref
``app.routers.providers._keyring_ref("gemini")`` uses for the
keyring-backed store in production) to ``GEMINI_API_KEY``. The fixture
value is passed in as the provider's explicit environment mapping with
``dotenv_path=False``, so the process environment variable is the ONLY
dev-time key source for this harness (no ``.env`` file is consulted).

Models-list note
----------------
``GeminiAdapter`` (Round 1) exposes no public models-list method — its
only public call is ``generate()``. The models-list smoke below therefore
makes its one GET through the adapter's *own* plumbing
(``adapter._api_key()`` for the key, ``adapter._client()`` for the HTTP
client, ``adapter.base_url or DEFAULT_BASE`` for the base URL) instead of
re-implementing the adapter's ``generateContent`` logic with raw httpx.
If a public ``list_models()`` is added to the adapter later, that one
helper is the only place to update.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.providers.llm import GeminiAdapter, LLMRequest, ModelRegistry, Tier
from app.providers.llm.gemini import DEFAULT_BASE
from app.providers.llm.registry import ModelEntry
from app.providers.secrets_env import EnvSecretProvider

pytestmark = pytest.mark.live

ENV_VAR = "GEMINI_API_KEY"

#: Production keyring reference for the Gemini API key:
#: ``app.routers.providers._keyring_ref("gemini")`` == ``"apikey/gemini"``,
#: stored under keyring service ``"YouTubeAgent"`` (app.secrets_store).
KEY_REF = "apikey/gemini"

BACKEND_DIR = Path(__file__).resolve().parents[2]
REGISTRY_PATH = BACKEND_DIR / "config" / "models.yaml"

SKIP_REASON = (
    "GEMINI_API_KEY is not set — Gemini live tests are opt-in only. "
    "Set GEMINI_API_KEY in the environment (dev) to run them; in "
    "production the key is supplied via app Settings -> Windows "
    "Credential Manager (keyring SecretProvider), never via env. "
    "See README.md in this directory."
)


def run(coro):
    """Sync wrapper, same pattern as tests/providers/test_llm_layer.py.

    Used instead of relying on pytest-asyncio mode so this file behaves
    identically under backend/pytest.ini and pyproject config.
    """
    return asyncio.run(coro)


@pytest.fixture()
def gemini_api_key() -> str:
    key = os.environ.get(ENV_VAR, "").strip()
    if not key:
        pytest.skip(SKIP_REASON)
    return key


def _make_adapter(api_key: str) -> GeminiAdapter:
    """Build the adapter exactly as Round 1 constructs it.

    No transport (real network) and no base_url override, so the adapter
    uses its own ``DEFAULT_BASE``. The key value is held only inside the
    injected SecretProvider and is fetched by the adapter via ``KEY_REF``;
    ``EnvSecretProvider`` maps that ref to ``GEMINI_API_KEY`` in the
    explicit mapping below (``dotenv_path=False``: no ``.env`` file).
    """
    secrets = EnvSecretProvider({ENV_VAR: api_key}, dotenv_path=False)
    return GeminiAdapter(secrets, KEY_REF)


def _t0_gemini_entry() -> ModelEntry:
    """The cheapest configured Gemini model: the T0 entry in models.yaml."""
    handle = ModelRegistry.load(REGISTRY_PATH)
    for entry in handle.registry.tier(Tier.T0).models:
        if entry.provider == "gemini":
            return entry
    raise AssertionError("no gemini entry configured for Tier T0 in models.yaml")


async def _list_models(adapter: GeminiAdapter) -> dict:
    """GET /models using the adapter's own key lookup + HTTP client.

    See the module docstring: the Round-1 adapter has no public
    models-list method, so this reuses its plumbing rather than
    duplicating its generateContent logic.
    """
    key = await adapter._api_key()
    base = adapter.base_url or DEFAULT_BASE
    async with adapter._client(30.0) as client:
        resp = await client.get(
            f"{base}/models", headers={"x-goog-api-key": key}
        )
    assert resp.status_code == 200, (
        f"Gemini models list failed: HTTP {resp.status_code} "
        "(check GEMINI_API_KEY is valid and the Gemini API is enabled)"
    )
    return resp.json()


def test_gemini_models_list_live(gemini_api_key: str) -> None:
    """(a) Authenticated models-list call against the real Gemini API."""
    adapter = _make_adapter(gemini_api_key)
    entry = _t0_gemini_entry()

    data = run(_list_models(adapter))

    models = data.get("models")
    assert isinstance(models, list) and models, (
        f"expected a non-empty 'models' list, got: {sorted(data)[:5]}"
    )
    names = [m.get("name", "") for m in models if isinstance(m, dict)]
    assert all(names), "every listed model should carry a 'name'"
    assert any("gemini" in name.lower() for name in names), (
        "models list contains no Gemini model"
    )
    # The model the app is configured to call should actually be offered
    # to this key (list names are 'models/<model_id>').
    assert any(name.rsplit("/", 1)[-1] == entry.model_id for name in names), (
        f"configured T0 model {entry.model_id!r} not in the models list "
        "returned for this API key"
    )


def test_gemini_minimal_completion_live(gemini_api_key: str) -> None:
    """(b) Smallest completion, through GeminiAdapter.generate() itself."""
    adapter = _make_adapter(gemini_api_key)
    entry = _t0_gemini_entry()

    async def scenario():
        return await adapter.generate(
            LLMRequest(
                tier=Tier.T0,
                prompt="Reply with the single word: OK",
                # 1 output token: the smallest completion the API allows
                # us to request (maxOutputTokens=1 in generationConfig).
                max_output_tokens=1,
                temperature=0.0,
                agent_id="live-smoke",
                task="live.gemini_smoke",
            ),
            entry,
            timeout_s=30.0,
        )

    result = run(scenario())

    # Sane-response assertions. Note on `text`: with maxOutputTokens=1,
    # Gemini may spend that budget on a thought token and return an
    # empty/partial text part, so sanity is asserted on the structured
    # result (provider/model/usage) rather than on exact text content.
    assert result.provider == "gemini"
    assert result.model == entry.model_id
    assert result.status == "ok"
    assert isinstance(result.text, str)
    assert result.usage.total_input > 0
    assert result.cost_usd >= 0.0
    assert result.latency_ms >= 0.0
