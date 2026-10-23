# Style Lab — durable contract

Style Lab lets the user point the app at 1..N competitor YouTube
channels, attach 1–2 of each competitor's videos (file upload or
pasted metadata), have those videos analysed, and distil the result
into a **style profile scoped to one of the user's own channels**.
Generation (script / visuals / thumbnail / SEO) then consumes the
active profile for that channel.

This document describes the system **as built** in the Style Lab
mini-round (intake + storage, 2026-10-23). Where the built reality
differs from the original feature brief, the difference is called
out explicitly in [§9 Deviations](#9-deviations-from-the-original-brief).

---

## 1. Hard boundary (non-negotiable)

Style Lab learns **style / formula only**: concept structure, hook
pattern, pacing, per-scene visual-source mix (stock vs AI-generated,
always reported as probabilistic likelihoods, never facts), editing
patterns, and title / description *patterns*.

It must **never**:

- reuse a competitor's footage, script text, exact titles,
  thumbnails or branding (copyright, YouTube's reused-content
  policy, and the app's own similarity gate);
- scrape YouTube or use unofficial endpoints;
- download videos from YouTube — there is **no** YouTube download
  endpoint anywhere in this feature. Videos enter only as
  user-supplied file uploads or as user-pasted / URL metadata;
- fetch public metadata by any route other than the official
  YouTube Data API v3, and then only when a credential is
  configured (§7).

---

## 2. Schema (migration `0004_style_lab`)

Alembic migration `backend/migrations/versions/0004_style_lab.py`
(revision `0004`, down-revision `0003`) creates three tables. The
identical DDL lives in `backend/app/style_lab/schema.py` and is
ensured lazily at runtime (`ensure_style_lab_schema`), so the
feature works on databases that have not run migrations. TEXT
primary keys are uuid4 hex; timestamps are INTEGER Unix epoch
milliseconds.

### `competitor_channels` — one competitor, scoped to an owner

| column | notes |
|---|---|
| `id` | PK |
| `channel_id` | **the scope**: FK → `channels(id)` ON DELETE RESTRICT — the *user's own* channel this competitor belongs to |
| `platform` | `'youtube'` only (CHECK) |
| `source_input` | verbatim user input (provenance) |
| `source_kind` | CHECK: `channel_url` \| `handle` \| `channel_id` \| `video_url` \| `custom_url` |
| `external_channel_id` | competitor's UC id when known |
| `handle` | stored without `@`, case preserved |
| `display_name`, `channel_url` | filled by metadata fetch when it succeeds |
| `metadata_status` | CHECK: `not_fetched` \| `fetched` \| `metadata_unavailable` \| `fetch_failed` |
| `metadata_json` | fetched public metadata payload, or `{status, reason, detail}` for non-fetched outcomes |
| `status` | `active` \| `archived` |

### `competitor_videos` — one video of a competitor

| column | notes |
|---|---|
| `id` | PK |
| `competitor_channel_id` | FK → `competitor_channels(id)` ON DELETE RESTRICT |
| `source` | CHECK: `attached_file` \| `url_metadata` \| `pasted_metadata` |
| `external_video_id`, `video_url` | for metadata-sourced rows |
| `title`, `description`, `tags_json` | user-pasted / URL metadata |
| `storage_path`, `thumbnail_storage_path` | relative to the app data dir (§5) |
| `file_sha256`, `file_size_bytes`, `mime_type`, `duration_s`, `width`, `height`, `thumbnail_sha256` | ffprobe-probed fields for `attached_file` rows |
| `analysis_status` | CHECK: `not_queued` \| `queued` \| `analysing` \| `analysed` \| `analysis_failed` (default `not_queued`) |
| `analysis_requested_at` | epoch ms, stamped when analysis is queued |

### `style_profiles` — a distilled style profile, channel-scoped

| column | notes |
|---|---|
| `id` | PK |
| `channel_id` | **the scope**: FK → `channels(id)` ON DELETE RESTRICT |
| `name` | user-facing label |
| `version` | INTEGER, default 1 (see versioning rule, §6) |
| `status` | CHECK: `draft` \| `active` \| `superseded` |
| `competitor_ids_json` | JSON list of `competitor_channels.id` the profile was distilled from — all must belong to `channel_id` |
| `profile_json` | the style profile payload itself (§6) |

Indexes: `(channel_id, status)` on competitors and profiles,
`(competitor_channel_id)` and `(analysis_status)` on videos.

---

## 3. Code layout

Intake / storage package — `backend/app/style_lab/`:

| module | responsibility |
|---|---|
| `schema.py` | DDL + `ensure_style_lab_schema` (kept in sync with migration 0004) |
| `parsing.py` | pure parsing of competitor inputs (channel URL, `@handle`, UC id, `/c/`+`/user/` custom URLs, video URLs) → `ParsedCompetitor`; rejections raise `StyleLabInputError` with a machine-readable `reason`. A video URL alone never identifies a channel — channel fields stay `None` until a metadata fetch resolves them |
| `store.py` | `StyleLabStore`: all CRUD, per-channel dedupe (same external id or case-insensitive handle returns the existing active row), and **all owner scoping** (§4) |
| `metadata.py` | YouTube Data API v3 metadata fetch behind the credential gate (§7) |
| `service.py` | orchestration the router talks to: owner resolution, upload streaming/probing/storage, metadata-fetch persistence, the analysis seam, profile create/activate |
| *(no `media.py` — see §9)* | media validation/storage for uploads currently lives in the service's local ffprobe/Pillow fallback and in the analysis package's `app.stylelab.media` |

Router: `backend/app/routers/style_lab.py`, registered in
`backend/app/routers/api.py`. UI: "Style Lab" section in
`src/screens/Channels.tsx` (+ `src/api/queries/styleLab.ts`),
scoped to the selected channel.

Analysis package — `backend/app/stylelab/` (note: **no
underscore**; a separate sibling package): `engine.analyse_video()`
runs probe → scene detection → visual classification → speech →
dressing → thumbnail analysis and persists results via
`app.stylelab.storage` (§6). Media validation/storage helpers with
the content-addressed layout live in `app.stylelab.media`.

---

## 4. Owner scoping / isolation rules

Every competitor, video and profile belongs to exactly one owner
channel. The rules, enforced in `StyleLabStore` and re-enforced by
the service layer:

- `list_*` always filters by owner `channel_id`.
- Scoped reads of another channel's row return `None` / `[]` —
  cross-channel existence is indistinguishable from non-existence.
- Scoped writes on another channel's row raise
  `StyleLabScopeError`, which the service maps to **404 (never
  403)**.
- `create_profile` refuses competitor ids owned by another channel
  (`StyleLabScopeError` → API 422 `competitor_not_in_channel`) and
  unknown ids (`StyleLabNotFoundError` → API 422
  `competitor_not_found`). A profile can never span channels.
- The owner path segment accepts the internal `channels.id` **or**
  the channel's YouTube channel id (resolved to the internal id);
  an unknown owner is 404 `channel_not_found` on every endpoint.

Dedicated adversarial proof:
`backend/tests/test_style_lab_isolation.py` (API matrix + store
level), alongside `test_style_lab_api.py`, `test_style_lab_store.py`
and the end-to-end flow in `test_style_lab_e2e.py`.

---

## 5. Endpoints

All under `/api/v1`, nested by owner channel, guarded by the
launch-token middleware. Lists answer
`{"items": [...], "next_cursor": null}`; creates answer 201;
failures use the standard `ApiError` envelope.

| method & path | purpose |
|---|---|
| `POST /channels/{channel_id}/competitors` | add competitor from `input` (URL / `@handle` / UC id / video URL; aliases `url`, `channel_url`, `handle`, `raw_input`; optional `display_name`). 422 `invalid_competitor_input` + `details.reason` on unparsable input |
| `GET /channels/{channel_id}/competitors` | list (`?include_archived=true` to include archived) |
| `GET /channels/{channel_id}/competitors/{competitor_id}` | get one |
| `POST /channels/{channel_id}/competitors/{competitor_id}/archive` | archive (drops out of default lists) |
| `POST /channels/{channel_id}/competitors/{competitor_id}/metadata/fetch` | fetch public metadata (§7); honest 200 with `metadata_status` + `fetch_status`/`fetch_reason` |
| `GET /channels/{channel_id}/competitors/{competitor_id}/videos` | list a competitor's videos |
| `POST /channels/{channel_id}/competitors/{competitor_id}/videos` | attach by metadata: `source` = `url_metadata` \| `pasted_metadata`, plus `title`/`description`/`tags`/`video_url`/`external_video_id`. Needs at least one of title / URL / id. Never blocked on a fetch |
| `POST /channels/{channel_id}/competitors/{competitor_id}/videos/attach` | attach a FILE (multipart: `video`, optional `thumbnail`, `title`, `description`). Streamed to disk in 1 MiB chunks, probed, hashed. Invalid media → 422 with machine-readable `details.reason`, and leaves **no row and no stored file**. Limits: video 2 GiB, thumbnail 25 MiB (413 past the limit) |
| `POST /channels/{channel_id}/competitor-videos/{video_id}/analysis` | **queue analysis** — the intake→engine seam (§6) |
| `POST /channels/{channel_id}/style-profiles` | create profile: `name`, `competitor_ids`, optional `profile`/`profile_json` payload, optional `version`, `status: "active"` to create-and-activate |
| `GET /channels/{channel_id}/style-profiles` | list the channel's profiles |
| `POST /channels/{channel_id}/style-profiles/{profile_id}/activate` | activate; the channel's previously active profile becomes `superseded` in the same transaction |

There is deliberately **no** endpoint that fetches, downloads or
proxies a competitor's video from YouTube.

---

## 6. Storage layout

App data dir = `app.config.Settings.data_dir` (`YTA_DATA_DIR`,
default `~/.youtube-agent`); the SQLite DB is `<data_dir>/app.db`.

**Attached files (as built by the intake service,**
`app.style_lab.service.attach_video_file`**):**

```
<data_dir>/style_lab/
    tmp/                                   # streamed uploads land here first, always cleaned up
    competitors/<competitor_id>/
        <uuid>-<sanitised original filename>          # the video
        <uuid>-thumb-<sanitised thumbnail filename>   # optional thumbnail
```

`competitor_videos.storage_path` / `thumbnail_storage_path` store
the path **relative to `<data_dir>`** (e.g.
`style_lab/competitors/<competitor_id>/<uuid>-clip.mp4`); the row's
`file_sha256` is the authoritative content identity. A failed
validation deletes any partially stored file — disk and DB never
disagree about an attach.

**Analysis results (analysis package,** `app.stylelab.storage`**):**

```
<data_dir>/stylelab/
    index.json                                  # one entry per analysed video
    <slug of competitor_video_id>/
        analysis.json                           # StyleLabAnalysis payload
        breakdown.md                            # human-readable breakdown
```

**Content-addressed layout (defined, not yet wired into intake —**
see §9): `app.stylelab.media.store_video_file` /
`store_thumbnail` store to
`<data_dir>/style_lab/<owner_channel_id>/<competitor_id>/<sha256><ext>`
(thumbnail: `<sha256>-thumb<ext>`), with path-escape checks.

---

## 7. The analysis job contract

This is the hand-off between intake (this document's tables) and
the analysis team (`app.stylelab`).

1. **Queue.** Intake performs exactly one write:
   `POST …/competitor-videos/{video_id}/analysis` →
   `StyleLabStore.set_analysis_status(video_id, "queued")`, which
   also stamps `analysis_requested_at`. No other intake code path
   mutates `analysis_status`, and intake never runs analysis
   itself.
2. **Claim.** The analysis side claims rows with
   `analysis_status = 'queued'` (index
   `idx_competitor_videos_analysis_status` exists for this scan)
   and sets them to `'analysing'` via
   `StyleLabStore.set_analysis_status(video_id, "analysing",
   channel_id=<owner>)` — always passing the owner `channel_id`
   from the competitor row, so the scope check stays in force.
3. **Success.** Write the results (§6 analysis layout:
   `analysis.json` + `breakdown.md` + `index.json` entry, keyed by
   `competitor_videos.id`, carrying the owner `channel_id`), then
   set `analysis_status = 'analysed'`. If the analysis distilled a
   style profile, it lands as a **new `style_profiles` row** for
   that channel with the payload in `profile_json` and
   `competitor_ids_json` naming the source competitors (created
   via the store/service, never by raw cross-channel inserts).
4. **Failure.** Set `analysis_status = 'analysis_failed'`. A
   partial/uncertain analysis is *not* a failure by default — the
   engine records per-stage provenance and falls back to explicit
   "uncertain" results; `'analysis_failed'` is for when the engine
   decides nothing usable could be produced.
5. **Versioning rule.** New analysis → **new profile version**,
   never an in-place rewrite: create a fresh `style_profiles` row
   (`version` = previous latest + 1 for that channel/name lineage,
   status `draft`). Activation is the only status transition that
   retires a profile: activating a profile moves the channel's
   previously `active` profile to `superseded` in the same
   transaction, so a channel has **at most one active profile** at
   any time.

**As-built status of the claim loop (updated by the closure round,
2026-10-23 — SL-002 resolved):** the claimer is wired.
`app.stylelab.claimer.CompetitorAnalysisClaimer` polls for `'queued'`
rows and is hosted by the sidecar execution service
(`ExecutionService` runs it as the `yta-stylelab-claimer` task;
cancelled/awaited on `stop()` like the scheduler task). It claims via
a status-guarded conditional UPDATE (double-claim impossible), runs
`analyse_video(...)` with results persisted through the §6 file
layout, and flips the row to `'analysed'` — or, on any exception,
to `'analysis_failed'` after exactly one attempt, with the reason
recorded at `<data_dir>/stylelab/<video-id>/analysis_failure.json`.
Persisted `stop_all`/`pause_all` flags are re-read before every
claim and block claiming (`stop_all` also ends the loop), mirroring
the job runner. The engine's storage module still deliberately does
not write the intake tables — the claimer owns the
`set_analysis_status` flips, with the owner `channel_id` always
passed. See `docs/STYLELAB_BUGS.md` SL-002 for the full resolution
and test evidence.

---

## 8. Credential gate for metadata fetch

`app.style_lab.metadata.fetch_channel_metadata` is the **only**
Style Lab code path that talks to YouTube, and it is
metadata-only by construction: `channels.list` / `videos.list` on
`https://www.googleapis.com/youtube/v3`, one attempt per call, no
retries.

The gate, consulted in order, **before any HTTP**:

1. **API key** — secret ref `youtube/api_key` via the app secret
   provider (production: OS keyring; dev-only env backend:
   `YOUTUBE_API_KEY`). A credential-store failure counts as "not
   configured".
2. **OAuth access token** — only when a caller supplies a token
   manager *and* a connected-channel id (the intake router does not
   supply these today, so in practice the API key is the gate).

Outcomes, all persisted on the competitor row by the service:

- no usable credential → `metadata_status =
  'metadata_unavailable'`, reason `no_youtube_credential`, and
  **zero HTTP requests are made**;
- API answered → `'fetched'`; identity fields (`display_name`,
  `external_channel_id`, `handle`, `channel_url`) and counts are
  filled in, payload kept in `metadata_json`;
- API/transport failure → `'fetch_failed'` with a machine-readable
  reason (`quota_exceeded`, `invalid_api_key`, `not_found`,
  `network_error`, …); raw exceptions never leak to callers.

Resolved credentials are registered with the redaction choke
point before use and are never logged or returned.

Metadata attach (pasting a video's title/description/tags, or a
video URL) is a separate, always-available path — it is never
blocked on this gate.

---

## 9. Deviations from the original brief

Documented so the next round can reconcile deliberately rather
than discover them:

1. **No `app.style_lab.media` module.** The brief's module list
   (`app.style_lab.{schema, parsing, store, media, metadata,
   service}`) has no `media.py`; the media implementation lives in
   the analysis package as `app.stylelab.media`. The intake service
   imports `app.style_lab.media` defensively, gets `None`, and
   falls back to its own local ffprobe probe + Pillow thumbnail
   verification with the same validation contract — intake works,
   but there are two media implementations.
2. **Attached-file layout differs from the brief.** Brief:
   `<data_dir>/style_lab/<owner_channel_id>/<competitor_id>/<sha256><ext>`.
   Built (intake): `<data_dir>/style_lab/competitors/<competitor_id>/<uuid>-<filename>`
   (§6). The brief's content-addressed layout exists in
   `app.stylelab.media` but is not wired into the intake path, and
   the built layout has no owner-channel segment (ownership is
   recoverable via the DB row's competitor → channel chain).
3. **Two packages, two `StyleLabStore` classes.** Intake is
   `app.style_lab` (DB store); analysis is `app.stylelab` (engine +
   file-based results store), a naming collision the analysis
   package itself flags for the integration pass.
4. **Analysis results are file-based, not in `profile_json` by
   default.** `competitor_videos` has no analysis-payload column;
   results land in `<data_dir>/stylelab/…` (§6). The contract in
   §7 step 3 (distilled profile → new `style_profiles` row with
   `profile_json`) is the agreed seam, not yet an automated flow.
5. **No queued-row claimer yet** — see §7 "as-built status".
   *(Resolved by the closure round, 2026-10-23: the claimer is wired
   and hosted by the execution service — see §7 as-built status and
   `docs/STYLELAB_BUGS.md` SL-002.)*

---

## 10. Tests

| file | covers |
|---|---|
| `backend/tests/test_style_lab_parsing.py` | input parsing matrix + rejection reasons |
| `backend/tests/test_style_lab_schema.py` | DDL / migration parity |
| `backend/tests/test_style_lab_store.py` | store CRUD, dedupe, scoping, profiles |
| `backend/tests/test_style_lab_media.py` | `app.stylelab.media` validation/storage (real ffmpeg files) |
| `backend/tests/test_style_lab_metadata.py` | credential gate, honest states, mocked HTTP |
| `backend/tests/test_style_lab_api.py` | full endpoint matrix, attach, fetch (patched boundary), profiles |
| `backend/tests/test_style_lab_isolation.py` | dedicated adversarial cross-channel pass (API + store) |
| `backend/tests/test_style_lab_e2e.py` | one unpatched end-to-end intake flow incl. the no-credential honest state |
| `backend/tests/stylelab/`, `backend/tests/integration/test_style_profile*.py` | analysis engine + generation-side profile consumption |
