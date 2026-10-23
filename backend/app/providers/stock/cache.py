"""Caching layers (§24): L1 search-response cache + L3 negative cache.

L1: metadata only, key = provider + normalised query + filters + page,
TTL 24h for both free providers (Pixabay terms REQUIRE 24h caching).
L3: query+provider with 0 usable results remembered 7 days per niche.
Asset files on disk (L2) are owned by the download/asset-library layer;
this module is the metadata side the adapters consult before any HTTP call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from .models import Candidate, SearchFilters, normalise_query

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SearchCache:
    """L1 search-response cache (24h TTL)."""

    def __init__(self, ttl_hours: float = 24.0, *, clock: Clock = _utcnow):
        self.ttl = timedelta(hours=ttl_hours)
        self._clock = clock
        self._store: dict[tuple, tuple[datetime, list[Candidate]]] = {}

    @staticmethod
    def make_key(provider: str, query: str, filters: SearchFilters) -> tuple:
        return (provider, normalise_query(query), filters.cache_key_parts())

    def get(self, provider: str, query: str, filters: SearchFilters) -> list[Candidate] | None:
        key = self.make_key(provider, query, filters)
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, candidates = entry
        if self._clock() - stored_at >= self.ttl:
            del self._store[key]
            return None
        return list(candidates)

    def put(self, provider: str, query: str, filters: SearchFilters, candidates: list[Candidate]) -> None:
        key = self.make_key(provider, query, filters)
        self._store[key] = (self._clock(), list(candidates))

    def __len__(self) -> int:
        return len(self._store)


class NegativeCache:
    """L3 negative cache: 0-usable-result query+provider, 7 days per niche."""

    def __init__(self, ttl_days: float = 7.0, *, clock: Clock = _utcnow):
        self.ttl = timedelta(days=ttl_days)
        self._clock = clock
        self._store: dict[tuple, datetime] = {}

    @staticmethod
    def make_key(provider: str, query: str, niche: str = "", media_type: str = "") -> tuple:
        return (provider, normalise_query(query), (niche or "").strip().lower(), media_type or "")

    def record(self, provider: str, query: str, niche: str = "", *, media_type: str = "") -> None:
        self._store[self.make_key(provider, query, niche, media_type)] = self._clock()

    # Alias matching the task wording.
    def record_negative(self, provider: str, query: str, niche: str = "", *, media_type: str = "") -> None:
        self.record(provider, query, niche, media_type=media_type)

    def is_negative(self, provider: str, query: str, niche: str = "", *, media_type: str = "") -> bool:
        key = self.make_key(provider, query, niche, media_type)
        stored_at = self._store.get(key)
        if stored_at is None:
            return False
        if self._clock() - stored_at >= self.ttl:
            del self._store[key]
            return False
        return True

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: tuple) -> bool:
        if len(key) == 3:
            return self.is_negative(key[0], key[1], key[2])
        if len(key) == 4:
            return self.is_negative(key[0], key[1], key[2], media_type=key[3])
        return False
