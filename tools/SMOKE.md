# UI ↔ Backend Smoke — Round 2 (Integration)

**Date:** 2026-10-22 · **Sidecar:** real backend via `tools/dev-ui.sh`
(`YTA_PORT=8790`, `YTA_TOKEN=dev-token`, `YTA_BOOT_ID=dev-boot`,
`YTA_DATA_DIR=$REPO/.dev-data` on the workspace disk, `YTA_DETACHED=1`) ·
**UI build:** `npm run typecheck` ✅ zero errors · `npm run build` ✅
(`dist/` produced; one cosmetic >500 kB chunk-size warning).

All requests below were issued with header `X-Local-Token: dev-token`
unless marked "no token". Bodies are trimmed to the fields that matter.

---

## Health & system

### `GET /health/live` (no token) → 200
```json
{"status":"ok"}
```

### `GET /health/ready` → 200
```json
{"status":"ready","db":"ok","migrations":"ok"}
```

### `GET /api/v1/system/info` (no token) → 401
```json
{"error":{"code":"missing_token","message":"Missing authentication token","details":{},"request_id":"ea6454d1…","retryable":false}}
```

### `GET /api/v1/system/info` → 200
```json
{"app_version":"0.1.0","backend_version":"0.1.0","api_version":1,"ws_schema":1,"min_ui_api":1,"boot_id":"dev-boot","capabilities":["gates.v1","cost_ticker.v1","ws_commands.v1"]}
```

## Mission Control / queue

### `GET /api/v1/queue/stats` → 200
```json
{"by_status":{},"running":0,"queued":0}
```
(After the pipeline action below: `{"by_status":{"done":1},"running":0,"queued":0}`.)

### `GET /api/v1/queue/snapshot` → 200
```json
{"projects":[],"queue":{"by_status":{},"running":0,"queued":0},"quota":{},"today_cost_usd":0.0,"generated_at":"2026-10-22T20:34:27.882850Z"}
```
Note: `quota` is `{}` — **the backend exposes no YouTube quota figures
anywhere**; both quota panels in the UI render an explicit
"not exposed yet" state.

## Niches (wizard)

### `GET /api/v1/niches?limit=50` → 200
8 builtins: `facts, islamic_stories, horror_stories, history, motivation,
tech, crime_stories, health` — list items are `{id, name, source:"builtin"}`.

### `POST /api/v1/niches` — new Other → 201
Request (`Idempotency-Key: smoke-niche-1`):
```json
{"name":"Cricket Analysis","idea":"Cricket matches ki kahani aur stats Urdu mein, har match ka analysis"}
```
Response:
```json
{"id":"63bd8d10-79aa-4337-846a-358937e8a21d","name":"Cricket Analysis","source":"other","status":"provisional","idea":"Cricket matches ki kahani aur stats Urdu mein, har match ka analysis","language":null,"tone":null,"target_minutes":null,"key":"cricket_analysis"}
```

### `POST /api/v1/niches` — invalid (name <2 chars) → 422
```json
{"error":{"code":"validation_error","message":"Niche name must be 2..40 characters.","details":{"field":"name","length":1},"request_id":"bd2dc777…","retryable":false}}
```
This `details.field` shape is what the wizard maps to inline field errors.

**Cosmetic backend inconsistency:** the POST response says
`"status":"provisional"` but the subsequent list shows the same profile
with `"status":"active"`.

## Channels

### `GET /api/v1/channels` → 200 (before seeding)
```json
{"items":[],"next_cursor":null}
```
For the project smoke, one channel row was seeded directly into the dev
DB (`smoke-channel-1`, "Smoke Test Channel", status `active`) — the
connect endpoint is a 202 stub that creates nothing. After seeding, the
same GET returns the row verbatim (raw snake_case DB fields).

## Projects / create-video flow

### `POST /api/v1/projects` WITHOUT `Idempotency-Key` → 400
```json
{"error":{"code":"idempotency_key_required","message":"This endpoint requires an Idempotency-Key header.","details":{},"request_id":"dcf0abcb…","retryable":false}}
```

### `POST /api/v1/projects` with `Idempotency-Key: smoke-proj-1` → 201
Request (exactly what the wizard sends):
```json
{"channel_id":"smoke-channel-1","niche_profile_id":"facts","topic_mode":"auto","target_minutes":10,"publish_mode":"review_first"}
```
Response:
```json
{"id":"896c8dcb-10e7-417a-a21a-02d58778a735","channel_id":"smoke-channel-1","niche_profile_id":"facts","topic_mode":"auto","topic":null,"target_minutes":10,"publish_mode":"review_first","status":"draft","current_phase":null,"created_at":"2026-10-22T20:35:53.487507Z","updated_at":"2026-10-22T20:35:53.487536Z","task_dag":{"status":"not_started","total_agents":35,"phases":["research","scripting","production","editing","packaging","publishing","growth","safety_qa"]}}
```

### Replay — same key, same body → 201, **identical response**
Same `id` (`896c8dcb-…`), same timestamps: the backend replays the
original response; **no duplicate project is created**. This is the
guarantee the UI now uses: the wizard forwards one stable
per-attempt UUID as the key (see `client.ts` `createProject` options).

### `POST /api/v1/projects/{id}/pipeline/start` (`Idempotency-Key: smoke-pipe-1`) → 202
```json
{"status":"accepted","project_id":"896c8dcb-10e7-417a-a21a-02d58778a735","action":"start","job_id":"7d559628-74cd-448d-b0c4-ad59e9d1b545"}
```
The job really enqueued and completed (`queue/stats` → `done: 1`).

## Agents / approvals

### `GET /api/v1/agents` → 200
35 items; first: `{"id":"agent_01","phase":"research","name":"Trend Research","status":"idle"}`.
All statuses are `"idle"` today (no producer publishes live frames yet).

### `GET /api/v1/approvals` → 200
```json
{"items":[],"next_cursor":null}
```
Genuinely empty: **the schema has no approvals table yet**; the Review
screen shows its designed "All caught up" state. Decide endpoints exist
(`POST /approvals/{id}/approve|reject`, 409 code `already_decided`
verified in the router + backend tests by the Review worker).

## Providers / settings (Settings screen)

### `GET /api/v1/providers` → 200
8 items `{id, configured, has_key, key_last4}`: gemini, openai, anthropic,
elevenlabs, pexels, pixabay, azure_tts, google_tts — all unconfigured here.

### `PUT /api/v1/providers/gemini/key` with the OLD body `{"key": "…"}` → 422
```json
{"error":{"code":"validation_error","message":"Request validation failed","details":{"errors":[{"type":"missing","loc":["body","value"],"msg":"Field required","input":{"key":"sk-dummy"}}]},"retryable":false}}
```
This was the Round 1 client bug (`putProviderKey` sent `{key}`); the
client now sends `{value}` (fixed in Round 2).

### `PUT /api/v1/providers/gemini/key` with `{"value": "…"}` → 503 (this VM only)
```json
{"error":{"code":"credential_store_unavailable","message":"The OS credential store is unavailable on this machine.","details":{"detail":"No recommended backend was available…"},"retryable":true}}
```
Validation passes; the write then fails because **this Linux VM has no OS
keyring backend**. On Windows (Credential Manager) this path completes.
The UI surfaces this error on the card and keeps the previous state.

### `POST /api/v1/providers/gemini/test` → 200 (stub)
```json
{"provider":"gemini","ok":true,"detail":"stub — real adapter pending"}
```
The Settings UI detects the stub marker and shows "not verified",
never a green success.

### `GET /api/v1/settings` → 200
```json
{}
```

## Usage / costs

### `GET /api/v1/usage` → 200
```json
{"items":[],"next_cursor":null}
```
### `GET /api/v1/usage/today` → 200
```json
{"date":"2026-10-22","totals":{}}
```
No caps are exposed by `/usage` or `/settings` — the Costs budget card
states this instead of inventing numbers.

## Jobs / publications / event journal

### `GET /api/v1/jobs` → 200 · `GET /api/v1/publications` → 200
Both `{"items":[],"next_cursor":null}` on a fresh DB.

### `GET /api/v1/logs?limit=5` → 200 (after the pipeline action)
The hub persists every published frame; the pipeline action produced:
```json
{"id":"01M5JN8CCC0021SPKD5P5M21QW","seq":4,"boot_id":"dev-boot","type":"pipeline.phase_changed","message":"{\"v\": 1, \"type\": \"pipeline.phase_changed\", \"event_id\": \"01M5JN8CCC0021SPKD5P5M21QW\", \"seq\": 4, \"boot_id\": \"dev-boot\", \"ts\": \"2026-10-22T20:36:18.956Z\", \"payload\": {\"project_id\": null, \"phase\": \"pipeline.start\"}, \"job_id\": \"7d559628-74cd-448d-b0c4-ad59e9d1b545\"}","created_at":"2026-10-22T20:36:18.957000Z"}
```
This journal is what Agents Live's activity feed decodes (same mappers
as live frames).

## WebSocket handshake (exactly the UI's flow)

### `POST /api/v1/ws-ticket` → 200
```json
{"ticket":"RtHoEjSFAKGLW2_NxrdST2i0apwLZUouS2d_of1DDSk","expires_in":60}
```

### `WS /api/v1/ws` + first frame `client.hello` (ticket in frame, never in URL)
→ first server frame:
```
system.hello_ack · boot_id "dev-boot" · seq 4
```
### Ticket replay (same ticket, second connection)
→ connection closed with code **4401** (single-use enforced).

---

## Environment notes

- Dev DB (`.dev-data/`) is disposable: it now contains the seeded
  channel, the "Cricket Analysis" Other profile, and one draft project
  from this smoke. Delete `.dev-data/` to reset.
- `/tmp` on this VM is full — all commands ran with
  `TMPDIR=$HOME/workspace/.tmp`.
- Sidecar stopped after the smoke (`tools/dev-ui.sh --stop`).
