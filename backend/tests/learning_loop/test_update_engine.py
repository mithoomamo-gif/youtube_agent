"""Update-engine tests — ALL fixtures are SYNTHETIC.

Every ``VideoEvidence`` below is invented test data (labelled
synthetic), never real analytics.  They drive: rule creation,
shrinkage direction/magnitude, the minimum-evidence gate,
supersession on materially changed evidence, idempotent re-runs,
cross-niche isolation, expectation-band grading, and the partial
real SQLite analytics adapter.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import init_db
from app.learning_loop.adapters import (
    InMemoryLearningRuleStore,
    ListAnalyticsSource,
    SqliteAnalyticsSource,
)
from app.learning_loop.contracts import ExpectationBand, VideoEvidence
from app.learning_loop.engine import (
    SHRINKAGE_K,
    UpdateEngine,
    duration_band_for,
    publish_window_for,
)

DAY_MS = 86_400_000
PUBLISHED = 1_700_000_000_000  # synthetic timestamp


def vid(video_id, *, channel="chan-1", niche="facts", point="D+7",
        views=1000, hook=None, topic=None, duration=None, band=None,
        published=PUBLISHED):
    """Build one SYNTHETIC evidence record."""
    return VideoEvidence(
        video_id=video_id, channel_id=channel, niche=niche,
        snapshot_point=point, published_at_ms=published,
        snapshot_at_ms=published + 7 * DAY_MS, views=views,
        expectation_band=band, topic=topic, hook_type=hook,
        duration_sec=duration,
    )


def hook_cohort(prefix, *, q_views=2000, s_views=1000, n_q=3, n_s=3,
                **kw):
    """SYNTHETIC cohort: n_q 'question' hooks + n_s 'statement' hooks,
    every video on a distinct topic (distinct-topic gate satisfied)."""
    out = []
    for i in range(n_q):
        out.append(vid(f"{prefix}-q{i}", views=q_views, hook="question",
                       topic=f"{prefix} topic q{i}", **kw))
    for i in range(n_s):
        out.append(vid(f"{prefix}-s{i}", views=s_views, hook="statement",
                       topic=f"{prefix} topic s{i}", **kw))
    return out


def hook_outcome(report, family_part="hook_pattern:question"):
    matches = [
        o for o in report.rule_outcomes
        if o.statement_family.startswith(family_part)
    ]
    assert len(matches) == 1, report.rule_outcomes
    return matches[0]


# ----------------------------------------------------------------------
# Rule creation + shrinkage
# ----------------------------------------------------------------------

def test_rule_creation_active_with_shrunk_effect():
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    report = engine.run_batch("D+7", hook_cohort("a"))
    outcome = hook_outcome(report)
    # baseline = median(2000x3, 1000x3) = 1500; group mean 2000.
    assert outcome.action == "created_active"
    assert outcome.status == "active"
    assert outcome.version == 1
    assert outcome.evidence_count == 3
    assert outcome.thin_evidence is False
    assert outcome.raw_effect == pytest.approx(1 / 3)
    # shrunk = raw * n/(n+k), k=5 for hooks -> 1/3 * 3/8 = 0.125
    assert outcome.effect_estimate == pytest.approx(0.125)
    assert abs(outcome.effect_estimate) < abs(outcome.raw_effect)
    assert outcome.confidence == pytest.approx(3 / 8)  # agreement = 1
    rule = store.get_latest("chan-1", "facts", "hook_pattern",
                            "hook_pattern:question:positive")
    assert rule is not None
    assert rule["effect_estimate"] == pytest.approx(0.125)
    assert rule["created_from"]["baseline_views_median"] == 1500
    assert rule["created_from"]["shrinkage_k"] == SHRINKAGE_K["hook_pattern"]


def test_negative_effect_creates_negative_family():
    store = InMemoryLearningRuleStore()
    report = UpdateEngine(store).run_batch("D+7", hook_cohort("b"))
    neg = hook_outcome(report, "hook_pattern:statement")
    assert neg.statement_family == "hook_pattern:statement:negative"
    assert neg.effect_estimate == pytest.approx(-0.125)
    assert neg.status == "active"


def test_shrinkage_small_sample_claims_little_and_grows_with_n():
    # n=1 group: same raw lift (+200%) claims only 1/(1+5) of it.
    store1 = InMemoryLearningRuleStore()
    one = [vid("x-q", views=3000, hook="question", topic="t q"),
           vid("x-s1", views=1000, hook="statement", topic="t s1"),
           vid("x-s2", views=1000, hook="statement", topic="t s2")]
    out1 = hook_outcome(UpdateEngine(store1).run_batch("D+7", one))
    assert out1.raw_effect == pytest.approx(2.0)  # median baseline 1000
    assert out1.effect_estimate == pytest.approx(2.0 * 1 / 6)

    # n=6 group with the same raw lift claims 6/(6+5) of it — larger,
    # same sign, still below raw.
    store6 = InMemoryLearningRuleStore()
    many = hook_cohort("y", q_views=3000, s_views=1000, n_q=6, n_s=10)
    out6 = hook_outcome(UpdateEngine(store6).run_batch("D+7", many))
    assert out6.raw_effect == pytest.approx(2.0)  # median still 1000
    assert out6.effect_estimate == pytest.approx(2.0 * 6 / 11)
    assert 0 < out1.effect_estimate < out6.effect_estimate < out6.raw_effect


def test_topic_affinity_uses_k4():
    store = InMemoryLearningRuleStore()
    videos = hook_cohort("c")
    report = UpdateEngine(store).run_batch("D+7", videos)
    topic_outcomes = [
        o for o in report.rule_outcomes if o.kind == "topic_affinity"
    ]
    assert topic_outcomes  # one per distinct topic, each n=1
    for outcome in topic_outcomes:
        assert outcome.effect_estimate == pytest.approx(
            outcome.raw_effect * 1 / (1 + SHRINKAGE_K["topic_affinity"]),
            abs=1e-6,  # effects are stored rounded to 6 dp
        )


# ----------------------------------------------------------------------
# Minimum-evidence gate
# ----------------------------------------------------------------------

def test_two_videos_stay_candidate_thin_evidence():
    store = InMemoryLearningRuleStore()
    report = UpdateEngine(store).run_batch(
        "D+7", hook_cohort("d", n_q=2, n_s=2)
    )
    outcome = hook_outcome(report)
    assert outcome.evidence_count == 2
    assert outcome.action == "created_candidate"
    assert outcome.status == "candidate"
    assert outcome.thin_evidence is True
    rule = store.get_latest("chan-1", "facts", "hook_pattern",
                            outcome.statement_family)
    assert rule["status"] == "candidate"  # never activated
    assert rule["created_from"]["thin_evidence"] is True


def test_three_videos_cross_the_gate():
    # Covered positively in test_rule_creation_active_with_shrunk_effect;
    # here the same attribute goes 2 (candidate) -> later batch of 3
    # eligible evidence supersedes/activates rather than mutating.
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    engine.run_batch("D+7", hook_cohort("e", n_q=2, n_s=2))
    report = engine.run_batch("D+28", hook_cohort("e", point="D+28"))
    outcome = [
        o for o in report.rule_outcomes
        if o.statement_family == "hook_pattern:question:positive"
    ][0]
    assert outcome.status == "active"
    assert outcome.version == 2  # superseded, not mutated


# ----------------------------------------------------------------------
# Band + baseline evaluation (requirement 1)
# ----------------------------------------------------------------------

def test_expectation_band_verdicts_and_baseline():
    band = ExpectationBand(low=800, high=1200)
    videos = [
        vid("band-hi", views=2000, band=band, topic="t1"),
        vid("band-mid", views=1000, band=band, topic="t2"),
        vid("band-lo", views=500, band=band, topic="t3"),
        vid("band-none", views=900, topic="t4"),
    ]
    report = UpdateEngine(InMemoryLearningRuleStore()).run_batch("D+7", videos)
    verdicts = {v.video_id: v.band_verdict for v in report.videos_evaluated}
    assert verdicts == {
        "band-hi": "above_band",
        "band-mid": "within_band",
        "band-lo": "below_band",
        "band-none": "no_band_recorded",
    }
    hi = [v for v in report.videos_evaluated if v.video_id == "band-hi"][0]
    assert hi.baseline == pytest.approx(950)  # median of 2000,1000,500,900
    assert hi.vs_baseline == pytest.approx((2000 - 950) / 950)


def test_invalid_snapshot_point_rejected():
    with pytest.raises(ValueError):
        UpdateEngine(InMemoryLearningRuleStore()).run_batch(
            "D+3", hook_cohort("f")
        )


# ----------------------------------------------------------------------
# Idempotency + supersession
# ----------------------------------------------------------------------

def test_rerun_is_idempotent_no_duplicate_rules():
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    videos = hook_cohort("g")
    first = engine.run_batch("D+7", videos)
    assert first.rules_created > 0
    count_after_first = len(store.all_rules())
    second = engine.run_batch("D+7", list(videos))
    assert second.rules_created == 0
    assert all(o.action == "unchanged_idempotent" for o in second.rule_outcomes)
    assert len(store.all_rules()) == count_after_first


def test_supersession_on_materially_changed_evidence():
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    engine.run_batch("D+7", hook_cohort("h"))  # shrunk +0.125, v1 active
    # D+28: question hooks now far stronger (raw +66.7% vs median 3000).
    report = engine.run_batch(
        "D+28", hook_cohort("h", q_views=5000, s_views=1000, point="D+28")
    )
    outcome = hook_outcome(report)
    assert outcome.action == "superseded_active"
    assert outcome.version == 2
    assert outcome.effect_estimate == pytest.approx(0.25)
    rules = [
        r for r in store.all_rules()
        if r["statement_family"] == "hook_pattern:question:positive"
    ]
    assert len(rules) == 2
    v1, v2 = sorted(rules, key=lambda r: r["version"])
    assert v1["status"] == "superseded"  # old never mutated beyond status
    assert v1["effect_estimate"] == pytest.approx(0.125)
    assert v2["status"] == "active"
    assert v2["supersedes_id"] == v1["id"]


def test_not_material_change_keeps_existing_rule():
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    # Small raw lift (+15%): shrunk 0.05625 at n=3.
    batch1 = hook_cohort("i", q_views=1150, s_views=1000, n_q=3, n_s=9)
    engine.run_batch("D+7", batch1)
    family = "hook_pattern:question:positive"
    before = len([r for r in store.all_rules()
                  if r["statement_family"] == family])
    # One more identical question video: set changes, shrunk moves to
    # 0.15 * 4/9 = 0.0667 — a 0.0104 change, below the 0.05 threshold.
    batch2 = hook_cohort("i", q_views=1150, s_views=1000, n_q=4, n_s=9)
    report = engine.run_batch("D+7", batch2)
    outcome = hook_outcome(report)
    assert outcome.action == "unchanged_not_material"
    assert outcome.version == 1
    # No new version of THIS family (a brand-new topic rule for the
    # added video's own topic is a separate family and is expected).
    assert len([r for r in store.all_rules()
                if r["statement_family"] == family]) == before


def test_direction_flip_supersedes_opposite_rule():
    store = InMemoryLearningRuleStore()
    engine = UpdateEngine(store)
    engine.run_batch("D+7", hook_cohort("j"))  # question positive
    report = engine.run_batch(
        "D+28", hook_cohort("j", q_views=200, s_views=1000, point="D+28")
    )
    outcome = hook_outcome(report, "hook_pattern:question:negative")
    assert outcome.action == "superseded_active"
    assert outcome.version == 2
    active = [
        r for r in store.all_rules()
        if r["kind"] == "hook_pattern"
        and r["statement_family"].startswith("hook_pattern:question")
        and r["status"] == "active"
    ]
    assert len(active) == 1
    assert active[0]["statement_family"].endswith(":negative")


# ----------------------------------------------------------------------
# Cross-niche / cross-channel isolation
# ----------------------------------------------------------------------

def test_cross_niche_contamination_impossible():
    store = InMemoryLearningRuleStore()
    videos = (
        hook_cohort("facts", niche="facts")
        + hook_cohort("horror", niche="horror", q_views=500, s_views=1500)
    )
    report = UpdateEngine(store).run_batch("D+7", videos)
    facts = [
        o for o in report.rule_outcomes
        if o.niche == "facts"
        and o.statement_family == "hook_pattern:question:positive"
    ][0]
    horror = [
        o for o in report.rule_outcomes
        if o.niche == "horror"
        and o.statement_family == "hook_pattern:question:negative"
    ][0]
    # Same hook, opposite conclusions per niche — baselines never pooled
    # (pooled median would be 1250 for both).
    assert facts.effect_estimate == pytest.approx(0.125)   # baseline 1500
    assert horror.effect_estimate == pytest.approx(-0.1875)  # baseline 1000
    for rule in store.all_rules():
        created = rule["created_from"]
        assert created["baseline_views_median"] in (1500.0, 1000.0)
        ids = set(created["snapshot_set"])
        assert ids <= {v.video_id for v in videos if v.niche == rule["niche"]}


def test_cross_channel_isolation():
    store = InMemoryLearningRuleStore()
    videos = hook_cohort("k1", channel="chan-1") + hook_cohort(
        "k2", channel="chan-2", q_views=500, s_views=1500
    )
    report = UpdateEngine(store).run_batch("D+7", videos)
    scopes = {(o.channel_id, o.niche) for o in report.rule_outcomes}
    assert ("chan-1", "facts") in scopes and ("chan-2", "facts") in scopes
    chan2 = [
        o for o in report.rule_outcomes if o.channel_id == "chan-2"
        and o.statement_family == "hook_pattern:question:negative"
    ]
    assert len(chan2) == 1


# ----------------------------------------------------------------------
# Other rule kinds + helpers + source plumbing
# ----------------------------------------------------------------------

def test_duration_and_publish_window_rules_and_helpers():
    assert duration_band_for(300) == "under_8m"
    assert duration_band_for(600) == "8_12m"
    assert duration_band_for(900) == "12_20m"
    assert duration_band_for(1500) == "over_20m"
    assert duration_band_for(None) is None
    assert publish_window_for(PUBLISHED) is not None

    store = InMemoryLearningRuleStore()
    videos = hook_cohort("m")
    for v in videos:
        object.__setattr__(v, "duration_sec", 600.0)
    # VideoEvidence is frozen; rebuild instead of mutating.
    videos = [
        VideoEvidence(**{**v.__dict__, "duration_sec": 600.0}) for v in videos
    ]
    report = UpdateEngine(store).run_batch("D+7", videos)
    kinds = {o.kind for o in report.rule_outcomes}
    assert "duration_band" in kinds
    assert "publish_window" in kinds
    assert "topic_affinity" in kinds and "hook_pattern" in kinds


def test_run_from_source_uses_list_source():
    store = InMemoryLearningRuleStore()
    source = ListAnalyticsSource(hook_cohort("n"))
    engine = UpdateEngine(store, source)
    report = engine.run_from_source("D+7")
    assert len(report.videos_evaluated) == 6
    assert engine.run_from_source("D+28").videos_evaluated == []


# ----------------------------------------------------------------------
# Real Learning Store integration (landed sibling store, bridged by
# app.learning.service._EngineStoreAdapter — same adapter the REST
# trigger uses).  Evidence is still SYNTHETIC; the store is real.
# ----------------------------------------------------------------------

def test_engine_against_real_learning_store(tmp_path):
    pytest.importorskip("app.learning.store")
    from app.learning.service import _EngineStoreAdapter
    from app.learning.store import LearningStore

    conn = init_db(tmp_path / "app.db")
    niche_id, channel_id = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, created_at,"
        " updated_at) VALUES (?, 'facts', 'Facts', 'builtin', ?, ?)",
        (niche_id, PUBLISHED, PUBLISHED),
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, 'UCsynthetic', 'T', 'en', ?, ?)",
        (channel_id, PUBLISHED, PUBLISHED),
    )
    conn.close()

    store = LearningStore(lambda: init_db(tmp_path / "app.db"))
    engine = UpdateEngine(_EngineStoreAdapter(store))
    videos = hook_cohort("real", channel=channel_id)
    report = engine.run_batch("D+7", videos)
    assert hook_outcome(report).action == "created_active"

    active = store.list_active_rules(channel_id, "facts")
    hook_rules = [r for r in active if r["kind"] == "hook_pattern"]
    assert len(hook_rules) == 2  # question (+), statement (-)
    question = [r for r in hook_rules
                if r["statement_family"] == "hook_pattern:question:positive"][0]
    assert question["effect_value"] == pytest.approx(0.125)
    assert question["evidence_count"] == 3
    # Unified policy (R5-005 closure): activation is n>=3 everywhere
    # (engine AND store); the store's n<5 thin label is a DISPLAY
    # band only — an active n=3 rule labelled thin is the policy
    # working as decided, not a discrepancy.
    assert question["is_thin_evidence"] is True

    # Idempotent re-run through the real store + bridge.
    again = engine.run_batch("D+7", hook_cohort("real", channel=channel_id))
    assert again.rules_created == 0

    # Material change at D+28 supersedes in the real store.
    report28 = engine.run_batch(
        "D+28",
        hook_cohort("real", channel=channel_id, q_views=5000,
                    s_views=1000, point="D+28"),
    )
    assert hook_outcome(report28).action == "superseded_active"
    assert hook_outcome(report28).version == 2
    history = store.list_rule_history(
        channel_id, "facts", kind="hook_pattern",
        statement_family="hook_pattern:question:positive",
    )
    assert [r["version"] for r in history] == [1, 2]
    assert history[0]["status"] == "superseded"
    assert history[1]["status"] == "active"


def test_direction_flip_against_real_learning_store(tmp_path):
    """R5L-002 regression: through the PRODUCTION bridge
    (UpdateEngine -> _EngineStoreAdapter -> real LearningStore), a
    direction flip must land under the NEW statement_family — the
    old-family rule superseded, never a live rule whose family says
    'positive' while its statement says 'underperforms'."""
    pytest.importorskip("app.learning.store")
    from app.learning.service import _EngineStoreAdapter
    from app.learning.store import LearningStore

    conn = init_db(tmp_path / "app.db")
    niche_id, channel_id = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, created_at,"
        " updated_at) VALUES (?, 'facts', 'Facts', 'builtin', ?, ?)",
        (niche_id, PUBLISHED, PUBLISHED),
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, 'UCsynthetic', 'T', 'en', ?, ?)",
        (channel_id, PUBLISHED, PUBLISHED),
    )
    conn.close()

    store = LearningStore(lambda: init_db(tmp_path / "app.db"))
    engine = UpdateEngine(_EngineStoreAdapter(store))

    def cohort(prefix, ocean_views, point):
        out = []
        for i in range(3):
            out.append(vid(f"{prefix}-ocean{i}", channel=channel_id,
                           point=point, views=ocean_views, topic="ocean"))
            out.append(vid(f"{prefix}-desert{i}", channel=channel_id,
                           point=point, views=1000, topic="desert"))
        return out

    engine.run_batch("D+7", cohort("d7", 2000, "D+7"))   # ocean positive
    report = engine.run_batch("D+28", cohort("d28", 200, "D+28"))  # flip
    outcome = [
        o for o in report.rule_outcomes
        if o.statement_family == "topic_affinity:ocean:negative"
    ][0]
    assert outcome.action == "superseded_active"
    assert outcome.version == 2

    # The live rule exists under the NEGATIVE family...
    neg = store.list_rule_history(
        channel_id, "facts", kind="topic_affinity",
        statement_family="topic_affinity:ocean:negative",
    )
    assert [(r["version"], r["status"]) for r in neg] == [(2, "active")]
    assert "underperforms" in neg[0]["statement"]
    # ...the positive family holds only the superseded v1...
    pos = store.list_rule_history(
        channel_id, "facts", kind="topic_affinity",
        statement_family="topic_affinity:ocean:positive",
    )
    assert [(r["version"], r["status"]) for r in pos] == [(1, "superseded")]
    assert "outperforms" in pos[0]["statement"]
    # ...and the adapter's family-keyed get_latest agrees.
    adapter = _EngineStoreAdapter(store)
    latest_neg = adapter.get_latest(
        channel_id, "facts", "topic_affinity",
        "topic_affinity:ocean:negative",
    )
    assert latest_neg is not None
    assert latest_neg["id"] == neg[0]["id"]
    assert latest_neg["status"] == "active"
    assert adapter.get_latest(
        channel_id, "facts", "topic_affinity",
        "topic_affinity:ocean:positive",
    ) is None
    # Not co-active: exactly one active ocean rule in the store.
    active_ocean = [
        r for r in store.list_active_rules(channel_id, "facts")
        if "ocean" in r["statement_family"]
    ]
    assert [r["id"] for r in active_ocean] == [neg[0]["id"]]


# ----------------------------------------------------------------------
# SQLite analytics adapter (partial real schema — see adapters docstring)
# ----------------------------------------------------------------------

def test_sqlite_adapter_reads_real_snapshot_tables(tmp_path):
    conn = init_db(tmp_path / "app.db")
    now = PUBLISHED
    niche_id, channel_id, project_id = (uuid.uuid4().hex for _ in range(3))
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, created_at,"
        " updated_at) VALUES (?, 'facts', 'Facts', 'builtin', ?, ?)",
        (niche_id, now, now),
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, 'UCsynthetic', 'T', 'en', ?, ?)",
        (channel_id, now, now),
    )
    conn.execute(
        "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
        " topic_source, target_minutes, published_at, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Synthetic topic', 'user', 10, ?, ?, ?)",
        (project_id, channel_id, niche_id, now, now, now),
    )
    conn.execute(
        "INSERT INTO analytics_snapshots (id, project_id, channel_id,"
        " youtube_video_id, snapshot_at, views, watch_hours_x100,"
        " ctr_bps, avg_view_duration_sec, created_at)"
        " VALUES (?, ?, ?, 'YTsynthetic', ?, 1234, 5000, 450, 300, ?)",
        (uuid.uuid4().hex, project_id, channel_id, now + 7 * DAY_MS, now),
    )
    records = SqliteAnalyticsSource(conn).fetch("D+7")
    assert len(records) == 1
    rec = records[0]
    assert rec.video_id == "YTsynthetic"
    assert rec.niche == "facts"
    assert rec.views == 1234
    assert rec.watch_hours == pytest.approx(50.0)
    assert rec.ctr == pytest.approx(0.045)
    assert rec.topic == "Synthetic topic"
    assert rec.duration_sec == pytest.approx(600.0)
    # No calibration row yet -> no band; hook metadata has no source
    # columns anywhere (documented seam).
    assert rec.expectation_band is None
    assert rec.hook_type is None
    assert SqliteAnalyticsSource(conn).fetch("D+28") == []

    # The sibling calibration team's calibration_log (landed during
    # the round) is the real home of issued bands: once a views band
    # exists for (project, D+7), the adapter surfaces it.
    from app.calibration.schema import ensure_calibration_schema

    ensure_calibration_schema(conn)
    conn.execute(
        "INSERT INTO calibration_log (id, project_id, youtube_video_id,"
        " channel_id, niche, metric, snapshot_point, predicted_low,"
        " predicted_mid, predicted_high, actual_value, created_at)"
        " VALUES (?, ?, 'YTsynthetic', ?, 'facts', 'views', 'D+7',"
        " 800, 1000, 1200, 1234, ?)",
        (uuid.uuid4().hex, project_id, channel_id, now),
    )
    rec2 = SqliteAnalyticsSource(conn).fetch("D+7")[0]
    assert rec2.expectation_band is not None
    assert rec2.expectation_band.low == 800
    assert rec2.expectation_band.high == 1200
    assert rec2.expectation_band.verdict(rec2.views) == "above_band"
    conn.close()
