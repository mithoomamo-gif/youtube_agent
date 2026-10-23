# YouTube Agent — Test Suite

Test infrastructure for the YouTube Agent (Windows desktop app: Tauri shell,
Python 3.12 FastAPI sidecar, LangGraph orchestration, SQLite state, FFmpeg
rendering). The governing spec is roadmap **section 17 — Testing & QA
Strategy** (`~/workspace/youtube-windows-agent/roadmap-sections/17_testing_qa.md`)
and **MASTER_ROADMAP Part 12 §12.7**.

> **The one rule above all:** *a gate never seen to fail is assumed broken.*
> Every gate (QC, similarity, fact-check, policy, secret scan, eval
> thresholds) must periodically prove it can still say NO — via
> fault-injection and planted-error corpora. When you add a gate, add the
> positive-control test that proves it fires (see the planted-secret tests
> in `contract/test_no_secrets_in_repo.py` for the pattern).

## How to run

From the repo root (`~/workspace/youtube-agent-app/`):

```bash
python -m pytest tests/ -q            # default run: everything except `live`
python -m pytest tests/unit -q        # just the always-on L1 smoke/unit tests
python -m pytest tests/ -v            # see individual skip reasons
python -m pytest tests/ -rs           # list every skip + its reason
```

The suite must import and collect with **only pytest installed**. Backend
modules that are still landing are probed with `pytest.importorskip` (see
the `requires_module` fixture in `conftest.py`) and report as *skips*, not
errors. Optional plugins are never a collection-time dependency:

- **pytest-asyncio** — if installed, async tests run with
  `asyncio_mode = auto`. The setting is a commented line in `pytest.ini`;
  uncomment it only once the plugin is in the dev requirements, otherwise
  every run warns about an unknown config key. Absence of the plugin must
  never break collection — guard async-only modules with `importorskip`.
- **pytest-cov** — coverage is a CI concern; run locally with
  `python -m pytest tests/ --cov=backend` when installed.

### Marker selection

Markers are registered in `tests/pytest.ini` (`--strict-markers` is on, so
an unregistered marker is an error). Examples:

```bash
python -m pytest tests/ -m unit                 # L1 unit only
python -m pytest tests/ -m contract             # API / security contracts
python -m pytest tests/ -m integration          # L4 hermetic pipeline
python -m pytest tests/ -m ffmpeg               # needs a local ffmpeg binary
python -m pytest tests/ -m "not slow"           # quick loop, skip soak/long tests
python -m pytest tests/ -m golden               # golden-dataset / golden-video evals
python -m pytest tests/ -m live                 # REAL providers — see gating below
```

| Marker | Meaning |
|---|---|
| `unit` | L1 unit tests — pure, hermetic, fast; every PR. |
| `contract` | L1/L2/L9 contract tests — API shapes, adapter contracts, security scans. |
| `integration` | L4 pipeline integration — hermetic DAG runs, providers faked. |
| `ffmpeg` | Requires a local `ffmpeg` binary; **auto-skipped** when absent (see `conftest.py`). |
| `live` | Needs real provider API keys + network. **Excluded by default.** |
| `golden` | L3/L5 golden-dataset evals and golden-video regression (statistical thresholds). |
| `slow` | Soak / long-render / nightly-scale tests. |

### How `live` and `golden` tests are gated

- **`live`** tests are excluded by default via
  `addopts = -m "not live"` in `pytest.ini`. They talk to real provider
  APIs (YouTube sandbox channel, LLM/TTS providers) with real keys, spend
  real quota/money, and belong to the weekly sandbox smoke and release
  gates — never to a PR run. Run them deliberately, on the sandbox
  project only: `python -m pytest tests/ -m live`. An explicit `-m` on
  the command line overrides the default exclusion. Only `live` tests see
  real credentials: the autouse fixture in `conftest.py` strips every
  `*_API_KEY` / `*_SECRET` / `*_TOKEN`-style environment variable for all
  other tests, so a developer's keys cannot leak into a hermetic run.
- **`golden`** tests compare against *blessed* reference data with
  **tolerance bands, never exact equality** (encoders and TTS are not
  bit-deterministic). The bars from §12.7 / section 17:
  - Video frames: SSIM **≥ 0.98** mean per matched frame (any frame < 0.90
    is investigated); VMAF regression gate: mean drop **> 2 points** blocks.
  - Audio: loudness **−14 LUFS ± 1**, true peak **≤ −1 dBTP**, waveform
    envelope correlation ≥ 0.95 on hermetic runs.
  - Duration ± 0.5 s; subtitle cue offset ≤ 200 ms; thumbnail text layer
    exact (backgrounds are stochastic and are *not* pixel-gated).
  - Goldens change **only** via a dedicated "bless" PR with side-by-side
    diffs and a written reason — never in the same PR as the code change
    that caused the diff. An unexplained golden diff is a regression
    until proven intentional.

Golden datasets and fixtures live in `tests/golden/` and `tests/fixtures/`
(maintained separately from this infrastructure).

## Layer map (roadmap section 17)

The product has two kinds of correctness: the **deterministic** half
(FFmpeg graphs, queues, quota math, upload idempotency, file QC) gets
classic exact assertions; the **probabilistic** half (the LLM agents)
gets statistical testing — golden datasets, scored rubrics, regression
thresholds, never exact string matches.

| Layer | What | Where it lives / will live | Cadence | Gate |
|---|---|---|---|---|
| L1 | Unit & contract — pure functions, Pydantic I/O schemas for all agents, quota math, slug/title rules, licence ledger, niche-profile builder (incl. **Other**) | `tests/unit/`, `tests/contract/` | Every PR | Hard fail |
| L2 | Provider adapter contracts — one adapter per external API vs recorded responses (VCR cassettes); sandbox keys weekly | `tests/contract/` (+ cassettes under fixtures) | PR (recorded) + weekly live | Hard fail |
| L3 | Agent prompt evals — every agent's prompt + model version scored on golden datasets (≥30 cases/agent at MVP, 50+ by v1); DeepEval/promptfoo; LLM-as-judge must be a *different, stronger* model, calibrated at Cohen's κ ≥ 0.7 | `tests/golden/` (datasets as data), marked `golden` | PR smoke subset, nightly full | Statistical threshold |
| L4 | Pipeline integration — full DAG on frozen fixture projects (F-EN-BASIC, F-UR-BASIC, F-MIX-CODESWITCH, F-OTHER-CUSTOM, F-NOSTOCK, F-LONG, F-FAIL-*), all providers **faked**; asserts artefact completeness, DAG/gate ordering, checkpoint/resume, upload idempotency (exactly one `videos.insert`), quota accounting | `tests/integration/`, marked `integration` | PR (short), nightly (full) | Hard fail |
| L5 | Golden-video regression — end-to-end output vs blessed references at frame/audio/subtitle level (tolerances above) | marked `golden` | Nightly + release | Tolerance bands |
| L6 | Render QC automation — the in-app QC gate itself tested against a fault-injection corpus of deliberately broken videos (≥25 cases by v1). Bars: **100% recall** on safety/policy must-fails, ≥98% on technical must-fails, ≤2% false-FAIL on clean goldens | fault corpus + QC tests (marked `ffmpeg` where decoding) | PR + nightly | Detection-recall targets |
| L7 | Desktop/UI — Tauri shell, WebView UI, installer, tray, auto-update on clean Windows VMs (Playwright / Tauri WebDriver) | Outside this Python suite | Nightly + release | Hard fail |
| L8 | Performance / soak / resilience — concurrency pools, 30-video soak, kill/resume chaos; kill switch halts all pools ≤ 10 s | marked `slow` (+ `integration`) | Weekly + release | Threshold |
| L9 | Security & privacy — secret storage, loopback token, log redaction, path handling; grep-based secret scans (see `contract/test_no_secrets_in_repo.py`) | `tests/contract/` | Every PR | Hard fail |
| L10 | Production (online) evals — sampled real runs re-scored post-release; drift + cost monitoring | Post-release jobs, sampled `live`/`golden` runs | Continuous | Alert, not block |

**Environments:** (1) *Hermetic* — no network, providers faked,
deterministic seeds, temperature 0; the default for L1–L6 and for this
suite. (2) *Sandbox* — real APIs, test keys, a dedicated unlisted test
channel (`live` marker). (3) *Clean Windows VM* — L7 installer/first-run.

**CI gating reminders (section 17):** prompt/model/provider changes are
code changes and run the full affected eval suites. **No retry-to-pass** —
a flaky case is quarantined with a ticket, never re-run until green.
Deterministic regressions block merge; judge-score drops > 2% relative or
cost/run rises > 25% without a recorded quality gain also block.

## Shared fixtures (`tests/conftest.py`)

| Fixture | What it gives you |
|---|---|
| `repo_root` / `tests_dir` / `roadmap_root` | Session-scoped `Path`s (repo root, `tests/`, sibling roadmap tree). |
| `tmp_project_dir` / `tmp_project` | Factory / single throwaway project dir with `assets/ renders/ audio/ subtitles/` subdirs. |
| `fake_clock` / `fake_clock_unpatched` | Deterministic clock (starts 2026-01-01T00:00:00Z); the patched variant also replaces `time.time`/`time.monotonic` for the test. |
| `clean_env` (+ autouse redaction) | Environment with all `*_API_KEY`-style variables stripped (non-`live` tests only). |
| `ffmpeg_path` / `ffprobe_path` / `ffmpeg_available` | Locate the binaries / report availability without failing. |
| `require_ffmpeg` / `require_ffprobe` | Skip the test when the binary is absent. (The `ffmpeg` *marker* does the same automatically via `pytest_runtest_setup`.) |
| `requires_module` | `requires_module("backend.jobs")` → the module, or a clean skip while it is still landing. |

## Layout

```
tests/
  pytest.ini        # config: testpaths, markers, addopts (lives here, not repo root)
  conftest.py       # shared fixtures — stdlib + pytest only
  README.md         # this file
  unit/             # L1 — incl. test_smoke.py (must always pass)
  contract/         # L1/L2/L9 — API contract, no-secrets scan
  integration/      # L4 — hermetic pipeline skeletons
  fixtures/         # frozen fixture projects & cassettes (separate owner)
  golden/           # blessed golden datasets & references (separate owner)
```
