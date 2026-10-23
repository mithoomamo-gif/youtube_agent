"""Style Lab — thumbnail directives -> thumbnail pipeline profile (W4).

Pure mapping layer between the Style Lab style profile
(:mod:`app.integration.style_profile`, owned by W1) and the thumbnail
pipeline (:mod:`app.pipeline.thumbnail`).  It consumes ONLY the
``thumbnail`` block of :func:`style_directives`::

    {
        "layout_class":   str | None,   # e.g. "face_right_text_left"
        "text_words_min": int | None,
        "text_words_max": int | None,
        "face_expected":  bool,         # majority verdict across analyses
        "palette_class":  str | None,   # e.g. "high_contrast_warm"
    }

NO-COPY RULE
------------
Only PATTERN transfers: a layout class, a text-length band, a face
presence expectation and a palette class.  Competitor thumbnail pixels
and competitor thumbnail text never enter this module — the style
profile itself stores no pixels and no wording (enforced upstream by
``assert_no_copy``), and the headline adjusted here is always OUR OWN
bundle headline, only ever length-adjusted or swapped for another of
our own headline candidates.

Mapping table (directive -> pipeline field)
--------------------------------------------
``layout_class``    -> :attr:`ChannelStyleProfile.default_layout_id`
                       when the class has a real pipeline counterpart
                       (see :func:`map_layout_class`); otherwise the
                       niche profile's layout is kept and the
                       directive is recorded as unmapped.
``palette_class``   -> :attr:`ChannelStyleProfile.palette` +
                       :attr:`ChannelStyleProfile.accent_color`, via
                       this module's OWN preset palettes for the class
                       (a class label is all that transfers — never a
                       competitor colour).  Unknown classes keep the
                       niche palette and are recorded as unmapped.
``face_expected``   -> NO counterpart.  ``ChannelStyleProfile`` has no
                       face field, :func:`generate_variants` places no
                       face/foreground asset, and we must not synthesise
                       or copy a face.  ``True`` is therefore recorded
                       as unmapped; ``False`` is a no-op the pipeline
                       already satisfies (it never places faces), so it
                       is NOT recorded as unmapped.
``text_words_min`` / ``text_words_max``
                    -> NO profile field.  They are consumed at the
                       headline level by :func:`adjust_headlines`
                       (the assemble stage applies the band to our own
                       headline text), so they are carried on the
                       mapping result, never recorded as unmapped.

Everything unmapped is reported, never silently dropped and never
faked by inventing a model field.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence

from app.pipeline.thumbnail.models import BUILTIN_LAYOUTS, ChannelStyleProfile

__all__ = [
    "ELLIPSIS",
    "HeadlineAdjustment",
    "StyleThumbnailMapping",
    "adjust_headline_to_band",
    "adjust_headlines",
    "apply_style_thumbnail",
    "extract_thumbnail_directives",
    "map_layout_class",
    "palette_preset_for",
]

#: Ellipsis convention used when a headline is truncated at a word
#: boundary — the codebase's existing one (``content.py`` truncates
#: over-long chapter titles with a ``"..."`` suffix).  It is glued to
#: the last kept word so it never adds a word of its own.
ELLIPSIS = "..."

_THUMBNAIL_KEYS = (
    "layout_class",
    "text_words_min",
    "text_words_max",
    "face_expected",
    "palette_class",
)


# ---------------------------------------------------------------------------
# Directive extraction (defensive: directives dict / profile / sub-object)
# ---------------------------------------------------------------------------


def _coerce_words(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _coerce_face(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "present", "yes", "expected"):
            return True
        if low in ("false", "absent", "no", "not_expected"):
            return False
    return None


def _normalise_thumbnail(raw: Any) -> Optional[dict[str, Any]]:
    """Normalise any thumbnail-directive shape to the 5-key dict."""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        if not any(key in raw for key in _THUMBNAIL_KEYS):
            return None
        face = _coerce_face(raw.get("face_expected"))
        if face is None and raw.get("face_rate") is not None:
            try:
                face = float(raw["face_rate"]) >= 0.5
            except (TypeError, ValueError):
                face = None
        return {
            "layout_class": (
                str(raw["layout_class"]).strip()
                if raw.get("layout_class") else None
            ),
            "text_words_min": _coerce_words(raw.get("text_words_min")),
            "text_words_max": _coerce_words(raw.get("text_words_max")),
            "face_expected": face,
            "palette_class": (
                str(raw["palette_class"]).strip()
                if raw.get("palette_class") else None
            ),
        }
    # Object shape (e.g. style_profile.ThumbnailStyle): read attributes.
    if any(hasattr(raw, key) for key in _THUMBNAIL_KEYS):
        return _normalise_thumbnail(
            {key: getattr(raw, key, None) for key in _THUMBNAIL_KEYS}
            | {"face_rate": getattr(raw, "face_rate", None)}
        )
    return None


def extract_thumbnail_directives(source: Any) -> Optional[dict[str, Any]]:
    """Pull the thumbnail directive dict out of ``source``.

    Accepts, in order: a full :func:`style_directives` dict (its
    ``"thumbnail"`` block), a bare thumbnail dict, a
    :class:`~app.integration.style_profile.StyleProfile` (its
    ``thumbnail`` sub-object), or a bare ``ThumbnailStyle``-like
    object.  Returns ``None`` when no thumbnail directives exist at
    all (distinct from a present-but-empty block, which normalises to
    a dict of ``None`` values).
    """
    if source is None:
        return None
    if isinstance(source, Mapping):
        if "thumbnail" in source:
            nested = _normalise_thumbnail(source.get("thumbnail"))
            if nested is not None:
                return nested
            # Present but empty/None block: a profile WAS resolved —
            # report the block as all-None directives, not absence.
            return _normalise_thumbnail({key: None for key in _THUMBNAIL_KEYS})
        return _normalise_thumbnail(source)
    thumbnail = getattr(source, "thumbnail", None)
    if thumbnail is not None:
        nested = _normalise_thumbnail(thumbnail)
        if nested is not None:
            return nested
        return _normalise_thumbnail({key: None for key in _THUMBNAIL_KEYS})
    return _normalise_thumbnail(source)


# ---------------------------------------------------------------------------
# Layout mapping
# ---------------------------------------------------------------------------


def map_layout_class(layout_class: Any) -> Optional[str]:
    """Pipeline layout id for a style ``layout_class``, or ``None``.

    Only classes with a real counterpart in
    :data:`BUILTIN_LAYOUTS` map; anything else returns ``None`` (the
    caller keeps the niche layout and records the directive as
    unmapped).  Recognised shapes:

    * a pipeline id itself (``"text-left"`` / ``"text_bottom_band"``…)
    * ``"...text_left..."`` composites (``"face_right_text_left"`` —
      text left, subject right IS the ``text-left`` layout)
    * ``"...bottom..."`` classes -> ``"text-bottom-band"``
    * ``"big_number"`` / ``"number_led"`` -> ``"big-number"``
    * ``"text_heavy"`` / ``"text_only"`` -> ``"text-left"`` (the
      largest headline box; the closest honest counterpart)

    ``face_led`` / ``object_led`` / ``mixed`` / ``minimal`` /
    ``split_screen`` / ``full_bleed`` / ``text_right`` composites /
    ``uncertain`` have no pipeline counterpart and return ``None``.
    """
    if not layout_class or not isinstance(layout_class, str):
        return None
    slug = layout_class.strip().lower().replace("-", "_").replace(" ", "_")
    if not slug or slug == "uncertain":
        return None
    dashed = slug.replace("_", "-")
    if dashed in BUILTIN_LAYOUTS:
        return dashed
    if "bottom" in slug:
        return "text-bottom-band"
    if "big_number" in slug or "number_led" in slug:
        return "big-number"
    if "text_left" in slug:
        return "text-left"
    if slug in ("text_heavy", "text_only", "headline_led"):
        return "text-left"
    return None


# ---------------------------------------------------------------------------
# Palette mapping — OUR preset palettes for a class label (never a
# competitor colour: the profile carries no hex values at all).
# Each preset: (matching tokens, palette[dark, accent-ish, pop, white],
# accent).  First match wins, in the order listed — temperature /
# treatment tokens beat the generic contrast tokens so the W1 example
# "high_contrast_warm" lands on the warm preset.
# ---------------------------------------------------------------------------

_PALETTE_PRESETS: tuple[tuple[tuple[str, ...], list[str], str], ...] = (
    (("neon",),
     ["#0B0B0F", "#FF2BD6", "#00E5FF", "#FFFFFF"], "#00E5FF"),
    (("pastel", "muted", "soft"),
     ["#23232B", "#E8B4B8", "#A8DADC", "#FFFFFF"], "#A8DADC"),
    (("mono", "grayscale", "greyscale", "black_and_white"),
     ["#0B0B0F", "#3A3A44", "#9A9AA5", "#FFFFFF"], "#9A9AA5"),
    (("warm",),
     ["#170D08", "#FF6B35", "#FFC53D", "#FFFFFF"], "#FFC53D"),
    (("cool", "cold"),
     ["#08131F", "#2470C7", "#00C2D1", "#FFFFFF"], "#00C2D1"),
    (("high_contrast", "bold"),
     ["#0B0B0F", "#FFD500", "#FF3B30", "#FFFFFF"], "#FFD500"),
    (("dark",),
     ["#050508", "#23232E", "#FFD500", "#FFFFFF"], "#FFD500"),
    (("vibrant", "vivid", "bright", "saturated"),
     ["#101418", "#FF3B30", "#FFD500", "#FFFFFF"], "#FFD500"),
)


def palette_preset_for(palette_class: Any) -> Optional[tuple[list[str], str]]:
    """``(palette, accent_color)`` preset for a palette class label.

    Returns ``None`` for unknown / empty / ``uncertain`` classes.
    """
    if not palette_class or not isinstance(palette_class, str):
        return None
    slug = palette_class.strip().lower().replace("-", "_").replace(" ", "_")
    if not slug or slug == "uncertain":
        return None
    for tokens, palette, accent in _PALETTE_PRESETS:
        if any(token in slug for token in tokens):
            return list(palette), accent
    return None


# ---------------------------------------------------------------------------
# Profile mapping
# ---------------------------------------------------------------------------


@dataclass
class StyleThumbnailMapping:
    """Result of mapping style thumbnail directives onto a profile."""

    #: The profile to hand to ``generate_variants`` (style-mapped when
    #: any directive mapped, otherwise the base profile itself).
    profile: ChannelStyleProfile
    layout_class: Optional[str] = None
    palette_class: Optional[str] = None
    face_expected: Optional[bool] = None
    text_words_min: Optional[int] = None
    text_words_max: Optional[int] = None
    #: Pipeline layout id actually applied, or None when unmapped/absent.
    layout_applied: Optional[str] = None
    palette_applied: bool = False
    #: Directive keys that were supplied but have no pipeline
    #: counterpart (``"layout_class"`` / ``"palette_class"`` /
    #: ``"face_expected"``), in directive order.
    unmapped: list[str] = field(default_factory=list)


def apply_style_thumbnail(
    base_profile: ChannelStyleProfile,
    thumbnail_directives: Any,
) -> StyleThumbnailMapping:
    """Map style thumbnail directives onto ``base_profile``.

    Style takes precedence for the fields it maps; every other field
    (fonts, text/stroke/shadow colours, logo, gradient/vignette, name)
    is inherited from the niche profile untouched.  ``thumbnail_
    directives`` may be any shape :func:`extract_thumbnail_directives`
    accepts; ``None`` returns the base profile with an empty mapping.
    """
    directives = extract_thumbnail_directives(thumbnail_directives)
    if directives is None:
        return StyleThumbnailMapping(profile=base_profile)

    unmapped: list[str] = []
    changes: dict[str, Any] = {}

    layout_applied: Optional[str] = None
    layout_class = directives["layout_class"]
    if layout_class:
        layout_applied = map_layout_class(layout_class)
        if layout_applied is not None:
            changes["default_layout_id"] = layout_applied
        else:
            unmapped.append("layout_class")

    palette_applied = False
    palette_class = directives["palette_class"]
    if palette_class:
        preset = palette_preset_for(palette_class)
        if preset is not None:
            palette, accent = preset
            changes["palette"] = palette
            changes["accent_color"] = accent
            palette_applied = True
        else:
            unmapped.append("palette_class")

    face_expected = directives["face_expected"]
    if face_expected is True:
        # No face field / face placement exists in the pipeline, and
        # no face may be copied or synthesised: honestly unmapped.
        unmapped.append("face_expected")

    profile = base_profile
    if changes:
        try:
            profile = replace(base_profile, **changes)
        except Exception:
            # A mapped value failed profile validation: keep the niche
            # profile and report the attempted directives as unmapped
            # rather than crashing the stage or shipping a bad profile.
            profile = base_profile
            layout_applied = None
            palette_applied = False
            if "default_layout_id" in changes and "layout_class" not in unmapped:
                unmapped.append("layout_class")
            if "palette" in changes and "palette_class" not in unmapped:
                unmapped.append("palette_class")

    return StyleThumbnailMapping(
        profile=profile,
        layout_class=layout_class,
        palette_class=palette_class,
        face_expected=face_expected,
        text_words_min=directives["text_words_min"],
        text_words_max=directives["text_words_max"],
        layout_applied=layout_applied,
        palette_applied=palette_applied,
        unmapped=unmapped,
    )


# ---------------------------------------------------------------------------
# Headline band adjustment — OUR headline only, never invented text
# ---------------------------------------------------------------------------


@dataclass
class HeadlineAdjustment:
    """What the band check did to one headline."""

    headline: str
    words_before: int
    words_after: int
    #: "no_band" | "within_band" | "truncated" | "fallback_variant" |
    #: "below_min_kept" | "invalid_band" | "kept_unadjustable"
    action: str


def _word_count(text: Any) -> int:
    return len(str(text or "").split())


def _within_band(count: int, low: Optional[int], high: Optional[int]) -> bool:
    if low is not None and count < low:
        return False
    if high is not None and count > high:
        return False
    return True


def adjust_headline_to_band(
    headline: str,
    fallbacks: Sequence[str] = (),
    text_words_min: Optional[int] = None,
    text_words_max: Optional[int] = None,
) -> HeadlineAdjustment:
    """Band-check ONE headline against ``[text_words_min, text_words_max]``.

    Deterministic, and only ever reshapes OUR OWN text:

    * inside the band (or no band) -> untouched;
    * outside -> the shortest existing fallback headline that IS
      inside the band wins (``"fallback_variant"``);
    * too long with no in-band fallback -> truncate at a word
      boundary to ``text_words_max`` words plus the codebase's
      ``"..."`` ellipsis (``"truncated"``);
    * too short with no in-band fallback -> kept verbatim
      (``"below_min_kept"``): padding would mean inventing words.
    """
    text = str(headline or "")
    before = _word_count(text)
    low = _coerce_words(text_words_min)
    high = _coerce_words(text_words_max)
    if low is None and high is None:
        return HeadlineAdjustment(text, before, before, "no_band")
    if low is not None and high is not None and low > high:
        return HeadlineAdjustment(text, before, before, "invalid_band")
    if _within_band(before, low, high):
        return HeadlineAdjustment(text, before, before, "within_band")

    # Shortest existing variant text inside the band (ties: first seen).
    candidates = [
        str(candidate) for candidate in fallbacks
        if candidate and str(candidate).strip() and str(candidate) != text
        and _within_band(_word_count(candidate), low, high)
    ]
    if candidates:
        chosen = min(
            enumerate(candidates),
            key=lambda pair: (_word_count(pair[1]), pair[0]),
        )[1]
        return HeadlineAdjustment(
            chosen, before, _word_count(chosen), "fallback_variant")

    if high is not None and before > high:
        if high == 0:
            return HeadlineAdjustment(text, before, before, "kept_unadjustable")
        truncated = " ".join(text.split()[:high]) + ELLIPSIS
        return HeadlineAdjustment(
            truncated, before, _word_count(truncated), "truncated")

    # Too short and nothing to fall back to: never invent words.
    return HeadlineAdjustment(text, before, before, "below_min_kept")


def adjust_headlines(
    headlines: Sequence[str],
    text_words_min: Optional[int] = None,
    text_words_max: Optional[int] = None,
) -> tuple[list[str], Optional[HeadlineAdjustment]]:
    """Band-check the PRIMARY headline (``headlines[0]``).

    The remaining headlines are the fallback pool.  When a fallback
    wins it is swapped to the front (list length and the other
    candidates are preserved); truncation replaces the first entry in
    place.  Returns ``(new_headlines, adjustment)`` — ``adjustment``
    is ``None`` only for an empty headline list.
    """
    items = [str(h) for h in headlines]
    if not items:
        return [], None
    adjustment = adjust_headline_to_band(
        items[0], items[1:], text_words_min, text_words_max)
    if adjustment.action == "fallback_variant":
        for index in range(1, len(items)):
            if items[index] == adjustment.headline:
                items[0], items[index] = items[index], items[0]
                break
    elif adjustment.headline != items[0]:
        items[0] = adjustment.headline
    return items, adjustment
