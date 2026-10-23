# YouTube Agent — Backend Sidecar

Python 3.12 + FastAPI sidecar for the YouTube Agent Windows desktop app.
Loopback-only (`127.0.0.1`), authenticated with a per-launch token.
Protocol spec: `~/workspace/youtube-windows-agent/MASTER_ROADMAP.md` Part 4
and `roadmap-sections/31_backend_protocol.md`.

## Run

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
$env:YTA_TOKEN = "<43-char launch token>"; $env:YTA_BOOT_ID = "<uuid>"
$env:YTA_PORT = "0"            # 0 = pick a free loopback port
youtube-agent-backend          # or: python -m app.main
```

On startup the process prints exactly one structured stdout line —
`{"event":"listening","port":<port>,"boot_id":"<boot_id>"}` — which the
Tauri shell uses as its fast-path readiness signal, then polls
`GET /health/live` and `GET /health/ready`.

## Environment contract (the ONLY configuration channel)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `YTA_PORT` | no | `0` (free ephemeral port) | Port to bind on `127.0.0.1` |
| `YTA_TOKEN` | **yes** | — | Per-launch token; shell generates 32 random bytes, base64url (43 chars), fresh every launch. Sent by the UI as header `X-Local-Token` |
| `YTA_BOOT_ID` | no | random UUID hex | Stamps every log line + WS event for this backend process |
| `YTA_DATA_DIR` | no | `%APPDATA%\YouTubeAgent` (else `~/.youtube-agent`) | DB (`app.db`), logs and PID file live here |
| `YTA_PARENT_PID` | no | — | Shell PID for the parent watchdog |
| `YTA_LOG_LEVEL` | no | `info` | `debug` only from Settings → Diagnostics |
| `YTA_DETACHED` | no | `0` | `1` only for Task-Scheduler headless runs (disables parent watchdog) |

The token travels by environment variable only — never command line,
file, or URL — and is never persisted.

## Exit codes (shell restart logic keys off these)

| Code | Meaning |
|---|---|
| 0 | Clean shutdown |
| 2 | Fatal bind-scope error (non-loopback) — do not auto-restart |
| 3 | Port bind failure — shell retries with a fresh port |
| 4 | DB schema newer than this binary — do not restart, route to updater |
| 5 | Missing/invalid launch environment (e.g. no `YTA_TOKEN`) — do not restart |

## Tests

```bash
cd backend
.venv/bin/python -m pytest tests/test_config.py -q   # config + redaction
.venv/bin/python -m pytest -q                        # full suite
```

Secrets are scrubbed at write time by the single choke point in
`app/redaction.py` (Bearer tokens, `x-api-key`, Google `AIza…` keys,
`sk-…` keys, refresh tokens, and the launch token), wired into every
log handler via `app/logging_setup.py`.
