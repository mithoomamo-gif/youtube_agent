# ROUND 4 — Bug Log & Acceptance Tracker (acceptance worker, verification only)

> **Project:** YouTube Agent (`~/workspace/youtube-agent-app/`)
> **Round:** 4 — full automation engine (2 channels), TTS calibration + Roman→Urdu transliteration A/B, pipeline hardening (stock tiers/cache, single-flight LLM pacing, unregistered-model refusal, unique-call retry counting, report-from-ledger), Gate B conditional-pass policy, Budget Guard full enforcement
> **Date opened:** 2026-10-23
> **Scope of this file:** every FAIL / PARTIAL acceptance item and every defect found by the independent acceptance worker. Items that fully pass are **not** in this file — they are in the acceptance report only.
> **Method / honesty rule:** no claim without an artifact personally inspected (file read, video ffprobed, or test run by this worker). All pytest runs used `TMPDIR=~/workspace/youtube-agent-app/.tmp-yta` with `--basetemp ~/workspace/youtube-agent-app/.tmp-yta/bt_w7*`.
> **Round 3 baselines:** backend **856 passed / 8 skipped** · root **44 passed / 1 skipped** · Phase 0 gate **CONDITIONAL PASS** (commit `e5bf77a`, the HEAD throughout this verification).
> **Final check time:** 2026-10-23 ~01:55 UTC. Round 4 teams were still landing work for most of this window; verdicts below are against the tree/artifacts as they stood at final check.
> **Amendment (2026-10-23 ~01:57–02:05 UTC):** parent-supplied acceptance inputs from the completed Teams B/C/D were incorporated — R4-002 upgraded to **HIGH-PRIORITY** with acceptor-verified `file:line` evidence, R4-007 added, and a “Classified non-defects (parent-supplied, acceptor-verified)” section added (stale fixture pin, `test_content_live` full-run pollution, and the intermittent **EPERM** environment fault, which this worker reproduced live in-session). Verdicts unchanged except where those sections say so.
> **Erratum (session-attribution, applied on late tool-result deliveries):** three pass figures in earlier versions of this file — budget “11 passed in 65.02 s”, combined-set “147 passed in 36.42 s”, and N-2's “12 passed, 3 skipped in 28.44 s” — came from background-result deliveries that the acceptor could not, on later delivery of its own session records, attribute to its own runs (the acceptor's combined-set session in fact EPERM-aborted in 2.3 s; a similar misattribution affected one stock-resolution breakdown quoted in the acceptor's chat report, corrected separately to the parent: Space project stock = 14 clips, 13×1920×1080 + 1×1280×720, ~147.6 MB total). All three figures were therefore **withdrawn and re-verified by first-hand foreground re-runs**: budget **11 passed in 26.48 s**; combined 10-file hardening+gate set **168 passed in 15.54 s**; `test_content_live` — acceptor's own session **9 passed in 12.93 s**, then **8 passed / 1 failed** on foreground re-run with the failure root-caused to the canonical `models.yaml` being polluted in the working tree at that moment (see N-2, including the restore-before-commit hazard). No checklist verdict changed — budget and hardening/gate remain PASS on the re-verified numbers; N-2's classification (pollution, not a product defect) is strengthened, not changed. Figures in this file that came from the acceptor's own foreground runs or files read directly (102 gate tests, 100-test hardening log, 26 planning tests, N-1 run, all ffprobes) are unaffected.

> **Fix-pass amendment (2026-10-23, later the same day):** a dedicated fix pass landed after this file's final check. **R4-001 resolved by new evidence, R4-002 resolved, R4-003 partially resolved**, the D3 guardrail taboo-screening defect resolved, and N-1/N-2 closed. Final settled-tree verification by the fix-pass coordinator: backend **1222 passed / 1 failed / 8 skipped** (the 1 failure is the known R4-007 in-suite flake — green standalone and in slices; stays monitor), root **44 passed / 1 skipped**, UI typecheck + build green. Full detail in **§ Fix-pass resolutions** at the end of this file.

**Severity legend:** `blocker` = Round 4 cannot be called done while open · `major` = a deliverable works partially or a suite is red · `minor` = works, but degraded / noisy · `trivial` = cosmetic / docs.

---

## Acceptance results — summary (full detail in the acceptance report)

| # | Item | Verdict |
|---|---|---|
| 1 | Automation proof — unattended tick → end-to-end, 2 channels, 2 real videos (`outputs/round4/automation/`) | ❌ **FAIL** at final check — since **RESOLVED by later evidence** (fix pass), see R4-001 resolution |
| 2 | TTS calibration proof (`outputs/round4/tts_proof/`) — measured WPM, ±10% duration without per-run rate override, transliteration A/B | ⚠️ **PARTIAL** — evidence real, shipped default does not embody it: see **R4-002 (HIGH-PRIORITY)**, R4-004, R4-006, R4-007 — **R4-002 resolved in the fix pass** (see § Fix-pass resolutions); R4-004/R4-006 remain open minors |
| 3a | Hardening — stock smallest-adequate tier + cache cap | ✅ PASS (unit + live-run evidence; see report) |
| 3b | Hardening — single-flight LLM queue with pacing | ✅ PASS (tests run by this worker) |
| 3c | Hardening — router refuses unregistered models | ✅ PASS (tests run by this worker) |
| 3d | Hardening — unique-call retry counting | ✅ PASS (tests run by this worker) |
| 3e | Hardening — report-from-ledger integrity | ✅ PASS (tests run by this worker) |
| 4 | Gate B conditional-pass policy (`outputs/round4/gate_policy/`) | ✅ PASS (acceptance replay + acceptor's gate-test runs: 102 passed foreground, and the gate files inside the verified 168-test combined re-run — see erratum; thresholds unchanged) |
| 5 | Budget Guard full enforcement (`backend/tests/test_budget_guard_full.py`) | ✅ PASS — **11 passed** in this worker's final run (an earlier mid-landing run was 10 passed / 1 failed; the failure was fixed by the owning team before the final run — chronology in the acceptance report, not a bug entry because the item fully passes at final check) |

---

## Bug table

| ID | Severity | Item / File | Evidence (artifact personally inspected) | Status |
|---|---|---|---|---|
| R4-001 | **blocker** | Automation proof — `outputs/round4/automation/`, harness `tools/round4_automation_proof.py` | Three runs observed, none produced a real video: run 1 (00:28–00:30 UTC) all-stub, `videos: []`; run 2 (00:53–~01:18 UTC) real through the visuals stage, never completed render, no `run_log.json`; run 3 (from ~01:37 UTC) left only 0-byte `_proof_state/app.db` + `waltest.db`, no projects, no `run_log.json` at final check (~01:55 UTC) | **RESOLVED** — fix pass, by new evidence (see § Fix-pass resolutions) |
| R4-002 | **HIGH-PRIORITY (major, top gap)** | TTS calibration — adopted Urdu-script default is **not shipped**: canonical store is script-blind + `tts_input_script` still defaults to `"roman"` at `backend/app/integration/models.py:80`, `backend/app/providers/tts/engine.py:41`, `backend/app/providers/tts/fallback.py:34` (all three verified by this worker, see detailed entry) | Run 1 preflight (`outputs/round4/tts_proof/run1_preflight_result.json`): planned at Roman-sample 287.43 WPM → voice 223.716 s vs 180 s target = **+24.29%, `within_pm10_band: false`**. The ±10% pass (run 2, 182.7 s, +1.5%) required a **proof-local** recalibrated file (`outputs/round4/tts_proof/calibration_recalibrated.json`) that is **not** the canonical store. A/B was decisive (raw script match **0.0024 → 0.6110**, `ab_results.json`), so the shipped default now contradicts the round's own adopted decision | **RESOLVED** — fix pass: script-aware canonical store + `urdu_script` default shipped (see § Fix-pass resolutions). Was: open (team's own `CALIBRATION.md` §6.1 agrees: fix “not done here”) |
| R4-007 | minor | Planner override WARNING log — `backend/tests/test_tts_calibration_planning.py::test_plan_explicit_override_plans_at_its_own_bucket` vs `backend/app/integration/planning.py` | **Team-reported:** behaviour correct (explicit override plans at its own bucket: rate_source `spec-override`, bucket −25%, 320 words in the fixture) but the override WARNING log was not emitted from `app.integration.planning` in the team's run. **Acceptor re-check (clean run, ~02:00 UTC): not reproducing** — the warning exists at `planning.py:180–186` (`logger.warning("TTS rate override explicitly set by user: …")` in `resolve_planning_rate`) and the whole file passes: **26 passed in 1.08 s**, including this test. Filed per parent instruction with both observations; treat as fixed-since-team-run or intermittent — monitor, do not re-open without a failing clean run | not reproducing (acceptor clean run) — monitor |
| R4-003 | **major** | Automation stage handlers — silent stub fallback: `backend/app/execution/handlers.py` (documented in `docs/ROUND4_AUTOMATION.md` §3) | If `integration_client.call_stage()` returns `None` **or raises any exception**, the handler silently substitutes a deterministic stub artefact; the exception is not recorded in the job result. Run 1 (R4-001) is the demonstrated consequence: a complete “all jobs terminal → status `ready` → fake publish simulated” run with **zero real media** | **RESOLVED (closure pass)** — stub substitution only on explicit stub-mode selection; real-stage failures write a `failed` `agent_runs` row and raise `StageFailedError` into the runner failure policy (retry once → park project `failed`). Was: PARTIALLY RESOLVED after the fix pass (see § Fix-pass resolutions and the R4-003 closure addendum there) |
| R4-004 | minor | TTS proof production — visuals not stock: `outputs/round4/tts_proof/run2/samundar-ki-gehrai-ke-raaz-347db799/visuals/scene_manifest.json`, `CALIBRATION.md` §6.2 | Accepted proof production used **31/31 local plates** (`source: plate:local`, stock share 0.0) because no `PIXABAY_API_KEY` was supplied to that worker. Duration/Gate results are unaffected, but the TTS proof itself does not exercise the stock path (the automation run 2 did — see report §3a) | open — disclosed by the team; re-run proof with Pixabay key to close |
| R4-005 | minor | Subtitle script-match in production QC still raw/cross-script: `outputs/round4/tts_proof/run2/produce_result.json` (QC warnings), `outputs/round4/gate_policy/subtitle_metrics.md` | Accepted TTS production's assemble QC still reports `script_match_ratio: 0.0` (Roman reference vs Urdu-script Whisper captions). The transliteration-aware **fair** metric exists and was recomputed honestly for Round 3 (facts 0.0066→0.1279, history 0.0000→0.0339, cricket 0.0041→0.0558 — still low, i.e. the ASR itself is weak, not just the metric), but it is not the metric the production QC summary reports | **RESOLVED** — Round 5: production QC now reports BOTH scores, each named (see detailed entry) |
| R4-006 | minor | Transliteration A/B — critical-token quality: `outputs/round4/tts_proof/ab_results.json` (`verdict` block) | Adopted Path B (Urdu-script input) wins WER decisively (strict 0.389 vs 0.9976; skeleton 0.3252 vs 0.8374) but matches **fewer** critical tokens than Path A (**4/15 vs 5/15**; strict+near-miss audit ties them ~10/15), and dates/numbers still garble on both paths (B heard the 1960 date as “1907”, depth 10935 as “1935”). Numbers/names remain the weakest class after adoption | open — documented limitation, not a regression |

---

## Detailed entries

### R4-001 — blocker — Automation proof: no real video from the automation path was ever evidenced

**Claim under test:** an unattended schedule tick drives end-to-end production for **2 channels**, producing **2 real proof videos** under `outputs/round4/automation/`.

**What was observed (all times UTC, 2026-10-23):**

1. **Run 1 (00:28:03–00:30:06)** — `outputs/round4/automation/run_log.json` (read in full by this worker):
   - Seeds were correct: channel A `channel-a-facts` (builtin `facts`, topic “Samundar ki Gehrai ke Raaz”), channel B `channel-b-space-mysteries` (OTHER/custom, `source='other'`, topic “Space ke Woh Raaz Jo Ab Tak Solve Nahi Hue”), both `publish_mode='auto'`, `target_minutes=1`, `human_interventions: 0`, tick outcome `all_jobs_terminal`, all 10 DAG jobs `done` per project.
   - **But every content stage except research was a stub:** `content_stage_stub_flags = {script, voice, visuals, render, thumbnail, qc, package: true}`, `all_content_stages_real: false`, artefact `file_path`s are `stub://<project>/…` with `exists_on_disk: false`, **`videos: []` for both channels**, copied pack per channel = only `research.json` (1,443 B / 1,545 B), publish via `pack_flow: fake_provider_direct_no_pack`.
   - Root cause visible in the log itself: at run start the stage bridge was not yet wired (`bridge_at_start.run_functions` lacks `run_loop`; the bridge module `backend/app/integration/stage_bridge.py` landed at ~00:40, after this run).
2. **Run 2 (started 00:53, harness since edited at 01:35)** — real stages, incomplete:
   - Projects `480ea338-…` (facts) and `bae6b3b8-…` (space mysteries): real Gemini scripts (`bridge_state.json`: `script.llm_path = "gemini-live"`), real edge-tts audio (chapter WAVs + sidecars), real Pixabay stock (see report §3a for per-clip sizes/resolutions), render segments in progress (7 and 6 segment files at last good observation, ~01:25).
   - The run **ended without completing**: `bridge_state.json` stops at `visuals` for both projects, no `final.mp4` / pack was ever produced under `projects/`, the channel copy folders contained only `research.json` (channel A) / nothing (channel B), and **no `run_log.json` was written** for this run. The harness process was gone by ~01:31.
   - While the run was live this worker also observed the throwaway DB in a suspect state (`outputs/round4/automation/_proof_state/app.db-shm` truncated to 3 bytes; direct sqlite reads returned `OperationalError: disk I/O error`). Other workers were simultaneously investigating a filesystem/WAL write quirk in this environment (scratch dirs in `/tmp`), so the precise cause (app vs environment) is **not** established — recorded here as observed, not attributed.
3. **Run 3 (from ~01:37)** — the outputs root was cleaned again; at final check (~01:55) `outputs/round4/automation/` contained only `_proof_state/app.db` (**0 bytes**) and `_proof_state/waltest.db` (**0 bytes** — a WAL test file left by the owning worker's debugging). No projects, no videos, no `run_log.json`, no report.

**Reproduction:**

```bash
ls -R outputs/round4/automation/            # at final check: only _proof_state/{app.db,waltest.db}, both 0 bytes
find outputs/round4/automation -name '*.mp4' # no production video (run-2 stock clips under projects/ were also gone at final check)
# run 1 evidence is preserved in the transcript of this verification and was read from
# outputs/round4/automation/run_log.json (generated_at 2026-10-23T00:28:03Z) before run 2 cleaned the root:
#   "all_content_stages_real": false, "videos": [] for both channels
```

**Verdict:** FAIL. The tick→DAG→gates→simulated-publish *machinery* is proven (run 1, albeit on stubs; run 2's real research/script/voice/visuals stages), but the Round 4 headline claim — 2 real videos from 2 channels via the automation path — has **no artifact**. If a later run lands after this file was written, it must be re-verified from its `run_log.json` stub audit + ffprobe of both videos; do not close this on a job-status claim alone (see R4-003).

### R4-002 — HIGH-PRIORITY (major, top gap) — Adopted Urdu-script TTS default is not shipped: canonical calibration is script-blind and all three `tts_input_script` defaults are still `"roman"`

**Severity rationale (parent-supplied Team B input, acceptor-verified):** this is the round's top gap. The A/B was decisive — raw script match **0.0024 (Path A) → 0.6110 (Path B)** in `outputs/round4/tts_proof/ab_results.json` — and the team adopted Urdu-script input for ur-PK voices, yet a default production run today still takes the Roman path with Roman-measured planning numbers. The TTS checklist item is therefore graded **PARTIAL**, not PASS: the calibration *evidence* is real, the shipped *default* does not embody it.

**The three default sites — verified by this worker at the exact supplied locations (2026-10-23, amendment pass):**

```bash
grep -n 'tts_input_script' backend/app/integration/models.py backend/app/providers/tts/engine.py backend/app/providers/tts/fallback.py
# backend/app/integration/models.py:80:    tts_input_script: str = "roman"     (ProjectSpec; comment at :75-79: "the A/B winner flips the default later, not this field's existence")
# backend/app/providers/tts/engine.py:41:    tts_input_script: str = "roman"     (JobResult; also the last_transliteration default at engine.py:55)
# backend/app/providers/tts/fallback.py:34:  tts_input_script: str = "roman"     (VoiceProfile)
```

**Evidence (all read by this worker):**

- `backend/app/providers/tts/calibration_data.json` (canonical, schema v1, measured 2026-10-23T00:05:54Z) stores **one** WPM table per voice, keyed by voice + rate only. For both ur-PK voices the sample is `roman_urdu_facts`: Uzma **287.43 WPM** @0%, Asad **278.22 WPM** @0%.
- The same measurement run (`outputs/round4/tts_proof/wpm_measurements.json`, 24 rows, 0 failures, ffprobe durations) shows the **Urdu-script** sample for the same voices: Uzma **179.58 WPM** @0%, Asad **174.91 WPM** @0% — a ~108 WPM script effect the canonical store cannot represent.
- **Run 1** (canonical store, A/B-winning `urdu_script` input, no rate override): plan 862 words @287.43 WPM → real Gemini bundle 656 words → voice **223.716 s** (delivered 175.94 WPM) vs 180 s target = **+24.29%**, `within_pm10_band: false`; Gate A also FAIL (climax at 58% vs required 65–75%). Source: `outputs/round4/tts_proof/run1_preflight_result.json`.
- **Run 2** (proof-local recalibrated store: Uzma rate-0 bucket = A/B long-form **169.3 WPM**): plan 508 words → bundle 521 words → voice/final **182.7 s = +1.5%**, `within_pm10_band: true`, Gate A/B/QC PASS. Source: `outputs/round4/tts_proof/run2_preflight_result.json`, `run2/produce_result.json`. The recalibrated file lives at `outputs/round4/tts_proof/calibration_recalibrated.json` — it is **not** `backend/app/providers/tts/calibration_data.json`, and no production code path loads it by default.
- The adopted input script is opt-in only: `tts_input_script` code default remains `"roman"` (`CALIBRATION.md` §3, asserted by `backend/tests/providers/test_tts_transliteration.py`).

**Reproduction:**

```bash
cat backend/app/providers/tts/calibration_data.json        # ur-PK rows: roman_urdu_facts only
python3 -c "import json;d=json.load(open('outputs/round4/tts_proof/run1_preflight_result.json'));print(d['voice'])"  # 223.716 s, +24.29%, within_pm10_band False
python3 -c "import json;d=json.load(open('outputs/round4/tts_proof/run2_preflight_result.json'));print(d['voice'])"  # 182.7 s, +1.5%, True — recalibrated_store: true
```

**Required fix (per Team B, agreed):** (1) canonical calibration needs a **(voice × input-script)** dimension — or, minimally, the ur-PK rows re-measured on the Urdu-script sample now that Path B is adopted; (2) flip the `tts_input_script` default to `"urdu_script"` for ur-PK voices at all three sites above (integration/models.py:80, tts/engine.py:41, tts/fallback.py:34). Until then, a default production run either takes the Roman path the A/B rejected, or — if Urdu-script is opted into per-run — plans it from Roman WPM and misses its duration target by ~+24% (run 1, above). The proof's PASS was achieved only via the documented recalibrated file (`outputs/round4/tts_proof/calibration_recalibrated.json`), which no production code path loads by default.

### R4-003 — major — Automation handlers silently fall back to stubs on any real-stage error

**Evidence:** `docs/ROUND4_AUTOMATION.md` §3 (team's own doc, read by this worker): “if `call_stage` returns `None` *or raises any exception*, the stage handler falls back to a deterministic stub artefact (`stub: True`, `file_path` of the form `stub://…`)… This fallback is silent in the handler (the exception is not recorded in the job result), so a run's stub flags … are the record of whether the real stage actually ran.”

**Demonstrated impact:** R4-001 run 1 — the entire 10-job DAG for both channels completed, projects reached `status='ready'` and a (simulated) publish was recorded, while 7/8 content stages per project were stubs and no media existed. Only the harness's explicit stub audit (`all_content_stages_real: false`) made this visible; job statuses alone would have reported success.

**Reproduction:** read run 1's `run_log.json` stub audit (quoted in R4-001), or force any `run_<stage>` in `backend/app/integration/stage_bridge.py` to raise and observe the DAG still completing with `stub://` artefacts.

### R4-004 — minor — TTS proof production used local plates for all visuals

Evidence and reproduction in the bug table above. The proof's own report discloses it (`visuals: local-plates`, “Local plate scenes: 31” in `outputs/round4/tts_proof/report.md`, read by this worker). Stock itself is not in question — automation run 2 downloaded real Pixabay stock through the hardened tier path (report §3a) — but the TTS proof pack cannot be cited as a stock-visual production.

### R4-005 — minor — Production QC still reports the cross-script `script_match_ratio: 0.0`

Evidence in the bug table above. The fair metric (`backend/app/pipeline/subtitles/validation.py::script_match_ratio_fair`, tests in `backend/tests/pipeline/test_subtitle_fair_metric.py` — passed in this worker's runs) fixes the *measurement artefact*; it is not what the assemble/production QC summary emits, and the fair recompute itself shows the underlying Whisper captions agree with the scripts on only a small share of tokens (see `subtitle_metrics.md` honest interpretation).

**RESOLVED (Round 5, production-quality carry-overs):** the existing fair metric is now wired into the production QC path — no second implementation was written. `check_script_match` computes both scores and `ScriptMatchReport` carries them (`ratio` + `ratio_fair`) with a `metrics_dict()` that pairs each score with its metric name (`STRICT_SCRIPT_MATCH_METRIC = "script_match_ratio"`, `FAIR_SCRIPT_MATCH_METRIC = "script_match_ratio_fair"`). `SubtitleResult.metadata`, the assemble stage's `subtitle_provenance` (embedded verbatim as `qc_summary["subtitles"]` in every production QC report), and a new `## Subtitle Script Match` section in the pack `report.md` all carry BOTH scores, named. The subtitle script-match WARN message now includes the fair score as report-only context. **Gate rule (unchanged by design):** no documented Gate B WARN rule ever referenced subtitle scores — `pipeline.gates.conditional.classify_qc` reads QC verdict + reason-code flags only — so no threshold was changed: `passed` is still decided by the strict score against the unchanged 0.98 bar, and the fair score's role is pinned by the named policy `SUBTITLE_SCRIPT_MATCH_GATE_POLICY = "report_only"` (`backend/app/pipeline/subtitles/validation.py`), including a pinned case where fair = 1.0 / strict = 0.0 still yields `passed = False`, and Gate B fixtures whose QC-token evidence carries subtitle scores produce byte-identical outcomes. Tests: `backend/tests/pipeline/test_qc_fair_subtitle_metric.py` (16 tests) plus the existing subtitle/fair/QC/gate suites — see the Round 5 report for counts. The underlying ASR weakness this entry also records is NOT fixed by this wiring (that is R4-006 territory); the fair score now simply stops the script mismatch from being counted twice in production reports.

### R4-006 — minor — Critical tokens (names/numbers/dates) remain garbled after transliteration adoption

Evidence in the bug table above (`ab_results.json` verdict block, read in full by this worker). Path B is still the correct adoption (WER/skeleton/duration all win decisively); this entry records that the A/B's first-listed criterion — critical tokens — did **not** improve (4/15 vs 5/15 strict; ~10/15 each after the team's documented near-miss audit), so downstream consumers must not claim name/number fidelity from this round.

### R4-007 — minor (team-reported) — Planner override WARNING log not emitted in the team's run; NOT reproducing in the acceptor's clean re-run

**Team report (Team B):** `backend/tests/test_tts_calibration_planning.py::test_plan_explicit_override_plans_at_its_own_bucket` — the planning *behaviour* is correct (explicit `tts_rate_percent=-25.0` → `rate_source="spec-override"`, calibration bucket −25%, fixture WPM 160.0, target 320 words), but the override WARNING log was not emitted from `app.integration.planning` in their run; the test's final assertion is `any("override" in r.message.lower() for r in caplog.records)` under `caplog.at_level(logging.WARNING, logger="app.integration.planning")`.

**Acceptor verification (amendment pass, ~02:00 UTC):**

- Code inspection: the warning **exists** in the current tree — `backend/app/integration/planning.py:180–186`, inside `resolve_planning_rate` (called by `plan_word_target` at :289): `logger.warning("TTS rate override explicitly set by user: %+.1f%% …")`, logger = `logging.getLogger(__name__)` = `app.integration.planning`.
- Clean re-run by this worker: `pytest backend/tests/test_tts_calibration_planning.py` → **26 passed in 1.08 s**, including this test. (The first two re-run attempts in this pass were aborted by the live EPERM fault — see N-3 — which is itself a plausible mechanism for a caplog/tempfile-adjacent failure in a contended run.)

**Disposition:** filed at the parent's instruction with both observations. On current evidence this is either already-fixed-since-the-team's-run or an intermittent/environmental failure — **not** a standing code defect. Keep as monitor; re-open only on a failing clean (isolated, EPERM-free) run.

---

## Classified non-defects (parent-supplied, acceptor-verified)

These were supplied by the completed teams as classifications and were **independently checked by this worker**. They are recorded here so nobody re-files them as product bugs. They are **not** code regressions.

### N-1 — Stale fixture pin, NOT a regression: `test_visuals_short_scenes` 17.455 s expectation

- **File:** `backend/tests/integration/test_visuals_short_scenes.py:103` — fixture-sanity assertion `voice.duration_s == pytest.approx(17.455, abs=0.5)` for the Round-2-shaped spec (`facts`, “Ocean depth secrets”, `minutes=0.5`, durations synthesised at 275 WPM from the bundle's word count).
- **Acceptor's own run (~02:00 UTC):** **1 failed, 3 passed** — `assert 33.163636363636364 == 17.455 ± 0.5`.
- **Why it is stale, not a defect:** under Round 4 calibrated planning the same 0.5-minute spec now produces a longer bundle, so the synthesised narration measures **33.16 s ≈ the spec's own 30 s target** — the planning change working as intended. The 17.455 s pin encodes the pre-calibration (Round 2/3) word count.
- **Action:** re-pin the fixture (and the docstring's “~17.45 s” framing) to the calibrated bundle; the remaining 3 tests in the file pass, and the scene-timeline assertions after the pin were not reached in the failing test — re-check them at re-pin time. A full backend-suite run will show this 1 failure until re-pinned; do not count it as a Round 4 code regression.

### N-2 — Cross-team `models.yaml` pollution, NOT a product defect: `test_content_live` model-set failure — pollution caught live in the working tree

- **Team report (Team B):** the `test_content_live` model-set failure seen in full-suite runs passes in isolation; cause is cross-team pollution of `backend/config/models.yaml` during parallel runs (substituted flash-lite registries).
- **Corrected acceptor numbers (see erratum — the “12 passed, 3 skipped in 28.44 s” figure first filed here was NOT the acceptor's session and is withdrawn):**
  - Acceptor's own isolation session (`bt_w7_followup6`, ~02:02 UTC, per its session record): **9 passed in 12.93 s** — at that moment the config was still clean.
  - Acceptor's foreground isolation re-run (erratum pass): **8 passed, 1 failed in 16.90 s** — `test_real_router_records_ledger_and_models`, `tests/integration/test_content_live.py:241`: `assert {'gemini-3.1-flash-lite'} == {'gemini-3.8-flash', 'gemini-3.1-pro-preview'}`.
- **Root cause, verified by the acceptor at re-run time:** `git status` shows `backend/config/models.yaml` **modified vs HEAD**, and `git diff` shows it rewritten as a YAML dump (comments/formatting stripped) with the Gemini entries at T0/T1/T2 (lines 8, 28, 48) **all set to `gemini-3.1-flash-lite`** — a proof-style substituted registry written over the canonical config. HEAD's file has the expected T1 `gemini-3.8-flash` / T2 `gemini-3.1-pro-preview` that the test asserts. The test, the router and the product code are all behaving correctly against the file they are given; the file itself was polluted.
- **Disposition:** environment/working-tree pollution, not a product defect — classification as supplied by Team B, now with direct evidence. **Coordinator hazard (new, from this verification): the canonical `backend/config/models.yaml` is polluted in the tree right now; it must be restored from HEAD (`git checkout -- backend/config/models.yaml`) before any Round 4 commit, or the flash-lite substitution ships as the production registry.** Which harness wrote it in place is not established (the TTS and automation harnesses both wrote their substituted copies to their own paths — `.tmp-yta/proof_work/proof_registry.yaml` and `outputs/round4/automation/models_substituted_flash_lite.yaml` respectively); do not attribute without further evidence. Any future red on this file must be checked against `git diff backend/config/models.yaml` before filing.

### N-3 — Intermittent EPERM environment fault on new-file writes (Teams C/D note, acceptor reproduced live)

- **Team report (Teams C/D):** an intermittent `EPERM` fault on new-file writes appeared during the round — pytest capture/sqlite failures, e.g. a `test_ws` `OperationalError`.
- **Acceptor reproduction (2026-10-23 ~01:57 UTC, in-session, verbatim):** `open('/tmp/w7_probe2','w')` → `PermissionError: [Errno 1] Operation not permitted` despite uid 0, `/tmp` mode 1777 and `os.access('/tmp', W_OK|X_OK) == True`; `tempfile.mkstemp` failed identically in `/tmp`, `~/workspace/youtube-agent-app/.tmp-yta`, `~/workspace` and `~`; `tempfile.gettempdir()` raised `FileNotFoundError: No usable temporary directory found`, aborting two pytest launches before any test ran. The fault **cleared within minutes** (write probe OK at ~02:00 UTC, after which the N-1/R4-007/N-2 runs above executed normally). Earlier in the round the same class of anomaly was observed as: `/tmp` reset (~00:37 UTC, VM reboot), 0-byte DB files and a 3-byte `app.db-shm` with transient sqlite `disk I/O error` in `outputs/round4/automation/_proof_state/` (see R4-001), and this worker's first budget-suite attempt being SIGTERM-killed under load.
- **Disposition rule applied throughout this file (per parent instruction):** any failure that looks write-related is treated as **suspect-environmental** and is **not** logged as a code bug without a clean re-check. Concretely: (i) R4-001's verdict rests on *absence of artifacts* (no automation video existed under any interpretation, including in EPERM-free windows) and therefore stands — but run 2/3's *cause* may be this fault rather than the harness, and the entry says so; (ii) the budget mid-landing failure was an assertion-value mismatch (not a write fault) and passed 11/11 clean at final check; (iii) R4-007's team-reported failure is filed as not-reproducing for the same reason.

---

## Verification runs (this worker only)

| Run | When (UTC) | Result |
|---|---|---|
| Gate policy tests: `test_gate_conditional_policy.py`, `test_gate_b_conditional_flow.py`, `test_gate_b_audit.py`, `test_qc_declared_annotation.py` | ~00:15 | **102 passed** |
| Hardening set (first run): `test_llm_model_validation.py`, `test_llm_pacing.py`, `test_llm_agent_loop_unique_calls.py`, `test_report_integrity.py`, `test_stock_tiers_cache.py`, `test_tts_calibration.py`, `test_tts_transliteration.py`, `test_subtitle_fair_metric.py` | ~00:37 | **100 passed** |
| `backend/tests/test_budget_guard_full.py` (mid-landing) | ~00:23 | 10 passed, **1 failed** — `test_per_channel_budget_view_sums_to_totals`: `assert snap["day"]["settled_micros"] == 40_000` → `assert 0 == 40000` (line 604). Fixed by the owning team during the round; see final run below |
| `backend/tests/test_budget_guard_full.py` (final, settled tree) | ~01:46 | **11 passed** — timing/attribution of the original “65.02 s” figure withdrawn in the erratum below; result itself re-verified first-hand in the erratum pass: **11 passed in 26.48 s** (foreground run, basetemp `bt_w7_verify_budget`) |
| Final combined re-run (settled tree): hardening set above + `test_gate_conditional_policy.py` + `test_gate_b_conditional_flow.py` | ~01:48 | “147 passed in 36.42 s” **withdrawn** in the erratum below (acceptor's own session for that command EPERM-aborted in 2.3 s; the figure could not be attributed to the acceptor's run). Verified replacement, erratum pass, foreground, basetemp `bt_w7_verify_combined`: **168 passed in 15.54 s** for the identical 10-file set |
| Gate B acceptance replay artifacts | — | `outputs/round4/gate_policy/acceptance.md` status **PASS**; this worker independently ffprobed `cricket_replay/final.mp4` (207.7 s, 1280×720, H.264+AAC, 63,028,981 B) and read `gate_b_audit.jsonl` (4 entries: minted / approval_recorded / publish_released / blocked) |
| QC threshold integrity | — | `git diff backend/app/pipeline/qc/technical.py` is annotation-only; constants unchanged from HEAD (`BLACK_WARN_MIN_S=0.5`, `BLACK_BLOCK_MIN_S=2.0`, freeze/silence/loudness constants likewise) — corroborated by `outputs/round4/gate_policy/verification_notes.md` |
| TTS proof video | — | ffprobe by this worker: `outputs/round4/tts_proof/final.mp4` = 182.7 s, 1280×720, H.264+AAC, 10,446,002 B, sha256 `5eeea2f4a80b7cc3c5a2f167178e715e9d63405b7544fc4dbb109c76a6f1ed57` (identical copy in `run2/…/pack/`) |
| Automation videos | — | **None existed to probe** at any check (see R4-001) |
| Amendment: `backend/tests/test_tts_calibration_planning.py` (R4-007 re-check) | ~02:00 UTC | **26 passed** in 1.08 s (first two attempts aborted by the live EPERM fault, N-3) |
| Amendment: `backend/tests/integration/test_visuals_short_scenes.py` (N-1) | ~02:00 UTC | 3 passed, **1 failed** — stale pin only: obtained 33.1636 s vs pinned 17.455 s (33.16 s ≈ the spec's 30 s target) |
| Amendment: `backend/tests/integration/test_content_live.py` in isolation (N-2) | ~02:02 UTC | “12 passed, 3 skipped in 28.44 s” **withdrawn** (not the acceptor's session — see erratum). Acceptor's own session: **9 passed in 12.93 s**; foreground re-run in the erratum pass: **8 passed, 1 failed in 16.90 s** — the 1 failure root-caused to the polluted working-tree `models.yaml` (see N-2) |
| Amendment: EPERM reproduction probes (N-3) | ~01:57 UTC | `open`/`mkstemp` → `PermissionError [Errno 1]` on `/tmp`, workspace and home despite root + `W_OK`; cleared spontaneously within minutes |

---

## Fix-pass resolutions (2026-10-23, coordinator fix pass — after the acceptance final check)

Verification basis: the fix-pass coordinator's own settled-tree runs (backend **1222 passed / 1 failed / 8 skipped** in 340.36 s; root **44 passed / 1 skipped**; UI typecheck + build green) plus the per-item worker evidence cited below. Baselines: Round 3 backend 856 passed / 8 skipped; the pre-fix-pass full run was 1195 passed / 8 failed / 8 skipped — the 8 classified failures are now 1 (the R4-007 in-suite flake, see “Still open”).

### R4-001 — RESOLVED by new evidence (not by a code fix in this pass)

A later clean automation run landed after the acceptance final check: `outputs/round4/automation/run_log.json` generated 2026-10-23T02:53:44Z, tick wall 768.1 s, `human_interventions: 0`, `run_outcome: "all_jobs_terminal"`; both channels 10/10 jobs `done`, final status `ready`. Both videos were ffprobe-verified by the fix-pass evidence worker (not stock clips):

- Channel A (`channel-a-facts`, “Samundar ki Gehrai ke Raaz”, project `938dd37d-…`): `projects/938dd37d-…/pack/final.mp4` = 40,329,611 B, **76.47 s**, 1280×720 H.264+AAC, sha256 `e2fccf2a…`, QC all PASS, **−14.03 LUFS**, cost $0.00834.
- Channel B (`channel-b-space-mysteries`, OTHER/custom, “Space ke Woh Raaz Jo Ab Tak Solve Nahi Hue”, project `65c939bc-…`): `projects/65c939bc-…/pack/final.mp4` = 42,225,432 B, **84.07 s**, 1280×720 H.264+AAC, sha256 `aa4ab6a0…`, **−14.02 LUFS**, cost $0.008166.

Provenance is real throughout (script `gemini-live` on `gemini-3.1-flash-lite` with the tier substitution disclosed and the substituted registry restored sha256-verified; voice edge-tts `ur-PK-UzmaNeural`; visuals stock-first Pixabay, 19 scenes, `stock_share: 1.0`, 0 plates; subtitles whisper). Publish remains `fake_provider_upload_simulated` — a production pass, not a real upload.

Two caveats recorded, not hidden: **(i)** the run log's `all_content_stages_real` still literally reads `false` for both channels, while its per-stage stub audit is all-null (`stub_flags_found: []`, `stub: null` for every stage) — the harness treats a null flag as not-affirmatively-real, so this close rests on the stub audit + provenance + ffprobe, never on that flag alone, in either direction. **(ii)** Channel B's pack paperwork is internally inconsistent: its `report.md`/`package.json` claim `gate_a_passed: false`, `gate_b_passed: false`, `publish_state: "manifest_frozen_not_uploaded"` (Gate A retention-lint warning), while its `publish_manifest.json` and the run log record the DAG gates `auto_approved` and a finalised simulated publish. Channel A has no such conflict. B's video/QC evidence is unaffected; the follow-up is to reconcile the pack-report generator with the finaliser's gate record (documentation-consistency, minor).

### R4-002 — RESOLVED

- **Canonical calibration is script-aware:** `backend/app/providers/tts/calibration_data.json` now carries a per-voice `scripts` dimension populated from the measured rows. urdu_script (sample `urdu_script_facts`): **Uzma {−25: 134.67, −10: 161.59, 0: 179.58, +10: 197.52}**, **Asad {−25: 131.23, −10: 157.44, 0: 174.91, +10: 192.41}** WPM. Roman rows retained verbatim (Uzma 287.43 / Asad 278.22 @0); English voices roman-only with `default_input_script: roman`.
- **Defaults flipped, voice-aware:** `tts/engine.py` JobResult default is now `"urdu_script"` (and the `last_transliteration` default); `integration/models.py` ProjectSpec resolves via `_resolve_tts_input_script_default` (`urdu_script` iff the voice is Urdu and the field was not explicitly set — an explicit caller choice always wins); `tts/fallback.py` VoiceProfile `tts_input_script` is `None` → resolved by voice locale (ur → `urdu_script`, else `roman`). Per-channel override intact.
- **Planner** resolves the (voice × actual input script) row; tests pin both directions, including the inverse mismatch (planning a roman row against an urdu_script production fails the ±10% band — the run-1 +24% shape cannot recur silently).
- **Coupled re-pin (the N-1 action):** `backend/tests/integration/test_visuals_short_scenes.py` re-pinned to the settled calibrated behaviour — narration **17.455 → 19.2 s**, scenes **10 → 11**, with explanatory comments; scene-validity assertions unchanged. File: **4 passed**. (The 33.16 s figure in N-1 was measured under Roman planning; the shipped Urdu-script planning produces 19.2 s for that spec.)
- Fix worker's slices: focused TTS 103 passed, niches 49 passed, combined regression slice (providers/ + planning + short-scenes + glue + bridge + loop + report-integrity + gate-B flow) **349 passed**.

### R4-003 — PARTIALLY RESOLVED (verified line-by-line in the fix pass; the “log + propagate” shorthand is only half correct)

- **Fixed:** the stage bridge (`backend/app/integration/stage_bridge.py`, `_build_content`) no longer masks live failures — the old blanket fallback was removed and genuine errors propagate verbatim to the handler; the handler (`backend/app/execution/handlers.py:497–505`) logs every real-stage exception with traceback (`logger.exception`, project/stage/kind) before any fallback.
- **Still open (the defect's core, narrowed to the handler layer):** on a real-stage exception the handler still substitutes the deterministic stub, the `agent_runs` row is written `succeeded`, and the project advances — no re-raise, no failure marking, so the runner's failure machinery (retry-once → park `failed`) never sees stage errors; and when `integration_client.call_stage` returns `None` there is still no log line at all. Closing this needs a policy decision (fail the job vs stub-with-failure-record) that was not in this pass's scope. Status: remains open as a major, narrowed. (Related doc debt: `docs/ROUND4_AUTOMATION.md` §3's “silent” wording is now outdated for the exception path only.)

### R4-003 closure — RESOLVED (2026-10-23, closure pass, after the fix pass)

- **Policy implemented:** execution stage handlers no longer substitute stub artefacts when a real stage raises or returns `None`, unless the job explicitly selects stub mode. A real-stage failure writes a `failed` `agent_runs` row and raises `handlers.StageFailedError`, so the runner's failure policy applies — retry exactly once, then park the project `failed` (`error_summary` + `project.failed` event). Stub-selected jobs and the offline produce path (`app.integration.produce_video`) are unchanged.
- **Where:** `backend/app/execution/handlers.py` — `StageFailedError`, the `_stub_mode_selected` gate, and `_write_failed_agent_run` (which also populates the dedicated `agent_runs.error` column); the new contract is also documented in the `integration_client` docstring in `backend/app/execution/integration_client.py`.
- **Provenance, precisely:** top-level `"stub": True` marks handler-substituted artefacts on explicitly stub-selected jobs; stub-origin results returned *by* a stage keep their provenance in `result["provenance"]` (e.g. `llm_path`) and the `agent_runs` `output_ref` — a real stage returning a deterministic bundle is not re-stamped as a handler stub.
- **Selection seam:** `payload["stub_mode"]` is the operative selection seam today (project rows carry no stub config column; no production caller selects stub mode — by design of this closure).
- **Verification evidence (closure verification worker, settled tree, `TMPDIR=~/workspace/youtube-agent-app/.tmp-yta`, `--basetemp ~/workspace/youtube-agent-app/.tmp-yta/bt-w3`):** affected slices — `tests/test_r4003_stage_failure_policy.py`, `tests/test_execution_handlers.py`, `tests/test_execution_lifecycle.py`, `tests/test_execution_failure_policy.py`, `tests/test_execution_runner.py`, `tests/test_execution_scheduler.py`, `tests/test_stop_all_live.py`, `tests/integration/test_stage_bridge.py`, `tests/test_budget_release_on_failure.py` — **90 passed**. Full backend suite (`pytest tests/ -q`): **1229 passed, 1 failed, 8 skipped** — the sole failure is the known R4-007 in-suite flake (`tests/test_tts_calibration_planning.py::test_plan_explicit_override_plans_at_its_own_bucket`, the override-warning test; green standalone). Regression file: `backend/tests/test_r4003_stage_failure_policy.py`. An earlier slice run that straddled the in-flight test-pin updates and the `agent_runs.error` micro-correction (5 failed / 85 passed) was superseded by the clean re-run above on the stable tree.

### D3 guardrail taboo screening — RESOLVED (defect had no numbered entry; recorded here)

New `backend/app/execution/guardrail_gate.py` (mirrors `budget_gate.py`) delegating matching verbatim to the existing `prompt_matches_taboo` (`backend/app/pipeline/scenes/safety.py:372`); machine-readable reason code `guardrail_taboo`. Screening at **both** automation entry points: `SchedulerTick._materialise` (after topic selection, before project INSERT — transaction rolls back, queued topic not consumed, visible `schedule_runs` record with reason + matched term, `guardrail.blocked` event) and **fail-closed** at the research stage entry in `handlers.py` (deliberately *not* inside the budget helper's fail-open try/except — parks the project at Gate A with a stamped `guardrail_taboo` approval, zero artefacts/cost), plus screening of the resolved topic for `auto:` projects. An approved `guardrail_taboo` approval is the explicit human override on resume, mirroring budget semantics. Evidence: reproduction file `backend/tests/test_guardrails_automation.py` **5 passed with unmodified assertions** (was 2 failed / 3 passed); new `backend/tests/test_guardrail_gate.py` **6 passed** including the clean-topic control flowing through both points; affected execution batch 104 passed; schedules/budget/channel API slices 28 passed. Scope note: topics invented *inside* later stages are not screened by this fix.

### Other fix-pass items (no prior entries)

- **Migration assertion** (`test_execution_approvals.py:224`): `0002` → `0003` (`backend/migrations/versions/0003_budget_reservations.py` verified present, `down_revision = "0002"`). File: 1 failed / 8 passed → **9 passed**.
- **Lifecycle seam isolation:** `test_execution_lifecycle.py::test_auto_project_runs_full_chain` given the same `app.integration` seam isolation as `test_stop_all_live.py`, all chain assertions verbatim. File: 1 failed / 6 passed in 37.34 s → **7 passed in 9.75 s** (the test itself 3.08 s).
- **Budget failure-release (Team E Part 1 gap):** `release_budget_for_project` wired into the runner's permanent-failure paths — new guarded helper (`runner.py:167`) called from the retries-exhausted `dead` branch (`runner.py:451`) and the startup recovery-park sweep (`runner.py:213–226`), `reason="run_failed"`, store-idempotent (only an `active` row releases; settled spend stays committed). **Pause proven NOT to release** (separate code paths + a dedicated retention test). New `backend/tests/test_budget_release_on_failure.py`: **3 passed**; budget + runner/failure slices **97 passed**; full execution slice **106 passed**.
- **`.gitignore` `render/` hazard:** the bare `render/` rule replaced with anchored output-only patterns (`/render/`, `outputs/**/render/`, `backend/outputs/**/render/`, `**/.tmp*/**/render/`). The source package `backend/app/pipeline/render/` (7 `.py` files) is now check-ignore-clean and shows as addable in `git status`; all real pack render dirs (round3/round4 outputs, backend/outputs, .tmp roots) remain ignored. Caveat: a hypothetical future pack render dir outside those roots is no longer blanket-covered — none exists in the tree.
- **`models.yaml` (N-2 hazard): CLOSED** — confirmed git-clean vs HEAD (`git status --porcelain` and `git diff HEAD` both empty); the acceptor-era pollution was already restored (sha256-verified by the owning team). Nothing to restore; `test_content_live` passes in the final suite.
- **EPERM litter (N-3 leftovers):** `backend/5kkvtloh`, `backend/fyq789sl`, `backend/oxjwfitx`, repo-root `nm2vc2_h`, repo-root `wyy3xxy3` — each re-verified 0-byte immediately before deletion, absence confirmed after. `pyw3` not found in root or `backend/` (checked twice). Nothing else deleted.
- **N-1: CLOSED** by the re-pin under R4-002 above.

### Still open after the fix pass

- **R4-003** — handler-layer remainder (above). **Closed afterwards by the R4-003 closure pass — see the closure addendum under R4-003 above; no longer open.**
- **R4-004** — TTS proof stock rerun with the Pixabay key supplied (minor; team-disclosed).
- **R4-005** — production QC should emit the transliteration-aware fair subtitle metric instead of the raw cross-script 0.0 (minor). **Resolved in Round 5** — production QC now emits BOTH scores, each named; strict still decides `passed`, fair is report-only (see the R4-005 detailed entry).
- **R4-006** — critical-token (names/numbers/dates) fidelity limitation; no fix proposed (minor, documented).
- **R4-007** — monitor. In the coordinator's settled-tree full-suite run (below) this test failed once in-suite, while passing: standalone file **32/32**, in two mixed fix-pass slices **37/37 ×2**, and in the TTS worker's 349-test slice. Classified **environmental / test-ordering** per this entry's own re-open rule (no failing *clean* run exists); the precise interfering neighbour was not isolated.

### Final verification (fix-pass coordinator's own runs, settled tree)

| Suite | Result | Reference |
|---|---|---|
| Backend (`backend/tests/`) | **1222 passed, 1 failed, 8 skipped** (340.36 s) | Round 3 close: 856 passed / 8 skipped · pre-fix-pass full run: 1195 passed / 8 failed / 8 skipped |
| Root (`tests/`) | **44 passed, 1 skipped** (33.00 s) | Baseline 44 passed / 1 skipped — unchanged |
| UI | `npm run typecheck` + `npm run build` green | green |

The single backend failure is `test_tts_calibration_planning.py::test_plan_explicit_override_plans_at_its_own_bucket` (R4-007) — classification **environmental / test-ordering, not a code regression**: the planning code and its override WARNING behave correctly in every isolated and sliced run listed above. Every other previously-classified failure (guardrail reproduction ×2, migration assertion, short-scenes pin, lifecycle chain, `test_content_live` model set, automation/budget slices) is green in this run.

---

## Notes for the coordinator

- **Publish is still fake-provider everywhere** (automation run 1/2, TTS proof, Gate B replay): `fake_provider_upload_simulated` / namespaced `simulated:fake-video-1:<project_id>` records, `projects.youtube_video_id`/`published_at` left NULL by the finaliser's provenance rule (`docs/ROUND4_AUTOMATION.md` §7). No real YouTube upload was attempted in Round 4 — consistent with Rounds 2–3 and with the still-pending interactive channel sign-in.
- **LLM tier substitution is disclosed everywhere it occurred:** all live Gemini work this round (automation run 2, TTS proof runs 1–2) ran on `gemini-3.1-flash-lite` via a substituted registry (`outputs/round4/automation/models_substituted_flash_lite.yaml`; TTS proof-local registry), because the registry's T1/T2 Gemini models return HTTP 429 on this key (Round 3 precedent). Costs were accounted at flash-lite's real prices (TTS proof: $0.008053 run 1 / $0.009839 run 2).
- **Environment fault — intermittent EPERM on new-file writes (not attributed to any team; full entry N-3):** during this round `/tmp` was reset at least once (~00:37 UTC, VM uptime confirms a reboot), several workers observed filesystem/WAL write anomalies (0-byte DB files, a 3-byte `app.db-shm`, transient `disk I/O error` on sqlite reads), and this worker reproduced the EPERM fault live at ~01:57 UTC (writes refused on `/tmp`, workspace and home despite root + `W_OK`; cleared within minutes). Long renders also ran under heavy parallel-suite load. R4-001's run 2/3 failures occurred inside this environment; the automation team should re-run the proof in a quieter, EPERM-free window before the failure is attributed to the harness alone — but until a clean run's artifacts exist, the item stays FAIL. No write-related failure anywhere in this file has been logged as a code bug without a clean re-check.
- Scratch artefacts intentionally **not** committed by this worker: `.tmp-yta/` (pytest basetemps, proof work copies).
