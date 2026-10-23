# Round 5 — Bugs & Verification Findings

Opened by the Round 5 Verification + Acceptance team. Entries are appended as
workers report; severity scale: CRITICAL / HIGH / MEDIUM / LOW.

---

## R5-001 — Duplicate Alembic revision id "0008": two heads, `alembic upgrade head` fails outright

**Closure status: RESOLVED (final verification, 2026-10-23).** The
calibration-log migration was renumbered to `0009_calibration_log.py`
(`revision = "0009"`, `down_revision = "0008"`), the reports migration
chained after it as `0010_reports.py`, and the head-expectation
literals in `test_execution_approvals.py` / `test_style_lab_schema.py`
were bumped to `"0010"`. Evidence from the final verification runs:
`alembic heads` prints exactly one head (`0010`); `alembic history`
is a single linear chain 0001→0010; a fresh-DB smoke
(`YTA_DATA_DIR` scratch dir) upgraded to head (43 tables,
`alembic_version = 0010`, `calibration_log` present), downgraded
0010→0007→base, and re-upgraded to head cleanly; the migration test
slice (approvals, style-lab schema, calibration log, channel quality
tier, analytics/comments/learning schema, base schema) ran
**109 passed**, including the three tests this defect previously
broke and `test_migration_0009_upgrade_and_downgrade_via_operations`.

**Severity: HIGH** (migration path completely blocked; the running app is
mitigated — see Impact)

**State of the chain (verified on disk, settled working tree):**

```
<base> -> 0001 (down: None)
0001 -> 0002 (Approvals)            down_revision "0001"
0002 -> 0003 (Budget reservations)  down_revision "0002"
0003 -> 0004 (Style Lab)            down_revision "0003"
0004 -> 0005 (Learning rules)       down_revision "0004"   backend/migrations/versions/0005_learning_rules.py
0005 -> 0006 (Comments)             down_revision "0005"   backend/migrations/versions/0006_comments.py
0006 -> 0007 (Analytics)            down_revision "0006"   backend/migrations/versions/0007_analytics.py
0007 -> 0008 (Calibration log)      down_revision "0007"   backend/migrations/versions/0008_calibration_log.py
0007 -> 0008 (Channel quality tier) down_revision "0007"   backend/migrations/versions/0008_channel_quality_tier.py
```

The chain is linear and valid up to `0007`. At `0008` it breaks in the worst of
the possible ways: the two files do **not** merely fork from the same parent —
they declare the **identical revision id**:

- `backend/migrations/versions/0008_calibration_log.py` — `revision = "0008"`, `down_revision = "0007"` (creates `calibration_log`)
- `backend/migrations/versions/0008_channel_quality_tier.py` — `revision = "0008"`, `down_revision = "0007"` (per-channel production quality tier)

**Evidence (Alembic's own tooling, backend venv, run by verification):**

`alembic heads`:

```
UserWarning: Revision 0008 is present more than once
0008 (head)
0008 (head)
```

`alembic upgrade head` against a fresh SQLite database:

```
FAILED: Multiple head revisions are present for given argument 'head';
please specify a specific target revision, '<branchname>@head' to narrow
to a specific head, or 'heads' for all heads
```

`alembic history` shows only one `0007 -> 0008` edge ("Per-channel production
quality tier (Round 5 — master encode)") — because both files share the id
`0008`, the revision map resolves the duplicate to a single file and the
`calibration_log` migration is shadowed: it cannot be reached by a normal
revision walk at all.

**Impact:**

- No Alembic-driven upgrade can reach head: fresh installs, CI database
  setup, and any deployment step that runs `alembic upgrade head` fail.
- Whichever of the two 0008 migrations Alembic resolves, the other one's
  table (`calibration_log` or the channel quality-tier schema) would silently
  never be created by the migration path.
- Related symptom already observed by the Learning verification worker:
  `backend/tests/test_calibration_log.py::test_migration_0008_upgrade_and_downgrade_via_operations`
  fails deterministically (reproduced standalone twice) — the calibration
  team's own test has to work around the broken migration graph.
- **Mitigation that keeps this HIGH rather than CRITICAL for the app
  itself:** the application's primary schema mechanism is runtime lazy DDL
  (`ensure_*_schema` parity functions, heavily test-covered), so the running
  sidecar creates these tables without Alembic. The defect blocks the
  migration path, not current app runtime.

**Fix direction (for the feature/integration teams — verification does not
renumber production files):** keep one file as revision `0008`, renumber the
other to `0009` with `down_revision = "0008"`, and confirm
`test_migration_ddl_in_sync_with_runtime_schema`-style parity tests still
pass; then `alembic heads` must print exactly one head and
`alembic upgrade head` must succeed on a fresh database.

**Note on the earlier mid-landing sighting:** during the landing window a
second duplicate (a `0005_reports.py` beside `0005_learning_rules.py`) was
reported by the Learning worker. In the current settled tree that file no
longer exists — `0005` is claimed only by `0005_learning_rules.py` and the
`0005 -> 0006 -> 0007` segment is a valid linear chain. The `0008`
duplication above is the only remaining break.

**Regression evidence (full-suite worker, settled tree):** this defect is
the sole cause of the only deterministic failures in the final backend run
(2160 passed / 4 failed / 8 skipped / 8 errors of 2181 collected — all other
non-passing results proven environmental on standalone re-runs). The three
baseline tests it breaks, each reproduced standalone:

- `test_execution_approvals.py::test_migration_0002_upgrade_and_downgrade`
  — `MultipleHeads: … 0008, 0008`
- `test_style_lab_schema.py::test_migration_0004_upgrade_from_empty_and_downgrade`
- `test_style_lab_schema.py::test_ensure_style_lab_schema_idempotent_and_matches_migration`

plus, in the calibration suite,
`test_calibration_log.py::test_migration_0008_upgrade_and_downgrade_via_operations`
(deterministic, reproduced standalone twice by the Learning worker).

---

## R5-002 — Two competing `channel_metric_snapshots` schemas: per-video analytics reads broken in production (sync-first) order

**Closure status: RESOLVED (final verification, 2026-10-23).**
`channel_metric_snapshots` now has exactly one DDL definition
(`app/analytics/schema.py`); `app/analytics_sync/schema.py` defines
only the sync bookkeeping tables and the sync writes analytics data
through `AnalyticsStore`. Evidence: the analytics slice
(schema/store/sync/E2E/read API/retention API/canonical-schema/
retention sections/YouTube analytics client) ran **127 passed**;
the former strict-xfail E2E test
(`test_end_to_end_read_api_videos_baseline_and_snapshot_listing`)
passes as a regular test (run explicitly: **1 passed**, no xfail
markers remain in the analytics test files), proving the sync-first
order now reads back per-video.

**Severity: HIGH** (synced analytics data exists but cannot be read back
per-video in the order production actually creates the tables)

**Detail (from the Analytics verification worker; the bug is also documented
in the feature team's own E2E test as `@pytest.mark.xfail(strict=False)`):**

Two Round 5 modules define the same table with different columns, both via
`CREATE TABLE IF NOT EXISTS` — whichever runs first wins:

- Storage shape — `backend/app/analytics/schema.py`:
  `subscribers / captured_at / snapshot_kind / capture_day`
- Sync shape — `backend/app/analytics_sync/schema.py`:
  `subscriber_count / snapshot_at / snapshot_day`

In sync-first (production) order, `AnalyticsStore.ensure_analytics_schema`
then executes index statements referencing columns the sync shape lacks →
`OperationalError: no such column: snapshot_kind` →
`GET …/analytics/videos` returns `data_state='no_data'` and the per-video
snapshots route 404s **even though the sync stored the snapshots**.

**What still works:** channel overview and sync-status freshness
(`analytics_read.py` is column-adaptive for the channel table); not-connected
zero-HTTP behaviour; snapshot idempotency within each layer; retention
mapping. The Analytics acceptance item (a) is therefore **conditional** —
sub-items 1–3 PASS, sub-item 4 (freshness in read models) PASS for
overview/sync-status but the per-video read path is broken until the two
schemas are unified into one definition.

**Fix direction:** single shared DDL for `channel_metric_snapshots` (one
owner module), the other layer adapted to it; then remove the xfail and prove
the E2E in both creation orders.

**Minor related inconsistency (LOW):** idempotency day boundaries differ by
layer — the store keys on UTC `capture_day`, the sync service on Pacific
day. Each layer is internally idempotent, but the definitions should be
reconciled when the schemas are unified.

---

## R5-003 — Receipts presented simulated publishes as real publications (found in verification, fixed by the team mid-round)

**Severity: HIGH at discovery — RESOLVED during the round; the fix is
uncommitted and must be confirmed in the Round 5 commit.**

**Closure status: RESOLVED — confirmed by final verification
(2026-10-23).** `is_simulated_publish` in
`backend/app/reports/facts.py` recognises the `simulated:` prefix
(`video_id.startswith("simulated:")`), and the reporting slice
(reporting engine, W7 reconciliation, reports API) ran **42 passed**;
the W7 tests seed publish records with the real finaliser id shape
(`simulated:fake-video-7:<pid>`) and pin the simulated labelling, so
the fix is test-covered against the exact production shape, not a
hand-shaped fixture.

**Detail (Reporting/UI verification worker):** the W2 receipts module
(`backend/app/reports/facts.py::is_simulated_publish`) decided "simulated"
only by `video_id.startswith("fake")`. The publish finaliser
(`backend/app/execution/publish_finaliser.py:633`) actually writes ids in
the shape `simulated:fake-video-1:<pid>`, which the check missed — so a
video receipt stated `state: 'published_recorded', simulated: False` and the
channel receipt printed a "published record" for a **fake-provider simulated
publish**, contradicting the module's own docstring ("a simulation is NEVER
presented as a live publication").

**Fix (by the feature team, 07:13 UTC during verification):** `facts.py`
now recognises the `simulated:` prefix. The verification worker re-ran the
identical seeded fixture afterwards: video receipt →
`state: 'simulated_fake_provider', simulated: True`, markdown "**Simulated
(fake provider)** — … NOT live on YouTube"; channel receipt likewise
labelled. All cost/quota figures were exact before and after the fix
(independently recomputed by raw SQL: total 125,000 micros; quota
1600 + 1 + 1 = 1602 units).

**Residual risk:** the fix lives only in the uncommitted working tree
(`backend/app/reports/facts.py`). Whoever commits Round 5 must make sure
this file's fix is included; the W7 reconciliation tests
(`test_reporting_reconciliation_w7.py`, passing) pin the correct behaviour
over HTTP against the real finaliser id shape.

---

## R5-004 — Comment drafts cannot be edited; no distinct discard state

**Closure status: RESOLVED (final verification, 2026-10-23).** Draft
edit now exists (store/engagement/API + UI) with provenance recorded,
terminal drafts refuse edit, and `discarded` is a distinct terminal
state: `DRAFT_STATUSES = ("proposed", "approved", "rejected",
"discarded")`, `DRAFT_TRANSITIONS = {"proposed": ("approved",
"rejected", "discarded")}` (`app/comments/store.py`), with the
Comments UI handling the `discarded` state. Evidence: the comments
slice (store, engagement, engagement API, API, fetch, sync, schema,
comment_intel, closure API fixes) ran **129 passed**, including
`test_edit_proposed_draft_records_provenance`,
`test_edit_terminal_draft_refused`,
`test_discard_is_distinct_terminal_state`,
`test_existing_db_with_old_check_is_upgraded`, and
`test_edit_and_discard_draft_via_api` from
`tests/test_closure_r5_api_fixes.py`.

**Severity: LOW–MEDIUM** (completeness gap against the draft-lifecycle spec,
not a safety defect)

**Detail (Comments re-verification worker, settled tree; 123 comment tests
passed / 0 failed):** the draft lifecycle is proven for create → approve /
reject — terminal states are immutable, decisions are stamped, cross-channel
approval 404s, and drafts can never auto-send (`posted: false`,
`posting: "not_wired"` on every view). But against the specified lifecycle
(create → **edit** → approve/reject/**discard**):

- **No edit exists anywhere:** no `update_draft`/edit method in
  `backend/app/comments/store.py` or `engagement.py`, no PATCH/PUT/edit
  route in `backend/app/routers/comments.py`, no edit control in
  `src/screens/Comments.tsx`. A reviewer who wants different wording must
  reject and regenerate.
- **No discard state:** `DRAFT_STATUSES = ("proposed", "approved",
  "rejected")` — `rejected` doubles as discard; there is no distinct
  `discarded` status/transition.

Minor related note: the comment *analysis* `Provenance` model
(`backend/app/comment_intel/schema.py`) has no `prompt_version` field (the
draft provenance does record `prompt_version="comments.reply_draft.v1"`).
Worth a constant in a future tidy-up; the provenance requirement itself is
met via engine + model + named-method labels.

---

## R5-005 — Learning loop: two different evidence thresholds; store-level activation is ungated

**Closure status: RESOLVED (final verification, 2026-10-23).** There
is now ONE activation threshold: `MIN_EVIDENCE_FOR_ACTIVATION = 3`
in `app/learning/schema.py`, imported by the engine contract
(`app/learning_loop/contracts.MIN_EVIDENCE` re-exports it), and
`LearningStore.activate_rule` enforces it as the last line of
defence — below n=3 it raises `InsufficientEvidenceError` with no
state change (the engine adapter enforces the same gate).
`THIN_EVIDENCE_THRESHOLD = 5` remains, explicitly documented in
code as a DISPLAY band only (an n=3–4 active rule is labelled thin;
the label never gates). Evidence: the learning slice (store, schema,
API, consumption, lessons, publish window, learning_loop engine,
calibration math/log, closure API fixes) ran **142 passed**,
including `test_activate_refuses_zero_evidence_with_typed_error` and
`test_activate_refuses_below_threshold_and_allows_at_threshold`.
See also R5L-004 in `docs/ROUND5_LEARNING_BUGS.md` (same item).

**Severity: MEDIUM** (the safety property — weak rules must not influence
generation — currently holds via the engine and the read path, but the
store's public API does not enforce it)

**Detail (Learning verification worker; learning battery 119 passed /
1 failed, the failure being R5-001-linked):**

- The engine activates rules at `MIN_EVIDENCE = 3`
  (`backend/app/learning_loop/contracts.py`), but the store's thin-evidence
  *label* uses `THIN_EVIDENCE_THRESHOLD = 5` (`app/learning/schema.py`). A
  rule with n=3–4 is simultaneously `active` and labelled
  `is_thin_evidence=1`; consumption surfaces the label but does not block
  such rules.
- `LearningStore.activate_rule` performs **no evidence check at all** — the
  gate lives only in the engine's decision logic. A direct store caller
  could activate an n=0 candidate. No test asserts a store-level refusal.
- Why this is not worse today: the only generation consumer
  (`app/learning/consumption.py::fetch_active_rules`) reads `status='active'`
  only, and the engine is the only writer that activates. The invariant is
  one future caller away from being broken.

**Fix direction:** one threshold constant shared by engine and store, and
an evidence check inside `activate_rule` (refuse below the minimum, with a
test asserting the refusal).

---

## Closure verification addendum (final verification worker, 2026-10-23)

Tree state verified: HEAD `aaabab4` + uncommitted Round 5 closure
working tree, quiet ≥5 min (no source writes after ~09:35 UTC, no
sibling pytest/ffmpeg processes) before any run started; all runs
serial, `TMPDIR=.tmp-yta` with per-run `--basetemp`. A VM restart
occurred at ~09:36 UTC (uptime reset); the full-suite run started at
uptime ~14 min and crossed the ~1030 s `test_ws` heartbeat-flake
window mid-run — `test_ws` passed in the full run.

Per-slice results (each serial):

- (i) Migration/head: **109 passed** (approvals, style-lab schema,
  calibration log, channel quality tier, analytics/comments/learning
  schema, base schema). `alembic heads` = exactly one (`0010`).
  Fresh-DB smoke: upgrade→head (43 tables, version 0010),
  downgrade→0007→base, re-upgrade→head — all clean.
- (ii) Analytics + analytics_sync: **127 passed**; former strict
  xfail E2E test passes as a regular test (explicit re-run 1 passed;
  no xfail markers remain in the analytics test files).
- (iii) Learning: **142 passed**; named reproductions re-run
  explicitly, all PASSED: R5L-002
  `test_supersede_into_new_family_direction_flip` +
  `test_direction_flip_against_real_learning_store`; R5L-003
  `test_publish_window_repro_zero_data_active_rule_not_eligible`.
- (iv) Comments: **129 passed** (incl. draft edit + discarded state
  closure tests).
- (v) Reporting: **42 passed** (incl. W7 simulated-labelling tests
  against the real finaliser id shape).

Full backend suite (ONE serial run, `pytest backend/tests`):
**2187 passed, 1 failed, 8 skipped, 8 errors; 2204 collected**
in 1104.8 s (vs Style Lab baseline 1698 passed / ~1706 collected
and the ~2181 Round 5 expectation — the tree grew by the closure
test files). Classification of every non-pass, each re-run
standalone before classifying:

- 1 FAILED — `test_tts_calibration_planning.py::
  test_plan_explicit_override_plans_at_its_own_bucket`: caplog
  assertion (no "override" log record captured). ENVIRONMENTAL —
  the known R4-007 caplog-ordering flake class; standalone re-run
  PASSED.
- 8 ERRORS — all `test_schedules_api.py`, `sqlite3.IntegrityError:
  FOREIGN KEY constraint failed` at the `_channel` fixture's seed
  INSERT. ENVIRONMENTAL — full-suite ordering/shared-DB-state
  signature, identical to the instance documented by the Comments
  verification worker earlier in the round; standalone re-run of
  the whole file PASSED (combined re-run: **9 passed in 9.7 s**).
- Fault-free expectation for the settled closure tree:
  **2196 passed / 8 skipped / 0 failed / 0 errors (2204 collected)**;
  0 xfailed / 0 xpassed in the full run.

Credential hygiene spot-check: CLEAN. Pattern sweep (Google API-key
`AIza…`, `sk-…`, `ghp_…`, `GOCSPX-…`, private-key headers, Slack/AWS
shapes) over all 108 new/changed files: zero matches. The only
credential-shaped literals in the tree are short synthetic
word-strings in one analytics E2E test helper seeded through
`InMemorySecretStore` — none matches a real Google credential format
(no `.apps.googleusercontent.com` suffix, no `1//` refresh-token
prefix, no `GOCSPX-` prefix). `.env` remains git-ignored, untracked,
mode 600; no `.env`/`.pem`/`.key` files are tracked. No values were
printed or recorded here.

Still open after closure: NONE of R5-001…R5-005 / R5L-002…R5L-004.
Pre-existing non-closure items are unchanged (e.g. R5A-003 racy
poller-hosting test helper, LOW/test-only; R4-007 flake monitor).
The tree is left uncommitted, as instructed.
