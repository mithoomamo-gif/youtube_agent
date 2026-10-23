"""ComfyUI local adapter — PRIMARY provider for bulk scenes.

Drives a headless local ComfyUI server over its HTTP API with the
versioned workflow template shipped in this package
(``flux-schnell-txt2img-v1``): POST /prompt -> poll GET /history/{id}
-> GET /view for the image bytes.

GPU discipline (roadmap §21): image generation shares a single GPU lock
with faster-whisper + NVENC, so ALL local generation runs under a
semaphore of 1. VRAM tiers select checkpoint precision:

* 8GB  -> GGUF Q4/Q5 + ``--lowvram`` (UnetLoaderGGUF)
* 12GB -> FP8
* 24GB -> FP16 full precision

Only allow-listed local models run here; [dev] / [klein] 9B checkpoints
are blocked by :func:`.allowlist.validate_model` unless the request
carries the per-channel BFL commercial-licence attestation.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass

import httpx

from ..http_client import create_async_client
from . import allowlist
from .base import ImageProvider
from .errors import ProviderError, ProviderUnavailable
from .models import (
    ASPECT_DIMENSIONS,
    ImageRequest,
    ImageResult,
    ProvenanceRecord,
    SamplerSettings,
    prompt_hash,
    utcnow,
)
from .workflows import WORKFLOW_VERSION, build_workflow

PROVIDER_NAME = "comfyui-local"

_SHARED_GPU_SEMAPHORE: asyncio.Semaphore | None = None


def get_gpu_semaphore() -> asyncio.Semaphore:
    """The process-wide single-GPU semaphore (shared with render/TTS)."""
    global _SHARED_GPU_SEMAPHORE
    if _SHARED_GPU_SEMAPHORE is None:
        _SHARED_GPU_SEMAPHORE = asyncio.Semaphore(1)
    return _SHARED_GPU_SEMAPHORE


@dataclass(frozen=True)
class VramTier:
    vram_gb: int
    #: "gguf-q4" | "fp8" | "fp16"
    precision: str
    unet_loader_class: str
    weight_dtype: str
    lowvram: bool
    schnell_file: str


VRAM_TIER_8GB = VramTier(
    vram_gb=8, precision="gguf-q4", unet_loader_class="UnetLoaderGGUF",
    weight_dtype="default", lowvram=True,
    schnell_file="flux1-schnell-Q4_K_S.gguf",
)
VRAM_TIER_12GB = VramTier(
    vram_gb=12, precision="fp8", unet_loader_class="UNETLoader",
    weight_dtype="fp8_e4m3fn", lowvram=False,
    schnell_file="flux1-schnell-fp8.safetensors",
)
VRAM_TIER_24GB = VramTier(
    vram_gb=24, precision="fp16", unet_loader_class="UNETLoader",
    weight_dtype="default", lowvram=False,
    schnell_file="flux1-schnell.safetensors",
)


def tier_for_vram(vram_gb: float) -> VramTier:
    """Pick the VRAM tier for a detected GPU size."""
    if vram_gb < 10:
        return VRAM_TIER_8GB
    if vram_gb < 20:
        return VRAM_TIER_12GB
    return VRAM_TIER_24GB


def _model_file_for(info_canonical: str, tier: VramTier) -> str:
    if info_canonical == allowlist.FLUX_1_SCHNELL:
        return tier.schnell_file
    info = allowlist.MODEL_REGISTRY[info_canonical]
    return info.local_file or tier.schnell_file


class ComfyUIAdapter(ImageProvider):
    """FLUX via a local headless ComfyUI server."""

    name = PROVIDER_NAME

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        vram_gb: float = 12,
        tier: VramTier | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.5,
        max_polls: int = 240,
        gpu_semaphore: asyncio.Semaphore | None = None,
        busy_queue_threshold: int = 8,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tier = tier or tier_for_vram(vram_gb)
        self._owns_client = http_client is None
        self._client = http_client or create_async_client(
            base_url=self.base_url, transport=transport, timeout=120.0
        )
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self._semaphore = gpu_semaphore or get_gpu_semaphore()
        self.busy_queue_threshold = busy_queue_threshold
        self.workflow_version = WORKFLOW_VERSION

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def is_available(self) -> bool:
        try:
            resp = await self._client.get("/system_stats")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def queue_depth(self) -> int | None:
        """Pending + running jobs, or None when the server is unreachable."""
        try:
            resp = await self._client.get("/queue")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return len(data.get("queue_running", [])) + len(
                data.get("queue_pending", [])
            )
        except httpx.HTTPError:
            return None

    async def is_busy(self) -> bool:
        depth = await self.queue_depth()
        return depth is not None and depth > self.busy_queue_threshold

    async def generate(self, req: ImageRequest) -> ImageResult:
        info = allowlist.validate_model(
            req.model_id or allowlist.FLUX_1_SCHNELL,
            holds_bfl_commercial_licence=req.holds_bfl_commercial_licence,
            surface="local",
        )
        if not await self.is_available():
            raise ProviderUnavailable(
                f"ComfyUI server not reachable at {self.base_url}"
            )
        sampler = req.sampler or SamplerSettings()
        seed = req.seed if req.seed is not None else random.randint(0, 2**31 - 1)
        width, height = ASPECT_DIMENSIONS.get(req.aspect, ASPECT_DIMENSIONS["16:9"])
        workflow = build_workflow(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            seed=seed,
            width=width,
            height=height,
            steps=sampler.steps,
            cfg=sampler.cfg,
            guidance=sampler.guidance,
            sampler_name=sampler.sampler_name,
            scheduler=sampler.scheduler,
            unet_loader_class=self.tier.unet_loader_class,
            model_file=_model_file_for(info.canonical_id, self.tier),
            weight_dtype=self.tier.weight_dtype,
        )
        # Single-GPU lock: never run local generation concurrently with
        # the render pipeline / transcription on the same GPU.
        async with self._semaphore:
            image_bytes, filename = await self._run_workflow(workflow)
        provenance = ProvenanceRecord(
            provider=self.name,
            model_id=info.canonical_id,
            prompt_hash=prompt_hash(req.prompt, req.negative_prompt),
            generated_at=utcnow(),
            licence=info.licence,
            provenance="GENERATED",
            seed=seed,
            style_profile_id=req.style_profile_id,
            notes=(
                f"workflow={self.workflow_version}; tier={self.tier.precision}; "
                f"lowvram={self.tier.lowvram}"
            ),
        )
        return ImageResult(
            provider=self.name,
            model_id=info.canonical_id,
            provenance=provenance,
            cost_usd=0.0,  # marginal cost only (electricity amortised ≈ $0)
            image_bytes=image_bytes,
            width=width,
            height=height,
            seed=seed,
            metadata={"filename": filename, "workflow_version": self.workflow_version},
        )

    async def _run_workflow(
        self, workflow: dict
    ) -> tuple[bytes, str]:
        try:
            resp = await self._client.post(
                "/prompt",
                json={"prompt": workflow, "client_id": str(uuid.uuid4())},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ComfyUI /prompt failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"ComfyUI /prompt returned HTTP {resp.status_code}"
            )
        prompt_id = resp.json().get("prompt_id")
        if not prompt_id:
            raise ProviderError("ComfyUI /prompt response missing prompt_id")

        image_ref: dict | None = None
        for _ in range(self.max_polls):
            try:
                hist = await self._client.get(f"/history/{prompt_id}")
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"ComfyUI /history failed: {exc}") from exc
            if hist.status_code == 200:
                entry = hist.json().get(prompt_id) or {}
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ProviderError(
                        f"ComfyUI execution error for prompt {prompt_id}: "
                        f"{status.get('messages')}"
                    )
                for node_out in (entry.get("outputs") or {}).values():
                    images = node_out.get("images") or []
                    if images:
                        image_ref = images[0]
                        break
                if image_ref is not None:
                    break
            if self.poll_interval_s:
                await asyncio.sleep(self.poll_interval_s)
        if image_ref is None:
            raise ProviderError(
                f"ComfyUI prompt {prompt_id} produced no image after "
                f"{self.max_polls} polls"
            )
        try:
            view = await self._client.get(
                "/view",
                params={
                    "filename": image_ref.get("filename", ""),
                    "subfolder": image_ref.get("subfolder", ""),
                    "type": image_ref.get("type", "output"),
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ComfyUI /view failed: {exc}") from exc
        if view.status_code != 200:
            raise ProviderError(f"ComfyUI /view returned HTTP {view.status_code}")
        return view.content, str(image_ref.get("filename", ""))
