"""Versioned price catalog (roadmap Part 10 / section 35, Leak 11).

Prices are DATA in ``backend/config/prices.yaml`` — never constants in code.
:class:`PriceCatalog` loads that file, is hot-reloadable (re-reads when the
file's mtime changes), and flags itself stale when any entry's
``verified_at`` is older than 60 days.

All arithmetic is Decimal -> integer micros; floats never accumulate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .money import MICROS_PER_USD

STALE_AFTER_DAYS = 60

# Default location: backend/config/prices.yaml relative to this package
# (backend/app/providers/budget/catalog.py -> parents[3] == backend/).
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "prices.yaml"


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_decimal(value: object) -> Decimal:
    # str() round-trip keeps YAML floats exact (0.00000005 -> Decimal('5E-8')).
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class PriceEntry:
    """One catalog row — mirrors the price_catalog table in the roadmap."""

    provider: str
    model_or_service: str
    unit: str  # input_token | output_token | cached_input_token | tts_char | image | minute
    unit_price_usd: Decimal  # price of ONE unit
    effective_from: date
    verified_at: date
    source: str = ""
    billing_route: str = "api_payg"  # api_payg | subscription_credits | free | local
    currency: str = "USD"
    tier: str | None = None
    notes: str = ""

    @property
    def unit_price_micros(self) -> Decimal:
        return self.unit_price_usd * MICROS_PER_USD

    def cost_micros(self, units: int) -> int:
        """Exact integer-micros cost of ``units`` units at this row's price."""
        raw = self.unit_price_micros * int(units)
        return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def is_stale(self, now: date | None = None) -> bool:
        ref = now or date.today()
        return (ref - self.verified_at).days > STALE_AFTER_DAYS

    def ref(self, version: str = "") -> str:
        """Snapshot reference stored on ledger entries (row + catalog version)."""
        base = f"{self.provider}/{self.model_or_service}:{self.unit}[{self.billing_route}]"
        return f"{base}@{version}" if version else base


@dataclass
class PriceCatalog:
    """Hot-reloadable, versioned price catalog."""

    entries: list[PriceEntry] = field(default_factory=list)
    version: str = ""
    currency: str = "USD"
    _path: Path | None = field(default=None, repr=False)
    _mtime: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path | None = None) -> "PriceCatalog":
        """Load from YAML. ``path=None`` uses backend/config/prices.yaml."""
        p = Path(path) if path is not None else DEFAULT_CATALOG_PATH
        catalog = cls(_path=p)
        catalog._read()
        return catalog

    # Backwards/alternate name some callers reach for.
    from_yaml = load

    def _read(self) -> None:
        import yaml  # PyYAML — declared backend dependency

        assert self._path is not None
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        parsed: list[PriceEntry] = []
        for row in raw.get("entries", []):
            parsed.append(
                PriceEntry(
                    provider=str(row["provider"]),
                    model_or_service=str(row["model_or_service"]),
                    unit=str(row["unit"]),
                    unit_price_usd=_as_decimal(row["unit_price_usd"]),
                    effective_from=_as_date(row["effective_from"]),
                    verified_at=_as_date(row["verified_at"]),
                    source=str(row.get("source", "")),
                    billing_route=str(row.get("billing_route", "api_payg")),
                    currency=str(row.get("currency", raw.get("currency", "USD"))),
                    tier=(str(row["tier"]) if row.get("tier") else None),
                    notes=str(row.get("notes", "")),
                )
            )
        self.entries = parsed
        self.version = str(raw.get("version", ""))
        self.currency = str(raw.get("currency", "USD"))
        self._mtime = os.path.getmtime(self._path)

    def reload(self, *, force: bool = False) -> bool:
        """Re-read the YAML if it changed on disk (hot reload).

        Returns True when the catalog was actually reloaded.
        """
        if self._path is None:
            return False
        mtime = os.path.getmtime(self._path)
        if force or self._mtime is None or mtime > self._mtime:
            self._read()
            return True
        return False

    # -------------------------------------------------------------- staleness
    def stale_entries(self, now: date | None = None) -> list[PriceEntry]:
        return [e for e in self.entries if e.is_stale(now)]

    def is_stale(self, now: date | None = None) -> bool:
        """True when any entry's verified_at is older than 60 days."""
        return bool(self.stale_entries(now))

    # ---------------------------------------------------------------- lookup
    def find(
        self,
        provider: str,
        model_or_service: str,
        unit: str | None = None,
        billing_route: str | None = None,
    ) -> list[PriceEntry]:
        out = [
            e
            for e in self.entries
            if e.provider == provider and e.model_or_service == model_or_service
        ]
        if unit is not None:
            out = [e for e in out if e.unit == unit]
        if billing_route is not None:
            out = [e for e in out if e.billing_route == billing_route]
        return out

    def get(
        self,
        provider: str,
        model_or_service: str,
        unit: str,
        billing_route: str = "api_payg",
    ) -> PriceEntry:
        """The single row for (provider, model, unit, billing_route).

        Raises KeyError when the catalog has no such row — an unpriced call
        must fail loudly, never silently bill $0.
        """
        matches = self.find(provider, model_or_service, unit, billing_route)
        if not matches:
            raise KeyError(
                f"no price for {provider}/{model_or_service} unit={unit} "
                f"route={billing_route} in catalog {self.version!r}"
            )
        return matches[0]

    # ----------------------------------------------------------------- costs
    def cost_micros(
        self,
        provider: str,
        model_or_service: str,
        unit: str,
        units: int,
        billing_route: str = "api_payg",
    ) -> int:
        return self.get(provider, model_or_service, unit, billing_route).cost_micros(units)

    def llm_cost_micros(
        self,
        provider: str,
        model_or_service: str,
        *,
        units_in: int = 0,
        units_out: int = 0,
        units_cached: int = 0,
        billing_route: str = "api_payg",
    ) -> int:
        """Cost of one LLM call from MUTUALLY EXCLUSIVE token buckets.

        ``units_cached`` are billed at the cached rate and must NOT also be
        counted in ``units_in`` (double-counting cached tokens is the #1
        cost-tracking error — roadmap Part 10.6).
        """
        total = 0
        if units_in:
            total += self.cost_micros(provider, model_or_service, "input_token", units_in, billing_route)
        if units_out:
            total += self.cost_micros(provider, model_or_service, "output_token", units_out, billing_route)
        if units_cached:
            total += self.cost_micros(provider, model_or_service, "cached_input_token", units_cached, billing_route)
        return total

    def snapshot_ref(
        self,
        provider: str,
        model_or_service: str,
        unit: str,
        billing_route: str = "api_payg",
    ) -> str:
        return self.get(provider, model_or_service, unit, billing_route).ref(self.version)
