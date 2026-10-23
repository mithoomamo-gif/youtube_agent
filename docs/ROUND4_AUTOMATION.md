# Round 4 — Automation: schedule tick → unattended video

> How the Round 4 automation path actually works, as implemented in the
> code at the time of writing. Every claim below cites the file that
> implements it. This document describes only what the code does today —
> including its honest limitations (stub fallbacks, simulated publish).

## 1. The path at a glance

```
schedules row (due, enabled)
  -> SchedulerTick.tick()                     app/execution/scheduler.py
  -> projects row + schedule_runs row
  -> create_project_dag()                     app/execution/dag.py
       10 jobs: stage.research -> stage.script -> gate.a ->
       stage.voice -> stage.visuals -> stage.render ->
       stage.thumbnail -> stage.qc -> stage.package -> gate.b
  -> JobRunner claim/execute loop             app/execution/runner.py
  -> handlers (stage + gate)                  app/execution/handlers.py
  -> integration_client.call_stage()          app/execution/integration_client.py
  -> app.integration.run_<stage>()            app/integration/__init__.py
  -> stage bridge                             app/integration/stage_bridge.py
  -> real stage machinery (content / voice / visuals / assemble / publish)
  -> gate.b completion hook                   app/execution/service.py
  -> publish finaliser (simulated publish)    app/execution/publish_finaliser.py
  -> projects.status = 'ready' + one publish_records row
```

The whole loop is owned by `ExecutionService`
(`backend/app/execution/service.py`): it runs the `JobRunner` as an
asyncio task and a scheduler loop that calls `SchedulerTick.tick()`
roughly every 60 s (first tick after ~5 s), each tick in a worker thread
(`asyncio.to_thread`) because the tick is synchronous SQLite work.
`app.main` attaches the service; autostart is governed by `YTA_EXECUTION`
/ the `YTA_EXECUTION_AUTOSTART=0` kill-switch (see the `attach()`
docstring in `service.py`).

The Schedules UI (`src/screens/Schedules.tsx`,
`src/api/queries/schedules.ts`) is bound to `backend/app/routers/
schedules.py`. Its **Run now** action (`POST /schedules/{id}/run-now`,
202) does not use a separate production path: the router marks the
schedule due and runs one real `SchedulerTick.tick()` synchronously, so
a manual run-now and an unattended tick materialise projects in exactly
the same way. A paused (disabled) schedule rejects run-now with 409
`schedule_disabled`.

## 2. How a schedule tick becomes a project

`SchedulerTick.tick(now_ms)` (`backend/app/execution/scheduler.py`):

* Selects enabled, non-deleted schedules with `next_run_at <= now`,
  oldest due first.
* **Catch-up policy `catch_up_once`:** if a schedule is several slots
  behind (app closed, machine asleep), the tick walks every missed
  occurrence but materialises exactly **one** project, for the latest
  missed slot. Collapsed slots are counted as `missed_collapsed` in the
  tick summary and in the `schedule_runs.detail_json`.
* **Topic selection** by `schedules.topic_mode`:
  * `queue` — the highest-priority `pending` row of `topic_queue` for
    the schedule's channel/niche (a NULL `channel_id` counts as
    channel-agnostic); the row is marked `used` with
    `used_in_project_id` pointing at the new project. An empty queue
    fails the slot rather than inventing a topic.
  * `fixed_list` — rotation over `cadence_json["topics"]`, indexed by
    the count of prior **`created`** schedule runs only (the R2-004 fix:
    failed/skipped runs consume no topic and do not shift the rotation).
  * `auto` — stores the sentinel topic `"auto:<niche_profile_id>"` in
    `projects.topic` (the column is NOT NULL); the research stage of the
    stage bridge resolves it (see §3).
* Target words are derived from the niche profile's
  `words_per_minute` × the schedule's `target_minutes`.
* The project row is created with `topic_source='schedule'`,
  `status='in_production'`, `current_phase='research'`, and the
  schedule's `publish_mode` (default `review_first`).
* **Failure handling:** a missing channel/niche row, an empty topic
  queue, or an empty fixed list does not abort the tick. A
  `schedule_runs` row with `status='failed'` and a `reason` is written,
  the schedule is advanced exactly as on success (so a broken schedule
  cannot fail again on every 60 s tick), and the tick continues with the
  remaining schedules.
* After the project + run rows commit, the production DAG is built via
  `create_project_dag()`; if the DAG module is unavailable or raises,
  the scheduler falls back to enqueueing a single `pipeline.start` job
  for the project. A `schedule.materialised` event is emitted per
  created project (guarded — a broken event hub never breaks scheduling).

`create_project_dag()` (`backend/app/execution/dag.py`) enqueues the
10-job linear chain listed in §1. The queue supports a single
`depends_on` per job, so each job depends on the previous one; a job
becomes claimable only after its dependency finishes successfully.
Every job is enqueued with `idempotency_key=f"dag:{project_id}:{kind}"`
and `max_attempts` from the failure policy (§5), so re-creating a DAG
returns the existing job ids instead of duplicating the chain.

## 3. The stage bridge: DAG stages → real production machinery

Until Round 4, `app.integration` exported no `run_<stage>` functions,
so `integration_client.call_stage()` returned `None` for every stage
and every scheduled stage silently used the deterministic stub in
`handlers.py`. `backend/app/integration/stage_bridge.py` closes that
gap; `backend/app/integration/__init__.py` re-exports its eight
non-gate `run_*` functions, which is what `call_stage` dispatches to.

| DAG stage | Real machinery (all in `backend/app/integration/`) |
|---|---|
| `research` | Topic resolution (`auto:` sentinel → deterministic ideation from the resolved profile, seeded by project id) + a research brief built from `content.py` helpers (`_keywords` / `_plan_sections` / `_budgets`) |
| `script` | `content.build_content_live` driven through the run-loop owner (`app/integration/run_loop.py`): real Gemini when a key resolves (provenance `gemini-live`), otherwise the content layer's own provenance-marked deterministic bundle (`deterministic-stub`) |
| `voice` | `voice_stage.synthesize_voice` (real edge-tts, with the voice stage's own per-chapter fallback + chapter cache) |
| `visuals` | `visuals_stage.plan_and_generate_visuals` (stock-first Pixabay, local-plate fallback) |
| `render` | `assemble_stage.assemble_project` (segment render + real-Whisper subtitles + thumbnails + technical QC) — the render/thumbnail/qc DAG stages share **one** assemble run |
| `thumbnail` | The thumbnail variants of that same assemble run |
| `qc` | The technical-QC verdict of that same assemble run, verbatim |
| `package` | `publish_stage.finalize_publish` (Gate B tokens, frozen manifest, fake-provider workflow) + the pack writers (licence-ledger CSV + `report.md`) |

Bridge contract and state:

* Each `run_<stage>(project, payload)` receives the `projects` row as a
  plain dict plus the job payload, and returns a result dict carrying
  an `artefact` dict, a `file_path` where a real file exists, cost in
  `cost_usd`/`cost_micros` when the stage actually incurred it, and
  mandatory `provenance` — what actually ran (LLM path, TTS path,
  stock-vs-plate visuals, Whisper-vs-alignment subtitles, publish
  provider). The content stage's own provenance string is passed
  through verbatim; the bridge does not relabel anything as "real".
* The project row's `niche_profile_id` is a DB row id, while the
  pipeline works with `NicheProfile` objects; `resolve_profile_for_
  project()` resolves payload/project hints → the DB row (builtin key,
  or the OTHER builder for `source='other'` rows, or row fields over a
  base profile) → the id itself as a builtin key → an honestly recorded
  `facts` fallback.
* Each project gets a working directory `<base>/<project_id>/` built by
  `models.make_context()` (`content/`, `audio/`, `visuals/`,
  `render/`, `pack/`). `base` resolves from, in order:
  `payload["base_dir"]` / `project["base_dir"]`, `$YTA_PROJECTS_DIR`,
  `$YTA_DATA_DIR/projects`, the app settings data dir, else
  `<cwd>/outputs/projects`.
* **Checkpoints / idempotency:** every stage persists its output as a
  JSON checkpoint inside that layout (`content/research.json`,
  `content/bundle.json`, `audio/voice.json`, `visuals/visuals.json`,
  `render/assemble.json`, `render/qc.json`, `pack/package.json`) plus a
  `bridge_state.json` index. Re-running a stage whose checkpoint (and
  headline file, where one exists) is present returns the cached result
  with `cached: True` and does not redo the stage call — stages are
  resumable after a crash and safe under job retries.
* **Loop ownership:** `produce.py` / the bridge never let stage modules
  create their own event loops. `run_loop.run_coroutine()` runs a
  coroutine on the produce run's single loop when one is active, opens
  a one-call loop at an outer boundary, or drives the coroutine on a
  worker thread (with its own loop) when a foreign loop is already
  running in the calling thread — which is the DAG handler case, since
  handlers are async.
* **Stub only on explicit selection; real-stage failures fail
  (handler policy, not bridge policy — R4-003 closure):** in
  `handlers.py`, the deterministic stub artefact (`stub: True`,
  `file_path` of the form `stub://…`) is substituted **only** when
  stub / offline mode was explicitly selected for the job
  (`_stub_mode_selected`: payload / project / config `stub_mode` /
  `offline`; `payload["stub_mode"]` is the operative selection seam
  today — project rows carry no stub config column, and no
  production caller selects stub mode, by design of the closure).
  Otherwise, if `call_stage` raises any exception *or* returns
  `None` / a non-dict result, the handler logs the failure, writes a
  `failed` `agent_runs` row, and raises `StageFailedError`, so the
  runner's failure policy applies (retry exactly once, then park the
  project `failed` — see §5). No stub content, artefact row, project
  advance or stage cost is recorded for a failed attempt. Provenance:
  top-level `stub: True` marks handler-substituted artefacts on
  explicitly stub-selected jobs; stub-origin results returned *by* a
  stage keep their provenance in `result["provenance"]` (e.g.
  `llm_path`) and the `agent_runs` output_ref, and are not re-stamped
  as handler stubs. Auditors should still check a run's stub flags —
  in the job results / `agent_runs.output_ref_json` (the Round 4
  proof harness does; see §8). The offline produce path
  (`app.integration.produce_video`) is unchanged.

## 4. Gates: review-first vs full-auto

Gate behaviour is implemented in three layers: the DAG gate handlers
(`handlers.py`), the approval bridge (`gates_bridge.py`), and — for
Gate B's QC dimension — the conditional-pass policy
(`pipeline/gates/conditional.py`, applied by
`pipeline/gates/aggregator.py::evaluate_gate_b` inside
`publish_stage.finalize_publish`).

### 4.1 DAG gate handlers (Gate A and Gate B)

For a project whose `publish_mode` is:

* **`auto` (full-auto):** the gate handler immediately returns
  `{decision: 'auto_approved'}` and records
  `gate_status_json[gate_a|gate_b] = 'auto_approved'`. No approval row
  is created and no human is involved.
* **`review_first`:** with no recorded decision, the handler delegates
  to `gates_bridge.ensure_approval()` (which creates the single pending
  `approvals` row for `(project_id, gate)`, parks the project at
  `status='awaiting_review'`, and emits `gate.pending`) and then raises
  `GatePending`. The runner requeues the gate job with a 15 s delay
  **without consuming an attempt** (see §5) and polls again. When a
  human decides, the approvals router calls
  `gates_bridge.apply_decision()`, which merges
  `payload['approval'] = {decision, approval_id, reason_code}` into the
  gate job and — for `approved` — zeroes its `run_after` so the runner
  picks it up immediately. On the next poll the handler returns
  `{decision: 'approved'}`; a `rejected` decision raises `GateRejected`
  (`error_code='gate_rejected'`), which the runner treats as a
  permanent, non-retryable failure, and the bridge has already parked
  the project as `blocked`.
* `ensure_approval()` is idempotent: repeat polls return the existing
  pending approval id and do not re-emit `gate.pending`.

The same `GatePending` mechanism parks a project for **budget**
reasons: at `pipeline.start` and at the first DAG stage (`research`),
`budget_gate.check_budget_for_project()` (now full per-video + daily +
monthly enforcement with durable reservations in
`execution/budget_reservations.py`, migration `0003`) parks an over-cap
project at Gate A with a `budget_pending` approval and a
`budget.warning` event instead of letting it proceed. Approving that
approval is the user's explicit override and bypasses the check for
that project on later starts.

### 4.2 Gate B conditional pass (QC WARN with minor flags only)

Implemented in `backend/app/pipeline/gates/conditional.py` (policy
layer only — QC thresholds and detection in `pipeline/qc/technical.py`
are unchanged):

* QC `PASS` → pass.
* QC `WARN` whose flags are **all** in the versioned minor-flag class
  `MINOR_QC_FLAGS` (policy v1.0: `BLACK_SEGMENT_BRIEF`,
  `FREEZE_DECLARED_STILL`) → **conditional**. A conditional is *not* a
  pass.
* QC `WARN` with any non-minor flag, or with no flags at all, and any
  `BLOCK`/`FAIL`/unknown verdict → hard block. The policy fails closed.

What Gate B may do with a conditional, by mode:

* `review_first` + no recorded approval →
  `conditional_pending_approval`, not allowed.
* `review_first` + recorded human approval → `conditional_approved`,
  allowed to proceed (via the approval, not as a pass).
* `auto` (full-auto) → `blocked_full_auto`, **always blocked regardless
  of any approval**: full-auto never accepts a conditional.
* Unknown mode or classification → block (fail closed).

In `publish_stage.finalize_publish()` this is applied on top of the
strict Gate B verification: the manifest's `gate_b` block records
`outcome` (`pass` | `conditional` | `block`), `mode`,
`has_recorded_approval`, the `conditional` detail (QC flags,
minor/non-minor partition, policy version, classification and decision
reasons) and `strict_passed`. Every conditional minting, approval,
release and block is appended to an append-only JSONL audit log
(`<pack_dir>/gate_b_audit.jsonl` by default) by
`pipeline/gates/audit.py::record_gate_b_event`, with the manifest hash,
mode, outcome, QC flags and the stock asset IDs taken from the licence
ledger as footage provenance.

## 5. Failure & retry policy

The single definition lives in `backend/app/execution/failure_policy.py`
and is consumed by `runner.py` and `dag.py`:

1. **Retry exactly once.** DAG jobs are enqueued with
   `max_attempts = 2` (`STAGE_MAX_ATTEMPTS`: one initial attempt + one
   automatic retry). The runner additionally refuses to mark a DAG job
   retryable once its persisted `jobs.attempts` reaches 2, so even a
   legacy row created with the queue default of 3 cannot be retried
   twice. Attempt counts live in `jobs.attempts` (incremented atomically
   by `queue.claim`), never in memory — a restart cannot reset the
   budget into an infinite loop. Non-DAG kinds defer to the queue's own
   `max_attempts`, backoff (`base · 2^(attempts-1)` + deterministic
   jitter) and dead-letter semantics.
2. **Parking is not failure.** `GatePending` (human gates and the
   budget park, which is raised as the same exception) is handled by
   the runner's dedicated requeue path, which refunds the claim's
   attempt. It never reaches the failure policy and never consumes the
   retry. `GateRejected` is a decision, not a transient error: terminal,
   no retry, and the project is not re-parked as failed (the gates
   bridge has already parked it as `blocked`).
3. **Second failure parks the project terminally.** When the queue
   dead-letters a DAG job, `park_project_failed()`:
   * sets `projects.status='failed'` and persists
     `projects.error_summary` (stage + kind + attempt count + error);
   * dead-letters every still-queued DAG job of the project with
     `error_code='dependency_failed'`, so no downstream job is left
     dangling or claimable;
   * emits `project.failed` both persisted (an `event_log` journal row)
     and live (via the events object / WS hub) — persisted exactly once
     (the direct journal insert is skipped when a real `WSHub`, whose
     publish already journals, is behind the events object).

   `failed` (a system failure nothing a human approves will resume) is
   deliberately distinct from `blocked` (the human-actionable state
   used by gate decisions). Human/terminal project states (`blocked`,
   `cancelled`, `published`, already `failed`) are never clobbered by
   parking.
4. **Crash / restart safety.** At runner startup,
   `failure_policy.recover_and_park()` runs the queue's existing lease
   sweep (`sweep_expired_leases`: requeue while attempts remain, else
   dead-letter with `error_code='lease_expired'`) and then parks any
   project whose DAG job is dead while the project is still in a live
   status — covering a crash between "job dead" and "project parked".
   The scan is idempotent.
5. **Scheduler level.** The scheduler's own failure recording (§2) is
   unchanged by this policy: a failed slot is recorded in
   `schedule_runs` and `next_run_at` advances exactly as on success, so
   a failed slot never re-materialises.

Separately, the execution layer honours the operator flags in
`settings` (`execution/flags.py`), re-read by the runner on every poll:
`pause_all` stops claiming (in-flight jobs finish); `stop_all` cancels
in-flight work and ends the loop.

## 6. The multi-channel model

Channel scoping is resolved in exactly one place:
`backend/app/execution/channel_context.py`. Stage handlers resolve the
context once per stage and pass it to integration stages as
`channel_context` on both the project and payload dicts; the resolution
is also recorded on stage results. (See also
`docs/ROUND4_CHANNEL_SCOPING.md`.)

Precedence, per project:

* **Niche:** the project's schedule's `niche_profile_id` wins, then the
  project's own `niche_profile_id`, then the channel's
  `default_niche_profile_id`. A schedule whose `channel_id` does **not**
  match the project's channel is ignored entirely (niche *and* budget) —
  a mismatched schedule is cross-talk, never a source of truth.
* **Voice:** `niche_profiles.default_voice_id`, with the niche's
  `voice_style` carried as the hint the voice stage falls back to.
  There is deliberately **no channel-level voice override**: no code
  reads or writes a voice key in `channels.config_json` (that column
  carries connect-state keys only), so none was invented.
* **Budgets (app controls):** monthly cap from
  `channels.monthly_budget_micros`; daily cost cap from the
  (channel-matching) schedule's `schedules.daily_cost_cap_micros`;
  daily upload cap from `channels.daily_upload_cap`. The resolved
  context also carries the niche's `words_per_minute` and the effective
  `publish_mode` (project's, else schedule's, else `review_first`).

**Per-channel usage view:** `GET /api/v1/usage/channels` (and
`/channels/{channel_id}`), implemented in `app/routers/usage.py` over
`channel_context.build_channel_usage_views()`, returns per channel:
month-to-date spend attributed strictly through
`project_id → projects.channel_id` (`SUM(agent_runs.cost_micros)` for
the current UTC calendar month), month-to-date `api_usage` provider
costs reported **separately** (never added to the spend figure, so
provider calls are never double-counted), lifetime
`SUM(projects.total_cost_micros)`, the monthly cap and remaining
headroom, the schedules' daily caps, and project counts by status.
Missing tables / no data degrade to honest zeros.

> **YouTube API quota is NOT per-channel.** Quota is allocated per
> Google Cloud project, so all channels in this app share the single
> GCP project's quota — the app-level buckets exposed since Round 3
> (`GET /api/v1/usage/quota`: uploads / search / shared, resetting at
> midnight Pacific). The per-channel caps above are budget and
> scheduling controls enforced by the app itself; they do not create,
> reserve, or partition any YouTube API quota.

## 7. Publish finalisation and the simulated-publish provenance rule

The DAG ends at `gate.b`; nothing after the gate moved a scheduled
project into a publish state until `backend/app/execution/
publish_finaliser.py`. `ExecutionService` wraps the registry's `gate.b`
handler (`service._wrap_gate_b_handler`): finalisation hooks gate
*completion* (the handler's return value), never approval creation — so
in `review_first` mode nothing publishes until the human approval has
landed and `apply_decision` has resumed the gate job to completion,
while in `auto` mode finalisation happens unattended in the same run.
A `GatePending` return is untouched (the gate has not completed); a
`GateRejected` records a non-publication note and re-raises.

`finalise_after_gate_b()` then applies the provenance rule, which is a
hard rule of this module:

* The **only** provider ever used is the fake one from
  `publish_stage` (`provider='fake'`, `simulated=True`). Real YouTube
  upload remains out of scope until interactive channel sign-in.
* A simulated publish lands the project at **`status='ready'` — NOT
  `'published'`** (`'ready'` = production finished, Gate B approved,
  simulated publish recorded; ready for a real upload once a channel is
  connected). `projects.youtube_video_id` and `projects.published_at`
  are left **NULL**, and `publish_records.video_url` is left **NULL**:
  no real video exists, so no real id, URL or publish time is
  fabricated.
* The fake provider mints per-instance ids like `fake-video-1` (not
  globally unique, and easy to mistake for a real id), so the value
  stored in `publish_records.youtube_video_id` is namespaced:
  `simulated:<provider_video_id>:<project_id>` — unmistakably simulated
  and unique per project. The raw provider id is kept in the provenance
  payload (`projects.gate_status_json['publish']` and the
  `project.publish_simulated` event) for traceability.
* **Exactly once:** the finaliser checks for an existing
  `publish_records` row first and returns it
  (`already_finalised=True`); the record insert + project update run in
  one `BEGIN IMMEDIATE` transaction that re-checks, and
  `publish_records.project_id`, `upload_idempotency_key` and
  `youtube_video_id` are UNIQUE in the schema. The upload idempotency
  key is deterministic: the frozen manifest's own `idempotency_key`
  (pack tier) or `simulated-publish:<project_id>` (no-pack tier).
* **Pack tiers:** if a `master_video` artefact (or a stage job result)
  points at a real master file, the full `publish_stage.finalize_
  publish` flow runs (copyright/policy/QC Gate B tokens bound to the
  frozen manifest, then the fake-provider workflow); if *that* Gate B
  fails, no publish record is written and the project is marked
  `blocked` with the problems in `error_summary`. If there is no pack
  on disk (the DAG's stub stages produced only `stub://` artefacts, so
  there is no master to freeze a manifest against), the
  `FakeProvider` is driven directly (one resumable session, zero-byte
  completion) and the record is marked
  `pack_flow='fake_provider_direct_no_pack'` in its provenance. This is
  still only ever a simulation.
* **Blocked / failed gates are visible, never silent:** when Gate B
  does not approve, `record_non_publication()` merges
  `gate_status_json['publish'] = {state: 'not_published', reason}` and
  fills `error_summary` (when empty); the project's status is left
  exactly where the gate left it, except for the pack-tier Gate B
  failure above, which actively marks `blocked`.
* `projects.current_phase` is deliberately not touched by the
  finaliser — it is owned by the DAG (final phase `packaging`).

The monolith path (`produce.py` → `publish_stage.finalize_publish`)
follows the same honesty rule: a successful fake run is reported as
`publish_state='fake_provider_upload_simulated'`, never as a real
publication.

## 8. Proof artefacts

> **Worker-7 audit note (2026-10-23).** The 00:28 UTC audited run predates
> the stage-bridge fallback fix: only the research stage ran real; all
> downstream stages recorded `stub: true` and no media files were produced
> (root cause: the bridge's sync fallback cannot run inside the async DAG
> handler's event loop, and the handler swallows the exception silently —
> tracked as a defect for the coordinator). A re-run with a disclosed
> flash-lite registry substitution showed both channels' content stages
> running real (real scripts, edge-tts audio, Pixabay stock) and both
> projects reaching `status='ready'` with fake-provider simulated publish;
> its `run_log.json` had not been finalised when the outputs directory was
> reset by a later attempt (~01:40 UTC), so the re-run's machine-readable
> audit is pending a fresh proof run on the fixed tree.

The two-channel live proof harness is
`tools/round4_automation_proof.py`. It creates a throwaway app database
and outputs root under `outputs/round4/automation/` (the dev DB is
never touched), seeds two channels — A: the builtin `facts` niche;
B: an OTHER/custom niche ("Space Mysteries") built with the real
Other-niche builder and persisted with `source='other'` + `user_idea` —
one `publish_mode='auto'`, `target_minutes=1` schedule per channel, then
starts the real `ExecutionService` and makes zero further calls (no
manual approvals, no per-stage invocations). Provider keys are read
from the process environment only and are never written to disk; the
log records only their presence.

Artefacts under `outputs/round4/automation/`:

* `run_log.json` — the machine-readable record: seed data, tick
  timestamps, per-job/per-stage timings, gate outcomes, publish states
  + provenance, per-stage stub flags (`stub_audit` /
  `content_stage_stub_flags` / `all_content_stages_real`), and
  `human_interventions`.
* `channel-a-facts/` and `channel-b-space-mysteries/` — the copied pack
  artefacts per channel (video + pack files when produced).
* `projects/<project_id>/` — the stage-bridge working trees
  (checkpoints, `bridge_state.json`, pack dirs).
* `_proof_state/app.db` — the throwaway database the run executed
  against (jobs, `agent_runs`, approvals, `publish_records`,
  `schedule_runs` are all inspectable there).

An auditor should verify, mechanically: the videos exist in the
channel folders and `ffprobe` shows sane duration/streams; the
per-stage stub flags in `run_log.json` (a stage that fell back to the
handler stub is marked `stub: true` with a `stub://` file path — such a
run did **not** produce real media for that stage, whatever its job
status says); the gate outcomes and publish provenance
(`gate_status.publish`, `publish_records`); and
`human_interventions == 0`.
