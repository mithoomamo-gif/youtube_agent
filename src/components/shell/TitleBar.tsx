import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Gauge,
  Minus,
  OctagonX,
  Play,
  Square,
  X,
  MonitorPlay,
} from "lucide-react";

const HOLD_DURATION_MS = 600;
const HOLD_TICK_MS = 20;

type WindowAction = "minimize" | "toggleMaximize" | "close";

async function runWindowAction(action: WindowAction) {
  try {
    // Lazy import so plain-browser dev (no Tauri runtime) never crashes.
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    if (action === "minimize") {
      await win.minimize();
    } else if (action === "toggleMaximize") {
      await win.toggleMaximize();
    } else {
      await win.close();
    }
  } catch {
    // Not running inside Tauri (e.g. `vite dev` in a browser) — no-op.
  }
}

export function TitleBar() {
  const [holdProgress, setHoldProgress] = useState(0);
  const [stopped, setStopped] = useState(false);
  const intervalRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);

  const clearHoldTimer = () => {
    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  };

  const cancelHold = () => {
    clearHoldTimer();
    if (!stopped) {
      setHoldProgress(0);
    }
  };

  const startHold = () => {
    if (stopped) return;
    clearHoldTimer();
    startedAtRef.current = Date.now();
    setHoldProgress(0);
    intervalRef.current = window.setInterval(() => {
      const elapsed = Date.now() - startedAtRef.current;
      const progress = Math.min(100, (elapsed / HOLD_DURATION_MS) * 100);
      setHoldProgress(progress);
      if (elapsed >= HOLD_DURATION_MS) {
        clearHoldTimer();
        setHoldProgress(100);
        // Visual confirm state only for now.
        // TODO: call the sidecar kill switch via the API (POST /engine/stop-all)
        // so every running agent/job is halted, not just this UI state.
        setStopped(true);
      }
    }, HOLD_TICK_MS);
  };

  useEffect(() => clearHoldTimer, []);

  return (
    <header
      data-tauri-drag-region
      className="flex h-12 shrink-0 items-center gap-4 border-b border-line bg-panel/60 px-4"
    >
      {/* Brand */}
      <div className="flex items-center gap-2.5" data-tauri-drag-region>
        <div className="accent-gradient flex h-8 w-8 items-center justify-center rounded-lg">
          <Play className="h-4 w-4 fill-white text-white" />
        </div>
        <span className="font-semibold text-ink">YouTube Agent</span>
      </div>

      {/* Channel switcher (placeholder) */}
      <button
        type="button"
        className="flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-sm text-ink transition hover:text-ink"
      >
        <MonitorPlay className="h-4 w-4 shrink-0 text-danger" />
        <span className="whitespace-nowrap">Channel: TechExplained Hindi</span>
        <ChevronDown className="h-3.5 w-3.5 text-muted" />
      </button>

      {/* Autonomy dial (placeholder) */}
      <div className="hidden items-center gap-2 md:flex" data-tauri-drag-region>
        <Gauge className="h-5 w-5 shrink-0 text-accent2" />
        <div className="leading-tight">
          <p className="text-sm font-semibold text-ink">Autonomy: Supervised</p>
          <p className="hidden text-[11px] text-muted md:block">
            Human approval required
          </p>
        </div>
      </div>

      <div className="flex-1" data-tauri-drag-region />

      {/* STOP ALL — press-and-hold kill switch */}
      <button
        type="button"
        aria-pressed={stopped}
        title={stopped ? "All agents stopped" : "Press and hold to stop all agents"}
        onPointerDown={startHold}
        onPointerUp={cancelHold}
        onPointerLeave={cancelHold}
        onContextMenu={(event) => event.preventDefault()}
        className={`relative flex select-none items-center gap-2 overflow-hidden rounded-lg px-4 py-1.5 font-bold text-white transition ${
          stopped ? "bg-danger" : "bg-stop hover:brightness-110"
        }`}
      >
        {!stopped && holdProgress > 0 ? (
          <span
            className="absolute inset-y-0 left-0 bg-white/30"
            style={{ width: `${holdProgress}%` }}
          />
        ) : null}
        <span className="relative flex items-center gap-2">
          {stopped ? (
            <Check className="h-4 w-4" />
          ) : (
            <OctagonX className="h-4 w-4" />
          )}
          {stopped ? "ALL STOPPED" : "STOP ALL"}
        </span>
      </button>

      {/* Window controls */}
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-label="Minimize window"
          onClick={() => void runWindowAction("minimize")}
          className="flex h-8 w-9 items-center justify-center rounded-md text-ink2 transition hover:bg-raised hover:text-ink"
        >
          <Minus className="h-4 w-4" />
        </button>
        <button
          type="button"
          aria-label="Maximize window"
          onClick={() => void runWindowAction("toggleMaximize")}
          className="flex h-8 w-9 items-center justify-center rounded-md text-ink2 transition hover:bg-raised hover:text-ink"
        >
          <Square className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          aria-label="Close window"
          onClick={() => void runWindowAction("close")}
          className="flex h-8 w-9 items-center justify-center rounded-md text-ink2 transition hover:bg-stop hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}
