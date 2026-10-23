"""Compliance Gate tables — runtime DDL + lazy schema creation.

Round 6 (Compliance Gate — the roadmap's #1 launch blocker).  The
compliance checklist is stored as DATA, one row per checklist item,
plus an append-only transition log: every status change is a
deliberate, logged action carrying who/when + evidence, never a
free-form edit of a state blob.

The lazy-ensure pattern mirrors :mod:`app.learning.schema` /
:mod:`app.reports.schema`: the tables are created (and the four
canonical checklist items seeded) on first use, so the gate works on
databases that have not run migrations.  Alembic migration
``0011_compliance`` carries the identical DDL.

Conventions (section 15): TEXT uuid4 primary keys on the log,
INTEGER Unix epoch-millisecond timestamps.  The checklist is
PER-APP (one install = one compliance state), so there is
deliberately no ``channel_id`` anywhere here.

Item statuses: ``pending`` / ``in_progress`` / ``passed`` /
``failed``.  The overall state is DERIVED, never stored:
``passed`` iff every item is ``passed``; ``not_started`` iff every
item is still ``pending``; otherwise ``in_progress``.
"""

from __future__ import annotations

import time
from typing import Any

__all__ = [
    "COMPLIANCE_ITEMS_TABLE",
    "COMPLIANCE_TRANSITIONS_TABLE",
    "COMPLIANCE_ITEMS_DDL",
    "COMPLIANCE_TRANSITIONS_DDL",
    "COMPLIANCE_DDL",
    "INDEX_STATEMENTS",
    "ITEM_DEFINITIONS",
    "ITEM_KEYS",
    "ITEM_STATUSES",
    "OVERALL_STATES",
    "ensure_compliance_schema",
    "derive_overall",
]

COMPLIANCE_ITEMS_TABLE = "compliance_items"
COMPLIANCE_TRANSITIONS_TABLE = "compliance_transitions"

#: The canonical checklist (key, label, description), in display
#: order.  This is the roadmap's launch-audit checklist; a key here
#: is the only valid ``item_key`` for a transition.
ITEM_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (
        "api_audit_status",
        "YouTube API audit status",
        "The YouTube API Services audit for this app / API project "
        "has been submitted and approved by YouTube.",
    ),
    (
        "terms_branding_review",
        "Terms & branding review",
        "YouTube API Terms of Service and branding guidelines have "
        "been reviewed and the app complies with them.",
    ),
    (
        "privacy_policy_live",
        "Privacy policy live",
        "A public privacy policy page for the app is live at a "
        "stable URL and linked from the app / OAuth consent screen.",
    ),
    (
        "quota_project_verified",
        "Quota project verified",
        "The Google Cloud / API project identity is verified and "
        "its quota is confirmed for production use.",
    ),
)

ITEM_KEYS: tuple[str, ...] = tuple(key for key, _, _ in ITEM_DEFINITIONS)

#: Per-item statuses (mirror the schema CHECK).
ITEM_STATUSES: tuple[str, ...] = ("pending", "in_progress", "passed", "failed")

#: Derived overall states.
OVERALL_STATES: tuple[str, ...] = ("not_started", "in_progress", "passed")

# Kept identical with migrations/versions/0011_compliance.py.
COMPLIANCE_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS compliance_items (
    key        TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'in_progress',
                                 'passed', 'failed')),
    evidence   TEXT NULL,
    note       TEXT NULL,
    updated_by TEXT NULL,
    updated_at INTEGER NULL,
    created_at INTEGER NOT NULL
)
"""

COMPLIANCE_TRANSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS compliance_transitions (
    id          TEXT PRIMARY KEY,
    item_key    TEXT NOT NULL
                REFERENCES compliance_items (key) ON DELETE RESTRICT,
    from_status TEXT NOT NULL
                CHECK (from_status IN ('pending', 'in_progress',
                                        'passed', 'failed')),
    to_status   TEXT NOT NULL
                CHECK (to_status IN ('pending', 'in_progress',
                                      'passed', 'failed')),
    actor       TEXT NOT NULL,
    evidence    TEXT NULL,
    note        TEXT NULL,
    created_at  INTEGER NOT NULL
)
"""

COMPLIANCE_DDL = (COMPLIANCE_ITEMS_DDL, COMPLIANCE_TRANSITIONS_DDL)

INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_transitions_item_time "
    "ON compliance_transitions (item_key, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_transitions_time "
    "ON compliance_transitions (created_at DESC)",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def derive_overall(statuses: Any) -> str:
    """Overall state derived from the per-item statuses.

    ``passed`` iff every canonical item is passed (an empty/missing
    set is NOT a pass — no evidence, no pass); ``not_started`` iff
    every item is still pending; otherwise ``in_progress``.
    """
    values = [str(s) for s in (statuses or [])]
    if len(values) < len(ITEM_KEYS):
        # Missing rows can never add up to a pass.
        if values and any(v != "pending" for v in values):
            return "in_progress"
        return "not_started"
    if all(v == "passed" for v in values):
        return "passed"
    if all(v == "pending" for v in values):
        return "not_started"
    return "in_progress"


def ensure_compliance_schema(conn: Any) -> None:
    """Create the compliance tables + indexes and seed the checklist.

    Idempotent: every statement is IF NOT EXISTS and seeding is
    INSERT OR IGNORE, so existing rows (with their stamps) are never
    reset by a re-ensure.
    """
    for statement in COMPLIANCE_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    now = _now_ms()
    for key, _label, _description in ITEM_DEFINITIONS:
        conn.execute(
            "INSERT OR IGNORE INTO compliance_items "
            "(key, status, evidence, note, updated_by, updated_at, created_at) "
            "VALUES (?, 'pending', NULL, NULL, NULL, NULL, ?)",
            (key, now),
        )
    _commit_quietly(conn)
