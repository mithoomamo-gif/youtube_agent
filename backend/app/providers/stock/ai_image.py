"""AI-image failover interface (§24 failover step 5).

The image worker (Agent 16 equivalent) builds the real provider; this
module defines the interface call the stock layer depends on, plus a
configurable stub. Generated assets are flagged GENERATED in the ledger
(provider == "generated").
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .licences import generated_licence
from .models import Candidate, MediaType, utcnow


@runtime_checkable
class AIImageProvider(Protocol):
    async def generate(
        self, *, scene_id: str, prompt: str, niche: str = ""
    ) -> Candidate | None:
        """Generate one still for a scene, or return None if generation fails."""
        ...


class StubAIImageProvider:
    """Stub acceptable for the pipeline: returns a GENERATED candidate.

    Set ``enabled=False`` (or subclass and return None) to exercise the
    BLOCKED + escalation path.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.calls: list[dict] = []

    async def generate(self, *, scene_id: str, prompt: str, niche: str = "") -> Candidate | None:
        self.calls.append({"scene_id": scene_id, "prompt": prompt, "niche": niche})
        if not self.enabled:
            return None
        licence = generated_licence()
        return Candidate(
            provider="generated",
            provider_asset_id=f"gen-{scene_id}",
            media_type=MediaType.PHOTO,
            preview_url="",
            download_url="",
            duration_s=None,
            width=1920,
            height=1080,
            fps=None,
            licence_id=licence.licence_id,
            author_name="",
            author_url="",
            page_url="",
            tags=["GENERATED", *(prompt.split()[:4])],
            title=prompt,
            retrieved_at=utcnow(),
        )


class NullAIImageProvider:
    """AI generation unavailable — scenes that reach here become BLOCKED."""

    async def generate(self, *, scene_id: str, prompt: str, niche: str = "") -> Candidate | None:
        return None
