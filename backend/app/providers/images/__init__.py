"""Image generation layer for the YouTube Agent app.

One internal interface (:class:`ImageProvider`), a hard model
allow-list (FLUX.1 [dev] / FLUX.2 [klein] 9B blocked without a BFL
commercial-licence attestation), a ComfyUI local adapter (bulk scenes),
a BFL FLUX API adapter (overflow + premium hero/thumbnail), a versioned
per-channel style system, a prompt safety layer, and a content-hash
cache — all behind the :class:`ImageRouter`.

TEXT RULE: production thumbnail/scene text is rendered with Pillow by
the packaging pipeline; this package has NO text-in-image path for
production use. See :mod:`.base` for the full rationale.
"""

from .allowlist import (
    API_ALLOWED,
    FLUX_1_DEV,
    FLUX_1_SCHNELL,
    FLUX_2_DEV,
    FLUX_2_KLEIN_4B,
    FLUX_2_KLEIN_9B,
    FLUX_PRO,
    LOCAL_ALLOWED,
    MODEL_REGISTRY,
    ModelInfo,
    canonicalize_model_id,
    validate_model,
)
from .base import ImageProvider
from .cache import ImageCache, content_hash
from .comfyui import (
    ComfyUIAdapter,
    VramTier,
    get_gpu_semaphore,
    tier_for_vram,
)
from .errors import (
    DiegeticTextDisabled,
    ImageError,
    LicensingBlocked,
    PromptBlockedError,
    ProviderError,
    ProviderUnavailable,
)
from .flux_api import (
    API_PRICING_USD,
    PRICING_VERIFIED_AT,
    FluxApiAdapter,
)
from .models import (
    ASPECT_DIMENSIONS,
    ImageRequest,
    ImageResult,
    ProvenanceRecord,
    Purpose,
    SamplerSettings,
    SecretProvider,
    StyleProfile,
)
from .router import ImageRouter
from .safety import check_prompt, is_allowed
from .style import StyleRegistry, layer_prompts, make_draft_style_profile

__all__ = [
    "API_ALLOWED",
    "API_PRICING_USD",
    "ASPECT_DIMENSIONS",
    "FLUX_1_DEV",
    "FLUX_1_SCHNELL",
    "FLUX_2_DEV",
    "FLUX_2_KLEIN_4B",
    "FLUX_2_KLEIN_9B",
    "FLUX_PRO",
    "LOCAL_ALLOWED",
    "MODEL_REGISTRY",
    "PRICING_VERIFIED_AT",
    "ComfyUIAdapter",
    "DiegeticTextDisabled",
    "FluxApiAdapter",
    "ImageCache",
    "ImageError",
    "ImageProvider",
    "ImageRequest",
    "ImageResult",
    "ImageRouter",
    "LicensingBlocked",
    "ModelInfo",
    "PromptBlockedError",
    "ProvenanceRecord",
    "ProviderError",
    "ProviderUnavailable",
    "Purpose",
    "SamplerSettings",
    "SecretProvider",
    "StyleProfile",
    "StyleRegistry",
    "VramTier",
    "canonicalize_model_id",
    "check_prompt",
    "content_hash",
    "get_gpu_semaphore",
    "is_allowed",
    "layer_prompts",
    "make_draft_style_profile",
    "tier_for_vram",
    "validate_model",
]
