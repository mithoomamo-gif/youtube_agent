# Style Lab Mini-Round — Consolidated Acceptance Summary

Verification team report, 2026-10-23. Tree: HEAD `637cf9a` (Round 4) +
uncommitted Style Lab working tree (36 git-status paths at close).
Defects referenced as SL-xxx live in `docs/STYLELAB_BUGS.md`.

## Overall verdict: **CONDITIONAL PASS**

The feature works end to end with real artifacts — intake → analysis →
style profile → styled generation → gates — and its honesty machinery
(no-copy gate, abstaining classifier, self-disclosing STYLE_MATCH) is
genuinely in place. The condition: **SL-001 (HIGH)** — the REST API
profile-creation path bypasses the strict schema and `assert_no_copy` —
should be fixed before the API path ships, and **SL-002** means analysis
is not yet hands-free from the UI (queued rows are never auto-claimed).

## Regression vs Round 4 baselines

| Suite | Baseline | Result | Verdict |
|---|---|---|---|
| Backend (`backend/tests`) | 1229 passed, 8 skipped | **1668 passed, 2 failed, 8 skipped** (1,678 collected; +~449 new, of which **Style Lab 430/430 standalone-green**) | PASS — both failures non-code: SL-008 test expectation (fixed mid-round, re-run green) + known caplog environment flake (green solo and whole-file) |
| Root (`tests/`) | 44 passed, 1 skipped | 41 passed / 1 failed / 2 errors under load 17.9–35.1 (all timeout-class, zero assertion failures); **settled coordinator re-run of both affected files: 5 passed** | PASS (environmental classification confirmed) |
| UI typecheck | clean | `tsc -b` exit 0, zero errors | PASS |
| UI build | green | exit 0 (only vite's standard >500 kB chunk advisory) | PASS |
| Known flakes ledger | — | `test_ws` heartbeat PASSED in-suite (uptime crossed ~1030 s during the long run); EPERM write fault did not occur | — |

## Acceptance checklist

**(a) Intake — PASS.**
Competitor added per channel via channel-nested routes
(`backend/app/routers/style_lab.py`); attach is multipart-only, validated
by ffprobe (`app/stylelab/media.py`: rejects `no_video_stream`,
`zero_duration`, non-allowlisted containers, >2 GiB; a text file renamed
`.mp4` → 422 with no orphan DB row or file — live-verified). Migration
`0004` creates `competitor_channels` / `competitor_videos` /
`style_profiles`, all FK-scoped to the owning channel. Cross-channel
isolation **proven**: 10 API + 4 store tests in
`backend/tests/test_style_lab_isolation.py`, independently reproduced
live (channel B → 404-never-403 everywhere; foreign-competitor profile →
422). UI exists and is functional (`StyleLabSection` in
`src/screens/Channels.tsx`, `src/api/queries/styleLab.ts`).

**(b) Analysis — PARTIAL.**
Structured breakdowns are real: per-scene classifications + confidences,
duration-weighted visual mix, transcript/speech structure
(`app.stylelab` schema/engine), produced in live runs by the verification
team and by the Analysis Engine team's ground-truth check
(`outputs/stylelab/analysis_check/CHECK.md`, reconciled line-by-line).
Two analysers: the **ledger-based** one is exact against ground truth
(Round 3 facts pack: 25/25 stock scenes → `stock_ratio 1.0` @ 0.9).
The **pixel classifier** with vision on: 17/21 scenes correct, 1 false
AI claim @ 0.57, 3 honest abstentions (committed accuracy 94.4%); with
heuristics only it abstains ~100% (SL-004). Segmentation is the weak
link — 21/25 on the stock montage, 31→1 collapse on the plates video,
whose per-plate accuracy is honestly *unmeasured* (SL-003). Fixture
honesty itself: **PASS** (aggregate-level grading, abstentions counted,
inflated candidate figure explicitly refused) — but it exists only as an
outputs artifact, not a repeatable test (SL-004ii).

**(c) Style profile — PARTIAL.**
Synthesis path is rigorous: strict `style_profile.v1` schema
(`extra="forbid"`, channel-scoped, patterns only — title keywords survive
only as a position enum, descriptions only as section labels),
`synthesize_style_profile` runs `assert_no_copy` (verbatim + ≥4-word
shingle) before returning; `contains_competitor_assets: Literal[False]`.
Persisted proof profile (`outputs/stylelab/proof/style_profile.json`)
independently re-grepped by the verification team: full title absent,
full description absent, **0 shingle leaks**. **But** the REST API
creation path persists arbitrary JSON with neither the schema nor the
gate (SL-001, HIGH), and synthesis→DB wiring is manual.

**(d) Generation — PASS, with flagged soft spots.**
Real proof on disk (`outputs/stylelab/proof/`): styled `final.mp4`
ffprobe-verified (131.2 s, 720p, −14.04 LUFS, real edge-tts Urdu-script
voice, real Whisper captions, 15 Pixabay stock scenes + 1 plate, licence
ledger, 3 thumbnails); its `report.md` contains `## Style Profile Applied`
with per-stage style parameters; `STYLE_MATCH.md` compares measured
attributes and **does not inflate** — it discloses Gate B/QC FAIL, the
frozen manifest, duration and WPM deviations, and a title-length FAIL.
Gates demonstrably run unchanged with a profile (test artifacts: styled
Gate A/B PASS and a styled Gate-A-blocked pack). Generation tests:
**87/87** on the tasked 5-file set. Soft spots (SL-006): the proof script
was the deterministic stub and STYLE_MATCH's cost line doesn't say so;
reported stock share 1.0 hides the one plate scene; the generated title
duplicates a bracket suffix. The proof video itself is correctly
gate-blocked, not publishable (SL-007, observation).

**(e) Boundary audit — PASS (HOLDS).**
No `yt_dlp`/`youtube_dl`/`pytube`/scraping libraries or code anywhere;
dependency diff vs HEAD is only `+python-multipart`. Video bytes enter
only via user-attached files / multipart upload; the sole YouTube contact
is the official Data API v3 (`channels.list`/`videos.list`,
credential-gated, metadata only — no credential → `metadata_unavailable`
with zero HTTP, live-verified). Frames sent to Gemini vision are outbound
analysis of local files. `competitor_analyse.py` is local-file-only and
raises unless the file exists.

**Collision check (added mid-round) — PASS + SL-009 logged.**
`app.stylelab` (engine) and `app.style_lab` (intake) both define
`StyleLabStore`; verified no import/behaviour collision (distinct module
paths and constructors, fully-qualified imports, clean collection of
1,678–1,679 tests). Rename recommendation recorded in SL-009 (keep
`app.style_lab`; rename the engine package to `app.style_analysis`).

**Credential hygiene — PASS.**
Full sweep of tracked tree, working tree (including the newly landed
Style Lab files), docs and 859 output text files: **no real credential
values**; `.env` git-ignored, untracked, mode 600, containing only the
allowed YouTube OAuth pair; `.env.example` all-empty; every pattern hit
classified placeholder / redaction code / synthetic test fixture.

## What remains genuinely open

1. **SL-001 (HIGH)** — enforce schema + `assert_no_copy` on the API
   profile-creation path before shipping it.
2. **SL-002** — a background claimer for queued analyses (intake→analysis
   is manual today).
3. **SL-003 / SL-004** — segmentation quality on gradual-transition and
   plate content; pixel-classifier usefulness depends on the vision path;
   wire the ground-truth honesty check in as a repeatable test.
4. **SL-006** — run a real-LLM styled proof; fix the stock-share
   measurement to surface plate fallbacks; name the stub in cost lines.
5. **SL-009** — package rename before shipping.
6. Everything Style Lab is **uncommitted** (by design — the parent
   commits); migration `0004` is test-exercised but not applied to any
   developer database in the repo.

## Evidence index

- Worker logs: `.tmp-yta/verify-w1b/` (backend suite), `.tmp-yta/verify-w2/`
  (root + UI), `.tmp-yta/verify-w3b/` (intake/analysis/profile, incl.
  `live_engine_output.json`), `.tmp-yta/verify-w4/` + `.tmp-yta/verify-w4b/`
  (generation + boundary, incl. `SUMMARY_w4.md`, `SUMMARY_w4b.md`),
  `.tmp-yta/verify-w5/credential_hygiene_sweep.md` (hygiene),
  `.tmp-yta/verify-w6/` (landed-work sweep).
- Feature-team docs: `docs/STYLE_LAB.md`,
  `outputs/stylelab/analysis_check/CHECK.md`,
  `outputs/stylelab/proof/STYLE_MATCH.md` + `proof_result.json`.
- Process note: verification ran *while the feature was still landing*
  (tree grew 0 → 36 changed paths during the round) and through two
  runtime restarts that killed three worker runs; every killed scope was
  re-dispatched and completed, and every mid-flight snapshot verdict was
  re-checked against the settled tree before being recorded here.
