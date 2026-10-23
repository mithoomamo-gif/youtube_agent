"""OpenAI adapter (Responses API, no SDK).

Usage normalisation: ``input_tokens`` INCLUDES cached tokens and
``output_tokens`` includes reasoning tokens — both are split into the
exclusive buckets here (the classic double-counting trap).
"""

from __future__ import annotations

import time

from .base import BaseAdapter
from .errors import PolicyRefusal
from .models import LLMRequest, LLMResult, UsageBuckets
from .registry import ModelEntry

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAIAdapter(BaseAdapter):
    provider_name = "openai"

    async def list_models(self, *, timeout_s: float = 10.0) -> list[str]:
        """Cheapest authenticated call: GET /models (test-connection).

        Returns model IDs. Costs no generation quota; errors arrive in
        the shared LLM taxonomy via ``BaseAdapter._get_json``.
        """
        key = await self._api_key()
        data = await self._get_json(
            f"{self.base_url or DEFAULT_BASE}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout_s=timeout_s,
        )
        return [
            str(entry["id"])
            for entry in data.get("data") or []
            if isinstance(entry, dict) and entry.get("id")
        ]

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

        input_items = [
            {"role": m.role, "content": m.content}
            for m in req.normalised_messages()
        ]
        if req.cache_prefix and req.cache_prefix.text:
            input_items.insert(
                0, {"role": "system", "content": req.cache_prefix.text}
            )
        body: dict = {
            "model": entry.model_id,
            "input": input_items,
            "max_output_tokens": req.max_output_tokens,
            "temperature": req.temperature,
        }
        if (
            req.cache_prefix is not None
            and req.cache_prefix.should_cache
        ):
            body["prompt_cache_key"] = req.cache_prefix.id
        if req.output_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": req.output_schema.__name__,
                    "schema": req.output_schema.model_json_schema(),
                    "strict": True,
                }
            }

        data = await self._post_json(
            f"{self.base_url or DEFAULT_BASE}/responses",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            body=body,
            timeout_s=timeout_s,
        )
        return self._parse(req, entry, data, started)

    def _parse(
        self, req: LLMRequest, entry: ModelEntry, data: dict, started: float
    ) -> LLMResult:
        text = data.get("output_text") or ""
        if not text:
            for item in data.get("output") or []:
                for part in item.get("content") or []:
                    if part.get("type") == "refusal":
                        raise PolicyRefusal(
                            f"openai refusal: {part.get('refusal', '')}",
                            provider=self.provider_name,
                        )
                    if part.get("type") == "output_text":
                        text += part.get("text", "")
        if data.get("status") == "incomplete" and (
            (data.get("incomplete_details") or {}).get("reason")
            == "content_filter"
        ):
            raise PolicyRefusal(
                "openai content filter", provider=self.provider_name
            )
        u = data.get("usage") or {}
        in_total = int(u.get("input_tokens", 0))
        cached = int((u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        out_total = int(u.get("output_tokens", 0))
        reasoning = int(
            (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
        )
        usage = UsageBuckets(
            input_uncached=max(0, in_total - cached),  # inclusive -> exclusive
            input_cached=cached,
            output=max(0, out_total - reasoning),
            reasoning=reasoning,
        )
        return self._result(req, entry, text=text, usage=usage, started=started)
