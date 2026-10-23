"""Pexels adapter — OPTIONAL co-primary for existing key holders only (§24).

CRITICAL PRODUCT FACT: Pexels paused NEW API-key issuance on 2026-09-30.
This adapter is never an onboarding requirement: with no key configured it
reports unavailable and the failover chain proceeds on Pixabay alone.
Never advise multi-accounting to multiply quota.

Quota state is read from X-Ratelimit-* headers on SUCCESSFUL responses
only — those headers are ABSENT on 429 responses, so a 429 must not
overwrite the last known quota state.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from ..http_client import create_async_client
from .base import ProviderUnavailableError, SecretProvider, StockProvider
from .cache import SearchCache
from .circuit_breaker import CircuitBreaker
from .licences import pexels_licence
from .models import Candidate, DownloadedAsset, MediaType, SearchFilters, utcnow
from .rate_limit import SlidingWindowRateLimiter

VIDEO_URL = "https://api.pexels.com/videos/search"
PHOTO_URL = "https://api.pexels.com/v1/search"


@dataclass
class PexelsQuota:
    """Default limits: 200 req/hour, 20,000/month (increases also paused)."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None  # unix timestamp from X-Ratelimit-Reset
    monthly_limit: int = 20_000
    hourly_limit: int = 200


class PexelsProvider(StockProvider):
    name = "pexels"
    reliability = 0.95
    max_per_page = 80  # Pexels API maximum

    def __init__(
        self,
        secret_provider: SecretProvider,
        key_ref: str = "pexels_api_key",
        *,
        http_client: httpx.AsyncClient | None = None,
        cache: SearchCache | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self._secret_provider = secret_provider
        self._key_ref = key_ref
        self._client = http_client
        self.cache = cache if cache is not None else SearchCache(ttl_hours=24.0)
        self.rate_limiter = rate_limiter if rate_limiter is not None else SlidingWindowRateLimiter(200, 3600.0)
        self.circuit_breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker("pexels", threshold=3)
        self.quota = PexelsQuota()

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

        ONE minimal photo search (``per_page=1``) bypassing the cache and
        circuit breaker (either would prove nothing about the key).
        Unlike :meth:`search`, failures are NOT swallowed: HTTP errors
        raise ``httpx.HTTPStatusError`` and transport failures propagate
        as ``httpx`` exceptions for §28 taxonomy mapping by the caller.
        On success the rate-limit headers ARE read (they are present on
        successful responses) and folded into the returned info.
        """
        secret = await self._secret_provider.get_secret(self._key_ref)
        if not secret:
            raise ProviderUnavailableError("pexels: API key not available")
        client = self._client
        owns = client is None
        if owns:
            client = create_async_client(timeout=timeout_s)
        try:
            resp = await client.get(
                PHOTO_URL,
                params={"query": "nature", "per_page": 1},
                headers={"Authorization": secret},  # never logged
            )
        finally:
            if owns:
                await client.aclose()
        resp.raise_for_status()
        self._update_quota_from_headers(resp.headers)
        payload = resp.json()
        return {
            "total_results": payload.get("total_results"),
            "returned": len(payload.get("photos") or []),
            "rate_limit_limit": self.quota.limit,
            "rate_limit_remaining": self.quota.remaining,
        }

    def _update_quota_from_headers(self, headers) -> None:
        """Call ONLY on successful responses; headers are absent on 429."""
        limit = headers.get("x-ratelimit-limit")
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if limit is not None:
            try:
                self.quota.limit = int(limit)
            except ValueError:
                pass
        if remaining is not None:
            try:
                self.quota.remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self.quota.reset = int(float(reset))
            except ValueError:
                pass

    async def search(
        self,
        query: str,
        media_type: MediaType,
        filters: SearchFilters | None = None,
    ) -> list[Candidate]:
        filters = filters or SearchFilters(media_type=media_type)
        if self.circuit_breaker.is_open:
            return []
        cached = self.cache.get(self.name, query, filters)
        if cached is not None:
            return cached
        secret = await self._secret_provider.get_secret(self._key_ref)
        if not secret:
            return []  # no key -> unavailable, chain proceeds (never an error)
        await self.rate_limiter.acquire()
        is_video = media_type == MediaType.VIDEO
        # Honour the caller's page size, clamped to Pexels' maximum (80).
        # The sourcing layer asks for the provider maximum (§24); Pexels'
        # own vocabulary for orientation IS landscape/portrait/square,
        # so it passes through verbatim (unlike Pixabay's).
        per_page = min(max(int(filters.per_page or self.max_per_page), 1), self.max_per_page)
        params = {
            "query": query,
            "per_page": per_page,
            "page": filters.page,
            "orientation": filters.orientation,
        }
        if is_video:
            # Pexels video search supports server-side size floors.
            params["min_width"] = filters.effective_min_width()
        url = VIDEO_URL if is_video else PHOTO_URL
        try:
            resp = await self._ensure_client().get(
                url, params=params, headers={"Authorization": secret}  # never logged
            )
        except httpx.HTTPError:
            self.circuit_breaker.record_failure()
            return []
        if resp.status_code == 429 or resp.status_code >= 500:
            # Deliberately do NOT read quota headers here: absent on 429.
            self.circuit_breaker.record_failure()
            return []
        if resp.status_code != 200:
            return []
        self.circuit_breaker.record_success()
        self._update_quota_from_headers(resp.headers)
        try:
            payload = resp.json()
        except ValueError:
            return []
        licence = pexels_licence()
        now = utcnow()
        if is_video:
            candidates = [self._parse_video(v, licence.licence_id, now) for v in payload.get("videos") or []]
        else:
            candidates = [self._parse_photo(p, licence.licence_id, now) for p in payload.get("photos") or []]
        self.cache.put(self.name, query, filters, candidates)
        return candidates

    def _parse_video(self, video: dict, licence_id: str, now) -> Candidate:
        tiers = []
        for f in video.get("video_files") or []:
            if f.get("link"):
                tiers.append({
                    "quality": f.get("quality", ""), "url": f["link"],
                    "width": f.get("width"), "height": f.get("height"), "fps": f.get("fps"),
                })
        tiers.sort(key=lambda t: (t.get("width") or 0), reverse=True)
        best = tiers[0] if tiers else {}
        user = video.get("user") or {}
        tags = video.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return Candidate(
            provider=self.name,
            provider_asset_id=str(video.get("id")),
            media_type=MediaType.VIDEO,
            preview_url=video.get("image") or "",
            download_url=best.get("url", ""),
            download_tiers=tiers,
            duration_s=float(video["duration"]) if video.get("duration") is not None else None,
            width=best.get("width") or video.get("width"),
            height=best.get("height") or video.get("height"),
            fps=float(best["fps"]) if best.get("fps") is not None else None,
            licence_id=licence_id,
            author_name=user.get("name", "") if isinstance(user, dict) else "",
            author_url=user.get("url", "") if isinstance(user, dict) else "",
            page_url=video.get("url") or video.get("link") or "",
            tags=list(tags),
            title=video.get("alt") or "",
            retrieved_at=now,
            downloads=None,  # Pexels API does not expose download counts
        )

    def _parse_photo(self, photo: dict, licence_id: str, now) -> Candidate:
        src = photo.get("src") or {}
        download_url = src.get("original") or src.get("large2x") or src.get("large") or ""
        return Candidate(
            provider=self.name,
            provider_asset_id=str(photo.get("id")),
            media_type=MediaType.PHOTO,
            preview_url=src.get("large") or src.get("medium") or "",
            download_url=download_url,
            download_tiers=[{"quality": "original", "url": download_url,
                             "width": photo.get("width"), "height": photo.get("height")}],
            duration_s=None,
            width=photo.get("width"),
            height=photo.get("height"),
            fps=None,
            licence_id=licence_id,
            author_name=photo.get("photographer") or "",
            author_url=photo.get("photographer_url") or "",
            page_url=photo.get("url") or "",
            tags=[photo["alt"]] if photo.get("alt") else [],
            title=photo.get("alt") or "",
            retrieved_at=now,
            downloads=None,
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
            local_path=f"cache/pexels/{candidate.provider_asset_id}",
            file_sha256=digest,
            width=candidate.width,
            height=candidate.height,
            duration_s=candidate.duration_s,
            fps=candidate.fps,
        )
