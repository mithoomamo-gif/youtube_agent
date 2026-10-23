"""Mocked-stock test suite for the Round-3 stock-first visuals mode.

MISSION (Round 3, visuals stage): ``plan_and_generate_visuals`` gains a
stock-first mode —

* stock mode is enabled when a fetcher is *injected* (or when a
  ``PIXABAY_API_KEY`` env var is present — the env-only path is NOT
  exercised here because these tests are zero-network by contract);
* the fetcher follows the ``app.integration.stock_fetch`` contract::

      fetch_for_scene(scene_duration_s, queries, exclude_asset_ids)
          -> StockSceneAsset | None

* every scene records its source (stock asset id, or local plate);
* a stock miss / fetcher error falls back to a local Pillow plate —
  no scene is ever left without a visual;
* an asset id is never used for two scenes (anti-repetition): the
  fetcher is told what to exclude, and a re-offered id is rejected in
  favour of the alternate asset or a plate;
* the licence ledger covers 100% of scenes: stock scenes carry
  provider + licence + content SHA, plate scenes carry generated rows;
* the stage result exposes ``stock_share == stock_scenes/total_scenes``;
* with stock disabled the output is structurally the Round-2 plate
  output.

CREDENTIAL RULES: no key is ever written or printed here; every test
is fully mocked (``FakeFetcher`` below) — zero live network.  Asset
stand-ins are tiny real PNG files created under a workspace-relative
temp dir (NOT ``pytest tmp_path`` / ``/tmp``).

INJECTION ADAPTATION: the stage is called through ``_run_visuals``,
which inspects the live signature and injects the fake by whichever
documented route the implementation offers (keyword parameter named
``stock_fetcher``/``fetcher``/``stock_fetch``/``stock_provider``, a
fifth positional parameter, or ``ctx.stock_fetcher``/``ctx.fetcher``).
If NO route exists the stock tests fail with a "missing
implementation" message rather than passing vacuously.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from PIL import Image

from app.integration import visuals_stage
from app.integration.visuals_stage import plan_and_generate_visuals

# ---------------------------------------------------------------------------
# Optional real contract types (used when the parallel implementation lands)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on parallel worker landing order
    from app.integration.stock_fetch import (  # type: ignore[attr-defined]
        StockSceneAsset as _RealStockSceneAsset,
    )
except Exception:  # module not landed yet / different export names
    _RealStockSceneAsset = None


# ---------------------------------------------------------------------------
# Workspace-relative scratch dirs (never /tmp, never pytest tmp_path)
# ---------------------------------------------------------------------------

_SCRATCH_ROOT = Path(__file__).resolve().parent / ".tmp_stock_visuals"


@pytest.fixture()
def workdir():
    """A per-test scratch dir inside the workspace, cleaned up after."""
    path = _SCRATCH_ROOT / uuid.uuid4().hex[:12]
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            _SCRATCH_ROOT.rmdir()  # only succeeds when empty
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Universal fake stock asset
# ---------------------------------------------------------------------------

_STOCK_LICENCE = "Pixabay Content License"
_STOCK_PROVIDER = "pixabay"


class FakeStockAsset:
    """A downloaded-asset stand-in carrying every plausible field name.

    The parallel implementation may read the asset as an object
    (attribute access), a mapping, or via ``model_dump()``/``to_dict()``
    — all three are supported, and every alias the codebase already
    uses (see ``providers/stock/models.py`` DownloadedAsset and
    ``integration/produce.py`` ledger normalisation) is populated.
    """

    def __init__(self, asset_id: str, local_path: Path, sha256: str):
        self.asset_id = asset_id
        self.provider_asset_id = asset_id
        self.candidate_id = asset_id
        self.id = asset_id
        self.provider = _STOCK_PROVIDER
        self.provider_name = _STOCK_PROVIDER
        self.source = _STOCK_PROVIDER
        self.licence = _STOCK_LICENCE
        self.license = _STOCK_LICENCE
        self.licence_name = _STOCK_LICENCE
        self.license_name = _STOCK_LICENCE
        self.licence_id = f"lic-{asset_id}"
        self.license_id = f"lic-{asset_id}"
        self.content_hash = sha256
        self.file_sha256 = sha256
        self.sha256 = sha256
        self.local_path = str(local_path)
        self.asset_path = str(local_path)
        self.path = str(local_path)
        self.file_path = str(local_path)
        self.downloaded_path = str(local_path)
        self.page_url = f"https://pixabay.com/videos/id-{asset_id}/"
        self.source_url = self.page_url
        self.download_url = self.page_url
        self.width = 1920
        self.height = 1080
        self.width_px = 1920
        self.height_px = 1080
        self.duration_s = 30.0
        self.kind = "video"
        self.media_type = "video"
        self.is_video = True
        self.is_still = False
        self.title = f"fake stock asset {asset_id}"
        self.author_name = "Fake Stock Author"

    # -- mapping / dump interfaces --------------------------------------
    def _as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    def __getitem__(self, key: str) -> Any:
        return self._as_dict()[key]

    def __contains__(self, key: str) -> bool:
        return key in self._as_dict()

    def get(self, key: str, default: Any = None) -> Any:
        return self._as_dict().get(key, default)

    def keys(self):  # noqa: D102 - dict-protocol shim
        return self._as_dict().keys()

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self._as_dict()

    def to_dict(self) -> dict[str, Any]:
        return self._as_dict()


def _build_asset(asset_id: str, local_path: Path, sha256: str) -> Any:
    """Return a StockSceneAsset if the real type is importable, else fake.

    The real type is constructed by filling its declared fields from
    the same value pool; if its shape is unrecognised we fall back to
    the duck-typed :class:`FakeStockAsset` (the stage is documented to
    consume the contract, and contract consumers are duck-typed
    throughout this codebase).
    """
    fake = FakeStockAsset(asset_id, local_path, sha256)
    if _RealStockSceneAsset is None:
        return fake
    pool = fake._as_dict()
    try:
        fields: list[str] = []
        model_fields = getattr(_RealStockSceneAsset, "model_fields", None)
        if isinstance(model_fields, dict):
            fields = list(model_fields)
        elif hasattr(_RealStockSceneAsset, "__dataclass_fields__"):
            fields = list(_RealStockSceneAsset.__dataclass_fields__)  # type: ignore[attr-defined]
        if not fields:
            return fake
        kwargs = {name: pool[name] for name in fields if name in pool}
        required = [
            name
            for name in fields
            if name not in kwargs
            and getattr(
                getattr(_RealStockSceneAsset, "__dataclass_fields__", {}).get(name),
                "default",
                None,
            )
            is None
        ]
        # Fill any still-missing declared field with a benign stand-in.
        for name in required:
            kwargs[name] = pool.get(name, asset_id)
        return _RealStockSceneAsset(**kwargs)
    except Exception:
        return fake


# ---------------------------------------------------------------------------
# Fake fetcher — scripted implementation of the StockFetcher contract
# ---------------------------------------------------------------------------


class FakeFetcher:
    """Scripted ``StockFetcher``: ``fetch_for_scene`` with no network.

    ``script`` is a list of outcomes consumed one per call; each item
    is an asset-id string (a hit), ``None`` (a miss), an exception
    instance (raised), or a callable ``(exclude_ids) -> outcome`` for
    exclude-aware scripting.  Once the script is exhausted the default
    outcome (``default=None`` → miss) is used.  Every call is recorded
    in ``calls`` as ``{scene_duration_s, queries, exclude_asset_ids}``.
    Asset stand-in files are tiny real PNGs under ``assets_dir``.
    """

    def __init__(
        self,
        assets_dir: Path,
        script: Optional[list] = None,
        *,
        raise_always: bool = False,
    ):
        self.assets_dir = Path(assets_dir)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.script = list(script or [])
        self.raise_always = raise_always
        self.calls: list[dict[str, Any]] = []
        self._asset_cache: dict[str, Any] = {}

    # -- asset factory ----------------------------------------------------
    def make_asset(self, asset_id: str) -> Any:
        if asset_id not in self._asset_cache:
            path = self.assets_dir / f"{asset_id}.png"
            if not path.exists():
                Image.new("RGB", (64, 36), (18, 42, 92)).save(path, format="PNG")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self._asset_cache[asset_id] = _build_asset(asset_id, path, sha)
        return self._asset_cache[asset_id]

    # -- the contract -------------------------------------------------------
    def fetch_for_scene(
        self,
        scene_duration_s: Any = None,
        queries: Any = None,
        exclude_asset_ids: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Tolerate alternate kwarg spellings a caller might use.
        if scene_duration_s is None:
            scene_duration_s = kwargs.get("duration_s", kwargs.get("duration"))
        if queries is None:
            queries = kwargs.get("query", kwargs.get("search_queries"))
        if exclude_asset_ids is None:
            exclude_asset_ids = kwargs.get(
                "exclude_ids", kwargs.get("exclude", kwargs.get("used_asset_ids"))
            )
        exclude = set(exclude_asset_ids or ())
        self.calls.append(
            {
                "scene_duration_s": scene_duration_s,
                "queries": list(queries) if isinstance(queries, (list, tuple)) else queries,
                "exclude_asset_ids": exclude,
            }
        )
        if self.raise_always:
            raise RuntimeError("fake stock provider exploded (mocked, no network)")
        outcome = self.script.pop(0) if self.script else None
        if callable(outcome):
            outcome = outcome(set(exclude))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return None
        return self.make_asset(str(outcome))


# ---------------------------------------------------------------------------
# Stage inputs (duck-typed exactly like the Round-2 stage consumes them)
# ---------------------------------------------------------------------------

# Plain REAL_WORLD / STANDARD sentences: no quote words (TEXTUAL), no
# >=2 numeric tokens (DATA), no list markers, no explanatory markers,
# each >= 4 words so the sentence merger never folds them together.
SENTENCES_2 = [
    "The deep ocean currents carry cold water across the silent basin",
    "Bright coral gardens shelter small fish along the rocky shoreline",
]
SENTENCES_4 = SENTENCES_2 + [
    "Gentle waves roll over smooth sand toward the quiet harbour",
    "Tall pine forests cover the green hills beyond the calm valley",
]


def _bundle(sentences: list[str]) -> SimpleNamespace:
    narration = ". ".join(sentences) + "."
    chapter = SimpleNamespace(index=0, title="Chapter", narration=narration, text=narration)
    return SimpleNamespace(
        chapters=[chapter],
        narration=narration,
        script_text=narration,
        niche_id="facts",
        video_id="stock-test-video",
    )


def _voice(total_s: float) -> SimpleNamespace:
    return SimpleNamespace(duration_s=total_s, total_duration_s=total_s, chapter_files=[])


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        profile_id="facts", niche_id="facts", visual_style="ocean documentary", taboo=[]
    )


def _ctx(workdir: Path, fetcher: Any = None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        visuals_dir=workdir / "visuals",
        video_id="stock-test-video",
        project_id="stock-test-video",
    )
    if fetcher is not None:
        # Belt-and-braces: implementations that read the fetcher off the
        # context find it under either documented attribute name.
        ctx.stock_fetcher = fetcher
        ctx.fetcher = fetcher
    return ctx


_FETCHER_PARAM_NAMES = (
    "stock_fetcher",
    "fetcher",
    "stock_fetch",
    "stock_provider",
    "stock_source",
)


def _run_visuals(bundle, voice, profile, ctx, fetcher=None):
    """Call the visuals stage, injecting ``fetcher`` by any known route.

    Returns the stage result.  Raises AssertionError with a precise
    "missing implementation" message when the stage offers NO
    injection route at all (pre-Round-3 signature).
    """
    fn = plan_and_generate_visuals
    if fetcher is None:
        return fn(bundle, voice, profile, ctx)

    params = inspect.signature(fn).parameters
    for name in _FETCHER_PARAM_NAMES:
        if name in params:
            return fn(bundle, voice, profile, ctx, **{name: fetcher})
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(bundle, voice, profile, ctx, stock_fetcher=fetcher)
    positional = [
        p
        for p in params.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 5:
        return fn(bundle, voice, profile, ctx, fetcher)
    if getattr(ctx, "stock_fetcher", None) is fetcher:
        # Last documented route: the context carries the fetcher.
        return fn(bundle, voice, profile, ctx)
    raise AssertionError(
        "MISSING IMPLEMENTATION: plan_and_generate_visuals has no fetcher "
        "injection route (no stock_fetcher/fetcher parameter, no **kwargs, "
        "no 5th positional parameter) — stock-first mode is not landed yet."
    )


# ---------------------------------------------------------------------------
# Result inspection helpers (tolerant of documented field-name aliases)
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
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


def _scenes(result: Any) -> list[dict]:
    scenes = _get(result, "scene_manifest", "scenes", default=[])
    return [dict(s) if isinstance(s, dict) else _scene_to_dict(s) for s in scenes]


def _scene_to_dict(scene: Any) -> dict:
    dump = getattr(scene, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:
            pass
    if hasattr(scene, "__dict__"):
        return dict(vars(scene))
    return {"_raw": scene}


def _ledger_rows(result: Any) -> list[dict]:
    rows = _get(result, "ledger_rows", "licence_rows", "license_rows", default=[])
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append(_scene_to_dict(row))
    return out


def _scene_stock_asset_id(scene: dict) -> Optional[str]:
    """The stock asset id recorded for a scene, or None if not stock."""
    for key in ("stock_asset_id", "provider_asset_id", "stock_id"):
        value = scene.get(key)
        if value:
            return str(value)
    source = str(scene.get("source") or "").strip().lower()
    chosen = str(scene.get("chosen_source") or "").strip().lower()
    kind = str(scene.get("source_kind") or scene.get("kind") or "").strip().lower()
    provider = str(scene.get("provider") or "").strip().lower()
    is_stock = (
        source in {"stock", "pixabay", "pexels"}
        or source.startswith("stock")
        or chosen == "stock"
        or kind == "stock"
        or provider in {"pixabay", "pexels", "stock"}
        or str(scene.get("decision_reason") or "").strip().lower() == "stock_match"
    )
    if not is_stock:
        return None
    for key in ("asset_id", "asset_ref", "stock_asset_id", "provider_asset_id"):
        value = scene.get(key)
        if value:
            return str(value)
    return ""  # stock, but the record carries no explicit asset id


def _is_stock_scene(scene: dict) -> bool:
    return _scene_stock_asset_id(scene) is not None


def _is_plate_scene(scene: dict) -> bool:
    if _is_stock_scene(scene):
        return False
    source = str(scene.get("source") or "").strip().lower()
    generation = str(scene.get("generation") or "").strip().lower()
    chosen = str(scene.get("chosen_source") or "").strip().lower()
    return (
        source == "local_plate"
        or "pillow" in generation
        or "local_plate" in generation
        or chosen in {"graphic", "text_card"}
    )


def _result_stock_share(result: Any, scenes: list[dict]) -> Optional[float]:
    value = _get(result, "stock_share", "stock_fraction", "share_stock")
    if value is not None:
        return float(value)
    manifest_path = _get(result, "manifest_path")
    if manifest_path and Path(str(manifest_path)).is_file():
        try:
            doc = json.loads(Path(str(manifest_path)).read_text(encoding="utf-8"))
        except Exception:
            doc = {}
        for key in ("stock_share", "stock_fraction", "share_stock"):
            if isinstance(doc, dict) and doc.get(key) is not None:
                return float(doc[key])
    return None


def _require_stock_share(result: Any, scenes: list[dict]) -> float:
    share = _result_stock_share(result, scenes)
    assert share is not None, (
        "MISSING IMPLEMENTATION: stage result exposes no stock_share "
        "(checked result.stock_share / stock_fraction and the persisted "
        "scene_manifest.json) — Round-3 stock mode is not landed yet."
    )
    return share


_HEX64 = set("0123456789abcdef")


def _row_sha(row: dict) -> Optional[str]:
    for key in ("content_hash", "file_sha256", "sha256", "sha", "hash"):
        value = row.get(key)
        if isinstance(value, str) and len(value.strip()) == 64 and set(value.strip().lower()) <= _HEX64:
            return value.strip().lower()
    notes = str(row.get("notes") or "")
    for token in notes.replace(";", " ").replace("=", " ").split():
        token = token.strip().lower()
        if len(token) == 64 and set(token) <= _HEX64:
            return token
    return None


def _row_for_scene(rows: list[dict], scene_id: str) -> Optional[dict]:
    for row in rows:
        if str(row.get("scene_id") or "") == scene_id:
            return row
    return None


def _assert_every_scene_has_visual(result: Any, scenes: list[dict]) -> None:
    """No scene left without a visual: manifest entry + file on disk."""
    assert scenes, "stage produced no scenes"
    asset_files = _get(result, "asset_files", default={}) or {}
    for scene in scenes:
        scene_id = str(scene.get("scene_id"))
        path = scene.get("asset_path") or scene.get("asset_ref") or asset_files.get(scene_id)
        assert path, f"scene {scene_id} has no asset path recorded"
        assert Path(str(path)).is_file(), f"scene {scene_id} asset missing on disk: {path}"
        assert Path(str(path)).stat().st_size > 0, f"scene {scene_id} asset is empty: {path}"


def _stock_scenes(scenes: list[dict]) -> list[dict]:
    return [s for s in scenes if _is_stock_scene(s)]


# ---------------------------------------------------------------------------
# (a) Stock hit — scenes use stock assets, sources recorded with asset ids
# ---------------------------------------------------------------------------


def test_stock_hit_scenes_use_stock_assets_and_record_sources(workdir):
    fetcher = FakeFetcher(workdir / "assets", script=["px-ocean-1", "px-coral-2", "px-waves-3", "px-pines-4"])
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)

    assert len(scenes) == len(SENTENCES_4)
    assert fetcher.calls, "stock mode enabled by injection, but the fetcher was never called"
    stock = _stock_scenes(scenes)
    assert stock, (
        "fetcher returned assets for every call, yet no scene is recorded "
        f"as stock; scene sources were: {[s.get('source') for s in scenes]}"
    )
    # Every REAL_WORLD scene is stock-eligible (roadmap decision step 5);
    # in an all-hit run each one the fetcher served must be stock.
    for scene in scenes:
        if str(scene.get("scene_class") or "").upper() == "REAL_WORLD":
            assert _is_stock_scene(scene), (
                f"REAL_WORLD scene {scene.get('scene_id')} was not stock-sourced "
                f"in an all-hit run: {scene}"
            )
    # Sources are recorded WITH asset ids, and each id was really offered.
    offered = {"px-ocean-1", "px-coral-2", "px-waves-3", "px-pines-4"}
    for scene in stock:
        asset_id = _scene_stock_asset_id(scene)
        assert asset_id, f"stock scene {scene.get('scene_id')} records no asset id: {scene}"
        assert asset_id in offered, f"scene records unoffered asset id {asset_id!r}"
    _assert_every_scene_has_visual(result, scenes)


def test_fetcher_receives_duration_queries_and_exclusions(workdir):
    """Contract shape: fetch_for_scene(scene_duration_s, queries, exclude)."""
    fetcher = FakeFetcher(workdir / "assets", script=["px-a", "px-b"])
    _run_visuals(
        _bundle(SENTENCES_2), _voice(6.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    assert len(fetcher.calls) >= 1, "fetcher was never called"
    for call in fetcher.calls:
        duration = call["scene_duration_s"]
        assert isinstance(duration, (int, float)) and duration > 0, (
            f"fetch_for_scene received a non-positive scene_duration_s: {duration!r}"
        )
        queries = call["queries"]
        assert isinstance(queries, (list, tuple)) and queries, (
            f"fetch_for_scene received no queries: {queries!r}"
        )
        assert all(isinstance(q, str) and q.strip() for q in queries), (
            f"queries must be non-empty strings: {queries!r}"
        )
        assert isinstance(call["exclude_asset_ids"], (set, frozenset, list, tuple)), (
            "exclude_asset_ids must be a collection, got "
            f"{type(call['exclude_asset_ids']).__name__}"
        )


# ---------------------------------------------------------------------------
# (b) Stock miss — every scene falls back to a local plate
# ---------------------------------------------------------------------------


def test_stock_miss_every_scene_falls_back_to_local_plate(workdir):
    fetcher = FakeFetcher(workdir / "assets", script=[None, None, None, None])
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)

    assert fetcher.calls, "fetcher was never called — stock mode not active"
    assert len(scenes) == len(SENTENCES_4)
    assert not _stock_scenes(scenes), "fetcher missed everywhere, yet a scene claims stock"
    for scene in scenes:
        assert _is_plate_scene(scene), (
            f"scene {scene.get('scene_id')} is neither stock nor a local plate "
            f"after a stock miss: {scene}"
        )
    _assert_every_scene_has_visual(result, scenes)
    assert _require_stock_share(result, scenes) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# (c) Fetcher raises — same fallback, stage does not crash
# ---------------------------------------------------------------------------


def test_fetcher_exception_falls_back_to_plate_without_crashing(workdir):
    fetcher = FakeFetcher(workdir / "assets", raise_always=True)
    # Must not raise: a provider explosion degrades to plates.
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)

    assert fetcher.calls, "fetcher was never called — stock mode not active"
    assert len(scenes) == len(SENTENCES_4)
    assert not _stock_scenes(scenes)
    for scene in scenes:
        assert _is_plate_scene(scene), (
            f"scene {scene.get('scene_id')} has no plate fallback after a "
            f"fetcher exception: {scene}"
        )
    _assert_every_scene_has_visual(result, scenes)


def test_fetcher_exception_mid_run_keeps_earlier_stock_scenes(workdir):
    fetcher = FakeFetcher(
        workdir / "assets",
        script=["px-first-1", RuntimeError("mocked provider boom"), None, None],
    )
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)
    assert len(scenes) == len(SENTENCES_4)
    assert fetcher.calls, "fetcher was never called — stock mode not active"
    assert _stock_scenes(scenes), (
        "first fetch succeeded before the exception, yet no scene is "
        "recorded as stock"
    )
    _assert_every_scene_has_visual(result, scenes)
    # Whatever was successfully fetched before the failure stays stock;
    # everything else is a plate — and nothing is left visual-less.
    for scene in scenes:
        assert _is_stock_scene(scene) or _is_plate_scene(scene), (
            f"scene {scene.get('scene_id')} has an unrecorded source: {scene}"
        )


# ---------------------------------------------------------------------------
# (d) Anti-repetition — a re-offered asset id is used at most once
# ---------------------------------------------------------------------------


def test_anti_repetition_same_asset_offered_twice_used_once(workdir):
    # The (sloppy, mocked) provider offers px-dup-1 for scene 1, offers
    # it AGAIN for scene 2 (ignoring exclusions), and would offer
    # px-alt-2 on a further attempt.  The stage must use px-dup-1 at
    # most once: scene 2 gets the alternate or a local plate.
    fetcher = FakeFetcher(
        workdir / "assets", script=["px-dup-1", "px-dup-1", "px-alt-2", "px-alt-2"]
    )
    result = _run_visuals(
        _bundle(SENTENCES_2), _voice(6.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)
    assert len(scenes) == 2

    used_ids = [
        _scene_stock_asset_id(s) for s in scenes if _scene_stock_asset_id(s)
    ]
    assert used_ids.count("px-dup-1") == 1, (
        f"asset px-dup-1 was used {used_ids.count('px-dup-1')} times "
        f"(anti-repetition violated); recorded ids: {used_ids}"
    )
    second = scenes[1]
    assert _scene_stock_asset_id(second) != "px-dup-1"
    assert _is_plate_scene(second) or _scene_stock_asset_id(second) == "px-alt-2", (
        f"second scene should hold the alternate stock asset or a plate: {second}"
    )
    # The stage must tell the fetcher what to exclude on later calls.
    later_exclusions = [c["exclude_asset_ids"] for c in fetcher.calls[1:]]
    assert any("px-dup-1" in set(ex or ()) for ex in later_exclusions), (
        "fetcher was never told to exclude the already-used asset id "
        f"px-dup-1; recorded exclusions: {[sorted(c['exclude_asset_ids']) for c in fetcher.calls]}"
    )
    _assert_every_scene_has_visual(result, scenes)


# ---------------------------------------------------------------------------
# (e) Stock disabled — output identical in structure to Round-2 plate mode
# ---------------------------------------------------------------------------

# The exact per-scene record keys the Round-2 plate stage emits
# (visuals_stage.plan_and_generate_visuals, pre-stock version).
_ROUND2_SCENE_KEYS = {
    "scene_id", "chapter_index", "sentence_ids", "text", "start_s", "end_s",
    "duration_s", "beat_class", "scene_class", "asset_path", "source",
    "generation", "chosen_source", "decision_source", "decision_reason",
    "source_decision", "decision_detail", "motion", "overlay_text",
    "text_rendered", "is_still", "licence_ref", "content_hash",
    "safety_flags", "notes", "status",
}
_ROUND2_LEDGER_SOURCE = "own/generated (local Pillow plate)"


def test_stock_disabled_matches_round2_plate_structure(workdir, monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir), fetcher=None
    )
    scenes = _scenes(result)
    rows = _ledger_rows(result)

    assert len(scenes) == len(SENTENCES_4)
    for scene in scenes:
        missing = _ROUND2_SCENE_KEYS - set(scene)
        assert not missing, (
            f"plate-mode scene {scene.get('scene_id')} lost Round-2 record "
            f"keys: {sorted(missing)}"
        )
        # Round 2 recorded source="local_plate"; the landed Round-3
        # stage records the provenance spelling "plate:local" in the
        # same field.  Both unambiguously mean a local plate — what
        # must NOT happen in disabled mode is any stock provenance.
        assert scene.get("source") in {"local_plate", "plate:local"}, (
            f"disabled stock must stay a local plate, got {scene.get('source')!r}"
        )
        assert not str(scene.get("source") or "").startswith("stock"), (
            f"disabled stock produced a stock source record: {scene}"
        )
        assert not _is_stock_scene(scene)
    assert len(rows) == len(scenes)
    for row in rows:
        assert row.get("source") == _ROUND2_LEDGER_SOURCE, (
            f"plate ledger row changed shape in disabled mode: {row}"
        )
        assert _row_sha(row), f"plate ledger row lost its content hash: {row}"
    path_used = _get(result, "path_used")
    if path_used is not None:
        assert path_used == "local-plates", (
            f"disabled stock must report the Round-2 path, got {path_used!r}"
        )
    share = _result_stock_share(result, scenes)
    if share is not None:
        assert share == pytest.approx(0.0)
    _assert_every_scene_has_visual(result, scenes)


# ---------------------------------------------------------------------------
# (f) Mixed pack ledger — stock rows carry provider+licence+sha, plate
#     rows are generated, and the ledger covers 100% of scenes
# ---------------------------------------------------------------------------


def test_mixed_pack_ledger_covers_all_scenes(workdir):
    # First served scene hits, everything after that misses -> a mixed
    # pack with at least one stock scene and at least one plate scene.
    fetcher = FakeFetcher(workdir / "assets", script=["px-ledger-1", None, None, None])
    result = _run_visuals(
        _bundle(SENTENCES_4), _voice(20.0), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)
    rows = _ledger_rows(result)

    stock = _stock_scenes(scenes)
    plates = [s for s in scenes if not _is_stock_scene(s)]
    assert stock, "mixed script produced no stock scene — ledger mix untestable"
    assert plates, "mixed script produced no plate scene — ledger mix untestable"

    # 100% coverage: exactly one ledger row per scene, keyed by scene_id.
    scene_ids = {str(s.get("scene_id")) for s in scenes}
    row_scene_ids = [str(r.get("scene_id")) for r in rows]
    assert set(row_scene_ids) == scene_ids, (
        f"ledger covers {sorted(set(row_scene_ids))}, scenes are {sorted(scene_ids)}"
    )
    assert len(rows) == len(scenes), (
        f"expected exactly one ledger row per scene ({len(scenes)}), got {len(rows)}"
    )

    for scene in stock:
        row = _row_for_scene(rows, str(scene.get("scene_id")))
        assert row is not None, f"no ledger row for stock scene {scene.get('scene_id')}"
        providerish = " ".join(
            str(row.get(k) or "") for k in ("provider", "source", "provider_name")
        ).lower()
        assert "pixabay" in providerish or "pexels" in providerish or "stock" in providerish, (
            f"stock ledger row names no provider: {row}"
        )
        licence = _get(row, "licence", "license", "licence_name", "license_name")
        assert licence and "generated" not in str(licence).lower() and str(licence).lower() != "n/a — generated in-app", (
            f"stock ledger row carries no real licence: {row}"
        )
        assert _row_sha(row), f"stock ledger row carries no content SHA: {row}"
        recorded_id = _scene_stock_asset_id(scene)
        row_asset = str(row.get("asset_id") or "")
        # The ledger row must reference the very asset the scene records:
        # either by asset id, or by the asset's path.
        assert row_asset == recorded_id or str(row.get("asset_path") or "") == str(scene.get("asset_path") or "") or str(row.get("local_path") or "") == str(scene.get("asset_path") or ""), (
            f"stock ledger row does not reference the scene's asset: row={row} scene={scene}"
        )

    for scene in plates:
        row = _row_for_scene(rows, str(scene.get("scene_id")))
        assert row is not None, f"no ledger row for plate scene {scene.get('scene_id')}"
        sourceish = " ".join(
            str(row.get(k) or "") for k in ("source", "provider")
        ).lower()
        assert "generated" in sourceish or "local_plate" in sourceish or "local plate" in sourceish, (
            f"plate ledger row is not a generated row: {row}"
        )
        assert _row_sha(row), f"plate ledger row carries no content SHA: {row}"


# ---------------------------------------------------------------------------
# (g) stock_share in the stage result == stock_scenes / total_scenes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,total_s,sentences",
    [
        (["px-s1", "px-s2", "px-s3", "px-s4"], 20.0, SENTENCES_4),  # all hit
        ([None, None, None, None], 20.0, SENTENCES_4),  # all miss
        (["px-m1", None, "px-m3", None], 20.0, SENTENCES_4),  # mixed
        (["px-two-1", None], 6.0, SENTENCES_2),  # mixed, 2 scenes
    ],
    ids=["all-hit", "all-miss", "mixed-4", "mixed-2"],
)
def test_stock_share_equals_stock_scenes_over_total(workdir, script, total_s, sentences):
    fetcher = FakeFetcher(workdir / "assets", script=list(script))
    result = _run_visuals(
        _bundle(sentences), _voice(total_s), _profile(), _ctx(workdir, fetcher), fetcher=fetcher
    )
    scenes = _scenes(result)
    assert scenes, "stage produced no scenes"
    share = _require_stock_share(result, scenes)
    expected = len(_stock_scenes(scenes)) / len(scenes)
    assert share == pytest.approx(expected), (
        f"stock_share={share} but the manifest records "
        f"{len(_stock_scenes(scenes))}/{len(scenes)} stock scenes "
        f"(expected {expected})"
    )
    assert 0.0 <= share <= 1.0
