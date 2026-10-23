"""Compliance Gate (Round 6) — stored audit checklist + Gate C enforcement.

Public surface: the pure visibility logic (:mod:`.gate`), the schema
(:mod:`.schema`) and the stored-state service (:mod:`.service`).
"""

from __future__ import annotations

from app.compliance.gate import (
    REASON_CODE,
    REASON_MESSAGE,
    ComplianceDecision,
    coerce_overall,
    evaluate_visibility,
    is_passed,
    resolve_checker,
)
from app.compliance.schema import (
    ITEM_DEFINITIONS,
    ITEM_KEYS,
    ITEM_STATUSES,
    OVERALL_STATES,
    derive_overall,
    ensure_compliance_schema,
)

__all__ = [
    "REASON_CODE",
    "REASON_MESSAGE",
    "ComplianceDecision",
    "coerce_overall",
    "evaluate_visibility",
    "is_passed",
    "resolve_checker",
    "ITEM_DEFINITIONS",
    "ITEM_KEYS",
    "ITEM_STATUSES",
    "OVERALL_STATES",
    "derive_overall",
    "ensure_compliance_schema",
]
