"""Visuals stage — scene planning + stock-first sourcing (Round 3).

Worker 4 of the Round 2 integration; Round 3 adds a stock-first path.
Consumes the content stage's ``ContentBundle`` (chapters of narration)
and the voice stage's ``VoiceResult`` (measured narration duration),
and produces:

* a sentence-level scene plan (atomic unit = the sentence, per
  ``pipeline/scenes``), timed from the *voice* duration — narration is
  the master clock, durations are distributed across scenes weighted by
  word count and reconciled with the beat ranges in
  ``pipeline/scenes/beats.py`` (HOOK 2-4 s, STANDARD 4-8 s, ...) *only
  when those ranges are jointly feasible for the measured total*.
  The beat ranges are preferred targets, never padding targets: when
  fast measured narration (real edge-tts runs ~250-300 effective WPM)
  makes a chapter shorter than the sum of its scenes' beat minimums,
  the measured timings win and sub-minimum scenes — including 2-4 s
  hooks and shorter — are legal (Round 3 policy; Round 2 died here
  when clamping inflated the timeline past the measured total and the
  final scene collapsed to ``end_s <= start_s``);
* one visual per scene.  When stock mode is ON (a fetcher is injected,
  or ``PIXABAY_API_KEY`` is present in the environment so a
  ``StockFetcher`` can be constructed from the sibling
  ``stock_fetch`` module), each scene first tries stock: queries come
  from ``stock_query.build_query_ladder(scene, profile)`` and assets
  from ``StockFetcher.fetch_for_scene(duration, queries,
  exclude_ids)``.  A stock hit becomes the scene's visual (a video
  clip is handed to the render path as-is — it trims/loops it to the
  scene duration; a photo is treated like a plate image with the
  scene's planned motion).  On a miss / error / duplicate the scene
  falls back to a locally generated plate — never a placeholder.
  When stock mode is OFF the stage behaves exactly as Round 2:
  one 1920x1080 PNG "local plate" per scene, generated in-app with
  Pillow.  A per-run spec flag (``use_stock=False`` /
  ``force_stock_off=True`` / ``stock_mode="off"`` on the spec, the
  ctx, or the call itself) forces stock off even when a key exists.
* a licence-ledger row per scene: the REAL row for stock scenes (via
  ``stock_ledger.ledger_row_for_stock_asset`` when available, else
  built here in the same LicenceLedger format with provider /
  licence / URL / sha fields filled), own/generated for plates;
* per-scene provenance on every record: ``source`` is exactly
  ``"stock:<provider>:<asset_id>"`` for stock scenes or
  ``"plate:local"`` for plates, plus aggregate ``scene_sources`` /
  ``stock_share`` / counts on the result and in the manifest;
* ``scene_manifest.json`` persisted into ``ctx.visuals_dir``.

Everything is duck-typed against the sibling stage contracts
(``bundle.chapters``, ``voice.duration_s`` / ``voice.chapter_files``,
``profile.profile_id`` / ``profile.visual_style``, ``ctx.visuals_dir``)
and deterministic: every random choice is seeded from the niche id +
video id + scene id, so re-running a job reproduces identical plates.

Text cards: TEXTUAL / HERO (and DATA) scenes render a short key phrase
(<= 6 words, the overlay cap) in DejaVu Sans Bold.  DejaVu cannot shape
Arabic-script text, so a key phrase containing Urdu/Arabic script is
NEVER rendered — the scene falls back to a graphic-only plate and the
record carries a ``urdu_script_not_rendered`` note.  Roman Urdu / Latin
text renders normally.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from ..data_dir import data_dir_layout
from ..pipeline.scenes.beats import BEAT_DURATION_RANGES, LIST_MIN_SECONDS_PER_ITEM
from ..pipeline.scenes.decision import PlanBudgetState, SceneFeatures, decide_source
from ..pipeline.scenes.models import (
    MAX_HERO_SCENES,
    MAX_OVERLAY_WORDS,
    BeatClass,
    MotionInstruction,
    MotionKind,
    ReasonCode,
    SceneClass,
    SceneRecord,
    SceneStatus,
    SourceKind,
)
from ..pipeline.scenes.motion import suggest_next_motion
from ..pipeline.scenes.safety import (
    NicheSafetyProfile,
    SceneSafetyFacts,
    validate_scene_safety,
)
from ..pipeline.scenes.timing import probe_duration_s
from ..pipeline.scenes.validators import validate_scene_plan

__all__ = ["VisualsResult", "plan_and_generate_visuals"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 1920, 1080

#: Licence-ledger values for every plate (contract-fixed strings).
LEDGER_SOURCE = "own/generated (local Pillow plate)"
LEDGER_LICENCE = "n/a — generated in-app"
LEDGER_KIND = "image"

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

#: Arabic-script blocks DejaVu cannot shape (Urdu lives in the first two).
_ARABIC_SCRIPT_RE = re.compile(
    "[" + "".join(
        chr(a) + "-" + chr(b)
        for a, b in ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
    ) + "]"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…۔؟])\s+|\n+")
_NUMBER_RE = re.compile(r"\d")
_QUOTE_RE = re.compile(r"[\"“”‘’«»„]|\bquote\b|\bverse\b|ayah|surah", re.IGNORECASE)

_EXPLANATORY_MARKERS = (
    "because", "kyunke", "kyun kay", "is liye", "isliye", "matlab",
    "wajah", "reason", "that means", "yaani", "yani", "socho", "imagine",
    "darasal", "haqeeqat", "samjh", "explains", "therefore", "is ka matlab",
)
_LIST_MARKERS = (
    "pehla", "pehli", "dusra", "dusri", "teesra", "teesri", "chautha",
    "first", "second", "third", "fourth", "finally", "aakhir",
)
_INTENSITY_MARKERS = (
    "sab se", "sabse", "biggest", "greatest", "most", "amazing",
    "zabardast", "khaufnaak", "hairan", "incredible", "shandaar",
    "khaas", "never", "hamesha", "always", "shocking", "raaz", "secret",
)

#: Per-niche palettes: (top RGB, bottom RGB, accent RGB).
_NICHE_PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "facts": ((13, 42, 92), (6, 18, 38), (255, 176, 66)),
    "islamic_stories": ((8, 62, 44), (4, 30, 22), (212, 175, 55)),
    "horror_stories": ((38, 6, 10), (5, 3, 6), (178, 34, 44)),
    "history": ((74, 52, 32), (26, 18, 10), (216, 182, 120)),
    "motivation": ((24, 22, 64), (94, 34, 18), (255, 154, 66)),
    "tech": ((6, 24, 58), (3, 10, 26), (56, 199, 255)),
    "crime_stories": ((26, 30, 40), (8, 10, 14), (196, 64, 60)),
    "health": ((8, 66, 62), (4, 28, 30), (140, 230, 160)),
}


# ---------------------------------------------------------------------------
# Parallel-built stock modules (Round 3) — monkeypatch / injection points.
#
# ``stock_query.build_query_ladder``, ``stock_fetch.StockFetcher`` and
# ``stock_ledger.ledger_row_for_stock_asset`` are built by sibling
# workers in parallel.  They are imported LAZILY (see the ``_load_*``
# helpers below) so this stage keeps working — pure Round 2 plates —
# when they are absent.  The module-level names exist so tests may
# monkeypatch them directly on this module.
# ---------------------------------------------------------------------------

build_query_ladder = None  # type: ignore[assignment]
StockFetcher = None  # type: ignore[assignment]
ledger_row_for_stock_asset = None  # type: ignore[assignment]
#: An already-constructed fetcher may also be parked on the module.
stock_fetcher = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Style Lab integration (mini-round) — monkeypatch / injection points.
#
# ``app.integration.style_profile`` is owned by a sibling worker (W1)
# and may land after this module; it is imported LAZILY (see
# ``_load_style_profile_module``) so this stage keeps working — pure
# pre-Style-Lab behaviour — when it is absent.  The module-level names
# exist so tests may monkeypatch them directly on this module.
# Contract (shared): ``resolve_style_profile(spec, base_dir)`` ->
# StyleProfile | None; ``style_directives(profile)`` -> dict incl.
# ``stock_ratio``, ``ai_ratio``, ``avg_shot_len_s``, ``cuts_per_min``,
# ``target_duration_s``.  Profiles carry PATTERNS ONLY (ratios, pacing)
# — never competitor assets, text or media (NO-COPY RULE).
# ---------------------------------------------------------------------------

resolve_style_profile = None  # type: ignore[assignment]
style_directives = None  # type: ignore[assignment]
#: An already-resolved profile / directives dict may be parked here.
style_profile = None  # type: ignore[assignment]


@dataclass
class VisualsResult:
    """Contract result of the visuals stage."""

    scene_manifest: list[dict]
    asset_files: dict[str, str]
    ledger_rows: list[dict]
    total_duration_s: float
    manifest_path: str = ""
    # -- Round 3 stock reporting (defaults keep Round 2 callers whole) --
    #: Per-scene provenance strings, in scene order:
    #: ``"stock:<provider>:<asset_id>"`` or ``"plate:local"``.
    scene_sources: list[str] = field(default_factory=list)
    #: Fraction of scenes sourced from stock (0..1).
    stock_share: float = 0.0
    stock_count: int = 0
    plate_count: int = 0
    #: True when stock mode was actually active for this run (a fetcher
    #: was available); False = pure Round 2 local-plate behaviour.
    stock_enabled: bool = False
    # -- Brightness selection filter (Round 5, SL-006) --
    #: Run stats of the near-black stock selection filter, copied from
    #: the fetcher (``brightness_filter.stats()``): floor, candidates
    #: checked, candidates rejected as too dark + per-rejection
    #: detail.  Empty dict when the fetcher carries no filter (fake
    #: fetchers in tests) — i.e. "not measured", never a fake zero.
    brightness_stats: dict = field(default_factory=dict)
    # -- Style Lab reporting (mini-round; empty = no profile resolved,
    #    i.e. exact pre-Style-Lab behaviour) --
    #: Applied style parameters, shaped ``{"visuals": {...}}`` when a
    #: style profile resolved for this run, else an empty dict.
    style_applied: dict = field(default_factory=dict)

    # -- sibling-stage aliases (assemble looks for ``scenes`` first;
    #    produce records ``path_used`` like the voice stage's) -----------
    @property
    def scenes(self) -> list[dict]:
        return self.scene_manifest

    @property
    def sources(self) -> list[str]:
        """Alias for :attr:`scene_sources` (per-scene provenance)."""
        return self.scene_sources

    @property
    def counts(self) -> dict[str, int]:
        return {
            "stock": self.stock_count,
            "plate": self.plate_count,
            "total": self.stock_count + self.plate_count,
        }

    @property
    def path_used(self) -> str:
        # Honest path: stock only claims the run when it actually
        # sourced at least one scene; an all-plate run (stock off, or
        # every fetch missed) still reports the Round 2 value.
        return "stock-first" if self.stock_count > 0 else "local-plates"


# ---------------------------------------------------------------------------
# Duck-typing helpers (bundle / voice / profile / ctx shapes vary by stage)
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute/key among ``names`` (objects and dicts)."""
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _ctx_less_tmp_dir(name: str) -> Path:
    """Fallback work dir when no StageContext supplies one.

    Lives under the canonical data dir's tmp area
    (``<data dir>/tmp/<name>``, via :mod:`app.data_dir`) — never a
    hard-coded OS ``/tmp`` path, which the packaged Windows app
    cannot rely on. Pure path arithmetic; creates nothing.
    """
    return data_dir_layout().tmp_dir / name


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _split_sentences(text: str) -> list[str]:
    """Split narration into sentences (protects decimals like ``3.5``)."""
    protected = re.sub(
        r"(\d)\.(\d)", lambda m: f"{m.group(1)}\x00{m.group(2)}", text
    )
    parts = _SENTENCE_SPLIT_RE.split(protected)
    sentences = []
    for part in parts:
        cleaned = part.replace("\x00", ".").strip()
        if cleaned:
            sentences.append(cleaned)
    return sentences


def _chapter_sentences(chapter: Any) -> list[str]:
    """All narration sentences of one chapter, however the stage stored them."""
    pre_split = _get(chapter, "sentences", "sentence_list", "lines")
    if isinstance(pre_split, (list, tuple)) and pre_split:
        sentences: list[str] = []
        for item in pre_split:
            text = _as_text(_get(item, "text", "sentence", default=item)).strip()
            if text:
                # A pre-split item may still hold several sentences.
                sentences.extend(_split_sentences(text) or [text])
        if sentences:
            return sentences
    text = _as_text(
        _get(
            chapter,
            "narration", "narration_text", "script", "script_text",
            "voiceover_text", "voiceover", "text", "body", "content",
            default="",
        )
    )
    return _split_sentences(text)


def _voice_total_s(voice: Any) -> float:
    total = _get(voice, "duration_s", "total_duration_s", "duration", default=0.0)
    try:
        return float(total)
    except (TypeError, ValueError):
        return 0.0


def _chapter_durations_s(voice: Any, n_chapters: int) -> Optional[list[float]]:
    """Per-chapter measured durations from the voice result, if available.

    A *present, correctly shaped* duration list that contains a
    zero/negative entry is a hard error, not a silent fallback: a
    zero-length measured segment cannot become a scene, and quietly
    re-deriving timings from word counts would fabricate the very
    durations the measurement just contradicted.
    """
    explicit = _get(voice, "chapter_durations_s", "chapter_durations")
    durations: list[float] = []
    if isinstance(explicit, (list, tuple)) and len(explicit) == n_chapters:
        for item in explicit:
            value = _get(item, "duration_s", "duration", default=item)
            try:
                durations.append(float(value))
            except (TypeError, ValueError):
                return None
        if any(d <= 0 for d in durations):
            raise ValueError(
                "visuals stage: measured chapter duration must be > 0 s "
                f"(got {durations}); a zero-length measured segment "
                "cannot be turned into a scene"
            )
        return durations

    files = _get(voice, "chapter_files", "chapter_audio_files", default=None)
    if isinstance(files, (list, tuple)) and len(files) == n_chapters:
        durations = []
        for entry in files:
            value = _get(entry, "duration_s", "duration", default=None)
            if value is None:
                path = _get(entry, "path", "file_path", "audio_path", default=entry)
                try:
                    if isinstance(path, (str, Path)) and Path(path).is_file():
                        value = probe_duration_s(path)
                except Exception:
                    value = None
            try:
                durations.append(float(value))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        if any(d <= 0 for d in durations):
            raise ValueError(
                "visuals stage: measured chapter duration must be > 0 s "
                f"(got {durations}); a zero-length measured segment "
                "cannot be turned into a scene"
            )
        return durations
    return None


# ---------------------------------------------------------------------------
# Round 3 stock-first helpers (all imports lazy + defensive)
#
# Credential rule: the Pixabay key is only ever read as "present /
# absent" from the environment, or passed *into* a StockFetcher
# constructor that asks for it.  It is never written, printed or
# logged here, and exception messages from the stock layer are reduced
# to their exception *type* before they reach a scene note.
# ---------------------------------------------------------------------------

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".ts", ".m4v", ".avi"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _import_sibling(module_tail: str) -> Any:
    """Import ``app.integration.<module_tail>`` (or the relative form)."""
    import importlib

    for name in (f"app.integration.{module_tail}", module_tail):
        try:
            if name == module_tail and __package__:
                return importlib.import_module(f".{module_tail}", __package__)
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _load_build_query_ladder() -> Optional[Any]:
    fn = globals().get("build_query_ladder")
    if callable(fn):
        return fn
    module = _import_sibling("stock_query")
    fn = getattr(module, "build_query_ladder", None) if module is not None else None
    return fn if callable(fn) else None


def _load_stock_fetcher_class() -> Optional[Any]:
    cls = globals().get("StockFetcher")
    if cls is not None:
        return cls
    module = _import_sibling("stock_fetch")
    if module is None:
        return None
    cls = getattr(module, "StockFetcher", None)
    if cls is not None:
        return cls
    # A module exposing only the bare function still satisfies the
    # contract shape — wrap it so callers see one interface.
    fn = getattr(module, "fetch_for_scene", None)
    if callable(fn):
        return SimpleNamespace(fetch_for_scene=fn)
    return None


def _load_ledger_helper() -> Optional[Any]:
    fn = globals().get("ledger_row_for_stock_asset")
    if callable(fn):
        return fn
    module = _import_sibling("stock_ledger")
    fn = (
        getattr(module, "ledger_row_for_stock_asset", None)
        if module is not None
        else None
    )
    return fn if callable(fn) else None


def _truthy_flag(value: Any) -> Optional[bool]:
    """Tri-state bool for spec flags (strings included); None = unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return bool(value)


#: Spec keys that force stock OFF when truthy.
_STOCK_OFF_KEYS = (
    "force_stock_off", "disable_stock", "stock_disabled",
    "no_stock", "stock_off",
)
#: Spec keys carrying a plain on/off boolean for stock.
_STOCK_BOOL_KEYS = (
    "use_stock", "stock_enabled", "enable_stock",
    "stock_first", "use_stock_first",
)
#: Spec keys carrying a mode string ("off" / "local" vs "stock-first").
_STOCK_MODE_KEYS = ("stock_mode", "visuals_mode", "visual_mode")
_STOCK_MODE_OFF = {"off", "disabled", "local", "plate", "plates", "local-plates", "local_plates"}
_STOCK_MODE_ON = {"on", "stock", "stock-first", "stock_first", "stockfirst", "enabled"}


def _stock_setting_from(obj: Any) -> Optional[str]:
    """``"off"`` / ``"on"`` / None from one spec-like object or dict."""
    if obj is None:
        return None
    for key in _STOCK_OFF_KEYS:
        if _truthy_flag(_get(obj, key)):
            return "off"
    for key in _STOCK_BOOL_KEYS:
        flag = _truthy_flag(_get(obj, key))
        if flag is not None:
            return "on" if flag else "off"
    for key in _STOCK_MODE_KEYS:
        raw = _get(obj, key)
        if isinstance(raw, str):
            mode = raw.strip().lower()
            if mode in _STOCK_MODE_OFF:
                return "off"
            if mode in _STOCK_MODE_ON:
                return "on"
    return None


def _resolve_stock_setting(
    explicit: Any, ctx: Any, bundle: Any, profile: Any, extra: dict
) -> Optional[str]:
    """First stock on/off setting found, most specific source first."""
    candidates: list[Any] = []
    if explicit is not None:
        candidates.append({"use_stock": explicit})
    if extra:
        candidates.append(extra)
    candidates.append(ctx)
    spec = _get(ctx, "spec")
    if spec is not None:
        candidates.append(spec)
    candidates.append(profile)
    candidates.append(bundle)
    bundle_spec = _get(bundle, "spec")
    if bundle_spec is not None:
        candidates.append(bundle_spec)
    for candidate in candidates:
        setting = _stock_setting_from(candidate)
        if setting is not None:
            return setting
    for env_key in ("YOUTUBE_AGENT_DISABLE_STOCK", "DISABLE_STOCK", "STOCK_OFF"):
        if _truthy_flag(os.environ.get(env_key)):
            return "off"
    return None


def _find_injected_fetcher(ctx: Any, bundle: Any, profile: Any, extra: dict) -> Any:
    """A fetcher handed in via ctx / spec / bundle / profile / module."""
    for source in (extra, ctx, _get(ctx, "spec"), profile, bundle):
        if source is None:
            continue
        for key in ("stock_fetcher", "fetcher", "stock_client", "stock_fetch"):
            value = _get(source, key)
            if value is not None:
                return value
    parked = globals().get("stock_fetcher")
    if parked is not None:
        return parked
    return None


def _instantiate_fetcher(cls: Any, download_dir: Path, ctx: Any) -> Any:
    """Construct a StockFetcher, filling constructor params by name.

    The parallel module's constructor signature is not part of the
    fixed contract, so parameters are matched by name: key-ish params
    receive the env key (value never logged), dir-ish params receive
    the stock download dir, ctx-ish params receive the stage context.
    Several construction shapes are tried; total failure returns None
    and the stage falls back to plates.
    """
    if cls is None:
        return None
    if not isinstance(cls, type):
        return cls  # already an instance / namespace with the method
    api_key = os.environ.get("PIXABAY_API_KEY") or None
    try:
        params = [
            p
            for p in inspect.signature(cls).parameters.values()
            if p.kind
            in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):
        params = []
    kwargs: dict[str, Any] = {}
    for param in params:
        name = param.name.lower()
        if "key" in name or name in ("secret", "token", "api_secret"):
            if api_key:
                kwargs[param.name] = api_key
        elif (
            name in ("dir", "directory", "folder", "cache_dir")
            or name.endswith(("_dir", "_directory"))
            or (name.endswith("_path") and "url" not in name)
        ):
            # Directory-ish params only — a ``downloader`` callable or
            # an ``adapter``/``provider`` object must never receive a
            # path string (the real StockFetcher has both shapes).
            kwargs[param.name] = str(download_dir)
        elif name in ("ctx", "context"):
            kwargs[param.name] = ctx
    attempts: list[tuple[tuple, dict]] = []
    if kwargs:
        attempts.append(((), kwargs))
    attempts.append(((), {}))
    if api_key:
        attempts.append(((api_key,), {}))
        attempts.append(((str(download_dir),), {}))
        attempts.append(((api_key, str(download_dir)), {}))
    for args, kw in attempts:
        try:
            return cls(*args, **kw)
        except Exception:
            continue
    # An uninstantiable class may still expose a classmethod contract.
    if callable(getattr(cls, "fetch_for_scene", None)):
        return cls
    return None


def _resolve_stock_fetcher(
    *,
    explicit_fetcher: Any,
    ctx: Any,
    bundle: Any,
    profile: Any,
    extra: dict,
    visuals_dir: Path,
    setting: Optional[str],
) -> tuple[Any, bool]:
    """(fetcher, stock_enabled) per the Round 3 enablement rules."""
    if setting == "off":
        return None, False
    injected = explicit_fetcher
    if injected is None:
        injected = _find_injected_fetcher(ctx, bundle, profile, extra)
    if injected is not None:
        if isinstance(injected, type):
            built = _instantiate_fetcher(injected, visuals_dir / "stock", ctx)
            return (built if built is not None else injected), True
        return injected, True
    # No injection: stock is ON only when the env key is present.
    if os.environ.get("PIXABAY_API_KEY"):
        cls = _load_stock_fetcher_class()
        if cls is not None:
            built = _instantiate_fetcher(cls, visuals_dir / "stock", ctx)
            if built is not None:
                return built, True
            if not isinstance(cls, type):
                return cls, True
        # Key present but the parallel module / constructor is not
        # usable yet: degrade honestly to plates for this run.
        return None, False
    return None, False


class _SceneQueryView(dict):
    """Scene handed to ``build_query_ladder`` / the ledger helper.

    A dict that also supports attribute access, so the parallel
    modules may read the scene either way.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    __setattr__ = dict.__setitem__  # type: ignore[assignment]


def _fallback_query_ladder(text: str, niche_id: str) -> list[str]:
    """Deterministic queries when the query module is unavailable."""
    words = [
        w.strip(" .,!?:;\"'()[]")
        for w in (text or "").split()
    ]
    words = [w for w in words if len(w) > 2]
    ladder: list[str] = []
    if words:
        ladder.append(" ".join(words[:8]))
        ladder.append(" ".join(words[:4]))
    if niche_id and niche_id != "custom":
        ladder.append(f"{niche_id} {' '.join(words[:3])}".strip())
    seen: set[str] = set()
    out: list[str] = []
    for query in ladder:
        query = query.strip()
        if query and query.lower() not in seen:
            seen.add(query.lower())
            out.append(query)
    return out


def _queries_for_scene(
    view: _SceneQueryView, profile: Any, *, niche_id: str, text: str
) -> list[str]:
    """Query ladder via the parallel module, with a local fallback."""
    fn = _load_build_query_ladder()
    if fn is not None:
        attempts = (
            lambda: fn(view, profile),
            lambda: fn(dict(view), profile),
            lambda: fn(SimpleNamespace(**dict(view)), profile),
            lambda: fn(view),
        )
        for attempt in attempts:
            try:
                result = attempt()
            except TypeError:
                continue
            except Exception:
                break
            if isinstance(result, str):
                result = [result]
            if result:
                queries = [
                    str(q).strip() for q in result if str(q).strip()
                ]
                if queries:
                    return queries
            break
    return _fallback_query_ladder(text, niche_id)


def _await_if_needed(result: Any) -> Any:
    """Resolve a coroutine fetcher result from this sync stage.

    Only a true coroutine is driven. Objects that merely define
    ``__await__`` as a tolerance shim (e.g. stock_fetch.StockSceneAsset,
    whose ``__await__`` lets async callers ``await`` an already-realised
    value) are realised values, not pending work —
    ``inspect.isawaitable`` misclassifies them and driving them as
    coroutines raises ``ValueError: a coroutine was expected``, which
    cost every scene its stock asset in the first Round 3 live run
    (all scenes fell back to plates).

    Loop ownership (Round 4): the coroutine is driven via
    ``app.integration.run_loop.run_coroutine`` — on the produce run's
    single loop inside a run, never on a loop owned by this stage.
    """
    import asyncio

    if not asyncio.iscoroutine(result):
        return result

    from .run_loop import run_coroutine

    return run_coroutine(result)


def _fetch_stock_asset(
    fetcher: Any, duration_s: float, queries: list[str], exclude_ids: set[str]
) -> Any:
    """Call ``fetch_for_scene`` honouring its contract signature.

    Positional contract first —
    ``fetch_for_scene(scene_duration_s, queries, exclude_asset_ids)`` —
    with a keyword call built from the callee's own parameter names as
    the fallback for differently-named (but contract-shaped) params.
    Returns whatever the fetcher returns (asset or None); exceptions
    propagate so the caller can fall back per scene.
    """
    method = getattr(fetcher, "fetch_for_scene", None)
    if method is None:
        for alias in ("fetch", "fetch_scene", "fetch_asset", "get_asset"):
            method = getattr(fetcher, alias, None)
            if callable(method):
                break
    if method is None and callable(fetcher):
        method = fetcher
    if method is None:
        raise TypeError("stock fetcher exposes no fetch_for_scene")
    try:
        return _await_if_needed(method(float(duration_s), list(queries), set(exclude_ids)))
    except TypeError:
        pass
    kwargs: dict[str, Any] = {}
    try:
        names = list(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        names = []
    for name in names:
        lowered = name.lower()
        if "duration" in lowered or lowered in ("seconds", "dur"):
            kwargs[name] = float(duration_s)
        elif "quer" in lowered or lowered in ("ladder",):
            kwargs[name] = list(queries)
        elif "exclude" in lowered or "used" in lowered or lowered in ("exclude",):
            kwargs[name] = set(exclude_ids)
    if kwargs:
        return _await_if_needed(method(**kwargs))
    raise TypeError("fetch_for_scene accepted neither positional nor keyword call")


def _asset_field(asset: Any, *names: str, default: Any = None) -> Any:
    """Duck-typed asset field, also looking inside ``asset.candidate``."""
    value = _get(asset, *names, default=None)
    if value is not None:
        return value
    candidate = _get(asset, "candidate")
    if candidate is not None:
        value = _get(candidate, *names, default=None)
        if value is not None:
            return value
    return default


def _valid_sha256(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if _SHA256_RE.match(cleaned):
            return cleaned
    return None


def _file_sha256(path: Path) -> Optional[str]:
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return None


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    for method_name in ("model_dump", "to_csv_dict", "to_dict"):
        method = getattr(row, method_name, None)
        if callable(method):
            try:
                data = method()
            except Exception:
                continue
            if isinstance(data, dict):
                return dict(data)
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    return {}


def _stock_ledger_row(
    asset: Any,
    view: _SceneQueryView,
    *,
    scene_id: str,
    source_str: str,
    provider: str,
    provider_asset_id: str,
    local_path: str,
    content_hash: Optional[str],
    kind: str,
) -> dict[str, Any]:
    """The REAL licence-ledger row for one stock scene.

    Uses ``stock_ledger.ledger_row_for_stock_asset(asset, scene)`` when
    the parallel module is available; otherwise builds the row here in
    the existing LicenceLedger format with provider / licence / URL /
    sha fields filled (the ledger worker reconciles formats).
    """
    licence_ref = f"lic-{scene_id}-{provider_asset_id}"
    row: dict[str, Any] = {}
    helper = _load_ledger_helper()
    if helper is not None:
        for attempt in (
            lambda: helper(asset, view),
            lambda: helper(asset, dict(view)),
            lambda: helper(asset, scene_id),
            lambda: helper(asset=asset, scene=view),
        ):
            try:
                row = _row_to_dict(attempt())
            except Exception:
                row = {}
            if row:
                break

    # The ledger helper's row is authoritative when it supplied one;
    # raw asset fields only fill the gaps.
    licence_name = (
        row.get("licence_name")
        or row.get("licence")
        or row.get("license_name")
        or _asset_field(asset, "licence_name", "licence", "license_name", "license")
        or "Pixabay Content License"
    )
    terms_hash = (
        row.get("licence_terms_hash")
        or row.get("terms_hash")
        or _asset_field(
            asset, "terms_hash", "licence_terms_hash", "license_terms_hash",
            "terms_sha256",
        )
        or ""
    )
    page_url = (
        row.get("asset_page_url")
        or row.get("page_url")
        or _asset_field(asset, "asset_page_url", "page_url", "asset_url", "url")
        or ""
    )
    download_url = (
        row.get("download_url")
        or _asset_field(asset, "download_url", "download", "file_url")
        or ""
    )
    file_sha = (
        _valid_sha256(row.get("file_sha256"))
        or _valid_sha256(row.get("content_hash"))
        or _valid_sha256(
            _asset_field(asset, "file_sha256", "sha256", "content_hash", "file_hash")
        )
        or content_hash
        or ""
    )
    width = row.get("width") or row.get("width_px") or _asset_field(asset, "width", "width_px")
    height = row.get("height") or row.get("height_px") or _asset_field(asset, "height", "height_px")
    notes_parts = []
    if page_url:
        notes_parts.append(f"page={page_url}")
    if terms_hash:
        notes_parts.append(f"terms_hash={terms_hash}")
    normalised: dict[str, Any] = {
        "licence_ref": row.get("licence_ref") or row.get("licence_id") or licence_ref,
        "licence_id": row.get("licence_id") or row.get("licence_ref") or licence_ref,
        "scene_id": row.get("scene_id") or scene_id,
        "asset_id": provider_asset_id,
        "provider_asset_id": provider_asset_id,
        "provider": provider,
        "asset_path": local_path,
        "local_path": row.get("local_path")
        or row.get("local_file_path")
        or row.get("asset_path")
        or local_path,
        "kind": row.get("kind") or kind,
        "source": row.get("source") or source_str,
        "licence": str(licence_name),
        "licence_name": str(licence_name),
        "content_hash": file_sha,
        "file_sha256": file_sha,
        "terms_hash": str(terms_hash),
        "licence_terms_hash": str(terms_hash),
        "asset_page_url": str(page_url),
        "page_url": str(page_url),
        "download_url": str(download_url),
        "width_px": width,
        "height_px": height,
        "width": width,
        "height": height,
        "notes": row.get("notes") or "; ".join(notes_parts),
    }
    # Preserve any extra fields the ledger helper supplied.
    for key, value in row.items():
        normalised.setdefault(key, value)
    return normalised


# ---------------------------------------------------------------------------
# Scene classification
# ---------------------------------------------------------------------------


def _classify_sentence(
    text: str, *, is_hook: bool
) -> tuple[SceneClass, BeatClass, int]:
    """Return (scene_class, beat_class, list_item_count) for one sentence."""
    lowered = text.lower()
    # Numeric *tokens* ("2020" is one fact, not four).
    numbers = sum(1 for word in text.split() if _NUMBER_RE.search(word))

    if _QUOTE_RE.search(text):
        return SceneClass.TEXTUAL, BeatClass.STANDARD, 1
    if numbers >= 2 or "%" in text or "percent" in lowered:
        return SceneClass.DATA, BeatClass.STANDARD, 1

    list_hits = sum(1 for m in _LIST_MARKERS if m in lowered)
    if list_hits >= 2 or (list_hits >= 1 and text.count(",") >= 2):
        items = max(2, min(6, text.count(",") + 1))
        return SceneClass.REAL_WORLD, BeatClass.LIST, items

    if any(marker in lowered for marker in _EXPLANATORY_MARKERS) or len(text.split()) >= 24:
        return SceneClass.ABSTRACT_CONCEPT, BeatClass.EXPLANATORY, 1
    if is_hook:
        return SceneClass.REAL_WORLD, BeatClass.HOOK, 1
    return SceneClass.REAL_WORLD, BeatClass.STANDARD, 1


def _intensity_score(text: str) -> float:
    lowered = text.lower()
    score = 3.0 if "!" in text else 0.0
    score += 2.0 * sum(1 for m in _INTENSITY_MARKERS if m in lowered)
    score += min(1.5, len(text.split()) / 40.0)
    return score


def _key_phrase(text: str, scene_class: SceneClass) -> str:
    """Short (<= MAX_OVERLAY_WORDS) renderable phrase for a text card."""
    quoted = re.search(r"[\"“]([^\"”]{3,80})[\"”]", text)
    source = quoted.group(1) if quoted else text
    words = source.strip(" .!?,;:").split()
    if scene_class is SceneClass.DATA:
        for idx, word in enumerate(words):
            if _NUMBER_RE.search(word):
                start = max(0, idx - 2)
                words = words[start : start + MAX_OVERLAY_WORDS]
                break
        else:
            words = words[:MAX_OVERLAY_WORDS]
    else:
        words = words[:MAX_OVERLAY_WORDS]
    return " ".join(words)


def _has_arabic_script(text: str) -> bool:
    return bool(_ARABIC_SCRIPT_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Duration distribution (word-weighted, reconciled with beat ranges)
# ---------------------------------------------------------------------------


def _distribute_durations(
    total_s: float,
    weights: Sequence[float],
    ranges: Sequence[tuple[float, Optional[float]]],
) -> list[float]:
    """Split the *measured* ``total_s`` by weight across scenes.

    Policy (Round 3 — measured narration wins):

    * The beat ``ranges`` are preferred targets.  They are applied via
      iterative clamp-and-redistribute ONLY when they are jointly
      feasible for ``total_s`` (sum of minimums <= total <= sum of
      maximums): scenes pinned at a bound stop absorbing time and the
      remainder flows to the still-free scenes, so the split still
      sums exactly to the measured total.
    * When the ranges are jointly infeasible — fast measured
      narration whose chapter total is *below* the sum of the beat
      minimums (the Round 2 failure: STANDARD >= 4 s/scene against a
      ~17 s video) — clamping would inflate the split past the
      measured total and collapse a later scene to ``end <= start``.
      In that case NO clamping or padding is applied at all: the pure
      word-weighted split of the measured total is returned, and the
      resulting sub-minimum scenes are legal (see
      ``pipeline/scenes/beats.py``, measured-narration policy).
    * The only floor ever enforced here is positivity, and it is
      structural, not a clamp: with ``total_s > 0`` and positive
      weights every share is positive by construction, so no scene
      can come out zero/negative.  A non-positive ``total_s`` or
      weight is a broken measurement and raises — it is never
      silently floored up to an invented duration.
    """
    n = len(weights)
    if n == 0:
        return []
    if total_s <= 0:
        raise ValueError(
            "visuals stage: cannot distribute a non-positive measured "
            f"duration ({total_s}s) across {n} scene(s); a zero-length "
            "measured segment cannot be turned into a scene"
        )
    if any(w <= 0 for w in weights):
        raise ValueError(
            f"visuals stage: scene weights must be positive, got {list(weights)}"
        )
    weight_sum = sum(weights)
    durations = [total_s * (w / weight_sum) for w in weights]

    lows_total = sum(low for low, _ in ranges)
    highs_total = sum(
        high if high is not None else float("inf") for _, high in ranges
    )
    if lows_total <= total_s <= highs_total:
        # Jointly feasible: reconcile with the beat ranges.
        for _ in range(80):
            for i in range(n):
                low, high = ranges[i]
                if durations[i] < low:
                    durations[i] = low
                if high is not None and durations[i] > high:
                    durations[i] = high
            diff = total_s - sum(durations)
            if abs(diff) < 1e-6:
                break
            free = []
            for i in range(n):
                low, high = ranges[i]
                if diff > 0 and (high is None or durations[i] < high - 1e-9):
                    free.append(i)
                elif diff < 0 and durations[i] > low + 1e-9:
                    free.append(i)
            if not free:
                break
            free_weight = sum(weights[i] for i in free) or float(len(free))
            for i in free:
                durations[i] += diff * (weights[i] / free_weight)
    # else: infeasible for the measured total — measured wins, the
    # proportional split above stands unclamped (no fabricated time).

    # Final exactness fix on the longest scene: absorbs float drift
    # only (~1e-12 s), never a policy adjustment.  The guard keeps it
    # from ever producing a non-positive duration.
    drift = total_s - sum(durations)
    if abs(drift) > 1e-9:
        idx = max(range(n), key=lambda i: durations[i])
        if durations[idx] + drift > 0:
            durations[idx] += drift
    if any(d <= 0 for d in durations):  # structural invariant, unreachable by construction
        raise ValueError(
            f"visuals stage: distribution produced a non-positive scene "
            f"duration from measured total {total_s}s: {durations}"
        )
    return durations


def _beat_range(beat: BeatClass, list_items: int) -> tuple[float, Optional[float]]:
    """*Preferred* (min, max) seconds for a beat — a target range for
    :func:`_distribute_durations`, not a legality floor: measured
    narration shorter than the minimum yields a legal shorter scene."""
    if beat is BeatClass.LIST:
        return (LIST_MIN_SECONDS_PER_ITEM * list_items, 15.0)
    low, high = BEAT_DURATION_RANGES[beat]
    return (low or 1.5, high)


# ---------------------------------------------------------------------------
# Palettes + plate rendering (Pillow, seeded, fast)
# ---------------------------------------------------------------------------


def _hash_int(*parts: str) -> int:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _hsv_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (round(r * 255), round(g * 255), round(b * 255))


def _palette_for(niche_id: str, visual_style: str) -> tuple[tuple[int, int, int], ...]:
    if niche_id in _NICHE_PALETTES:
        return _NICHE_PALETTES[niche_id]
    style = (visual_style or "").lower()
    for key, palette in _NICHE_PALETTES.items():
        if key.split("_")[0] in style:
            return palette
    hue = (_hash_int("palette", niche_id) % 360) / 360.0
    top = _hsv_rgb(hue, 0.62, 0.30)
    bottom = _hsv_rgb(hue + 0.07, 0.68, 0.12)
    accent = _hsv_rgb(hue + 0.48, 0.75, 0.88)
    return (top, bottom, accent)


def _shift(rgb: tuple[int, int, int], delta: float, rng: random.Random) -> tuple[int, int, int]:
    import colorsys

    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    return _hsv_rgb(h + delta, s, max(0.05, min(1.0, v * rng.uniform(0.92, 1.08))))


def _gradient_image(
    top: tuple[int, int, int], bottom: tuple[int, int, int]
) -> Image.Image:
    """Vertical gradient via a small strip upscaled (fast + smooth)."""
    strip_h = 270
    strip = Image.new("RGB", (1, strip_h))
    px = strip.load()
    for y in range(strip_h):
        t = y / (strip_h - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return strip.resize((WIDTH, HEIGHT), Image.BICUBIC)


_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        key = (path, size)
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        try:
            font = ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
        _FONT_CACHE[key] = font
        return font
    return ImageFont.load_default()  # type: ignore[return-value]


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_card_text(
    draw: ImageDraw.ImageDraw, text: str, *, hero: bool
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest font (from a size ladder) whose wrapped text fits the card."""
    max_w = 1480
    for size in (118, 104, 92, 80, 68, 58, 48, 40):
        if not hero and size > 92:
            continue
        font = _font(size)
        lines = _wrap_text(draw, text, font, max_w)
        if len(lines) <= 3 and all(
            draw.textlength(line, font=font) <= max_w for line in lines
        ):
            return font, lines
    font = _font(36)
    return font, _wrap_text(draw, text, font, max_w)[:3]


def _draw_patterns(
    img: Image.Image,
    accent: tuple[int, int, int],
    rng: random.Random,
    pattern: str,
) -> None:
    # Patterns are subtle decoration: draw at half resolution and upscale
    # (full-res RGBA shape drawing is the plate-render bottleneck).
    hw, hh = WIDTH // 2, HEIGHT // 2
    overlay = Image.new("RGBA", (hw, hh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    r, g, b = accent
    if pattern == "circles":
        for _ in range(16):
            cx, cy = rng.uniform(-100, hw), rng.uniform(-75, hh)
            rad = rng.uniform(20, 160)
            alpha = rng.randint(14, 46)
            width = rng.randint(1, 3)
            draw.ellipse(
                [cx - rad, cy - rad, cx + rad, cy + rad],
                outline=(r, g, b, alpha), width=width,
            )
    elif pattern == "lines":
        for _ in range(14):
            y = rng.uniform(0, hh)
            alpha = rng.randint(12, 40)
            draw.line(
                [(-50, y + rng.uniform(-130, 130)), (hw + 50, y)],
                fill=(r, g, b, alpha), width=rng.randint(1, 3),
            )
    elif pattern == "dots":
        for gx in range(40, hw, 60):
            for gy in range(35, hh, 60):
                jx, jy = rng.uniform(-7, 7), rng.uniform(-7, 7)
                rad = rng.uniform(1.2, 3.0)
                draw.ellipse(
                    [gx + jx - rad, gy + jy - rad, gx + jx + rad, gy + jy + rad],
                    fill=(r, g, b, rng.randint(22, 52)),
                )
    elif pattern == "arcs":
        for _ in range(10):
            cx, cy = rng.uniform(0, hw), rng.uniform(0, hh)
            rad = rng.uniform(60, 260)
            start, end = rng.uniform(0, 360), rng.uniform(0, 360)
            if end < start:
                start, end = end, start
            draw.arc(
                [cx - rad, cy - rad, cx + rad, cy + rad],
                start=start, end=end,
                fill=(r, g, b, rng.randint(18, 48)), width=rng.randint(2, 4),
            )
    else:  # grid
        for x in range(0, hw, 80):
            draw.line([(x, 0), (x, hh)], fill=(r, g, b, 16), width=1)
        for y in range(0, hh, 80):
            draw.line([(0, y), (hw, y)], fill=(r, g, b, 16), width=1)
    overlay = overlay.resize((WIDTH, HEIGHT), Image.BILINEAR)
    # Soft accent glow, bottom-right (built small, upscaled = soft).
    small_w, small_h = WIDTH // 8, HEIGHT // 8
    glow_small = Image.new("RGBA", (small_w, small_h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_small)
    gx = rng.uniform(WIDTH * 0.55, WIDTH * 0.95) / 8
    gy = rng.uniform(HEIGHT * 0.5, HEIGHT * 0.95) / 8
    grad = rng.uniform(260, 460) / 8
    gd.ellipse([gx - grad, gy - grad, gx + grad, gy + grad], fill=(r, g, b, 42))
    glow = glow_small.resize((WIDTH, HEIGHT), Image.BICUBIC)
    # One RGBA round-trip for both composites (full-size converts are slow).
    base = img.convert("RGBA")
    base.alpha_composite(glow)
    base.alpha_composite(overlay)
    img.paste(base.convert("RGB"), (0, 0))


def _draw_data_bars(
    img: Image.Image, accent: tuple[int, int, int], rng: random.Random
) -> None:
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    base_y, max_h, bar_w, gap, x = 880, 420, 150, 70, 300
    r, g, b = accent
    draw.line([(220, base_y), (WIDTH - 220, base_y)], fill=(255, 255, 255, 110), width=3)
    for i in range(6):
        h = rng.uniform(0.25, 1.0) * max_h
        draw.rounded_rectangle(
            [x, base_y - h, x + bar_w, base_y], radius=10, fill=(r, g, b, 150)
        )
        x += bar_w + gap
    base = img.convert("RGBA")
    base.alpha_composite(overlay)
    img.paste(base.convert("RGB"), (0, 0))


def _draw_text_card(
    img: Image.Image,
    accent: tuple[int, int, int],
    phrase: str,
    *,
    hero: bool,
) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    font, lines = _fit_card_text(draw, phrase, hero=hero)
    line_h = int(font.size * 1.28)
    text_w = max(draw.textlength(line, font=font) for line in lines)
    panel_w = min(1640, int(text_w) + 170)
    panel_h = line_h * len(lines) + 130
    x0, y0 = (WIDTH - panel_w) // 2, (HEIGHT - panel_h) // 2
    x1, y1 = x0 + panel_w, y0 + panel_h
    draw.rounded_rectangle(
        [x0, y0, x1, y1], radius=28, fill=(6, 8, 14, 208),
        outline=accent + (255,), width=4,
    )
    draw.rectangle([x0 + 60, y0 + 42, x0 + 190, y0 + 50], fill=accent + (255,))
    ty = y0 + 78
    for line in lines:
        draw.text(
            ((WIDTH - draw.textlength(line, font=font)) / 2, ty),
            line, font=font, fill=(246, 246, 240, 255),
        )
        ty += line_h


def _render_plate(
    out_path: Path,
    *,
    palette: tuple[tuple[int, int, int], ...],
    seed: int,
    phrase: Optional[str],
    scene_class: SceneClass,
    hero: bool,
) -> None:
    """Render one 1920x1080 plate; fast (small-strip gradient + overlays)."""
    rng = random.Random(seed)
    top, bottom, accent = palette
    top = _shift(top, rng.uniform(-0.02, 0.02), rng)
    bottom = _shift(bottom, rng.uniform(-0.02, 0.02), rng)
    img = _gradient_image(top, bottom)
    pattern = rng.choice(["circles", "lines", "dots", "arcs", "grid"])
    _draw_patterns(img, accent, rng, pattern)
    if scene_class is SceneClass.DATA:
        _draw_data_bars(img, accent, rng)
    if phrase:
        _draw_text_card(img, accent, phrase, hero=hero)
    img.save(out_path, format="PNG", optimize=False, compress_level=2)


# ---------------------------------------------------------------------------
# Style Lab wiring — profile resolution, directives, shot-length
# re-segmentation and stock:AI mix planning (mini-round, W3)
#
# NO-COPY RULE: a style profile carries planning PATTERNS only (shot
# length, stock:AI ratio).  Nothing here imports, reads or reuses any
# competitor asset; "AI"-planned scenes use the existing local
# plate/generated-still path, exactly like a stock fallback does.
# ---------------------------------------------------------------------------


def _load_style_profile_module() -> Any:
    """Import ``app.integration.style_profile`` lazily, or None."""
    return _import_sibling("style_profile")


def _plain_profile_data(obj: Any) -> dict[str, Any]:
    """Best-effort plain-dict view of a profile / directives object."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return {
                str(k): v for k, v in vars(obj).items()
                if not str(k).startswith("_")
            }
        except Exception:
            return {}
    return {}


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN guard


def _coerce_ratio(value: Any) -> Optional[float]:
    """A ratio in [0, 1]; percent-scale values (>1) are divided by 100."""
    out = _coerce_float(value)
    if out is None:
        return None
    if out > 1.0:
        out = out / 100.0 if out <= 100.0 else None
        if out is None:
            return None
    return max(0.0, min(1.0, out))


_DIRECTIVE_SOURCES = (
    "directives", "style_directives", "targets", "pacing",
    "scenes", "scene_analysis", "visuals", "visual_mix", "video",
    "duration",
)


def _normalise_directives(raw: Any) -> dict[str, Optional[float]]:
    """Extract the contract directives from any profile-shaped object.

    Accepts the flat contract shape (``stock_ratio`` / ``ai_ratio`` /
    ``avg_shot_len_s`` / ``cuts_per_min`` / ``target_duration_s``) AND
    the Style Lab analysis aggregates it may be derived from
    (``visual_mix.stock_like`` percents, ``scenes.avg_shot_length_s`` /
    ``cuts_per_minute``, ``video.duration_s``).  Unknown / missing
    values stay ``None`` — nothing is invented.
    """
    top = _plain_profile_data(raw)
    sources: list[dict[str, Any]] = [top]
    for key in _DIRECTIVE_SOURCES:
        nested = top.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
        elif nested is not None:
            plain = _plain_profile_data(nested)
            if plain:
                sources.append(plain)

    def find(aliases: tuple[str, ...], *, ratio: bool = False) -> Optional[float]:
        for source in sources:
            for alias in aliases:
                if alias in source and source[alias] is not None:
                    value = (
                        _coerce_ratio(source[alias]) if ratio
                        else _coerce_float(source[alias])
                    )
                    if value is not None:
                        return value
        return None

    stock = find(
        ("stock_ratio", "stock_share", "stock_like_ratio", "stock_like",
         "stock_pct", "stock_percent"),
        ratio=True,
    )
    ai = find(
        ("ai_ratio", "ai_share", "ai_generated_ratio", "ai_generated_like",
         "ai_like", "ai_pct", "ai_percent"),
        ratio=True,
    )
    avg = find(
        ("avg_shot_len_s", "avg_shot_length_s", "average_shot_length_s",
         "avg_shot_len", "average_shot_len_s")
    )
    cuts = find(("cuts_per_min", "cuts_per_minute"))
    target_duration = find(
        ("target_duration_s", "target_duration", "duration_s", "target_s")
    )
    if avg is None and cuts:
        avg = 60.0 / cuts
    if cuts is None and avg:
        cuts = 60.0 / avg
    return {
        "stock_ratio": stock,
        "ai_ratio": ai,
        "avg_shot_len_s": avg if avg and avg > 0 else None,
        "cuts_per_min": cuts if cuts and cuts > 0 else None,
        "target_duration_s": (
            target_duration if target_duration and target_duration > 0 else None
        ),
    }


def _directives_have_signal(directives: dict[str, Any]) -> bool:
    return any(value is not None for value in directives.values())


def _load_profile_file(path: Any) -> Optional[Any]:
    """Load a raw-JSON style profile file; None on any failure."""
    try:
        candidate = Path(str(path)).expanduser()
        if not candidate.is_file():
            return None
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _style_spec_candidate(ctx: Any, bundle: Any, extra: dict) -> Any:
    """The spec object a resolver should see (ctx.spec wins)."""
    for source in (extra, ctx, bundle):
        spec = _get(source, "spec") if source is not None else None
        if spec is not None:
            return spec
    # No explicit spec: build a duck-typed proxy from whatever explicit
    # style fields the callers DO carry, so the resolver's documented
    # order (spec.style_profile_path first) still applies.
    proxy: dict[str, Any] = {}
    for source in (ctx, bundle):
        for key in ("style_profile_path", "channel_id"):
            value = _get(source, key) if source is not None else None
            if value is not None and key not in proxy:
                proxy[key] = value
    return SimpleNamespace(**proxy) if proxy else None


def _base_dir_candidates(
    ctx: Any, bundle: Any, visuals_dir: Path, extra: dict, spec: Any
) -> list[Path]:
    """Ordered base-dir candidates for the channel-scoped lookup."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        try:
            path = Path(str(value)).expanduser()
        except Exception:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)

    for source in (extra, ctx, spec, bundle):
        if source is not None:
            add(_get(source, "base_dir", "projects_base", "work_base_dir"))
    root = _get(ctx, "root", "project_root", "project_dir")
    if root is not None:
        add(Path(str(root)).parent)
        add(root)
    # visuals_dir is <base>/<project>/visuals in the StageContext layout.
    add(visuals_dir.parent.parent)
    add(visuals_dir.parent)
    return out


def _fallback_resolve_style_profile(
    spec: Any, base_candidates: Sequence[Path], ctx: Any, bundle: Any,
    extra: dict,
) -> Optional[Any]:
    """Contract-order resolver used when W1's module has not landed
    (or cannot parse the file): spec.style_profile_path ->
    <base_dir>/style_profiles/<channel_id>.json -> env
    ``YTA_STYLE_PROFILE_PATH``.  Raw JSON only; never raises."""
    # 1. Explicit path on the spec (then ctx / bundle / call extras).
    explicit = None
    for source in (spec, extra, ctx, bundle):
        if source is not None:
            explicit = _get(source, "style_profile_path", "style_profile_file")
            if explicit:
                break
    if explicit:
        profile = _load_profile_file(explicit)
        if profile is not None:
            return profile
        for base in base_candidates:
            profile = _load_profile_file(base / str(explicit))
            if profile is not None:
                return profile
    # 2. Channel-scoped file under each base candidate.
    channel_id = None
    for source in (spec, ctx, bundle, extra):
        if source is not None:
            channel_id = _get(source, "channel_id")
            if channel_id:
                break
    if channel_id:
        channel = str(channel_id).strip()
        # A channel id is a single path component, never a path.
        if channel and "/" not in channel and "\\" not in channel:
            for base in base_candidates:
                profile = _load_profile_file(
                    base / "style_profiles" / f"{channel}.json"
                )
                if profile is not None:
                    return profile
    # 3. Environment override.
    env_path = os.environ.get("YTA_STYLE_PROFILE_PATH")
    if env_path:
        return _load_profile_file(env_path)
    return None


def _resolve_style_for_visuals(
    *, ctx: Any, bundle: Any, profile_arg: Any, visuals_dir: Path,
    extra: dict,
) -> tuple[Optional[Any], dict[str, Optional[float]], bool]:
    """(profile, normalised_directives, present) for this run.

    Resolution is fully defensive: any failure anywhere degrades to
    ``(None, {}, False)`` — the exact pre-Style-Lab behaviour — rather
    than breaking the stage.  Order: direct injection (call / ctx /
    module global) -> W1 ``style_profile`` resolver -> local fallback
    resolver (contract order).  Directives come from W1's
    ``style_directives`` when usable, else local extraction.
    """
    empty: dict[str, Optional[float]] = {
        "stock_ratio": None, "ai_ratio": None, "avg_shot_len_s": None,
        "cuts_per_min": None, "target_duration_s": None,
    }
    try:
        # -- direct injection -------------------------------------------
        injected: Any = None
        for source in (extra, ctx, bundle):
            if source is not None:
                injected = _get(
                    source, "style_profile", "competitor_style_profile",
                    "style_directives",
                )
                if injected is not None:
                    break
        if injected is None:
            injected = globals().get("style_profile")
        if isinstance(injected, (str, Path)):
            injected = _load_profile_file(injected)
        if isinstance(injected, dict) and _directives_have_signal(
            _normalise_directives(injected)
        ) and not any(
            key in injected
            for key in ("visual_mix", "scenes", "channel_id", "profile_id")
        ):
            # A bare directives dict was handed in directly.
            return None, _normalise_directives(injected), True
        if injected is not None:
            directives = _normalise_directives(injected)
            return injected, directives, True

        spec = _style_spec_candidate(ctx, bundle, extra)
        base_candidates = _base_dir_candidates(
            ctx, bundle, visuals_dir, extra, spec
        )

        # -- W1 resolver -------------------------------------------------
        profile_obj: Any = None
        module = _load_style_profile_module()
        resolver = globals().get("resolve_style_profile")
        if not callable(resolver):
            resolver = (
                getattr(module, "resolve_style_profile", None)
                if module is not None else None
            )
        if callable(resolver) and spec is not None:
            for base in base_candidates or [Path.cwd()]:
                for attempt in (
                    lambda b=base: resolver(spec, b),
                    lambda b=base: resolver(spec, base_dir=b),
                    lambda: resolver(spec),
                ):
                    try:
                        candidate = attempt()
                    except TypeError:
                        continue
                    except Exception:
                        candidate = None
                        break
                    if candidate is not None:
                        profile_obj = candidate
                        break
                if profile_obj is not None:
                    break

        # -- local fallback resolver --------------------------------------
        if profile_obj is None:
            profile_obj = _fallback_resolve_style_profile(
                spec, base_candidates, ctx, bundle, extra
            )
        if profile_obj is None:
            return None, empty, False

        # -- directives: W1's function first, local extraction fills -----
        directives: dict[str, Optional[float]] = dict(empty)  # type: ignore[assignment]
        directives_fn = globals().get("style_directives")
        if not callable(directives_fn):
            directives_fn = (
                getattr(module, "style_directives", None)
                if module is not None else None
            )
        if callable(directives_fn):
            try:
                raw = directives_fn(profile_obj)
                if raw is not None:
                    from_w1 = _normalise_directives(raw)
                    if _directives_have_signal(from_w1):
                        directives = from_w1
            except Exception:
                pass
        if not _directives_have_signal(directives):
            directives = _normalise_directives(profile_obj)
        else:
            local = _normalise_directives(profile_obj)
            for key, value in local.items():
                if directives.get(key) is None and value is not None:
                    directives[key] = value
        return profile_obj, directives, True
    except Exception:
        return None, empty, False


# -- shot-length re-segmentation -------------------------------------------


def _split_sentence_words(text: str, n_chunks: int) -> list[str]:
    """Split one sentence's words into ``n_chunks`` near-equal chunks."""
    words = text.split()
    if n_chunks <= 1 or len(words) <= 1:
        return [text]
    n_chunks = min(n_chunks, len(words))
    base, remainder = divmod(len(words), n_chunks)
    chunks: list[str] = []
    index = 0
    for chunk_idx in range(n_chunks):
        size = base + (1 if chunk_idx < remainder else 0)
        chunks.append(" ".join(words[index:index + size]))
        index += size
    return [chunk for chunk in chunks if chunk]


def _merge_sentence_groups(sentences: list[str], desired: int) -> list[str]:
    """Join consecutive sentences into ``desired`` near-equal groups."""
    if desired >= len(sentences):
        return list(sentences)
    base, remainder = divmod(len(sentences), desired)
    out: list[str] = []
    index = 0
    for group_idx in range(desired):
        size = base + (1 if group_idx < remainder else 0)
        out.append(" ".join(sentences[index:index + size]))
        index += size
    return out


def _resegment_to_count(sentences: list[str], desired: int) -> list[str]:
    """Re-segment one chapter to exactly ``desired`` scenes (or as
    close as its word count allows), preserving every word."""
    if not sentences or desired <= 0:
        return list(sentences)
    if desired == len(sentences):
        return list(sentences)
    if desired < len(sentences):
        return _merge_sentence_groups(sentences, desired)
    # Splitting: one chunk per sentence first, then extra chunks go to
    # the sentences with the most words per current chunk (capped at
    # one chunk per word — a scene needs at least one word).
    splits = [1] * len(sentences)
    word_counts = [max(1, len(s.split())) for s in sentences]
    remaining = desired - len(sentences)
    while remaining > 0:
        best_idx, best_ratio = -1, -1.0
        for idx, count in enumerate(word_counts):
            if splits[idx] < count:
                ratio = count / splits[idx]
                if ratio > best_ratio:
                    best_idx, best_ratio = idx, ratio
        if best_idx < 0:
            break
        splits[best_idx] += 1
        remaining -= 1
    out: list[str] = []
    for sentence, count in zip(sentences, splits):
        out.extend(_split_sentence_words(sentence, count))
    return out


def _resegment_chapters_for_shot_length(
    chapter_sentences: list[list[str]],
    chapter_totals: Sequence[float],
    total_s: float,
    target_avg_s: float,
) -> list[list[str]]:
    """Re-plan scene counts so the average shot length approaches
    ``target_avg_s`` WITHOUT touching the measured total: counts are
    allocated per chapter proportional to its measured duration (at
    least one scene per non-empty chapter, at most one per word), the
    global count is ``round(total / target)``, and durations are later
    redistributed inside the same measured chapter totals by the
    existing word-weighted distributor — no time is fabricated."""
    if target_avg_s <= 0 or total_s <= 0:
        return chapter_sentences
    nonempty = [i for i, s in enumerate(chapter_sentences) if s]
    if not nonempty:
        return chapter_sentences
    total_words = sum(
        max(1, len(sentence.split()))
        for sentences in chapter_sentences for sentence in sentences
    )
    desired_global = int(round(total_s / target_avg_s))
    desired_global = max(len(nonempty), min(desired_global, total_words))
    allocation = {i: 1 for i in nonempty}
    remaining = desired_global - len(nonempty)
    ideals = {
        i: (chapter_totals[i] / target_avg_s if i < len(chapter_totals) else 1.0)
        for i in nonempty
    }
    chapter_words = {
        i: sum(max(1, len(s.split())) for s in chapter_sentences[i])
        for i in nonempty
    }
    while remaining > 0:
        best_idx, best_deficit = -1, None
        for i in nonempty:
            if allocation[i] < chapter_words[i]:
                deficit = ideals[i] - allocation[i]
                if best_deficit is None or deficit > best_deficit:
                    best_idx, best_deficit = i, deficit
        if best_idx < 0:
            break
        allocation[best_idx] += 1
        remaining -= 1
    return [
        _resegment_to_count(sentences, allocation.get(idx, len(sentences)))
        if sentences else sentences
        for idx, sentences in enumerate(chapter_sentences)
    ]


# -- stock:AI mix planning ---------------------------------------------------


def _evenly_spaced_positions(candidates: list[int], k: int) -> set[int]:
    """``k`` positions spread evenly across ``candidates`` (Bresenham)."""
    if k <= 0 or not candidates:
        return set()
    if k >= len(candidates):
        return set(candidates)
    picks = {
        candidates[min(len(candidates) - 1, int((i + 0.5) * len(candidates) / k))]
        for i in range(k)
    }
    # Bresenham picks are distinct for k <= len; top up defensively.
    for candidate in candidates:
        if len(picks) >= k:
            break
        picks.add(candidate)
    return picks


def _plan_visual_mix(
    n_scenes: int, directives: dict[str, Optional[float]]
) -> tuple[Optional[list[str]], int, int, Optional[float], Optional[float]]:
    """(planned_classes, planned_stock, planned_ai, stock_target,
    ai_target).  ``planned_classes`` is None when the profile carries
    no ratio signal at all (no mix planning — stock-first default).

    Classes are ``"stock"`` / ``"ai"`` (plus ``"plate"`` for the
    remainder when stock+ai ratios deliberately sum to < 1).  Counts
    approximate the target ratios to within rounding (±1 scene);
    positions are spread evenly through the video, never clustered.
    """
    if n_scenes <= 0:
        return None, 0, 0, None, None
    stock_t = directives.get("stock_ratio")
    ai_t = directives.get("ai_ratio")
    if stock_t is None and ai_t is None:
        return None, 0, 0, None, None
    if stock_t is None:
        stock_t = max(0.0, 1.0 - float(ai_t))  # type: ignore[arg-type]
        planned_stock = int(round(stock_t * n_scenes))
        planned_ai = n_scenes - planned_stock
        remainder = 0
    elif ai_t is None:
        ai_t = max(0.0, 1.0 - float(stock_t))
        planned_stock = int(round(float(stock_t) * n_scenes))
        planned_ai = n_scenes - planned_stock
        remainder = 0
    else:
        planned_stock = int(round(float(stock_t) * n_scenes))
        planned_ai = int(round(float(ai_t) * n_scenes))
        remainder = n_scenes - planned_stock - planned_ai
        if remainder < 0:
            # Rounding pushed the sum over: trim AI first, then stock.
            excess = -remainder
            trim = min(planned_ai, excess)
            planned_ai -= trim
            planned_stock -= (excess - trim)
            remainder = 0
        elif remainder > 0 and abs((float(stock_t) + float(ai_t)) - 1.0) <= 0.06:
            # Ratios (nearly) exhaust the mix: rounding remainder is AI.
            planned_ai += remainder
            remainder = 0
    classes: list[str] = ["plate"] * n_scenes
    stock_positions = _evenly_spaced_positions(
        list(range(n_scenes)), planned_stock
    )
    for pos in stock_positions:
        classes[pos] = "stock"
    remaining_positions = [i for i in range(n_scenes) if classes[i] != "stock"]
    ai_positions = _evenly_spaced_positions(remaining_positions, planned_ai)
    for pos in ai_positions:
        classes[pos] = "ai"
    return classes, planned_stock, planned_ai, stock_t, ai_t


# ---------------------------------------------------------------------------
# Main stage entry point
# ---------------------------------------------------------------------------


def plan_and_generate_visuals(
    bundle: Any,
    voice: Any,
    profile: Any,
    ctx: Any,
    stock_fetcher: Any = None,
    fetcher: Any = None,
    use_stock: Optional[bool] = None,
    **stock_kwargs: Any,
) -> VisualsResult:
    """Plan sentence-level scenes and source a visual for each.

    ``bundle`` = content.ContentBundle (chapters), ``voice`` =
    voice_stage.VoiceResult (duration_s, chapter_files), ``profile`` =
    niches.NicheProfile, ``ctx`` = integration StageContext
    (visuals_dir).  All duck-typed.

    Round 3 stock-first: when stock mode is ON — a fetcher injected
    via ``stock_fetcher`` / ``fetcher`` / ``ctx`` / spec, or
    ``PIXABAY_API_KEY`` present in the environment — each scene tries
    stock first and falls back to its local Pillow plate on any miss.
    ``use_stock=False`` (or a spec flag of the same name, or
    ``force_stock_off=True`` / ``stock_mode="off"`` anywhere on the
    spec/ctx) forces the exact Round 2 local-plate behaviour.
    """
    _visuals_dir = _get(
        ctx, "visuals_dir", "visuals_path", "output_dir", "work_dir",
        default=None,
    )
    visuals_dir = (
        Path(str(_visuals_dir))
        if _visuals_dir is not None
        else _ctx_less_tmp_dir("visuals")
    )
    visuals_dir.mkdir(parents=True, exist_ok=True)

    niche_id = _as_text(
        _get(profile, "profile_id", "niche_id", "id",
             default=_get(bundle, "niche_id", "profile_id", default="custom"))
    ) or "custom"
    visual_style = _as_text(_get(profile, "visual_style", "visual_hints", default=""))
    video_id = _as_text(
        _get(ctx, "video_id", "project_id",
             default=_get(bundle, "video_id", "project_id", "id", default="video"))
    ) or "video"

    chapters = _get(bundle, "chapters", "chapter_list", default=None)
    if not chapters:
        whole = _as_text(
            _get(bundle, "narration", "script", "text", "content", default="")
        )
        chapters = [{"narration": whole}] if whole.strip() else []
    chapters = list(chapters)
    if not chapters:
        raise ValueError("visuals stage: bundle carries no chapters/narration")

    total_s = _voice_total_s(voice)
    if total_s <= 0:
        raise ValueError("visuals stage: voice result has no positive duration_s")

    # -- 1. Sentence-level segmentation (atomic unit = sentence) ----------
    chapter_sentences: list[list[str]] = []
    for chapter in chapters:
        sentences = _chapter_sentences(chapter)
        # Merge very short fragments into the previous sentence so a
        # 1-3 word fragment does not become a scene of its own (models
        # allow sharing).  This is a segmentation choice only — short
        # scenes are legal under the measured-narration policy, so
        # nothing here pads durations up to a beat floor.
        merged: list[str] = []
        for sentence in sentences:
            if merged and len(sentence.split()) <= 3:
                merged[-1] = f"{merged[-1]} {sentence}"
            else:
                merged.append(sentence)
        chapter_sentences.append(merged)
    if not any(chapter_sentences):
        raise ValueError("visuals stage: no narration sentences found in bundle")

    # -- 2. Chapter time allocation (measured chapter audio wins) ---------
    chapter_totals = _chapter_durations_s(voice, len(chapters))
    if chapter_totals is not None:
        scale = total_s / sum(chapter_totals)
        chapter_totals = [d * scale for d in chapter_totals]
    else:
        weights = [
            max(1, sum(len(s.split()) for s in sentences))
            for sentences in chapter_sentences
        ]
        weight_sum = sum(weights)
        chapter_totals = [total_s * w / weight_sum for w in weights]

    # -- 2b. Style Lab: resolve profile + shot-length re-segmentation ----
    # When a style profile resolves, scene COUNT is re-planned so the
    # average shot length approaches the profile's target: sentences
    # are merged (fewer, longer shots) or split at word boundaries
    # (more, shorter shots) within each chapter.  Chapter totals stay
    # exactly the measured ones — durations are redistributed inside
    # them by section 4, never fabricated.  No profile -> this block
    # is a no-op and planning is byte-identical to before.
    _style_profile_obj, style_directives_map, style_present = (
        _resolve_style_for_visuals(
            ctx=ctx, bundle=bundle, profile_arg=profile,
            visuals_dir=visuals_dir, extra=stock_kwargs,
        )
    )
    style_avg_target = (
        style_directives_map.get("avg_shot_len_s") if style_present else None
    )
    if style_present and style_avg_target:
        chapter_sentences = _resegment_chapters_for_shot_length(
            chapter_sentences, chapter_totals, total_s,
            float(style_avg_target),
        )

    # -- 3. Classify scenes ------------------------------------------------
    flat: list[dict[str, Any]] = []
    for chapter_index, sentences in enumerate(chapter_sentences):
        for local_index, sentence in enumerate(sentences):
            scene_class, beat_class, list_items = _classify_sentence(
                sentence,
                is_hook=(chapter_index == 0 and local_index < 2),
            )
            flat.append(
                {
                    "chapter_index": chapter_index,
                    "text": sentence,
                    "scene_class": scene_class,
                    "beat_class": beat_class,
                    "list_item_count": list_items,
                    "words": max(1, len(sentence.split())),
                }
            )

    # HERO scenes: at most 2 (cap is MAX_HERO_SCENES=3; keep headroom) —
    # the strongest hook/peak candidate plus the strongest later scene.
    hero_candidates = [
        (idx, _intensity_score(item["text"]) + (2.0 if idx == 0 else 0.0))
        for idx, item in enumerate(flat)
        if item["scene_class"] in (SceneClass.REAL_WORLD, SceneClass.ABSTRACT_CONCEPT)
    ]
    hero_ids: set[int] = set()
    if hero_candidates and len(flat) >= 3:
        hero_candidates.sort(key=lambda pair: (-pair[1], pair[0]))
        hero_ids.add(hero_candidates[0][0])
        later = [pair for pair in hero_candidates if pair[0] >= len(flat) // 2]
        if later and len(flat) >= 6:
            hero_ids.add(later[0][0])
        hero_ids = set(sorted(hero_ids)[: MAX_HERO_SCENES - 1])
    for idx in hero_ids:
        flat[idx]["scene_class"] = SceneClass.HERO

    # -- 3b. Style Lab: stock:AI mix plan ----------------------------------
    # Per-scene PLANNED source classes approximating the profile's
    # ratios (spread evenly, never clustered).  "ai" scenes skip the
    # stock attempt and use the existing local plate/generated-still
    # path; "stock" scenes try stock first and honestly fall back to a
    # plate on a miss — planned vs actual are both recorded per scene.
    planned_classes: Optional[list[str]] = None
    planned_stock = 0
    planned_ai = 0
    stock_ratio_target: Optional[float] = None
    ai_ratio_target: Optional[float] = None
    style_applied_visuals: Optional[dict[str, Any]] = None
    if style_present:
        (
            planned_classes,
            planned_stock,
            planned_ai,
            stock_ratio_target,
            ai_ratio_target,
        ) = _plan_visual_mix(len(flat), style_directives_map)
        style_applied_visuals = {
            "stock_ratio_target": stock_ratio_target,
            "ai_ratio_target": ai_ratio_target,
            "avg_shot_len_target_s": style_avg_target,
            "scenes_planned": len(flat),
            "planned_stock": planned_stock,
            "planned_ai": planned_ai,
        }

    # -- 4. Durations: word-weighted within each chapter, measured-first --
    # Scene boundaries are laid end-to-end from the measured chapter
    # totals.  The last scene's end is whatever the distribution sums
    # to — it is NEVER force-set to the voice total (the Round 2 bug:
    # when beat-minimum clamping had inflated the cursor past the
    # total, forcing end=total made the final scene end <= start).
    cursor = 0.0
    for chapter_index, sentences in enumerate(chapter_sentences):
        items = [item for item in flat if item["chapter_index"] == chapter_index]
        if not items:
            continue
        if chapter_totals[chapter_index] <= 0:
            raise ValueError(
                f"visuals stage: chapter {chapter_index} has a "
                f"non-positive measured duration "
                f"({chapter_totals[chapter_index]}s) but "
                f"{len(items)} scene(s) to place in it; a zero-length "
                "measured segment cannot be turned into a scene"
            )
        durations = _distribute_durations(
            chapter_totals[chapter_index],
            [float(item["words"]) for item in items],
            [
                _beat_range(item["beat_class"], item["list_item_count"])
                for item in items
            ],
        )
        for item, duration in zip(items, durations):
            start_s = round(cursor, 3)
            end_s = round(cursor + duration, 3)
            if end_s <= start_s:
                # Millisecond-rounding guard ONLY: the distributed
                # duration was positive but rounds to zero at ms
                # precision.  Nudge the end by the smallest
                # representable step (1 ms) so end > start — this is
                # a rounding correction, never a duration pad, and it
                # can only trigger for sub-millisecond scenes.
                end_s = round(start_s + 0.001, 3)
            item["start_s"] = start_s
            item["end_s"] = end_s
            item["duration_s"] = round(end_s - start_s, 3)
            cursor = end_s

    # -- 5. Source decisions + safety + plates ------------------------------
    palette = _palette_for(niche_id, visual_style)
    niche_safety = NicheSafetyProfile(
        niche_id=niche_id,
        is_islamic=("islamic" in niche_id),
        taboo_terms=[str(t) for t in (_get(profile, "taboo", default=[]) or [])],
        forbid_human_figures=False,
    )
    budget = PlanBudgetState(total_scenes=len(flat))

    # -- 5b. Round 3 stock enablement --------------------------------------
    # ON when a fetcher is injected OR the env key is present (then a
    # StockFetcher is constructed from the parallel stock_fetch
    # module); a per-run spec flag can force it OFF.  When OFF (or the
    # stock layer is unusable) everything below is exactly Round 2.
    stock_setting = _resolve_stock_setting(use_stock, ctx, bundle, profile, stock_kwargs)
    stock_fetcher_resolved, stock_enabled = _resolve_stock_fetcher(
        explicit_fetcher=stock_fetcher if stock_fetcher is not None else fetcher,
        ctx=ctx,
        bundle=bundle,
        profile=profile,
        extra=stock_kwargs,
        visuals_dir=visuals_dir,
        setting=stock_setting,
    )
    #: Anti-repetition: provider asset ids already used in THIS video.
    used_stock_asset_ids: set[str] = set()
    scene_sources: list[str] = []
    stock_count = 0

    scene_manifest: list[dict[str, Any]] = []
    asset_files: dict[str, str] = {}
    ledger_rows: list[dict[str, Any]] = []
    records: list[SceneRecord] = []
    previous_motion: Optional[MotionInstruction] = None

    for idx, item in enumerate(flat):
        scene_id = f"scene_{idx + 1:04d}"
        scene_class: SceneClass = item["scene_class"]
        beat_class: BeatClass = item["beat_class"]
        text: str = item["text"]
        is_hero = scene_class is SceneClass.HERO

        features = SceneFeatures(
            scene_class=scene_class,
            is_named_subject=bool(re.search(r"\s[A-Z][a-z]{2,}", text)),
            safety_restricted=False,
            wants_ai_video=False,
        )
        decision = decide_source(features, [], budget)
        if decision.choice in (SourceKind.AI_IMAGE, SourceKind.AI_VIDEO):
            budget.ai_scenes_so_far += 1
            if decision.choice is SourceKind.AI_VIDEO:
                budget.ai_video_scenes_so_far += 1
        if scene_class is SceneClass.HERO:
            budget.hero_scenes_so_far += 1

        # Effective source: no stock keys / AI provider in this round —
        # TEXTUAL and HERO plates are text cards, everything else is a
        # programmatic graphic plate (decision.py fallback chain).
        wants_text_card = scene_class in (SceneClass.TEXTUAL, SceneClass.HERO)
        if decision.choice is SourceKind.TEXT_CARD:
            effective_source, effective_reason = SourceKind.TEXT_CARD, decision.reason_code
        elif decision.choice is SourceKind.GRAPHIC and wants_text_card:
            # Budget-degraded HERO: the roadmap fallback chain ends at a
            # stylised graphic / text card — a hero takes the text card.
            effective_source = SourceKind.TEXT_CARD
            effective_reason = ReasonCode.FALLBACK_TEXT_CARD
        elif decision.choice is SourceKind.GRAPHIC:
            effective_source, effective_reason = SourceKind.GRAPHIC, decision.reason_code
        elif wants_text_card:
            effective_source = SourceKind.TEXT_CARD
            effective_reason = ReasonCode.FALLBACK_TEXT_CARD
        else:
            effective_source = SourceKind.GRAPHIC
            effective_reason = ReasonCode.FALLBACK_GRAPHIC

        # Key phrase for the card — never render Arabic-script text with
        # a font that cannot shape it: graphic-only plate + note instead.
        notes: list[str] = []
        phrase: Optional[str] = None
        overlay_text: Optional[str] = None
        if effective_source is SourceKind.TEXT_CARD or scene_class is SceneClass.DATA:
            candidate = _key_phrase(text, scene_class)
            if candidate and _has_arabic_script(candidate):
                notes.append(
                    "urdu_script_not_rendered: key phrase is Arabic-script; "
                    "DejaVu cannot shape it, graphic-only plate generated"
                )
            elif candidate:
                phrase = candidate
                overlay_text = candidate

        # Faceless-safety pass (plates depict no figures by construction;
        # the gate still runs and can force a graphic-only SAFETY_ROUTE).
        safety = validate_scene_safety(
            SceneSafetyFacts(
                scene_id=scene_id,
                scene_class=scene_class,
                chosen_source=effective_source,
                visual_prompt=(
                    f"local Pillow plate: gradient + geometric shapes, "
                    f"{scene_class.value} scene"
                ),
                narration_text=text,
            ),
            niche_safety,
        )
        safety_flags = safety.flags
        if safety.blocked:
            phrase = None
            overlay_text = None
            effective_source = SourceKind.GRAPHIC
            effective_reason = ReasonCode.SAFETY_ROUTE
            notes.append("safety_route: blocked by faceless-safety gate")

        motion = suggest_next_motion(previous_motion)
        if motion.kind in (MotionKind.ZOOM_IN, MotionKind.PARALLAX):
            motion = MotionInstruction(
                kind=motion.kind, start_scale=1.0, end_scale=1.08
            )
        elif motion.kind is MotionKind.ZOOM_OUT:
            motion = MotionInstruction(
                kind=motion.kind, start_scale=1.08, end_scale=1.0
            )
        previous_motion = motion

        # -- Round 3 stock-first attempt (never for safety-blocked
        #    scenes; any miss / error / duplicate falls back to the
        #    local plate below — never a placeholder or blank) --------
        #    Style Lab: a scene PLANNED as "ai" (or "plate") never
        #    attempts stock — it goes straight to the local
        #    plate/generated-still path.  No plan (no profile) keeps
        #    the exact stock-first-for-every-scene behaviour.
        planned_class: Optional[str] = (
            planned_classes[idx]
            if planned_classes is not None and idx < len(planned_classes)
            else None
        )
        if planned_class is not None and planned_class != "stock":
            notes.append(
                f"style_planned_{planned_class}: local plate/generated "
                "still per style profile mix; stock not attempted"
            )
        stock_info: Optional[dict[str, Any]] = None
        stock_queries: list[str] = []
        if (
            stock_enabled and stock_fetcher_resolved is not None
            and not safety.blocked
            and (planned_class is None or planned_class == "stock")
        ):
            view = _SceneQueryView(
                scene_id=scene_id,
                text=text,
                sentence=text,
                narration_text=text,
                source_sentence=text,
                scene_class=scene_class.value,
                beat_class=beat_class.value,
                duration_s=float(item["duration_s"]),
                duration=float(item["duration_s"]),
                start_s=item["start_s"],
                end_s=item["end_s"],
                chapter_index=item["chapter_index"],
                niche_id=niche_id,
                profile_id=niche_id,
                visual_style=visual_style,
                video_id=video_id,
            )
            stock_queries = _queries_for_scene(
                view, profile, niche_id=niche_id, text=text
            )
            view["queries"] = stock_queries
            view["query"] = stock_queries[0] if stock_queries else ""
            asset: Any = None
            fetch_error: Optional[str] = None
            try:
                asset = _fetch_stock_asset(
                    stock_fetcher_resolved,
                    float(item["duration_s"]),
                    list(stock_queries),
                    set(used_stock_asset_ids),
                )
            except Exception as exc:  # per-scene fallback; type only —
                fetch_error = type(exc).__name__  # never the message
                asset = None  # (it could carry a URL; stay leak-safe)
            if fetch_error is not None:
                notes.append(
                    f"stock_fallback: fetcher raised {fetch_error}; "
                    "local plate generated"
                )
            elif asset is None:
                notes.append("stock_fallback: no stock asset; local plate generated")
            else:
                raw_pid = _asset_field(
                    asset, "provider_asset_id", "asset_id", "provider_id", "id"
                )
                pid = str(raw_pid).strip() if raw_pid is not None else ""
                raw_path = _asset_field(
                    asset, "local_path", "path", "file_path", "filepath",
                    "asset_path", "local_file_path",
                )
                local_path = str(raw_path).strip() if raw_path is not None else ""
                if not pid:
                    notes.append(
                        "stock_fallback: asset carried no provider_asset_id; "
                        "local plate generated"
                    )
                elif pid in used_stock_asset_ids:
                    # Anti-repetition is enforced here too: even a
                    # fetcher that ignores exclude_asset_ids can never
                    # make one asset serve two scenes in one video.
                    notes.append(
                        "stock_fallback: provider asset already used in "
                        "this video (anti-repetition); local plate generated"
                    )
                elif not local_path:
                    notes.append(
                        "stock_fallback: asset carried no local_path; "
                        "local plate generated"
                    )
                else:
                    provider = str(
                        _asset_field(asset, "provider", "provider_name",
                                     "source_provider")
                        or "pixabay"
                    ).strip() or "pixabay"
                    kind_raw = _asset_field(
                        asset, "kind", "media_type", "media_kind", "asset_kind"
                    )
                    kind_value = str(
                        getattr(kind_raw, "value", kind_raw) or ""
                    ).lower()
                    suffix = Path(local_path).suffix.lower()
                    is_video = (
                        "video" in kind_value
                        or suffix in _VIDEO_SUFFIXES
                    )
                    asset_sha = _valid_sha256(
                        _asset_field(asset, "file_sha256", "sha256",
                                     "content_hash", "file_hash")
                    ) or _file_sha256(Path(local_path))
                    used_stock_asset_ids.add(pid)
                    stock_info = {
                        "asset": asset,
                        "view": view,
                        "provider": provider,
                        "provider_asset_id": pid,
                        "local_path": local_path,
                        "source_str": f"stock:{provider}:{pid}",
                        "kind": "video" if is_video else "photo",
                        "is_video": is_video,
                        "content_hash": asset_sha,
                        "width": _asset_field(asset, "width", "width_px"),
                        "height": _asset_field(asset, "height", "height_px"),
                        "duration_s": _asset_field(asset, "duration_s", "duration"),
                    }

        if stock_info is not None:
            # ---- stock scene: the downloaded asset IS the visual ----
            # Video clips are handed to the render path as files (it
            # trims/loops them to the scene duration); photos flow
            # through exactly like plates (still + planned motion).
            stock_count += 1
            source_str = stock_info["source_str"]
            asset_path = Path(stock_info["local_path"])
            content_hash = stock_info["content_hash"]
            ledger_row = _stock_ledger_row(
                stock_info["asset"],
                stock_info["view"],
                scene_id=scene_id,
                source_str=source_str,
                provider=stock_info["provider"],
                provider_asset_id=stock_info["provider_asset_id"],
                local_path=str(asset_path),
                content_hash=content_hash,
                kind=stock_info["kind"],
            )
            licence_ref = str(
                ledger_row.get("licence_ref") or ledger_row.get("licence_id")
                or f"lic-{scene_id}-{stock_info['provider_asset_id']}"
            )
            if not Path(stock_info["local_path"]).is_file():
                notes.append(
                    "stock_asset_file_not_on_disk: fetcher-supplied path "
                    "kept as the scene asset reference"
                )
            record = {
                "scene_id": scene_id,
                "chapter_index": item["chapter_index"],
                "sentence_ids": [f"c{item['chapter_index']}s{idx}"],
                "text": text,
                "start_s": item["start_s"],
                "end_s": item["end_s"],
                "duration_s": item["duration_s"],
                "beat_class": beat_class.value,
                "scene_class": scene_class.value,
                "asset_path": str(asset_path),
                "local_path": str(asset_path),
                "source": source_str,
                "provenance": source_str,
                "provider": stock_info["provider"],
                "provider_asset_id": stock_info["provider_asset_id"],
                "asset_id": stock_info["provider_asset_id"],
                "asset_page_url": ledger_row.get("asset_page_url", ""),
                "download_url": ledger_row.get("download_url", ""),
                "terms_hash": ledger_row.get("terms_hash", ""),
                "file_sha256": ledger_row.get("file_sha256", ""),
                "generation": "stock_fetch",
                "chosen_source": SourceKind.STOCK.value,
                "decision_source": decision.choice.value,
                "decision_reason": decision.reason_code.value,
                "source_decision": ReasonCode.STOCK_MATCH.value,
                "decision_detail": (
                    f"stock-first hit via {stock_info['provider']} "
                    f"(queries: {stock_queries[:3]})"
                ),
                "stock_query": stock_queries[0] if stock_queries else None,
                "stock_queries": stock_queries,
                "motion": {
                    "kind": motion.kind.value,
                    "start_scale": motion.start_scale,
                    "end_scale": motion.end_scale,
                },
                "overlay_text": overlay_text,
                "text_rendered": False,
                "is_still": not stock_info["is_video"],
                "is_video": stock_info["is_video"],
                "kind": stock_info["kind"],
                "width_px": stock_info["width"] or WIDTH,
                "height_px": stock_info["height"] or HEIGHT,
                "asset_duration_s": stock_info["duration_s"],
                "licence_ref": licence_ref,
                "licence_name": ledger_row.get("licence_name", ""),
                "content_hash": content_hash,
                "safety_flags": safety_flags,
                "notes": notes,
                "status": SceneStatus.READY.value,
            }
            if planned_class is not None:
                record["planned_class"] = planned_class
                record["planned_source"] = planned_class
                record["actual_class"] = "stock"
                record["actual_source"] = source_str
            scene_manifest.append(record)
            asset_files[scene_id] = str(asset_path)
            ledger_rows.append(ledger_row)
            records.append(
                SceneRecord(
                    scene_id=scene_id,
                    source_sentence=text,
                    sentence_ids=record["sentence_ids"],
                    start_s=item["start_s"],
                    end_s=item["end_s"],
                    beat_class=beat_class,
                    scene_class=scene_class,
                    chosen_source=SourceKind.STOCK,
                    decision_reason=ReasonCode.STOCK_MATCH,
                    asset_ref=str(asset_path),
                    licence_ref=licence_ref,
                    content_hash=content_hash,
                    motion=motion,
                    overlay_text=overlay_text,
                    is_still=not stock_info["is_video"],
                    stock_query=stock_queries[0] if stock_queries else None,
                    list_item_count=item["list_item_count"],
                    safety_flags=safety_flags,
                    status=SceneStatus.READY,
                )
            )
            scene_sources.append(source_str)
            continue

        # ---- local plate (Round 2 path; also the stock fallback) ----
        asset_path = visuals_dir / f"{scene_id}.png"
        _render_plate(
            asset_path,
            palette=palette,
            seed=_hash_int(niche_id, video_id, scene_id),
            phrase=phrase,
            scene_class=scene_class,
            hero=is_hero,
        )
        content_hash = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        licence_ref = f"lic-{scene_id}"
        source_str = "plate:local"

        record = {
            "scene_id": scene_id,
            "chapter_index": item["chapter_index"],
            "sentence_ids": [f"c{item['chapter_index']}s{idx}"],
            "text": text,
            "start_s": item["start_s"],
            "end_s": item["end_s"],
            "duration_s": item["duration_s"],
            "beat_class": beat_class.value,
            "scene_class": scene_class.value,
            "asset_path": str(asset_path),
            "source": source_str,
            "provenance": source_str,
            "provider": "local_plate",
            "generation": "pillow_local_plate",
            "chosen_source": effective_source.value,
            "decision_source": decision.choice.value,
            "decision_reason": decision.reason_code.value,
            "source_decision": effective_reason.value,
            "decision_detail": decision.detail,
            "motion": {
                "kind": motion.kind.value,
                "start_scale": motion.start_scale,
                "end_scale": motion.end_scale,
            },
            "overlay_text": overlay_text,
            "text_rendered": phrase is not None,
            "is_still": True,
            "licence_ref": licence_ref,
            "content_hash": content_hash,
            "safety_flags": safety_flags,
            "notes": notes,
            "status": SceneStatus.READY.value,
        }
        if planned_class is not None:
            record["planned_class"] = planned_class
            record["planned_source"] = planned_class
            record["actual_class"] = "plate"
            record["actual_source"] = source_str
        scene_manifest.append(record)
        scene_sources.append(source_str)
        asset_files[scene_id] = str(asset_path)
        ledger_rows.append(
            {
                "licence_ref": licence_ref,
                "scene_id": scene_id,
                "asset_id": scene_id,
                "asset_path": str(asset_path),
                "kind": LEDGER_KIND,
                "source": LEDGER_SOURCE,
                "licence": LEDGER_LICENCE,
                "licence_id": licence_ref,
                "content_hash": content_hash,
                "width_px": WIDTH,
                "height_px": HEIGHT,
            }
        )
        records.append(
            SceneRecord(
                scene_id=scene_id,
                source_sentence=text,
                sentence_ids=record["sentence_ids"],
                start_s=item["start_s"],
                end_s=item["end_s"],
                beat_class=beat_class,
                scene_class=scene_class,
                chosen_source=effective_source,
                decision_reason=effective_reason,
                asset_ref=str(asset_path),
                licence_ref=licence_ref,
                content_hash=content_hash,
                motion=motion,
                overlay_text=overlay_text,
                is_still=True,
                list_item_count=item["list_item_count"],
                safety_flags=safety_flags,
                status=SceneStatus.READY,
            )
        )

    # -- 6. Aggregate validation (flags only; never blocks the stage) -------
    # measured_narration=True: every record's timings derive from the
    # measured voice durations, so sub-beat-minimum scenes are legal
    # here and must not be flagged as violations (Round 3 policy).
    validation_summary: dict[str, Any] = {}
    try:
        report = validate_scene_plan(
            records, niche=niche_safety, measured_narration=True
        )
        validation_summary = {
            "beat_violations": [v.model_dump() for v in report.beat_violations],
            "motion_violations": [v.model_dump() for v in report.motion_violations],
            "budget_violations": report.budget_violations,
            "blocked_scene_ids": report.blocked_scene_ids,
            "is_valid": report.is_valid,
        }
    except Exception as exc:  # validation is advisory in this round
        validation_summary = {"error": f"{type(exc).__name__}: {exc}"}

    total_scenes = len(scene_manifest)
    plate_count = total_scenes - stock_count
    stock_share = round(stock_count / total_scenes, 4) if total_scenes else 0.0
    if stock_count:
        generator = "visuals_stage stock-first + local Pillow plate fallback"
    elif stock_enabled:
        generator = "visuals_stage stock-first attempted, local Pillow plates"
    else:
        generator = "visuals_stage local Pillow plates"
    manifest_doc = {
        "video_id": video_id,
        "niche_id": niche_id,
        "total_duration_s": round(total_s, 3),
        "scene_count": total_scenes,
        "generator": generator,
        "stock_enabled": stock_enabled,
        "stock_mode": "stock-first" if stock_enabled else "local-plates",
        "scene_sources": list(scene_sources),
        "sources": list(scene_sources),
        "stock_share": stock_share,
        "stock_count": stock_count,
        "plate_count": plate_count,
        "counts": {
            "stock": stock_count,
            "plate": plate_count,
            "total": total_scenes,
        },
        "used_stock_asset_ids": sorted(used_stock_asset_ids),
        "validation": validation_summary,
        "scenes": scene_manifest,
    }
    if style_applied_visuals is not None:
        manifest_doc["style_applied"] = {"visuals": dict(style_applied_visuals)}
    # SL-006: surface the stock brightness selection filter's run
    # stats (read from the fetcher that actually chose candidates).
    brightness_stats: dict[str, Any] = {}
    _bs = getattr(stock_fetcher_resolved, "brightness_stats", None)
    if isinstance(_bs, dict) and _bs:
        brightness_stats = dict(_bs)
    else:
        _bf = getattr(stock_fetcher_resolved, "brightness_filter", None)
        _stats_fn = getattr(_bf, "stats", None)
        if callable(_stats_fn):
            try:
                _s = _stats_fn()
                if isinstance(_s, dict) and _s:
                    brightness_stats = dict(_s)
            except Exception:
                brightness_stats = {}
    if brightness_stats:
        manifest_doc["brightness_filter"] = brightness_stats
    manifest_path = visuals_dir / "scene_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return VisualsResult(
        scene_manifest=scene_manifest,
        asset_files=asset_files,
        ledger_rows=ledger_rows,
        total_duration_s=float(round(total_s, 3)),
        manifest_path=str(manifest_path),
        scene_sources=list(scene_sources),
        stock_share=stock_share,
        stock_count=stock_count,
        plate_count=plate_count,
        stock_enabled=stock_enabled,
        brightness_stats=brightness_stats,
        style_applied=(
            {"visuals": dict(style_applied_visuals)}
            if style_applied_visuals is not None else {}
        ),
    )
