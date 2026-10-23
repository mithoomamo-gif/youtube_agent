"""Style system: prompt layering, profile registry, draft profiles.

Consistency is a SYSTEM, not a prompt trick (roadmap §21): a versioned
per-channel :class:`~.models.StyleProfile` is applied by the router to
EVERY request. Layering order is fixed:

    positive = scene prompt  ->  style block (locked suffix)
    negative = request negative  ->  profile negative block
"""

from __future__ import annotations

import hashlib
import re

from .models import SamplerSettings, StyleProfile

#: Composition suffixes used to vary the 4 anchor candidates for a draft
#: profile (one-click approval set for Other/custom niches).
ANCHOR_COMPOSITIONS: tuple[str, ...] = (
    "wide establishing shot, balanced composition",
    "medium shot, subject slightly left of frame, negative space on the right",
    "close detail shot, shallow depth of field",
    "high-angle overview shot, strong leading lines",
)

#: Fixed seeds for anchor candidates (deterministic per niche idea).
ANCHOR_SEED_BASE = 1000


def layer_prompts(
    scene_prompt: str,
    request_negative: str,
    profile: StyleProfile | None,
) -> tuple[str, str]:
    """Apply the fixed prompt-layering order.

    Returns ``(positive_prompt, negative_prompt)``. With no profile the
    request passes through unchanged (still a valid layering of one).
    """
    positive = scene_prompt.strip()
    negative_parts = [p.strip() for p in (request_negative or "").split(",") if p.strip()]
    if profile is not None:
        if profile.style_block.strip():
            positive = f"{positive}, {profile.style_block.strip()}"
        for part in (profile.negative_block or "").split(","):
            part = part.strip()
            if part and part not in negative_parts:
                negative_parts.append(part)
    return positive, ", ".join(negative_parts)


class StyleRegistry:
    """In-memory registry of style profiles by ``profile_id``.

    The composition root backs this with SQLite in production; the router
    only depends on :meth:`get`.
    """

    def __init__(self, profiles: dict[str, StyleProfile] | None = None) -> None:
        self._profiles: dict[str, StyleProfile] = dict(profiles or {})

    def register(self, profile: StyleProfile) -> StyleProfile:
        self._profiles[profile.profile_id] = profile
        return profile

    def get(self, profile_id: str | None) -> StyleProfile | None:
        if profile_id is None:
            return None
        return self._profiles.get(profile_id)


#: Keyword -> (palette/medium/lighting) inference for draft profiles.
_NICHE_STYLE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("horror", "scary", "haunted", "ghost"),
     "dark cinematic digital painting, deep teal #0B2B2B and bone #E8E0CC "
     "palette, single candlelight source, heavy shadows, gritty texture, "
     "no people facing camera"),
    (("history", "historical", "ancient", "empire"),
     "aged archival photograph style, sepia-warm palette #8A6D3B and "
     "#D9C7A7, film grain, documentary framing, period-accurate detail"),
    (("islamic", "quran", "prophet stories", "seerah"),
     "warm illuminated manuscript illustration, deep emerald #0E4D3C and "
     "gold #C9A227 palette, intricate geometric borders, soft lantern "
     "light, no depiction of faces"),
    (("cooking", "food", "recipe", "kitchen"),
     "bright overhead food photography, warm rustic palette #B5533C and "
     "cream #F5EBDD, natural window light, steam and texture detail"),
    (("tech", "technology", "gadgets", "ai news"),
     "clean futuristic editorial illustration, slate #1F2937 and electric "
     "blue #38BDF8 palette, soft studio rim light, crisp minimal detail"),
    (("motivation", "success", "self improvement", "mindset"),
     "uplifting cinematic photography, sunrise gold #F2A33C and deep "
     "navy #16283F palette, dramatic sky, solitary silhouette from behind"),
    (("cricket", "sport", "sports"),
     "dynamic sports photography, vivid green #1E7A34 and stadium white "
     "palette, floodlit night match atmosphere, motion energy"),
    (("crime", "mystery", "detective", "true crime"),
     "moody noir illustration, charcoal #232323 and muted crimson #7A1F1F "
     "palette, venetian-blind shadows, evidence-board atmosphere"),
    (("health", "fitness", "wellness", "medical"),
     "calm clinical illustration, soft sage #9DBE9C and clean white "
     "palette, gentle daylight, uncluttered composition"),
    (("space", "astronomy", "universe", "cosmos"),
     "awe-inspiring space art, deep indigo #141433 and starlight silver "
     "#C7CEDA palette, volumetric nebula light, vast scale"),
)

_DEFAULT_STYLE_BLOCK = (
    "cinematic digital illustration, rich teal #14555A and warm amber "
    "#E0A458 palette, soft directional light, detailed texture"
)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "custom"


def make_draft_style_profile(
    niche_idea: str,
    *,
    channel_id: str | None = None,
) -> StyleProfile:
    """Generate a DRAFT style profile from a one-line niche idea.

    For Other/custom niches: palette/medium/lighting are inferred from
    the idea, status is ``draft`` and the channel stays review-first
    until the 4 anchor candidates (see router
    ``generate_anchor_candidates``) are approved with one click.
    """
    idea_lower = niche_idea.lower()
    style_block = _DEFAULT_STYLE_BLOCK
    for keywords, block in _NICHE_STYLE_HINTS:
        if any(k in idea_lower for k in keywords):
            style_block = block
            break
    return StyleProfile(
        profile_id=f"draft-{_slug(niche_idea)}",
        style_block=style_block,
        negative_block=(
            "text, watermark, logo, deformed hands, extra fingers, "
            "modern objects out of period, faces looking at camera"
        ),
        sampler=SamplerSettings(),
        aspect="16:9",
        version=1,
        channel_id=channel_id,
        status="draft",
        niche_idea=niche_idea,
    )


def anchor_seed(niche_idea: str, index: int) -> int:
    """Deterministic per-niche anchor seed for candidate ``index``."""
    digest = hashlib.sha256(niche_idea.strip().lower().encode("utf-8")).hexdigest()
    return ANCHOR_SEED_BASE + (int(digest[:6], 16) % 10000) + index * 137
