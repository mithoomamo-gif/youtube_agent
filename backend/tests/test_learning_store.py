"""Learning Store: CRUD, versioning/supersession, scope isolation.

Two owner channels (A and B) and two niches (facts and horror) are
seeded in the real section-15 schema; every isolation assertion
checks that a rule written for channel A / niche facts is invisible
to channel B and to niche horror at the store level — the store has
no unscoped read path.
"""

from __future__ import annotations

import inspect
import time
import uuid

import pytest

from app.learning.store import (
    InsufficientEvidenceError,
    LearningScopeError,
    LearningStore,
)
from tests.execution_fixtures import make_db


def _seed_channel(factory, label: str) -> str:
    channel_id = str(uuid.uuid4())
    ts = int(time.time() * 1000)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'en-US',"
            " 'active', ?, ?)",
            (channel_id, f"UC-owner-{label}-{uuid.uuid4().hex[:8]}",
             f"Owner {label}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    return {
        "factory": factory,
        "store": LearningStore(factory),
        "channel_a": _seed_channel(factory, "A"),
        "channel_b": _seed_channel(factory, "B"),
    }


def _candidate(env, channel=None, niche="facts", **kwargs):
    kwargs.setdefault("kind", "hook_pattern")
    kwargs.setdefault("statement", "Question hooks retain +18% in first 30s")
    kwargs.setdefault("statement_family", "question-hook")
    return env["store"].create_candidate(
        channel or env["channel_a"], niche, **kwargs
    )


# ---------------------------------------------------------------------------
# Create + read
# ---------------------------------------------------------------------------


def test_create_candidate_defaults(env):
    rule = _candidate(env, evidence_count=7, confidence=0.8,
                      effect_value=18.0, effect_unit="pct_retention_first_30s",
                      created_from=["snap-1", "vid-2"])
    assert rule["channel_id"] == env["channel_a"]
    assert rule["niche"] == "facts"
    assert rule["kind"] == "hook_pattern"
    assert rule["statement_family"] == "question-hook"
    assert rule["version"] == 1
    assert rule["status"] == "candidate"
    assert rule["effect_value"] == 18.0
    assert rule["effect_unit"] == "pct_retention_first_30s"
    assert rule["evidence_count"] == 7
    assert rule["confidence"] == 0.8
    assert rule["created_from"] == ["snap-1", "vid-2"]
    assert rule["supersedes_id"] is None
    # Round-trips through get_rule in the same scope.
    fetched = env["store"].get_rule(
        rule["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert fetched == rule


def test_create_candidate_validation(env):
    store = env["store"]
    with pytest.raises(ValueError):
        store.create_candidate(env["channel_a"], "facts", "bogus_kind", "x")
    with pytest.raises(ValueError):
        store.create_candidate(env["channel_a"], "facts", "hook_pattern", "  ")
    with pytest.raises(ValueError):
        store.create_candidate("", "facts", "hook_pattern", "x")
    with pytest.raises(ValueError):
        store.create_candidate(env["channel_a"], "", "hook_pattern", "x")
    with pytest.raises(ValueError):
        _candidate(env, evidence_count=-1)
    with pytest.raises(ValueError):
        _candidate(env, confidence=1.5)


def test_statement_family_defaults_to_statement(env):
    rule = env["store"].create_candidate(
        env["channel_a"], "facts", "title_pattern", "Titles with numbers win"
    )
    assert rule["statement_family"] == "Titles with numbers win"


def test_all_rule_kinds_accepted(env):
    store = env["store"]
    for kind in (
        "topic_affinity", "hook_pattern", "duration_band",
        "title_pattern", "thumbnail_style", "publish_window",
    ):
        rule = store.create_candidate(
            env["channel_a"], "facts", kind, f"{kind} statement",
            statement_family=f"fam-{kind}",
        )
        assert rule["kind"] == kind


# ---------------------------------------------------------------------------
# Thin-evidence labelling
# ---------------------------------------------------------------------------


def test_thin_evidence_label_present_and_tracks_count(env):
    thin = _candidate(env, evidence_count=2)
    assert thin["thin_evidence"] == 1
    assert thin["is_thin_evidence"] is True

    solid = _candidate(env, statement_family="other-family", evidence_count=5)
    assert solid["thin_evidence"] == 0
    assert solid["is_thin_evidence"] is False

    zero = _candidate(env, statement_family="third-family", evidence_count=0)
    assert zero["is_thin_evidence"] is True


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def test_activate_and_list_active(env):
    store = env["store"]
    # R5-005 POLICY UPDATE (flagged): this test previously activated
    # a 0-evidence candidate, which the DECIDED unified policy now
    # forbids — the store itself refuses activation below n>=3.
    # The candidate is therefore seeded at exactly the threshold.
    rule = _candidate(env, evidence_count=3)
    assert store.list_active_rules(env["channel_a"], "facts") == []
    assert [r["id"] for r in store.list_candidate_rules(
        env["channel_a"], "facts")] == [rule["id"]]

    activated = store.activate_rule(
        rule["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert activated["status"] == "active"
    # n=3 activates AND stays labelled thin (display band is n<5):
    # the decided policy in one row.
    assert activated["is_thin_evidence"] is True
    assert [r["id"] for r in store.list_active_rules(
        env["channel_a"], "facts")] == [rule["id"]]
    assert store.list_candidate_rules(env["channel_a"], "facts") == []


def test_activate_supersedes_previous_active_in_family(env):
    store = env["store"]
    # Evidence >= 3 on both (R5-005 store gate — see above).
    first = _candidate(env, evidence_count=3)
    store.activate_rule(first["id"], channel_id=env["channel_a"], niche="facts")
    second = _candidate(env, statement="Question hooks retain +22%",
                        evidence_count=4)
    assert second["version"] == 2  # next version within the family
    store.activate_rule(second["id"], channel_id=env["channel_a"], niche="facts")

    active = store.list_active_rules(env["channel_a"], "facts")
    assert [r["id"] for r in active] == [second["id"]]
    old = store.get_rule(first["id"], channel_id=env["channel_a"], niche="facts")
    assert old["status"] == "superseded"


def test_activate_unknown_and_superseded(env):
    store = env["store"]
    assert store.activate_rule(
        "nope", channel_id=env["channel_a"], niche="facts"
    ) is None
    rule = _candidate(env)
    replacement = store.supersede_rule(
        rule["id"], channel_id=env["channel_a"], niche="facts",
        statement="Revised statement",
    )
    assert replacement is not None
    with pytest.raises(ValueError):
        store.activate_rule(
            rule["id"], channel_id=env["channel_a"], niche="facts"
        )


# ---------------------------------------------------------------------------
# Store-level evidence gate (R5-005 / R5L-004 — DECIDED policy:
# activation threshold n>=3 EVERYWHERE, enforced by the store itself;
# the n<5 'thin' label is a display band only and never blocks n=3–4)
# ---------------------------------------------------------------------------


def test_activate_refuses_zero_evidence_with_typed_error(env):
    store = env["store"]
    rule = _candidate(env, evidence_count=0)
    with pytest.raises(InsufficientEvidenceError):
        store.activate_rule(
            rule["id"], channel_id=env["channel_a"], niche="facts"
        )
    # No state change: still a candidate, nothing active in scope.
    after = store.get_rule(
        rule["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert after["status"] == "candidate"
    assert store.list_active_rules(env["channel_a"], "facts") == []
    # The typed error is also a ValueError (store validation family).
    assert issubclass(InsufficientEvidenceError, ValueError)


def test_activate_refuses_below_threshold_and_allows_at_threshold(env):
    store = env["store"]
    for n in (1, 2):
        rule = _candidate(
            env, statement_family=f"gate-{n}", evidence_count=n
        )
        with pytest.raises(InsufficientEvidenceError):
            store.activate_rule(
                rule["id"], channel_id=env["channel_a"], niche="facts"
            )
        assert store.get_rule(
            rule["id"], channel_id=env["channel_a"], niche="facts"
        )["status"] == "candidate"
    # Exactly n=3 activates — and is still labelled thin (n<5 band).
    at_gate = _candidate(env, statement_family="gate-3", evidence_count=3)
    activated = store.activate_rule(
        at_gate["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert activated["status"] == "active"
    assert activated["evidence_count"] == 3
    assert activated["is_thin_evidence"] is True
    # n=5 clears the thin display band as well.
    solid = _candidate(env, statement_family="gate-5", evidence_count=5)
    activated5 = store.activate_rule(
        solid["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert activated5["status"] == "active"
    assert activated5["is_thin_evidence"] is False


def test_supersede_active_below_gate_lands_as_candidate(env):
    """No store path may produce an active rule below n>=3: a
    supersession replacing an active rule with sub-threshold
    evidence inherits candidate status, not active."""
    store = env["store"]
    original = _candidate(env, evidence_count=6)
    store.activate_rule(
        original["id"], channel_id=env["channel_a"], niche="facts"
    )
    new = store.supersede_rule(
        original["id"], channel_id=env["channel_a"], niche="facts",
        statement="Same claim, far less evidence", evidence_count=2,
    )
    assert new["status"] == "candidate"
    assert store.list_active_rules(env["channel_a"], "facts") == []


# ---------------------------------------------------------------------------
# Supersession / versioning — published rules are never mutated
# ---------------------------------------------------------------------------


def test_supersede_active_rule_versions_and_preserves_old_row(env):
    store = env["store"]
    original = _candidate(
        env, evidence_count=6, confidence=0.7,
        effect_value=18.0, effect_unit="pct", created_from=["snap-1"],
    )
    store.activate_rule(
        original["id"], channel_id=env["channel_a"], niche="facts"
    )

    new = store.supersede_rule(
        original["id"], channel_id=env["channel_a"], niche="facts",
        statement="Question hooks retain +24% in first 30s",
        evidence_count=11, created_from=["snap-1", "snap-9"],
    )
    assert new["version"] == original["version"] + 1
    assert new["supersedes_id"] == original["id"]
    assert new["status"] == "active"  # inherits the replaced rule's state
    assert new["statement"] == "Question hooks retain +24% in first 30s"
    assert new["evidence_count"] == 11
    assert new["confidence"] == 0.7  # inherited when not passed
    assert new["created_from"] == ["snap-1", "snap-9"]
    assert new["is_thin_evidence"] is False

    # The old row is preserved verbatim apart from its status flip.
    old = store.get_rule(
        original["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert old["status"] == "superseded"
    assert old["statement"] == original["statement"]
    assert old["effect_value"] == 18.0
    assert old["evidence_count"] == 6
    assert old["version"] == original["version"]

    active = store.list_active_rules(env["channel_a"], "facts")
    assert [r["id"] for r in active] == [new["id"]]


def test_supersede_candidate_stays_candidate_and_chains(env):
    store = env["store"]
    v1 = _candidate(env)
    v2 = store.supersede_rule(
        v1["id"], channel_id=env["channel_a"], niche="facts",
        statement="Second take",
    )
    assert v2["status"] == "candidate"
    assert v2["version"] == 2
    v3 = store.supersede_rule(
        v2["id"], channel_id=env["channel_a"], niche="facts",
        statement="Third take",
    )
    assert v3["version"] == 3
    assert v3["supersedes_id"] == v2["id"]

    history = store.list_rule_history(env["channel_a"], "facts")
    assert [(r["version"], r["status"]) for r in history] == [
        (1, "superseded"), (2, "superseded"), (3, "candidate"),
    ]
    # Superseding an already-superseded row is refused.
    with pytest.raises(ValueError):
        store.supersede_rule(
            v1["id"], channel_id=env["channel_a"], niche="facts",
            statement="Zombie",
        )


def test_supersede_into_new_family_direction_flip(env):
    """R5L-002: a direction flip writes the replacement under the
    NEW statement_family; the old-family row is superseded and the
    two families never hold co-active opposing rules."""
    store = env["store"]
    original = store.create_candidate(
        env["channel_a"], "facts", "topic_affinity",
        "topic_affinity 'ocean' outperforms the facts channel "
        "baseline by 14.3% (shrunk, n=3)",
        statement_family="topic_affinity:ocean:positive",
        evidence_count=3, confidence=0.4,
    )
    store.activate_rule(
        original["id"], channel_id=env["channel_a"], niche="facts"
    )

    flipped = store.supersede_rule(
        original["id"], channel_id=env["channel_a"], niche="facts",
        statement="topic_affinity 'ocean' underperforms the facts "
                  "channel baseline by 28.6% (shrunk, n=3)",
        statement_family="topic_affinity:ocean:negative",
        evidence_count=3,
    )
    assert flipped["statement_family"] == "topic_affinity:ocean:negative"
    assert flipped["version"] == original["version"] + 1
    assert flipped["supersedes_id"] == original["id"]
    assert flipped["status"] == "active"  # active inheritance, n>=3

    old = store.get_rule(
        original["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert old["status"] == "superseded"
    assert old["statement_family"] == "topic_affinity:ocean:positive"

    # Exactly ONE active ocean rule, and it is the negative one.
    active = [
        r for r in store.list_active_rules(env["channel_a"], "facts")
        if "ocean" in r["statement_family"]
    ]
    assert [r["id"] for r in active] == [flipped["id"]]
    # Family-keyed history: the negative family holds the live rule,
    # the positive family holds only the superseded original.
    neg = store.list_rule_history(
        env["channel_a"], "facts", kind="topic_affinity",
        statement_family="topic_affinity:ocean:negative",
    )
    assert [(r["version"], r["status"]) for r in neg] == [(2, "active")]
    pos = store.list_rule_history(
        env["channel_a"], "facts", kind="topic_affinity",
        statement_family="topic_affinity:ocean:positive",
    )
    assert [(r["version"], r["status"]) for r in pos] == [(1, "superseded")]


def test_history_filters_stay_in_scope(env):
    store = env["store"]
    _candidate(env)
    _candidate(env, kind="title_pattern", statement_family="number-title",
               statement="Number titles win")
    assert len(store.list_rule_history(env["channel_a"], "facts")) == 2
    hooks = store.list_rule_history(
        env["channel_a"], "facts", kind="hook_pattern"
    )
    assert len(hooks) == 1 and hooks[0]["kind"] == "hook_pattern"
    fam = store.list_rule_history(
        env["channel_a"], "facts", statement_family="number-title"
    )
    assert len(fam) == 1 and fam[0]["kind"] == "title_pattern"


# ---------------------------------------------------------------------------
# Scope isolation — channel AND niche, on every path
# ---------------------------------------------------------------------------


def test_scope_isolation_reads(env):
    store = env["store"]
    rule = _candidate(env, evidence_count=6)  # >= 3: store gate (R5-005)
    store.activate_rule(rule["id"], channel_id=env["channel_a"], niche="facts")

    # Channel B sees nothing of channel A's rules.
    assert store.get_rule(
        rule["id"], channel_id=env["channel_b"], niche="facts"
    ) is None
    assert store.list_active_rules(env["channel_b"], "facts") == []
    assert store.list_candidate_rules(env["channel_b"], "facts") == []
    assert store.list_rule_history(env["channel_b"], "facts") == []

    # Niche horror on the SAME channel also sees nothing.
    assert store.get_rule(
        rule["id"], channel_id=env["channel_a"], niche="horror"
    ) is None
    assert store.list_active_rules(env["channel_a"], "horror") == []
    assert store.list_rule_history(env["channel_a"], "horror") == []

    # A same-kind rule in the other niche does not collide on version.
    other = _candidate(env, niche="horror")
    assert other["version"] == 1
    assert len(store.list_rule_history(env["channel_a"], "horror")) == 1
    assert len(store.list_rule_history(env["channel_a"], "facts")) == 1


def test_scope_isolation_writes_raise(env):
    store = env["store"]
    rule = _candidate(env)
    with pytest.raises(LearningScopeError):
        store.activate_rule(
            rule["id"], channel_id=env["channel_b"], niche="facts"
        )
    with pytest.raises(LearningScopeError):
        store.activate_rule(
            rule["id"], channel_id=env["channel_a"], niche="horror"
        )
    with pytest.raises(LearningScopeError):
        store.supersede_rule(
            rule["id"], channel_id=env["channel_b"], niche="facts",
            statement="Hijacked",
        )
    # The failed writes changed nothing.
    untouched = store.get_rule(
        rule["id"], channel_id=env["channel_a"], niche="facts"
    )
    assert untouched["status"] == "candidate"
    assert untouched["statement"] == rule["statement"]


def test_no_unscoped_read_path():
    """Every public read on the store demands channel_id AND niche."""
    read_methods = (
        "get_rule", "list_active_rules", "list_candidate_rules",
        "list_rule_history",
    )
    for name in read_methods:
        params = inspect.signature(getattr(LearningStore, name)).parameters
        assert "channel_id" in params, name
        assert "niche" in params, name
    # And there is no list-everything / get-by-id-only helper at all.
    public = [n for n in dir(LearningStore) if not n.startswith("_")]
    assert not any("all" in n or "every" in n for n in public)


def test_shared_connection_construction(env, tmp_path):
    """A bare connection may be passed instead of a factory."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    store = LearningStore(conn)
    # No FK target row needed for the in-memory parent we just made is
    # false — insert the channel so the FK holds.
    conn.execute("INSERT INTO channels (id) VALUES ('chan-x')")
    conn.commit()
    rule = store.create_candidate(
        "chan-x", "facts", "publish_window", "Weekday evenings win",
        statement_family="evening-window", evidence_count=3,
    )
    assert rule["is_thin_evidence"] is True
    assert store.list_rule_history("chan-x", "facts")[0]["id"] == rule["id"]
