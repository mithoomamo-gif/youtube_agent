# Live provider harnesses (`tests/providers_live/`)

Real-API smoke tests for the provider adapters, kept OUT of the mocked
suite (`tests/providers/`). Rules for every harness in this folder:

- Marked with the pytest marker **`live`** — run them explicitly with
  `-m live`; they are never part of a normal CI run's pass/fail signal.
- **Skipped by default.** Each harness skips cleanly (with a reason) when
  its API-key environment variable is not set. No key = skip, never a
  failure, never a network call.
- **Keys are never hardcoded, committed, or logged.** In dev a key comes
  from an environment variable only; in production the same key is
  entered by the user in the app (see each provider's section).
- Each harness exercises the EXISTING adapter + shared components
  (no re-implemented HTTP calls), so a pass proves the real integration,
  not a copy of it.

> One section per provider below — append your provider's section here
> instead of creating a second README.

---

## Pixabay — `test_pixabay_live.py`

### Key the user must supply

| | |
|---|---|
| Environment variable (dev) | `PIXABAY_API_KEY` |
| Where to get it | A free Pixabay account → the API key shown on Pixabay's API/documentation page for your account |
| Adapter key reference | `pixabay_api_key` (the default `key_ref` of `PixabayProvider`) |

### What it unlocks

One real run of the shipping stock path:

1. `PixabayProvider.search("ocean", MediaType.VIDEO, SearchFilters(...))`
   — ONE real search call to the Pixabay videos API through the existing
   adapter (`app/providers/stock/pixabay.py`), returning real
   `Candidate` objects (the adapter is Pixabay-first in the stock chain;
   100 req/60 s, no monthly cap, results cached 24 h per Pixabay's terms).
2. A real **licence-ledger row** built from the first result via the
   existing ledger API (`build_row` + `InMemoryLicenceLedger.write_row`,
   `app/providers/stock/ledger.py`, licence from `pixabay_licence()`),
   asserting the row exists with provider `pixabay`, the asset's
   provider id / page URL / download URL, and the Pixabay Content
   License name, URL, snapshot date and terms hash — and that the
   render gate (`assert_renderable`) passes for that asset.

Without the key this proves nothing is broken: the test simply skips.

### How to run (dev)

```bash
cd backend
PIXABAY_API_KEY=<your key> .venv/bin/python -m pytest tests/providers_live/test_pixabay_live.py -m live -rs
# without the env var: 1 skipped, with the skip reason shown by -rs
```

Dev key handling: the harness hands the key to the adapter through
the existing env-based `EnvSecretProvider`
(`app.providers.secrets_env`, landed in Round 2) under the
`pixabay_api_key` ref, which resolves the `PIXABAY_API_KEY`
environment variable — adapters never read the environment themselves.
(On this sandbox VM the harness's httpx client also applies a local
proxy/TLS workaround — explicit `proxy=` from the env, `trust_env=False`,
system CA context — because a default client dies on the sandbox's
`no_proxy` entries with `httpx.InvalidURL: Invalid port: ':1]'`; see
the test module docstring.) The ledger row stores only the key
**reference** (`retrieved_by_key_id`), never the key value.

### Where the key goes in production

**Not** an environment variable. The user enters the Pixabay key in the
app's **Settings → Providers** screen; `PUT /api/v1/providers/pixabay/key`
(`app/routers/providers.py`) stores the value via `app/secrets_store.py`
in the OS credential store — **Windows Credential Manager via the
`keyring` package** (service name `YouTubeAgent`, keyring reference
`apikey/pixabay`). SQLite (`provider_credentials`) keeps only metadata —
the keyring reference and a last-4 hint — never the key itself, and no
API response ever returns the key value.

---

## Gemini — `test_gemini_live.py`

### Key the user must supply

| | |
|---|---|
| Environment variable (dev) | `GEMINI_API_KEY` |
| Where to get it | A Google Gemini API key (Google AI Studio / Google Cloud API key with the Gemini API — `generativelanguage.googleapis.com` — enabled) |
| Adapter key reference | `apikey/gemini` (the production keyring ref for Gemini — `app.routers.providers._keyring_ref("gemini")`, also shown in `app/secrets_store.py`) |

### What it unlocks

Two real, minimal calls through the shipping LLM path:

1. **Models list** — `GET /models` on the Gemini API, authenticated with
   the supplied key, asserting a non-empty Gemini model list that
   includes the model the app is configured to call. NOTE: the Round-1
   `GeminiAdapter` exposes no public models-list method (its only public
   call is `generate()`), so this one GET reuses the adapter's own
   plumbing — `adapter._api_key()` for the key, `adapter._client()` for
   the HTTP client, and `adapter.base_url or DEFAULT_BASE`
   (`https://generativelanguage.googleapis.com/v1beta`) — instead of
   re-implementing the adapter with raw HTTP elsewhere.
2. **Minimal completion** — `GeminiAdapter.generate(...)` itself (the
   existing adapter in `app/providers/llm/gemini.py`, no duplicate
   `generateContent` logic), with `max_output_tokens=1` — the smallest
   completion that can be requested — on the cheapest configured model:
   the T0 Gemini entry loaded from `backend/config/models.yaml` via
   `ModelRegistry` (currently `gemini-3.1-flash-lite`; no model ID is
   hardcoded in the test). Asserts a sane structured result: provider
   `gemini`, the registry model id, status `ok`, and non-zero input
   usage. (Exact reply text is not asserted: with a 1-token budget
   Gemini may spend it on a thought token and return empty/partial
   text.)

Cost of a run: effectively negligible — one list call plus one
1-output-token completion, nothing else. Without the key both tests
skip, so this proves nothing is broken and makes no network call.

### How to run (dev)

```bash
cd backend
GEMINI_API_KEY=<your key> .venv/bin/python -m pytest tests/providers_live/test_gemini_live.py -m live -rs
# without the env var: 2 skipped, with the skip reasons shown by -rs
```

Dev key handling: the harness reads `GEMINI_API_KEY` from the
environment and hands it to the adapter through the Round-2 env-backed
`SecretProvider` — `EnvSecretProvider`
(`app/providers/secrets_env.py`, built by a sibling worker; this
harness ships no loader of its own) — under the `apikey/gemini` ref,
which that loader maps to `GEMINI_API_KEY`. The adapter itself never
reads the environment (it only ever calls
`SecretProvider.get_secret(key_ref)`). The loader is constructed with
the fixture's value as its explicit environment mapping and
`dotenv_path=False`, so for this harness the process environment
variable is the only key source — no `.env` file is consulted.

### Where the key goes in production

**Not** an environment variable. The user enters the Gemini key in the
app's **Settings → Providers** screen; `PUT /api/v1/providers/gemini/key`
(`app/routers/providers.py`) stores the value via `app/secrets_store.py`
in the OS credential store — **Windows Credential Manager via the
`keyring` package** (service name `YouTubeAgent`, keyring reference
`apikey/gemini`). SQLite (`provider_credentials`) keeps only metadata —
the keyring reference and a last-4 hint — never the key itself, and no
API response ever returns the key value. At call time a keyring-backed
`SecretProvider` resolves `apikey/gemini` just-in-time for
`GeminiAdapter`, so production wiring is identical to this harness
apart from where the value is stored.

---

## edge-tts — `test_edge_tts_live.py`

### Key the user must supply

None — edge-tts is the free default TTS provider (unofficial Microsoft
Edge Read-Aloud endpoint, no key, no quota). Because there is no key,
the opt-in environment variable `YTA_LIVE_EDGE_TTS=1` plays the
gatekeeping role the key variables play above (same pattern as
`YTA_LIVE_WHISPER`): without it the tests skip cleanly and make no
network call, so a bare `-m live` run with no keys/opt-ins set is
all-skipped.

### What it unlocks

Three real calls through the shipping adapter
(`EdgeTTSAdapter`, `app/providers/tts/adapters.py`):

1. **Canary** — `adapter.canary("en-US", "en-US-GuyNeural")` performs a
   REAL one-line synthesis against `speech.platform.bing.com` and must
   return True (non-empty audio, positive duration).
2. **English synthesis** — a real `synthesize()` whose MP3 bytes are
   written to a file, converted to WAV with ffmpeg, and ffprobe-verified
   with duration > 0.
3. **Urdu voice** — `list_voices("ur-PK")` must include
   `ur-PK-AsadNeural`, and a short Urdu line synthesised with it must
   return real audio with duration > 0.

Environment notes (found in the Round-2 integration run on the sandbox
VM, handled inside the adapter): when the standard `*_proxy` env vars
are set, the adapter passes the proxy to edge-tts (aiohttp ignores env
proxies otherwise); when `SSL_CERT_FILE` is set, edge-tts's pinned
certifi-only SSL context is repointed at the default OS trust store so
TLS-intercepting egress proxies verify; and if aiohttp's strict
WebSocket handshake validation rejects a proxy-relayed `101`
(`Connection: close` instead of `Connection: Upgrade`), `synthesize`
falls back to a raw-socket transport that reuses edge-tts's own DRM /
SSML builders. On a normal direct connection none of these paths
engage and stock edge-tts behaviour is unchanged.

### How to run (dev)

```bash
cd backend
YTA_LIVE_EDGE_TTS=1 .venv/bin/python -m pytest tests/providers_live/test_edge_tts_live.py -m live -rs
# without -m live: 3 skipped by the folder conftest, no network call
# with -m live but without YTA_LIVE_EDGE_TTS: 3 skipped, no network call
```

> Harness note (Round 3): this folder's `conftest.py` also sanitises
> bracketed IPv6 entries (`[::1]`, …) in `NO_PROXY`/`no_proxy` for the
> pytest process only — httpx raises `InvalidURL: Invalid port: ':1]'`
> at client construction on those entries before any request can be
> made. The proxy variables themselves are untouched and external API
> hosts still route through them.

---

## faster-whisper — `test_whisper_live.py`

### Key the user must supply

None — transcription is a local model, not an API. The opt-in
environment variable `YTA_LIVE_WHISPER=1` plays the gatekeeping role
the API-key variables play above: without it the test skips, so the
normal suite never downloads a model or spends CPU on transcription.

### What it unlocks

One real transcription through the shipping wiring:

1. `app.providers.transcription.create_transcriber("tiny")` builds the
   subtitles pipeline's `WhisperTranscriber` with a provider-side
   `model_loader` (the wrapper's established injection seam — no
   pipeline file is modified). The loader constructs
   `faster_whisper.WhisperModel` with `download_root` pinned to
   **`<repo>/models/whisper/`** (overridable via
   `YTA_WHISPER_MODEL_DIR`) — model weights (~75 MB for `tiny`) live
   on the workspace disk, never `/tmp`.
2. It transcribes `fixtures/whisper_en.wav` (8.5 s of clean Piper
   `en_US-amy-medium` speech; exact source text in
   `fixtures/whisper_en.txt`) and asserts the provenance is
   `"whisper"` (real path, not the `alignment_stub` fallback), word
   timestamps are present, and the **WER ≤ 0.20** against the known
   source text (hand-rolled WER in
   `app.providers.transcription.wer` — no `jiwer` dependency). The
   Round-2 integration run measured WER = 0.000 on this fixture;
   0.20 leaves headroom for CPU/int8 variation (tiny scores ~12–15%
   on human LibriSpeech speech, and clean TTS is easier).

Requires `faster-whisper` in `backend/.venv`
(`pip install faster-whisper`, or the `transcription` extra in
`backend/pyproject.toml`, which also pins `av>=12,<14` —
faster-whisper 1.2.1 is incompatible with newer PyAV).

### How to run (dev)

```bash
cd backend
YTA_LIVE_WHISPER=1 .venv/bin/python -m pytest tests/providers_live/test_whisper_live.py -m live -rs
# without the env var: 1 skipped, with the skip reason shown by -rs
```
