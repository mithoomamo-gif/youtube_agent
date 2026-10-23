"""Versioned, hot-reloadable model registry backed by config/models.yaml.

Prices and model IDs are DATA: a price change or a deprecated model is a
config edit, not a release. The loader re-reads the file when its mtime
changes, and resolves promotional prices that carry ``price_expires``
(+ optional ``*_after_expiry`` rates) at lookup time.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .errors import ModelRegistryError
from .models import Tier, UsageBuckets

DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "models.yaml"
)


class ModelEntry(BaseModel):
    provider: str
    model_id: str
    input_price_per_1m: float
    output_price_per_1m: float
    cached_input_price_per_1m: float | None = None
    price_expires: date | None = None
    input_price_after_expiry_per_1m: float | None = None
    output_price_after_expiry_per_1m: float | None = None
    verified_at: str | None = None

    def prices_on(self, when: date | None = None) -> tuple[float, float, float]:
        """(input, output, cached_input) USD-per-1M in effect on `when`."""
        when = when or datetime.now(timezone.utc).date()
        inp, out = self.input_price_per_1m, self.output_price_per_1m
        if (
            self.price_expires is not None
            and when > self.price_expires
            and self.input_price_after_expiry_per_1m is not None
        ):
            inp = self.input_price_after_expiry_per_1m
            out = self.output_price_after_expiry_per_1m or out
        cached = (
            self.cached_input_price_per_1m
            if self.cached_input_price_per_1m is not None
            else inp
        )
        return inp, out, cached

    def cost_usd(
        self, usage: UsageBuckets, when: date | None = None
    ) -> float:
        inp, out, cached = self.prices_on(when)
        return (
            usage.input_uncached * inp
            + usage.input_cached * cached
            + usage.input_cache_write * inp  # writes billed at input rate*
            + (usage.output + usage.reasoning) * out  # reasoning = output rate
        ) / 1_000_000

    # * Conservative v1 simplification: Anthropic cache writes carry a
    #   1.25x/2x premium and OpenAI explicit writes 1.25x; the registry has
    #   no per-provider write multiplier yet, so writes are priced at the
    #   plain input rate and any premium shows up in invoice reconciliation.


class TierConfig(BaseModel):
    models: list[ModelEntry] = Field(default_factory=list)

    @property
    def primary(self) -> ModelEntry:
        if not self.models:
            raise ModelRegistryError("tier has no models configured")
        return self.models[0]


class ModelRegistry(BaseModel):
    registry_version: str = "unknown"
    verified_at: str | None = None
    tiers: dict[str, TierConfig] = Field(default_factory=dict)

    # -- loading / hot reload -------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "RegistryHandle":
        return RegistryHandle(path or DEFAULT_REGISTRY_PATH)

    def tier(self, tier: Tier | str) -> TierConfig:
        key = tier.value if isinstance(tier, Tier) else str(tier)
        cfg = self.tiers.get(key)
        if cfg is None or not cfg.models:
            raise ModelRegistryError(f"no models configured for tier {key}")
        return cfg

    def entry(self, tier: Tier | str, provider: str, model_id: str) -> ModelEntry:
        for e in self.tier(tier).models:
            if e.provider == provider and e.model_id == model_id:
                return e
        raise ModelRegistryError(
            f"model {provider}/{model_id} not in registry for tier {tier}"
        )

    def all_entries(self) -> list[tuple[str, ModelEntry]]:
        """Every (tier_key, entry) pair in the registry, in file order."""
        out: list[tuple[str, ModelEntry]] = []
        for tier_key in sorted(self.tiers):
            for e in self.tiers[tier_key].models:
                out.append((tier_key, e))
        return out

    def find(
        self, model_id: str, provider: str | None = None
    ) -> list[tuple[str, ModelEntry]]:
        """All (tier_key, entry) pairs whose model_id matches EXACTLY.

        Exact match only — never prefix/family matching: a lookup for
        'gemini-3.6-flash' must NOT resolve to 'gemini-3.8-flash'
        (that analogy resolution is what let an unregistered model run
        in Round 3). ``provider`` further restricts when given.
        """
        return [
            (tier_key, e)
            for tier_key, e in self.all_entries()
            if e.model_id == model_id
            and (provider is None or e.provider == provider)
        ]


class RegistryHandle:
    """Hot-reloadable wrapper: re-parses models.yaml when its mtime moves."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._registry: ModelRegistry | None = None
        self.reload_if_changed(force=True)

    def reload_if_changed(self, force: bool = False) -> ModelRegistry:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError as exc:
            raise ModelRegistryError(
                f"model registry not found: {self.path}"
            ) from exc
        if force or self._registry is None or mtime != self._mtime:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self._registry = ModelRegistry.model_validate(raw)
            self._mtime = mtime
        return self._registry

    @property
    def registry(self) -> ModelRegistry:
        return self.reload_if_changed()

    def tier(self, tier: Tier | str) -> TierConfig:
        return self.reload_if_changed().tier(tier)

    def find(
        self, model_id: str, provider: str | None = None
    ) -> list[tuple[str, ModelEntry]]:
        return self.reload_if_changed().find(model_id, provider)
