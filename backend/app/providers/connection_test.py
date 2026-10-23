"""Real provider test-connection service (roadmap section 28).

``POST /api/v1/providers/{id}/test`` delegates here. Per the section 28
contract a test is the CHEAPEST authenticated call per provider — never
a quota-costing one:

==================  =====================================================
provider            test call
==================  =====================================================
gemini              ``GeminiAdapter.list_models()`` — GET /models
openai              ``OpenAIAdapter.list_models()`` — GET /models
anthropic           ``AnthropicAdapter.list_models()`` — GET /models
pixabay             ``PixabayProvider.test_connection()`` — one minimal
                    image search (per_page=3, Pixabay's minimum)
pexels              ``PexelsProvider.test_connection()`` — one minimal
                    photo search (per_page=1)
elevenlabs          ``ElevenLabsAdapter.subscription_info()`` —
                    GET /v1/user/subscription (also the balance fetch)
azure_tts           ``AzureSpeechAdapter.list_voices()`` — GET voices/list
==================  =====================================================

Result vocabulary (the endpoint answers HTTP 200 for all three states;
a missing key is a normal state, NOT an error and NOT "Connected"):

* ``not_configured`` — no key in the secret store; no call was made.
* ``ok`` — key present AND the cheap call succeeded. Only this state may
  be treated downstream as "connected".
* ``error`` — key present but the call failed; ``category`` is one of
  the section 28 taxonomy: ``invalid_key`` / ``wrong_project`` /
  ``quota_exhausted`` / ``insufficient_credits`` / ``network`` /
  ``timeout`` / ``model_not_available``. The provider's own message is
  kept only in the ``detail`` field (redacted + truncated).

Keys never appear in results or logs: the fetched key is registered
with :mod:`app.redaction` before any call so even a provider message
that echoes it is scrubbed, and every detail string passes through
``redact_text``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.providers.http_client import create_async_client
from app.redaction import redact_text, register_secret

logger = logging.getLogger(__name__)

#: Providers with a real cheapest-call test. ``google_tts`` has no
#: implemented adapter yet (Phase-2 stub in the TTS layer), so it is
#: deliberately absent: its test reports an honest error, never success.
TESTABLE_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "openai",
    "anthropic",
    "elevenlabs",
    "pexels",
    "pixabay",
    "azure_tts",
)

CATEGORIES: tuple[str, ...] = (
    "invalid_key",
    "wrong_project",
    "quota_exhausted",
    "insufficient_credits",
    "network",
    "timeout",
    "model_not_available",
)

_DETAIL_LIMIT = 300


def _key_ref(provider_id: str) -> str:
    return f"apikey/{provider_id}"


def _clean_detail(text: Any) -> str:
    cleaned = redact_text(str(text or "")).strip()
    return cleaned[:_DETAIL_LIMIT]


def _result(
    provider_id: str,
    status: str,
    *,
    category: str | None = None,
    detail: str = "",
    info: dict | None = None,
    latency_ms: int = 0,
) -> dict:
    return {
        "provider": provider_id,
        "status": status,
        "ok": status == "ok",
        "category": category,
        "detail": _clean_detail(detail),
        "info": info or {},
        "latency_ms": latency_ms,
    }


# ---------------------------------------------------------------------------
# Error taxonomy mapping (section 28)
# ---------------------------------------------------------------------------
def _category_for_status(status: int | None, text: str) -> str:
    """Map an HTTP status + provider message onto the §28 taxonomy.

    Body markers win over the bare status where providers overload codes
    (e.g. Gemini answers 400 for an invalid key AND for a disabled API;
    ElevenLabs answers 401 with a quota/credits payload once a plan's
    characters are spent).
    """
    lowered = (text or "").lower()
    if any(
        marker in lowered
        for marker in (
            "quota",
            "resource_exhausted",
            "rate limit",
            "rate_limit",
            "too many requests",
        )
    ):
        return "quota_exhausted"
    if any(
        marker in lowered
        for marker in (
            "insufficient",
            "insufficient_quota",
            "billing",
            "credit balance",
            "credits are finished",
            "payment required",
        )
    ):
        return "insufficient_credits"
    if any(
        marker in lowered
        for marker in (
            "service_disabled",
            "api_key_service_not_enabled",
            "has not been used in project",
            "api not enabled",
            "not been enabled",
        )
    ):
        return "wrong_project"
    if any(
        marker in lowered
        for marker in (
            "model not found",
            "unknown model",
            "no such model",
            "model is not available",
            "model not available",
        )
    ):
        return "model_not_available"
    if status == 429:
        return "quota_exhausted"
    if status == 402:
        return "insufficient_credits"
    if status in (401, 403):
        return "invalid_key"
    if status == 404:
        return "model_not_available"
    if status == 400:
        # Gemini/OpenAI report an invalid key as 400 ("API key not valid").
        return "invalid_key"
    # 5xx and anything unexpected: the provider could not be reached in a
    # usable way — the taxonomy's closest plain category is network.
    return "network"


def _classify_exception(exc: Exception) -> tuple[str, str]:
    """Return ``(category, provider_message)`` for any adapter failure."""
    import httpx as _httpx

    if isinstance(exc, _httpx.TimeoutException):
        return "timeout", str(exc) or f"{type(exc).__name__}"
    if isinstance(exc, _httpx.HTTPStatusError):
        resp = exc.response
        return (
            _category_for_status(resp.status_code, resp.text),
            f"HTTP {resp.status_code}: {resp.text}",
        )
    if isinstance(exc, _httpx.TransportError):
        return "network", str(exc) or f"{type(exc).__name__}"

    # LLM layer taxonomy (app.providers.llm.errors).
    from app.providers.llm import errors as llm_errors

    if isinstance(exc, llm_errors.RateLimitError):
        return "quota_exhausted", str(exc)
    if isinstance(exc, llm_errors.ModelRegistryError):
        return "model_not_available", str(exc)
    if isinstance(exc, llm_errors.ProviderConfigError):
        return _category_for_status(exc.status_code, str(exc)), str(exc)
    if isinstance(exc, llm_errors.TransientProviderError):
        # BaseAdapter keeps the httpx type name in the message
        # ("transport failure: ReadTimeout") so timeouts stay distinct.
        if "timeout" in f"{type(exc).__name__} {exc}".lower():
            return "timeout", str(exc)
        return "network", str(exc)

    # TTS layer (ElevenLabs/Azure): ProviderError carries a status code;
    # ProviderUnavailable embeds it in the message ("azure: HTTP 401 ...").
    from app.providers.tts.base import ProviderError as TTSErrorWithStatus

    if isinstance(exc, TTSErrorWithStatus) and exc.status_code is not None:
        return _category_for_status(exc.status_code, str(exc)), str(exc)
    status_match = re.search(r"HTTP (\d{3})", str(exc))
    if status_match:
        status = int(status_match.group(1))
        return _category_for_status(status, str(exc)), str(exc)
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return _category_for_status(status_code, str(exc)), str(exc)
    return "network", str(exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# Per-provider cheapest calls. Each returns an ``info`` dict on success.
# ---------------------------------------------------------------------------
async def _test_gemini(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.llm.gemini import GeminiAdapter

    adapter = GeminiAdapter(secrets, key_ref, transport=transport)
    models = await adapter.list_models(timeout_s=timeout_s)
    return {"model_count": len(models)}


async def _test_openai(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.llm.openai import OpenAIAdapter

    adapter = OpenAIAdapter(secrets, key_ref, transport=transport)
    models = await adapter.list_models(timeout_s=timeout_s)
    return {"model_count": len(models)}


async def _test_anthropic(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.llm.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(secrets, key_ref, transport=transport)
    models = await adapter.list_models(timeout_s=timeout_s)
    return {"model_count": len(models)}


def _stock_client(transport, timeout_s: float) -> httpx.AsyncClient | None:
    if transport is None:
        return None
    # Shared factory (app.providers.http_client): trust_env=False plus
    # sanitised proxy handling — proxy env vars must never break (or
    # silently reroute) a test call.
    return create_async_client(transport=transport, timeout=timeout_s)


async def _test_pixabay(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.stock.pixabay import PixabayProvider

    client = _stock_client(transport, timeout_s)
    provider = PixabayProvider(secrets, key_ref, http_client=client)
    try:
        return await provider.test_connection(timeout_s=timeout_s)
    finally:
        if client is not None:
            await client.aclose()


async def _test_pexels(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.stock.pexels import PexelsProvider

    client = _stock_client(transport, timeout_s)
    provider = PexelsProvider(secrets, key_ref, http_client=client)
    try:
        return await provider.test_connection(timeout_s=timeout_s)
    finally:
        if client is not None:
            await client.aclose()


async def _test_elevenlabs(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.tts.adapters import ElevenLabsAdapter

    client = _stock_client(transport, timeout_s)
    adapter = ElevenLabsAdapter(secrets, key_ref, client=client)
    try:
        return await adapter.subscription_info(timeout_s=timeout_s)
    finally:
        if client is not None:
            await client.aclose()


async def _test_azure_tts(secrets, key_ref: str, transport, timeout_s: float) -> dict:
    from app.providers.tts.adapters import AzureSpeechAdapter

    client = _stock_client(transport, timeout_s)
    adapter = AzureSpeechAdapter(secrets, key_ref, client=client)
    try:
        voices = await adapter.list_voices()
        return {"voice_count": len(voices)}
    finally:
        if client is not None:
            await client.aclose()


_TESTERS = {
    "gemini": _test_gemini,
    "openai": _test_openai,
    "anthropic": _test_anthropic,
    "pixabay": _test_pixabay,
    "pexels": _test_pexels,
    "elevenlabs": _test_elevenlabs,
    "azure_tts": _test_azure_tts,
}


def _success_detail(provider_id: str, info: dict) -> str:
    if "model_count" in info:
        return f"Connected — {info['model_count']} models available."
    if provider_id == "elevenlabs":
        tier = info.get("tier")
        return f"Connected — ElevenLabs tier '{tier}'." if tier else "Connected."
    if "total_hits" in info:
        return f"Connected — search reports {info['total_hits']} results."
    if "total_results" in info:
        return f"Connected — search reports {info['total_results']} results."
    if "voice_count" in info:
        return f"Connected — {info['voice_count']} voices available."
    return "Connected."


async def run_provider_test(
    provider_id: str,
    secret_provider,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 10.0,
) -> dict:
    """Run the §28 test for ``provider_id`` and return the result dict.

    ``secret_provider`` is any implementation of the shared async
    ``get_secret(ref)`` protocol (keyring-backed in production). The
    ``transport`` parameter exists so tests can inject
    ``httpx.MockTransport`` — production passes ``None``.
    """
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    key_ref = _key_ref(provider_id)
    try:
        key = await secret_provider.get_secret(key_ref)
    except Exception:
        # Credential store unavailable is surfaced exactly like the
        # providers list endpoint surfaces it: as "not configured"
        # rather than a 503 stack (see app.routers.providers._configured).
        key = None
    if not key:
        return _result(
            provider_id,
            "not_configured",
            detail=f"No API key configured for {provider_id}.",
            latency_ms=elapsed_ms(),
        )
    # Scrub this exact value from anything the provider echoes back.
    register_secret(key)

    tester = _TESTERS.get(provider_id)
    if tester is None:
        return _result(
            provider_id,
            "error",
            category="model_not_available",
            detail=(
                f"No connection test is implemented for provider "
                f"'{provider_id}' yet."
            ),
            latency_ms=elapsed_ms(),
        )
    try:
        info = await tester(secret_provider, key_ref, transport, timeout_s)
    except Exception as exc:  # taxonomy mapping is the whole point here
        category, message = _classify_exception(exc)
        logger.info(
            "provider test failed provider=%s category=%s", provider_id, category
        )
        return _result(
            provider_id,
            "error",
            category=category,
            detail=message,
            latency_ms=elapsed_ms(),
        )
    logger.info("provider test ok provider=%s", provider_id)
    return _result(
        provider_id,
        "ok",
        detail=_success_detail(provider_id, info),
        info=info,
        latency_ms=elapsed_ms(),
    )


__all__ = [
    "CATEGORIES",
    "TESTABLE_PROVIDERS",
    "run_provider_test",
]
