# Round 4 — Channel scoping, context resolution & quota note

## Channel context resolution

`backend/app/execution/channel_context.py` is the single way the
execution/integration path resolves, for a given project, its channel's
effective profile. Stage handlers (`app.execution.handlers`) resolve it
once per stage and pass it to integration stages as `channel_context`
on the project/payload dicts.

Precedence (grounded in existing code):

- **Niche:** the project's schedule's `niche_profile_id` wins, then the
  project's own `niche_profile_id`, then the channel's
  `default_niche_profile_id`. (The scheduler already copies the
  schedule's niche onto projects it materialises.) A schedule owned by
  a *different* channel is ignored entirely — niche and budget — as a
  cross-talk guard.
- **Voice:** `niche_profiles.default_voice_id`, with `voice_style` as
  the downstream hint. There is no channel-level voice override: no
  code reads a voice key from `channels.config_json` (that column
  carries connect-state keys only), so none was invented.
- **Budgets:** monthly cap from `channels.monthly_budget_micros`;
  daily cost cap from the (channel-matching) schedule's
  `daily_cost_cap_micros`; daily upload cap from
  `channels.daily_upload_cap`.

## Per-channel usage view

`GET /api/v1/usage/channels` (and `/channels/{channel_id}`) returns,
per channel: month-to-date spend attributed strictly through
`project_id → projects.channel_id` (`agent_runs` costs; `api_usage`
costs reported separately, never double-counted), the channel's
monthly cap and remaining headroom, its schedules' daily caps, and
project counts by status. Read-only; honest zeros when no data.

## YouTube quota is NOT per-channel

YouTube API quota is allocated **per Google Cloud project**, not per
channel. All channels connected in this app share the single GCP
project's quota, so they share the app-level quota buckets exposed in
Round 3 (`GET /api/v1/usage/quota`: uploads / search / shared, reset
at midnight Pacific). Channels do **not** get separate YouTube quota
partitions. The per-channel caps in this app (`monthly_budget_micros`,
`daily_cost_cap_micros`, `daily_upload_cap`) are budget and scheduling
controls enforced by the app itself — they do not create, reserve, or
partition any YouTube API quota.
