"""Google Gemini adapter (generateContent, no SDK).

Usage normalisation: ``promptTokenCount`` INCLUDES cached tokens, and
``candidatesTokenCount`` includes thought tokens — both are split into the
exclusive buckets here.
"""

from __future__ import annotations

import time

import httpx

from .base import BaseAdapter
from .errors import PolicyRefusal, TransientProviderError
from .models import LLMRequest, LLMResult, UsageBuckets
from .registry import ModelEntry

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiAdapter(BaseAdapter):
    provider_name = "gemini"

    async def list_models(self, *, timeout_s: float = 30.0) -> list[str]:
        """List the models this API key may call (``GET /models``).

        Returns the resource names exactly as the API reports them
        (``"models/<model_id>"``), in API order, following
        ``nextPageToken`` pagination. This is a discovery / key-validation
        call: it consumes no tokens, so it writes no ledger row and has
        no registry cost. The key comes only from the injected
        SecretProvider (via :meth:`BaseAdapter._api_key`) and HTTP errors
        map through the same taxonomy as :meth:`generate`.
        """
        key = await self._api_key()
        base = self.base_url or DEFAULT_BASE
        names: list[str] = []
        page_token: str | None = None
        try:
            async with self._client(timeout_s) as client:
                while True:
                    params = {"pageToken": page_token} if page_token else None
                    resp = await client.get(
                        f"{base}/models",
                        headers={"x-goog-api-key": key},
                        params=params,
                    )
                    if resp.status_code >= 400:
                        raise self._map_error(resp)
                    data = resp.json()
                    for model in data.get("models") or []:
                        if isinstance(model, dict) and model.get("name"):
                            names.append(str(model["name"]))
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        return names
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientProviderError(
                f"{self.provider_name} transport failure: {type(exc).__name__}",
                provider=self.provider_name,
            ) from exc

    async def generate(
        self,
        req: LLMRequest,
        entry: ModelEntry | None = None,
        *,
        timeout_s: float = 60.0,
    ) -> LLMResult:
        assert entry is not None, "router must supply a registry entry"
        started = time.monotonic()
        key = await self._api_key()

        contents = []
        for m in req.normalised_messages():
            if m.role == "system":
                continue  # carried via system_instruction
            contents.append(
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
            )
        body: dict = {"contents": contents}
        system = req.system_text()
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        gen: dict = {
            "maxOutputTokens": req.max_output_tokens,
            "temperature": req.temperature,
        }
        if req.output_schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = req.output_schema.model_json_schema()
        body["generationConfig"] = gen
        if (
            req.cache_prefix is not None
            and req.cache_prefix.should_cache
            and req.cache_prefix.text
        ):
            # Explicit context caching needs a separate cache-create call on
            # the real API; the stable prefix is already sent as the system
            # instruction, so Gemini's implicit caching applies. Flag kept
            # for parity with the router contract.
            body["cachedContent"] = req.cache_prefix.id

        url = (
            f"{self.base_url or DEFAULT_BASE}/models/"
            f"{entry.model_id}:generateContent"
        )
        data = await self._post_json(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            body=body,
            timeout_s=timeout_s,
        )
        return self._parse(req, entry, data, started)

    def _parse(
        self, req: LLMRequest, entry: ModelEntry, data: dict, started: float
    ) -> LLMResult:
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise PolicyRefusal(
                f"gemini blocked prompt: {feedback['blockReason']}",
                provider=self.provider_name,
            )
        candidates = data.get("candidates") or []
        if not candidates:
            raise PolicyRefusal(
                "gemini returned no candidates", provider=self.provider_name
            )
        cand = candidates[0]
        if cand.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
            raise PolicyRefusal(
                f"gemini refusal: {cand['finishReason']}",
                provider=self.provider_name,
            )
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        um = data.get("usageMetadata") or {}
        prompt_tokens = int(um.get("promptTokenCount", 0))
        cached = int(um.get("cachedContentTokenCount", 0))
        candidates_tokens = int(um.get("candidatesTokenCount", 0))
        thoughts = int(um.get("thoughtsTokenCount", 0))
        usage = UsageBuckets(
            input_uncached=max(0, prompt_tokens - cached),
            input_cached=cached,
            output=max(0, candidates_tokens - thoughts),
            reasoning=thoughts,
        )
        return self._result(req, entry, text=text, usage=usage, started=started)
