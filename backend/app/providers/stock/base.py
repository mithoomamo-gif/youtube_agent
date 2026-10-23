"""Provider abstraction + the shared SecretProvider contract.

Adapters take a ``SecretProvider`` and a key *reference*; they never read
env/files for keys and never log key values.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .models import Candidate, DownloadedAsset, MediaType, SearchFilters


@runtime_checkable
class SecretProvider(Protocol):
    async def get_secret(self, ref: str) -> str | None:
        ...


class DictSecretProvider:
    """Simple SecretProvider backed by a dict (tests / composition root)."""

    def __init__(self, secrets: dict[str, str | None] | None = None):
        self._secrets = dict(secrets or {})

    async def get_secret(self, ref: str) -> str | None:
        value = self._secrets.get(ref)
        return value if value else None


class ProviderUnavailableError(Exception):
    """Raised internally when a provider has no usable key/config."""


class StockProvider(ABC):
    """ONE internal interface; every source is an adapter behind it (§24)."""

    name: str = "base"
    #: relative trust score used by ranking (reliability term, 0..1)
    reliability: float = 0.8
    #: provider's maximum page size — the ladder makes ONE call per step
    #: at this size, never a burst of small calls.
    max_per_page: int = 80

    @property
    def key_ref(self) -> str:
        return getattr(self, "_key_ref", "")

    async def is_available(self) -> bool:
        """True when the provider can be attempted (key present, not tripped)."""
        return True

    @abstractmethod
    async def search(
        self,
        query: str,
        media_type: MediaType,
        filters: SearchFilters | None = None,
    ) -> list[Candidate]:
        """Return candidates; 0 usable results is NORMAL, not an error."""

    @abstractmethod
    async def download(self, candidate: Candidate) -> DownloadedAsset:
        """Download one candidate and return the local asset record."""
