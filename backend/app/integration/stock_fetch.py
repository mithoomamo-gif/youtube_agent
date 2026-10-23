"""Stock fetch service for the visuals stage (Round 3, §24).

One public contract, coded against by the visuals-stage worker:

* :class:`StockSceneAsset` — the downloaded, licence-stamped asset for
  one scene.
* :class:`StockFetcher.fetch_for_scene` — synchronous method ::

      fetch_for_scene(scene_duration_s: float,
                      queries: list[str],
                      exclude_asset_ids: set[str] | None = None
                      ) -> StockSceneAsset | None

  (``fetch_for_scene_async`` is the same logic as a coroutine, for
  callers that already live on an event loop.)

Behaviour (§24 + Round-3 spec):

1. Query ladder: for each query in order, search Pixabay **videos**
   first.  Only when *no* query in the ladder yields a usable video do
   we try **photos** with the same ladder.
2. Hard filters (drop before ranking): landscape (``width > height``);
   video ``width >= 1280`` and ``duration_s >= scene_duration_s``;
   photo ``width >= 1280`` minimum with ``>= 1920`` preferred (when a
   query yields any >=1920 photo, narrower photos are dropped for that
   query); safesearch on (NSFW candidates rejected; the adapter search
   is also requested with ``safesearch=True``); candidates whose
   tags/title/flags indicate brands or logos are excluded; ids in
   ``exclude_asset_ids`` are excluded (bare ids and ``"pixabay:<id>"``
   keys both honoured).
3. Ranking: ``score = 0.65 * relevance + 0.35 * freshness`` where
   relevance is the query/tag+title match (the adapter's relevance
   signal), and freshness deliberately de-ranks the most-downloaded
   asset (fewer downloads ranks higher among near-equal relevance —
   the most-downloaded clip is the one most likely already used in
   hundreds of other faceless videos).  Deterministic tie-break:
   ascending provider asset id.
4. Downloads land in a **content-addressed** cache,
   ``backend/cache/stock/<sha256>.<ext>``.  An asset-id -> hash index
   (``index.json`` in the same directory) means a repeat fetch of the
   same asset performs **no download at all**; a hash file that already
   exists is never rewritten.
4a. **Tier selection (Round 4).**  Provider adapters promote the
   *largest* tier to ``download_url`` (for Pixabay that is the UHD
   ``large`` tier — Round 3 measured single files up to ~135 MB and a
   ~1.3 GB cache for one proof run).  This fetcher instead re-selects
   from the candidate's ``download_tiers`` (see :func:`_select_tier`):

   * adequate tiers are those ``>= 1280`` px wide; the base pick is
     the *smallest* adequate tier;
   * the pick is upgraded to the smallest ``>= 1920`` px tier only
     when that tier is *modest* next to the base pick: estimated size
     ``<= 3.0x`` the base estimate and, when both tiers carry real
     byte sizes, at most 32 MiB larger in absolute terms.  Size
     estimates use the tier's ``size`` when present, else a
     width*height pixel-area proxy (same duration/fps across tiers,
     so area tracks bytes);
   * if no tier is ``>= 1280``, the largest available tier is used
     and the fallback is recorded (``tier_fallback=True`` on the asset
     and in the index entry);
   * candidates with no usable tier list keep their ``download_url``
     unchanged.
4b. **Cache cap (Round 4).**  The content cache is capped at
   ``cache_max_bytes`` (constructor argument; else the
   ``YTA_STOCK_CACHE_MAX_BYTES`` env var; else 2 GiB).  After each new
   download the least-recently-used index entries (by ``last_access``,
   refreshed on every index hit) are evicted — file plus index entry —
   until the indexed total fits the cap.  Files returned during the
   current fetcher's lifetime (the current run) are never evicted, a
   content file shared by several index entries is deleted only with
   its last entry, and ``index.json`` itself is only ever rewritten
   atomically, never deleted.  Unindexed orphan files are left alone.
5. Search responses rely on the adapter's own 24h cache when it has
   one (the Pixabay adapter does).  Adapters without one get a file
   cache at ``backend/cache/stock/search/`` keyed by
   ``sha256(query + filters)`` with a 24h TTL.
6. ``None`` — never an exception — for ordinary no-result / outage:
   provider exceptions are caught, logged *without secrets*, and
   treated as no result; the caller falls back to local plates.
7. **Brightness selection filter (Round 5, SL-006).**  Every
   materialised candidate is measured by
   :mod:`app.integration.brightness` (mean ``signalstats`` YAVG over
   sampled keyframes) and a candidate measuring below
   ``BRIGHTNESS_FLOOR_YAVG`` is REJECTED as near-black: it is added
   to the run's exclusion set and the next ranked candidate is
   tried, so a dark clip never reaches the visuals stage or QC.
   This is a SELECTION filter only — no QC threshold or gate rule
   changes.  Unmeasurable candidates (corrupt / non-media bytes)
   abstain and pass, exactly as before the filter existed.  Run
   stats live on ``fetcher.brightness_filter.stats()`` /
   ``fetcher.brightness_stats`` and are surfaced by the visuals
   stage into the production report.

Credentials: the fetcher never reads, writes, prints or logs the
Pixabay key.  The default adapter is the existing
:class:`~app.providers.stock.pixabay.PixabayProvider` wired to
:class:`~app.providers.secrets_env.EnvSecretProvider`, which resolves
the key solely from the ``PIXABAY_API_KEY`` environment variable
supplied at runtime.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

__all__ = [
    "CACHE_MAX_BYTES_ENV_VAR",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_CACHE_MAX_BYTES",
    "LICENCE_NAME",
    "PIXABAY_TERMS_URL",
    "TERMS_HASH",
    "StockFetcher",
    "StockSceneAsset",
    "default_cache_dir",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Licence constants (stable; terms change -> bump deliberately, ledger
# rows already written keep the hash they were created with).
# ---------------------------------------------------------------------------

LICENCE_NAME = "Pixabay Content License"
PIXABAY_TERMS_URL = "https://pixabay.com/service/license-summary/"
#: sha256 of the licence name + provider terms URL string.
TERMS_HASH = hashlib.sha256(
    (LICENCE_NAME + PIXABAY_TERMS_URL).encode("utf-8")
).hexdigest()

PROVIDER_NAME = "pixabay"

#: Historical dev cache root: backend/cache/stock/ (workspace disk —
#: /tmp is a small tmpfs on the build VM and must never hold caches).
#: Kept as the fallback when no data root was explicitly configured;
#: see :func:`default_cache_dir` for the full resolution.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "cache" / "stock"


def default_cache_dir() -> Path:
    """Resolve the default stock cache directory (Round 6 contract).

    1. ``$YTA_DATA_DIR/cache/stock`` when the data-dir override is set
       (packaged/frozen runs: the install tree may be read-only, so the
       cache MUST live under the data root —
       :func:`app.data_dir.explicit_data_dir` is the one place that
       distinction is made);
    2. the historical dev location ``backend/cache/stock``
       (:data:`DEFAULT_CACHE_DIR`), unchanged when the override is
       unset.
    """
    from app.data_dir import data_dir_layout, explicit_data_dir

    explicit = explicit_data_dir()
    if explicit is not None:
        return data_dir_layout(explicit).stock_cache_dir
    return DEFAULT_CACHE_DIR


_VIDEO_MIN_WIDTH = 1280
_PHOTO_MIN_WIDTH = 1280
_PHOTO_PREFERRED_WIDTH = 1920
_SEARCH_CACHE_TTL_S = 24 * 60 * 60

# --- Tier selection (Round 4; rule documented in the module docstring) --
#: A tier this wide or wider is adequate for a 720p+ deliverable.
_TIER_ADEQUATE_WIDTH = 1280
#: The preferred width the pick is upgraded to when the delta is modest.
_TIER_PREFERRED_WIDTH = 1920
#: "Modest" (ratio): the >=1920 tier's estimated size may be at most
#: this multiple of the smallest-adequate tier's estimated size.
_TIER_MODEST_MAX_RATIO = 3.0
#: "Modest" (absolute): when both tiers carry real byte sizes, the
#: >=1920 tier may be at most this many bytes larger than the base pick.
_TIER_MODEST_MAX_EXTRA_BYTES = 32 * 1024 * 1024

# --- Stock cache cap (Round 4) ------------------------------------------
#: Env override for the content-cache cap (bytes), following the repo's
#: YTA_* launch-environment convention (see app.config).
CACHE_MAX_BYTES_ENV_VAR = "YTA_STOCK_CACHE_MAX_BYTES"
#: Default content-cache cap: 2 GiB.
DEFAULT_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024

_BRAND_WORDS = {
    "logo", "logos", "brand", "branded", "branding", "trademark",
    "trademarked", "watermark",
}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


@dataclass
class StockSceneAsset:
    """One downloaded stock asset for one scene (public contract)."""

    local_path: str
    provider: str  # always "pixabay" from this fetcher
    provider_asset_id: str
    asset_page_url: str
    download_url: str
    licence_name: str  # "Pixabay Content License"
    terms_hash: str
    file_sha256: str
    width: int
    height: int
    duration_s: float | None  # None for photos
    kind: str  # "video" | "photo"
    # Tier-selection metadata (Round 4): the provider quality label of
    # the tier actually downloaded ("" when the candidate carried no
    # usable tier list), and whether the pick fell back to the largest
    # tier because no tier was >= 1280 px wide.
    tier_quality: str = ""
    tier_fallback: bool = False

    def __await__(self):  # tolerate `await fetcher.fetch_for_scene(...)`
        async def _identity():
            return self

        return _identity().__await__()


# ---------------------------------------------------------------------------
# Internal normalised candidate
# ---------------------------------------------------------------------------


@dataclass
class _Cand:
    provider_asset_id: str
    kind: str  # "video" | "photo"
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    downloads: int | None = None
    tags: list[str] = field(default_factory=list)
    title: str = ""
    page_url: str = ""
    download_url: str = ""
    is_nsfw: bool = False
    trademark_or_logo: bool = False
    adapter_index: int = 0
    raw: Any = None  # original adapter candidate (for adapter.download)
    # Tier selection (Round 4): the candidate's usable download tiers
    # (normalised dicts) plus the outcome recorded by _select_tier.
    download_tiers: list[dict] = field(default_factory=list)
    tier_quality: str = ""
    tier_fallback: bool = False


def _tokens(text: str) -> set[str]:
    import re

    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Tier selection (Round 4)
# ---------------------------------------------------------------------------


def _normalise_tiers(raw_tiers: Any) -> list[dict]:
    """Normalise a candidate's ``download_tiers`` into plain dicts.

    Keeps only tiers with a URL and a usable width; ``size`` is carried
    through when the provider supplies it (the Pixabay API does, even
    though its adapter currently drops it — see the report note).
    """
    tiers: list[dict] = []
    for tier in raw_tiers or []:
        if not isinstance(tier, dict):
            continue
        url = tier.get("url") or tier.get("link")
        width = _as_int(tier.get("width"))
        if not url or not width:
            continue
        size = _as_int(tier.get("size"))
        tiers.append(
            {
                "quality": str(tier.get("quality") or ""),
                "url": str(url),
                "width": width,
                "height": _as_int(tier.get("height")),
                "size": size if size and size > 0 else None,
            }
        )
    return tiers


def _tier_size_estimate(tier: dict) -> float:
    """Estimated download size of one tier.

    The real byte ``size`` when the provider supplies it; otherwise a
    width*height pixel-area proxy (duration and fps are constant across
    a candidate's tiers, so area tracks bytes closely enough for the
    modest-delta comparison).
    """
    if tier.get("size"):
        return float(tier["size"])
    return float(tier["width"] * (tier.get("height") or tier["width"]))


def _select_tier(tiers: list[dict]) -> tuple[dict, bool] | None:
    """Pick the download tier for a candidate (rule: module docstring §4a).

    Returns ``(tier, fallback)`` or ``None`` when there is no usable
    tier.  ``fallback`` is True only when no tier was >= 1280 px wide
    and the largest available tier had to be used.
    """
    usable = [t for t in tiers if t.get("url") and t.get("width")]
    if not usable:
        return None
    by_width = sorted(usable, key=lambda t: (t["width"], _tier_size_estimate(t)))
    adequate = [t for t in by_width if t["width"] >= _TIER_ADEQUATE_WIDTH]
    if not adequate:
        return by_width[-1], True  # largest available; caller records it
    base = adequate[0]  # smallest adequate tier
    preferred = next(
        (t for t in adequate if t["width"] >= _TIER_PREFERRED_WIDTH), None
    )
    if preferred is not None and preferred is not base:
        base_est = _tier_size_estimate(base)
        pref_est = _tier_size_estimate(preferred)
        modest = pref_est <= _TIER_MODEST_MAX_RATIO * base_est
        if modest and base.get("size") and preferred.get("size"):
            modest = (preferred["size"] - base["size"]) <= _TIER_MODEST_MAX_EXTRA_BYTES
        if modest:
            return preferred, False
    return base, False


def _normalise_candidate(obj: Any, kind: str, index: int) -> _Cand | None:
    """Accept adapter ``Candidate`` objects, dicts, or duck-typed rows."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        get = obj.get
    else:
        def get(name, default=None):  # type: ignore[misc]
            return getattr(obj, name, default)

    asset_id = get("provider_asset_id") or get("id")
    if asset_id is None:
        return None
    flags = get("sensitive_flags")
    trademark = False
    if flags is not None:
        if isinstance(flags, dict):
            trademark = bool(flags.get("trademark_or_logo"))
        else:
            trademark = bool(getattr(flags, "trademark_or_logo", False))
    tags = get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    width = _as_int(get("width"))
    height = _as_int(get("height"))
    # Tier selection (Round 4): adapters promote the LARGEST tier to
    # download_url/width/height; re-select the smallest adequate tier
    # from download_tiers instead (see _select_tier for the rule).
    tiers = _normalise_tiers(get("download_tiers"))
    tier_quality = ""
    tier_fallback = False
    selected = _select_tier(tiers) if tiers else None
    if selected is not None:
        tier, tier_fallback = selected
        download_url = tier["url"]
        width = tier["width"]
        height = tier["height"] or height
        tier_quality = tier["quality"]
    else:
        download_url = get("download_url") or ""
        # Video candidates may carry their best tier's dimensions only
        # in download_tiers; be tolerant of tiers-only shapes.
        if width is None or height is None:
            for tier in get("download_tiers") or []:
                if isinstance(tier, dict):
                    width = width or _as_int(tier.get("width"))
                    height = height or _as_int(tier.get("height"))
                    if width and height:
                        break
        if not download_url:
            for tier in get("download_tiers") or []:
                if isinstance(tier, dict) and tier.get("url"):
                    download_url = tier["url"]
                    break
    return _Cand(
        provider_asset_id=str(asset_id),
        kind=kind,
        width=width,
        height=height,
        duration_s=_as_float(get("duration_s", get("duration"))),
        downloads=_as_int(get("downloads")),
        tags=[str(t) for t in tags],
        title=str(get("title") or ""),
        page_url=str(get("page_url") or get("pageURL") or ""),
        download_url=str(download_url),
        is_nsfw=bool(get("is_nsfw") or False),
        trademark_or_logo=trademark,
        adapter_index=index,
        raw=obj,
        download_tiers=tiers,
        tier_quality=tier_quality,
        tier_fallback=tier_fallback,
    )


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class StockFetcher:
    """Pixabay stock fetcher for the visuals stage.

    Parameters (all optional; tests inject fakes):

    * ``adapter`` / ``provider`` — a stock provider with the existing
      adapter interface (``async search(query, media_type, filters)``,
      like :class:`PixabayProvider`).  Default: the real Pixabay
      adapter, keyed only via the ``PIXABAY_API_KEY`` env var.
    * ``cache_dir`` — content-addressed cache root
      (default ``backend/cache/stock/``).
    * ``downloader`` — callable ``url -> bytes`` (sync or async) used
      instead of the adapter/HTTP download path (tests).
    * ``clock`` — ``() -> float`` epoch-seconds clock (TTL tests).
    * ``cache_max_bytes`` — content-cache cap with LRU eviction
      (Round 4).  ``None`` (default) resolves the
      ``YTA_STOCK_CACHE_MAX_BYTES`` env var, falling back to
      :data:`DEFAULT_CACHE_MAX_BYTES` (2 GiB); ``<= 0`` disables the cap.
    * ``brightness_filter`` — the SL-006 near-black selection filter
      (:class:`app.integration.brightness.StockBrightnessFilter`);
      a default instance at the module floor is used when omitted.
    """

    def __init__(
        self,
        adapter: Any = None,
        *,
        provider: Any = None,
        cache_dir: str | Path | None = None,
        downloader: Callable[[str], Any] | None = None,
        secret_provider: Any = None,
        clock: Callable[[], float] | None = None,
        cache_max_bytes: int | None = None,
        brightness_filter: Any = None,
    ):
        self._adapter = adapter if adapter is not None else provider
        if self._adapter is None:
            self._adapter = self._default_adapter(secret_provider)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        self.search_cache_dir = self.cache_dir / "search"
        self._downloader = downloader
        self._clock = clock or time.time
        if cache_max_bytes is None:
            cache_max_bytes = self._cache_cap_from_env()
        self.cache_max_bytes = cache_max_bytes
        # Paths handed out by this fetcher instance (the current run):
        # the LRU eviction never deletes these.
        self._active_paths: set[str] = set()
        # The adapter's own 24h search cache is authoritative when it
        # exists; otherwise this fetcher keeps a 24h file cache.
        self._adapter_has_search_cache = getattr(self._adapter, "cache", None) is not None
        # SL-006 brightness selection filter (see module docstring §7).
        if brightness_filter is not None:
            self.brightness_filter = brightness_filter
        else:
            from .brightness import StockBrightnessFilter

            self.brightness_filter = StockBrightnessFilter()

    @property
    def brightness_stats(self) -> dict:
        """SL-006 run stats: checked / rejected-too-dark / detail."""
        filt = getattr(self, "brightness_filter", None)
        if filt is None or not hasattr(filt, "stats"):
            return {}
        return filt.stats()

    @staticmethod
    def _cache_cap_from_env() -> int:
        raw = (os.environ.get(CACHE_MAX_BYTES_ENV_VAR) or "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                logger.warning(
                    "stock_fetch: ignoring malformed %s=%r",
                    CACHE_MAX_BYTES_ENV_VAR, raw,
                )
            else:
                if value > 0:
                    return value
        return DEFAULT_CACHE_MAX_BYTES

    # -- construction ---------------------------------------------------

    @staticmethod
    def _default_adapter(secret_provider: Any = None) -> Any:
        from ..providers.secrets_env import EnvSecretProvider
        from ..providers.stock.pixabay import PixabayProvider

        secrets = secret_provider if secret_provider is not None else EnvSecretProvider()
        return PixabayProvider(secrets, "pixabay_api_key")

    @property
    def adapter(self) -> Any:
        return self._adapter

    # -- public contract --------------------------------------------------

    def fetch_for_scene(
        self,
        scene_duration_s: float,
        queries: list[str],
        exclude_asset_ids: set[str] | None = None,
    ) -> StockSceneAsset | None:
        """Sync contract: fetch one stock asset for one scene, or None.

        Loop ownership (Round 4, ``app.integration.run_loop``): this
        fetcher owns NO loop.  The coroutine runs via
        :func:`run_coroutine`, i.e. on the produce run's single loop
        when called inside a run — which is what keeps the shared
        provider adapter's httpx connection pool (bound to that loop)
        valid for every scene of the run.  Earlier mechanisms each
        broke that: a fresh ``asyncio.run()`` per call closed the loop
        under the pool (``RuntimeError: Event loop is closed`` on later
        calls), and the persistent private loop that replaced it was a
        second, fetcher-owned loop living outside the run's lifetime.
        Both are gone; outside a produce run, ``run_coroutine`` is the
        outer-boundary facade documented in ``run_loop``.
        """
        from .run_loop import run_coroutine

        return run_coroutine(
            lambda: self.fetch_for_scene_async(
                scene_duration_s, list(queries or []), exclude_asset_ids
            )
        )

    async def fetch_for_scene_async(
        self,
        scene_duration_s: float,
        queries: list[str],
        exclude_asset_ids: set[str] | None = None,
    ) -> StockSceneAsset | None:
        """Async core of :meth:`fetch_for_scene`."""
        excluded = self._normalise_exclusions(exclude_asset_ids)
        clean_queries = [q.strip() for q in (queries or []) if q and q.strip()]
        if not clean_queries:
            return None
        # Phase 1: videos across the whole ladder.
        # Phase 2: photos across the whole ladder (only if no video).
        for kind in ("video", "photo"):
            for query in clean_queries:
                candidates = await self._search(query, kind)
                passed = self._filter(
                    candidates,
                    kind=kind,
                    scene_duration_s=scene_duration_s,
                    excluded=excluded,
                )
                if not passed:
                    continue
                ranked = self._rank(passed, query)
                for cand in ranked:
                    asset = await self._materialise(cand)
                    if asset is None:
                        continue
                    # SL-006 brightness selection filter: a measured
                    # near-black candidate is rejected HERE, at
                    # selection time — excluded for the rest of the
                    # run and never handed to the visuals stage.
                    # Measurement runs off-loop (subprocess ffmpeg).
                    verdict = await asyncio.to_thread(
                        self.brightness_filter.check,
                        asset.local_path,
                        asset_id=cand.provider_asset_id,
                    )
                    if verdict.too_dark:
                        excluded.add(cand.provider_asset_id)
                        excluded.add(f"{PROVIDER_NAME}:{cand.provider_asset_id}")
                        continue
                    return asset
                # This query produced candidates but every download
                # failed: keep walking the ladder rather than giving up.
        return None

    # -- exclusions / filters --------------------------------------------

    @staticmethod
    def _normalise_exclusions(exclude_asset_ids: Iterable[str] | None) -> set[str]:
        out: set[str] = set()
        for raw in exclude_asset_ids or ():
            value = str(raw).strip()
            if not value:
                continue
            out.add(value)
            if ":" in value:  # "pixabay:123" also excludes bare "123"
                out.add(value.split(":", 1)[-1])
        return out

    @staticmethod
    def _has_brand_or_logo(cand: _Cand) -> bool:
        if cand.trademark_or_logo:
            return True
        haystack: set[str] = set()
        for tag in cand.tags:
            haystack |= _tokens(tag)
        haystack |= _tokens(cand.title)
        return bool(haystack & _BRAND_WORDS)

    def _passes(self, cand: _Cand, *, kind: str, scene_duration_s: float,
                excluded: set[str]) -> bool:
        if cand.provider_asset_id in excluded:
            return False
        if cand.is_nsfw:  # safesearch on
            return False
        if self._has_brand_or_logo(cand):
            return False
        if cand.width is None or cand.height is None:
            return False
        if not cand.width > cand.height:  # landscape, strictly
            return False
        if kind == "video":
            if cand.width < _VIDEO_MIN_WIDTH:
                return False
            if cand.duration_s is None or cand.duration_s < scene_duration_s:
                return False
        else:  # photo
            if cand.width < _PHOTO_MIN_WIDTH:
                return False
        return True

    def _filter(self, candidates: list[_Cand], *, kind: str,
                scene_duration_s: float, excluded: set[str]) -> list[_Cand]:
        passed = [
            c for c in candidates
            if self._passes(c, kind=kind, scene_duration_s=scene_duration_s,
                            excluded=excluded)
        ]
        if kind == "photo":
            # >=1920 preferred: if this query yielded any, narrower
            # photos from the same query are not considered.
            wide = [c for c in passed if (c.width or 0) >= _PHOTO_PREFERRED_WIDTH]
            if wide:
                passed = wide
        return passed

    # -- ranking -----------------------------------------------------------

    @staticmethod
    def _relevance(cand: _Cand, query: str, total: int) -> float:
        # Relevance = query/tag+title match. The adapter returns its
        # results relevance-ordered, and that order correlates with this
        # match; scoring it directly (instead of raw position) is what
        # lets freshness decide among near-equal relevance and lets the
        # deterministic asset-id tie-break apply on exact score ties.
        tokens = _tokens(query)
        if not tokens:
            return 0.5
        haystack: set[str] = set()
        for tag in cand.tags:
            haystack |= _tokens(tag)
        haystack |= _tokens(cand.title)
        if not haystack:
            return 0.0
        return len(tokens & haystack) / len(tokens)

    def _rank(self, candidates: list[_Cand], query: str) -> list[_Cand]:
        downloads = [c.downloads for c in candidates if c.downloads is not None]
        max_dl = max(downloads) if downloads else None

        def score(cand: _Cand) -> float:
            if cand.downloads is None or not max_dl:
                freshness = 0.5
            else:
                freshness = 1.0 - (cand.downloads / max_dl)
            return 0.65 * self._relevance(cand, query, len(candidates)) + 0.35 * freshness

        # Deterministic: score desc, then provider_asset_id asc.
        return sorted(candidates, key=lambda c: (-score(c), c.provider_asset_id))

    # -- search (adapter + optional 24h file cache) -------------------------

    async def _search(self, query: str, kind: str) -> list[_Cand]:
        media_value = "video" if kind == "video" else "photo"
        cache_key = None
        if not self._adapter_has_search_cache:
            cache_key = self._search_cache_key(query, media_value)
            cached = self._read_search_cache(cache_key)
            if cached is not None:
                return [
                    c for c in
                    (_normalise_candidate(row, kind, i) for i, row in enumerate(cached))
                    if c is not None
                ]
        raw_candidates: list[Any] = []
        try:
            raw_candidates = await self._call_adapter_search(query, kind)
        except Exception as exc:  # provider outage -> no result, never raise
            logger.warning(
                "stock_fetch: pixabay search failed (query=%r kind=%s): %s: %s",
                query, kind, type(exc).__name__, exc,
            )
            return []
        normalised = [
            c for c in
            (_normalise_candidate(obj, kind, i) for i, obj in enumerate(raw_candidates or []))
            if c is not None
        ]
        if cache_key is not None:
            self._write_search_cache(cache_key, normalised)
        return normalised

    async def _call_adapter_search(self, query: str, kind: str) -> list[Any]:
        """Call the adapter in its native shape, tolerating variants."""
        search = getattr(self._adapter, "search", None)
        if search is None:
            raise TypeError("stock adapter has no search() method")
        from ..providers.stock.models import MediaType, SearchFilters

        media_type = MediaType.VIDEO if kind == "video" else MediaType.PHOTO
        filters = SearchFilters(
            media_type=media_type,
            orientation="landscape",
            safesearch=True,
            per_page=80,
            page=1,
        )
        attempts = (
            (query, media_type, filters),
            (query, media_type),
            (query, kind),
            (query,),
        )
        last_type_error: TypeError | None = None
        for args in attempts:
            try:
                result = search(*args)
                if inspect.isawaitable(result):
                    result = await result
                return list(result or [])
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        return []

    # file search cache (only for adapters lacking their own) ---------
    def _search_cache_key(self, query: str, media_value: str) -> str:
        payload = json.dumps(
            {
                "provider": PROVIDER_NAME,
                "query": " ".join(query.lower().strip().split()),
                "media_type": media_value,
                "orientation": "landscape",
                "safesearch": True,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_search_cache(self, key: str) -> list[dict] | None:
        path = self.search_cache_dir / f"{key}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - float(doc.get("stored_at", 0)) >= _SEARCH_CACHE_TTL_S:
            return None
        rows = doc.get("candidates")
        return rows if isinstance(rows, list) else None

    def _write_search_cache(self, key: str, candidates: list[_Cand]) -> None:
        try:
            self.search_cache_dir.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "provider_asset_id": c.provider_asset_id,
                    "width": c.width,
                    "height": c.height,
                    "duration_s": c.duration_s,
                    "downloads": c.downloads,
                    "tags": c.tags,
                    "title": c.title,
                    "page_url": c.page_url,
                    "download_url": c.download_url,
                    "is_nsfw": c.is_nsfw,
                    "trademark_or_logo": c.trademark_or_logo,
                    # Tier data so a cache round-trip re-selects the
                    # same tier instead of the adapter's largest.
                    "download_tiers": c.download_tiers,
                    "tier_quality": c.tier_quality,
                    "tier_fallback": c.tier_fallback,
                }
                for c in candidates
            ]
            path = self.search_cache_dir / f"{key}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"stored_at": self._clock(), "candidates": rows}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning("stock_fetch: search cache write failed: %s", exc)

    # -- download + content-addressed cache --------------------------------

    def _index_path(self) -> Path:
        return self.cache_dir / "index.json"

    def _read_index(self) -> dict[str, dict]:
        try:
            doc = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write_index(self, index: dict[str, dict]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._index_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # -- cache cap / LRU eviction (Round 4) ------------------------------

    def _enforce_cache_cap(self) -> None:
        """Evict least-recently-used cache entries until within the cap.

        Index-driven: only files referenced by ``index.json`` are
        managed (orphans are left alone), a content file shared by
        several entries is unlinked only when its last entry goes, and
        entries whose path was handed out during this fetcher's
        lifetime (``_active_paths`` — the current run) are never
        evicted.  The index is rewritten atomically after eviction;
        ``index.json`` itself is never deleted.  Best-effort: failures
        are logged, never raised into the fetch path.
        """
        cap = self.cache_max_bytes
        if not cap or cap <= 0:
            return
        try:
            index = self._read_index()
            # Group entries by content file; a shared file's size is
            # counted once, its last_access is the newest of its entries.
            files: dict[str, dict] = {}
            for asset_id, entry in index.items():
                path = str(entry.get("local_path", ""))
                if not path:
                    continue
                size = _as_int(entry.get("size_bytes"))
                if not size:
                    try:
                        size = Path(path).stat().st_size
                    except OSError:
                        size = 0
                info = files.setdefault(
                    path, {"size": size, "last_access": 0.0, "ids": []}
                )
                info["ids"].append(asset_id)
                info["last_access"] = max(
                    info["last_access"], _as_float(entry.get("last_access")) or 0.0
                )
            total = sum(info["size"] for info in files.values())
            if total <= cap:
                return
            evictable = sorted(
                (
                    (info["last_access"], path)
                    for path, info in files.items()
                    if path not in self._active_paths
                ),
                key=lambda item: (item[0], item[1]),  # LRU first, deterministic
            )
            evicted: list[str] = []
            for _last_access, path in evictable:
                if total <= cap:
                    break
                info = files[path]
                for asset_id in info["ids"]:
                    index.pop(asset_id, None)
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "stock_fetch: cache eviction unlink failed (%s): %s",
                        path, exc,
                    )
                total -= info["size"]
                evicted.append(path)
            if evicted:
                self._write_index(index)
                logger.info(
                    "stock_fetch: cache cap %d bytes: evicted %d file(s), "
                    "indexed total now %d bytes",
                    cap, len(evicted), total,
                )
        except OSError as exc:
            logger.warning("stock_fetch: cache cap enforcement failed: %s", exc)

    @staticmethod
    def _extension(cand: _Cand) -> str:
        suffix = Path(urlparse(cand.download_url).path).suffix.lower()
        allowed = _VIDEO_EXTS if cand.kind == "video" else _PHOTO_EXTS
        if suffix in allowed:
            return suffix
        return ".mp4" if cand.kind == "video" else ".jpg"

    async def _materialise(self, cand: _Cand) -> StockSceneAsset | None:
        """Return the asset, downloading only on a cache/index miss."""
        # Index hit: repeat fetch of this asset performs NO download.
        index = self._read_index()
        entry = index.get(cand.provider_asset_id)
        if entry:
            cached_path = Path(str(entry.get("local_path", "")))
            digest = str(entry.get("file_sha256", ""))
            if digest and cached_path.is_file() and cached_path.name.startswith(digest):
                # LRU bookkeeping: a hit refreshes last_access (and
                # backfills size_bytes for pre-Round-4 entries).
                entry["last_access"] = self._clock()
                if not entry.get("size_bytes"):
                    try:
                        entry["size_bytes"] = cached_path.stat().st_size
                    except OSError:
                        pass
                try:
                    self._write_index(index)
                except OSError as exc:
                    logger.warning("stock_fetch: index touch failed: %s", exc)
                self._active_paths.add(str(cached_path))
                return StockSceneAsset(
                    local_path=str(cached_path),
                    provider=PROVIDER_NAME,
                    provider_asset_id=cand.provider_asset_id,
                    asset_page_url=str(entry.get("asset_page_url") or cand.page_url),
                    download_url=str(entry.get("download_url") or cand.download_url),
                    licence_name=LICENCE_NAME,
                    terms_hash=TERMS_HASH,
                    file_sha256=digest,
                    width=int(entry.get("width") or cand.width or 0),
                    height=int(entry.get("height") or cand.height or 0),
                    duration_s=(
                        _as_float(entry.get("duration_s"))
                        if entry.get("duration_s") is not None
                        else cand.duration_s
                    ) if cand.kind == "video" else None,
                    kind=cand.kind,
                    tier_quality=str(entry.get("tier_quality") or cand.tier_quality),
                    tier_fallback=bool(entry.get("tier_fallback", cand.tier_fallback)),
                )
        # Download the bytes.
        try:
            data = await self._download_bytes(cand)
        except Exception as exc:  # outage / 404 -> try next candidate
            logger.warning(
                "stock_fetch: download failed (asset=%s): %s: %s",
                cand.provider_asset_id, type(exc).__name__, exc,
            )
            return None
        if not data:
            logger.warning(
                "stock_fetch: empty download for asset %s", cand.provider_asset_id
            )
            return None
        digest = hashlib.sha256(data).hexdigest()
        ext = self._extension(cand)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        local_path = self.cache_dir / f"{digest}{ext}"
        if not local_path.is_file():
            tmp = self.cache_dir / f".{digest}.part"
            tmp.write_bytes(data)
            tmp.replace(local_path)
        asset = StockSceneAsset(
            local_path=str(local_path),
            provider=PROVIDER_NAME,
            provider_asset_id=cand.provider_asset_id,
            asset_page_url=cand.page_url,
            download_url=cand.download_url,
            licence_name=LICENCE_NAME,
            terms_hash=TERMS_HASH,
            file_sha256=digest,
            width=int(cand.width or 0),
            height=int(cand.height or 0),
            duration_s=cand.duration_s if cand.kind == "video" else None,
            kind=cand.kind,
            tier_quality=cand.tier_quality,
            tier_fallback=cand.tier_fallback,
        )
        if cand.tier_fallback:
            logger.info(
                "stock_fetch: asset %s has no >=%dpx tier; fell back to "
                "largest tier %r (%sx%s)",
                cand.provider_asset_id, _TIER_ADEQUATE_WIDTH,
                cand.tier_quality, asset.width, asset.height,
            )
        index[cand.provider_asset_id] = {
            "file_sha256": digest,
            "local_path": str(local_path),
            "asset_page_url": asset.asset_page_url,
            "download_url": asset.download_url,
            "width": asset.width,
            "height": asset.height,
            "duration_s": asset.duration_s,
            "kind": asset.kind,
            "tier_quality": asset.tier_quality,
            "tier_fallback": asset.tier_fallback,
            "size_bytes": len(data),
            "last_access": self._clock(),
        }
        try:
            self._write_index(index)
        except OSError as exc:
            logger.warning("stock_fetch: index write failed: %s", exc)
        self._active_paths.add(str(local_path))
        self._enforce_cache_cap()
        return asset

    async def _download_bytes(self, cand: _Cand) -> bytes:
        # 1. Injected downloader (tests / alternative transports).
        if self._downloader is not None:
            result = self._downloader(cand.download_url)
            if inspect.isawaitable(result):
                result = await result
            return bytes(result or b"")
        # 2. Adapter download hooks.
        adapter = self._adapter
        download_bytes = getattr(adapter, "download_bytes", None)
        if callable(download_bytes):
            result = download_bytes(cand.raw if cand.raw is not None else cand)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, (bytes, bytearray)) and result:
                return bytes(result)
        download = getattr(adapter, "download", None)
        # The real Pixabay adapter's download() fetches internally but
        # returns only metadata (no bytes, no real file), so for it we
        # go straight to the CDN URL below instead of double-fetching.
        from ..providers.stock.pixabay import PixabayProvider

        if callable(download) and not isinstance(adapter, PixabayProvider):
            result = download(cand.raw if cand.raw is not None else cand)
            if inspect.isawaitable(result):
                result = await result
            extracted = self._extract_bytes(result)
            if extracted:
                return extracted
        # 3. Direct fetch of the candidate's download URL (Pixabay CDN
        #    URLs are public; no key is involved or logged).
        #    MUST go through the shared provider HTTP factory: a bare
        #    httpx.AsyncClient() (trust_env=True) dies at construction on
        #    this sandbox's bracketed-IPv6 NO_PROXY entries with
        #    httpx.InvalidURL "Invalid port: ':1]'" — the Round 3 bug
        #    that broke every stock download while searches (which use
        #    the factory via the adapter) worked. See
        #    app.providers.http_client for the full explanation.
        if not cand.download_url:
            raise ValueError(f"candidate {cand.provider_asset_id} has no download_url")
        from ..providers.http_client import create_async_client

        async with create_async_client(timeout=60.0) as client:
            resp = await client.get(cand.download_url)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def _extract_bytes(result: Any) -> bytes:
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if result is None:
            return b""
        for attr in ("content", "data", "bytes"):
            value = getattr(result, attr, None)
            if isinstance(value, (bytes, bytearray)) and value:
                return bytes(value)
        local_path = getattr(result, "local_path", None)
        if isinstance(local_path, (str, Path)):
            path = Path(local_path)
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path.read_bytes()
            except OSError:
                return b""
        return b""
