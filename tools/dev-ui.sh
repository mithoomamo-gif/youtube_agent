#!/usr/bin/env bash
# dev-ui.sh — start the REAL FastAPI sidecar for browser-based UI dev.
#
# The React UI normally gets its backend connection from the Tauri
# shell over IPC. In a plain browser there is no shell, so this script
# runs the sidecar with a fixed, known launch environment and the UI
# picks the same values up from Vite env (see src/api/tauri.ts and
# tools/DEV_UI.md).
#
# Usage:
#   tools/dev-ui.sh           start the sidecar in the background
#   tools/dev-ui.sh --stop    stop the sidecar started by this script
#
# Env contract (all optional; defaults shown):
#   YTA_PORT=8790             port the sidecar binds on 127.0.0.1
#   YTA_TOKEN=dev-token       launch token (UI must send the same one)
#   YTA_BOOT_ID=dev-boot      boot id stamped on logs + WS events
#   YTA_DATA_DIR=$REPO/.dev-data   DB/logs/pid — on the workspace disk,
#                             deliberately NOT /tmp (which can be full
#                             or wiped; the data must survive restarts)
#   YTA_DETACHED=1            always set: disables the parent watchdog
#                             (there is no Tauri parent in dev)
#
# Sidecar entrypoint: backend/pyproject.toml [project.scripts]
# youtube-agent-backend = "app.main:run". When that console script is
# not installed in backend/.venv, we fall back to the equivalent
# `python -m app.main` (same run() function).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
CONSOLE_SCRIPT="$VENV_DIR/bin/youtube-agent-backend"

PORT="${YTA_PORT:-8790}"
TOKEN="${YTA_TOKEN:-dev-token}"
BOOT_ID="${YTA_BOOT_ID:-dev-boot}"
DATA_DIR="${YTA_DATA_DIR:-$REPO_ROOT/.dev-data}"
PID_FILE="$DATA_DIR/sidecar.pid"
LOG_FILE="$DATA_DIR/sidecar.log"
HEALTH_URL="http://127.0.0.1:${PORT}/health/ready"

mkdir -p "$DATA_DIR"

# ── --stop ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      # Give uvicorn a moment to drain, then force if still alive.
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.3
      done
      kill -9 "$PID" 2>/dev/null || true
      echo "Stopped dev sidecar (pid $PID)."
    else
      echo "Dev sidecar is not running (stale pid file removed)."
    fi
    rm -f "$PID_FILE"
  else
    echo "No dev sidecar pid file at $PID_FILE — nothing to stop."
  fi
  exit 0
fi

# ── Guards ────────────────────────────────────────────────────────────────
if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: backend venv python not found at $VENV_PY" >&2
  echo "Set the backend up first, e.g.:" >&2
  echo "  python3 -m venv backend/.venv && backend/.venv/bin/pip install -e backend/" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    if curl -sf -H "X-Local-Token: $TOKEN" "$HEALTH_URL" >/dev/null 2>&1; then
      echo "Dev sidecar already running (pid $OLD_PID, port $PORT)."
      echo ""
      echo "Run the UI with:"
      echo "  VITE_YTA_PORT=$PORT VITE_YTA_TOKEN=$TOKEN npm run dev"
      echo "Then open http://localhost:1420 — stop the sidecar with: tools/dev-ui.sh --stop"
      exit 0
    fi
    echo "ERROR: pid $OLD_PID from $PID_FILE is alive but not answering on port $PORT." >&2
    echo "It may be a different process. Stop it (tools/dev-ui.sh --stop) or pick another YTA_PORT." >&2
    exit 1
  fi
  rm -f "$PID_FILE" # stale
fi

# Port must be free (the sidecar exits code 3 on bind failure — fail
# here with a clearer message instead).
if "$VENV_PY" - "$PORT" <<'PYEOF'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
then
  :
else
  echo "ERROR: port $PORT is already in use on 127.0.0.1." >&2
  echo "Pick another port, e.g.:  YTA_PORT=8791 tools/dev-ui.sh" >&2
  echo "(and use the same port in the VITE_YTA_PORT it prints for the UI)." >&2
  exit 1
fi

# ── Launch ────────────────────────────────────────────────────────────────
if [[ -x "$CONSOLE_SCRIPT" ]]; then
  CMD=("$CONSOLE_SCRIPT")
else
  CMD=("$VENV_PY" -m app.main)
fi

echo "Starting dev sidecar on 127.0.0.1:$PORT (data dir: $DATA_DIR)…"
(
  cd "$BACKEND_DIR"
  # Keep the child's temp files on the workspace disk too — /tmp may
  # be full on dev VMs, and the pipeline writes scratch media.
  mkdir -p "$DATA_DIR/tmp"
  env \
    PYTHONPATH="$BACKEND_DIR" \
    TMPDIR="$DATA_DIR/tmp" \
    YTA_PORT="$PORT" \
    YTA_TOKEN="$TOKEN" \
    YTA_BOOT_ID="$BOOT_ID" \
    YTA_DATA_DIR="$DATA_DIR" \
    YTA_DETACHED=1 \
    nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
)
PID="$(cat "$PID_FILE")"

# ── Wait for readiness ────────────────────────────────────────────────────
READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "ERROR: sidecar exited during startup. Last log lines:" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -sf -H "X-Local-Token: $TOKEN" "$HEALTH_URL" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.5
done

if [[ "$READY" != "1" ]]; then
  echo "ERROR: sidecar did not become ready at $HEALTH_URL within 30s." >&2
  echo "Last log lines:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  echo "Leaving it running (pid $PID) for debugging; stop with tools/dev-ui.sh --stop" >&2
  exit 1
fi

echo "Sidecar ready (pid $PID, boot_id $BOOT_ID)."
echo "  health:  $HEALTH_URL"
echo "  log:     $LOG_FILE"
echo "  data:    $DATA_DIR (app.db, logs/)"
echo ""
echo "Now run the UI in another terminal:"
echo "  VITE_YTA_PORT=$PORT VITE_YTA_TOKEN=$TOKEN npm run dev"
echo "Then open http://localhost:1420 — the status pill should read \"Live\"."
echo "Stop the sidecar with: tools/dev-ui.sh --stop"
