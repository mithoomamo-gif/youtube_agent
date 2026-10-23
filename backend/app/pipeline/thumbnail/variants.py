"""3-variant generation.

Given background candidates, headline options and a style profile, produce
exactly 3 thumbnail variants with deterministic file naming:

    v1 -> profile's default layout, background[0], headline[0]
    v2 -> "text-bottom-band",      background[1] (or last), headline[1] (or last)
    v3 -> "big-number",            background[2] (or last), headline[2] (or [0])

Files: ``<base_name>_v1_<layout>.png``, ``..._v2_...``, ``..._v3_...`` in the
output directory. Every variant carries its own legibility result and
metadata (variant id, layout, headline) via :class:`ThumbnailResult`.
"""

from __future__ import annotations

import os
from typing import Union

from .builder import ImageLike, ThumbnailBuilder
from .models import ChannelStyleProfile, VariantSet

#: (variant_id, layout_id or None-for-profile-default, bg_idx, headline_idx)
_VARIANT_PLAN = (
    ("v1", None, 0, 0),
    ("v2", "text-bottom-band", 1, 1),
    ("v3", "big-number", 2, 0),
)


def _pick(items: list, index: int):
    return items[min(index, len(items) - 1)]


def generate_variants(
    backgrounds: list[ImageLike],
    headlines: list[str],
    profile: ChannelStyleProfile,
    output_dir: Union[str, os.PathLike],
    base_name: str = "thumbnail",
    image_format: str = "PNG",
) -> VariantSet:
    """Build exactly 3 variants. Raises ValueError on empty inputs."""
    if not backgrounds:
        raise ValueError("at least one background candidate is required")
    if not headlines or not any(h.strip() for h in headlines):
        raise ValueError("at least one non-empty headline option is required")

    ext = ".jpg" if image_format.upper() in ("JPEG", "JPG") else ".png"
    os.makedirs(str(output_dir), exist_ok=True)
    builder = ThumbnailBuilder(profile)

    variants = []
    for variant_id, layout_id, bg_idx, head_idx in _VARIANT_PLAN:
        effective_layout = layout_id or profile.default_layout_id
        filename = f"{base_name}_{variant_id}_{effective_layout}{ext}"
        result = builder.build(
            _pick(list(backgrounds), bg_idx),
            _pick(list(headlines), head_idx),
            os.path.join(str(output_dir), filename),
            layout_id=layout_id,
            variant_id=variant_id,
        )
        result.metadata.update(
            {
                "variant_id": variant_id,
                "layout": result.layout_id,
                "headline": result.headline,
                "background_index": min(bg_idx, len(backgrounds) - 1),
                "headline_index": min(head_idx, len(headlines) - 1),
            }
        )
        variants.append(result)
    return VariantSet(variants=variants)
