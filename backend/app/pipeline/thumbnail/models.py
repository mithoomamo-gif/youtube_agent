"""Data models for the thumbnail builder (worker 5).

Pure stdlib dataclasses -- no pydantic dependency -- so the thumbnail
pipeline can run in the sidecar with only Pillow installed.

Product rules encoded here / in the builder:
* Headline text is ALWAYS rendered programmatically with Pillow.
  AI image generators are used (elsewhere) for text-free backgrounds only.
* A channel palette is 3-5 hex colours.
* Headlines longer than ~5 words are flagged, not silently shipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Canvas / safe-area constants (YouTube thumbnail spec: 1280x720, 16:9)
# ---------------------------------------------------------------------------
THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720

#: Text must live inside this box (x0, y0, x1, y1). Margins keep text off the
#: edges, and the bottom is raised so text never collides with YouTube's
#: duration badge in the bottom-right corner.
SAFE_AREA: tuple[int, int, int, int] = (48, 40, 1232, 668)

#: Region occupied by YouTube's duration badge overlay (bottom-right).
BADGE_ZONE: tuple[int, int, int, int] = (1056, 628, 1280, 720)

#: Headlines above this word count get a warning (roadmap: <=3-4 ideal, 5 max).
MAX_HEADLINE_WORDS = 5

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse '#RRGGBB' (or '#RGB') into an (r, g, b) tuple. Raises ValueError."""
    if not isinstance(value, str) or not _HEX_RE.match(value.strip()):
        raise ValueError(f"not a hex colour: {value!r}")
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


# ---------------------------------------------------------------------------
# Layout templates
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LayoutTemplate:
    """A named thumbnail layout.

    ``text_box`` is the (x0, y0, x1, y1) region the headline is fitted into;
    it is always intersected with :data:`SAFE_AREA` by the builder.
    ``subject_box`` is where an optional foreground/subject cutout goes.
    ``start_font_size`` is the first size the auto-fit tries (it only shrinks).
    """

    id: str
    name: str
    text_box: tuple[int, int, int, int]
    subject_box: tuple[int, int, int, int]
    align: str = "left"  # "left" | "center"
    start_font_size: int = 110
    band: bool = False  # draw a solid palette band behind the text zone
    big_number: bool = False  # leading numeric token rendered oversized


LAYOUT_TEXT_LEFT = LayoutTemplate(
    id="text-left",
    name="Text Left / Subject Right",
    text_box=(48, 120, 700, 620),
    subject_box=(716, 90, 1232, 690),
    align="left",
    start_font_size=112,
)
LAYOUT_TEXT_BOTTOM_BAND = LayoutTemplate(
    id="text-bottom-band",
    name="Text Bottom Band",
    # Band spans the bottom; text stops short of the duration-badge zone.
    text_box=(48, 512, 1040, 668),
    subject_box=(820, 40, 1232, 500),
    align="left",
    start_font_size=88,
    band=True,
)
LAYOUT_BIG_NUMBER = LayoutTemplate(
    id="big-number",
    name="Big Number / Stat",
    text_box=(48, 90, 760, 640),
    subject_box=(780, 90, 1232, 690),
    align="left",
    start_font_size=100,
    big_number=True,
)

#: Built-in layouts, selectable by id.
BUILTIN_LAYOUTS: dict[str, LayoutTemplate] = {
    layout.id: layout
    for layout in (LAYOUT_TEXT_LEFT, LAYOUT_TEXT_BOTTOM_BAND, LAYOUT_BIG_NUMBER)
}


def get_layout(layout_id: str) -> LayoutTemplate:
    try:
        return BUILTIN_LAYOUTS[layout_id]
    except KeyError:
        raise ValueError(
            f"unknown layout id {layout_id!r}; known: {sorted(BUILTIN_LAYOUTS)}"
        ) from None


# ---------------------------------------------------------------------------
# Channel style profile
# ---------------------------------------------------------------------------
@dataclass
class ChannelStyleProfile:
    """Per-channel visual identity applied to every thumbnail.

    ``palette``: 3-5 hex colours (validated). ``accent_color`` defaults to the
    last palette colour. ``font_path_latin`` is used for Latin headlines;
    ``font_path_nastaliq`` (e.g. Noto Nastaliq Urdu) for Urdu/Arabic-script
    headlines when set. Either may be None -> a bundled-system bold font is
    searched as a fallback.
    """

    name: str
    palette: list[str]
    font_path_latin: Optional[str] = None
    font_path_nastaliq: Optional[str] = None
    default_layout_id: str = "text-left"
    accent_color: Optional[str] = None
    text_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    shadow_color: str = "#000000"
    logo_path: Optional[str] = None  # optional watermark/logo PNG (with alpha)
    use_gradient: bool = True
    use_vignette: bool = True

    def __post_init__(self) -> None:
        if not (3 <= len(self.palette) <= 5):
            raise ValueError(
                f"palette must have 3-5 colours, got {len(self.palette)}"
            )
        for colour in self.palette:
            hex_to_rgb(colour)  # validates
        hex_to_rgb(self.text_color)
        hex_to_rgb(self.stroke_color)
        hex_to_rgb(self.shadow_color)
        if self.accent_color is not None:
            hex_to_rgb(self.accent_color)
        get_layout(self.default_layout_id)  # validates

    # -- helpers ------------------------------------------------------------
    def palette_rgb(self) -> list[tuple[int, int, int]]:
        return [hex_to_rgb(c) for c in self.palette]

    @property
    def accent_rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.accent_color or self.palette[-1])

    @property
    def text_rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.text_color)

    @property
    def stroke_rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.stroke_color)

    @property
    def shadow_rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.shadow_color)

    def font_path_for(self, arabic_script: bool) -> Optional[str]:
        if arabic_script and self.font_path_nastaliq:
            return self.font_path_nastaliq
        return self.font_path_latin


def default_profile() -> ChannelStyleProfile:
    """A ready-to-use high-contrast profile (yellow/white on near-black)."""
    return ChannelStyleProfile(
        name="default-bold",
        palette=["#0B0B0F", "#FFD500", "#FF3B30", "#FFFFFF"],
        accent_color="#FFD500",
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class LegibilityResult:
    """Outcome of the mobile-legibility check (thumbnail at 10% size)."""

    passed: bool
    score: float  # 0.0 - 1.0
    checked_size: tuple[int, int] = (128, 72)
    details: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ThumbnailResult:
    """Everything a caller (CTR agent / UI) needs about one built thumbnail."""

    path: str
    width: int
    height: int
    image_format: str  # "PNG" | "JPEG"
    headline: str
    layout_id: str
    style_profile: str
    word_count: int
    font_size: int  # final (possibly auto-shrunk) headline font size, px
    text_bbox: Optional[tuple[int, int, int, int]]
    shaped: bool  # True iff Arabic-script shaping (reshaper + bidi) was applied
    warnings: list[str] = field(default_factory=list)
    legibility: Optional[LegibilityResult] = None
    variant_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class VariantSet:
    """The 3 variants generated for one video."""

    variants: list[ThumbnailResult]

    def __len__(self) -> int:
        return len(self.variants)

    def __iter__(self):
        return iter(self.variants)
