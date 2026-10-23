# Round 5 — Learning Loop: Update Engine

Team: LEARNING LOOP. Scope delivered: the batch learner (Agent 30's
grading step) that turns analytics snapshots into learning rules.
Code: `backend/app/learning_loop/` (`contracts.py`, `engine.py`,
`adapters.py`). Tests: `backend/tests/learning_loop/test_update_engine.py`
(18 tests, all fixtures explicitly SYNTHETIC; one drives the real
landed Learning Store through its service bridge).

## Adapter seam status (read this first)

The engine was built against the documented contracts while the
siblings were in flight (HEAD `aaabab4`); both landed in the
working tree during the round, and the engine needed **no changes**:

- **Learning Store — LANDED + BRIDGED.** Migration
  `0005_learning_rules` + `app.learning.store.LearningStore` are
  real, with scoped signatures differing from the engine's briefed
  protocol (`create_candidate(channel, niche, kind, …)`,
  `activate_rule(id, channel_id=, niche=)`, `supersede_rule(…)`;
  `effect_value`; `created_from` = list of provenance strings).
  The sibling service layer (`app.learning.service`) bridges them
  via `_EngineStoreAdapter` (encoding the engine's dict-shaped
  `created_from` as one JSON string in the provenance list), and
  its REST trigger already runs this engine. An integration test
  (`test_engine_against_real_learning_store`) drives the engine
  against the real SQLite store through that bridge: creation,
  activation, idempotent re-run, D+28 supersession all pass.
  `InMemoryLearningRuleStore` remains as the deterministic
  in-memory reference implementation of the engine-side protocol.
  Both discrepancies flagged during the round are FIXED in the
  Round 5 closure pass: (1) the evidence policy is unified —
  see "Evidence policy (unified)" below; (2) R5L-002: the bridge's
  `supersede` now forwards the engine's `statement_family` and
  `LearningStore.supersede_rule` accepts a family override, so a
  *direction-flip* supersession lands under the NEW family with
  the old-family rule superseded (regression test:
  `test_direction_flip_against_real_learning_store`).
- **Analytics — PARTIAL REAL.** `SqliteAnalyticsSource` reads the
  real `analytics_snapshots` ⨝ `projects` ⨝ `niche_profiles`
  (views, watch hours, CTR, avg view duration, topic, target
  duration, niche key, publish time; D+7 window = 5–<14 days after
  publish, D+28 = 21–<38 days, latest snapshot in window wins) and
  the **issued expectation band from `calibration_log`** (landed,
  migration 0008; metric `views`, per snapshot point; absent band →
  `no_band_recorded`, baseline-only grading). Still unsuppliable:
  `hook_type / title_pattern / thumbnail_style` (no project
  metadata columns record them; always `None` → SQLite source can
  feed only topic/duration/publish-window rules today).

## Engine behaviour

- Batch only, at snapshot points `D+7` and `D+28` (others rejected).
  Per video: actual vs its issued expectation band (below / within /
  above / no_band_recorded) **and** vs the channel baseline.
- Baseline = **median views** of all videos in the same
  `(channel_id, niche, snapshot_point)` scope in the batch. Grouping
  keys always include channel + niche: cross-niche / cross-channel
  pooling is structurally impossible (proven by fixtures where the
  same hook is positive in facts and negative in horror).
- Rule kinds are built from project metadata: topic →
  `topic_affinity`, hook type → `hook_pattern`, duration buckets
  (<8m / 8–12m / 12–20m / >20m) → `duration_band`, title pattern,
  thumbnail style, publish UTC-hour window → `publish_window`.
  A missing metadata field emits no rule for that dimension.

## Math (constants in `engine.py`)

- `raw_effect = (mean(group views) − baseline) / baseline`
  (0 when baseline is 0).
- Shrinkage (empirical Bayes; roadmap formula
  `score_used = (n·score_observed + k·score_baseline)/(n+k)` applied
  to the group mean is exactly):
  **`shrunk_effect = raw_effect × n / (n + k)`**, with
  **k = 4 for `topic_affinity`, k = 5 for all other kinds**
  (the roadmap's own constants for topic clusters vs
  hooks/packaging). Rationale: k = phantom baseline videos the
  prior is worth; n=1 keeps only 1/6 (or 1/5) of a raw lift, so
  small samples cannot produce large claimed effects. The stored
  `effect_estimate` is always the shrunk value.
- `confidence = (n / (n+k)) × agreement`, where agreement is the
  fraction of group videos on the same side of the baseline as the
  group mean.
- Minimum evidence: `evidence_count ≥ 3` **and** (for non-topic
  kinds) ≥ 2 distinct topics before a rule may become `active`;
  below that it stays `candidate` with `thin_evidence=True`.

## Evidence policy (unified — Round 5 closure, R5-005 / R5L-004)

DECIDED POLICY, implemented exactly as stated:

- **Activation threshold is n≥3 EVERYWHERE.** One shared constant:
  `app.learning.schema.MIN_EVIDENCE_FOR_ACTIVATION = 3`, imported
  by the engine contract as `MIN_EVIDENCE`. The engine activates
  only at n≥3, and `LearningStore.activate_rule` itself refuses
  activation below n≥3 with the typed error
  `app.learning.store.InsufficientEvidenceError` and **no state
  change** — the store is the last line of defence, so no direct
  caller can activate a weak rule by bypassing the engine. The
  in-memory reference store enforces the same gate. A supersession
  that would inherit `active` status with replacement evidence
  below n≥3 lands as `candidate` instead, so no store path can
  produce an active rule below the threshold.
- **The `thin` label is a display band only.** The store labels a
  rule `is_thin_evidence` while n<5 (`THIN_EVIDENCE_THRESHOLD = 5`)
  so readers can see the evidence is still thin. It does NOT block
  activation: a rule with n=3–4 is legitimately `active` AND
  labelled thin at the same time — that combination is the policy
  working as decided, not an inconsistency.
- Versioning: never mutate. Identical snapshot-set (sorted video
  ids + snapshot point in `created_from`) re-run = idempotent
  no-op. A new version (via `supersede`, old rule → `superseded`)
  is created only when material: `|Δ shrunk effect| ≥ 0.05`, or
  direction flips (supersedes the opposite-direction rule), or
  activation eligibility changes. Otherwise the existing rule
  stands and the batch report records why.
- Deterministic: groups/scopes/video ids are sorted before
  processing; no randomness, no wall-clock in effect math.

## Honest gaps

- Persistence works through the sibling store + service bridge
  (above), but the engine package itself ships no scheduler/claimer
  (batches run via the sibling REST trigger), and rule consumption
  by Agents 1/4/8 is sibling scope.
- Outcome metric is views only; watch-hours / satisfied-click
  composites, traffic-source splits, confound quarantine and
  time-decay from the roadmap are not implemented in this engine.
- SQLite adapter's expectation-band probe assumes one exact future
  table shape; any other sibling schema needs a small adapter edit.
