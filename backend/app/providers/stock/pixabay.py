"""Pixabay adapter — FIRST / shipping default (§24).

Free key obtainable today; 100 req/60s, no monthly cap; results MUST be
cached 24h (their terms) — enforced here via the shared L1 SearchCache.
"""
from __future__ import annotations

import hashlib
from datetime import timezone

import httpx

from ..http_client import create_async_client
from .base import ProviderUnavailableError, SecretProvider, StockProvider
from .cache import SearchCache
from .circuit_breaker import CircuitBreaker
from .licences import pixabay_licence
from .models import Candidate, DownloadedAsset, MediaType, SearchFilters, SensitiveFlags, utcnow
from .rate_limit import SlidingWindowRateLimiter

VIDEO_URL = "https://pixabay.com/api/videos/"
IMAGE_URL = "https://pixabay.com/api/"

#: Internal ``SearchFilters.orientation`` vocabulary -> Pixabay's.
#: Pixabay accepts only ``all`` / ``horizontal`` / ``vertical``; sending
#: the internal ``landscape`` verbatim is not a documented value.
_ORIENTATION_MAP = {
    "landscape": "horizontal",
    "horizontal": "horizontal",
    "portrait": "vertical",
    "vertical": "vertical",
    "square": "all",
    "all": "all",
}


def _pixabay_orientation(orientation: str) -> str:
    return _ORIENTATION_MAP.get((orientation or "").strip().lower(), "all")


def _parse_tags(raw: str | list | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


class PixabayProvider(StockProvider):
    name = "pixabay"
    reliability = 0.90
    max_per_page = 200  # Pixabay API maximum

    def __init__(
        self,
        secret_provider: SecretProvider,
        key_ref: str = "pixabay_api_key",
        *,
        http_client: httpx.AsyncClient | None = None,
        cache: SearchCache | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self._secret_provider = secret_provider
        self._key_ref = key_ref
        self._client = http_client
        self._owns_client = http_client is None
        self.cache = cache if cache is not None else SearchCache(ttl_hours=24.0)
        # 100 requests / 60s, no monthly cap.
        self.rate_limiter = rate_limiter if rate_limiter is not None else SlidingWindowRateLimiter(100, 60.0)
        self.circuit_breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker("pixabay", threshold=3)

    @property
    def key_ref(self) -> str:
        return self._key_ref

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Shared factory (app.providers.http_client): trust_env=False
            # plus a sanitised read of the proxy env — raw httpx
            # construction dies with ``InvalidURL: ':1]'`` on
            # bracketed-IPv6 NO_PROXY entries before any request is sent,
            # and a bare trust_env=False client cannot leave sandboxes
            # whose egress requires the env proxy.
            self._client = create_async_client(timeout=30.0)
        return self._client

    async def is_available(self) -> bool:
        if self.circuit_breaker.is_open:
            return False
        secret = await self._secret_provider.get_secret(self._key_ref)
        return bool(secret)

    async def test_connection(self, *, timeout_s: float = 10.0) -> dict:
        """Cheapest authenticated call for the test-connection flow (§28).

        ONE minimal image search (``per_page=3``, Pixabay's minimum)
        that deliberately bypasses the 24h search cache and the circuit
        breaker — a cached or short-circuited result would prove
        nothing about the key. Unlike :meth:`search`, failures are NOT
        swallowed: HTTP errors raise ``httpx.HTTPStatusError`` and
        transport failures propagate as ``httpx`` exceptions so the
        caller can map them onto the §28 error taxonomy. The key travels
        only as Pixabay's required ``key`` query param and is never
        logged (this module has no logger for exactly that reason).
        """
        secret = await self._secret_provider.get_secret(self._key_ref)
        if not secret:
            raise ProviderUnavailableError("pixabay: API key not available")
        client = self._client
        owns = client is None
        if owns:
            client = create_async_client(timeout=timeout_s)
        try:
            resp = await client.get(
                IMAGE_URL,
                params={
                    "key": secret,
                    "q": "nature",
                    "per_page": 3,
                    "image_type": "photo",
                    "safesearch": "true",
                },
            )
        finally:
            if owns:
                await client.aclose()
        resp.raise_for_status()
        payload = resp.json()
        info: dict = {
            "total_hits": payload.get("totalHits", payload.get("total")),
            "returned": len(payload.get("hits") or []),
        }
        for header, field in (
            ("x-ratelimit-limit", "rate_limit_limit"),
            ("x-ratelimit-remaining", "rate_limit_remaining"),
        ):
            raw = resp.headers.get(header)
            if raw is not None:
                try:
                    info[field] = int(raw)
                except ValueError:
                    pass
        return info

    async def search(
        self,
        query: str,
        media_type: MediaType,
        filters: SearchFilters | None = None,
    ) -> list[Candidate]:
        filters = filters or SearchFilters(media_type=media_type)
        if self.circuit_breaker.is_open:
            return []
        # L1 cache first — a hit costs zero API calls (Pixabay 24h rule).
        cached = self.cache.get(self.name, query, filters)
        if cached is not None:
            return cached
        secret = await self._secret_provider.get_secret(self._key_ref)
        if not secret:
            return []  # unavailable is normal; the chain proceeds
        await self.rate_limiter.acquire()
        is_video = media_type == MediaType.VIDEO
        # Honour the caller's page size, clamped to Pixabay's documented
        # range (3..200). The sourcing layer asks for the provider maximum
        # (§24: one call per ladder step at MAX page size); direct callers
        # get exactly what they asked for within range.
        per_page = min(max(int(filters.per_page or self.max_per_page), 3), self.max_per_page)
        params = {
            # Sent to Pixabay only, as its required ``key`` query param.
            # NEVER log these params or the built request URL: the URL
            # embeds the key. This module has no logger for that reason,
            # and HTTP errors below are swallowed without the exception
            # (whose message can carry the request URL) being emitted.
            "key": secret,
            "q": query,
            "per_page": per_page,
            "page": filters.page,
            "safesearch": "true" if filters.safesearch else "false",
            # Both endpoints accept orientation (mapped to Pixabay's
            # horizontal/vertical vocabulary) and min_width.
            "orientation": _pixabay_orientation(filters.orientation),
            "min_width": filters.effective_min_width(),
        }
        if not is_video:
            params["image_type"] = "photo"
        url = VIDEO_URL if is_video else IMAGE_URL
        try:
            resp = await self._ensure_client().get(url, params=params)
        except httpx.HTTPError:
            self.circuit_breaker.record_failure()
            return []
        if resp.status_code == 429 or resp.status_code >= 500:
            self.circuit_breaker.record_failure()
            return []  # outage outcome; breaker trips after a storm
        if resp.status_code != 200:
            return []
        self.circuit_breaker.record_success()
        try:
            payload = resp.json()
        except ValueError:
            return []
        hits = payload.get("hits") or []
        licence = pixabay_licence()
        now = utcnow()
        candidates = [
            self._parse_video_hit(h, licence.licence_id, now) if is_video
            else self._parse_image_hit(h, licence.licence_id, now)
            for h in hits
        ]
        self.cache.put(self.name, query, filters, candidates)
        return candidates

    def _parse_video_hit(self, hit: dict, licence_id: str, now) -> Candidate:
        tiers: list[dict] = []
        videos = hit.get("videos") or {}
        if isinstance(videos, dict) and videos:
            for quality in ("large", "medium", "small", "tiny"):
                tier = videos.get(quality)
                if tier and tier.get("url"):
                    tiers.append({
                        "quality": quality, "url": tier["url"],
                        "width": tier.get("width"), "height": tier.get("height"),
                        "fps": tier.get("fps"),
                    })
        else:  # tolerate a flat video_files-style fixture
            for f in hit.get("video_files") or []:
                tiers.append({
                    "quality": f.get("quality", ""), "url": f.get("link") or f.get("url"),
                    "width": f.get("width"), "height": f.get("height"), "fps": f.get("fps"),
                })
        tiers = [t for t in tiers if t.get("url")]
        tiers.sort(key=lambda t: (t.get("width") or 0), reverse=True)
        best = tiers[0] if tiers else {}
        user = hit.get("user") or ""
        user_id = hit.get("user_id")
        return Candidate(
            provider=self.name,
            provider_asset_id=str(hit.get("id")),
            media_type=MediaType.VIDEO,
            preview_url=hit.get("preview_url") or hit.get("image") or (tiers[-1]["url"] if tiers else ""),
            download_url=best.get("url", ""),
            download_tiers=tiers,
            duration_s=float(hit["duration"]) if hit.get("duration") is not None else None,
            width=best.get("width") or hit.get("width"),
            height=best.get("height") or hit.get("height"),
            fps=float(best["fps"]) if best.get("fps") is not None else (float(hit["fps"]) if hit.get("fps") is not None else None),
            licence_id=licence_id,
            author_name=user,
            author_url=f"https://pixabay.com/users/{user}-{user_id}/" if user and user_id else "",
            page_url=hit.get("pageURL") or hit.get("page_url") or "",
            tags=_parse_tags(hit.get("tags")),
            retrieved_at=now,
            downloads=hit.get("downloads"),
            views=hit.get("views"),
            likes=hit.get("likes"),
        )

    def _parse_image_hit(self, hit: dict, licence_id: str, now) -> Candidate:
        user = hit.get("user") or ""
        user_id = hit.get("user_id")
        return Candidate(
            provider=self.name,
            provider_asset_id=str(hit.get("id")),
            media_type=MediaType.PHOTO,
            preview_url=hit.get("previewURL") or hit.get("webformatURL") or "",
            download_url=hit.get("largeImageURL") or hit.get("webformatURL") or "",
            download_tiers=[{"quality": "large", "url": hit.get("largeImageURL") or hit.get("webformatURL") or "",
                             "width": hit.get("imageWidth"), "height": hit.get("imageHeight")}],
            duration_s=None,
            width=hit.get("imageWidth") or hit.get("webformatWidth"),
            height=hit.get("imageHeight") or hit.get("webformatHeight"),
            fps=None,
            licence_id=licence_id,
            author_name=user,
            author_url=f"https://pixabay.com/users/{user}-{user_id}/" if user and user_id else "",
            page_url=hit.get("pageURL") or "",
            tags=_parse_tags(hit.get("tags")),
            retrieved_at=now,
            downloads=hit.get("downloads"),
            views=hit.get("views"),
            likes=hit.get("likes"),
        )

    async def download(self, candidate: Candidate) -> DownloadedAsset:
        data = b""
        if candidate.download_url:
            resp = await self._ensure_client().get(candidate.download_url)
            resp.raise_for_status()
            data = resp.content
        digest = hashlib.sha256(data or candidate.asset_key.encode()).hexdigest()
        return DownloadedAsset(
            candidate=candidate,
            local_path=f"cache/pixabay/{candidate.provider_asset_id}",
            file_sha256=digest,
            width=candidate.width,
            height=candidate.height,
            duration_s=candidate.duration_s,
            fps=candidate.fps,
        )
