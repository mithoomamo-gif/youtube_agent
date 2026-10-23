/**
 * UpdateBanner — the non-modal §11.4 update card, rendered app-level
 * by BackendGate above the dashboard (next to the degraded banner).
 *
 * It appears only when the shell has actually offered an update: a
 * background `updater://update-available` event or a foreground check
 * stored a pending update in src/api/updater.ts. Outside Tauri no
 * pending update can ever exist, so the banner never renders in a
 * plain-browser / dev build.
 *
 * Dismissing hides the banner for this session only (the dismissal
 * lives in the in-memory updater store — nothing is persisted).
 * Installing calls the shell's `install_update` command; on Windows
 * the install itself follows the shell's passive flow (progress is
 * shown by the installer, the app relaunches when done).
 */

import { ArrowUpCircle, X } from "lucide-react";
import {
  dismissUpdateBanner,
  installPendingUpdate,
  isUpdateBannerVisible,
  useUpdaterStore,
} from "../../api/updater";
import { Button } from "../ui/Button";

export function UpdateBanner() {
  const pendingUpdate = useUpdaterStore((s) => s.pendingUpdate);
  const visible = useUpdaterStore(isUpdateBannerVisible);
  const checkStatus = useUpdaterStore((s) => s.checkStatus);

  if (!visible || !pendingUpdate) return null;

  const installing = checkStatus.kind === "installing";
  const installError =
    checkStatus.kind === "install_done" && checkStatus.result.state === "error"
      ? (checkStatus.result.detail ?? "install_failed")
      : null;

  return (
    <div
      role="status"
      className="flex shrink-0 items-start gap-3 border-b border-accent/40 bg-raised px-4 py-2.5"
    >
      <ArrowUpCircle className="mt-0.5 h-4 w-4 shrink-0 text-accent2" />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink">
          <span className="font-medium">
            Update available — YouTube Agent {pendingUpdate.version}
          </span>
          {pendingUpdate.notes ? (
            <span className="text-ink2"> · {pendingUpdate.notes}</span>
          ) : null}
        </p>
        <p className="mt-0.5 text-xs text-muted">
          Installing downloads and verifies the update, then relaunches
          the app. On Windows the install follows the shell's passive
          flow — progress is shown by the installer, not here. Install
          when no video is rendering or uploading; in-flight work is
          never interrupted by a background check.
        </p>
        {installError ? (
          <p className="mt-1 text-xs text-danger">
            Install didn't happen: {installError.replace(/_/g, " ")}.
            The running app was left untouched — you can try again from
            Settings → Updates.
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={installing}
          onClick={() => void installPendingUpdate()}
        >
          <ArrowUpCircle className="h-3.5 w-3.5" />
          {installing ? "Installing…" : "Install update"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={installing}
          onClick={dismissUpdateBanner}
          aria-label="Dismiss update notification for this session"
        >
          <X className="h-3.5 w-3.5" />
          Dismiss
        </Button>
      </div>
    </div>
  );
}
