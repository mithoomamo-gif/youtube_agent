"""Anthropic adapter (Messages API, no SDK).

Usage normalisation: ``input_tokens`` EXCLUDES cache reads/writes already,
so buckets map directly (cache reads arrive as their own field). Structured
output is forced tool-use; the tool input is serialised to JSON text and
the router performs the authoritative Pydantic validation.
"""

from __future__ import annotations

import json
import time

from .base import BaseAdapter
from .errors import PolicyRefusal
from .models import LLMRequest, LLMResult, UsageBuckets
from .registry import ModelEntry

DEFAULT_BASE = "https://api.anthropic.com/v1"
TOOL_NAME = "structured_output"


class AnthropicAdapter(BaseAdapter):
    provider_name = "anthropic"

    async def list_models(self, *, timeout_s: float = 10.0) -> list[str]:
        """Cheapest authenticated call: GET /models (test-connection).

        Returns model IDs. Costs no generation quota; errors arrive in
        the shared LLM taxonomy via ``BaseAdapter._get_json``.
        """
        key = await self._api_key()
        data = await self._get_json(
            f"{self.base_url or DEFAULT_BASE}/models",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
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

        messages = [
            {"role": m.role, "content": m.content}
            for m in req.normalised_messages()
            if m.role in ("user", "assistant")
        ] or [{"role": "user", "content": req.prompt or ""}]
        body: dict = {
            "model": entry.model_id,
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
            "messages": messages,
        }
        system = req.system_text()
        if system:
            if (
                req.cache_prefix is not None
                and req.cache_prefix.should_cache
            ):
                body["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                body["system"] = system
        if req.output_schema is not None:
            body["tools"] = [
                {
                    "name": TOOL_NAME,
                    "description": "Return the answer in this exact shape.",
                    "input_schema": req.output_schema.model_json_schema(),
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": TOOL_NAME}

        data = await self._post_json(
            f"{self.base_url or DEFAULT_BASE}/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body=body,
            timeout_s=timeout_s,
        )
        return self._parse(req, entry, data, started)

    def _parse(
        self, req: LLMRequest, entry: ModelEntry, data: dict, started: float
    ) -> LLMResult:
        if data.get("stop_reason") == "refusal":
            raise PolicyRefusal(
                "anthropic refusal", provider=self.provider_name
            )
        text = ""
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                text = json.dumps(block.get("input") or {})
        u = data.get("usage") or {}
        reasoning = int(u.get("thinking_tokens", 0) or 0)
        usage = UsageBuckets(
            input_uncached=int(u.get("input_tokens", 0)),  # already exclusive
            input_cached=int(u.get("cache_read_input_tokens", 0)),
            input_cache_write=int(u.get("cache_creation_input_tokens", 0)),
            # output_tokens includes thinking when reported separately
            output=max(0, int(u.get("output_tokens", 0)) - reasoning),
            reasoning=reasoning,
        )
        return self._result(req, entry, text=text, usage=usage, started=started)
