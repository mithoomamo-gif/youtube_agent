"""Stock media layer (§24) — Pixabay-first, Pexels optional co-primary.

Only the pipeline (Agents 15/16 equivalents) calls this layer.
Provider order is CONFIG (``provider_order``), not code.
"""
from .ai_image import AIImageProvider, NullAIImageProvider, StubAIImageProvider
from .base import DictSecretProvider, SecretProvider, StockProvider
from .cache import NegativeCache, SearchCache
from .circuit_breaker import CircuitBreaker
from .ledger import (
    CSV_FIELDS,
    InMemoryLicenceLedger,
    JsonLicenceLedger,
    LedgerRow,
    LicenceLedger,
    LicenceLedgerError,
    assert_renderable,
)
from .licences import LicenceRecord, generated_licence, licence_for_provider, pexels_licence, pixabay_licence
from .manual import ManualAsset, ManualLibraryProvider
from .models import (
    Candidate,
    DownloadedAsset,
    MediaType,
    SearchFilters,
    SensitiveFlags,
    normalise_query,
)
from .pexels import PexelsProvider, PexelsQuota
from .pixabay import PixabayProvider
from .ranking import filter_candidates, rank_candidates, rejection_reason, score_candidate
from .rate_limit import SlidingWindowRateLimiter
from .sourcing import (
    DEFAULT_PROVIDER_ORDER,
    DecisionStatus,
    SceneDecision,
    SceneRequest,
    StockSourcingService,
)

__all__ = [
    "AIImageProvider",
    "CSV_FIELDS",
    "Candidate",
    "CircuitBreaker",
    "DEFAULT_PROVIDER_ORDER",
    "DecisionStatus",
    "DictSecretProvider",
    "DownloadedAsset",
    "InMemoryLicenceLedger",
    "JsonLicenceLedger",
    "LedgerRow",
    "LicenceLedger",
    "LicenceLedgerError",
    "LicenceRecord",
    "ManualAsset",
    "ManualLibraryProvider",
    "MediaType",
    "NegativeCache",
    "NullAIImageProvider",
    "PexelsProvider",
    "PexelsQuota",
    "PixabayProvider",
    "SceneDecision",
    "SceneRequest",
    "SearchCache",
    "SearchFilters",
    "SecretProvider",
    "SensitiveFlags",
    "SlidingWindowRateLimiter",
    "StockProvider",
    "StockSourcingService",
    "StubAIImageProvider",
    "assert_renderable",
    "filter_candidates",
    "generated_licence",
    "licence_for_provider",
    "normalise_query",
    "pexels_licence",
    "pixabay_licence",
    "rank_candidates",
    "rejection_reason",
    "score_candidate",
]
