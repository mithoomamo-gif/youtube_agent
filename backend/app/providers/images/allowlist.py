"""Model allow-list — the hard licensing defence.

Model IDs are ALLOW-LISTED, never free text from callers. Any string a
caller supplies is canonicalised and looked up here; anything unknown —
and anything non-commercial without an explicit per-channel attestation
— raises :class:`.errors.LicensingBlocked`.

Licence facts (roadmap §21, checked 2026-10-22):

* FLUX.1 [schnell]        — Apache-2.0, commercial-safe. LOCAL default.
* FLUX.2 [klein] 4B       — Apache-2.0, commercial-safe. LOCAL allowed.
* FLUX.1 [dev] (+ the whole [dev] family) — FLUX.1-dev Non-Commercial
  License. BLOCKED locally unless the channel attests it holds a BFL
  commercial licence. Community ComfyUI workflows default to dev
  checkpoints — this allow-list is the defence against that trap.
* FLUX.2 [klein] 9B       — non-commercial. Same block as [dev].
* FLUX.2 [dev]            — non-commercial. Same block.
* FLUX Pro class (API)    — commercial use included via the BFL API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import LicensingBlocked

# --- Canonical model IDs -------------------------------------------------

FLUX_1_SCHNELL = "flux.1-schnell"
FLUX_2_KLEIN_4B = "flux.2-klein-4b"
FLUX_1_DEV = "flux.1-dev"
FLUX_2_KLEIN_9B = "flux.2-klein-9b"
FLUX_2_DEV = "flux.2-dev"
#: "Pro class" served by the BFL API (FLUX.1 Pro / FLUX.2 [pro] tier).
FLUX_PRO = "flux-pro"


@dataclass(frozen=True)
class ModelInfo:
    canonical_id: str
    display_name: str
    licence: str
    #: True when the model may be used commercially with no attestation.
    commercial_safe_by_default: bool
    local_ok: bool
    api_ok: bool
    #: BFL API endpoint segment (API adapter only).
    api_endpoint: str | None = None
    #: Local checkpoint filename for the ComfyUI adapter (per VRAM tier
    #: the precision variant may differ; this is the FP16 reference name).
    local_file: str | None = None


MODEL_REGISTRY: dict[str, ModelInfo] = {
    FLUX_1_SCHNELL: ModelInfo(
        canonical_id=FLUX_1_SCHNELL,
        display_name="FLUX.1 [schnell]",
        licence="Apache-2.0",
        commercial_safe_by_default=True,
        local_ok=True,
        api_ok=True,
        api_endpoint="flux-schnell",
        local_file="flux1-schnell.safetensors",
    ),
    FLUX_2_KLEIN_4B: ModelInfo(
        canonical_id=FLUX_2_KLEIN_4B,
        display_name="FLUX.2 [klein] 4B",
        licence="Apache-2.0",
        commercial_safe_by_default=True,
        local_ok=True,
        api_ok=True,
        api_endpoint="flux-2-klein-4b",
        local_file="flux2-klein-4b.safetensors",
    ),
    FLUX_1_DEV: ModelInfo(
        canonical_id=FLUX_1_DEV,
        display_name="FLUX.1 [dev]",
        licence="FLUX.1-dev Non-Commercial License",
        commercial_safe_by_default=False,
        local_ok=True,
        api_ok=True,
        api_endpoint="flux-dev",
        local_file="flux1-dev.safetensors",
    ),
    FLUX_2_KLEIN_9B: ModelInfo(
        canonical_id=FLUX_2_KLEIN_9B,
        display_name="FLUX.2 [klein] 9B",
        licence="FLUX.2 Non-Commercial License",
        commercial_safe_by_default=False,
        local_ok=True,
        api_ok=False,
        local_file="flux2-klein-9b.safetensors",
    ),
    FLUX_2_DEV: ModelInfo(
        canonical_id=FLUX_2_DEV,
        display_name="FLUX.2 [dev]",
        licence="FLUX.2 Non-Commercial License",
        commercial_safe_by_default=False,
        local_ok=True,
        api_ok=True,
        api_endpoint="flux-2-dev",
        local_file="flux2-dev.safetensors",
    ),
    FLUX_PRO: ModelInfo(
        canonical_id=FLUX_PRO,
        display_name="FLUX Pro (API)",
        licence="BFL API commercial terms",
        commercial_safe_by_default=True,
        local_ok=False,
        api_ok=True,
        api_endpoint="flux-pro-1.1",
    ),
}

#: Models permitted on the local ComfyUI engine without attestation.
LOCAL_ALLOWED: frozenset[str] = frozenset(
    m.canonical_id
    for m in MODEL_REGISTRY.values()
    if m.local_ok and m.commercial_safe_by_default
)

#: Models permitted via the BFL API without attestation.
API_ALLOWED: frozenset[str] = frozenset(
    m.canonical_id
    for m in MODEL_REGISTRY.values()
    if m.api_ok and m.commercial_safe_by_default
)

_ALIASES: dict[str, str] = {
    "schnell": FLUX_1_SCHNELL,
    "flux1schnell": FLUX_1_SCHNELL,
    "flux1-schnell": FLUX_1_SCHNELL,
    "flux.1schnell": FLUX_1_SCHNELL,
    "klein4b": FLUX_2_KLEIN_4B,
    "flux2klein4b": FLUX_2_KLEIN_4B,
    "flux.2klein4b": FLUX_2_KLEIN_4B,
    "dev": FLUX_1_DEV,
    "flux1dev": FLUX_1_DEV,
    "flux1-dev": FLUX_1_DEV,
    "flux.1dev": FLUX_1_DEV,
    "klein9b": FLUX_2_KLEIN_9B,
    "flux2klein9b": FLUX_2_KLEIN_9B,
    "flux.2klein9b": FLUX_2_KLEIN_9B,
    "flux2dev": FLUX_2_DEV,
    "flux.2dev": FLUX_2_DEV,
    "pro": FLUX_PRO,
    "fluxpro": FLUX_PRO,
    "flux1pro": FLUX_PRO,
    "flux2pro": FLUX_PRO,
    "flux-pro-1.1": FLUX_PRO,
    "fluxpro1.1": FLUX_PRO,
}


def canonicalize_model_id(raw: str) -> str | None:
    """Map a caller-supplied model string to its canonical ID.

    Returns ``None`` when the string names no known model — callers must
    treat that as blocked (allow-list, never free text).
    """
    if raw is None:
        return None
    key = re.sub(r"[\s_\[\]()]+", "", str(raw).strip().lower())
    if key in _ALIASES:
        return _ALIASES[key]
    # Also try the lightly-normalised form (dots/dashes preserved).
    light = re.sub(r"[\s_\[\]()]+", "", str(raw).strip().lower().replace("-", ""))
    if light in _ALIASES:
        return _ALIASES[light]
    dashed = str(raw).strip().lower().replace("_", "-")
    if dashed in MODEL_REGISTRY:
        return dashed
    compact = re.sub(r"[^a-z0-9.]+", "", str(raw).strip().lower())
    for alias, canonical in _ALIASES.items():
        if re.sub(r"[^a-z0-9.]+", "", alias) == compact:
            return canonical
    return None


def validate_model(
    model_id: str,
    *,
    holds_bfl_commercial_licence: bool = False,
    surface: str | None = None,
) -> ModelInfo:
    """Validate ``model_id`` against the allow-list.

    Raises :class:`LicensingBlocked` when the model is unknown, not
    available on ``surface`` (``"local"`` / ``"api"``), or non-commercial
    without ``holds_bfl_commercial_licence=True``. Returns the
    :class:`ModelInfo` on success.
    """
    canonical = canonicalize_model_id(model_id)
    if canonical is None:
        raise LicensingBlocked(
            f"Model {model_id!r} is not on the allow-list. Model IDs are "
            "allow-listed, never free text. Allowed without attestation: "
            f"{sorted(LOCAL_ALLOWED | API_ALLOWED)}."
        )
    info = MODEL_REGISTRY[canonical]
    if surface == "local" and not info.local_ok:
        raise LicensingBlocked(
            f"Model {info.display_name} ({canonical}) is not available on "
            "the local ComfyUI engine (API-only model)."
        )
    if surface == "api" and not info.api_ok:
        raise LicensingBlocked(
            f"Model {info.display_name} ({canonical}) is not available "
            "via the BFL API adapter."
        )
    if not info.commercial_safe_by_default and not holds_bfl_commercial_licence:
        raise LicensingBlocked(
            f"Model {info.display_name} ({canonical}) is NON-COMMERCIAL "
            f"({info.licence}) and is BLOCKED. Monetised channels may only "
            "use it with a BFL commercial licence: pass "
            "holds_bfl_commercial_licence=True (per-channel attestation) "
            "if the channel owner holds one. Community ComfyUI workflows "
            "defaulting to this checkpoint are the #1 licensing trap — "
            "use FLUX.1 [schnell] or FLUX.2 [klein] 4B (Apache-2.0) instead."
        )
    return info
