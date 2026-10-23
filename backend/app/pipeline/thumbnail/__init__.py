"""Thumbnail pipeline (worker 5): Pillow thumbnail builder.

Public API:
    ChannelStyleProfile, default_profile, BUILTIN_LAYOUTS, get_layout
    ThumbnailBuilder, build_thumbnail
    generate_variants
    check_legibility
    ThumbnailResult, LegibilityResult, VariantSet
    THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT, SAFE_AREA, MAX_HEADLINE_WORDS

Hard rule: thumbnail headline text is rendered with Pillow only, never by
an AI image generator (backgrounds are text-free plates).
"""

from .builder import ThumbnailBuilder, build_thumbnail, fit_cover
from .legibility import check_legibility
from .models import (
    BUILTIN_LAYOUTS,
    MAX_HEADLINE_WORDS,
    SAFE_AREA,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    ChannelStyleProfile,
    LayoutTemplate,
    LegibilityResult,
    ThumbnailResult,
    VariantSet,
    default_profile,
    get_layout,
    hex_to_rgb,
)
from .text_render import contains_arabic_script, shape_text, shaping_available
from .variants import generate_variants

__all__ = [
    "BUILTIN_LAYOUTS",
    "MAX_HEADLINE_WORDS",
    "SAFE_AREA",
    "THUMBNAIL_HEIGHT",
    "THUMBNAIL_WIDTH",
    "ChannelStyleProfile",
    "LayoutTemplate",
    "LegibilityResult",
    "ThumbnailResult",
    "ThumbnailBuilder",
    "VariantSet",
    "build_thumbnail",
    "check_legibility",
    "contains_arabic_script",
    "default_profile",
    "fit_cover",
    "generate_variants",
    "get_layout",
    "hex_to_rgb",
    "shape_text",
    "shaping_available",
]
