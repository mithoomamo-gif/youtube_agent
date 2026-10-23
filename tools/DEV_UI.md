# Browser UI dev — running the real backend without Tauri

The app normally runs inside the Tauri shell, which spawns the Python
sidecar and hands the WebView its connection (port + per-launch token)
over the `get_backend_connection` IPC call. For UI development in a
plain browser there is no shell, so Round 2 added a dev connection
flow: `tools/dev-ui.sh` starts the **real** sidecar with a fixed,
known launch environment, and Vite env vars tell the UI where it is.

No mock data is involved: every screen talks to the live FastAPI
backend, and the status pill reflects real probes (see below).

## 1. Start the backend

```bash
tools/dev-ui.sh
```

This:

- uses `backend/.venv`'s Python and the sidecar entrypoint from
  `backend/pyproject.toml` (`[project.scripts] youtube-agent-backend
  = "app.main:run"`; falls back to `python -m app.main` when the
  console script isn't installed),
- launches it in the background with this env contract:

  | Variable | Default | Why |
  |---|---|---|
  | `YTA_PORT` | `8790` | fixed port so the UI can find it |
  | `YTA_TOKEN` | `dev-token` | launch token; the UI must send the same one |
  | `YTA_BOOT_ID` | `dev-boot` | stable boot id for logs / WS events |
  | `YTA_DATA_DIR` | `$REPO/.dev-data` | DB + logs on the workspace disk — **not** `/tmp` |
  | `YTA_DETACHED` | `1` | disables the parent watchdog (no Tauri parent in dev) |

- waits for `GET /health/ready` to succeed,
- writes the pid to `.dev-data/sidecar.pid` and logs to
  `.dev-data/sidecar.log`,
- prints the exact command to start the UI.

Stop it with:

```bash
tools/dev-ui.sh --stop
```

## 2. Start the UI

In a second terminal, run exactly what the script printed:

```bash
VITE_YTA_PORT=8790 VITE_YTA_TOKEN=dev-token npm run dev
```

Then open http://localhost:1420.

How the UI finds the backend (`src/api/tauri.ts`):

1. **Tauri IPC first** — `get_backend_connection`. In the real app
   this always wins and the `VITE_YTA_*` vars are irrelevant.
2. **Vite env fallback** — only when IPC is unavailable (plain
   browser): `VITE_YTA_PORT` (required), `VITE_YTA_TOKEN` (defaults
   to `dev-token`), `VITE_YTA_HOST` (defaults to `127.0.0.1`) →
   `http://{host}:{port}/api/v1` and `ws://{host}:{port}/api/v1/ws`.
3. **Neither** → an explicitly unusable marker (port 0, empty token).
   The UI reports the backend as down instead of pretending.

The token is held in memory only — never in a URL, never in
localStorage. The WebSocket authenticates with a single-use ticket
minted over REST (`POST /api/v1/ws-ticket`), sent in the
`client.hello` frame.

If you changed `YTA_PORT`/`YTA_TOKEN` when starting the backend, pass
the **same** values as `VITE_YTA_PORT`/`VITE_YTA_TOKEN` to the UI.

## 3. What the status pill means

The pill in the bottom status bar is derived from two live signals
(`src/state/connection.ts`): a REST probe every ~10s
(`/health/ready`, falling back to `/api/v1/system/info`) and the
WebSocket state (fed by `src/state/liveSync.ts`).

| Pill | Meaning |
|---|---|
| **Connecting** | Nothing has been verified yet (initial state). |
| **Live** | REST probe succeeds **and** the WebSocket is open. Events invalidate React Query caches as they arrive. |
| **Degraded** | Exactly one signal is up — e.g. backend reachable but the socket is down/reconnecting ("Backend up · live updates reconnecting"), or the socket is open but REST stopped answering. Data shown may be stale; mutations still go over REST. |
| **Offline** | Both signals failing. The backend is unreachable (not started, crashed, wrong port, or wrong token). |

The bar also shows the backend's `boot_id` (first 8 chars) and
backend version once `/system/info` has answered — if those are
absent, they haven't been verified yet. When the backend restarts
(boot_id changes) or the server demands a snapshot, all active
queries are refetched automatically.

## 4. Troubleshooting

- **Pill stuck on "Offline", backend running** — port mismatch:
  `VITE_YTA_PORT` must equal the `YTA_PORT` the sidecar was started
  with. Check `.dev-data/sidecar.log` for the `listening` line.
- **401 responses / pill "Offline" with matching ports** — token
  mismatch: `VITE_YTA_TOKEN` must equal `YTA_TOKEN` (default
  `dev-token` on both sides). A wrong token fails `/health/ready`,
  `/api/v1/*`, and the WS ticket exchange alike.
- **"port 8790 is already in use"** — a previous sidecar (or another
  process) holds it. `tools/dev-ui.sh --stop` stops ours; otherwise
  start with a different port, e.g. `YTA_PORT=8791 tools/dev-ui.sh`,
  and use the printed `VITE_YTA_PORT=8791` for the UI.
- **`/tmp` full warnings** — this dev VM's `/tmp` can fill up. The
  dev flow deliberately keeps everything on the workspace disk:
  data dir `.dev-data/`, and the sidecar's `TMPDIR` is pointed at
  `.dev-data/tmp`. If you run the backend by hand, set
  `YTA_DATA_DIR` (and ideally `TMPDIR`) to a workspace path yourself.
- **Backend exits immediately** — read `.dev-data/sidecar.log`.
  Common causes: missing `YTA_TOKEN` (exit code 5 — the script always
  sets one), port bind failure (exit code 3), or a broken venv
  (recreate it: `python3 -m venv backend/.venv &&
  backend/.venv/bin/pip install -e backend/`).
- **Pill "Degraded" that never recovers** — the WebSocket is failing
  while REST works. Check the browser console for the ws-ticket call
  and the socket URL (`ws://127.0.0.1:8790/api/v1/ws`); a token
  mismatch shows up here as a 401 on `POST /api/v1/ws-ticket`.
- **Tauri app shows "Offline" in dev** — expected if the shell isn't
  running the sidecar: the UI only falls back to `VITE_YTA_*` in a
  plain browser. Inside Tauri, the shell owns the connection.
