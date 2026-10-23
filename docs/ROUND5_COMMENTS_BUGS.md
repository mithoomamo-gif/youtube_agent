# Round 5 — Comments Intelligence (Agent 28) — Verification Bugs

Verification worker report for the comments feature only
(`backend/app/comments/`, `backend/app/comment_intel/`,
`backend/app/providers/youtube/comments.py`,
`backend/app/routers/comments.py`, `src/screens/Comments.tsx`,
`src/api/queries/comments.ts`, migration `0006_comments`).
Baselines: backend 1698 passed / 8 skipped, root 44 passed / 1 skipped,
UI typecheck+build green (HEAD aaabab4).

---

## R5C-001 — MAJOR → RESOLVED during verification — UI sync / rollups hooks called backend routes that did not exist

**Status: FIXED by the API worker while verification was running;
fix verified on the settled tree.** The four channel-level routes
now exist in `backend/app/routers/comments.py` delegating to
`app.comments.api_service` (`list_comment_videos`, `sync_channel`,
`analyse_channel_or_video`, `rollups`), with two new API tests
(`test_comment_videos_index_and_rollups`,
`test_channel_level_sync_all_and_single`). Verification on the
settled tree: full comments subset **123 passed** (was 121), routes
present in the live route table, response envelopes match the UI
normalisers (`{items}` for videos; `{status, channel, videos}` for
rollups — the UI reads `raw.channel` / `raw.videos`), channel-level
sync returns the honest typed `not_connected` result, rollups
return `not_analysed` when nothing is persisted. The original
finding is kept below for the record.

**Where:** `src/api/queries/comments.ts` vs `backend/app/routers/comments.py`

The UI data layer calls four channel-level endpoints that the shipped
backend does not register (verified live against the app's OpenAPI
schema via the test client — all four MISSING):

| UI hook | UI path | Backend actual |
|---|---|---|
| `useCommentVideos` | `GET /api/v1/channels/{id}/comments/videos` | — (only per-video `GET …/videos/{video_id}/comments`) |
| `useSyncComments` | `POST /api/v1/channels/{id}/comments/sync` body `{video_id?}` | `POST …/channels/{id}/videos/{video_id}/comments/sync` |
| `useCommentRollups` | `GET /api/v1/channels/{id}/comments/rollups` | `GET …/channels/{id}/analysis` (+ per-video `…/videos/{video_id}/analysis`) |
| `useRunCommentAnalysis` | `POST /api/v1/channels/{id}/comments/analysis` body `{video_id?}` | `POST …/channels/{id}/analyse` (+ per-video `…/videos/{video_id}/analyse`) |

The other five UI hooks match the backend exactly
(`GET …/comment-drafts`, `POST …/comments/{comment_id}/drafts`,
`POST …/comment-drafts/{id}/approve|reject`,
`GET …/moderation-queue`, `POST …/moderation-queue/{id}/decision`)
and work end-to-end (API tests pass over HTTP).

**Impact (while unfixed):** In the shipped UI, the Comments screen's
per-video sync panel and the rollups panel could only ever show
their error states (the screen does handle `isError` honestly — no
fake data is shown, see acceptance (h)), the Sync button and Run
Analysis button failed with an error, and no rollup could ever be
displayed. The backend capability itself was complete and tested
under its own per-video / canonical contract
(`test_comments_api.py`: sync not_connected 200-honest, listing,
analyse persists rollup + spam flags, channel rollup).

**Reproduction:**
1. `GET /openapi.json` from the running sidecar; search paths for
   `comments/videos`, `comments/sync`, `comments/rollups`,
   `comments/analysis` — none exist.
2. Or open the Comments screen, pick a channel, click Sync — the
   request 404s and the panel shows the error state.

**Fix applied:** the first option — the four channel-level routes
were added to the comments router delegating to
`app.comments.api_service`, exactly as the UI contract expected.

---

## R5C-002 — MAJOR (tree-wide, NOT comments-owned) — Duplicate alembic revisions 0005 and 0008 from sibling Round 5 teams break `upgrade head`

**Closure status: RESOLVED (final verification, 2026-10-23).** The
`0005_reports.py` duplicate was already gone from the settled tree;
the closure pass renumbered the calibration-log migration to
`0009` (down_revision `"0008"`) and chained reports as `0010`, so
the chain is a single linear 0001→0010. Evidence: `alembic heads`
prints exactly one head (`0010`); a fresh-DB upgrade→head /
downgrade→base / re-upgrade→head smoke passed; the migration slice
ran **109 passed** including the three tests listed below; the
comments slice ran **129 passed** in the same pass (comments subset
was 123 earlier in the round; it grew with the closure edit/discard
tests).

Found while verifying (g). In-tree migration files:

- `0005_learning_rules.py` — revision `0005`, down `0004`
- `0005_reports.py` — revision `0005`, down `0004`  ← duplicate
- `0006_comments.py` — revision `0006`, down `0005`  ← comments; unique, correct
- `0007_analytics.py` — revision `0007`, down `0006`
- `0008_calibration_log.py` — revision `0008`, down `0007`
- `0008_channel_quality_tier.py` — revision `0008`, down `0007`  ← duplicate

Alembic emits `UserWarning: Revision 0005 is present more than once`
and the same for `0008` on any script-directory load, and
`alembic heads` reports **two heads, both `0008`**. This is not
cosmetic: `alembic upgrade head` raises
`alembic.script.revision.MultipleHeads`, and three backend tests
fail **deterministically** (reproduced standalone):

- `test_execution_approvals.py::test_migration_0002_upgrade_and_downgrade`
- `test_style_lab_schema.py::test_migration_0004_upgrade_from_empty_and_downgrade`
- `test_style_lab_schema.py::test_ensure_style_lab_schema_idempotent_and_matches_migration`

The comments migration itself upgrades/downgrades cleanly
(targeted `upgrade 0006` → `downgrade 0005` → `upgrade 0006` probe
PASS, all five comments tables created/dropped), and the comments
schema test (targeted upgrade) passes. Comments is not one of the
colliding numbers; the owning teams (learning/reports for 0005,
calibration/channel-quality for 0008) must renumber one file of
each pair and re-chain `down_revision`s before the round commits —
a fresh install that migrates to head is broken until then.

---

## Acceptance summary (comments scope)

| Check | Verdict | Evidence |
|---|---|---|
| (a) Credential gate — no credential ⇒ `not_connected`, ZERO HTTP | PASS | Fetch tests `test_no_credential_not_connected_and_no_http` (+ credential-store-failure and OAuth-unavailable variants): client factory never invoked, `requests == []`. Sync-level twin `test_sync_not_connected_zero_http_and_state`. Verification probe (independent): patched `create_async_client` with a counting factory → `status=not_connected`, `factory_calls=0`, `http_requests=0`. |
| (b) Idempotent sync | PASS | `test_sync_is_idempotent_and_refreshes_counts`: sync same fixture page twice → `inserted 3 → 0`, `updated 3`, one row per comment id (PK), changed fixture refreshes `like_count 42→99` / `reply_count→5` in place, only the new comment inserts. Store-level `test_upsert_comment_idempotent_updates_counts`. |
| (c) Analysis honesty — provenance in both modes, labelled fallback, real top questions | PASS | `comment_intel` tests: deterministic rollup `engine="deterministic"`, `model=None`, `fallback_reason="llm_not_configured"`, `sentiment_method="lexicon_v1"`; LLM-mocked rollup `engine="llm"` with model + `fallback_reason=None`; LLM failure → deterministic WITH `fallback_reason` containing the exception (never silent); strict mode raises; mixed-engine channel rollup is NOT upgraded to llm (labelled with the mix reason). `test_top_questions_are_the_actual_fixture_questions`: top questions are the verbatim fixture questions ranked by likes (q_1 "What is the deepest point of the ocean?", 120 likes, first; q_4 last), with comment ids attached. LLM ghost-id / fake-quote outputs are dropped and counted in `rollup.notes`. |
| (d) NO-POST GUARANTEE | PASS | Grep of the entire comments feature (`app/comments/`, `app/comment_intel/`, `providers/youtube/comments.py`, `routers/comments.py`): the only `comments.insert` / `comments.update` / `comments.delete` / `setModerationStatus` strings are in a docstring stating they are NOT used. Provider's only API method is `METHOD = "commentThreads.list"`, single `client.get` to `/commentThreads` (fetch test `test_read_only_get_comment_threads_only` asserts every request is GET …/commentThreads). Engagement test `test_approval_performs_no_external_call`: provider layer + httpx rigged to raise, generate + approve still succeed, `http_guard == []`, `posted is False`. Own AST audit: `app/comments/engagement.py` and `app/routers/comments.py` import NO HTTP stack at all (no httpx/requests/urllib/socket). Approval is a local store transition only (`set_draft_status` → `UPDATE comment_drafts`), every draft view carries `posted: false` / `posting: "not_wired"`. Note: the pre-existing baseline quota table (`providers/youtube/quota.py`, unmodified) contains a `comments.insert: 50` cost entry — a lookup table, never called by the comments feature. |
| (e) Draft lifecycle — guarded transitions, terminal immutable | PASS | Store: `DRAFT_TRANSITIONS = {proposed: (approved, rejected)}`; `test_draft_terminal_states_immutable` (parametrised over terminal × target), noop/bad-status rejected. Engagement: approve/reject lifecycle tests + `test_terminal_draft_cannot_be_redecided`. API: `test_approve_and_reject_via_api` (second decision → 409), `test_comments_api.py` canonical-path lifecycle (approve → re-approve 409, listing filters). |
| (f) Isolation — channel A invisible under channel B (404) | PASS | Store `test_cross_channel_isolation` + cross-channel upsert raises scope error. API: `test_cross_channel_approve_is_404`, engagement-API list scoping (B sees none of A's drafts), `test_comments_api.py::test_cross_channel_isolation_404s` (draft get/decide, generate, drafts list, moderation queue, analysis all 404/empty under B), unknown channel → 404 `channel_not_found`. |
| (g) Migration — clean up/down, no collision with the analytics team's 0005 | PASS (with R5C-002 caveat) | Comments took **0006** (`0006_comments.py`, revision `0006`, down_revision `0005`) — it does not collide with 0005 (learning + reports) or 0007 (analytics). Worker's `test_migration_0006_upgrade_and_downgrade` + DDL byte-identical-with-runtime-schema test pass. Own probe: upgrade→0006 (5 tables), downgrade→0005 (all dropped), re-upgrade clean. Caveat = R5C-002 (duplicates are sibling teams' 0005_reports and 0008_channel_quality_tier). |
| (h) UI honesty | PASS | No fake/sample comments anywhere in `Comments.tsx` / `queries/comments.ts` (grep: the only "sample/fake" hit is a comment asserting there are none); every row comes from the API with explicit loading / error / empty states per panel. Not-connected state exists: sync-status chip `not_connected → "Not connected"`, `never_synced` state, and a channel-level not-connected empty state. Approved drafts are labelled in the UI: "Approved — not posted. Posting replies isn't available yet" (+ `posted:false`/`posting:"not_wired"` normalised in the data layer). Wiring: initially four hooks targeted non-existent routes (R5C-001); the API worker added them during verification and the settled tree passes the new API tests — see R5C-001. |

## Credential hygiene

Sweep of all new comments files (feature, provider, router, UI,
tests, docs) for key-shaped material (`AIza…`, `sk-…`, `ghp_…`,
private-key headers, literal `api_key = "…"` assignments): **none
found**. Tests use clearly-labelled synthetic credentials
(`FAKE_API_KEY` constants) against `httpx.MockTransport` only.
`api_service` reads only the `YTA_SECRET_BACKEND` selector env var;
credentials resolve through the existing secret-provider seam
(`youtube/api_key` ref) and are never logged or persisted by the
comments code.

## Regression (settled tree; baselines: backend 1698 passed / 8
skipped, root 44 passed / 1 skipped, UI green)

Environment note: this verification ran while ~6 sibling suites
shared the VM (load 20–29) and the VM **restarted once mid-run**
(killing the first full-suite attempt at ~3%). The documented
intermittent write fault (`sqlite3.OperationalError: disk I/O
error`, `PermissionError: [Errno 1]`, ffmpeg "Could not write")
fired en masse during the contended window. Every
write/timing-flavoured failure below was re-run standalone before
classification, per protocol.

- **Backend full run** (2,181 collected): **1850 passed, 58 failed,
  264 errors, 8 skipped, 1 xpassed** in 1591 s.
  Classification of the 322 non-passes:
  - **319 environmental** — all 264 errors are
    `sqlite3.OperationalError: disk I/O error` at fixture DB setup;
    failure signatures are the same I/O error (264×), EPERM (36×),
    and ffmpeg write failures (exit 255 / "Could not write").
    Evidence: standalone batch re-run of all 34 affected files →
    **444 passed**, 4 failed, 8 errors; the 8 errors
    (`test_schedules_api` FK-at-setup) and 1 of the failures
    (`test_tts_calibration_planning::test_plan_explicit_override…`,
    the known R4-007 caplog class) then passed **solo: 9 passed in
    4.6 s**. `test_ws` failures were journal-DB I/O errors; the
    file passed in the batch re-run (suite had also started at VM
    uptime ~120 s, inside the known heartbeat-flake window).
  - **3 REAL failures** — alembic `MultipleHeads` (= R5C-002,
    sibling teams' duplicate migration numbers, NOT comments):
    the 3 tests listed under R5C-002, reproduced standalone;
    `alembic heads` shows two `0008` heads.
  - Fault-free expectation for the settled tree: **2169 passed /
    3 failed (MultipleHeads only) / 8 skipped / 1 xpassed**
    (1850 + 319 environmental outcomes), with the caveat that the
    tree total grew from 1706 (baseline) to 2181 with all Round 5
    teams' new tests.
  - **Comments tests in the full run: 0 failed / 0 errors.**
- **Comments subset on the settled tree** (after the R5C-001 fix):
  **123 passed** (fetch 18, sync 7, schema, store, engagement,
  engagement API 6, API 16, comment_intel) in 34.8 s.
- **Root suite** (45 collected): in-run **41 passed, 1 failed,
  2 errors, 1 skipped**. All three non-passes are sidecar-boot
  integration tests failing with `httpx.ReadTimeout` under load
  (`test_project_execution_ws`, `test_approvals_persistence` ×2);
  standalone re-runs pass (`test_approvals_persistence` solo:
  **3 passed in 15.8 s** at load 1.7; the WS file passed in the
  combined re-run). Settled expectation: **44 passed / 1 skipped —
  baseline-exact.**
- **UI:** `npm run typecheck` PASS (exit 0); `npm run build` PASS
  (exit 0, "✓ built", only chunk-size warnings).

## Open items for the round (comments scope)

- None in the comments feature itself: all acceptance checks
  (a)–(h) PASS on the settled tree.
- R5C-002 (MAJOR, sibling-owned): **RESOLVED in the closure pass
  (final verification, 2026-10-23)** — see the closure status note
  under R5C-002 above; it no longer blocks committing the tree.
- The VM write fault + multi-suite contention made full-suite
  runs unreliable throughout the round; a final clean full run
  should be done once, serially, before the round's commit.
