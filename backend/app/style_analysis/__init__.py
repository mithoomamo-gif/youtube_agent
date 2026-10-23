"""Style Lab — competitor video analysis (Style Lab mini-round).

Public surface: the analysis schema (the shared contract), the
markdown breakdown renderer, and the file-based storage seam.
Sub-analysis modules (scenes, visual_classify, speech, dressing,
thumbnail, engine) are owned by other workers and are NOT imported
here, so this package imports cleanly before they land.
"""

from .render import render_breakdown, render_markdown
from .schema import (
    SCHEMA_VERSION,
    VISUAL_CLASSES,
    CTAAnalysis,
    CTAPlacement,
    DressingAnalysis,
    HookAnalysis,
    MotionEstimate,
    PresenceAssessment,
    SceneAnalysis,
    SceneSegment,
    SceneVisualLabel,
    SpeechAnalysis,
    SpeechBeat,
    StyleLabAnalysis,
    ThumbnailAnalysis,
    VideoAnalysisResult,
    VideoMetadata,
    VisualClass,
    VisualMix,
)
from .storage import (
    IndexEntry,
    StoredAnalysis,
    StyleLabIndex,
    StyleAnalysisStore,
    default_stylelab_dir,
    load_analysis,
    save_analysis,
    slugify_key,
)

__all__ = [
    "SCHEMA_VERSION",
    "VISUAL_CLASSES",
    "CTAAnalysis",
    "CTAPlacement",
    "DressingAnalysis",
    "HookAnalysis",
    "IndexEntry",
    "MotionEstimate",
    "PresenceAssessment",
    "SceneAnalysis",
    "SceneSegment",
    "SceneVisualLabel",
    "SpeechAnalysis",
    "SpeechBeat",
    "StoredAnalysis",
    "StyleLabAnalysis",
    "StyleLabIndex",
    "StyleAnalysisStore",
    "ThumbnailAnalysis",
    "VideoAnalysisResult",
    "VideoMetadata",
    "VisualClass",
    "VisualMix",
    "default_stylelab_dir",
    "load_analysis",
    "render_breakdown",
    "render_markdown",
    "save_analysis",
    "slugify_key",
]
