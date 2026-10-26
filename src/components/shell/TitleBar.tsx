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

import { useChannels } from "../../api/queries/channels";
import {
  useEngineStatus,
  useResumeEngine,
  useStopAll,
} from "../../api/queries/engine";
import { useChannelSelectionStore } from "../../state/channelSelection";

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

function ChannelSwitcher() {
  const channelsQuery = useChannels();
  const channels = channelsQuery.data ?? [];
  const selectedChannelId = useChannelSelectionStore(
    (s) => s.selectedChannelId,
  );
  const setSelectedChannelId = useChannelSelectionStore(
    (s) => s.setSelectedChannelId,
  );
  const [open, setOpen] = useState(false);

  const current =
    channels.find((c) => c.id === selectedChannelId) ?? channels[0] ?? null;

  const label = channelsQuery.isLoading
    ? "Channel: …"
    : current
      ? `Channel: ${current.title ?? current.handle ?? "Untitled channel"}`
      : "No channel connected";

  const canSwitch = channels.length > 1;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => canSwitch && setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-sm text-ink transition hover:text-ink"
      >
        <MonitorPlay className="h-4 w-4 shrink-0 text-danger" />
        <span className="whitespace-nowrap">{label}</span>
        {canSwitch && <ChevronDown className="h-3.5 w-3.5 text-muted" />}
      </button>
      {open && canSwitch && (
        <>
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div className="absolute left-0 top-full z-20 mt-2 w-72 overflow-hidden rounded-xl border border-line bg-panel shadow-xl">
            {channels.map((channel) => (
              <button
                key={channel.id}
                type="button"
                onClick={() => {
                  setSelectedChannelId(channel.id);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-ink transition hover:bg-raised"
              >
                <span className="flex-1 truncate">
                  {channel.title ?? channel.handle ?? "Untitled channel"}
                </span>
                {channel.status && (
                  <span className="text-[11px] text-muted">
                    {channel.status}
                  </span>
                )}
                {current?.id === channel.id && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-accent2" />
                )}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function TitleBar() {
  const [holdProgress, setHoldProgress] = useState(0);
  const intervalRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);

  // STOP ALL is real since patch v22: the stopped state comes from
  // the backend's persisted execution flags (GET /engine/status), not
  // from local UI state, so every screen agrees and a reload cannot
  // "un-stop" the engine.
  const engineStatus = useEngineStatus();
  const stopAll = useStopAll();
  const resumeEngine = useResumeEngine();
  const stopped = engineStatus.data?.stopAll === true;

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
        // Hold complete — fire the real kill switch: the backend
        // sets the persisted stop_all flag (the runner cancels
        // in-flight work and exits) and cancels waiting jobs.
        stopAll.mutate();
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

      {/* Channel switcher — real: shows the selected channel (or the
          first one) and switches the app-wide selection. */}
      <ChannelSwitcher />

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

      {/* STOP ALL — press-and-hold kill switch (real since v22:
          fires POST /engine/stop-all; while the backend reports
          stop_all, the button becomes RESUME instead). */}
      {stopped ? (
        <button
          type="button"
          title="The engine is stopped. Click to clear the stop flag and restart the engine."
          disabled={resumeEngine.isPending}
          onClick={() => {
            setHoldProgress(0);
            resumeEngine.mutate();
          }}
          className="relative flex select-none items-center gap-2 overflow-hidden rounded-lg bg-danger px-4 py-1.5 font-bold text-white transition hover:brightness-110 disabled:opacity-60"
        >
          <span className="relative flex items-center gap-2">
            <Play className="h-4 w-4" />
            {resumeEngine.isPending ? "RESUMING…" : "RESUME"}
          </span>
        </button>
      ) : (
        <button
          type="button"
          aria-pressed={false}
          title="Press and hold to stop all agents"
          onPointerDown={startHold}
          onPointerUp={cancelHold}
          onPointerLeave={cancelHold}
          onContextMenu={(event) => event.preventDefault()}
          className="relative flex select-none items-center gap-2 overflow-hidden rounded-lg bg-stop px-4 py-1.5 font-bold text-white transition hover:brightness-110"
        >
          {holdProgress > 0 ? (
            <span
              className="absolute inset-y-0 left-0 bg-white/30"
              style={{ width: `${holdProgress}%` }}
            />
          ) : null}
          <span className="relative flex items-center gap-2">
            <OctagonX className="h-4 w-4" />
            {stopAll.isPending ? "STOPPING…" : "STOP ALL"}
          </span>
        </button>
      )}

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
