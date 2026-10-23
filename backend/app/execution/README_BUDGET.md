# Budget caps — exposure + full enforcement (Round 4)

Round 3 shipped caps *exposure* plus *enforcement-lite* (per-video only).
Round 4 adds the missing half: a durable reservation store and full
daily/monthly enforcement at the execution gate, plus a read-side
budget view on the same accounting. This file documents the whole
semantics; the code of record is `app/execution/budget_gate.py`
(the gate), `app/execution/budget_reservations.py` (the store) and
`app/budget_view.py` (the read view).

## Endpoints

- `GET /api/v1/settings/budget` → `{per_video_cap_usd, daily_cap_usd, monthly_cap_usd, warn_pct, soft_pct}`
- `PUT /api/v1/settings/budget` → same shape; validates and persists.
- `GET /api/v1/usage/budget` → budget summary for the current Pacific
  day **and** month (see "Read view" below).
- `GET /api/v1/usage/quota` → YouTube quota buckets (Round 3; separate
  concern — quota is Google units, budget is USD).

Validation: caps must be positive finite numbers; the threshold ladder must
satisfy `0 < warn_pct < soft_pct < 100` (422 `budget_policy_invalid` /
`validation_error` otherwise).

## Storage

**Policy:** single settings-table key **`budget_policy`** whose `value_json`
is one JSON object with all five fields (see
`app/providers/budget/policy.py`). One key = atomic updates; the generic
`GET /api/v1/settings` shows it as a nested entry.

**Reservations:** table **`budget_reservations`** (Alembic migration
`0003_budget_reservations`; also created lazily by the store's
`ensure_reservations_table`, following the `gates_bridge` pattern, so
unmigrated databases work). One row per reservation:

| Column | Meaning |
|---|---|
| `id`, `project_id`, `channel_id NULL` | reservation id; owning project; channel attribution (`NULL` = unattributed) |
| `reserved_micros` | DAG estimate reserved at start (integer micros) |
| `settled_micros` | actual costs recorded against the reservation so far |
| `status` | `active` → `settled` \| `released` |
| `pacific_day` (`YYYY-MM-DD`), `pacific_month` (`YYYY-MM`) | Pacific bucket stamps, written at reserve time |
| `created_at`, `updated_at`, `settled_at`, `released_at`, `release_reason` | epoch-ms timestamps + release audit |

Money is integer **micros** everywhere (USD × 1,000,000); API dicts add
`*_usd` float mirrors for display only — floats are never accumulated.

## Defaults (roadmap section 35 / `35_cost_control.md`)

| Field | Default | Source |
|---|---|---|
| `per_video_cap_usd` | $1.00 | L3 Standard stack hard cap (Free $0.25 / Premium $5.00) |
| `daily_cap_usd` | $10.00 | L5 global/day default |
| `monthly_cap_usd` | $30.00 | L6 global/month suggested starter |
| `warn_pct` | 70 | 70/90/100 ladder — warning |
| `soft_pct` | 90 | 70/90/100 ladder — soft stop / downgrades |

Note: the LLM router's internal per-video *LLM* sub-cap stays $0.50
(`app.providers.llm.caps.PER_VIDEO_CAP_USD`); this policy's per-video cap is
the whole-video hard cap, not the LLM line alone.

## Reservation lifecycle

1. **Reserve at DAG start.** When a project passes the gate (below), the
   gate persists an `active` reservation for the *incremental* estimate —
   `max(0, estimate − projects.total_cost_micros)` — so past spend is
   never reserved twice. Reserving is idempotent per project
   (`ensure_reservation`): the gate is called from two seams
   (`pipeline.start` and the first DAG stage) and reserves exactly once.
   The row is stamped with the Pacific day/month **at reserve time**.
2. **Settle with actuals.** As each stage cost is recorded, the stage
   handlers call `settle_for_project`, adding the actual micros to
   `settled_micros`. A partial settle keeps the row `active` with a
   shrunken remainder; when the settled total reaches the reserved
   estimate (or a final settle lands), the row becomes `settled` and the
   unspent remainder is freed. An over-settle records the truthful
   actual — the excess still counts as spend.
3. **Release on failure / cancel / stop.** `pipeline.cancel` (and the
   run-ended paths) call `release_for_project`: the row becomes
   `released` and its unsettled remainder is freed. Amounts already
   settled **stay committed** — released money was really spent.

Derived per row (never stored): `remaining = max(0, reserved − settled)`
while `active`, else 0; `committed = settled + remaining`. Bucket spend
is the sum of `committed` over the bucket's rows — while a reservation
is active it contributes `max(reserved, settled)`, so settling shrinks
the reserved slice, never double-counts.

**Bucket membership** is by the stored Pacific stamps, not by settle
time: a settle that lands after Pacific midnight does not move spend
into the next day.

**Legacy projects.** Projects created before Round 4 have no reservation
rows. Their actuals still count: the bucket adds
`projects.total_cost_micros` for projects *created* inside the bucket
window that have **no reservation row at all**. A project with any
reservation row is never in the legacy sum — counted exactly once.

## Which caps are enforced where

All three policy caps are enforced by one function,
`check_budget_for_project` in `app/execution/budget_gate.py`, called at
project start from `pipeline.start` / `pipeline.resume`
(`app/execution/service.py`) and from the first DAG stage
(`stage.research` in `app/execution/handlers.py`):

| Cap | Check |
|---|---|
| per-video | `estimate > per_video_cap_usd`, where `estimate = projects.total_cost_micros + DAG reserved estimate` (`estimate_project_cost_micros` is the single seam where real provider estimates replace the deterministic stage-cost table) |
| daily | `daily bucket committed spend + incremental estimate > daily_cap_usd` |
| monthly | `monthly bucket committed spend + incremental estimate > monthly_cap_usd` |

- **Under / at every cap:** the reservation is persisted (step 1 above)
  and execution proceeds.
- **Over any cap:** the project does **not** proceed and does **not**
  hard-fail. It is parked at Gate A: `gates_bridge.ensure_approval`
  creates the single pending Gate A approval and sets
  `status='awaiting_review'`; the approval's `reason_code='budget_pending'`
  and its `note`/`artefact_versions_json` carry the figures for the
  *breached* cap (`cap_scope` ∈ `per_video` | `daily` | `monthly`, plus
  explicit `per_video_*` / `daily_*` / `monthly_*` blocks). A
  `budget.warning` WS event (guaranteed event, `events.py`) is emitted
  once per park with `spent_*`, `remaining_*`, `needed_*`, `estimated_*`,
  `cap_*` in micros and USD, where `spent` = committed spend against the
  breached cap, `remaining = max(0, cap − spent)`,
  `needed = max(0, estimate − cap)`. `pipeline.start` returns
  `dag='budget_pending'`, `budget_paused=true` and creates **no** DAG
  jobs — and **no reservation is made** for a parked project: nothing is
  committed until a run is allowed to start.
- The per-call layer (`app.providers.budget.guard`, the Budget Guard
  service) still owns the L1/L2 limits (per-call sizes, retry
  sub-caps) and the 70/90/100 threshold ladder with its downgrade
  steps for provider calls; the execution gate owns the persisted
  per-video/daily/monthly caps above.
- Helper errors fail open (logged) so a broken settings read, store or
  events hub never wedges the pipeline; an *established* over-cap
  result is never swallowed.

## Override path

Two explicit user actions unblock a budget-parked project — there is no
silent or agent-side override:

1. **Approve the `budget_pending` approval** (normal approvals flow).
   A decided/approved `budget_pending` Gate A approval bypasses the
   check for that project on later starts (otherwise start/resume
   would re-park forever). A tracking reservation is still recorded so
   the spend stays attributed. Rejecting the approval leaves the
   project blocked via the normal gate flow.
2. **Raise the cap** in Settings (`PUT /api/v1/settings/budget`). The
   estimate is then no longer over the cap and new starts proceed.
   (At the provider layer, `BudgetGuard.raise_cap` likewise requires
   the explicit user-action marker and is audit-logged; agent /
   supervisor / system actors may never raise a cap.)

## Clock choice — one "today" for the whole app

Both budget buckets use **America/Los_Angeles (Pacific)** day and
month boundaries — the *same* discipline as the YouTube quota view
(`app/quota_view.py`, `app/providers/youtube/quota.py`: buckets reset
at midnight Pacific). Deliberately one clock, for one reason: quota
and budget sit side by side in the UI, and two different definitions
of "today" (e.g. UTC for budget, Pacific for quota) would show two
different days at the same moment and make every figure look wrong
next to the other. Pacific is the natural anchor because Google's
quota day is Pacific; the budget day/month simply align to it.

Mechanics: Pacific month = the calendar month in America/Los_Angeles;
boundaries are computed with `zoneinfo` math (the store re-exports
`pacific_day_window_ms` / `pacific_month_window_ms`), so DST days are
handled exactly — the spring-forward day is 23 hours, the fall-back
day 25, and a cost at 01:30 on fall-back day belongs to that Pacific
day in both its occurrences. Tests monkeypatch the store's
`now_pacific` seam to steer boundaries (the gate reads its clock from
the same seam).

## Read view (`app/budget_view.py` → `GET /api/v1/usage/budget`)

A pure read over the same stores — no enforcement logic duplicated.
Its `spent` figures are the gate's `bucket_committed_micros` split
into settled/reserved (verified to the micro by
`tests/test_budget_view.py`):

```json
{
  "pacific_date": "2026-10-23", "pacific_month": "2026-10",
  "resets_at": "<next Pacific midnight, UTC ISO>",
  "seconds_until_reset": 0,
  "month_resets_at": "<first of next Pacific month, UTC ISO>",
  "seconds_until_month_reset": 0,
  "day":   {"cap_usd": 10.0, "settled_usd": 0.0, "reserved_usd": 0.0,
            "spent_usd": 0.0, "remaining_usd": 10.0, "…_micros": 0},
  "month": {"cap_usd": 30.0, "…": "same shape"},
  "channels": {"<channel_id>": {"day": {"…": "same shape, cap null"},
                                "month": {"…": "…"}},
               "unassigned": {"…": "…" }},
  "policy": {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0,
             "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90}
}
```

- `settled` = reservation settled slices in the bucket + legacy
  project actuals; `reserved` = active reservations' remainders;
  `spent = settled + reserved`; `remaining = max(0, cap − spent)`.
- Per-channel figures are keyed by channel id, with unattributed spend
  under the `"unassigned"` key (the store's `NULL` channel bucket).
  Channel buckets carry `cap/remaining = null` — the policy defines
  only app-level caps, so no per-channel cap is invented. Per-channel
  micros sum **exactly** to the app totals (the totals are built as
  their sum).
- The `api_usage` provider ledger is **not** budget spend and is never
  added here: it is the quota/provider-call ledger (Round 3's usage
  endpoints), reported separately just as
  `app/execution/channel_context.py` does. Provider actuals reach the
  budget buckets by settling against reservations.
- Missing tables / empty ledgers degrade to honest zeros with the real
  caps and reset math — never fabricated numbers. Reservations are
  read through the store's own `list_reservations`; a shape-adaptive
  SQL fallback covers a store whose module is unavailable.
