"""Budget Guard — deterministic cost enforcement (roadmap Part 10).

Public API:
  * :class:`BudgetGuard` — reserve/settle/release + caps, thresholds,
    downgrade ladder, audited raise_cap, quota + variance tracking.
  * :class:`PriceCatalog` / :class:`PriceEntry` — versioned prices.yaml.
  * :class:`QuotaAccount` — dual-denominated subscription quota tracking.
  * :data:`DOWNGRADE_LADDER` — the ordered ①->⑤ soft-stop policy.
  * Exceptions: :class:`BudgetHardStop` (100% — carries spent/remaining/
    needed), :class:`ReservationRefused`, :class:`RetryCapExceeded`,
    :class:`L1LimitExceeded`, :class:`NoSilentOverrideError`, ...
"""
from .catalog import DEFAULT_CATALOG_PATH, PriceCatalog, PriceEntry
from .downgrades import DOWNGRADE_LADDER, Downgrade, DowngradePolicy
from .exceptions import (
    BudgetError,
    BudgetHardStop,
    InvalidLedgerState,
    L1LimitExceeded,
    LedgerWriteError,
    NoSilentOverrideError,
    QuotaExhausted,
    ReservationNotFound,
    ReservationRefused,
    RetryCapExceeded,
    RetryLimitExceeded,
)
from .guard import (
    STACK_CAPS_MICROS,
    AuditEntry,
    BudgetCaps,
    BudgetGuard,
    CallSpec,
    EstimateRecord,
    ThresholdEvent,
)
from .ledger import InMemoryLedger, Ledger, LedgerEntry
from .money import MICROS_PER_USD, micros_to_usd, usd_to_micros
from .policy import (
    DEFAULT_BUDGET_POLICY,
    SETTINGS_KEY as BUDGET_POLICY_SETTINGS_KEY,
    load_budget_policy,
    save_budget_policy,
    validate_budget_policy,
)
from .quota import QuotaAccount

__all__ = [
    "AuditEntry",
    "BudgetCaps",
    "BudgetError",
    "BudgetGuard",
    "BudgetHardStop",
    "CallSpec",
    "DEFAULT_CATALOG_PATH",
    "DOWNGRADE_LADDER",
    "Downgrade",
    "DowngradePolicy",
    "EstimateRecord",
    "InMemoryLedger",
    "InvalidLedgerState",
    "L1LimitExceeded",
    "Ledger",
    "LedgerEntry",
    "LedgerWriteError",
    "MICROS_PER_USD",
    "NoSilentOverrideError",
    "PriceCatalog",
    "PriceEntry",
    "QuotaAccount",
    "QuotaExhausted",
    "ReservationNotFound",
    "ReservationRefused",
    "RetryCapExceeded",
    "RetryLimitExceeded",
    "STACK_CAPS_MICROS",
    "ThresholdEvent",
    "micros_to_usd",
    "usd_to_micros",
    "BUDGET_POLICY_SETTINGS_KEY",
    "DEFAULT_BUDGET_POLICY",
    "load_budget_policy",
    "save_budget_policy",
    "validate_budget_policy",
]
