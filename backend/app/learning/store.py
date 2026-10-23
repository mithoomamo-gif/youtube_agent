"""SQLite store for the per-niche Learning Store (``learning_rules``).

A small store class over the table created by
:mod:`app.learning.schema`, following the codebase patterns of
:mod:`app.style_lab.store` / :mod:`app.execution.budget_reservations`:

* constructed with a ``conn_factory`` returning a fresh
  ``sqlite3.Connection`` per call (e.g.
  ``functools.partial(app.db.connect, path)``); a bare connection may
  also be passed, in which case it is used as-is and never closed;
* ids are ``uuid4().hex``; timestamps are epoch milliseconds;
* the schema is ensured lazily on every entry point, so the store
  works on databases that have not run migrations.

Scope isolation (the bounded-learning rule)
--------------------------------------------
Every rule belongs to exactly one ``channel_id`` **and** exactly one
``niche``.  There is deliberately **no unscoped read path**:

* every read / list method requires both ``channel_id`` and
  ``niche`` and filters on both — a rule written for channel A /
  niche X is invisible to channel B and to niche Y;
* :meth:`LearningStore.get_rule` returns ``None`` when the row
  belongs to a different (channel, niche) — a cross-scope read looks
  exactly like "not found", so existence never leaks;
* writes that target an existing row (:meth:`activate_rule`,
  :meth:`supersede_rule`) raise :class:`LearningScopeError` on a
  scope mismatch.

Versioning / supersession
-------------------------
``version`` is per (channel_id, niche, kind, statement_family).
A published rule is **never mutated in place**:
:meth:`supersede_rule` flips the old row to ``status='superseded'``
and inserts a new row with ``version + 1`` and ``supersedes_id``
pointing at the replaced row, in one transaction.  The new row
inherits the old row's lifecycle state (superseding an active rule
activates its replacement; superseding a candidate keeps the
replacement a candidate).  :meth:`activate_rule` promotes a
candidate to ``active`` and supersedes any other active row in the
same family, so a family has at most one active rule.
:meth:`supersede_rule` accepts an explicit ``statement_family``
override: when the replacement belongs to a DIFFERENT family than
the old row — the direction-flip case, where e.g. a
``...:positive`` rule is contradicted and its replacement says
``...:negative`` — the new row is written under the NEW family
(never the old one), the old-family row is superseded, and any
other active row in the new family is superseded too, so the two
families never hold co-active opposing rules.

Evidence policy (UNIFIED — Round 5 closure, R5-005 / R5L-004)
-------------------------------------------------------------
Activation requires ``evidence_count >=``
:data:`app.learning.schema.MIN_EVIDENCE_FOR_ACTIVATION` (= 3)
EVERYWHERE.  The store enforces it itself, as the last line of
defence: :meth:`activate_rule` raises
:class:`InsufficientEvidenceError` (a typed error, no state
change) below n=3, and a supersession that would inherit
``active`` status with a replacement evidence count below n=3
lands as a ``candidate`` instead.  The ``thin_evidence`` label
below is a separate DISPLAY band (n<5) and never blocks
activation at n=3–4.

Thin evidence
-------------
``thin_evidence`` (surfaced as ``is_thin_evidence``) is maintained on
every write: it is true while ``evidence_count`` is below
:data:`app.learning.schema.THIN_EVIDENCE_THRESHOLD`.  Thin-evidence
rules are labelled, never silently treated as established.  The
label is display-only: it does not gate activation (see the
evidence policy above).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Optional

from app.learning.schema import (
    MIN_EVIDENCE_FOR_ACTIVATION,
    RULE_KINDS,
    RULE_STATUSES,
    THIN_EVIDENCE_THRESHOLD,
)

__all__ = [
    "LearningStore",
    "LearningStoreError",
    "LearningNotFoundError",
    "LearningScopeError",
    "InsufficientEvidenceError",
    "MIN_EVIDENCE_FOR_ACTIVATION",
    "RULE_KINDS",
    "RULE_STATUSES",
    "THIN_EVIDENCE_THRESHOLD",
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    from app.learning.schema import ensure_learning_schema

    ensure_learning_schema(conn)


class LearningStoreError(Exception):
    """Base class for Learning Store errors."""


class LearningNotFoundError(LearningStoreError, LookupError):
    """A referenced learning rule row does not exist."""


class LearningScopeError(LearningStoreError, PermissionError):
    """A row exists but belongs to a different (channel, niche) scope."""


class InsufficientEvidenceError(LearningStoreError, ValueError):
    """Activation refused: ``evidence_count`` is below the unified
    activation threshold (:data:`MIN_EVIDENCE_FOR_ACTIVATION`).

    Typed (and a ``ValueError`` for backwards compatibility with
    callers that already treat store validation failures as
    ``ValueError``).  Raised BEFORE any write, so a refused
    activation changes no state."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


def _is_thin(evidence_count: int) -> bool:
    return int(evidence_count) < THIN_EVIDENCE_THRESHOLD


def _require_scope(channel_id: Any, niche: Any) -> tuple[str, str]:
    channel = str(channel_id).strip() if channel_id is not None else ""
    niche_s = str(niche).strip() if niche is not None else ""
    if not channel:
        raise ValueError("channel_id is required (rules are channel-scoped)")
    if not niche_s:
        raise ValueError("niche is required (rules are never pooled across niches)")
    return channel, niche_s


def _validate_kind(kind: Any) -> str:
    if kind not in RULE_KINDS:
        raise ValueError(f"rule kind must be one of {RULE_KINDS}, got {kind!r}")
    return str(kind)


def _validate_evidence(evidence_count: Any, confidence: Any) -> tuple[int, Optional[float]]:
    count = int(evidence_count)
    if count < 0:
        raise ValueError(f"evidence_count must be >= 0, got {count}")
    conf: Optional[float] = None
    if confidence is not None:
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"confidence must be within 0..1, got {conf}")
    return count, conf


class LearningStore:
    """Scoped CRUD + versioning over the ``learning_rules`` table."""

    def __init__(self, conn_factory: Any) -> None:
        """``conn_factory`` is a callable returning a fresh
        ``sqlite3.Connection``, or a single ``sqlite3.Connection`` to
        reuse (never closed by the store)."""
        # NOTE: sqlite3.Connection objects report callable() == True in
        # CPython, so the connection check must come first.
        if hasattr(conn_factory, "execute"):
            self._conn_factory = None
            self._shared_conn = conn_factory
        elif callable(conn_factory):
            self._conn_factory = conn_factory
            self._shared_conn: Optional[sqlite3.Connection] = None
        else:
            raise TypeError(
                "conn_factory must be callable or a sqlite3 connection"
            )

    # ------------------------------------------------------------------
    # connection / transaction plumbing (same style as app.style_lab.store)
    # ------------------------------------------------------------------
    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(conn, should_close)``."""
        if self._shared_conn is not None:
            conn = self._shared_conn
            should_close = False
        else:
            conn = self._conn_factory()  # type: ignore[misc]
            should_close = True
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        _ensure_schema(conn)
        return conn, should_close

    @contextmanager
    def _write_tx(self, conn: sqlite3.Connection):
        """BEGIN IMMEDIATE transaction; a no-op if one is already open
        (shared-connection callers manage their own transaction)."""
        if conn.in_transaction:
            yield conn
            return
        previous_isolation = conn.isolation_level
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            conn.execute("COMMIT")
        finally:
            try:
                conn.isolation_level = previous_isolation
            except Exception:
                pass

    @staticmethod
    def _close(conn: sqlite3.Connection, should_close: bool) -> None:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # row shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _rule_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        raw = data.get("created_from_json")
        created_from: list = []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                created_from = parsed
        data["created_from"] = created_from
        data["is_thin_evidence"] = bool(data.get("thin_evidence"))
        return data

    def _fetch_rule(self, conn, rule_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM learning_rules WHERE id = ?",
            (str(rule_id),),
        ).fetchone()
        return self._rule_dict(row)

    @staticmethod
    def _check_scope(row: Mapping[str, Any], channel_id: str, niche: str) -> None:
        if str(row["channel_id"]) != channel_id or str(row["niche"]) != niche:
            raise LearningScopeError(
                f"rule {row['id']} belongs to channel {row['channel_id']} / "
                f"niche {row['niche']}, not {channel_id} / {niche}"
            )

    def _next_version(
        self, conn, channel_id: str, niche: str, kind: str, family: str
    ) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM learning_rules"
            " WHERE channel_id = ? AND niche = ? AND kind = ?"
            " AND statement_family = ?",
            (channel_id, niche, kind, family),
        ).fetchone()
        return int(row[0]) + 1

    def _insert_rule(
        self,
        conn,
        *,
        channel_id: str,
        niche: str,
        kind: str,
        statement_family: str,
        version: int,
        statement: str,
        effect_value: Optional[float],
        effect_unit: Optional[str],
        evidence_count: int,
        confidence: Optional[float],
        status: str,
        supersedes_id: Optional[str],
        created_from: Iterable[Any],
    ) -> dict:
        now = _now_ms()
        row_id = _new_id()
        conn.execute(
            "INSERT INTO learning_rules ("
            " id, channel_id, niche, kind, statement_family, version,"
            " statement, effect_value, effect_unit, evidence_count,"
            " confidence, thin_evidence, status, supersedes_id,"
            " created_from_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                channel_id,
                niche,
                kind,
                statement_family,
                version,
                statement,
                effect_value,
                effect_unit,
                evidence_count,
                confidence,
                1 if _is_thin(evidence_count) else 0,
                status,
                supersedes_id,
                json.dumps([str(ref) for ref in created_from]),
                now,
                now,
            ),
        )
        result = self._fetch_rule(conn, row_id)
        assert result is not None
        return result

    # ==================================================================
    # Writes
    # ==================================================================
    def create_candidate(
        self,
        channel_id: str,
        niche: str,
        kind: str,
        statement: str,
        *,
        statement_family: Optional[str] = None,
        effect_value: Optional[float] = None,
        effect_unit: Optional[str] = None,
        evidence_count: int = 0,
        confidence: Optional[float] = None,
        created_from: Iterable[Any] = (),
    ) -> dict:
        """Create a ``candidate`` rule in scope (channel_id, niche).

        ``version`` is the next version for the rule's family
        (channel_id, niche, kind, statement_family): 1 for a new
        family, otherwise max(existing) + 1 — so a candidate written
        against an already-published family never reuses its version
        (activation / supersession semantics stay unambiguous).
        ``statement_family`` defaults to ``statement`` itself.
        """
        channel, niche_s = _require_scope(channel_id, niche)
        kind_s = _validate_kind(kind)
        statement_s = str(statement).strip()
        if not statement_s:
            raise ValueError("statement must be non-empty")
        family = str(statement_family).strip() if statement_family else statement_s
        count, conf = _validate_evidence(evidence_count, confidence)

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                version = self._next_version(conn, channel, niche_s, kind_s, family)
                return self._insert_rule(
                    conn,
                    channel_id=channel,
                    niche=niche_s,
                    kind=kind_s,
                    statement_family=family,
                    version=version,
                    statement=statement_s,
                    effect_value=effect_value,
                    effect_unit=effect_unit,
                    evidence_count=count,
                    confidence=conf,
                    status="candidate",
                    supersedes_id=None,
                    created_from=created_from or (),
                )
        finally:
            self._close(conn, should_close)

    def activate_rule(
        self,
        rule_id: Any,
        *,
        channel_id: str,
        niche: str,
    ) -> Optional[dict]:
        """Promote a candidate rule to ``active``; return the row.

        Any other ``active`` row in the same family (channel, niche,
        kind, statement_family) is flipped to ``superseded`` in the
        same transaction, so a family has at most one active rule.
        Returns ``None`` for an unknown id; raises
        :class:`LearningScopeError` on a scope mismatch and
        ``ValueError`` when the rule is already superseded (a
        superseded rule is immutable history — supersede its
        replacement or create a fresh candidate instead).

        Evidence gate (the store as last line of defence, unified
        policy R5-005 / R5L-004): a rule whose ``evidence_count``
        is below :data:`MIN_EVIDENCE_FOR_ACTIVATION` (n>=3) is
        REFUSED with :class:`InsufficientEvidenceError` and no
        state change — no caller can activate a thin rule by
        bypassing the engine.  The ``is_thin_evidence`` display
        label (n<5) is NOT a gate: a rule with n=3–4 activates
        normally and stays labelled thin.
        """
        channel, niche_s = _require_scope(channel_id, niche)
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                rule = self._fetch_rule(conn, rule_id)
                if rule is None:
                    return None
                self._check_scope(rule, channel, niche_s)
                if rule["status"] == "superseded":
                    raise ValueError(
                        f"rule {rule_id} is superseded and cannot be activated"
                    )
                if int(rule["evidence_count"]) < MIN_EVIDENCE_FOR_ACTIVATION:
                    raise InsufficientEvidenceError(
                        f"rule {rule_id} has evidence_count="
                        f"{int(rule['evidence_count'])}, below the "
                        f"activation minimum "
                        f"{MIN_EVIDENCE_FOR_ACTIVATION}; activation "
                        "refused (no state change)"
                    )
                now = _now_ms()
                conn.execute(
                    "UPDATE learning_rules SET status = 'superseded',"
                    " updated_at = ? WHERE channel_id = ? AND niche = ?"
                    " AND kind = ? AND statement_family = ?"
                    " AND status = 'active' AND id != ?",
                    (
                        now,
                        channel,
                        niche_s,
                        rule["kind"],
                        rule["statement_family"],
                        rule["id"],
                    ),
                )
                conn.execute(
                    "UPDATE learning_rules SET status = 'active',"
                    " updated_at = ? WHERE id = ?",
                    (now, rule["id"]),
                )
                return self._fetch_rule(conn, rule["id"])
        finally:
            self._close(conn, should_close)

    def supersede_rule(
        self,
        rule_id: Any,
        *,
        channel_id: str,
        niche: str,
        statement: Optional[str] = None,
        statement_family: Optional[str] = None,
        effect_value: Optional[float] = None,
        effect_unit: Optional[str] = None,
        evidence_count: Optional[int] = None,
        confidence: Optional[float] = None,
        created_from: Optional[Iterable[Any]] = None,
    ) -> Optional[dict]:
        """Replace a rule with a new version; return the NEW row.

        The old row is flipped to ``status='superseded'`` and a new
        row is inserted with ``version + 1`` and ``supersedes_id``
        pointing at it — the old row's statement / effect / evidence
        are never mutated, so history stays auditable.  Fields left
        as ``None`` are inherited from the old row; pass them
        explicitly to change them.  The new row inherits the old
        row's status (active → active, candidate → candidate) —
        with one policy exception: an ``active`` inheritance whose
        replacement evidence count is below
        :data:`MIN_EVIDENCE_FOR_ACTIVATION` lands as ``candidate``
        instead, so no store path can produce an active rule below
        the unified n>=3 gate.

        ``statement_family``: pass it to move the replacement into
        a DIFFERENT family — the direction-flip case (R5L-002),
        where the old rule's family encodes the direction the new
        evidence contradicts (``...:positive``) and the replacement
        belongs under the NEW family (``...:negative``).  The new
        row is then written under the new family with the chain
        version (old ``version + 1``, never below the new family's
        next version), and any other active row in the new family
        is superseded in the same transaction, so opposing families
        never hold co-active rules for one attribute.  Left as
        ``None`` the family is inherited, as before.

        Returns ``None`` for an unknown id; raises
        :class:`LearningScopeError` on a scope mismatch and
        ``ValueError`` when the target is already superseded.
        """
        channel, niche_s = _require_scope(channel_id, niche)
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                old = self._fetch_rule(conn, rule_id)
                if old is None:
                    return None
                self._check_scope(old, channel, niche_s)
                if old["status"] == "superseded":
                    raise ValueError(
                        f"rule {rule_id} is already superseded; supersede "
                        "its current replacement instead"
                    )
                new_statement = (
                    str(statement).strip() if statement is not None
                    else old["statement"]
                )
                if not new_statement:
                    raise ValueError("statement must be non-empty")
                count, conf = _validate_evidence(
                    old["evidence_count"] if evidence_count is None else evidence_count,
                    old["confidence"] if confidence is None else confidence,
                )
                # Family: explicit override wins (direction flip,
                # R5L-002); otherwise the replacement stays in the
                # old row's family, as before.
                new_family = (
                    str(statement_family).strip()
                    if statement_family is not None
                    else old["statement_family"]
                )
                if not new_family:
                    raise ValueError("statement_family must be non-empty")
                # Chain version: old + 1, and never below the target
                # family's next version (a family never reuses one).
                new_version = max(
                    int(old["version"]) + 1,
                    self._next_version(
                        conn, channel, niche_s, old["kind"], new_family
                    ),
                )
                # Status: inherit, except an active inheritance below
                # the activation gate lands as candidate (unified
                # policy — no active rule below n>=3, by any path).
                new_status = old["status"]
                if (
                    new_status == "active"
                    and count < MIN_EVIDENCE_FOR_ACTIVATION
                ):
                    new_status = "candidate"
                now = _now_ms()
                conn.execute(
                    "UPDATE learning_rules SET status = 'superseded',"
                    " updated_at = ? WHERE id = ?",
                    (now, old["id"]),
                )
                if new_status == "active" and new_family != old["statement_family"]:
                    # Cross-family move: the target family keeps at
                    # most one active rule too — supersede any other
                    # active row already sitting in it.
                    conn.execute(
                        "UPDATE learning_rules SET status = 'superseded',"
                        " updated_at = ? WHERE channel_id = ? AND niche = ?"
                        " AND kind = ? AND statement_family = ?"
                        " AND status = 'active' AND id != ?",
                        (
                            now,
                            channel,
                            niche_s,
                            old["kind"],
                            new_family,
                            old["id"],
                        ),
                    )
                return self._insert_rule(
                    conn,
                    channel_id=channel,
                    niche=niche_s,
                    kind=old["kind"],
                    statement_family=new_family,
                    version=new_version,
                    statement=new_statement,
                    effect_value=(
                        old["effect_value"] if effect_value is None else effect_value
                    ),
                    effect_unit=(
                        old["effect_unit"] if effect_unit is None else effect_unit
                    ),
                    evidence_count=count,
                    confidence=conf,
                    status=new_status,
                    supersedes_id=old["id"],
                    created_from=(
                        old["created_from"] if created_from is None else created_from
                    ),
                )
        finally:
            self._close(conn, should_close)

    # ==================================================================
    # Reads — every path is scoped by (channel_id, niche); there is
    # deliberately no unscoped read.
    # ==================================================================
    def get_rule(
        self,
        rule_id: Any,
        *,
        channel_id: str,
        niche: str,
    ) -> Optional[dict]:
        """One rule by id within scope, or ``None``.

        A rule owned by a different channel or niche reads as
        ``None`` (no existence leak across scopes).
        """
        channel, niche_s = _require_scope(channel_id, niche)
        conn, should_close = self._connect()
        try:
            rule = self._fetch_rule(conn, rule_id)
            if rule is None:
                return None
            if str(rule["channel_id"]) != channel or str(rule["niche"]) != niche_s:
                return None
            return rule
        finally:
            self._close(conn, should_close)

    def list_active_rules(self, channel_id: str, niche: str) -> list[dict]:
        """All ``active`` rules for exactly (channel_id, niche)."""
        return self._list_rules(channel_id, niche, status="active")

    def list_candidate_rules(self, channel_id: str, niche: str) -> list[dict]:
        """All ``candidate`` rules for exactly (channel_id, niche)."""
        return self._list_rules(channel_id, niche, status="candidate")

    def list_rule_history(
        self,
        channel_id: str,
        niche: str,
        *,
        kind: Optional[str] = None,
        statement_family: Optional[str] = None,
    ) -> list[dict]:
        """Full rule history for exactly (channel_id, niche).

        Every status (candidate / active / superseded), ordered by
        family then version ascending — the version chain of each
        family reads oldest → newest.  Optional ``kind`` /
        ``statement_family`` filters stay inside the same scope.
        """
        channel, niche_s = _require_scope(channel_id, niche)
        sql = (
            "SELECT * FROM learning_rules WHERE channel_id = ? AND niche = ?"
        )
        params: list[Any] = [channel, niche_s]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(_validate_kind(kind))
        if statement_family is not None:
            sql += " AND statement_family = ?"
            params.append(str(statement_family))
        sql += (
            " ORDER BY kind ASC, statement_family ASC, version ASC,"
            " created_at ASC, rowid ASC"
        )
        conn, should_close = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._rule_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def _list_rules(
        self, channel_id: str, niche: str, *, status: str
    ) -> list[dict]:
        channel, niche_s = _require_scope(channel_id, niche)
        conn, should_close = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM learning_rules WHERE channel_id = ?"
                " AND niche = ? AND status = ?"
                " ORDER BY kind ASC, statement_family ASC, version ASC,"
                " created_at ASC, rowid ASC",
                (channel, niche_s, status),
            ).fetchall()
            return [d for d in (self._rule_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)
