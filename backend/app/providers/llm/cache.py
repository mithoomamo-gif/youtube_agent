"""Local exact-result cache for deterministic T0 tasks.

Keyed by hash(prompt_version + tier + messages + schema), 24h TTL,
in-memory. NO semantic cache — near-duplicate answers across videos are
exactly what the originality gate exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import time

from .models import LLMRequest, LLMResult

TTL_S = 24 * 3600


class LocalResultCache:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self.ttl_s = ttl_s
        self._store: dict[str, tuple[float, LLMResult]] = {}

    @staticmethod
    def eligible(req: LLMRequest) -> bool:
        return req.cacheable and req.temperature == 0 and req.tier.value == "T0"

    @staticmethod
    def key_for(req: LLMRequest) -> str:
        payload = {
            "tier": req.tier.value,
            "prompt_version": req.prompt_version,
            "messages": [m.model_dump() for m in req.normalised_messages()],
            "schema": req.output_schema.__name__ if req.output_schema else None,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    def get(self, req: LLMRequest) -> LLMResult | None:
        if not self.eligible(req):
            return None
        hit = self._store.get(self.key_for(req))
        if hit is None:
            return None
        expires, result = hit
        if time.monotonic() > expires:
            del self._store[self.key_for(req)]
            return None
        return result.model_copy(update={"status": "cached", "cost_usd": 0.0})

    def put(self, req: LLMRequest, result: LLMResult) -> None:
        if not self.eligible(req):
            return
        self._store[self.key_for(req)] = (time.monotonic() + self.ttl_s, result)
