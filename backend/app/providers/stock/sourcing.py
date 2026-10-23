"""Per-scene search ladder + failover chain (§24).

Provider order is CONFIG, not code. Shipping default is PIXABAY-FIRST
(Pexels new-key issuance paused 2026-09-30); pass ``provider_order`` to
swap Pixabay ⇄ Pexels without touching adapter code.

Failover per scene: local cache (inside adapters, L1) -> Pixabay ⇄ Pexels
-> manual library (complete licence only) -> AI image (GENERATED) ->
re-scene / BLOCKED + escalation. A black/placeholder scene is NEVER
silently shipped: BLOCKED carries ``escalation_required=True`` and no
chosen candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ai_image import AIImageProvider
from .base import StockProvider
from .cache import NegativeCache, SearchCache
from .licences import licence_for_provider
from .manual import ManualLibraryProvider
from .models import Candidate, DownloadedAsset, MediaType, SearchFilters
from .ranking import filter_candidates, rank_candidates

#: Shipping default order — Pixabay first, Pexels optional co-primary.
DEFAULT_PROVIDER_ORDER: list[str] = ["pixabay", "pexels"]

MIN_CANDIDATES_TO_STOP = 3


class DecisionStatus(str, Enum):
    FOUND = "found"
    MANUAL = "manual"
    AI_GENERATED = "ai_generated"
    BLOCKED = "blocked"


@dataclass
class SceneRequest:
    scene_id: str
    project_id: str = ""
    niche: str = ""
    # The 5-step ladder, in English (both free catalogues are English-tagged).
    exact_query: str = ""          # 1: exact visual query
    synonym_query: str = ""        # 2: synonym / subject query
    mood_query: str = ""           # 3: mood / concept query
    niche_query: str = ""          # 4: niche signature vocabulary
    # 5: photo fallback re-uses the best available query as a photo search.
    scene_duration_s: float = 5.0
    media_type: MediaType = MediaType.VIDEO
    used_in_project_ids: frozenset[str] = frozenset()
    channel_recent_ids: frozenset[str] = frozenset()  # last 20 videos / 90 days
    already_chosen: list[Candidate] = field(default_factory=list)

    def ladder_steps(self) -> list[tuple[int, str, MediaType]]:
        steps: list[tuple[int, str, MediaType]] = []
        for idx, q in enumerate(
            [self.exact_query, self.synonym_query, self.mood_query, self.niche_query], start=1
        ):
            if q and q.strip():
                steps.append((idx, q, self.media_type))
        fallback_query = self.niche_query or self.exact_query or self.synonym_query or self.mood_query
        if fallback_query and fallback_query.strip():
            steps.append((5, fallback_query, MediaType.PHOTO))
        return steps

    def excluded_ids(self) -> set[str]:
        return set(self.used_in_project_ids) | set(self.channel_recent_ids)


@dataclass
class SceneDecision:
    """Per-scene asset decision record (§24 Step 4).

    Stores the chosen candidate + 2 alternates + which ladder step
    succeeded, so a QC rejection swaps an alternate WITHOUT a new API call.
    """

    scene_id: str
    project_id: str = ""
    status: DecisionStatus = DecisionStatus.BLOCKED
    chosen: Candidate | None = None
    alternates: list[Candidate] = field(default_factory=list)
    ladder_step: int | None = None
    source: str = ""  # provider name / "manual" / "ai_image" / "" when blocked
    escalation_required: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.status == DecisionStatus.BLOCKED


class StockSourcingService:
    """The ONLY entry point the pipeline (Agents 15/16) calls for stock."""

    def __init__(
        self,
        providers: list[StockProvider] | None = None,
        *,
        provider_order: list[str] | None = None,
        manual_provider: ManualLibraryProvider | None = None,
        ai_provider: AIImageProvider | None = None,
        search_cache: SearchCache | None = None,
        negative_cache: NegativeCache | None = None,
    ):
        self._providers: dict[str, StockProvider] = {p.name: p for p in (providers or [])}
        # Provider order is CONFIG, not code.
        self.provider_order: list[str] = list(provider_order or DEFAULT_PROVIDER_ORDER)
        self.manual_provider = manual_provider
        self.ai_provider = ai_provider
        self.search_cache = search_cache
        self.negative_cache = negative_cache if negative_cache is not None else NegativeCache()

    @property
    def ordered_providers(self) -> list[StockProvider]:
        ordered = [self._providers[n] for n in self.provider_order if n in self._providers]
        # Any configured provider not named in the order runs last.
        ordered += [p for n, p in self._providers.items() if n not in self.provider_order]
        return ordered

    def provider(self, name: str) -> StockProvider | None:
        return self._providers.get(name)

    def _filters_for(self, scene: SceneRequest, media_type: MediaType) -> SearchFilters:
        used = scene.excluded_ids()
        return SearchFilters(
            media_type=media_type,
            orientation="landscape",
            scene_duration_s=scene.scene_duration_s if media_type == MediaType.VIDEO else None,
            min_fps=24.0,
            safesearch=True,
            # §24: ONE call per ladder step at MAX page size. 200 is
            # Pixabay's maximum; each adapter clamps to its own maximum
            # (Pexels 80), so this stays provider-agnostic.
            per_page=200,
            exclude_provider_asset_ids=frozenset(
                # Candidate ids are bare ids; also block "provider:id" keys below.
                {k.split(":", 1)[-1] for k in used}
            ),
            exclude_asset_keys=frozenset(used),
        )

    async def resolve_scene(self, scene: SceneRequest) -> SceneDecision:
        used_ids = scene.excluded_ids()
        best_partial: tuple[list[Candidate], int, str] | None = None

        for step_no, query, media_type in scene.ladder_steps():
            filters = self._filters_for(scene, media_type)
            step_passed: list[Candidate] = []
            step_source = ""
            for provider in self.ordered_providers:
                # Circuit breaker: an outage trips it for the rest of the job.
                breaker = getattr(provider, "circuit_breaker", None)
                if breaker is not None and breaker.is_open:
                    continue
                if not await provider.is_available():
                    continue
                # L3 negative cache: don't re-bill a known-empty query
                # (scoped per media type so a video miss cannot suppress
                # the step-5 photo fallback for the same query).
                if self.negative_cache.is_negative(
                    provider.name, query, scene.niche, media_type=media_type.value
                ):
                    continue
                candidates = await provider.search(query, media_type, filters)
                passed = filter_candidates(
                    candidates, filters, used_keys=set(used_ids),
                    used_ids={k.split(":", 1)[-1] for k in used_ids},
                )
                if not passed:
                    self.negative_cache.record(
                        provider.name, query, scene.niche, media_type=media_type.value
                    )
                else:
                    if not step_source:
                        step_source = provider.name
                    step_passed.extend(passed)
                if len(step_passed) >= MIN_CANDIDATES_TO_STOP:
                    break  # STOP: quality bar met, no more calls this step
            if len(step_passed) >= MIN_CANDIDATES_TO_STOP:
                ranked = rank_candidates(
                    step_passed, query, filters, already_chosen=scene.already_chosen
                )
                return SceneDecision(
                    scene_id=scene.scene_id,
                    project_id=scene.project_id,
                    status=DecisionStatus.FOUND,
                    chosen=ranked[0],
                    alternates=ranked[1:3],
                    ladder_step=step_no,
                    source=ranked[0].provider,
                )
            if step_passed and best_partial is None:
                best_partial = (step_passed, step_no, query)

        # Partial success (1-2 candidates): use them rather than degrading.
        if best_partial is not None:
            passed, step_no, query = best_partial
            filters = self._filters_for(scene, scene.media_type)
            ranked = rank_candidates(passed, query, filters, already_chosen=scene.already_chosen)
            return SceneDecision(
                scene_id=scene.scene_id,
                project_id=scene.project_id,
                status=DecisionStatus.FOUND,
                chosen=ranked[0],
                alternates=ranked[1:3],
                ladder_step=step_no,
                source=ranked[0].provider,
            )

        # Failover: manual library (complete licence records only).
        if self.manual_provider is not None:
            filters = self._filters_for(scene, scene.media_type)
            manual_hits = await self.manual_provider.search(
                scene.exact_query or scene.niche_query, scene.media_type, filters
            )
            manual_passed = filter_candidates(manual_hits, filters)
            if manual_passed:
                return SceneDecision(
                    scene_id=scene.scene_id,
                    project_id=scene.project_id,
                    status=DecisionStatus.MANUAL,
                    chosen=manual_passed[0],
                    alternates=manual_passed[1:3],
                    ladder_step=None,
                    source="manual",
                )

        # Failover: AI image (flagged GENERATED in the ledger).
        if self.ai_provider is not None:
            generated = await self.ai_provider.generate(
                scene_id=scene.scene_id,
                prompt=scene.exact_query or scene.niche_query or scene.mood_query,
                niche=scene.niche,
            )
            if generated is not None:
                return SceneDecision(
                    scene_id=scene.scene_id,
                    project_id=scene.project_id,
                    status=DecisionStatus.AI_GENERATED,
                    chosen=generated,
                    alternates=[],
                    ladder_step=None,
                    source="ai_image",
                )

        # Re-scene / BLOCKED + escalation — NEVER a silent placeholder.
        return SceneDecision(
            scene_id=scene.scene_id,
            project_id=scene.project_id,
            status=DecisionStatus.BLOCKED,
            chosen=None,
            alternates=[],
            ladder_step=None,
            source="",
            escalation_required=True,
        )

    # Backwards-friendly alias used by pipeline code.
    async def search_for_scene(self, scene: SceneRequest) -> SceneDecision:
        return await self.resolve_scene(scene)

    async def download_and_register(
        self,
        decision: SceneDecision,
        *,
        ledger,
        project_id: str = "",
    ) -> DownloadedAsset:
        """Download the chosen candidate and write its licence ledger row.

        After this call ``ledger.assert_renderable(asset)`` passes; before
        it, the gate correctly raises (no ledger row = no render).
        """
        if decision.chosen is None:
            raise ValueError("cannot download a BLOCKED scene decision")
        candidate = decision.chosen
        provider = self._providers.get(candidate.provider)
        if provider is not None:
            asset = await provider.download(candidate)
        elif self.manual_provider is not None and candidate.provider == "manual":
            asset = await self.manual_provider.download(candidate)
        else:
            # Generated / manual assets may already exist on disk.
            import hashlib

            asset = DownloadedAsset(
                candidate=candidate,
                local_path=f"generated/{candidate.provider_asset_id}",
                file_sha256=hashlib.sha256(candidate.asset_key.encode()).hexdigest(),
                width=candidate.width,
                height=candidate.height,
                duration_s=candidate.duration_s,
                fps=candidate.fps,
            )
        licence = licence_for_provider(candidate.provider)
        ledger.record_download(
            project_id=project_id or decision.project_id,
            scene_id=decision.scene_id,
            asset=asset,
            licence=licence,
            retrieved_by_key_id=(provider.key_ref if provider is not None else ""),
        )
        return asset
