"""Core models for the ROUND 2 end-to-end integration pipeline.

This module is the SHARED CONTRACT every integration stage module is
built against (Worker 1 — core models + pack I/O):

* :class:`ProjectSpec` — what the user asked for: one video, one topic,
  one niche (a built-in profile id *or* ``"other"`` plus the user's own
  niche name / idea), a target length, language, voice and channel.
* :class:`ProductionResult` — what the pipeline produced: the frozen
  facts a caller needs to render a report, publish, or audit the run
  (final MP4, thumbnails, captions, licence ledger, publish manifest,
  gate / QC verdicts, cost and timing). Paths are plain ``str`` so the
  result serialises to JSON with no conversion step.
* :class:`StageContext` — the per-project working context handed from
  stage to stage. Stages never guess paths: every directory a stage may
  write to is a property here, and :func:`make_context` is the one place
  that creates the on-disk layout::

      <base_dir>/<project_id>/
          content/    research, outline, script and SEO text artefacts
          audio/      voiceover, music and mix artefacts
          visuals/    scene plates / stock / generated stills
          render/     intermediate renders and the final master
          pack/       the deliverable pack (report, ledger, manifest, ...)

Only stdlib + Pydantic are used here; there is no network access.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import (BaseModel, ConfigDict, Field, field_validator,
                      model_validator)

__all__ = [
    "ProductionResult",
    "ProjectSpec",
    "StageContext",
    "make_context",
]


class ProjectSpec(BaseModel):
    """One video to produce: the user's request, frozen up front.

    ``niche`` is a built-in profile id (``"facts"``, ``"history"``, ...)
    OR the literal ``"other"`` — in which case ``custom_niche_name`` /
    ``niche_idea`` carry the user's own niche and a :class:`NicheProfile`
    is auto-built for it before the pipeline runs. The resolved profile
    itself is *not* part of the spec; it travels on
    :class:`StageContext.profile`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: Built-in niche profile id OR "other".
    niche: str = "facts"
    #: User's own niche label when ``niche == "other"``.
    custom_niche_name: str | None = None
    #: Free-text niche idea when ``niche == "other"``.
    niche_idea: str | None = None
    #: The video topic (the one required field).
    topic: str = Field(..., min_length=1)
    #: Target runtime in minutes.
    minutes: float = Field(default=1.0, gt=0)
    #: Narration language code, e.g. "ur" (default) / "en".
    language: str = "ur"
    #: Voice id for TTS; ``None`` lets the voice stage pick the default.
    voice_id: str | None = None
    #: Script the narration is handed to TTS in (Round 4): ``"roman"``
    #: or ``"urdu_script"`` — Roman Urdu narration is transliterated
    #: to Urdu script (with the pronunciation dictionary applied)
    #: before synthesis for ur-PK voices.  Per-channel selectable.
    #: DEFAULT (R4-002, the adopted A/B winner): ``"urdu_script"``
    #: for ur-PK voices / Urdu productions, ``"roman"`` for non-Urdu
    #: voices — resolved voice-aware by
    #: :meth:`_resolve_tts_input_script_default` when the caller did
    #: not set the field explicitly; an explicit value (either way)
    #: is always honoured.  The literal class default below is only
    #: the pre-resolution placeholder.
    tts_input_script: str = "roman"
    #: Explicit user TTS rate override in percent (e.g. ``-10.0``);
    #: ``None`` (the default) means NO override — the niche preset rate
    #: is used and the script-length planner plans with the voice's
    #: calibrated WPM at that rate, so durations land on target without
    #: any post-hoc rate compensation (Round 4).  When set, the value
    #: is honoured by both the planner and the voice stage and is
    #: logged as an explicit override.
    tts_rate_percent: float | None = None
    #: Channel the video belongs to (local test channel by default).
    channel_id: str = "local-test-channel"
    #: Output quality tier for this video (Round 5 — master encode):
    #: ``"draft"`` (the default; the historical 720p behaviour),
    #: ``"master_1080p"`` or ``"master_1440p_upscale"`` — exactly the
    #: render path's ``quality_tier`` vocabulary
    #: (``app.pipeline.render.renderer.QUALITY_TIERS``).  This is a
    #: PER-CHANNEL setting: it is stored on the channel
    #: (``channels.quality_tier``, see :mod:`app.channel_quality`) and
    #: resolved onto the spec at the spec-construction seams
    #: (``app.integration.stage_bridge.project_to_spec`` for both the
    #: manual/DAG path and scheduled automation, which materialises
    #: projects carrying the channel id).  An explicitly set spec
    #: value wins; the assemble stage receives it as its
    #: ``quality_tier`` argument (and reads ``ctx.spec.quality_tier``
    #: as its documented fallback seam).
    quality_tier: str = "draft"

    @field_validator("quality_tier")
    @classmethod
    def _quality_tier_is_known(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in ("draft", "master_1080p", "master_1440p_upscale"):
            raise ValueError(
                "quality_tier must be 'draft', 'master_1080p' or "
                f"'master_1440p_upscale', got {value!r}")
        return cleaned
    #: Channel's active Style Lab style profile JSON (``style_profile.v1``):
    #: path to the profile file for this channel, resolved via
    #: ``app.integration.style_profile.resolve_style_profile`` (this path
    #: wins, then ``<base_dir>/style_profiles/<channel_id>.json``, then
    #: env ``YTA_STYLE_PROFILE_PATH``).  The profile carries PATTERNS
    #: only (structure / pacing / title & description formulas) — never
    #: competitor text or footage (Style Lab no-copy rule).  ``None``
    #: (the default) means no style steering: production behaves
    #: exactly as before Style Lab.
    style_profile_path: str | None = None
    #: "review_first" (human approves before publish) or "auto".
    publish_mode: str = "review_first"
    #: Deterministic seed for every randomised stage.
    seed: int = 42

    @field_validator("tts_input_script")
    @classmethod
    def _tts_input_script_is_known(cls, value: str) -> str:
        cleaned = value.strip().lower().replace("-", "_")
        if cleaned not in ("roman", "urdu_script"):
            raise ValueError(
                "tts_input_script must be 'roman' or 'urdu_script', "
                f"got {value!r}")
        return cleaned

    @model_validator(mode="after")
    def _resolve_tts_input_script_default(self) -> "ProjectSpec":
        """Voice-aware default for ``tts_input_script`` (R4-002).

        Only applies when the caller did NOT set the field: an
        explicit per-channel choice (``"roman"`` included) is kept
        verbatim.  Resolution: an explicit ``voice_id`` wins (``ur-*``
        voices -> ``"urdu_script"``, any other voice -> ``"roman"``);
        without one, the narration language decides (Urdu ->
        ``"urdu_script"``, anything else -> ``"roman"``)."""
        if "tts_input_script" in self.model_fields_set:
            return self
        voice = str(self.voice_id or "").strip().lower()
        if voice:
            self.tts_input_script = (
                "urdu_script" if voice.startswith("ur-") else "roman")
            return self
        lang = str(self.language or "").strip().lower().replace("_", "-")
        is_urdu = lang in ("ur", "urdu", "roman-urdu", "roman urdu") \
            or lang.startswith("ur-")
        self.tts_input_script = "urdu_script" if is_urdu else "roman"
        return self

    @field_validator("niche")
    @classmethod
    def _niche_is_an_id(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("niche must not be empty")
        return cleaned


class ProductionResult(BaseModel):
    """The outcome of one end-to-end production run.

    Every field a report, publisher or auditor needs is here, already
    resolved to plain strings / numbers. ``final_mp4``,
    ``captions_srt`` and ``publish_manifest_json`` stay ``None`` when
    the run did not (or could not) produce them — a partial run is a
    result, not an exception.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str
    #: Resolved profile id actually used (e.g. "facts" / "other_cricket").
    niche_id: str
    topic: str
    #: Final (SEO) title chosen for the video.
    title: str
    #: Directory the deliverable pack was written to.
    pack_dir: str
    #: Path of the final rendered MP4, if rendering finished.
    final_mp4: str | None = None
    #: Thumbnail image paths produced for this video.
    thumbnails: list[str] = Field(default_factory=list)
    #: Path of the captions / subtitles SRT, if produced.
    captions_srt: str | None = None
    #: Path of the licence ledger CSV (always written, even if empty).
    licence_ledger_csv: str
    #: Path of the frozen publish manifest JSON, once frozen.
    publish_manifest_json: str | None = None
    #: Path of the human-readable production report (Markdown).
    report_md: str
    #: Measured duration of the final video in seconds, if known.
    duration_s: float | None = None
    #: Measured integrated loudness of the final video (LUFS), if known.
    loudness_lufs: float | None = None
    #: Total provider cost of the run in USD.
    cost_usd: float = 0.0
    #: Which code path / provider each stage actually used,
    #: e.g. ``{"tts": "edge-tts", "visuals": "generated-plates"}``.
    paths_used: dict[str, str] = Field(default_factory=dict)
    #: Gate A — Script Lock verdict.
    gate_a_passed: bool = False
    #: Gate B — Pre-Publish verdict.
    gate_b_passed: bool = False
    #: Technical QC verdict on the final file.
    qc_passed: bool = False
    #: Publish state machine state, e.g. "not_started" / "published".
    publish_state: str = "not_started"
    #: Wall-clock seconds the whole run took.
    elapsed_s: float = 0.0
    #: Non-fatal warnings collected along the way.
    warnings: list[str] = Field(default_factory=list)


@dataclass
class StageContext:
    """Per-project context passed through every integration stage.

    ``root`` is the project work dir (``<base>/<project_id>/``); the
    sub-directory properties are the ONLY paths stages should write to.
    The properties are pure path arithmetic — directories are created
    by :func:`make_context` (or explicitly via :meth:`ensure_dirs`), so
    constructing a context for an existing project never touches disk.
    """

    spec: ProjectSpec
    #: The resolved NicheProfile for ``spec.niche`` (kept as ``Any`` so
    #: this module never imports the niches package — no cycles).
    profile: Any
    project_id: str
    #: Project work dir: ``<base>/<project_id>/``.
    root: Path

    # -- sub-directories --------------------------------------------------
    @property
    def content_dir(self) -> Path:
        """Research / outline / script / SEO text artefacts."""
        return self.root / "content"

    @property
    def audio_dir(self) -> Path:
        """Voiceover / music / mix artefacts."""
        return self.root / "audio"

    @property
    def visuals_dir(self) -> Path:
        """Scene plates / stock / generated stills."""
        return self.root / "visuals"

    @property
    def render_dir(self) -> Path:
        """Intermediate renders and the final master."""
        return self.root / "render"

    @property
    def pack_dir(self) -> Path:
        """The deliverable pack: report, ledger, manifest, final files."""
        return self.root / "pack"

    # -- helpers ------------------------------------------------------------
    @property
    def niche_id(self) -> str:
        """Resolved niche/profile id (the profile wins over the spec)."""
        profile_id = getattr(self.profile, "profile_id", None)
        if isinstance(profile_id, str) and profile_id.strip():
            return profile_id
        return self.spec.niche

    def ensure_dirs(self) -> "StageContext":
        """Create ``root`` and every sub-directory (idempotent)."""
        for directory in (
            self.root,
            self.content_dir,
            self.audio_dir,
            self.visuals_dir,
            self.render_dir,
            self.pack_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_topic(topic: str, max_length: int = 32) -> str:
    slug = _SLUG_RE.sub("-", topic.strip().lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "video"


def _new_project_id(spec: ProjectSpec) -> str:
    """Readable, filesystem-safe, unique project id for one run."""
    return f"{_slugify_topic(spec.topic)}-{uuid.uuid4().hex[:8]}"


def make_context(
    spec: ProjectSpec,
    profile: Any,
    base_dir: str | Path,
    project_id: str | None = None,
) -> StageContext:
    """Build a :class:`StageContext` and create its directory layout.

    The project work dir is ``<base_dir>/<project_id>/`` with the
    ``content/``, ``audio/``, ``visuals/``, ``render/`` and ``pack/``
    sub-directories created inside it. When ``project_id`` is not given
    one is generated from the topic slug plus a short random suffix, so
    repeated runs on the same topic never collide.
    """
    if project_id is None:
        project_id = _new_project_id(spec)
    else:
        project_id = project_id.strip()
        if not project_id or "/" in project_id or "\\" in project_id:
            raise ValueError(
                f"project_id must be a single path component, got {project_id!r}"
            )
    root = Path(base_dir) / project_id
    context = StageContext(
        spec=spec,
        profile=profile,
        project_id=project_id,
        root=root,
    )
    return context.ensure_dirs()
