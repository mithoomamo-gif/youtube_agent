"""Core data models for the stock-media layer (§24).

Only the pipeline (Agents 15/16 equivalents) calls this layer. Models are
plain dataclasses so adapters, caches, ranking and the ledger all share one
vocabulary without a hard dependency on any web framework.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalise_query(query: str) -> str:
    """Normalise a search query for cache keys (trim + collapse + lower).

    Both free providers recommend this; Pixabay terms require results to be
    cached 24h instead of re-requested, and normalised keys are what make
    that cache actually hit across slightly different phrasings.
    """
    return " ".join((query or "").lower().strip().split())


def query_tokens(query: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", normalise_query(query)) if t}


class MediaType(str, Enum):
    VIDEO = "video"
    PHOTO = "photo"


@dataclass
class SensitiveFlags:
    identifiable_person: bool = False
    trademark_or_logo: bool = False
    editorial_only: bool = False
    restricted_licence: bool = False
    private_property: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "identifiable_person": self.identifiable_person,
            "trademark_or_logo": self.trademark_or_logo,
            "editorial_only": self.editorial_only,
            "restricted_licence": self.restricted_licence,
            "private_property": self.private_property,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "SensitiveFlags":
        if not data:
            return cls()
        return cls(**{k: bool(data.get(k, False)) for k in cls().to_dict()})


@dataclass
class SearchFilters:
    """Filters for one search call. ``None`` minima derive from media type."""

    media_type: MediaType = MediaType.VIDEO
    orientation: str = "landscape"
    scene_duration_s: float | None = None
    min_duration_s: float | None = None
    min_width: int | None = None
    min_fps: float = 24.0
    safesearch: bool = True
    per_page: int = 80
    page: int = 1
    # provider_asset_id values already used in this project or in the
    # channel's last 20 published videos / 90 days (§24 Step 2).
    exclude_provider_asset_ids: frozenset[str] = frozenset()
    exclude_asset_keys: frozenset[str] = frozenset()  # "provider:id" keys

    def effective_min_width(self) -> int:
        if self.min_width is not None:
            return self.min_width
        return 1280 if self.media_type == MediaType.VIDEO else 1920

    def effective_min_duration_s(self) -> float | None:
        if self.min_duration_s is not None:
            return self.min_duration_s
        return self.scene_duration_s

    def cache_key_parts(self) -> tuple:
        return (
            self.media_type.value,
            self.orientation,
            self.effective_min_width(),
            self.effective_min_duration_s(),
            self.min_fps if self.media_type == MediaType.VIDEO else None,
            self.safesearch,
            self.per_page,
            self.page,
        )


@dataclass
class Candidate:
    provider: str
    provider_asset_id: str
    media_type: MediaType = MediaType.VIDEO
    preview_url: str = ""
    download_url: str = ""
    download_tiers: list[dict] = field(default_factory=list)
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    licence_id: str = ""
    author_name: str = ""
    author_url: str = ""
    page_url: str = ""
    tags: list[str] = field(default_factory=list)
    title: str = ""
    retrieved_at: datetime = field(default_factory=utcnow)
    downloads: int | None = None
    views: int | None = None
    sensitive_flags: SensitiveFlags = field(default_factory=SensitiveFlags)
    is_nsfw: bool = False
    #: Provider like/favourite count (Pixabay exposes ``likes`` on both
    #: video and photo hits; Pexels exposes neither likes nor downloads,
    #: so it stays ``None`` there). Secondary freshness/popularity signal
    #: alongside ``downloads`` for the integration layer's ranking.
    likes: int | None = None

    @property
    def asset_key(self) -> str:
        return f"{self.provider}:{self.provider_asset_id}"

    @property
    def is_generated(self) -> bool:
        """AI-generated assets are flagged GENERATED in the ledger."""
        return self.provider == "generated"

    @property
    def is_landscape(self) -> bool:
        if self.width is None or self.height is None:
            return True
        return self.width >= self.height


@dataclass
class DownloadedAsset:
    candidate: Candidate
    local_path: str = ""
    file_sha256: str = ""
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    ledger_id: str | None = None

    @property
    def provider(self) -> str:
        return self.candidate.provider

    @property
    def provider_asset_id(self) -> str:
        return self.candidate.provider_asset_id

    @property
    def asset_key(self) -> str:
        return self.candidate.asset_key
