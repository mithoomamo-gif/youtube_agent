# Style Lab Mini-Round — Bug & Gap Log

Opened by the Style Lab verification team (2026-10-23) against the settled
working tree on HEAD `637cf9a` (all Style Lab work uncommitted). Severity:
HIGH = breaks a stated feature guarantee · MEDIUM = feature works but a
material path/quality gap · LOW = honesty/polish/pre-ship cleanup ·
OBSERVATION = not a defect, recorded so it is not misread later.

---

## SL-001 — HIGH — REST API style-profile creation bypasses the strict schema and the no-copy gate — **RESOLVED (closure round, 2026-10-23)**

**Where:** `backend/app/routers/style_lab.py::create_style_profile` →
`backend/app/style_lab/service.py::create_profile`.
**What:** The payload is arbitrary JSON persisted to
`style_profiles.profile_json` **without** validation against the strict
`style_profile.v1` schema (`backend/app/integration/style_profile.py::StyleProfile`,
`extra="forbid"`) and **without** running `assert_no_copy` (the verbatim +
≥4-word-shingle gate, `style_profile.py` L789). Competitor scoping *is*
enforced on this path (foreign/unknown competitor ids → 422, verified
live); the no-copy and schema guarantees are not.
**Impact:** The feature's hard boundary — "style yes, copies no" — is
unenforced on the API path. A crafted profile could carry verbatim
competitor title/description text into generation.
**Reproduction:** Read the two code paths side by side; the synthesis path
(`synthesize_style_profile`) validates + gates, the API path does neither.
Verified by verification worker W3b (code read + live API run).
**Fix direction (not applied — verification team does not edit production
code):** route API profile creation through `StyleProfile` validation and
`assert_no_copy`, or persist only profiles produced by
`synthesize_style_profile`.
**Resolution (closure round, applied + verified):** In
`backend/app/style_lab/service.py`, `create_profile` now gates **before**
any persistence, importing the synthesis layer's exact functions from
`backend/app/integration/style_profile.py` (no second validator; no
synthesis internals edited): (1) competitor scope pre-check — unknown id →
422 `competitor_not_found`, foreign id → 422 `competitor_not_in_channel`
(the store still re-checks atomically); (2) schema — `StyleProfile
.model_validate` (`extra="forbid"`) → 422 `invalid_style_profile`, and a
profile whose inner `channel_id` names another channel → 422 with
`reason="channel_mismatch"`; (3) no-copy — `assert_no_copy` run against
`CompetitorVideoAnalysis` objects built from the listed competitors'
stored titles/descriptions (plus fetched title/description persisted in
competitor `metadata_json`) → 422 `no_copy_violation`. A rejected create
persists **nothing** (the old row-first/write-later order is gone), and a
persisted `profile_json` is the validated model's canonical dump, never
raw caller JSON. Payload-less creation (the UI path) is unchanged.
`activate_profile` persists no content, but now re-runs the same schema +
no-copy gate over a stored content-bearing row before activating, so a
pre-fix legacy row cannot be laundered into `active`. Honest scope note:
the intake DB stores no transcript text (only `transcript_ref` sidecars),
so on this path the gate's source set is titles/descriptions — exactly
`assert_no_copy`'s defined input.
**Evidence:** `backend/tests/test_stylelab_profile_boundary.py` — 13
passed (verbatim title, verbatim description, reworded 5-word-shingle
chunk, 5× parametrized schema-invalid, clean proof-shaped payload
accepted + exact round-trip, cross-channel ids rejected, inner-channel
mismatch rejected, legacy poisoned row activation refused).
`backend/tests/test_stylelab_closure_adversarial.py` — the 6 SL-001
probes passed (shingle-only copy from a stored description, verbatim
title in a free-text field, nested wrong type, unknown extra key, clean
accept with no over-blocking, activation leaves `profile_json`
byte-identical). The one casualty was a **stale test expectation**:
`test_style_lab_e2e.py` step 6 posted the free-form payload that *was*
the SL-001 hole and asserted verbatim round-trip; the closure
coordinator reconciled it to post a valid `style_profile.v1`
proof-shaped payload (same precedent as SL-008) — green in all settled
runs below. Settled closure runs: Style Lab + routers + execution
slices **599 passed**; full backend suite **1698 passed, 1 failed,
8 skipped** (1707 collected) vs the 1668-passed baseline — the single
failure is the known `test_tts_calibration_planning` caplog environment
flake (standalone 1 passed, whole file 32 passed; see Environmental).

## SL-002 — MEDIUM — Queued analyses are never auto-claimed — **RESOLVED (closure round, 2026-10-23)**

**Where:** `POST …/competitor-videos/{video_id}/analysis`
(`backend/app/routers/style_lab.py`) + `backend/app/style_lab/service.py`.
**What:** Queueing flips `analysis_status` to `queued` and stamps
`analysis_requested_at`; nothing in the tree ever claims queued rows and
runs the engine. Self-disclosed in `docs/STYLE_LAB.md` §7.
**Reproduction:** W3b live run — attach a real video, queue analysis, row
stays `queued` indefinitely; the engine runs only when called directly.
**Impact:** From the UI, "queue analysis" is a dead end until a human or
another process invokes the engine. The intake→analysis→profile chain is
not yet hands-free.
**Resolution (closure round, applied + verified):** New
`backend/app/stylelab/claimer.py` (`CompetitorAnalysisClaimer`,
`poll_once` / `claim_next`), hosted by the sidecar execution service —
`ExecutionService` (`backend/app/execution/service.py`) builds it in
`__init__`, runs it as the `yta-stylelab-claimer` task alongside the
runner/scheduler tasks (defaults: 5 s initial delay, 15 s interval;
engine work via `asyncio.to_thread`), and cancels/awaits it in `stop()`
exactly like the scheduler task. No parallel runtime. Per §7: the
claimer scans `analysis_status='queued'` across channels (oldest
request first), claims each row, runs
`app.stylelab.engine.analyse_video(...)` with results persisted through
`app.stylelab.storage` (§6 layout), then flips to `analysed` via
`StyleLabStore.set_analysis_status(..., channel_id=<owner>)`. Two
documented deviations from §7's letter, both forced by the code as
built: (i) the claim itself uses a status-guarded conditional UPDATE
(`... WHERE id=? AND analysis_status='queued'`, proceeds only on
rowcount 1) because `set_analysis_status` has no expected-status guard
— double-claim is impossible; the store method is used for the
`analysed` / `analysis_failed` flips. (ii) `competitor_videos` has no
reason column and §7 specifies none, so a failure reason is recorded
durably at `<data_dir>/stylelab/<video-id>/analysis_failure.json`
(`reason` from the media vocabulary, `error_class`, `detail`,
`failed_at`, owner `channel_id`) plus an ERROR log; a later successful
re-analysis deletes the stale record. Failure semantics: exactly **one**
attempt per queueing — any exception flips the row to `analysis_failed`
and the claimer moves on (never a crash loop, never a silent stall);
metadata-only rows (no attached file) fail honestly with reason
`no_attached_file`; re-queue stays a user action via the existing
endpoint. STOP ALL / shutdown: flags are re-read via
`app.execution.flags` before **every** claim — `pause_all` or `stop_all`
block claiming, and `stop_all` latches the loop exit, mirroring
`JobRunner`. Scope: each run writes only the claimed video's DB row and
its own per-video analysis directory/index entry. §7 step 3's optional
auto-creation of a distilled profile on success is **not** automated
(§9 deviation 4 seam, unchanged); `docs/STYLE_LAB.md` §7's as-built
paragraph was updated in the same round.
**Evidence:** `backend/tests/test_style_lab_claimer.py` — 6 passed
(API end-to-end queue → claim → `analysed` with §6 files/index present;
corrupt file → `analysis_failed` with recorded reason, then a good row
succeeds; two-channel scope isolation; STOP ALL + pause block claiming;
ExecutionService hosting + clean shutdown; conditional claim /
no-double-claim). `backend/tests/test_stylelab_closure_adversarial.py`
— the 3 SL-002 probes passed (missing file → `analysis_failed` with a
recorded reason and a second poller pass does **not** retry it — status
and `updated_at` untouched; STOP ALL leaves a queued row `queued`).
Execution suites 106 passed; settled slices and full-suite counts as
recorded under SL-001 (599 passed; 1698 passed / 1 known flake /
8 skipped vs the 1668 baseline).

## SL-003 — MEDIUM — Scene segmentation under-segments gradual-transition content

**Where:** `backend/app/stylelab/scenes.py` (frame-difference detector,
threshold 0.3).
**Evidence:** `outputs/stylelab/analysis_check/CHECK.md` —
Case A (Round 3 facts video, 25 production scenes): detector found 21;
gradual transitions merge. Case B (Round 4 TTS proof, 31 local plates):
detector found **1 scene** spanning the whole video and self-flagged
`low_segmentation`; per-plate classification accuracy is therefore
**unmeasured, not proven**.
**Impact:** Style Lab results on plate/generated-style videos are
whole-video-level only until segmentation gets an adaptive threshold or
segmentation hints.

## SL-004 — MEDIUM — Pixel visual classifier yields no usable mix without a vision key; honesty check is not a repeatable test

**Where:** `backend/app/stylelab/visual_classify.py` (heuristics capped at
0.60, heuristics-only discounted to ≤0.55, commitment threshold
`UNCERTAIN_THRESHOLD = 0.55`).
**Evidence:** Heuristics-only live run (W3b, Round 2 facts video): visual
mix **100% `uncertain`** (candidate `stock_like` in 3/3 probed scenes at
0.30–0.34 — information preserved, nothing committed). With Gemini vision
enabled (CHECK.md Case A): **17/21 scenes `stock_like` correct, 1/21 false
`ai_generated_like`** (scene 5, conf 0.57, composite-style stock shot;
heuristics disagreed), 3/21 abstained with correct candidates;
committed-label accuracy 17/18 = 94.4%; duration-weighted stock share
71.95%. CHECK.md refuses the inflated 95.2% candidate-inclusive figure.
**Impact:** (i) Without a Gemini key the per-scene visual-source breakdown
is abstain-only — by design, but users must know the mix needs the vision
path. (ii) The ground-truth honesty check exists only as a persisted
artifact (`outputs/stylelab/analysis_check/`), not as a test that reruns —
classifier regressions would not be caught automatically.

## SL-005 — LOW — Speech-stage quirks and zero-confidence schema fields

**Evidence:** W3b heuristics run on the Round 2 Urdu video: Whisper
language detected as `pa` (Punjabi) for Urdu narration; a single over-long
beat labelled "call to action" across a whole 41 s video; hook `uncertain`
@ 0.4. (The CHECK.md runs on Round 3/4 videos detected `ur` correctly —
the misdetection is input-dependent, not systematic.) CHECK.md gap 5:
thumbnail confidence and speech-language confidence are emitted as `0.0`
("no evidence") in the schema — honest, but downstream consumers cannot
weight those stages.

## SL-006 — LOW — Proof-report honesty gaps (STYLE_MATCH.md)

**Where:** `outputs/stylelab/proof/` (`proof_result.json`, `STYLE_MATCH.md`),
generation via `tools/`/proof driver + `backend/app/integration/produce.py`.
(i) The proof's script came from the **deterministic stub**
(`paths_used.llm = "deterministic-stub"`, 0 tokens, $0.00) — style
steering of script *structure* is real, but `STYLE_MATCH.md` prints
"LLM cost $0.0000" without naming the stub. A real-LLM styled proof has
not been run.
(ii) Measured stock share is reported as **1.0** although the pack ledger
contains 1 local-plate fallback scene (`scene_0008`) — a ledger-overlap
measurement artefact that hides the plate (the same plate that tripped the
QC freeze flags, see SL-007).
(iii) The generated title repeats a bracket suffix
("… [Poori Kahani] [Poori Kahani] [Asal Sach]") and the title bracket
check passes literally. Title-length also honestly FAILs its band
(71 vs [60, 60]) — that part *is* disclosed in STYLE_MATCH.md.

## SL-007 — OBSERVATION — The style proof video is not publishable, and the gates are right to block it

`outputs/stylelab/proof/projects/chand-ki-doosri-janib-ke-raaz-ee0c8fe1/pack/`:
Gate A PASS, **Gate B FAIL / QC FAIL** (`BLACK_SEGMENT_TOO_LONG`,
`FREEZE_DECLARED_STILL`, `FREEZE_TOO_LONG`) → `manifest_frozen_not_uploaded`
(fake provider). These flags are outside Round 4's minor class
(`…_TOO_LONG` ≠ `…_BRIEF`), so the block is policy-consistent, and
STYLE_MATCH.md discloses it in full, including duration deviation
(131.2 s vs the 185.73 s band) and WPM deviation. Recorded so the proof is
never later mistaken for a publishable sample: generation works; this
particular generated video is correctly frozen.

## SL-008 — LOW (test expectation) — FIXED during verification by the feature team

`backend/tests/test_execution_approvals.py::test_migration_0002_upgrade_and_downgrade`
hardcoded alembic head `0003`; Style Lab migration `0004` legitimately
moves the head. Failed in the verification full-suite run
(`assert '0004' == '0003'`); the migration chain itself upgrades cleanly
0001→0004 and the new `test_style_lab_schema.py` 0004 tests pass. The file
now asserts `0004` with an explanatory comment (edited by a sibling team
mid-verification; git status count 35→36) and the standalone re-run
passes. No action remains; expect the same edit when migration 0005 lands.

## SL-009 — LOW (pre-ship cleanup) — RESOLVED (Round 6) — Two packages, one class name: `app.stylelab` vs `app.style_lab`, both defining `StyleLabStore`

**Verified no collision** (verification team, settled tree): distinct
module paths — `app.stylelab.storage.StyleLabStore(base_dir: Path | str)`
(file-based analysis store) vs `app.style_lab.store.StyleLabStore(conn_factory)`
(DB intake store, migration 0004). Both import side-by-side in one process;
all imports in the tree are fully qualified; no file binds the name from
both packages; the engine's `storage.py` docstring explicitly documents
adopting the DB store's contract. Full-suite collection is clean
(1,678–1,679 tests).
**Risk:** names differ by one underscore, constructors are incompatible,
and only the engine package exports the name at package level
(`app.stylelab.StyleLabStore`); a future wrong import fails loudly at
construction, not silently — but it is a maintainer trap.
**Recommendation (recorded, NOT executed by the verification team):**
keep `app.style_lab` (it owns the DB schema/migration, router and service —
the public surface) and rename the engine package `app.stylelab` →
`app.style_analysis`, its class → `StyleAnalysisStore`. Acceptable
alternative: merge both under `app.style_lab` as `intake`/`engine`
submodules. Rename before shipping.
**Resolution (Round 6, executed):** the recommended rename was performed
exactly as recorded above. `backend/app/stylelab/` → `backend/app/style_analysis/`
via `git mv` (history preserved; not committed by the rename worker);
the engine's file store `StyleLabStore` → `StyleAnalysisStore`
(`app.style_analysis.storage`, re-exported from `app.style_analysis`).
The intake package `app.style_lab` and its DB `StyleLabStore` are
untouched. All importers were updated (67 dotted `app.stylelab` →
`app.style_analysis` replacements across 23 Python files in
`backend/app` + `backend/tests`, including the dynamic import in
`engine._import_sibling`, the defensive candidate list in
`app.style_lab.service`, the `ExecutionService` claimer import, and
the claimer-discovery candidates in
`tests/test_stylelab_closure_adversarial.py`); clean break, no
compatibility shim — no persisted/dynamic reference required one
(stored analyses are JSON with relative paths only; nothing serialises
the module or class name). Deliberately NOT renamed (persisted /
on-disk strings): the `<data_dir>/stylelab/` results directory and
`default_stylelab_dir()` helper name, `analyzer_version = "stylelab-0.1"`,
the speech `kind = "stylelab_transcript"`, and `outputs/stylelab/…`
proof paths.
**Evidence:** grep for `app\.stylelab` over `backend/app` +
`backend/tests` (`*.py`) returns **0** matches after the rename;
`import app.stylelab` raises `ModuleNotFoundError`, while
`app.style_analysis` + `StyleAnalysisStore` and the intake
`app.style_lab.store.StyleLabStore` import side-by-side as distinct
classes. Affected test surface run serially by the rename worker
(`tests/stylelab/`, all `test_style_lab_*.py` / `test_stylelab_*.py`,
`test_analytics_sync.py`, `test_data_dir.py`): **394 passed, 1 failed**
— the one failure is
`test_style_lab_schema.py::test_migration_0004_upgrade_from_empty_and_downgrade`
asserting alembic head `0010` while a concurrent Round 6 worker's new
migration `0011_compliance.py` had already moved the head to `0011`
(the chain, including 0004, upgrades cleanly; same pattern as the
0003→0004 note above — not a rename defect). One full serial backend
suite run on the (concurrently moving) Round 6 tree: **2302 passed,
50 failed, 8 skipped, 89 errors** in 91 min under load 12–29 with
10–12 sibling suites running. Every style-lab failure/error in that
run carries the known EPERM write-fault signature (55× sqlite
`disk I/O error`, 35× `PermissionError: [Errno 1]`, ffmpeg
`Operation not permitted` on file writes) and is contradicted by the
clean affected-surface run of the same files; the only
non-environmental failures are the two `0011`-vs-`0010` head
assertions. Baseline at round start (settled Round 5 tree) was
2187 passed; the higher total reflects Round 6 tests added by
sibling workers during the run. Standalone re-runs (after the
EPERM fault — which had temporarily hardened to blocking all
Python file writes — cleared): the 9 style files that
failed/errored in the full run re-ran **125 passed, 1 failed**
(only the `0011`-head test); the remaining full-run failure
clusters (`test_tts_calibration_planning`, `test_uninstall_policy`,
`test_onboarding_system_check`, `test_schedules_api`,
`test_ws`, `test_execution_approvals`) re-ran **118 passed,
2 failed** — the same `0011`-head assertion in
`test_execution_approvals` and the known uptime-dependent
`test_ws` heartbeat flake. Classification confirmed by re-run:
no full-run failure is attributable to the rename.

---

## Environmental — not defects (classified during verification)

- **Root suite timeouts:** `test_project_execution_ws.py` (1 test) and
  `test_approvals_persistence.py` (2 tests) failed with httpx `ReadTimeout`
  / boot timeouts while VM load average was 17.9–35.1 (2 vCPU, sibling
  suites + ffmpeg renders). Settled re-run by the verification coordinator:
  **5 passed in 23.32 s**. Baseline (44 passed / 1 skipped) stands.
- **`test_tts_calibration_planning` caplog ordering flake** (known since
  Round 4): failed in-suite on the final caplog assertion only; functional
  assertions all passed; standalone solo 1 passed, whole file 32 passed.
  Note: "green standalone" means single-test or whole-file — an arbitrary
  2-test pairing reproduced the failure once.
- **`test_ws` heartbeat flake:** PASSED in-suite this round (suite ran
  28:52 under load; VM uptime crossed the ~1030 s threshold before the
  test executed). No action.

## Operational notes

- Migration `0004` exists as a file and is exercised by tests, but no
  developer database in the repo has it applied (`.dev-data/app.db` has
  no style tables) — expected for uncommitted work; the shipped app must
  run migrations on boot as usual.
- The entire Style Lab tree is **uncommitted** by design (verification
  leaves the commit to the parent). One tracked test file
  (`backend/tests/test_execution_approvals.py`, see SL-008) was modified
  by a feature team during the verification window.
