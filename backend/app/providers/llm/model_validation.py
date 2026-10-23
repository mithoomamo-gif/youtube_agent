"""Named-model validation: registry AND price catalog, or refusal.

Round 3 incident: an unregistered model (``gemini-3.6-flash`` — absent
from ``config/models.yaml``) actually generated production scripts, and
its cost could only be reconstructed by pricing it BY ANALOGY at
``gemini-3.8-flash`` rates. Round 4 hardening closes that hole:

* Any call that NAMES a physical model (``LLMRequest.model_id``) is
  validated here, by the router, BEFORE any provider HTTP call.
* The model must be present in BOTH sources, by EXACT model id:
    1. the model registry (``config/models.yaml``, via ModelRegistry), and
    2. the price catalog (``config/prices.yaml``, via PriceCatalog) with
       both an ``input_token`` and an ``output_token`` row.
* Otherwise :class:`ProviderConfigError` is raised, naming the offending
  model. There is no silent tier fallback and no analogy/prefix/family
  pricing anywhere in this path: ``gemini-3.1-pro`` does NOT price
  ``gemini-3.1-pro-preview``, and ``gemini-3.8-flash`` does NOT price
  ``gemini-3.6-flash``.

Provider naming differs between the two files for Google models: the
registry says ``gemini`` while the price catalog says ``google``. That
single known alias is mapped explicitly below; every other provider
name must match exactly.

Tier-only requests (the normal agent path, ``model_id is None``) never
reach this module: their chain comes from the registry itself, so the
models are registered by construction. :func:`audit_registry_against_catalog`
exists so operators (and tests) can see which registry models currently
lack catalog prices — several roadmap-era models are in that state, and
naming any of them in a call is refused until a real price row is added.
"""

from __future__ import annotations

from typing import Any

from .errors import ProviderConfigError
from .models import Tier
from .registry import ModelEntry, ModelRegistry

#: Registry provider name -> price-catalog provider name(s) to try, in
#: order. Only the one historically-divergent alias is listed; anything
#: not listed must match exactly.
_CATALOG_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini", "google"),
    "google": ("google", "gemini"),
}

_REQUIRED_UNITS = ("input_token", "output_token")


def _catalog_rows(catalog: Any, provider: str, model_id: str, unit: str) -> list:
    """Rows for (provider, model, unit) from a PriceCatalog-like object.

    Supports the real :class:`PriceCatalog` (``.find``) and any test
    double exposing ``.find`` or ``.entries``. Exact matches only.
    """
    find = getattr(catalog, "find", None)
    if callable(find):
        try:
            return list(find(provider, model_id, unit, "api_payg") or [])
        except TypeError:
            return list(find(provider, model_id, unit) or [])
    entries = getattr(catalog, "entries", None) or []
    return [
        e
        for e in entries
        if e.provider == provider
        and e.model_or_service == model_id
        and e.unit == unit
        and getattr(e, "billing_route", "api_payg") == "api_payg"
    ]


def catalog_has_llm_price(catalog: Any, provider: str, model_id: str) -> bool:
    """True when the catalog prices `model_id` (input AND output rows)."""
    if catalog is None:
        return False
    providers = _CATALOG_PROVIDER_ALIASES.get(provider, (provider,))
    for cat_provider in providers:
        if all(
            _catalog_rows(catalog, cat_provider, model_id, unit)
            for unit in _REQUIRED_UNITS
        ):
            return True
    return False


def ensure_model_registered_and_priced(
    registry: ModelRegistry,
    price_catalog: Any,
    *,
    model_id: str,
    provider: str | None,
    tier: Tier | str,
) -> ModelEntry:
    """Resolve a caller-named model or raise ProviderConfigError.

    Returns the registry :class:`ModelEntry` for (tier, model_id) when —
    and only when — the model is registered for that tier AND priced in
    the price catalog. Every refusal message names the offending model.
    """
    tier_key = tier.value if isinstance(tier, Tier) else str(tier)

    # 1) Registry: exact model id, in the requested tier.
    in_tier = [
        e
        for t, e in registry.find(model_id, provider)
        if t == tier_key
    ]
    if not in_tier:
        anywhere = registry.find(model_id, provider)
        if anywhere:
            tiers = ", ".join(sorted({t for t, _ in anywhere}))
            raise ProviderConfigError(
                f"model {model_id!r} is registered for tier(s) {tiers}, "
                f"not for requested tier {tier_key}; refusing to "
                "substitute tiers for a named model",
                status_code=400,
                provider=provider,
            )
        raise ProviderConfigError(
            f"model {model_id!r} is not in the model registry "
            "(config/models.yaml); refusing to call an unregistered "
            "model — register it and price it first",
            status_code=400,
            provider=provider,
        )
    entry = in_tier[0]

    # 2) Price catalog: exact model id, input + output rows. A missing
    #    catalog is a configuration failure too — fail closed rather
    #    than run a model whose cost cannot be accounted.
    if price_catalog is None:
        raise ProviderConfigError(
            f"model {model_id!r} cannot be price-checked: no price "
            "catalog (config/prices.yaml) is available; refusing to "
            "run an unpriced model",
            status_code=400,
            provider=entry.provider,
        )
    if not catalog_has_llm_price(price_catalog, entry.provider, model_id):
        raise ProviderConfigError(
            f"model {model_id!r} is in the model registry but has no "
            "price in the price catalog (config/prices.yaml); refusing "
            "to price it by analogy — add verified input/output price "
            "rows for it first",
            status_code=400,
            provider=entry.provider,
        )
    return entry


def audit_registry_against_catalog(
    registry: ModelRegistry, price_catalog: Any
) -> list[str]:
    """Human-readable list of registry models lacking catalog prices.

    One string per gap, naming provider/model/tier. Empty list means
    every registered model is priced. This is a reporting/audit helper;
    enforcement lives in :func:`ensure_model_registered_and_priced`.
    """
    gaps: list[str] = []
    for tier_key, entry in registry.all_entries():
        if not catalog_has_llm_price(
            price_catalog, entry.provider, entry.model_id
        ):
            gaps.append(
                f"{entry.provider}/{entry.model_id} (tier {tier_key}): "
                "in model registry but missing input/output price rows "
                "in price catalog"
            )
    return gaps
