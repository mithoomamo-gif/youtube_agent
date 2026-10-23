"""Core data model for the image-generation layer.

Every generation returns the image PLUS a licence/provenance record
(:class:`ProvenanceRecord`, marked ``GENERATED``) — the stock worker's
ledger consumes these records, so no result may ever ship without one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SecretProvider(Protocol):
    """Shared secret-provider contract.

    Cloud adapters receive a ``SecretProvider`` plus a ``key_ref`` string;
    they NEVER read environment variables or files for keys and NEVER log
    key material.
    """

    async def get_secret(self, ref: str) -> str | None: ...


class Purpose(str, Enum):
    """What the image is for. Drives routing (premium tier is reserved
    for HERO / THUMBNAIL_BACKGROUND) and QC."""

    SCENE = "scene"
    THUMBNAIL_BACKGROUND = "thumbnail_background"
    HERO = "hero"


#: Native output sizes per aspect. 16:9 is the product default and is
#: generated natively — never crop-stretched from a square.
ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "1:1": (1024, 1024),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
}

DEFAULT_ASPECT = "16:9"


@dataclass(frozen=True)
class SamplerSettings:
    """Sampler lock for a style profile. Changing any field is a style
    profile version bump, applied channel-wide — never per video.

    Defaults are the FLUX.1 [schnell] bulk lock: Euler / simple,
    4 steps, CFG 1.0 (distilled model), FluxGuidance 3.5.
    """

    steps: int = 4
    cfg: float = 1.0
    sampler_name: str = "euler"
    scheduler: str = "simple"
    guidance: float = 3.5


@dataclass
class StyleProfile:
    """Versioned per-channel style profile.

    The router applies the profile to EVERY request (callers cannot
    forget it): the effective prompt is layered as
    ``scene prompt -> style block`` and the effective negative prompt
    always includes ``negative_block``.
    """

    profile_id: str
    style_block: str
    negative_block: str = "text, watermark, logo, deformed hands, extra fingers"
    sampler: SamplerSettings = field(default_factory=SamplerSettings)
    aspect: str = DEFAULT_ASPECT
    version: int = 1
    channel_id: str | None = None
    #: "active" | "draft" — drafts are generated for Other/custom niches
    #: and stay review-first until their anchor set is approved.
    status: str = "active"
    niche_idea: str | None = None
    anchor_image_ids: tuple[str, ...] = ()

    def bumped(self, **changes: Any) -> "StyleProfile":
        """Return a copy with ``version + 1`` (any sampler/style change is
        a version bump)."""
        data = {
            "profile_id": self.profile_id,
            "style_block": self.style_block,
            "negative_block": self.negative_block,
            "sampler": self.sampler,
            "aspect": self.aspect,
            "version": self.version + 1,
            "channel_id": self.channel_id,
            "status": self.status,
            "niche_idea": self.niche_idea,
            "anchor_image_ids": self.anchor_image_ids,
        }
        data.update(changes)
        return StyleProfile(**data)


@dataclass
class ImageRequest:
    """A single generation request.

    ``model_id`` is NEVER free text in spirit: whatever string arrives is
    canonicalised and checked against the hard allow-list
    (see :mod:`.allowlist`); unknown or non-commercial models raise
    :class:`.errors.LicensingBlocked`.
    """

    prompt: str
    negative_prompt: str = ""
    aspect: str = DEFAULT_ASPECT
    style_profile_id: str | None = None
    seed: int | None = None
    purpose: Purpose = Purpose.SCENE
    model_id: str | None = None
    channel_id: str | None = None
    #: Per-channel attestation: the channel owner holds a BFL commercial
    #: licence. Required to unblock FLUX.1 [dev] / FLUX.2 [klein] 9B.
    holds_bfl_commercial_licence: bool = False
    #: Inline profile (used for DRAFT profiles of Other/custom niches
    #: before they are persisted). Takes precedence over the registry
    #: lookup by ``style_profile_id``.
    style_profile: StyleProfile | None = None
    #: Filled in by the router from the resolved style profile; adapters
    #: may read it but callers should not set it.
    sampler: SamplerSettings | None = None


@dataclass
class ProvenanceRecord:
    """Licence/provenance record attached to every generated image.

    Consumed by the stock worker's ledger / disclosure flow. ``provenance``
    is always ``GENERATED`` for this layer. ``synthid`` notes providers
    that embed an invisible SynthID-style watermark.
    """

    provider: str
    model_id: str
    prompt_hash: str
    generated_at: datetime
    licence: str
    provenance: str = "GENERATED"
    seed: int | None = None
    style_profile_id: str | None = None
    style_version: int | None = None
    synthid: bool = False
    cache_hit: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "generated_at": self.generated_at.isoformat(),
            "licence": self.licence,
            "provenance": self.provenance,
            "seed": self.seed,
            "style_profile_id": self.style_profile_id,
            "style_version": self.style_version,
            "synthid": self.synthid,
            "cache_hit": self.cache_hit,
            "notes": self.notes,
        }


def prompt_hash(prompt: str, negative_prompt: str = "") -> str:
    """Stable SHA-256 over the effective prompt pair."""
    payload = json.dumps(
        {"prompt": prompt, "negative_prompt": negative_prompt},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ImageResult:
    """Result of one generation: image payload + provenance + cost."""

    provider: str
    model_id: str
    provenance: ProvenanceRecord
    cost_usd: float = 0.0
    image_bytes: bytes | None = None
    image_path: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
