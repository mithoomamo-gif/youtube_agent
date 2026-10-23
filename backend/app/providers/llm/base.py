"""Shared adapter plumbing: secret lookup, HTTP, error mapping.

Adapters are thin HTTP translators (httpx, no vendor SDKs). Keys come ONLY
from the injected SecretProvider via a key_ref string; key values are never
logged and never stored on the adapter.
"""

from __future__ import annotations

import time

import httpx

from ..http_client import create_async_client
from .errors import (
    ModelRegistryError,
    ProviderConfigError,
    RateLimitError,
    TransientProviderError,
)
from .models import LLMRequest, LLMResult, SecretProvider
from .registry import ModelEntry


class BaseAdapter:
    provider_name: str = "base"

    def __init__(
        self,
        secret_provider: SecretProvider,
        key_ref: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._secrets = secret_provider
        self._key_ref = key_ref
        self._transport = transport
        self.base_url = base_url

    async def _api_key(self) -> str:
        key = await self._secrets.get_secret(self._key_ref)
        if not key:
            raise ProviderConfigError(
                f"no API key available for {self.provider_name} "
                f"(ref {self._key_ref!r})",
                status_code=401,
                provider=self.provider_name,
            )
        return key

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        # Shared factory: trust_env=False + sanitised env proxy handling
        # (see app.providers.http_client — raw httpx construction dies
        # with InvalidURL ':1]' on bracketed-IPv6 NO_PROXY entries).
        return create_async_client(
            transport=self._transport, timeout=httpx.Timeout(timeout_s)
        )

    def _map_error(self, resp: httpx.Response) -> Exception:
        status = resp.status_code
        provider = self.provider_name
        snippet = resp.text[:200] if resp.text else ""
        if status == 429:
            retry_after = resp.headers.get("retry-after")
            return RateLimitError(
                f"{provider} rate limited (429)",
                provider=provider,
                retry_after_s=float(retry_after) if retry_after else None,
            )
        if status in (400, 401, 403):
            return ProviderConfigError(
                f"{provider} rejected request ({status}): {snippet}",
                status_code=status,
                provider=provider,
            )
        if status == 404:
            return ModelRegistryError(
                f"{provider} model not found (404): {snippet}"
            )
        if status >= 500:
            return TransientProviderError(
                f"{provider} transient error ({status})", provider=provider
            )
        return ProviderConfigError(
            f"{provider} unexpected status {status}: {snippet}",
            status_code=status,
            provider=provider,
        )

    async def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict,
        timeout_s: float,
    ) -> dict:
        try:
            async with self._client(timeout_s) as client:
                resp = await client.post(url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientProviderError(
                f"{self.provider_name} transport failure: {type(exc).__name__}",
                provider=self.provider_name,
            ) from exc
        if resp.status_code >= 400:
            raise self._map_error(resp)
        return resp.json()

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_s: float,
        params: dict | None = None,
    ) -> dict:
        """GET counterpart of :meth:`_post_json` (same error mapping).

        Used by the cheapest authenticated calls — models-list endpoints
        for the provider test-connection flow (roadmap section 28). The
        transport-failure message keeps the httpx exception type name so
        callers can still distinguish timeouts (``*Timeout*``) from other
        network failures after mapping to :class:`TransientProviderError`.
        """
        try:
            async with self._client(timeout_s) as client:
                resp = await client.get(url, headers=headers, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientProviderError(
                f"{self.provider_name} transport failure: {type(exc).__name__}",
                provider=self.provider_name,
            ) from exc
        if resp.status_code >= 400:
            raise self._map_error(resp)
        return resp.json()

    async def generate(  # pragma: no cover - overridden per provider
        self, req: LLMRequest, entry: ModelEntry | None = None, **kw
    ) -> LLMResult:
        raise NotImplementedError

    def _result(
        self,
        req: LLMRequest,
        entry: ModelEntry,
        *,
        text: str,
        usage,
        started: float,
    ) -> LLMResult:
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=entry.model_id,
            tier=req.tier,
            usage=usage,
            cost_usd=entry.cost_usd(usage),
            prompt_version=req.prompt_version,
            latency_ms=(time.monotonic() - started) * 1000,
        )
