"""Image router — the one entry point pipeline stages call.

Routing policy (roadmap §21):

1. Prompt safety check — blocked prompts raise before ANY provider call.
2. Allow-list validation of the requested model (LicensingBlocked).
3. Style profile applied to EVERY request (fixed layering order:
   scene prompt -> style block; negatives merged).
4. Content-hash cache — identical requests return the cached image.
5. Local ComfyUI first for bulk scenes; overflow to the FLUX API
   (schnell) when local is busy/absent; premium Pro class ONLY for
   hero / thumbnail_background purposes and ONLY within the per-channel
   premium budget.

No pipeline stage ever calls a vendor adapter directly.
"""

from __future__ import annotations

import dataclasses

from . import allowlist, safety
from .allowlist import validate_model
from .base import ImageProvider
from .cache import ImageCache, content_hash
from .errors import PromptBlockedError, ProviderUnavailable
from .models import ImageRequest, ImageResult, Purpose
from .style import (
    ANCHOR_COMPOSITIONS,
    StyleRegistry,
    anchor_seed,
    layer_prompts,
    make_draft_style_profile,
)

#: Premium purposes eligible for the Pro tier.
PREMIUM_PURPOSES = frozenset({Purpose.HERO, Purpose.THUMBNAIL_BACKGROUND})


class ImageRouter(ImageProvider):
    """Routes generation across the local and API adapters."""

    name = "image-router"

    def __init__(
        self,
        *,
        local: ImageProvider | None = None,
        api: ImageProvider | None = None,
        styles: StyleRegistry | dict | None = None,
        cache: ImageCache | None = None,
        premium_budget_usd_per_channel: float | None = 1.0,
        prefer_premium_for_hero: bool = True,
        default_local_model: str = allowlist.FLUX_1_SCHNELL,
        overflow_model: str = allowlist.FLUX_1_SCHNELL,
        premium_model: str = allowlist.FLUX_PRO,
    ) -> None:
        self.local = local
        self.api = api
        if styles is None:
            self.styles = StyleRegistry()
        elif isinstance(styles, StyleRegistry):
            self.styles = styles
        else:
            self.styles = StyleRegistry(styles)
        self.cache = cache if cache is not None else ImageCache()
        self.premium_budget_usd_per_channel = premium_budget_usd_per_channel
        self.prefer_premium_for_hero = prefer_premium_for_hero
        self.default_local_model = default_local_model
        self.overflow_model = overflow_model
        self.premium_model = premium_model
        #: Cumulative premium (Pro) spend per channel_id.
        self._premium_spend: dict[str, float] = {}

    # -- budget ---------------------------------------------------------

    def premium_spend(self, channel_id: str | None) -> float:
        return self._premium_spend.get(channel_id or "", 0.0)

    def _premium_affordable(self, channel_id: str | None, cost: float) -> bool:
        if self.premium_budget_usd_per_channel is None:
            return True
        return self.premium_spend(channel_id) + cost <= (
            self.premium_budget_usd_per_channel + 1e-9
        )

    def _api_cost(self, model_id: str) -> float:
        cost_fn = getattr(self.api, "cost_for", None)
        if callable(cost_fn):
            return float(cost_fn(model_id))
        from .flux_api import API_PRICING_USD, DEFAULT_API_COST_USD

        return API_PRICING_USD.get(model_id, DEFAULT_API_COST_USD)

    # -- main entry -----------------------------------------------------

    async def generate(self, req: ImageRequest) -> ImageResult:
        # 1) Safety BEFORE any provider call (and before cache, so a
        #    blocked prompt can never be served from cache either).
        violations = safety.check_prompt(req.prompt)
        if violations:
            raise PromptBlockedError(
                "Prompt blocked by safety layer: " + "; ".join(violations),
                violations=violations,
            )

        # 2) Resolve style profile; inline (draft) profile wins.
        profile = req.style_profile or self.styles.get(req.style_profile_id)
        positive, negative = layer_prompts(req.prompt, req.negative_prompt, profile)

        # 3) Decide the model + validate against the allow-list.
        explicit_model = req.model_id is not None
        if explicit_model:
            info = validate_model(
                req.model_id,  # type: ignore[arg-type]
                holds_bfl_commercial_licence=req.holds_bfl_commercial_licence,
            )
            target_model = info.canonical_id
        else:
            target_model = self.default_local_model
            validate_model(
                target_model,
                holds_bfl_commercial_licence=req.holds_bfl_commercial_licence,
            )

        aspect = req.aspect or (profile.aspect if profile else req.aspect)

        # 4) Content-hash cache.
        cache_key = content_hash(
            prompt=positive,
            negative_prompt=negative,
            style_profile_id=profile.profile_id if profile else req.style_profile_id,
            style_version=profile.version if profile else None,
            seed=req.seed,
            model_id=target_model,
            aspect=aspect,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            prov = dataclasses.replace(cached.provenance, cache_hit=True)
            return dataclasses.replace(cached, cost_usd=0.0, provenance=prov)

        effective = dataclasses.replace(
            req,
            prompt=positive,
            negative_prompt=negative,
            aspect=aspect,
            model_id=target_model,
            sampler=req.sampler or (profile.sampler if profile else None),
            style_profile_id=(
                profile.profile_id if profile else req.style_profile_id
            ),
        )

        result = await self._dispatch(effective, explicit_model=explicit_model)
        # Enrich provenance with style version (adapters know the id,
        # the router owns the version).
        if profile is not None and result.provenance.style_version is None:
            result.provenance.style_version = profile.version
        self.cache.put(cache_key, result)
        return result

    async def _dispatch(
        self, req: ImageRequest, *, explicit_model: bool
    ) -> ImageResult:
        model = req.model_id or self.default_local_model
        info = allowlist.MODEL_REGISTRY[model]
        purpose = req.purpose

        # Premium path: hero / thumbnail purposes go to Pro via the API
        # when enabled and within the per-channel budget. An explicit
        # Pro request also lands here.
        wants_premium = model == self.premium_model or (
            not explicit_model
            and self.prefer_premium_for_hero
            and purpose in PREMIUM_PURPOSES
        )
        if wants_premium and self.api is not None and info.api_ok:
            pro_cost = self._api_cost(self.premium_model)
            if self._premium_affordable(req.channel_id, pro_cost):
                result = await self.api.generate(
                    dataclasses.replace(req, model_id=self.premium_model)
                )
                self._premium_spend[req.channel_id or ""] = (
                    self.premium_spend(req.channel_id) + result.cost_usd
                )
                return result
            # Budget exhausted: fall through to the standard path.

        errors: list[str] = []

        # Standard path: local first (when the model can run locally).
        if self.local is not None and info.local_ok:
            try:
                available = await self.local.is_available()
                busy = False
                if available:
                    is_busy = getattr(self.local, "is_busy", None)
                    if callable(is_busy):
                        busy = await is_busy()
                if available and not busy:
                    return await self.local.generate(req)
                errors.append(
                    "local provider busy" if available else "local provider unavailable"
                )
            except ProviderUnavailable as exc:
                errors.append(f"local provider unavailable: {exc}")

        # Overflow: FLUX API at the overflow (schnell) tier — or at the
        # explicitly requested model when it is API-served.
        if self.api is not None and info.api_ok:
            api_model = model if explicit_model else self.overflow_model
            validate_model(
                api_model,
                holds_bfl_commercial_licence=req.holds_bfl_commercial_licence,
                surface="api",
            )
            return await self.api.generate(
                dataclasses.replace(req, model_id=api_model)
            )

        raise ProviderUnavailable(
            "No image provider could serve the request"
            + (f" ({'; '.join(errors)})" if errors else "")
        )

    # -- Other/custom niches ---------------------------------------------

    async def generate_anchor_candidates(
        self,
        niche_idea: str,
        *,
        channel_id: str | None = None,
        base_prompt: str | None = None,
    ) -> list[ImageResult]:
        """Generate the 4 anchor candidates for a DRAFT style profile.

        The Niche Profiling flow calls this for Other/custom niches: a
        draft profile is inferred from ``niche_idea`` and 4 candidate
        anchor images are generated (varied compositions, deterministic
        seeds) for one-click approval at the review gate. Until anchors
        are approved the channel stays review-first for images.
        """
        profile = make_draft_style_profile(niche_idea, channel_id=channel_id)
        scene = base_prompt or (
            f"a signature scene for a YouTube channel about {niche_idea}"
        )
        results: list[ImageResult] = []
        for idx, composition in enumerate(ANCHOR_COMPOSITIONS):
            req = ImageRequest(
                prompt=f"{scene}, {composition}",
                aspect=profile.aspect,
                style_profile_id=profile.profile_id,
                style_profile=profile,
                seed=anchor_seed(niche_idea, idx),
                purpose=Purpose.HERO,
                channel_id=channel_id,
            )
            results.append(await self.generate(req))
        return results
