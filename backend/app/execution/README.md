# app/execution — the durable execution layer (Round 2)

> **How to read this document.** This README is the integration
> reference for the execution package. Each module named below is the
> source of truth for its own section; this document records the
> *shared* contracts between them (and the one design decision — §6 —
> that no single module owns). The shared surface is pinned by
> `backend/tests/test_execution_contracts.py`; shared test helpers live
> in `backend/tests/execution_fixtures.py`. Where a detail is marked
> *(designed)* the implementing code had not landed when this was
> written and the contract is the agreement, not yet the description.

The execution layer turns *projects* (rows in `projects`) into *work*:
a chain of leased jobs in the SQLite `jobs` table, executed by pooled
asyncio tasks, pausing at human gates, emitting WebSocket events as it
goes, and surviving process crashes without losing or duplicating work.

It sits on top of — and does not replace — the Round 1 foundations:

| Foundation | Role |
|---|---|
| `app.queue.SQLiteJobQueue` | The durable queue: atomic claims, leases, heartbeats, retries, dead-lettering, lease sweep. The execution layer never hand-rolls queue SQL except where noted (§5.1). |
| `app.db` | One SQLite file, WAL, `BEGIN IMMEDIATE` write transactions, epoch-millisecond timestamps. Every execution component takes a `conn_factory` (fresh connection per call); no connection is shared between tasks. |
| `app.ws_hub.WSHub` | The WebSocket envelope/sequencing/journal machinery. Execution code publishes through `events.EventEmitter`, never by building envelopes itself. |
| `app.orchestration` (LangGraph supervisor, `gates.py`) | The deterministic Gate A/B/C *checks* (`can_pass_gate_a/b/c`) and the supervisor graph, for graph-executed projects. Execution owns *scheduling and durability*; orchestration owns *gate logic* (§6). |
| `app.integration` | The real per-stage production work (research → script → … → pack). Execution handlers call into it lazily — see §7. |

## 1. Module map

| Module | Owns |
|---|---|
| `runner.py` | `JobRunner` — the asyncio claim/execute loop; `GatePending` and `JobCancelled` control-flow exceptions. |
| `pools.py` | `POOL_LIMITS`, `KIND_TO_POOL`, `pool_for_kind`, `PoolManager` (asyncio-semaphore pool enforcement). Pure constants + one class; no DB I/O. |
| `flags.py` | `read_flags` / `set_flag` / `Flags` — the `pause_all` / `stop_all` operator flags persisted in the `settings` table. |
| `dag.py` | `STAGES` (list of `{kind, stage, phase}` dicts) and `create_project_dag()` — the canonical 10-kind chain (§5). |
| `handlers.py` | `build_registry(conn_factory, events=None)` → `{kind: async handler}` for all 10 DAG kinds; stage handlers with stub fallback, gate handlers with the approval flow (§5.1, §7). Also exports `GateRejected`. |
| `integration_client.py` | `get_integration()` / `call_stage(stage, project, payload)` — the *only* bridge to `app.integration`; lazy import on every call (§7). |
| `events.py` | `EventEmitter` — typed, never-raising hub-backed event publishing, plus the module-level `emit(hub, type, payload)` helper (§9). |
| `gates_bridge.py` | `ensure_approval`, `apply_decision`, `resume_graph`, `find_pending_approval` — the bridge between gate jobs, the persisted `approvals` table, and (secondarily) the LangGraph supervisor (§6). Owns the defensive `APPROVALS_DDL`. |
| `scheduler.py` | `SchedulerTick` (synchronous `tick(now_ms=None)`) plus `parse_cadence` / `next_occurrence` — materialises due `schedules` rows into projects + DAGs (§8). |
| `service.py` | `ExecutionService`, `build_service`, `attach` — lifecycle wiring: runner + registry + scheduler loop + the `pipeline.*` control handlers (§11). |

The package `__init__.py` is intentionally empty: sibling modules are
imported lazily (by `service.py` and by each other) so importing
`app.execution` never fails because one module has not landed yet.

## 2. How the runner claims and executes (`runner.py`)

`JobRunner.run()` is an asyncio loop over the durable queue — it holds
no work state of its own beyond the in-flight task table, so killing
the process loses nothing:

1. **Startup sweep.** `run()` first calls
   `SQLiteJobQueue.sweep_expired_leases()` so work orphaned by a
   previous crash becomes claimable again (§4).
2. **Flags every poll.** Each poll re-reads the operator flags (§3).
   `pause_all` stops claiming (in-flight jobs finish); `stop_all`
   cancels in-flight work and ends the loop.
3. **Claim only what can run.** The runner claims a job only when its
   kind has a registered handler *and* its pool (§2.1) still has a free
   slot. Claiming itself is the queue's atomic operation: one
   `BEGIN IMMEDIATE` transaction with `UPDATE … RETURNING` — a job is
   claimable only when it is `queued`, its `run_after` has been
   reached, and its `depends_on_job_id` predecessor finished with
   status `done`. Two runners can never claim the same job.
4. **Lease + heartbeat.** A claim sets `lease_owner`, `lease_expires_at`
   and `heartbeat_at` and increments `attempts` (default lease:
   60 s). While the handler runs, a heartbeat task extends the lease
   every `lease_seconds / 3`, so a long render is never swept out from
   under a live worker. Only the lease owner may heartbeat, start,
   complete or fail the job.
5. **Terminal transitions.** The runner babysits each handler task to
   exactly one queue transition:

   | Handler outcome | Transition |
   |---|---|
   | returns a result dict | `queue.complete(result)` |
   | raises `GatePending` | requeued for a later poll, **no attempt consumed** (§5.1) |
   | raises `JobCancelled` | `queue.cancel(...)` (terminal, no retry) |
   | raises anything else | `queue.fail(...)` — retry/backoff/dead-letter semantics |
   | task cancelled (runner stopping) | job cancelled, cancellation re-raised |

   Retryable failures requeue with exponential backoff
   (`base · 2^(attempts-1)` plus deterministic jitter) until
   `max_attempts`, then dead-letter to `dead`. A dead job poisons its
   dependents: the queue marks queued dependents `dead` with
   `error_code='dependency_failed'` rather than letting a pipeline
   wait forever on a dead blocker.
6. **Events are guarded.** Event emission (§9) is wrapped so a broken
   or absent hub can never kill the runner or fail a job.

`JobRunner` also exposes the control surface `pause()` / `resume()` /
`stop_all()` / `stop()`; the flag-setting methods persist through
`flags.set_flag` so a control action survives restarts, and
`is_running` / `is_paused` / `inflight_count` for tests and the UI.

### 2.1 Pools (`pools.py`)

Concurrency is capped per resource class, not globally — a single
FFmpeg render must not starve cheap LLM calls, and eight parallel LLM
calls must not spawn eight renders. `PoolManager` enforces the limits
with lazily-created asyncio semaphores and tracks `in_use` per pool so
the runner can decide which kinds are worth claiming:

| Pool | Limit | Why |
|---|---|---|
| `render` | **1** | FFmpeg renders are CPU/memory saturating; serialising them keeps the machine (and loudnorm/QC measurements) predictable. |
| `tts` | **4** | Voice synthesis is network/provider-bound; modest parallelism hides latency without hammering provider rate limits. |
| `llm` | **8** | LLM calls are latency-bound and cheap locally; the widest pool. |
| `downloads` | **6** | Stock/asset downloads are bandwidth-bound; parallel enough to fill a pipe, capped to stay polite to sources. |
| `upload` | **2** | YouTube uploads are quota- and bandwidth-heavy; near-serial by design. |
| `default` | 4 | Gates and `pipeline.*` control kinds. |

`POOL_LIMITS` contains exactly these six entries; the first five values
are pinned by contract test. `KIND_TO_POOL` maps every `STAGES` kind to
a pool (contract-tested), and `pool_for_kind()` falls back to `default`
for unknown kinds:

| Kind | Pool | Kind | Pool |
|---|---|---|---|
| `stage.research` | `llm` | `stage.render` | `render` |
| `stage.script` | `llm` | `stage.thumbnail` | `downloads` |
| `gate.a` | `default` | `stage.qc` | `llm` |
| `stage.voice` | `tts` | `stage.package` | `llm` |
| `stage.visuals` | `downloads` | `gate.b` | `default` |

The `upload` pool serves the publish kinds (`stage.publish`,
`stage.upload`) which exist outside the 10-kind DAG chain.

## 3. Operator flags: pause-all / stop-all (`flags.py`)

Two boolean kill-switches live in the `settings` table (`key` /
`value_json` / `updated_at`, JSON-encoded), so they **survive restarts**
and are visible to every component that opens the DB:

- `read_flags(conn_factory) -> Flags` — a frozen dataclass with
  `pause_all` / `stop_all` booleans. Reading is deliberately tolerant:
  a missing table, missing keys or unparsable values all yield `False`
  rather than raising — the runner must never crash because a control
  flag could not be read.
- `set_flag(conn_factory, name, value) -> str` — returns the canonical
  key written. The canonical keys are namespaced
  (`execution.pause_all` / `execution.stop_all`); `set_flag` writes
  both the canonical and the bare key, and `read_flags` accepts either
  (namespaced wins), so old and new readers always agree.

Semantics:

- **`pause_all`** — the runner stops claiming new jobs. In-flight jobs
  finish normally; queued jobs stay queued. The graceful drain used
  before maintenance or during a provider outage.
- **`stop_all`** — the runner cancels in-flight jobs and exits its
  loop. Queued work is left untouched (never deleted); a cancelled
  in-flight job goes through the queue's cancel transition, never
  silently dropped. Clearing `stop_all` does not auto-resume anything —
  a stopped runner must be started again (§11).

## 4. Crash recovery

Recovery is a property of the queue, exercised — not reinvented — by
the runner:

- The startup sweep (§2 step 1) requeues `leased`/`running` jobs whose
  `lease_expires_at` has passed while `attempts < max_attempts`, or
  dead-letters them with `error_code='lease_expired'` once attempts are
  exhausted; dependents of a dead-lettered job are then poisoned as in
  §2. (The queue also offers the sweep for periodic use; the runner's
  contract is the startup call.)
- Because claims, results and gate decisions are all committed rows
  before any in-memory state moves, a crash at any point replays
  cleanly: completed stages stay `done`, the in-flight stage is simply
  claimed again. Handlers must therefore be **idempotent or
  idempotency-keyed** (the queue's UNIQUE `idempotency_key`, and the
  DAG's per-stage keys in §5, are the mechanisms) — at-least-once
  execution is the guarantee, effectively-once is the handler's half
  of the bargain.
- Approvals survive restarts for the same reason: they are rows in the
  `approvals` table (§10), not in-memory state.

## 5. The DAG chain and gate semantics (`dag.py`)

One project = one linear chain of 10 jobs, linked by a **single**
`depends_on` (`jobs.depends_on_job_id`) per job — the queue supports
exactly one dependency per job, so job *n+1* is enqueued with
`depends_on` = job *n*'s id and the queue itself enforces the order:

```
stage.research → stage.script → gate.a → stage.voice → stage.visuals
→ stage.render → stage.thumbnail → stage.qc → stage.package → gate.b
```

`dag.STAGES` is a list of `{kind, stage, phase}` dicts in exactly this
order (contract-tested via the kinds); `STAGE_KINDS`, `STAGE_BY_KIND`,
`KIND_BY_STAGE` and `PHASE_BY_KIND` are derived views. `stage` is the
short name used in payloads and integration dispatch (`run_<stage>`);
`phase` is the `projects.current_phase` value while the stage runs
(research→`research`, script→`scripting`, gate.a→`scripting`,
voice/visuals→`production`, render→`editing`, thumbnail→`packaging`,
qc→`editing`, package→`packaging`, gate.b→`packaging`).

`create_project_dag(queue, project_id, *, start_phase=0) -> [job_id]`
enqueues the chain with payload `{"project_id": …, "stage": …}` and
`idempotency_key=f"dag:{project_id}:{kind}"` per job, so re-creating a
project's DAG returns the existing job ids instead of duplicating the
chain; `start_phase` (an index into `STAGES`) restarts a project
mid-chain. The chain is created by the scheduler (§8, with a
`pipeline.start` fallback) and by the service's `pipeline.start` /
`pipeline.resume` control handlers (§11).

Gate kinds are real jobs in the chain, not side channels:

- **`gate.a` (after `stage.script`)** — the Script Lock gate position.
- **`gate.b` (terminal, after `stage.package`)** — the Pre-Publish
  gate position.
- **Gate C is deliberately not a queue job.** It is the publish-time
  rule enforced inside the orchestration/publish path: only Agent 26
  (Upload) may publish, and only after Gate B
  (`app.orchestration.gates.can_pass_gate_c`).

What a gate *job* does is the approval flow of §5.1. The deterministic
content checks behind the gates (locked script + zero RED claims +
retention lint for A; Agents 32/33/34 PASS tokens bound to the frozen
`publish_manifest_hash` for B) live in `app.orchestration.gates` and
are evaluated on the graph path (§6) and by the producing stages; the
queue-level gate job is where the *human* decision is collected.

### 5.1 Human review: `GatePending` polling

In `review_first` publish mode, a gate handler with no recorded
decision:

1. delegates to `gates_bridge.ensure_approval(...)` (lazy-imported),
   which persists the single pending approval row and emits
   `gate.pending` (§6, §10) — or, if the bridge is unavailable, sets
   the project `status='awaiting_review'` and emits `gate.pending`
   itself;
2. raises `runner.GatePending`.

`GatePending` is an `Exception` subclass used purely as control flow.
The runner requeues the job with `run_after = now +
gate_requeue_delay_ms` (default **15 s**) via a direct row update —
deliberately *not* `queue.fail()`: **no attempt is consumed and no
failure is recorded**, because the queue's claim path has no notion of
"requeue the same attempt". The gate job therefore *polls*: every ~15 s
it wakes, checks its payload for a decision, and either proceeds or
raises `GatePending` again.

Recorded decisions resolve the poll (see §6 for how they get there):

- `payload.approval.decision == 'approved'` → the handler returns
  `{gate, decision: 'approved', approval_id}` and the chain proceeds.
- `'rejected'` → the handler raises `handlers.GateRejected`
  (`error_code='gate_rejected'`); the runner fails the job permanently
  — a rejection is a decision, not a transient error, so it is never
  retried.
- In `auto` publish mode no approval is created at all: the gate
  handler returns `{gate, decision: 'auto_approved'}` immediately.
  Automation removes the *human wait*, never the gate's position in
  the chain.

## 6. THE RESUME DECISION (`gates_bridge.py`)

> **Decision: resuming a gated project is primarily a JOB-LEVEL
> operation. LangGraph graph resume is a secondary adapter, used only
> for projects executed directly through the supervisor graph.**

Concretely, `gates_bridge` exposes:

- `ensure_approval(...)` → creates (idempotently) the pending
  `approvals` row for a gate and parks the project at
  `status='awaiting_review'`. Idempotency is load-bearing: because the
  gate job polls (§5.1), `ensure_approval` may be called repeatedly —
  there is at most one *pending* approval per `(project_id, gate)`,
  repeat calls return the existing approval id and do **not** re-emit
  `gate.pending`.
- `apply_decision(...)` → **the primary resume.** Called by the
  approvals router when a human decides. It records the decision on
  the approval row and merges
  `payload['approval'] = {decision, approval_id, reason_code}` into
  the gate job's `payload_json`; for `approved` it also zeroes the
  job's `run_after` so the runner picks the job up on its next poll
  instead of waiting out the 15 s delay. The job's next poll then sees
  `approval.decision` and completes (approved) or fails via
  `GateRejected` (rejected). No graph state is involved on this path.
- `resume_graph(db_path, thread_id, decision)` → **the secondary
  adapter.** Only for projects executed directly through
  `app.orchestration.graph.build_supervisor_graph`, whose gate nodes
  pause with `langgraph.types.interrupt()`: it invokes the graph with
  `Command(resume={'decision': ...})` on the project's thread config.
  The approvals router does **not** use this path; it exists so
  graph-driven runs and tests can be resumed with the same decision
  vocabulary.

Why job-level is primary:

1. **The queue is the durable system of record.** Job rows, payloads
   and approval rows are committed SQLite state; they survive process
   restarts, machine reboots and app updates. A LangGraph checkpoint is
   an execution detail of one way of running a project — not every
   project runs through the graph (scheduler-created and
   integration runs are queue-native), but every gated project has a
   gate *job*.
2. **Approvals must outlive the process.** The decision may arrive
   hours after the gate pended, across several backend restarts. The
   `approvals` table (§10) is where that decision durably lives; the
   payload flip is how it re-enters the one mechanism (claiming) that
   already handles leases, retries and recovery.
3. **One resume path, one audit trail.** Whether the human decided in
   the UI or via the API, exactly one function (`apply_decision`)
   records it and exactly one mechanism (the gate job's next poll)
   acts on it. Graph resume, where it applies, is a separate adapter
   for a separate execution mode — never a second, competing record
   of the same decision.

## 7. Handlers and the integration layer (`handlers.py`)

`build_registry(conn_factory, events=None)` returns a mapping of **all
10** `STAGES` kinds to *async* handlers (contract-tested). A handler
receives the queue's normalised job dict and returns a result dict,
which the runner stores as the job result. Handlers are job-level:
they know nothing about LangGraph.

Stage handlers (`stage.*`) work in five steps:

1. **Load the project row** (`job.project_id`, falling back to
   `payload.project_id`); a missing project raises `ValueError` — a
   permanent failure, since the job can never succeed.
2. **Try the real implementation** via
   `integration_client.call_stage(stage, project, payload)`, which
   lazy-imports `app.integration` *on every call* and invokes
   `run_<stage>(project, payload)` if it exists (dict results pass
   through; pydantic results are `model_dump()`-ed). On `None` **or
   any exception** the handler falls back to a deterministic **stub**:
   no network, no randomness — a pure function of the payload returning
   a well-formed result, always flagged **`stub: True`**. Real results
   never get the flag. A broken real stage therefore degrades to a
   stub instead of wedging the chain, and a stub-produced run is
   always identifiable as such downstream.
3. **Record the run:** one `agent_runs` row (status `succeeded`) and,
   for stages with an artefact type, one `project_artefacts` row at
   the next version. (The artefact table has no content column: the
   content JSON lives in the run's `output_ref_json`, its SHA-256 in
   the artefact's `content_hash`, and `file_path` is the integration
   result's path or a `stub://` URI.)
4. **Advance the project:** `current_phase` per `PHASE_BY_KIND`,
   `status='in_production'`.
5. **Accrue cost:** a small deterministic per-stage `cost_micros`
   (fixed table `STAGE_COST_MICROS` in `handlers.py`; a real result
   may override with its own `cost_micros` / `cost_usd`) is added to
   `projects.total_cost_micros` and emitted as `cost.ticker` (§9),
   guarded so event failures never fail a stage.

Gate handlers (`gate.a` / `gate.b`) do not call the integration layer;
they implement §5.1 directly on top of `gates_bridge`.

## 8. Scheduler (`scheduler.py`)

`SchedulerTick(conn_factory, *, queue=None, events=None)` is invoked
periodically by `ExecutionService` (§11) — roughly every 60 s, via
`asyncio.to_thread`, because `tick()` is a plain synchronous method
doing blocking SQLite I/O. One `tick(now_ms=None)`:

- selects enabled, non-deleted schedules with `next_run_at <= now`,
  oldest first;
- for each, materialises **one** project (channel/niche from the
  schedule, `topic_source='schedule'`, `publish_mode` and
  `target_minutes` from the schedule, `target_words` derived from the
  niche's `words_per_minute`), writes a `schedule_runs` row with
  status `created`, sets `last_run_at = now` and advances
  `next_run_at` to the first occurrence strictly after `now`;
- builds the project's DAG via `dag.create_project_dag(queue,
  project_id)` (lazy import) — falling back to a single
  `pipeline.start` job if the DAG builder is unavailable or raises, so
  scheduling works standalone;
- emits a `schedule.materialised` event per created project (guarded;
  a broken hub never breaks scheduling);
- returns a summary dict
  `{"checked", "materialised", "collapsed", "failed"}`.

**Catch-up policy (inside `tick` — there is no separate
`catch_up_once` method).** When a schedule is several slots behind
(app closed, machine asleep), the tick walks *every* missed occurrence
from `next_run_at` up to `now` (bounded by a 10,000-step cap) but
creates exactly **one** project — for the *latest* missed slot. The
earlier slots are collapsed: counted in the summary's `collapsed` and
in the run's `detail_json` (`missed_collapsed`), never materialised —
a week offline produces one video, not seven.

**Cadence formats.** Cadences are JSON objects in
`schedules.cadence_json`, decoded tolerantly by `parse_cadence` and
advanced by `next_occurrence(cadence, after_ms)` (all times epoch-ms,
all wall-clock arithmetic in **UTC**; the next occurrence is always
*strictly after* `after_ms`):

| `cadence_json` | Meaning |
|---|---|
| `{"type": "interval", "interval_hours": N}` | Occurrences are anchor + k·N hours; next is one step forward from `after_ms`. |
| `{"type": "daily", "time": "HH:MM"}` | Next day-boundary occurrence at HH:MM UTC strictly after `after_ms` (an exact hit rolls to the next day). |
| `{"type": "weekly", "weekday": 0..6, "time": "HH:MM"}` | Next occurrence on that weekday (Mon=0) at HH:MM UTC strictly after `after_ms` (an exact hit rolls a full week). |

Unknown types, missing fields, out-of-range values or unparseable JSON
yield `None`: the schedule materialises at most its already-due slot
and then stops advancing.

**Topic selection** (`schedules.topic_mode`):

- `queue` — the highest-priority `pending` `topic_queue` row for the
  schedule's channel/niche (NULL `channel_id` = channel-agnostic),
  marked `used` with `used_in_project_id` set. An empty queue *fails
  the slot* rather than inventing a topic.
- `fixed_list` — `cadence_json["topics"]` is a list; the topic rotates
  by `topics[count(prior schedule_runs) % len(topics)]`.
- `auto` — the research stage generates the topic later; since
  `projects.topic` is NOT NULL, the sentinel `auto:<niche_profile_id>`
  is stored and the research stage replaces the `auto:`-prefixed value.

**Failure handling.** A missing channel/niche row, an empty topic
queue or an empty fixed list does not abort the tick: a
`schedule_runs` row with status `failed` and a `reason` is written, the
schedule is advanced exactly as on success (so a broken schedule
cannot spam a failure every 60 s), and the tick continues with the
remaining schedules.

## 9. Event catalogue (`events.py`)

`EventEmitter(hub)` wraps a hub — a real `WSHub`, any object with a
compatible `publish(type, payload, *, project_id, job_id)` method
(e.g. the `RecordingHub` test fake in
`tests/execution_fixtures.py`), or `None` (all methods become silent
no-ops). Every method is **never-raising**: events are observability,
never control flow. Payloads are plain dicts carrying the method's
fields plus `ts` (epoch **milliseconds**); the hub adds the standard
envelope (`v`, `type`, ULID `event_id`, monotonic `seq`, `boot_id`,
ISO-8601 `ts`, scope fields) and journals it to `event_log` for
reconnect replay.

| Method | WS type | Topic | Notes |
|---|---|---|---|
| `agent_status(job, status, **extra)` | `agent.status_changed` | `project:{id}` | Job/agent transition; job/project/agent_key/kind extracted from the job. Guaranteed (never dropped by the hub). |
| `phase_changed(project_id, phase, **extra)` | `pipeline.phase_changed` | `project:{id}` | Project entered a new phase. Guaranteed. |
| `cost_ticker(project_id, cost_micros, total_cost_micros, **extra)` | `cost.ticker` | `cost` | Incremental + cumulative cost. Droppable under hub backpressure. |
| `gate_pending(project_id, gate, approval_id, **extra)` | `gate.pending` | `project:{id}` | A human gate is waiting (§5.1). Guaranteed. |
| `budget_warning(project_id, **extra)` | `budget.warning` | `cost` | A budget cap is near/reached. Guaranteed. |

The module also exposes `emit(hub, type, payload, **kw)` — the
guarded one-shot publish used internally, for callers that hold a hub
but no emitter. Money is always integer micros (USD × 1,000,000),
never floats, per the schema conventions. (One further type,
`schedule.materialised`, is published by the scheduler directly
through its events object rather than through `EventEmitter`.)

## 10. Approvals persistence + migration 0002

Round 1's approvals router kept decisions in an in-memory set (a
restart forgot them). Round 2 makes approvals durable rows:

```sql
CREATE TABLE IF NOT EXISTS approvals (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
    gate                   TEXT NOT NULL CHECK (gate IN ('a', 'b')),
    artefact_versions_json TEXT NULL,
    decision               TEXT NULL CHECK (decision IN ('approved', 'rejected')),
    reason_code            TEXT NULL,
    note                   TEXT NULL,
    decided_by             TEXT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'decided')),
    created_at             INTEGER NOT NULL,
    decided_at             INTEGER NULL
)
```

- `status` tracks the lifecycle (`pending` → `decided`); the verdict
  itself lives in `decision`. A second decision on the same approval
  remains `409 already_decided`, now enforced by the row instead of a
  process-local set. `artefact_versions_json` freezes *what* the human
  was shown, so a decision is auditable against exact artefact
  versions.
- The canonical DDL ships in **Alembic migration `0002`**
  (`migrations/versions/0002_approvals.py`, `down_revision = "0001"`).
  Because test/startup order is not guaranteed, the table is also
  created defensively with `CREATE TABLE IF NOT EXISTS` (identical
  DDL) by `gates_bridge` (`APPROVALS_DDL` / `ensure_approvals_table`,
  before every use) and by the approvals router — the copies must be
  kept in sync; they are fallbacks, the migration is the authority.
- The same migration adds **`provider_credentials.last4`** (nullable
  TEXT): the last four characters of a credential, persisted as
  display metadata only (the providers router writes it when a key is
  saved, and reads it back as `key_last4`) so the UI can show "…1234"
  without ever touching the secret, which continues to live
  exclusively in the OS credential store referenced by `keyring_ref`.
- Bootstrap asymmetry to remember: `app.db.init_db` applies the
  section-15 DDL (schema v1) for a fresh file; `approvals` and `last4`
  arrive via `alembic upgrade head` (or the bridge's defensive DDL).
  Code reading approvals on an un-migrated database must tolerate the
  table being absent.

## 11. Service facade and autostart (`service.py`, `YTA_EXECUTION`)

`ExecutionService` is the single object `app.main` (or a test) talks
to. It owns:

- the `JobRunner`, built over the handler registry (its `run()`
  performs the startup lease sweep itself — the service does not sweep
  separately);
- the registry from `handlers.build_registry`, built over the same
  `conn_factory`;
- the scheduler loop: a `SchedulerTick` roughly every **60 s** (first
  tick after **5 s**), run via `asyncio.to_thread` so the blocking
  SQLite tick never stalls the event loop;
- the **`pipeline.*` control handlers**, merged *over* the stage
  registry so a stage handler can never shadow them:
  `pipeline.start` (ensure a DAG exists for the project; mark a draft
  project `in_production`), `pipeline.pause` (cancel the project's
  *queued* stage jobs; running jobs finish; project status untouched),
  `pipeline.resume` (re-create the DAG if no stage jobs are active —
  safe after a pause thanks to the DAG's idempotency keys),
  `pipeline.cancel` (cancel queued/leased stage jobs; set the project
  `cancelled`).

`start()` / `stop()` are async and idempotent. **Degradation
contract:** every sibling module is imported lazily inside try/except
(mirroring `app.main`'s style) — if a module has not landed, the
service logs a warning and runs in a reduced mode (scheduler-only,
runner-only, or control-handlers-only with an empty stage registry)
rather than failing to start. Nothing in `service.py` raises at import
time.

**Autostart policy.** `service.attach(app, autostart=False)` attaches
a dormant service by default, so tests and embedders start it manually.
The env var **`YTA_EXECUTION`** overrides the default in `app.main`;
`app.main.run()` — the real process entry point — sets
`YTA_EXECUTION=1` when unset, so the shipped sidecar executes out of
the box. **`YTA_EXECUTION_AUTOSTART=0`** is an explicit kill-switch:
it forces autostart off even when the caller (or the env default)
asked for it.

## 12. Known limitations

- **Single host, single database, single loop.** One SQLite file
  serialises all claims, and pool limits are enforced by
  per-process asyncio semaphores. Two backend processes against one DB
  file stay *correct* (claims are atomic) but pool limits become
  per-process, not global.
- **`render` = 1 is a hard serialisation point.** Renders queue behind
  each other; long videos make this the pipeline's throughput ceiling
  by design.
- **Gate polling costs a wake-up every ~15 s per pending gate.** The
  payload-flip design (§6) trades a small polling load for having no
  second notification channel to keep consistent; `apply_decision`
  zeroes `run_after` on approval so the *resume* is prompt even
  though the *wait* polls.
- **Stub-produced results are real rows.** Until `app.integration`
  lands a stage, its outputs exist in the DB/artefacts, flagged
  `stub: True`. Consumers must check the flag; nothing downstream
  auto-blocks a stub-produced project from reaching `gate.b`.
- **Approvals DDL lives in several places** (migration 0002,
  `gates_bridge.APPROVALS_DDL`, the approvals router's lazy create) by
  deliberate defensive design — they must be edited together or a
  fallback silently diverges.
- **Graph resume is best-effort and separate.** `resume_graph` only
  applies to projects executed directly through the supervisor graph
  with a live checkpoint; it is not wired into the approvals router,
  and queue-native projects neither need nor use it.
- **Flags are cooperative.** `stop_all` cancels in-flight *tasks*;
  a handler stuck in a synchronous call that never yields to the
  event loop can only be reclaimed by the lease sweep after expiry.
- **Scheduler cadences are UTC-only.** There is no per-schedule
  timezone; `daily`/`weekly` times are UTC wall-clock.
- **No cross-job parallelism inside a chain.** The single-`depends_on`
  chain is strictly linear; parallel branches would need a DAG
  generalisation Round 2 deliberately does not use.
