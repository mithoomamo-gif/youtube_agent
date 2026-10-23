"""Manual library provider (§24 failover step 4).

The user's own folder (incl. Mixkit / Storyblocks downloads under the
user's own accounts). ONLY assets with a COMPLETE licence record may
enter a project — incomplete records are invisible to search.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import StockProvider
from .ledger import LedgerRow
from .models import Candidate, DownloadedAsset, MediaType, SearchFilters, query_tokens


@dataclass
class ManualAsset:
    candidate: Candidate
    licence_row: LedgerRow | None = None

    def has_complete_licence(self) -> bool:
        return self.licence_row is not None and self.licence_row.is_complete()


class ManualLibraryProvider(StockProvider):
    name = "manual"
    reliability = 0.85

    def __init__(self, assets: list[ManualAsset] | None = None):
        self._assets: list[ManualAsset] = list(assets or [])

    def add(self, asset: ManualAsset) -> None:
        self._assets.append(asset)

    async def is_available(self) -> bool:
        return any(a.has_complete_licence() for a in self._assets)

    async def search(
        self,
        query: str,
        media_type: MediaType,
        filters: SearchFilters | None = None,
    ) -> list[Candidate]:
        tokens = query_tokens(query)
        out: list[Candidate] = []
        for asset in self._assets:
            if not asset.has_complete_licence():
                continue  # hard rule: no complete licence record, no entry
            cand = asset.candidate
            if cand.media_type != media_type:
                continue
            if tokens:
                hay: set[str] = set()
                for tag in cand.tags:
                    hay |= query_tokens(tag)
                hay |= query_tokens(cand.title)
                if not (tokens & hay):
                    continue
            out.append(cand)
        return out

    async def download(self, candidate: Candidate) -> DownloadedAsset:
        for asset in self._assets:
            if asset.candidate.asset_key == candidate.asset_key and asset.licence_row is not None:
                return DownloadedAsset(
                    candidate=candidate,
                    local_path=asset.licence_row.local_file_path,
                    file_sha256=asset.licence_row.file_sha256,
                    width=candidate.width,
                    height=candidate.height,
                    duration_s=candidate.duration_s,
                    fps=candidate.fps,
                    ledger_id=asset.licence_row.ledger_id,
                )
        return DownloadedAsset(
            candidate=candidate,
            local_path=f"manual/{candidate.provider_asset_id}",
            file_sha256="",
            width=candidate.width,
            height=candidate.height,
            duration_s=candidate.duration_s,
            fps=candidate.fps,
        )
