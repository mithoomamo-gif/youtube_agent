import { useConnectionStore, type ConnectionStatus } from "../../state/connection";

interface PillStyle {
  label: string;
  dotClass: string;
  textClass: string;
  /** Pulsing halo around the dot (live-ish states only). */
  ping: boolean;
}

const PILL: Record<ConnectionStatus, PillStyle> = {
  ready: { label: "Live", dotClass: "bg-ok", textClass: "text-ok", ping: true },
  degraded: { label: "Degraded", dotClass: "bg-warn", textClass: "text-warn", ping: true },
  down: { label: "Offline", dotClass: "bg-danger", textClass: "text-danger", ping: false },
  connecting: { label: "Connecting", dotClass: "bg-warn", textClass: "text-muted", ping: true },
  unknown: { label: "Connecting", dotClass: "bg-warn", textClass: "text-muted", ping: true },
};

/** Plain-language detail next to the pill, derived from the raw signals. */
function detailFor(
  status: ConnectionStatus,
  restOk: boolean | null,
  wsState: string,
): string {
  switch (status) {
    case "ready":
      return "Backend connected · live updates on";
    case "degraded":
      if (restOk === true) return "Backend up · live updates reconnecting";
      if (wsState === "open") return "Live updates up · backend not responding";
      return "Connection unstable";
    case "down":
      return "Backend unreachable · showing last known data";
    default:
      return "Contacting backend…";
  }
}

export function StatusBar() {
  const status = useConnectionStore((s) => s.status);
  const restOk = useConnectionStore((s) => s.restOk);
  const wsState = useConnectionStore((s) => s.wsState);
  const bootId = useConnectionStore((s) => s.bootId);
  const backendVersion = useConnectionStore((s) => s.backendVersion);

  const pill = PILL[status];

  return (
    <footer className="flex h-7 shrink-0 items-center gap-2 border-t border-line bg-panel px-4 text-[11px] text-muted">
      <span className="relative flex h-2 w-2 shrink-0">
        {pill.ping && (
          <span
            className={`absolute inline-flex h-full w-full animate-ping rounded-full ${pill.dotClass} opacity-75`}
          />
        )}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${pill.dotClass}`} />
      </span>
      <span className={pill.textClass}>{pill.label}</span>
      <span aria-hidden="true">·</span>
      <span>{detailFor(status, restOk, wsState)}</span>

      <div className="ml-auto flex items-center gap-3">
        {bootId && (
          <span className="tnum font-mono" title={`Backend boot_id: ${bootId}`}>
            boot {bootId.slice(0, 8)}
          </span>
        )}
        {backendVersion && <span className="tnum font-mono">v{backendVersion}</span>}
      </div>
    </footer>
  );
}
