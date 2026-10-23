/**
 * BackendGate — the §4.6 gate between the Tauri shell's backend
 * lifecycle and the dashboard.
 *
 * The dashboard must NEVER render against a non-READY backend in the
 * desktop app, so this component renders instead of it while the
 * bootstrap (src/api/bootstrap.ts) reports:
 *
 *   starting (spawning/starting) -> calm starting screen, spinner
 *   starting (draining/stopping) -> "Shutting down…" interstitial
 *   failed (crashed/failed/stopped) -> failure screen with the
 *       roadmap actions: View logs / Retry / Diagnostics, plus the
 *       shell's detail + exit code in plain language
 *   handshake out_of_sync (§4.7) -> blocking "components out of
 *       sync" screen; the action is Check for updates
 *
 * …and renders the dashboard when the phase is ready / degraded /
 * dev. Degraded additionally gets a persistent slim banner above the
 * dashboard: the app is usable, the connection is not trusted. A
 * pending shell update (src/api/updater.ts) gets the dismissible
 * UpdateBanner in the same above-dashboard slot.
 *
 * Diagnostics note: there is no dedicated in-app Diagnostics screen
 * in the app today (Settings has no diagnostics section), so the
 * Diagnostics action opens the logs folder — the same destination
 * the roadmap's failure screen points users at for diagnosis.
 */

import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpCircle,
  FolderOpen,
  Loader2,
  RefreshCw,
  Stethoscope,
} from "lucide-react";
import {
  noteBackendRetry,
  refreshBootstrapNow,
  registerBootstrapQueryClient,
  startBackendBootstrap,
  UI_API_MAJOR,
  useBackendBootstrapStore,
} from "../../api/bootstrap";
import { startUpdaterListener } from "../../api/updater";
import {
  checkForUpdates,
  openLogsFolder,
  retryBackendStart,
} from "../../api/tauri";
import { Button } from "../ui/Button";
import { UpdateBanner } from "./UpdateBanner";

/**
 * The shell's status `detail` is sometimes a machine-readable code
 * (e.g. `database_newer_than_binary` for exit code 4 — the shell
 * documents exact strings the UI routes on). Translate known codes
 * to plain language; free-text details pass through unchanged.
 */
const DETAIL_TEXT: Record<string, string> = {
  database_newer_than_binary:
    "Your saved data was made by a newer version of the app. Updating the app is the fix.",
  cold_start_timeout:
    "The engine took too long to start (over 30 seconds). Retrying often fixes this.",
  restart_budget_exhausted:
    "The engine kept stopping, so automatic restarts have been paused. The logs show why it stops.",
  bind_failure:
    "The engine could not claim a network port on this PC.",
  missing_launch_environment:
    "The engine was started without the information it needs — this points to an app installation problem.",
  fatal_config:
    "There is a problem in the engine's configuration, so it cannot start.",
};

function detailText(detail: string | null): string | null {
  if (!detail) return null;
  const known = DETAIL_TEXT[detail.trim().toLowerCase()];
  if (known) return known;
  // Unknown snake_case code: at least don't show raw underscores.
  if (/^[a-z0-9]+(_[a-z0-9]+)+$/.test(detail.trim())) {
    const words = detail.trim().replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1) + ".";
  }
  return detail;
}

/** Plain-language meaning of the documented backend exit codes (§4.6). */
function exitCodeExplanation(exitCode: number | null): string | null {
  if (exitCode === null) return null;
  switch (exitCode) {
    case 0:
      return "The engine stopped cleanly (exit code 0).";
    case 2:
      return "Exit code 2: a configuration or startup problem — the engine will not fix itself by retrying, so the logs are the fastest way to see what is wrong.";
    case 3:
      return "Exit code 3: the engine could not claim a network port on this PC.";
    case 4:
      return "Exit code 4: your saved data was made by a newer version of the app. Updating the app is the fix — do not reinstall an older version.";
    case 5:
      return "Exit code 5: the engine was started without the information it needs — this points to an app installation problem.";
    default:
      return `Exit code ${exitCode}: the engine stopped unexpectedly.`;
  }
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen flex-col items-center justify-center bg-app px-6 font-sans text-ink">
      <div className="w-full max-w-md">{children}</div>
    </div>
  );
}

function StartingScreen({ detail }: { detail: string | null }) {
  return (
    <Shell>
      <div className="flex flex-col items-center text-center">
        <Loader2 className="h-10 w-10 animate-spin text-accent2" />
        <h1 className="mt-5 text-xl font-semibold">
          Starting the YouTube Agent engine…
        </h1>
        <p className="mt-2 text-sm text-muted">
          {detail ?? "This usually takes a few seconds."}
        </p>
      </div>
    </Shell>
  );
}

function ShuttingDownScreen() {
  return (
    <Shell>
      <div className="flex flex-col items-center text-center">
        <Loader2 className="h-10 w-10 animate-spin text-muted" />
        <h1 className="mt-5 text-xl font-semibold">Shutting down…</h1>
        <p className="mt-2 text-sm text-muted">
          The engine is finishing its current work and saving state
          before it stops.
        </p>
      </div>
    </Shell>
  );
}

function FailureScreen() {
  const detail = useBackendBootstrapStore((s) => s.detail);
  const exitCode = useBackendBootstrapStore((s) => s.exitCode);
  const shellState = useBackendBootstrapStore((s) => s.shellState);
  const restartCount = useBackendBootstrapStore((s) => s.restartCount);
  const [actionError, setActionError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);

  const explanation = exitCodeExplanation(exitCode);
  const detailDisplay = detailText(detail);

  const runAction = async (action: () => Promise<void>) => {
    setActionError(null);
    try {
      await action();
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "That action isn't available right now.",
      );
    }
  };

  const handleRetry = async () => {
    setRetrying(true);
    await runAction(async () => {
      await retryBackendStart();
      noteBackendRetry();
      void refreshBootstrapNow();
    });
    setRetrying(false);
  };

  return (
    <Shell>
      <div className="rounded-2xl border border-danger/40 bg-panel p-6 shadow-panel">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-danger" />
          <div>
            <h1 className="text-lg font-semibold">
              {shellState === "crashed"
                ? "The YouTube Agent engine stopped unexpectedly"
                : "The YouTube Agent engine didn't start"}
            </h1>
            <p className="mt-1 text-sm text-ink2">
              Your videos, schedules and settings are saved on this PC
              and are not lost. You can retry, or open the logs to see
              what happened.
            </p>
          </div>
        </div>

        {detailDisplay ? (
          <p className="mt-4 rounded-md bg-raised p-3 text-sm text-ink2">
            {detailDisplay}
          </p>
        ) : null}
        {explanation ? (
          <p className="mt-2 text-sm text-muted">{explanation}</p>
        ) : null}
        {restartCount > 0 ? (
          <p className="mt-2 text-sm text-muted">
            The engine has already been restarted {restartCount}{" "}
            {restartCount === 1 ? "time" : "times"} this session.
          </p>
        ) : null}
        {actionError ? (
          <p className="mt-2 text-sm text-danger">{actionError}</p>
        ) : null}

        <div className="mt-5 flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => void handleRetry()} disabled={retrying}>
            <RefreshCw className="h-4 w-4" />
            {retrying ? "Retrying…" : "Retry"}
          </Button>
          <Button
            variant="outline"
            onClick={() => void runAction(openLogsFolder)}
          >
            <FolderOpen className="h-4 w-4" />
            View logs
          </Button>
          <Button
            variant="ghost"
            onClick={() => void runAction(openLogsFolder)}
          >
            <Stethoscope className="h-4 w-4" />
            Diagnostics
          </Button>
        </div>
        <p className="mt-3 text-xs text-muted">
          Diagnostics opens the logs folder — it holds the engine's
          log files, which show exactly why it stopped.
        </p>
      </div>
    </Shell>
  );
}

function OutOfSyncScreen() {
  const minUiApi = useBackendBootstrapStore((s) => s.minUiApi);
  const systemInfo = useBackendBootstrapStore((s) => s.systemInfo);
  const [actionError, setActionError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const handleCheck = async () => {
    setChecking(true);
    setActionError(null);
    try {
      const result = await checkForUpdates();
      if (result.state === "error") {
        setActionError(
          `Update check failed: ${(result.detail ?? "unknown error").replace(/_/g, " ")}`,
        );
      } else if (result.state === "available") {
        setActionError(
          `Update ${result.version ?? ""} is available — install it from the update banner or Settings → Updates.`,
        );
      }
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Update check isn't available right now.",
      );
    }
    setChecking(false);
  };

  return (
    <Shell>
      <div className="rounded-2xl border border-line bg-panel p-6 shadow-panel">
        <div className="flex items-start gap-3">
          <ArrowUpCircle className="mt-0.5 h-6 w-6 shrink-0 text-warn" />
          <div>
            <h1 className="text-lg font-semibold">
              Components out of sync
            </h1>
            <p className="mt-1 text-sm text-ink2">
              The engine on this PC needs a newer version of the app
              interface than this window provides
              {minUiApi
                ? ` (the engine requires interface v${minUiApi}, this window speaks v${UI_API_MAJOR})`
                : ""}
              . Updating the app brings them back in sync — there is
              nothing you need to fix by hand.
            </p>
            {systemInfo?.backendVersion ? (
              <p className="mt-2 font-mono text-xs text-muted tnum">
                Engine version {systemInfo.backendVersion}
              </p>
            ) : null}
          </div>
        </div>
        {actionError ? (
          <p className="mt-3 text-sm text-danger">{actionError}</p>
        ) : null}
        <div className="mt-5 flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => void handleCheck()} disabled={checking}>
            <ArrowUpCircle className="h-4 w-4" />
            {checking ? "Checking…" : "Check for updates"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              void openLogsFolder().catch(() => undefined);
            }}
          >
            <FolderOpen className="h-4 w-4" />
            View logs
          </Button>
        </div>
      </div>
    </Shell>
  );
}

export function BackendGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const phase = useBackendBootstrapStore((s) => s.phase);
  const shellState = useBackendBootstrapStore((s) => s.shellState);
  const detail = useBackendBootstrapStore((s) => s.detail);
  const handshake = useBackendBootstrapStore((s) => s.handshake);

  useEffect(() => {
    registerBootstrapQueryClient(queryClient);
    const stop = startBackendBootstrap();
    // Updater (§11.4): listen for the shell's background
    // `updater://update-available` events for the life of the gate.
    // No-op outside Tauri, so the banner can never appear in dev.
    const stopUpdater = startUpdaterListener();
    return () => {
      registerBootstrapQueryClient(null);
      stop();
      stopUpdater();
    };
  }, [queryClient]);

  // §4.7 hard floor: an out-of-sync handshake blocks everything else,
  // even a READY backend.
  if (handshake === "out_of_sync") return <OutOfSyncScreen />;

  if (phase === "failed") return <FailureScreen />;

  if (phase === "starting") {
    if (shellState === "draining" || shellState === "stopping") {
      return <ShuttingDownScreen />;
    }
    return <StartingScreen detail={detailText(detail)} />;
  }

  // ready | degraded | dev — the dashboard may render. Degraded keeps
  // a persistent slim banner so the unstable connection is never
  // silently trusted.
  return (
    <div className="flex h-screen flex-col">
      {phase === "degraded" ? (
        <div
          role="status"
          className="flex shrink-0 items-center gap-2 border-b border-warn/40 bg-raised px-4 py-1.5 text-xs text-warn"
        >
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          Engine connection unstable — retrying in the background. Data
          shown may be briefly out of date.
        </div>
      ) : null}
      <UpdateBanner />
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}
