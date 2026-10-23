"""FLUX API adapter (Black Forest Labs) — overflow + premium tier.

Used when the local ComfyUI engine is busy/absent (overflow, schnell
pricing) and for hero / thumbnail-background work (Pro class), within
the router's per-channel budget. API keys come ONLY from the shared
``SecretProvider`` (``key_ref``) — never from env/files, never logged.

Pricing is data, pinned here with its verification date because vendor
image pricing changes fast (roadmap §21 traps: models now retire on
~2-year cycles — re-verify before locking procurement).
"""

from __future__ import annotations

import asyncio
import re

import httpx

from ..http_client import create_async_client
from . import allowlist
from .base import ImageProvider
from .errors import DiegeticTextDisabled, ProviderError, ProviderUnavailable
from .models import (
    ImageRequest,
    ImageResult,
    ProvenanceRecord,
    SecretProvider,
    prompt_hash,
    utcnow,
)

PROVIDER_NAME = "bfl-flux-api"
DEFAULT_BASE_URL = "https://api.bfl.ai"
DEFAULT_KEY_REF = "bfl/api_key"

#: USD per image, by canonical model ID.
#: Verified against BFL pricing on 2026-10-22 (pricing_verified_at).
API_PRICING_USD: dict[str, float] = {
    allowlist.FLUX_1_SCHNELL: 0.003,
    allowlist.FLUX_PRO: 0.05,
    allowlist.FLUX_1_DEV: 0.025,
    allowlist.FLUX_2_KLEIN_4B: 0.01,
}
PRICING_VERIFIED_AT = "2026-10-22"
DEFAULT_API_COST_USD = 0.05

_LATIN_RE = re.compile(r"^[A-Za-z0-9\s.,'\"!?&:%\-]+$")


class FluxApiAdapter(ImageProvider):
    """FLUX models via the official BFL API (async submit -> poll -> fetch)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        secret_provider: SecretProvider,
        key_ref: str = DEFAULT_KEY_REF,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.5,
        max_polls: int = 120,
        diegetic_text_enabled: bool = False,
    ) -> None:
        self._secrets = secret_provider
        self.key_ref = key_ref
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or create_async_client(
            base_url=self.base_url, transport=transport, timeout=120.0
        )
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        #: Experimental diegetic text path is OFF by default (TEXT RULE).
        self.diegetic_text_enabled = diegetic_text_enabled

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def is_available(self) -> bool:
        try:
            return (await self._secrets.get_secret(self.key_ref)) is not None
        except Exception:
            return False

    async def _api_key(self) -> str:
        key = await self._secrets.get_secret(self.key_ref)
        if not key:
            raise ProviderUnavailable(
                f"BFL API key unavailable (secret ref {self.key_ref!r})"
            )
        return key

    def cost_for(self, canonical_model_id: str) -> float:
        return API_PRICING_USD.get(canonical_model_id, DEFAULT_API_COST_USD)

    async def generate(self, req: ImageRequest) -> ImageResult:
        info = allowlist.validate_model(
            req.model_id or allowlist.FLUX_1_SCHNELL,
            holds_bfl_commercial_licence=req.holds_bfl_commercial_licence,
            surface="api",
        )
        key = await self._api_key()
        payload: dict = {
            "prompt": req.prompt,
            "aspect_ratio": req.aspect,
            # Prompt upsampling OFF: the router's layered prompt (scene +
            # locked style block) must reach the model verbatim.
            "prompt_upsampling": False,
        }
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            payload["seed"] = req.seed
        image_bytes = await self._submit_poll_fetch(
            key, info.api_endpoint or "flux-schnell", payload
        )
        provenance = ProvenanceRecord(
            provider=self.name,
            model_id=info.canonical_id,
            prompt_hash=prompt_hash(req.prompt, req.negative_prompt),
            generated_at=utcnow(),
            licence=info.licence,
            provenance="GENERATED",
            seed=req.seed,
            style_profile_id=req.style_profile_id,
            notes=f"pricing_verified_at={PRICING_VERIFIED_AT}",
        )
        return ImageResult(
            provider=self.name,
            model_id=info.canonical_id,
            provenance=provenance,
            cost_usd=self.cost_for(info.canonical_id),
            image_bytes=image_bytes,
            seed=req.seed,
        )

    async def _submit_poll_fetch(
        self, key: str, endpoint: str, payload: dict
    ) -> bytes:
        headers = {"x-key": key, "Content-Type": "application/json"}
        try:
            resp = await self._client.post(
                f"/v1/{endpoint}", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"BFL API submit failed: {exc}") from exc
        if resp.status_code not in (200, 202):
            raise ProviderError(
                f"BFL API submit returned HTTP {resp.status_code}"
            )
        task_id = resp.json().get("id")
        if not task_id:
            raise ProviderError("BFL API submit response missing task id")

        sample_url: str | None = None
        for _ in range(self.max_polls):
            try:
                poll = await self._client.get(
                    "/v1/get_result", params={"id": task_id}, headers=headers
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"BFL API poll failed: {exc}") from exc
            if poll.status_code != 200:
                raise ProviderError(
                    f"BFL API poll returned HTTP {poll.status_code}"
                )
            data = poll.json()
            status = str(data.get("status", ""))
            if status.lower() == "ready":
                sample_url = (data.get("result") or {}).get("sample")
                break
            if status.lower() in ("error", "failed", "content moderated"):
                raise ProviderError(f"BFL API generation failed: {status}")
            if self.poll_interval_s:
                await asyncio.sleep(self.poll_interval_s)
        if not sample_url:
            raise ProviderError(
                f"BFL API task {task_id} not ready after {self.max_polls} polls"
            )
        # Signed URLs expire (~10 min) — download immediately.
        try:
            fetch = await self._client.get(sample_url)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"BFL sample download failed: {exc}") from exc
        if fetch.status_code != 200:
            raise ProviderError(
                f"BFL sample download returned HTTP {fetch.status_code}"
            )
        return fetch.content

    # ------------------------------------------------------------------
    # EXPERIMENTAL — diegetic text-in-image (text INSIDE a scene)
    # ------------------------------------------------------------------
    async def generate_diegetic_text_image(
        self,
        *,
        scene_prompt: str,
        exact_text: str,
        aspect: str = "16:9",
        seed: int | None = None,
        ocr_check=None,
        max_rerolls: int = 2,
    ) -> ImageResult:
        """EXPERIMENTAL: generate a scene containing ``exact_text`` as
        diegetic text (a shop sign, book cover, poster inside the scene).

        This is NOT the production text path — production text is
        rendered with Pillow by the packaging pipeline (see the TEXT RULE
        in :mod:`.base`). Rules enforced here, per roadmap §21:

        * default OFF — raises :class:`DiegeticTextDisabled` unless the
          adapter was built with ``diegetic_text_enabled=True``;
        * the exact string is quoted verbatim in the prompt and prompt
          upsampling is OFF (upsampling paraphrases and breaks strings);
        * Latin script only — non-Latin (e.g. Urdu/Arabic) diegetic text
          must be composited with Pillow, never generated;
        * max ``max_rerolls`` (default 2) rerolls, then a text-free
          fallback image is returned (flagged in metadata/provenance);
        * the OCR GATE IS THE CALLER'S JOB: pass ``ocr_check`` (a callable
          ``bytes -> str``); when supplied, a result whose OCR text does
          not match ``exact_text`` is rejected and rerolled.
        """
        if not self.diegetic_text_enabled:
            raise DiegeticTextDisabled(
                "Diegetic text-in-image generation is disabled by default. "
                "Production text is rendered with Pillow by the packaging "
                "pipeline; enable diegetic_text_enabled=True on the API "
                "adapter only for the in-scene-text exception path."
            )
        if not _LATIN_RE.match(exact_text or ""):
            raise ValueError(
                "Diegetic text generation supports Latin script only. "
                "Non-Latin text (e.g. Urdu Nastaliq) must be composited "
                "with Pillow."
            )
        if max_rerolls < 0 or max_rerolls > 2:
            raise ValueError("max_rerolls must be between 0 and 2")

        quoted_prompt = (
            f'{scene_prompt}, with a sign that reads exactly "{exact_text}"'
        )

        async def _gen(prompt_text: str) -> ImageResult:
            req = ImageRequest(
                prompt=prompt_text, aspect=aspect, seed=seed,
                model_id=allowlist.FLUX_PRO,
            )
            return await self.generate(req)

        attempts = 0
        while True:
            result = await _gen(quoted_prompt)
            attempts += 1
            if ocr_check is None:
                result.metadata["diegetic_text"] = exact_text
                result.metadata["ocr_gate"] = "caller-managed (no ocr_check supplied)"
                result.metadata["attempts"] = attempts
                return result
            ocr_text = ocr_check(result.image_bytes or b"")
            if ocr_text is not None and exact_text in str(ocr_text):
                result.metadata["diegetic_text"] = exact_text
                result.metadata["ocr_gate"] = "passed"
                result.metadata["attempts"] = attempts
                return result
            if attempts > max_rerolls:
                break
        # Reroll budget exhausted -> text-free fallback scene.
        fallback = await _gen(scene_prompt)
        fallback.metadata["diegetic_text"] = None
        fallback.metadata["diegetic_fallback"] = "text-free"
        fallback.metadata["attempts"] = attempts
        fallback.provenance.notes += "; diegetic OCR gate failed, text-free fallback"
        return fallback
