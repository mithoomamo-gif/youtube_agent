# Round 5 — Analytics Ingestion: Bugs & Acceptance

Verification pass by the Round 5 verification worker (analytics ingestion
scope) against the settled tree (git `master`, HEAD `aaabab4` + uncommitted
Round 5 working tree; the tree is intentionally left uncommitted).
Baselines from the Style Lab mini-round: backend 1698 passed, root
44 passed / 1 skipped, UI typecheck+build green.

Scope verified: Analytics API client (`app.providers.youtube.analytics`),
storage + migration 0007 (`app.analytics`, `0007_analytics`), sync
(`app.analytics_sync` + `app.routers.analytics_sync`), retention→section
mapping (`app.retention_sections`), read API (`app.analytics_read` +
`app.routers.analytics`), fixtures (`backend/tests/fixtures/analytics_api`).

---

## CROSS-TEAM INTEGRATION BREAKAGE (not analytics-team defects)

### X-001 — DUPLICATE Alembic revisions: two files claim revision `0008` (HIGH, blocks `upgrade head`)

**Closure status: RESOLVED (final verification, 2026-10-23).** The
calibration-log migration was renumbered to `0009` (down_revision
`"0008"`) and the reports migration chained as `0010`; the
head-expectation literals were bumped to `"0010"` in both
`test_execution_approvals.py` (~line 229) and
`test_style_lab_schema.py` (~line 209) — verified on disk.
`alembic heads` prints exactly one head (`0010`), the chain is
linear 0001→0010, a fresh-DB upgrade→head / downgrade→base /
re-upgrade→head smoke passed, and the migration slice ran
**109 passed** including the three tests this defect broke. The
analytics slice also ran **127 passed** in the same pass.

Exact duplicate files (both `revision = "0008"`, both `down_revision = "0007"`):

- `backend/migrations/versions/0008_calibration_log.py` (calibration team)
- `backend/migrations/versions/0008_channel_quality_tier.py` (quality-tier team)

Evidence on the settled tree:

- `alembic heads` → two heads, both `0008`, plus
  `UserWarning: Revision 0008 is present more than once`. The same warning
  fires inside `test_analytics_schema.py` runs.
- Any test that calls `upgrade("head")` fails with MultipleHeads (observed
  in the style-lab schema and approvals migration tests, per the storage
  worker's report and the full-suite run).
- Earlier in the round, `0005_reports.py` also duplicated revision `0005`
  (vs `0005_learning_rules.py`); the reports team has since removed /
  renumbered that file — only the `0008` pair remains duplicated.

Required fix (owners: calibration / quality-tier teams): renumber ONE of
the two `0008` migrations to `0009` with `down_revision = "0008"` (chained
after the other). **Follow-up:** the head-expectation literals in
`backend/tests/test_execution_approvals.py` (line ~228, `assert version ==
"0008"`) and `backend/tests/test_style_lab_schema.py` (line ~208, `==
"0008"`) must be bumped to the new head value after renumbering — they
currently pass only because both heads read `0008`.

The analytics migration itself is **not** part of this breakage: it is
revision `0007` (`down_revision = "0006"`) — it was specced as "0005" but
0005/0006 were already claimed by the learning/comments teams when it
landed, and the migration's docstring records that. Its own upgrade /
downgrade round-trip passes (see Acceptance (b)).

---

## ANALYTICS-TEAM FINDINGS

### R5-ANALYTICS-001 — Dual `channel_metric_snapshots` schema — **FIXED on the settled tree; residual test debt remains**

History (verified live during this pass, in sequence):

1. As first landed, `app.analytics_sync.schema` defined
   `channel_metric_snapshots` with `snapshot_at` / `snapshot_day` /
   `subscriber_count` columns while `app.analytics.schema` (and migration
   0007) defined it with `snapshot_kind` / `capture_day` / `captured_at` /
   `subscribers`. Both were `CREATE TABLE IF NOT EXISTS`, so whichever
   layer touched the DB first won and broke the other:
   - sync-first → storage `ensure_analytics_schema` INDEX_STATEMENTS
     raised `OperationalError: no such column: snapshot_kind`;
     `AnalyticsStore` calls raised; `app.analytics_read._known_videos`
     swallowed the error and `GET /analytics/videos` returned
     `data_state='no_data'` after a successful sync (documented by the
     E2E worker's xfail test).
   - storage-first → sync `ensure_analytics_sync_schema` raised
     `OperationalError: no such column: snapshot_at` (its index statement
     ran before its column-convergence block); `sync_channel` raised out
     of `resolve_channel`, breaking its "never raises" contract and
     writing zero rows (reproduced by this worker).
   - store `upsert_channel_metrics` on a sync-shaped table failed with
     `IntegrityError: NOT NULL constraint failed:
     channel_metric_snapshots.snapshot_at` (reproduced by this worker).
2. Settled-tree resolution (sync worker, instructed to align to the
   storage shape): `app.analytics_sync.schema` now defines **only** the
   sync bookkeeping tables (`analytics_sync_runs`, `analytics_sync_state`);
   the sync writes analytics data **exclusively through
   `AnalyticsStore`**; sync's channel/video upserts were rewritten onto
   the canonical shape (dual day-key lookup, `metrics_json` filled,
   `channel_id` in the video-snapshot lookup).

Settled-tree verification by this worker (fresh DBs, both orders):

- (1) Exactly one definition wins regardless of ensure order:
  storage-ensure→sync-ensure and sync-ensure→storage-ensure both succeed
  and yield the identical canonical column set
  (`id, channel_id, snapshot_kind, captured_at, capture_day, subscribers,
  total_views, watch_hours, qualified_watch_hours, metrics_json,
  created_at, updated_at`). **PASS**
- (2) Sync → AnalyticsStore round-trip: full sync with a fake client
  returns `success` (channel 1, video 1, retention 1, no errors);
  `AnalyticsStore.list_snapshots` reads the sync's snapshot back
  (views=100, kind=`d7` for a 10-day-old video, 2 retention points),
  `last_synced_at` is set, `latest_channel_metrics` returns the sync's
  subscribers/total_views; a subsequent store upsert with the same
  (channel, kind, day) key updates in place. **PASS**
- (3) The documenting xfail test
  (`test_analytics_ingestion_e2e.py::test_end_to_end_read_api_videos_baseline_and_snapshot_listing`):
  **XPASSED** in this worker's runs after the fix; the E2E worker has
  since removed the xfail marker and the test passes as a regular test
  in the final standalone re-run (sync + E2E: 25 passed).

Residual test debt from the alignment (test-expectation, not production
defects — see R5A-004): several E2E/sync tests asserted against the old
sync shape when first checked; **their owners fixed them during this
pass** (E2E file updated 08:44 UTC; final standalone re-run by this
worker: sync + E2E **25 passed**, xfail marker removed — see R5A-004).

### R5A-002 — Sync-written metrics omit explicit `ctr: None` (LOW → resolved as contract decision)

Mid-pass, `test_analytics_sync.py::test_sync_then_analytics_store_read_round_trip`
failed with `KeyError: 'ctr'`: snapshots written by the sync carry no
`ctr` key in `metrics_json`, while store-synthesised metrics for legacy
rows carry `ctr: None`. The sync worker settled the contract the other
way — "unreported = absent-or-None" — and the test now asserts
`snap["metrics"].get("ctr") is None`; `app.analytics_read` likewise
normalises absent → `None`. No path surfaces a fabricated CTR either
way. Closed as a documented contract decision, not a defect; noted here
because the two producers still differ (sync omits the key, store
synthesis includes it as `None`).

### R5A-003 — `test_execution_service_hosts_analytics_poller` is racy (LOW, test)

The test's `_rows` helper queries `analytics_sync_state` before the
hosted poller's first sync has created the table and lets
`sqlite3.OperationalError: no such table` escape, aborting the test.
Failed in a group run under heavy parallel load; passes standalone
(6.5 s). Classification: test-expectation/timing flaw (the helper should
treat a missing table as "no rows yet", or the service should ensure the
bookkeeping schema at construction). Not a production defect.

### R5A-004 — E2E + sync tests asserted the pre-alignment schema — **FIXED by owners during this pass**

As first checked against the settled canonical shape:

- `backend/tests/test_analytics_ingestion_e2e.py`: 5 failed, 3 passed,
  1 xpassed (standalone run by this worker).
  - 3 failures were `KeyError: 'subscriber_count'` — raw-row assertions
    against the removed sync-shape column (canonical: `subscribers`):
    `test_end_to_end_sync_stores_snapshots_and_read_api_serves_them`,
    `test_end_to_end_quota_nearly_exhausted_partial_progress_recorded`,
    `test_end_to_end_empty_totals_rows_store_absence_not_zeroes`.
  - 2 failures were `sqlite3.OperationalError: no such table:
    channel_metric_snapshots` — the tests SELECTed from the table after
    zero-write outcomes (not_connected / quota pre-flight), where the
    table is now created lazily by the store:
    `test_end_to_end_not_connected_records_run_and_makes_zero_http`,
    `test_end_to_end_quota_at_ceiling_stops_before_any_call`.
- `backend/tests/test_analytics_sync.py`: the same two
  "no such table" helper assertions failed in
  `test_not_connected_records_reason_and_zero_writes` and
  `test_quota_preflight_stops_before_any_call` (plus the R5A-002 `ctr`
  assertion): 3 failed, 13 passed standalone.

The behaviours those tests exist to prove were independently verified
by this worker's probes throughout (zero HTTP + recorded not_connected
run; quota pre-flight stops before any call; empty totals stored as
absence) — the failures were raw-SQL shape assertions, never behaviour
regressions. The E2E worker updated the file (08:44 UTC) to the
canonical columns, removed the now-obsolete xfail marker, and the sync
worker updated its helpers/assertions. **Final standalone re-run by
this worker on the settled tree: sync + E2E = 25 passed, 0 failed,
0 xpassed** (the former xfail test is now a regular passing test).
Closed.

### Observations (not defects)

- Legacy discrete columns: `AnalyticsStore` writes
  `views/watch_hours_x100/likes/comments_count/shares = 0` into the
  NOT NULL section-15 columns when a metric was not reported; the
  canonical `metrics_json` preserves absence, and the read API serves
  from `metrics_json` first. The learning loop's `SqliteAnalyticsSource`
  reads the discrete columns, so an unreported metric can read as 0
  there. In practice the sync only upserts videos with `has_data=True`,
  so this is a latent, documented-by-design tension, not an observed
  fabrication.
- Channel snapshots: manual and scheduled syncs on the same UTC day
  produce one row per `snapshot_kind` (the canonical identity is
  (channel, kind, capture_day)); `analytics_read.latest_channel_metrics`
  picks the newest by `captured_at`. By design.
- Video-snapshot day definition: storage idempotency uses the **UTC**
  day (`capture_day`); the sync's age/kind logic uses Pacific time.
  Post-alignment the sync writes through the store, so the UTC key is
  the single idempotency definition. Divergence closed.
- Quota double-count check: production wiring
  (`build_default_client`) constructs the analytics client **without**
  a `QuotaLedger`, so the sync's `api_usage` writes are the single
  accounting path — no double counting.

---

## ACCEPTANCE CHECKLIST

### (a) Client — credential-gated, zero HTTP when not connected — **PASS**

- Code: `YouTubeAnalyticsClient._resolve_access_token` (gate) runs
  before any HTTP in `_gate` for every public method; no token manager /
  no channel id → `not_connected`/`no_youtube_credential`; status not
  `connected` or token mint failure → `not_connected`/
  `oauth_token_unavailable`. There is deliberately **no API-key path**
  (Analytics requires OAuth; `yt-analytics.readonly` scope).
- Tests genuinely count HTTP: `RecordingFactory` records factory
  invocations + requests; `test_fixture_no_token_manager_not_connected_zero_http`,
  `test_fixture_channel_not_connected_zero_http`,
  `test_fixture_token_mint_failure_is_not_connected_zero_http` iterate
  **all six** public methods and assert `factory_calls == []` and
  `requests == []`. Standalone: **23 passed**.
- Error mapping is typed, single-attempt, ledgered (incl. failed calls);
  quota pre-flight refusal is also zero-HTTP.

### (b) Storage — migration 0007 round-trip, idempotent re-sync, channel isolation — **PASS** (with X-001 caveat)

- Migration `0007_analytics` evolves the pre-existing (0001)
  `analytics_snapshots` additively (5 columns + backfill + dedupe +
  unique index `(channel_id, youtube_video_id, snapshot_kind,
  capture_day)`) and creates `channel_metric_snapshots`; downgrade drops
  the new table/indexes/columns. `test_analytics_schema.py` +
  `test_analytics_store.py`: **25 passed** standalone, including
  `test_migration_0007_upgrade_from_empty_and_downgrade`,
  DDL-in-sync-with-runtime-schema, FK/CHECK enforcement, day-uniqueness.
  Caveat: a full-chain `alembic upgrade head` is blocked by X-001
  (duplicate `0008`), which is outside this migration.
- Idempotency: `test_upsert_same_video_kind_day_updates_in_place`,
  day-boundary, kind/channel separation, channel-metrics same-day
  upsert — same video+kind+UTC day = one row, updated in place (same
  id/created_at). Sync re-sync idempotency also proven in
  `test_analytics_sync.py::test_resync_same_day_is_idempotent` and the
  E2E two-run probe (2 runs, both success).
- Channel isolation: `test_get_snapshot_cross_channel_is_not_found`,
  `test_channel_metrics_latest_and_isolation`, kind/channel separation —
  cross-channel reads return not-found, never data.

### (c) Sync — failures recorded, shared-bucket quota, freshness — **PASS**

- Every sync settles an `analytics_sync_runs` row with terminal status +
  machine-readable reason (`success`/`partial`/`not_connected`/
  `quota_exhausted`/`failed`/`skipped`); client failures and raising
  clients are converted to recorded outcomes, never raised
  (`test_failed_client_is_recorded_never_raised`,
  `test_raising_client_is_recorded_never_raised`,
  `test_not_connected_records_reason_and_zero_writes` — the last one's
  raw-SQL helper was R5A-004 test debt, since fixed by its owner; the
  recorded-run assertions also pass in the E2E not-connected equivalent
  and this worker's probes).
- Quota: pre-flight + per-call accounting go through the shared
  Pacific-day bucket — the service reads `app.quota_view` over the
  `api_usage` ledger (90% safety ceiling), writes each client result's
  `quota_units` back into `api_usage` (provider `youtube`), and the
  client classifies Analytics queries as (shared, 1) via the same
  `classify_method` as Data API reads. No parallel counter.
  `test_quota_preflight_stops_before_any_call`,
  `test_quota_exhausted_mid_sync_records_partial_progress`, E2E quota
  probes (behaviour verified; the raw-SQL helpers were R5A-004, fixed).
- Freshness: `analytics_sync_state.last_synced_at` moves only on
  success/partial; `AnalyticsStore.last_synced_at` derives max capture
  across both snapshot tables; **every** read-API response carries
  `last_synced_at` + a `sync` block (see (e)).

### (d) Retention mapping — real-pack section map, honest `unmapped` — **PASS**

- `app.retention_sections.section_maps.load_section_map` derives chapter
  boundaries from real production artefacts with documented precedence
  (scene manifest → per-chapter WAV durations → SRT word-count alignment
  → word-proportional), each with a precision label that travels with
  the result; returns `None` when nothing is derivable.
- `map_retention_to_sections`: curve+map → `ok` with per-section
  entry/exit/mean/drop stats and annotations (steepest drop by drop
  **rate**, boundary tie → section starting at the boundary); curve
  without map → `unmapped` (overall stats + drops, **no section
  labels**); missing/empty curve → `no_data` (no stats at all).
- Standalone: **21 passed**, including `test_real_facts_pack_section_map`
  and `test_real_facts_pack_labelled_curve_cliff_in_chapter_3` (the real
  Round 3 facts pack in `outputs/round3/phase0/`).

### (e) Read API — typed empty states, median-of-last-10 baseline, insufficient baseline — **PASS**

- Endpoints: `GET /channels/{id}/analytics/overview | /videos |
  /videos/{video_id}/snapshots | /ypp-progress`. Every response carries
  `data_state ∈ {ok, no_data, not_connected}`, `last_synced_at`
  (None = never synced, never 0), and a `sync` status block. Empty
  states return `metrics: None` / typed per-item `no_data` — grep of
  `app.analytics_read` + routers shows no zero-filled metric payloads;
  a real stored `0` **is** returned as data with `captured_at`
  provenance (`test_videos_real_zero_is_data_not_absence`).
- Baseline: per-video comparison vs the median of the channel's
  **last-10 other videos'** latest snapshots **of the same kind**
  (`BASELINE_POOL_SIZE = 10`, `statistics.median`); pool < 2 →
  `comparison: None` + `comparison_unavailable_reason:
  "insufficient_baseline"` (`BASELINE_MIN_VIDEOS = 2`);
  `test_videos_newest_first_and_median_baseline_math` pins the math,
  `test_videos_insufficient_baseline_single_video_channel` pins the
  insufficient case.
- YPP: qualified watch hours come only from the snapshot's
  `qualified_watch_hours`; never estimated from ordinary hours
  (`test_overview_ypp_never_estimated_from_ordinary_hours`).
- Cross-channel: other channel's video id → 404; lists never leak
  (`test_cross_channel_video_snapshots_404`,
  `test_cross_channel_lists_never_leak`).
- Standalone: **20 passed** (after the read-API worker fixed a seeding
  bug in its own test fixture mid-pass — missing `updated_at` in its
  projects INSERT; production code was not at fault).

### (f) Honesty sweep — no fabricated metrics; fixtures labelled + confined — **PASS**

- Client: no-row → `ok` with `has_data=False`, all metrics `None`
  (never zeros); retention with no rows → empty point list (not a flat
  fabricated curve); CTR always `None` + `CTR_UNAVAILABLE_REASON`
  (legacy `cardClickRate`/`annotationClickRate` are never requested or
  relabelled as thumbnail CTR — asserted on the wire params).
- Sync: videos without data are skipped, not zero-stored; the
  zero-views fixture video (explicit all-zero API row) is stored as a
  real `0` — data, not absence (E2E fixture test + read-API zero test).
- Storage/read: absence is `None`/typed states end-to-end (see (b)/(e));
  the only zero-filling is the legacy NOT NULL discrete-column mirrors
  noted under Observations.
- Fixtures: `backend/tests/fixtures/analytics_api/README.md` states
  every file is a hand-written fixture, not real data, no credential
  involved; the directory is referenced **only** from tests
  (`test_analytics_ingestion_e2e.py`, `test_analytics_read_api.py`).
  Client-test fixtures are inline in the test file with the same
  labelling. No fixture payloads exist in production code paths.

### Credential hygiene — **PASS**

- Pattern sweep (API-key / token / private-key shapes) over 588 tracked +
  new source files (backend/app, backend/tests, src, prompts, docs):
  **zero matches**. The only test token is the literal
  `fake-access-token-1234567890` in the client test.
- Repo-root `.env` (dev OAuth client id/secret) is git-ignored
  (`.gitignore:63`), untracked, mode 600 — values not inspected or
  reproduced here. No `.env`/key/pem files are tracked.
- The analytics client registers minted access tokens with the app's
  redaction layer and never logs or returns them; failure `detail`
  strings pass through `redact_text`.

---

## REGRESSION (full suites, settled tree)

Environment note: this pass ran through extreme host contention
(~6 sibling full suites on a single vCPU) and **one VM restart** that
killed the first full run at ~56%. The completed backend run executed
inside a burst of the known intermittent EPERM write fault, which
polluted the raw counts; every failure cluster was therefore re-run
standalone for classification, per protocol.

### Backend suite (`pytest backend/tests`)

- Completed run: **1703 passed, 85 failed, 384 errors, 8 skipped,
  1 xpassed** (2180 collected; baseline was 1698 passed — Round 5 added
  ~480 tests).
- Failure/error signature of the raw run: 409×
  `sqlite3.OperationalError: disk I/O error`, 38× `PermissionError
  [Errno 1]`, ~20× ffmpeg subprocess `Operation not permitted`
  (`assert 255 == 0`) — the environmental write fault, not code.
- Standalone re-run classification of **every** failure cluster:
  - **Genuine (3 tests, all X-001 MultipleHeads):**
    `test_execution_approvals.py::test_migration_0002_upgrade_and_downgrade`,
    `test_style_lab_schema.py::test_migration_0004_upgrade_from_empty_and_downgrade`,
    `test_style_lab_schema.py::test_ensure_style_lab_schema_idempotent_and_matches_migration`
    — each fails verbatim with `alembic.script.revision.MultipleHeads:
    Multiple heads are present for given argument 'head'; 0008, 0008`.
  - **Environmental (all green standalone):** tts_calibration_planning
    26 + channel_quality_tier 10 (46 passed together);
    execution_approvals remaining 7 + test_schema (20 passed, 1 real
    failure as above); ws 5 + comments_schema + learning_schema
    (68 passed, 2 real failures as above); routers / contracts /
    channels_connect / projects_readthrough / stop_all_live /
    style_lab_claimer / style_lab_media / quota_view / r4003 /
    channel_isolation / execution_lifecycle / execution_runner
    (138 passed); style_lab_store / comments_store / queue error
    clusters (55 passed); retention_sections 3 (in the 90-passed
    analytics batch below). The remaining ERROR-cluster files
    (reporting/learning/execution/comments API + store fixtures) share
    the identical disk-I/O setup signature (337 of 384 error blocks).
  - **Analytics scope in the full run:** all analytics test files
    passed except 3 retention tests (EPERM on WAV/file writes) which
    pass standalone; the R5-ANALYTICS-001 xfail test appeared as the
    run's 1 XPASS (marker since removed by its owner — see R5A-004).
- Analytics cluster standalone on the settled tree (this worker):
  retention + client + schema + store + read API = **90 passed**;
  sync + E2E = **25 passed**.

### Root suite (`pytest tests`)

- Final settled-tree run: **44 passed, 1 skipped** — exact baseline
  match. (An earlier run under peak contention showed 1 failed +
  2 errors, all `httpx.ReadTimeout` against the live-booted sidecar in
  `test_approvals_persistence.py` / `test_project_execution_ws.py`;
  environmental, green on re-run.)

### UI

- `npm run typecheck`: **pass** (exit 0). `npm run build`: **pass**
  (only the pre-existing >500 kB chunk-size advisory). Baseline green
  maintained.

## What remains genuinely open

1. **X-001** — **RESOLVED in the closure pass (final verification,
   2026-10-23)** — see the closure status note under X-001 above:
   calibration log renumbered to `0009`, reports chained as `0010`,
   head literals bumped to `"0010"`, `alembic heads` = one head,
   fresh-DB smoke + migration slice (109 passed) green. No genuine
   code-level failure from this pass remains open.
2. **R5A-003** — racy poller-hosting test helper (LOW, test-only;
   passes standalone and passed in the final sync+E2E re-run).
   Owner: sync worker.
3. R5-ANALYTICS-001: **closed** (alignment verified both ensure orders
   + round-trip; documenting test un-xfailed and passing).
   R5A-002: closed as a contract decision. R5A-004: closed (owners
   fixed assertions during the pass).
