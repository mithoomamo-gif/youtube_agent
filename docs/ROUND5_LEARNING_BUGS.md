# Round 5 — Learning Loop: Verification Bug List

Verification worker (Learning Loop team), 2026-10-23. Tree verified:
HEAD `aaabab4` + uncommitted Round 5 working tree; learning-scope files
quiet ≥3 min from 07:11 UTC, whole tree quiet from ~07:33 UTC.
Reproductions are in the verification probes
(`/tmp/r5_probes/test_r5_verify_probes.py`, test-only, not committed).

## R5L-001 — HIGH — Alembic chain broken: duplicate revision "0008", two heads

**Closure status: RESOLVED (final verification, 2026-10-23).** Same
fix as R5-001 in `docs/ROUND5_BUGS.md`: calibration log renumbered
to `0009` (down_revision `"0008"`), reports chained as `0010`;
`alembic heads` prints exactly one head (`0010`), the chain is
linear 0001→0010, a fresh-DB upgrade/downgrade/re-upgrade smoke
passed, and the migration slice ran 109 passed including
`test_migration_0009_upgrade_and_downgrade_via_operations`.

`backend/migrations/versions/0008_calibration_log.py` and
`0008_channel_quality_tier.py` BOTH declare `revision = "0008"` with
`down_revision = "0007"`.

Evidence: `alembic heads` prints two heads, both `0008`, with
`UserWarning: Revision 0008 is present more than once`; `alembic
history` walks only one of the two 0008 migrations — the other is
unreachable, so an `alembic upgrade head` cannot apply both.
(Ironically, the channel-quality file's docstring claims it renumbered
to 0008 to avoid exactly this.) Fix: renumber one file (e.g.
calibration log → 0009, down_revision "0008") before any commit.
The learning chain itself (0005_learning_rules → 0006_comments →
0007_analytics) is contiguous and unique.

## R5L-002 — MEDIUM — Direction-flip supersession writes the new rule under the OLD statement_family (real store bridge)

**Closure status: RESOLVED (final verification, 2026-10-23).**
`LearningStore.supersede_rule` now writes the replacement under the
NEW `statement_family` on a direction flip. Evidence: the learning
slice ran **142 passed**, and the two reproduction tests were
re-run explicitly and PASSED —
`tests/test_learning_store.py::test_supersede_into_new_family_direction_flip`
(asserts the flipped row lands under `topic_affinity:ocean:negative`,
version+1, supersedes link, single active row) and
`tests/learning_loop/test_update_engine.py::test_direction_flip_against_real_learning_store`
(the original engine→adapter→real-store reproduction path).

Through the production path (UpdateEngine →
`app.learning.service._EngineStoreAdapter` → real `LearningStore`),
when evidence flips a rule's direction, the superseding row keeps the
old row's `statement_family`. Reproduced: after a D+7 positive rule
(`topic_affinity:ocean:positive`, active), a D+28 flip produced
v2 **active under `topic_affinity:ocean:positive`** whose statement
reads "topic_affinity 'ocean' **underperforms** … by 41.2%". No live
rule exists under the `:negative` family, so family-keyed lookups
(`get_latest`, history filters, consumption snapshots) mislabel the
rule's direction. Root cause: `LearningStore.supersede_rule` inherits
`statement_family` from the old row and the bridge cannot override it.
(Disclosed in `docs/ROUND5_LEARNING_LOOP.md` as a known discrepancy;
verified independently here.) Same-family supersession is unaffected
and old rows are never mutated beyond status — that part passes.

## R5L-003 — MEDIUM — API publish-window endpoint does not enforce the ≥10 videos / ≥28 days eligibility rule

**Closure status: RESOLVED (final verification, 2026-10-23).**
`app.publish_window` now exports `get_publish_window_suggestion`
wired to `PublishWindowService`, and `app.learning.service` resolves
it, so the endpoint enforces the ≥10 videos / ≥28 days rule instead
of the fallback. Evidence: the learning slice ran **142 passed**,
including the reproduction test re-run explicitly and PASSED —
`tests/test_closure_r5_api_fixes.py::test_publish_window_repro_zero_data_active_rule_not_eligible`
(a channel with zero published videos / zero analytics days but one
active publish-window rule is now answered ineligible).

`PublishWindowService` implements the roadmap rule correctly
(probes: 9 videos/60d → ineligible; 10/20d → ineligible; 10/28d →
eligible; 10/27d → ineligible). But the REST endpoint
`GET /api/v1/channels/{id}/learning/publish-window` never reaches it:
`app.learning.service` looks for a sibling function
`get_publish_window_suggestion` in `app.learning.publish_window` /
`app.learning_loop.publish_window` / `app.publish_window`, none of
which export it, so the service's **fallback** answers instead —
eligible iff ≥1 ACTIVE `publish_window` rule exists. Reproduced via
TestClient: a channel with **zero published videos and zero analytics
days** but one active publish-window rule (evidence_count=3) receives
`{"eligible": true, "status": "eligible", "source": "learning_rules"}`.
Fix: export a `get_publish_window_suggestion` adapter from
`app.publish_window` wired to `PublishWindowService`, or have the
service call the service class directly. Minor cosmetic issue in the
same fallback: the suggestion's `window` field is the literal string
`"publish_window"` (the statement-family constant), not a window label.

## R5L-004 — LOW — Active-but-thin inconsistency: engine activates at n≥3, store labels thin below n=5

**Closure status: RESOLVED (final verification, 2026-10-23).** Same
fix as R5-005 in `docs/ROUND5_BUGS.md`: one shared activation
constant (`MIN_EVIDENCE_FOR_ACTIVATION = 3`,
`app/learning/schema.py`; the engine's `MIN_EVIDENCE` re-exports
it), and the store now enforces it — `activate_rule` raises
`InsufficientEvidenceError` below n=3 with no state change, so the
0-evidence probe activation that previously succeeded is refused.
`THIN_EVIDENCE_THRESHOLD = 5` remains as a display-only label,
documented as such in code. Evidence: learning slice **142
passed**, including
`test_activate_refuses_zero_evidence_with_typed_error` and
`test_activate_refuses_below_threshold_and_allows_at_threshold`.

Engine gate `MIN_EVIDENCE = 3` (`app.learning_loop.contracts`) vs store
`THIN_EVIDENCE_THRESHOLD = 5` (`app.learning.schema`). A rule learned
from 3–4 videos is `status='active'` (and is consumed by research
suggestions) while its own row/API payload says
`is_thin_evidence: true`. Consumption provenance entries
(`why.supporting_rules`) do not carry the thin flag, so a suggestion
backed by an n=3 rule presents as fully learned. Disclosed by the
siblings in `docs/ROUND5_LEARNING_LOOP.md`; needs a coordinator
decision on one threshold. Related observation: `LearningStore.
activate_rule` itself performs **no** evidence check (probe: a
0-evidence candidate activates successfully) — the gate exists only
in the engine, so any future direct caller of the store can activate
thin rules silently.

## Environmental — root-suite timeouts (NOT classified as code bugs)

Root suite run under extreme VM contention (load average 20–28 from
~10 concurrent agents): 41 passed, 1 failed, 1 skipped, 2 errors vs
baseline 44 passed / 1 skipped. All three deviations are
`httpx.ReadTimeout` against the live sidecar subprocess in
`tests/integration/test_project_execution_ws.py` and
`tests/integration/test_approvals_persistence.py` (both time out on
`POST /pipeline/start`). A standalone re-run of just those files
under the same contention timed out again at the same call. A
low-load re-run is still required before final classification; no
learning-loop code path is implicated by the tracebacks.
