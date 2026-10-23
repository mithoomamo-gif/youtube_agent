"""Research consumption seam for the Learning Store (Round 5).

This module is how the research / topic-planning path *consumes*
learning.  It reads the ACTIVE rules for exactly one
``(channel_id, niche)`` scope from the sibling Learning Store
(:class:`app.learning.store.LearningStore` — landed; read API:
``list_active_rules(channel_id, niche)``) and returns them to the
caller as structured DATA next to ranked topic suggestions.

Bounded-learning guarantees (MASTER_ROADMAP: learning is bounded —
rules are consumed as data; prompts are NOT rewritten by learning;
no pooling across niches/channels):

* **Data only, never prompt text.**  Nothing in this module opens,
  writes or assembles a prompt template, and no rule statement is
  ever injected into prompt text.  Rules travel *alongside* the
  research output as structured fields (``applied_rules`` /
  ``why.supporting_rules``) so a human — or a later, separately
  approved step — can see exactly what learning said.  The research
  stage that integrates this seam
  (:func:`app.integration.stage_bridge.run_research`) records the
  applied rules in its brief and changes no prompt and no topic
  selection because of them.
* **Scope is exact.**  Rules are fetched through the store's scoped
  read, which requires both ``channel_id`` and ``niche`` and returns
  only ``status='active'`` rows for that pair.  Candidate /
  superseded rules and rules from any other channel or niche can
  never appear here.
* **Provenance is mandatory and honest.**  Every suggestion carries a
  ``why`` block.  A suggestion matched by at least one active
  ``topic_affinity`` rule is labelled ``learning_rule`` and lists the
  supporting rule ids with their versions, evidence counts and
  confidences.  A suggestion with no rule support is labelled
  ``baseline_no_evidence`` with an empty supporting list and
  ``confidence=None`` — it is never dressed up as learned.
* **Application is visible.**  The result records *every* active rule
  that was fetched for the scope (``applied_rules``: rule id +
  version + kind), whether or not it matched a particular topic, so
  the research report can show exactly which rules were applied.

Matching rule (deliberately narrow): only ``topic_affinity`` rules
can *support* a topic suggestion — the other kinds (hook, duration,
title, thumbnail, publish window) are recorded as applied context
but are never claimed as evidence for a topic they do not describe.
A topic_affinity rule supports a topic when they share at least one
meaningful token (lowercased word of length >= 3, stopwords removed)
across the rule's ``statement`` + ``statement_family`` and the topic
text.  Ranking is deterministic: rule-backed suggestions first,
ordered by total supporting evidence (desc), then best confidence
(desc), then original candidate order; baseline suggestions follow
in their original order.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

__all__ = [
    "BASELINE_BASIS",
    "RULE_BASIS",
    "TOPIC_AFFINITY_KIND",
    "build_topic_suggestions",
    "fetch_active_rules",
    "normalise_rule",
    "rank_topic_suggestions",
    "rule_supports_topic",
]

#: ``why.basis`` / ``score_basis`` for a rule-backed suggestion.
RULE_BASIS = "learning_rule"
#: ``why.basis`` / ``score_basis`` for a suggestion with no rule support.
BASELINE_BASIS = "baseline_no_evidence"

#: The only rule kind that can support a *topic* suggestion.
TOPIC_AFFINITY_KIND = "topic_affinity"

_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are",
    "was", "were", "with", "for", "per", "via", "ke", "ki", "ka", "aur",
    "mein", "hai", "hain", "ye", "yeh", "woh", "kya", "kyun", "par",
    "topics", "topic", "videos", "video", "channel", "baseline",
    "outperform", "outperforms", "underperform", "underperforms",
}


def _tokens(text: Any) -> set[str]:
    out: set[str] = set()
    for word in _TOKEN_RE.findall(str(text or "")):
        low = word.lower()
        if len(low) >= 3 and low not in _STOPWORDS:
            out.add(low)
    return out


# ---------------------------------------------------------------------------
# Rule snapshots
# ---------------------------------------------------------------------------


def normalise_rule(rule: Any) -> dict:
    """A store rule row -> the stable snapshot shape consumers record.

    Keeps the identity (``rule_id`` + ``version``) the research report
    must show, plus the evidence fields provenance is built from.
    Unknown/missing fields degrade to ``None``/``0`` — never invented.
    """
    if not isinstance(rule, dict):
        rule = dict(vars(rule)) if hasattr(rule, "__dict__") else {}
    confidence = rule.get("confidence")
    return {
        "rule_id": str(rule.get("id") or rule.get("rule_id") or ""),
        "version": int(rule.get("version") or 1),
        "kind": str(rule.get("kind") or ""),
        "statement": str(rule.get("statement") or ""),
        "statement_family": str(rule.get("statement_family") or ""),
        "evidence_count": int(rule.get("evidence_count") or 0),
        "confidence": float(confidence) if confidence is not None else None,
        "effect_value": rule.get("effect_value"),
        "effect_unit": rule.get("effect_unit"),
        "is_thin_evidence": bool(
            rule.get("is_thin_evidence", rule.get("thin_evidence", 0))
        ),
    }


def fetch_active_rules(
    channel_id: str,
    niche: str,
    *,
    store: Any = None,
    conn_factory: Any = None,
    conn: Any = None,
) -> list[dict]:
    """Active rules for exactly ``(channel_id, niche)``, normalised.

    Source resolution, in order: an explicit ``store`` (anything with
    ``list_active_rules(channel_id, niche)`` — the landed
    :class:`app.learning.store.LearningStore` satisfies this), a
    ``conn_factory`` / bare ``conn`` (wrapped in a fresh
    ``LearningStore``), else the app's default DB connection factory.
    There is deliberately no unscoped fallback.
    """
    channel = str(channel_id or "").strip()
    niche_s = str(niche or "").strip()
    if not channel:
        raise ValueError("channel_id is required (rules are channel-scoped)")
    if not niche_s:
        raise ValueError("niche is required (rules are never pooled across niches)")

    resolved = store
    if resolved is None:
        from app.learning.store import LearningStore

        if conn is not None:
            resolved = LearningStore(conn)
        elif conn_factory is not None:
            resolved = LearningStore(conn_factory)
        else:
            from app import db as _db

            resolved = LearningStore(_db.get_connection)
    rows = resolved.list_active_rules(channel, niche_s)
    return [normalise_rule(r) for r in rows]


# ---------------------------------------------------------------------------
# Topic matching + ranking
# ---------------------------------------------------------------------------


def rule_supports_topic(rule: dict, topic: str) -> bool:
    """True when an active ``topic_affinity`` rule describes ``topic``.

    Narrow by design (see module docstring): non-topic kinds never
    support a topic, and support requires a shared meaningful token
    between the topic and the rule's statement / statement family.
    """
    if str(rule.get("kind") or "") != TOPIC_AFFINITY_KIND:
        return False
    rule_tokens = _tokens(rule.get("statement")) | _tokens(
        rule.get("statement_family")
    )
    if not rule_tokens:
        return False
    return bool(rule_tokens & _tokens(topic))


def _support_entry(rule: dict) -> dict:
    """The per-rule provenance record inside a suggestion's ``why``."""
    return {
        "rule_id": rule["rule_id"],
        "version": rule["version"],
        "kind": rule["kind"],
        "statement": rule["statement"],
        "evidence_count": rule["evidence_count"],
        "confidence": rule["confidence"],
    }


def rank_topic_suggestions(
    candidate_topics: Iterable[Any], rules: list[dict]
) -> list[dict]:
    """Rank ``candidate_topics`` against normalised active ``rules``.

    Pure function of its inputs (no I/O, no prompt access): the same
    candidates + rules always produce the same ranked suggestions.
    Every returned suggestion carries its ``why`` provenance block.
    """
    topics: list[str] = []
    for cand in candidate_topics or []:
        text = cand.get("topic") if isinstance(cand, dict) else cand
        text = str(text or "").strip()
        if text and text not in topics:
            topics.append(text)

    scored: list[tuple[int, dict]] = []
    for idx, topic in enumerate(topics):
        supporters = [r for r in rules if rule_supports_topic(r, topic)]
        if supporters:
            entries = [_support_entry(r) for r in supporters]
            evidence_total = sum(e["evidence_count"] for e in entries)
            confidences = [
                e["confidence"] for e in entries if e["confidence"] is not None
            ]
            why = {
                "basis": RULE_BASIS,
                "label": (
                    f"Supported by {len(entries)} active learning "
                    f"rule(s) for this channel+niche"
                ),
                "supporting_rules": entries,
                "evidence_count": evidence_total,
                "confidence": max(confidences) if confidences else None,
            }
            sort_key = (0, -evidence_total, -(why["confidence"] or 0.0), idx)
        else:
            why = {
                "basis": BASELINE_BASIS,
                "label": (
                    "Baseline — no learning-rule evidence for this "
                    "channel+niche"
                ),
                "supporting_rules": [],
                "evidence_count": 0,
                "confidence": None,
            }
            sort_key = (1, 0, 0.0, idx)
        suggestion = {
            "topic": topic,
            "score_basis": why["basis"],
            "supporting_rule_ids": [
                e["rule_id"] for e in why["supporting_rules"]
            ],
            "why": why,
        }
        scored.append((idx, {**suggestion, "_sort": sort_key}))

    scored.sort(key=lambda pair: pair[1]["_sort"])
    out: list[dict] = []
    for rank, (_idx, suggestion) in enumerate(scored, start=1):
        suggestion.pop("_sort", None)
        suggestion["rank"] = rank
        out.append(suggestion)
    return out


def build_topic_suggestions(
    channel_id: str,
    niche: str,
    candidate_topics: Iterable[Any],
    *,
    store: Any = None,
    conn_factory: Any = None,
    conn: Any = None,
) -> dict:
    """The consumption seam's entry point: fetch rules + rank topics.

    Returns the full research-facing record: the scope, every active
    rule that was applied (id + version visible), and the ranked
    suggestions with per-suggestion provenance.  This dict is DATA
    for the research report — it is never merged into a prompt.
    """
    rules = fetch_active_rules(
        channel_id, niche, store=store, conn_factory=conn_factory, conn=conn
    )
    suggestions = rank_topic_suggestions(candidate_topics, rules)
    applied = [
        {
            "rule_id": r["rule_id"],
            "version": r["version"],
            "kind": r["kind"],
            "statement_family": r["statement_family"],
        }
        for r in rules
    ]
    return {
        "channel_id": str(channel_id),
        "niche": str(niche),
        "source": "learning_store.active_rules",
        "rules_considered": len(rules),
        "applied_rules": applied,
        "applied_rule_ids": [r["rule_id"] for r in applied],
        "suggestions": suggestions,
    }
