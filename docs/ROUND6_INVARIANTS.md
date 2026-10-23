# ROUND 6 — Security Invariants Proof Pack

> Automated, re-runnable proofs for the roadmap Part 9 / Part 11
> security invariants, plus a precise gap log where the code does not
> (yet) meet the strict invariant reading. Produced in Round 6 against
> a working tree whose git HEAD is `c99e10e` (Round 5) **plus
> uncommitted Round-6 work from parallel teams** — the tree moved
> during the round (the OpenAPI surface grew 106 → 117 paths and the
> PyInstaller spec landed mid-round); every proof below enumerates or
> reads the live tree, so the results track the tree, not a snapshot.
>
> No production behaviour was changed by this pack. The only new
> files are the four test modules and this document.

## How to re-run

Serially, from `backend/` (parallel runs hit the known intermittent
write fault; re-run a write-flavoured failure standalone before
believing it):

```bash
cd backend
TMPDIR=/home/hatch/workspace/youtube-agent-app/.tmp-yta \
  .venv/bin/python -m pytest \
  tests/test_round6_invariants_secrets.py \
  tests/test_round6_invariants_loopback_auth.py \
  tests/test_round6_invariants_publish_gate.py \
  tests/test_round6_invariants_disconnect_signing.py \
  --basetemp=/home/hatch/workspace/youtube-agent-app/.tmp-yta/r6-final \
  -q -p no:cacheprovider
```

Use an **absolute** `--basetemp` path: `--basetemp=~/…` is not
tilde-expanded by the shell (it follows `=`) and silently breaks
every `tmp_path` fixture with a `FileNotFoundError` under
`backend/~/…`. Verified the hard way during this round.

Results at proof time (2026-10-23): **64 new tests, all passing** —
secrets 12, loopback auth 8, publish gate 32, disconnect+signing 12.
Directly affected pre-existing suites re-run serially and green:
`test_gates`, `test_auth_health`, `test_ws`,
`pipeline/test_publishing`, `pipeline/test_gates`,
`providers/test_youtube_api`, `test_channels_connect` (131 passed),
and the root L9 contract `tests/contract/test_no_secrets_in_repo.py`
(5 passed). The full backend suite was not re-run (per brief).

## Verdict summary

| # | Invariant | Verdict |
|---|-----------|---------|
| 1 | No secret ever in plaintext anywhere | **GAP** — tracked source, docs and outputs proven clean; one plaintext exception: the git-ignored dev `.env` (**R6-F1, HIGH**) |
| 2 | No unauthenticated request reaches any sidecar endpoint, including loopback | **PROVEN** — every OpenAPI operation + framework docs endpoints reject missing/invalid tokens; sole exemption `/health/live`, justified and pinned |
| 3 | No unsigned code executes (installer / sidecar / updates) | **GAP (configuration) at proof time; pubkey point RESOLVED in the closure pass (R6-F6 CLOSED)** — the updater public key is wired into `tauri.conf.json` (152 chars, byte-identical to `src-tauri/keys/updater-pubkey.txt`); signing itself is CI-side and unverifiable from this VM |
| 4 | Upload Agent cannot run without a stored in-scope OAuth grant AND QA/compliance PASS tokens | **PARTIAL** — PASS-token half PROVEN at four layers; no-grant refusal PROVEN; **scope half is a GAP (R6-F4, MEDIUM)**; token "staleness" is hash-bound only, never age-bound (**R6-F5, LOW**) |
| 5 | Disconnect = revoke remotely first, delete locally second | **GAP at the API layer (R6-F2, MEDIUM)** — the `TokenManager.disconnect` primitive provably revokes-before-deletes, but **no route calls it**; the HTTP disconnect only soft-marks the DB row. The primitive also deletes locally even when revoke fails (**R6-F3, LOW**, deliberate design) |

Findings register: R6-F1 … R6-F6 are detailed per invariant below.
Characterisation tests (names ending `__gap_r6_fN`) pin the current
gap behaviour as executable evidence: when a gap is fixed, its test
goes red and must be flipped **together with** the finding here —
that flip is the closure record.

---

## Invariant 1 — No secret ever in plaintext anywhere

**Proof:** `backend/tests/test_round6_invariants_secrets.py` (12 tests).
Extends the Round-1 L9 scanner (`tests/contract/test_no_secrets_in_repo.py`,
backend/src/prompts only, 3 patterns) to 13 prefix patterns (Google
API key, Google client secret, Google access/refresh token shapes,
OpenAI-style, Stripe, GitHub, AWS, Slack, JWT, Bearer values, private
key blocks, Tauri signing-key values) plus a high-entropy assignment
detector (secret-named variable, quoted value ≥16 chars, Shannon
entropy ≥ 3.5, placeholder-filtered), at deliberately oversensitive
thresholds. Positive controls prove the scanner detects planted
fragment-assembled fakes of each major shape.

**Scopes and results at proof time:**

| Scope | Files | Hits |
|---|---|---|
| Strict: `backend/app`, `src`, `src-tauri`, `prompts`, `tools`, `oauth-pages`, `.github`, `packaging`, `scripts`, `docs/`, root configs (incl. `.env.example`) | ~700 | **0** |
| Outputs: text artefacts under `outputs/`, `backend/outputs/` (.json/.md/.txt/.srt/.csv/.log/.html/.xml/.yaml/.yml) | 1,093 | **0** |
| Test trees: `backend/tests`, `tests` | ~200 | 9, all adjudicated below |

Outputs disclosure: files ≤ 2 MB are read in full; larger text files
contribute head+tail 128 KB samples (bound on runtime over 7.5 GB of
generated media packs). Secret material in outputs would live in the
small metadata/report files, which are always read in full.

**Test-tree adjudication** (a hit is excused only by an exact
(file, pattern) allowlist entry **and** a guard — fake marker in the
value, or a length below the real-token floor for its type):

| File:line | Pattern | Why it is a fixture |
|---|---|---|
| `backend/tests/test_channels_connect.py:36,38,116` | google access token shape | synthetic connect-flow fixtures, each < 40 chars (real ones ~100–250) |
| `backend/tests/test_comments_fetch.py:354` | Bearer value | mock-transport value carrying a fake marker |
| `backend/tests/test_style_lab_metadata.py:395` | Bearer value | mock-transport value carrying a fake marker |
| `backend/tests/test_analytics_ingestion_e2e.py:83` | high-entropy assignment | value carries a fixture marker |
| `backend/tests/test_config.py:94` | high-entropy assignment | synthetic JWT-shaped fixture, 48 chars (real JWTs 100+) |
| `backend/tests/test_data_dir_contract.py:188` | high-entropy assignment | synthetic launch-token fixture, 26 chars (production launch tokens are 64-char hex) |
| `tests/integration/test_approvals_persistence.py:104` | high-entropy assignment | synthetic launch-token fixture, 29 chars |

**Supporting proofs:** the providers API is write-only — a test PUTs
a distinctive fake key and asserts the value appears in **no**
response (PUT echo or list), only `configured` + `key_last4`
(`app/routers/providers.py`: PUT stores via `app.secrets_store`,
§14/§31 design); the `provider_credentials` DDL stores `keyring_ref`
+ metadata only, never a token/secret value column
(`backend/app/schema_ddl.py`).

### Finding R6-F1 — HIGH: plaintext OAuth client secret in repo-root `.env`

* **What:** `.env:5` — the `YOUTUBE_CLIENT_SECRET` assignment holds a
  real Google OAuth client secret (fingerprint `GOCS…`, value length
  35). `.env:4` holds the matching client ID (a public identifier,
  not a secret; no pattern hit).
* **Mitigations in place (all test-pinned):** the file is
  git-ignored (`.gitignore:63`), untracked (`git ls-files` has no
  `.env`), mode `0600`, and values were never recorded in memory or
  docs. It exists by a Round-3 user decision for local dev runs;
  the production path stores the pair in the OS keyring
  (`youtube/client_id` / `youtube/client_secret` refs) and the dev
  file is read only via the `YTA_SECRET_BACKEND=env` dev store.
* **Why it is still a finding:** invariant 1 as written admits no
  plaintext exception; a copied repo directory (zip, backup, VM
  image) carries this secret with it, silently.
* **Recommendation (parent/user decision, NOT actioned here):**
  rotate the client secret in Google Cloud, move the dev pair into
  the keyring-backed store, and delete `.env`; or formally accept
  the dev-only exception and record that acceptance against this
  finding. The governance test
  (`test_dot_env_is_ignored_untracked_restricted_and_contains_only_r6_f1`)
  fails loudly if a second secret ever appears in the file.

**Verdict: GAP (single, governed, HIGH-visibility exception).**

---

## Invariant 2 — No unauthenticated request reaches any endpoint

**Proof:** `backend/tests/test_round6_invariants_loopback_auth.py`
(8 tests), building on `app/middleware.py` (`LaunchTokenMiddleware`,
Host allowlist + Origin checks + constant-time token compare +
bad-token rate limiting) and the existing `test_auth_health.py`.

**Token header contract (updated in the Round-6 closure pass —
G2 / R6-018):** the launch token is accepted in **both**
presentations — the `X-Local-Token` header **and**
`Authorization: Bearer <token>` — each compared against the
expected token in constant time (`hmac.compare_digest`, the same
mechanism for both). If both headers are present, **either**
valid token authenticates (the Tauri shell's `mini_http` client
sends both with the same token). No other weakening: a request
with no token in either form is still `401 missing_token`, a
request whose presented token(s) all fail the comparison is
still `401 invalid_token`, and a non-Bearer `Authorization`
header counts as no token presented. At proof time the backend
accepted **only** `X-Local-Token` (`Authorization: Bearer
<token>` → `401 missing_token`; register item R6-018 in
`docs/ROUND6_BUGS.md`); the closure pass extended
`LaunchTokenMiddleware` and pinned the new contract with tests
in `test_auth_health.py` (Bearer valid accepted, Bearer invalid
→ `401 invalid_token`, `X-Local-Token` unchanged, both-headers
precedence, no-token → `401 missing_token`). The full sweep
below was re-run after the change and still passes: Bearer
support opens no unauthenticated path.

**Method:** the sweep enumerates the operation list from the app's
own `app.openapi()` at runtime (so new routes are swept
automatically) and probes every operation twice — no token, wrong
token — expecting `401` with the typed codes `missing_token` /
`invalid_token`. Probing is chunked 5 operations per freshly built
app because the middleware escalates >10 failures/60 s to `429` by
design; a 401 can therefore never be masked by the limiter.

**Results at proof time:** 117 paths / **131 operations**, all
rejecting both failure modes. Additionally proven:

* **Sole exemption, justified:** `GET /health/live`
  (`app/middleware.py:40`, `EXEMPT_PATHS = {"/health/live"}`) — the
  Tauri shell must poll liveness before the UI (and thus the token
  hand-off) exists; the response is exactly `{"status": "ok"}` and
  carries no data. The exemption set is pinned by test: adding any
  path to it turns this suite red until justified here. Everything
  else — including `/health/ready` — requires the token.
* **Framework endpoints are not side doors:** `/openapi.json`,
  `/docs`, `/redoc`, `/docs/oauth2-redirect` all return
  `401 missing_token` unauthenticated.
* **Fail-closed:** a request to a nonexistent path is rejected with
  401 before routing.
* **WebSocket boundary:** WS scope bypasses the HTTP middleware by
  design; auth is the single-use 60 s ticket presented in the
  `client.hello` frame (`app/routers/ws.py`). Bogus ticket → close
  4401; missing ticket → close 4401; minting a ticket
  (`POST /api/v1/ws-ticket`) itself requires the launch token.
* **Control:** with the valid token, `/health/ready` and
  `/api/v1/system/info` answer normally (the gate is a gate, not a
  wall).

**Verdict: PROVEN, no undocumented exceptions.**

---

## Invariant 3 — No unsigned code executes

Scope per brief: verify the configuration points exist and document
their state; do not implement signing. No Rust toolchain exists on
this VM and signing identities live in CI, so no build or signature
was (or could be) executed here.

| Config point | State at proof time |
|---|---|
| `src-tauri/tauri.conf.json` → `bundle.active`, targets NSIS, `externalBin: ["binaries/agent-backend"]` | **Configured** |
| `src-tauri/tauri.conf.json` → `plugins.updater.endpoints` | **Configured** — two HTTPS endpoints (project update host + GitHub Releases fallback), `windows.installMode: passive` |
| `src-tauri/tauri.conf.json` → `plugins.updater.pubkey` | **Configured (corrected in the closure pass — R6-F6 CLOSED).** At proof time this cell read "EMPTY STRING"; re-verification against the live tree found a **152-character** minisign public key wired in, **byte-identical** to `src-tauri/keys/updater-pubkey.txt` (length + exact-match compared; key value not reproduced here) |
| `src-tauri/keys/updater-pubkey.txt` | **Present** — 152-byte base64 minisign public-key container (decodes to an "untrusted comment" key file); generated by the packaging team, and **now copied into `tauri.conf.json`** (byte-identical, verified in the closure pass) |
| `src-tauri/Cargo.toml` | **Configured** — `tauri-plugin-updater`, `tauri-plugin-single-instance`, `tauri-plugin-process` declared; `src-tauri/src/lib.rs` initialises the updater plugin and states the signing private key lives only in CI secrets |
| `.github/workflows/release.yml` | **Configured (uncommitted Round-6 state)** — signing identities referenced **by secret name only** (Azure Artifact Signing vars, `WINDOWS_CERT_*` PFX fallback, `TAURI_SIGNING_PRIVATE_KEY` + password for the updater minisign key, with a separate backend-channel key); a probe job detects which are configured and **warns that artefacts will be UNSIGNED and not publishable as a signed release when none are**; pipeline order is sign-inner → build → sign installer/updater → `signtool verify` gate (per `docs/BUILD.md` §5, `docs/RELEASE.md` §1.1/§1.5, incl. key-escrow verification and "no unsigned beta" rule) |
| `packaging/yta_sidecar.spec` | **Configured** (landed mid-round) — PyInstaller `--onedir` (`exclude_binaries` + `COLLECT`), entry `backend/app/main.py`, `console=True`; explicitly never bundles any `.env`; companion `packaging/build_sidecar.sh`/`.ps1`, `packaging/ffmpeg-pin.json` |
| CI secret values / certificates | **Absent from the repo (correct)** — presence in the CI provider cannot be verified from this VM |

### Finding R6-F6 — MEDIUM → **CLOSED in the Round-6 closure pass (R6-006)**

**Original finding (stale):** `plugins.updater.pubkey` in
`src-tauri/tauri.conf.json` was reported EMPTY, with the generated
key at `src-tauri/keys/updater-pubkey.txt` "not yet copied into
`tauri.conf.json`" — with the pubkey empty, a Tauri updater
artefact could not be signature-verified by installed clients.

**Closure evidence (re-verified against the live tree in the
closure pass):** `plugins.updater.pubkey` in
`src-tauri/tauri.conf.json` is **152 characters** long and
**byte-identical** to the full contents of
`src-tauri/keys/updater-pubkey.txt` (152 chars; length and
exact-match compared — the key value itself is deliberately not
reproduced in this document). The wiring step the original
finding waited on has therefore already happened; the
config-point test
(`test_updater_pubkey_state_is_empty_or_a_valid_minisign_key` in
`test_round6_invariants_disconnect_signing.py`) accepts this
state and still fails on any malformed non-empty value.

**Honest caveat (unchanged):** whether the matching
`TAURI_SIGNING_PRIVATE_KEY` exists in CI secrets + offline
escrow (`docs/RELEASE.md` §1.5 — losing that key permanently
orphans the installed base) **cannot be verified from this
VM**; signing itself remains a CI-side act. That residual is a
verification limit, not a repo configuration gap.

**Verdict: R6-F6 CLOSED — updater pubkey wired and verified
byte-identical to the generated key file; CI-side signing
secrets remain unverifiable from this VM.**

---

## Invariant 4 — Upload cannot run without grant + PASS tokens

**Proof:** `backend/tests/test_round6_invariants_publish_gate.py`
(32 tests) using the existing fake-provider / state-machine fixtures'
patterns. Enforcement is layered; each layer is proven where it
actually lives:

| Layer | Where | Proven behaviour |
|---|---|---|
| L1 Manifest G0 | `app/pipeline/publishing/manifest.py:109` (`gate_problems`), `:125` (`freeze`) | `freeze()` raises typed `ManifestError` naming each missing QC/Copyright/Policy PASS token, and on licence-ledger < 100% |
| L2 Workflow preflight | `app/pipeline/publishing/workflow.py:202` (`_preflight`) | An unfrozen manifest lands the job in `BLOCKED_HUMAN`; an exploding provider proves **zero** provider calls. Any post-freeze edit (`derive_new_version`) clears all tokens + hash, so stale evidence blocks identically. Control: a properly frozen manifest flows through the same workflow to `POST_LIVE_DONE` |
| L3 Pipeline Gate B | `app/pipeline/gates/aggregator.py:199` (`verify_gate_b`), `app/pipeline/gates/tokens.py` | Missing → `MISSING_TOKEN`; bound to a different manifest hash → `MANIFEST_HASH_MISMATCH`; digest-tampered → `TOKEN_TAMPERED`; WARN with valid digest → `VERDICT_NOT_PASS`. Tokens issued for manifest v1 never satisfy v2 (hash-staleness enforced) |
| L4 Orchestration Gate B/C | `app/orchestration/gates.py:213` (`can_pass_gate_c`), `can_pass_gate_b`, `issue_pass_token` | Gate C passes only for agent **26** (Upload) and only when Gate B holds (Gate A + PASS tokens from agents 32/33/34 bound to the project's manifest hash); agents 1/25/27/32/35 are refused with a reason naming agent 26 |
| L5 OAuth grant | `app/providers/youtube/tokens.py` (`TokenManager`), `app/providers/youtube/client.py` (`YouTubeClient.request`) | No stored grant → typed `NeedsReauth` from `get_access_token`, and the API client surfaces it **before any HTTP** (mock transport records zero calls). A blob marked `needs_reauth` refuses without HTTP. A refresh answered `invalid_grant` raises `NeedsReauth` and flips the stored status to `needs_reauth` |

### Finding R6-F4 — MEDIUM: no local scope check on the stored grant

The invariant requires an **in-scope** grant. The granted scopes are
stored (`TokenManager` blob) and surfaced in `CredentialStatus`, and
the connect flow requests the right set (`CONNECT_SCOPES` =
youtube.upload + youtube, `app/providers/youtube/oauth.py`), but **no
code path ever compares the stored scopes against what an upload
requires** — not at token use, not in the API client, not in any
gate. Characterisation test
`test_l5_out_of_scope_grant_is_not_refused_locally__gap_r6_f4`
proves it end-to-end: a grant holding only the readonly scope yields
an access token and a successful API-client request. In production
the only backstop would be Google's own `403
insufficientPermissions` at upload time — a late, untyped failure
instead of a local typed refusal. **Recommendation:** check
`CONNECT_SCOPES ⊆ CredentialStatus.scopes` where the upload path
obtains its token (or in Gate C) and raise `NeedsReauth` otherwise.

### Finding R6-F5 — LOW: gate tokens have no age-based expiry

`GateToken.issued_at` is recorded but never compared against any
clock in verification (`verify_gate_b` / binding checks are purely
hash + digest). A token issued in 2020 satisfies Gate B today
(characterisation test `test_l3_gate_b_token_age_is_not_checked__gap_r6_f5`).
"Stale" is therefore enforced only in the manifest-edit sense
(L2/L3). If PASS evidence is ever meant to expire, an explicit
max-age must be added to the verification path.

**Verdict: PARTIAL — PASS-token half PROVEN; grant-existence
PROVEN; grant-scope GAP (R6-F4); token-age GAP (R6-F5).**
Note: the scheduled/DAG path's only publish action remains the
fake-provider simulation (`app/execution/publish_finaliser.py`,
`provider='fake'`, `simulated=True` — no real upload exists until
interactive channel sign-in), so the real-upload layers above are
the enforcement surface this invariant will stand on.

---

## Invariant 5 — Disconnect = revoke first, delete second

**Proof:** `backend/tests/test_round6_invariants_disconnect_signing.py`
(12 tests, shared with invariant 3).

**The primitive is proven.** `TokenManager.disconnect`
(`app/providers/youtube/tokens.py:277`): with a recording secret
store + mock transport, the observed order is exactly
`[revoke POST to Google's revoke endpoint, local delete]`, the blob
is gone afterwards, status reads `not_connected`, and disconnecting
a never-connected channel is a no-op with zero HTTP.

### Finding R6-F3 — LOW: local deletion is unconditional (by design)

If the revoke call fails (transport error or HTTP 500), the local
blob is **still deleted** — deletion sits in a `finally` block
(tokens.py, disconnect docstring: "the local blob is deleted even
if the revoke call fails — the user asked to disconnect this
machine"). Order (attempt-first) holds; the strict invariant
reading ("delete only after successful revoke") does not. This is
a deliberate, documented product decision, logged here so the
deviation is explicit rather than discovered. If it stands, the UI
should surface "revoked at Google" vs "removed locally only" as
distinct outcomes.

### Finding R6-F2 — MEDIUM: the HTTP disconnect route performs neither step

`DELETE /api/v1/channels/{channel_id}`
(`app/routers/channels.py:939`, `disconnect_channel`) only soft-marks
the channel row (`status='disconnected'`, `deleted_at` set). Its own
docstring admits the real flow is "(wired with the OAuth work)".
Spy-proof (`test_router_disconnect_never_revokes_or_deletes_secret__gap_r6_f2`):
with a seeded channel row, the route returns 200/disconnected while
`TokenManager.disconnect` is called **0 times** and
`secrets_store.delete_secret` is called **0 times** — and a
source-level pin confirms **nothing in `backend/app` calls
`TokenManager.disconnect` at all**. Net effect today: a user who
"disconnects" a channel in the UI leaves the Google grant
un-revoked **and** the refresh token still stored locally — the
exact state invariant 5 exists to prevent.
**Recommendation (for the parent to schedule; not actioned here):**
wire the route to resolve the channel's YouTube id, call
`TokenManager.disconnect` first, then delete the credential
metadata row + soft-mark the channel, and emit `channel.updated`.

**Verdict: GAP at the API layer (R6-F2); primitive order PROVEN;
delete-on-revoke-failure deviation logged (R6-F3).**

---

## Findings register (redacted)

| ID | Severity | Summary | Evidence |
|---|---|---|---|
| R6-F1 | **HIGH** | Real OAuth client secret in plaintext in repo-root `.env` (dev-only, git-ignored, 0600, untracked) | `.env:5`, fingerprint `GOCS…` (len 35); governance test in secrets pack |
| R6-F2 | MEDIUM | UI/API disconnect never revokes the Google grant nor deletes the stored credential; `TokenManager.disconnect` has no caller | `backend/app/routers/channels.py:939`; spy + source-pin tests |
| R6-F4 | MEDIUM | No local OAuth scope enforcement: an out-of-scope (readonly-only) grant is never refused before API/upload use | Characterisation test L5 in publish-gate pack; scopes stored/surfaced only (`tokens.py`, `channels.py` credential row) |
| R6-F6 | MEDIUM → **CLOSED** (closure pass, R6-006) | ~~Tauri updater `pubkey` empty in `tauri.conf.json`~~ — stale: the pubkey IS wired in (152 chars, byte-identical to `src-tauri/keys/updater-pubkey.txt`); CI signing secrets remain unverifiable from this VM | `src-tauri/tauri.conf.json` (`plugins.updater`), `src-tauri/keys/updater-pubkey.txt`, config-point tests |
| R6-F3 | LOW | `TokenManager.disconnect` deletes the local blob even when remote revoke fails (deliberate, code-documented) | `backend/app/providers/youtube/tokens.py:277`; characterisation tests |
| R6-F5 | LOW | Gate PASS tokens never expire by age; only manifest-hash binding invalidates them | `backend/app/pipeline/gates/aggregator.py:199`; characterisation test L3 |

No other secret-shaped material was found anywhere in the scanned
scopes. No finding was fixed, deleted or silently allowlisted by
this pack: gaps are pinned by characterisation tests and listed
here for the parent to schedule.
