import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileText,
  Film,
  Image as ImageIcon,
  Info,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  ShieldCheck,
  WifiOff,
  X,
  XCircle,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ProgressBar } from "../components/ui/ProgressBar";
import {
  REJECT_REASONS,
  isAlreadyDecidedError,
  isApprovalGoneError,
  isOfflineError,
  useApprovals,
  useDecideApproval,
} from "../api/queries/approvals";
import type { ApprovalItemView, RejectReasonCode } from "../api/queries/approvals";
import type { ApiError } from "../api/types";

type Notice = { tone: "ok" | "warn" | "danger"; text: string };

function fmtClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable
  );
}

// ── Draft player (real <video> when the payload carries one) ─────────────

function DraftPlayer({ item }: { item: ApprovalItemView }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [currentLabel, setCurrentLabel] = useState("0:00");
  const [durationLabel, setDurationLabel] = useState<string | undefined>(
    item.draftDurationLabel,
  );

  const toggle = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      void video.play();
    } else {
      video.pause();
    }
  };

  // Space = play/pause draft (design keyboard map), when a draft exists.
  useEffect(() => {
    if (!item.draftUrl) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== " " || isTypingTarget(event.target)) return;
      event.preventDefault();
      toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const versionStamp = [item.draftVersion, item.draftAgentLabel]
    .filter(Boolean)
    .join(" · ");

  if (!item.draftUrl) {
    const isGateA = item.gate === "A";
    return (
      <Card className="p-4">
        <div className="relative flex aspect-video w-full flex-col items-center justify-center gap-2 rounded-lg bg-raised px-6 text-center">
          {isGateA ? (
            <FileText className="h-8 w-8 text-muted" />
          ) : (
            <Film className="h-8 w-8 text-muted" />
          )}
          <p className="text-sm font-medium text-ink">
            {isGateA ? "Draft renders after script lock" : "No draft preview attached"}
          </p>
          <p className="max-w-md text-xs leading-relaxed text-muted">
            {isGateA
              ? "This is a Gate A script review — the draft video is produced once the script is locked."
              : "This approval does not include a playable draft yet."}
            {item.draftDurationLabel ? ` Duration ${item.draftDurationLabel}.` : ""}
          </p>
          <span className="absolute left-3 top-3">
            <Badge tone="gray">{isGateA ? "Gate A · Script review" : "No draft"}</Badge>
          </span>
          {versionStamp && (
            <span className="absolute right-3 top-3 font-mono text-xs text-ink2">
              {versionStamp}
            </span>
          )}
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-4">
      <div className="relative flex aspect-video w-full items-center justify-center overflow-hidden rounded-lg bg-raised">
        <video
          ref={videoRef}
          className="absolute inset-0 h-full w-full object-contain"
          src={item.draftUrl}
          poster={item.draftPosterUrl}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onLoadedMetadata={(event) => {
            const video = event.currentTarget;
            if (Number.isFinite(video.duration)) {
              setDurationLabel(fmtClock(video.duration));
            }
          }}
          onTimeUpdate={(event) => {
            const video = event.currentTarget;
            if (Number.isFinite(video.duration) && video.duration > 0) {
              setProgress((video.currentTime / video.duration) * 100);
            }
            setCurrentLabel(fmtClock(video.currentTime));
          }}
        />
        {!playing && (
          <button
            type="button"
            aria-label="Play draft preview"
            onClick={toggle}
            className="accent-gradient relative flex h-16 w-16 items-center justify-center rounded-full text-ink shadow-lg"
          >
            <Play className="ml-1 h-7 w-7 fill-current" />
          </button>
        )}
        {playing && (
          <button
            type="button"
            aria-label="Pause draft preview"
            onClick={toggle}
            className="absolute bottom-3 right-3 flex h-9 w-9 items-center justify-center rounded-full bg-black/50 text-white"
          >
            <Pause className="h-4 w-4 fill-current" />
          </button>
        )}
        <span className="absolute left-3 top-3">
          <Badge tone="gray">Draft · 720p preview</Badge>
        </span>
        {versionStamp && (
          <span className="absolute right-3 top-3 font-mono text-xs text-ink2">
            {versionStamp}
          </span>
        )}
      </div>
      <div className="mt-3 flex items-center gap-3">
        <span className="tnum font-mono text-xs text-ink2">{currentLabel}</span>
        <div className="flex-1">
          <ProgressBar value={progress} />
        </div>
        <span className="tnum font-mono text-xs text-muted">
          {durationLabel ?? "—"}
        </span>
      </div>
    </Card>
  );
}

// ── Small render helpers ──────────────────────────────────────────────────

function checkVisual(status: string): { Icon: typeof CheckCircle2; className: string } {
  const s = status.toLowerCase();
  if (s.includes("pass") || s === "ok") return { Icon: CheckCircle2, className: "text-ok" };
  if (s.includes("fail") || s.includes("block")) return { Icon: XCircle, className: "text-danger" };
  return { Icon: AlertTriangle, className: "text-warn" };
}

function annotationVisual(tone: ApprovalItemView["annotations"][number]["tone"]): {
  Icon: typeof CheckCircle2;
  className: string;
} {
  switch (tone) {
    case "ok":
      return { Icon: CheckCircle2, className: "text-ok" };
    case "danger":
      return { Icon: XCircle, className: "text-danger" };
    case "warn":
      return { Icon: AlertTriangle, className: "text-warn" };
    default:
      return { Icon: Info, className: "text-muted" };
  }
}

// ── Screen ────────────────────────────────────────────────────────────────

export default function Review() {
  const approvalsQuery = useApprovals();
  const decide = useDecideApproval();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Items decided in this session are removed from the queue view at
  // once (the backend's skeleton decision log is in-memory and its list
  // endpoint does not filter decided rows out yet).
  const [decidedIds, setDecidedIds] = useState<ReadonlySet<string>>(new Set());
  const [rejectReason, setRejectReason] = useState<RejectReasonCode>(REJECT_REASONS[0].code);
  const [rejectNote, setRejectNote] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);

  const reasonSelectRef = useRef<HTMLSelectElement | null>(null);
  const noteRef = useRef<HTMLTextAreaElement | null>(null);

  const allItems = approvalsQuery.data ?? [];
  const visibleItems = allItems.filter((item) => !decidedIds.has(item.id));
  // Effective selection: the chosen item, else the oldest waiting item.
  // If the chosen item disappears (decided / refreshed away) the detail
  // resets gracefully to the next queue item instead of crashing.
  const selected =
    visibleItems.find((item) => item.id === selectedId) ?? visibleItems[0] ?? null;

  const visibleItemsRef = useRef<ApprovalItemView[]>([]);
  visibleItemsRef.current = visibleItems;
  const selectedRef = useRef<ApprovalItemView | null>(null);
  selectedRef.current = selected;

  const completeDecision = (id: string, verb: "approved" | "rejected", silent = false) => {
    const items = visibleItemsRef.current;
    const idx = items.findIndex((item) => item.id === id);
    const remaining = items.filter((item) => item.id !== id);
    const next =
      remaining.length > 0
        ? remaining[Math.min(Math.max(idx, 0), remaining.length - 1)]
        : null;
    setDecidedIds((prev) => {
      const nextSet = new Set(prev);
      nextSet.add(id);
      return nextSet;
    });
    setSelectedId(next ? next.id : null);
    if (!silent) {
      setNotice({
        tone: "ok",
        text:
          verb === "approved"
            ? "Approved. Moved to the next item in the queue."
            : "Rejected. Moved to the next item in the queue.",
      });
    }
  };

  const handleDecisionError = (id: string, err: ApiError) => {
    if (isAlreadyDecidedError(err)) {
      // 409 { error: { code: "already_decided" } } — someone (or another
      // window) decided this item first. Plain notice, no crash: drop
      // the item locally, refresh, and move on.
      setNotice({
        tone: "warn",
        text: "Already decided — this item was decided elsewhere, so it has been removed from your queue and the list refreshed.",
      });
      completeDecision(id, "approved", true);
    } else if (isApprovalGoneError(err)) {
      setNotice({
        tone: "warn",
        text: "That item is no longer in the queue. The list has been refreshed.",
      });
      completeDecision(id, "approved", true);
    } else if (err.code === "reason_code_required") {
      setNotice({
        tone: "danger",
        text: "The backend requires a reason code to reject. Pick one and try again.",
      });
      reasonSelectRef.current?.focus();
    } else {
      setNotice({
        tone: "danger",
        text: `The decision could not be recorded: ${err.message}`,
      });
    }
  };

  const handleApprove = () => {
    const target = selectedRef.current;
    if (!target || decide.isPending) return;
    decide.mutate(
      { id: target.id, decision: "approve" },
      {
        onSuccess: () => completeDecision(target.id, "approved"),
        onError: (err) => handleDecisionError(target.id, err),
      },
    );
  };

  const handleReject = () => {
    const target = selectedRef.current;
    if (!target || decide.isPending) return;
    if (rejectReason === "other" && rejectNote.trim() === "") {
      noteRef.current?.focus();
      return;
    }
    decide.mutate(
      {
        id: target.id,
        decision: "reject",
        reasonCode: rejectReason,
        note: rejectReason === "other" ? rejectNote.trim() : undefined,
      },
      {
        onSuccess: () => {
          completeDecision(target.id, "rejected");
          setRejectNote("");
        },
        onError: (err) => handleDecisionError(target.id, err),
      },
    );
  };

  const moveSelection = (delta: number) => {
    const items = visibleItemsRef.current;
    if (items.length === 0) return;
    const current = selectedRef.current;
    const idx = current ? items.findIndex((item) => item.id === current.id) : -1;
    const nextIdx = Math.min(items.length - 1, Math.max(0, (idx < 0 ? 0 : idx) + delta));
    setSelectedId(items[nextIdx].id);
  };

  const focusFirstEditable = () => {
    reasonSelectRef.current?.focus();
  };

  // Keyboard map (design, roadmap section 11): J/K next/prev item,
  // A approve, E first editable field, R reject (focuses the reason
  // select — this design's reject flow lives in the Decision Rail).
  const handlersRef = useRef({ handleApprove, handleReject, moveSelection, focusFirstEditable });
  handlersRef.current = { handleApprove, handleReject, moveSelection, focusFirstEditable };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      const handlers = handlersRef.current;
      switch (event.key.toLowerCase()) {
        case "j":
          event.preventDefault();
          handlers.moveSelection(1);
          break;
        case "k":
          event.preventDefault();
          handlers.moveSelection(-1);
          break;
        case "a":
          if (!event.repeat) handlers.handleApprove();
          break;
        case "e":
          event.preventDefault();
          handlers.focusFirstEditable();
          break;
        case "r":
          event.preventDefault();
          handlers.focusFirstEditable();
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ── Full-screen states (no data yet): loading / offline / error ──────

  if (!approvalsQuery.data) {
    const loadError = approvalsQuery.error;
    const offline = isOfflineError(loadError);
    return (
      <div className="flex min-h-full items-center justify-center bg-app p-6 text-ink">
        <Card className="flex w-full max-w-md flex-col items-center gap-3 p-8 text-center">
          {approvalsQuery.isError ? (
            <>
              {offline ? (
                <WifiOff className="h-8 w-8 text-warn" />
              ) : (
                <AlertTriangle className="h-8 w-8 text-danger" />
              )}
              <h2 className="text-base font-semibold text-ink">
                {offline ? "Backend offline" : "Couldn't load the approval queue"}
              </h2>
              <p className="text-sm leading-relaxed text-muted">
                {offline
                  ? "The local backend isn't responding, so the approval queue can't be loaded. Nothing here is cached or sample data — the queue will appear once the backend is back."
                  : `The backend returned an error (${(loadError as ApiError | null)?.code ?? "unknown"}). The queue has not been loaded.`}
              </p>
              <Button variant="outline" onClick={() => void approvalsQuery.refetch()}>
                <RefreshCw className="h-4 w-4" />
                Retry
              </Button>
            </>
          ) : (
            <>
              <Loader2 className="h-8 w-8 animate-spin text-accent2" />
              <p className="text-sm text-muted">Loading approval queue…</p>
            </>
          )}
        </Card>
      </div>
    );
  }

  const pending = decide.isPending;
  const rejectNeedsNote = rejectReason === "other";
  const rejectDisabled = pending || !selected || (rejectNeedsNote && rejectNote.trim() === "");
  const approveLabel =
    selected?.gate === "A"
      ? "Approve Script"
      : selected?.gate === "B"
        ? "Approve & Publish"
        : "Approve";

  return (
    <div className="flex min-h-full flex-col gap-4 bg-app p-6 text-ink">
      {notice && (
        <div
          role="status"
          className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-sm ${
            notice.tone === "ok"
              ? "border-ok/40 bg-ok/10"
              : notice.tone === "warn"
                ? "border-warn/40 bg-warn/10"
                : "border-danger/40 bg-danger/10"
          }`}
        >
          {notice.tone === "ok" ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
          ) : notice.tone === "warn" ? (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
          )}
          <p className="flex-1 leading-relaxed text-ink">{notice.text}</p>
          <button
            type="button"
            aria-label="Dismiss notice"
            onClick={() => setNotice(null)}
            className="shrink-0 text-muted hover:text-ink"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      <div className="flex gap-4">
        {/* Left: Approval Queue */}
        <Card className="w-72 shrink-0 self-start p-4">
          <div className="mb-1 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-ink">Approval Queue</h2>
            <Badge tone="violet">{visibleItems.length} waiting</Badge>
          </div>
          <p className="mb-4 flex items-center gap-1.5 text-xs text-muted">
            <ShieldCheck className="h-3.5 w-3.5 text-accent2" />
            Gate B items never auto-approve.
          </p>
          {visibleItems.length === 0 ? (
            <p className="text-xs leading-relaxed text-muted">
              No items waiting. New gate items will appear here as agents finish
              their steps.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {visibleItems.map((item) => {
                const isSelected = selected?.id === item.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedId(item.id)}
                    className={`rounded-lg border bg-raised p-3 text-left transition-colors ${
                      isSelected
                        ? "border-accent"
                        : "border-line hover:border-accent/50"
                    }`}
                  >
                    <p className="text-sm font-medium leading-snug text-ink">
                      {item.title}
                    </p>
                    {(item.channelLabel || item.nicheLabel) && (
                      <p className="mt-1 text-xs text-ink2">
                        {[item.channelLabel, item.nicheLabel]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    )}
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <Badge tone="violet">{item.gateLabel}</Badge>
                      {item.waitingLabel ? (
                        <span className="tnum flex items-center gap-1 text-xs text-muted">
                          <Clock className="h-3 w-3" />
                          {item.waitingLabel}
                        </span>
                      ) : item.status ? (
                        <span className="text-xs text-muted">{item.status}</span>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </Card>

        {!selected ? (
          /* Empty queue — the designed "all caught up" state */
          <Card className="flex flex-1 flex-col items-center justify-center gap-3 self-start p-12 text-center">
            <CheckCircle2 className="h-10 w-10 text-ok" />
            <h2 className="text-base font-semibold text-ink">All caught up</h2>
            <p className="max-w-sm text-sm leading-relaxed text-muted">
              Queue clear. Agents are working — new items will appear here.
            </p>
            {decidedIds.size > 0 && (
              <p className="text-xs text-muted">
                {decidedIds.size} item{decidedIds.size === 1 ? "" : "s"} decided this
                session.
              </p>
            )}
          </Card>
        ) : (
          <>
            {/* Center: player, variants, thumbnails, script */}
            <div className="flex min-w-0 flex-1 flex-col gap-4">
              <DraftPlayer key={selected.id} item={selected} />

              <Card className="p-4">
                <h3 className="mb-3 text-sm font-semibold text-ink">Scene variants</h3>
                {selected.sceneVariants.length === 0 ? (
                  <p className="text-sm text-muted">
                    No scene variants are attached to this approval.
                  </p>
                ) : (
                  <div className="flex gap-3">
                    {selected.sceneVariants.map((variant) => (
                      <div
                        key={variant.id}
                        className={`flex-1 rounded-lg border p-2 text-left ${
                          variant.selected
                            ? "border-accent bg-raised"
                            : "border-line bg-raised"
                        }`}
                      >
                        <div className="flex aspect-video items-center justify-center rounded bg-panel text-lg font-semibold text-ink2">
                          {variant.label.slice(0, 1).toUpperCase()}
                        </div>
                        <div className="mt-2 flex items-center justify-between">
                          <span className="text-xs font-medium text-ink">
                            {variant.label}
                          </span>
                          {variant.durationLabel && (
                            <span className="tnum font-mono text-xs text-muted">
                              {variant.durationLabel}
                            </span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card className="p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-ink">Thumbnails</h3>
                  {selected.thumbnailVersion && (
                    <Badge tone="gray">Thumbnail {selected.thumbnailVersion}</Badge>
                  )}
                </div>
                {selected.thumbnails.length === 0 ? (
                  <p className="flex items-center gap-2 text-sm text-muted">
                    <ImageIcon className="h-4 w-4 shrink-0" />
                    No thumbnail variants are attached to this approval.
                  </p>
                ) : (
                  <div className="flex gap-3">
                    {selected.thumbnails.map((thumb) => (
                      <div
                        key={thumb.id}
                        className={`flex-1 rounded-lg border p-2 text-left ${
                          thumb.selected
                            ? "border-accent bg-raised"
                            : "border-line bg-raised"
                        }`}
                      >
                        {thumb.imageUrl ? (
                          <img
                            src={thumb.imageUrl}
                            alt={thumb.label}
                            className="aspect-video w-full rounded object-cover"
                          />
                        ) : (
                          <div className="flex aspect-video items-center justify-center rounded bg-panel text-lg font-semibold text-ink2">
                            {thumb.label.slice(0, 1).toUpperCase()}
                          </div>
                        )}
                        <div className="mt-2 flex items-center justify-between gap-2">
                          <span className="text-xs font-medium text-ink">
                            {thumb.label}
                          </span>
                          {thumb.score !== undefined ? (
                            <span className="tnum font-mono text-xs text-muted">
                              CTR {thumb.score}
                            </span>
                          ) : thumb.text ? (
                            <span className="truncate text-xs text-muted">{thumb.text}</span>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card className="p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-ink">Script</h3>
                  <span className="flex items-center gap-2">
                    {selected.scriptVersion && (
                      <Badge tone="gray">Script {selected.scriptVersion}</Badge>
                    )}
                    {selected.annotations.length > 0 && (
                      <Badge tone="green">
                        QA · {selected.annotations.length} annotation
                        {selected.annotations.length === 1 ? "" : "s"}
                      </Badge>
                    )}
                  </span>
                </div>
                {selected.scriptLines.length === 0 ? (
                  <p className="flex items-center gap-2 text-sm text-muted">
                    <FileText className="h-4 w-4 shrink-0" />
                    No script text is attached to this approval yet.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2 text-sm leading-relaxed text-ink2">
                    {selected.scriptLines.map((line, index) => (
                      <p key={`${index}-${line.slice(0, 24)}`}>{line}</p>
                    ))}
                  </div>
                )}
                {selected.annotations.length > 0 && (
                  <div className="mt-3 flex flex-col gap-2">
                    {selected.annotations.map((annotation, index) => {
                      const visual = annotationVisual(annotation.tone);
                      return (
                        <div
                          key={index}
                          className="flex items-start gap-2 rounded-lg border border-line bg-raised p-3"
                        >
                          <visual.Icon
                            className={`mt-0.5 h-4 w-4 shrink-0 ${visual.className}`}
                          />
                          <p className="text-xs leading-relaxed text-ink2">
                            {annotation.label && (
                              <span className={`font-medium ${visual.className}`}>
                                {annotation.label}:{" "}
                              </span>
                            )}
                            {annotation.text}
                          </p>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>
            </div>

            {/* Right: Decision Rail */}
            <Card className="w-80 shrink-0 self-start p-5">
              <h2 className="mb-4 text-sm font-semibold text-ink">Decision Rail</h2>

              {selected.checks.length === 0 ? (
                <p className="text-xs leading-relaxed text-muted">
                  No QA results are attached to this approval yet.
                </p>
              ) : (
                <div className="flex flex-col gap-2">
                  {selected.checks.map((check) => {
                    const visual = checkVisual(check.status);
                    return (
                      <div
                        key={check.label}
                        className="flex items-center justify-between rounded-lg border border-line bg-raised px-3 py-2"
                      >
                        <span className="text-sm text-ink">{check.label}</span>
                        <span
                          className={`flex items-center gap-1.5 text-xs font-semibold ${visual.className}`}
                        >
                          <visual.Icon className="h-4 w-4" />
                          {check.status}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="mt-5 border-t border-line pt-4">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                  Metadata
                </h3>
                {!selected.metadata ? (
                  <p className="text-xs leading-relaxed text-muted">
                    No metadata pack is attached to this approval yet.
                  </p>
                ) : (
                  <>
                    <p className="text-sm font-medium leading-snug text-ink">
                      {selected.metadata.title ?? selected.title}
                    </p>
                    <p className="tnum mt-1 text-xs text-ink2">
                      {[
                        `${selected.metadata.tags.length} tags`,
                        selected.metadata.description
                          ? "Description ready"
                          : "No description",
                        selected.thumbnailVersion
                          ? `Thumbnail ${selected.thumbnailVersion}`
                          : null,
                        selected.metadataVersion
                          ? `Metadata ${selected.metadataVersion}`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    {selected.metadata.description && (
                      <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-ink2">
                        {selected.metadata.description}
                      </p>
                    )}
                    {selected.metadata.syntheticMedia !== undefined ? (
                      <>
                        <div className="mt-3 flex items-center justify-between">
                          <span className="text-xs text-ink2">
                            Synthetic media: {selected.metadata.syntheticMedia ? "Yes" : "No"}
                          </span>
                          <span
                            role="switch"
                            aria-checked={selected.metadata.syntheticMedia}
                            aria-label="Synthetic media disclosure"
                            className={`relative inline-flex h-5 w-9 items-center rounded-full ${
                              selected.metadata.syntheticMedia ? "bg-accent" : "bg-line"
                            }`}
                          >
                            <span
                              className={`absolute h-4 w-4 rounded-full bg-white ${
                                selected.metadata.syntheticMedia
                                  ? "right-0.5"
                                  : "left-0.5"
                              }`}
                            />
                          </span>
                        </div>
                        <p className="mt-1 text-xs text-muted">
                          {selected.metadata.syntheticMedia
                            ? "Disclosure toggle ON — AI voice and visuals declared."
                            : "Disclosure toggle OFF — not declared as synthetic media."}
                        </p>
                      </>
                    ) : (
                      <p className="mt-3 text-xs text-muted">
                        Synthetic media: not specified in the metadata pack.
                      </p>
                    )}
                  </>
                )}
              </div>

              {selected.costLabel && (
                <div className="mt-5 flex items-center justify-between border-t border-line pt-4">
                  <span className="text-xs text-muted">Cost so far</span>
                  <span className="tnum font-mono text-sm font-semibold text-ink">
                    {selected.costLabel}
                  </span>
                </div>
              )}

              <div className="mt-4 flex flex-col gap-2">
                <Button variant="primary" disabled={pending || !selected} onClick={handleApprove}>
                  {pending && <Loader2 className="h-4 w-4 animate-spin" />}
                  {approveLabel}
                </Button>
                <Button variant="outline" disabled title="The backend has no edit endpoint yet">
                  Edit &amp; Continue
                </Button>
                <p className="-mt-1 text-[11px] leading-relaxed text-muted">
                  Edit &amp; Continue isn&apos;t available yet — the backend has no
                  edit endpoint, so items can only be approved or rejected.
                </p>
                <label htmlFor="reject-reason" className="mt-1 text-xs text-muted">
                  Reject requires a reason code
                </label>
                <select
                  id="reject-reason"
                  ref={reasonSelectRef}
                  value={rejectReason}
                  disabled={pending}
                  onChange={(event) => setRejectReason(event.target.value as RejectReasonCode)}
                  className="w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink"
                >
                  {REJECT_REASONS.map((reason) => (
                    <option key={reason.code} value={reason.code}>
                      {reason.label}
                    </option>
                  ))}
                </select>
                {rejectNeedsNote && (
                  <textarea
                    ref={noteRef}
                    value={rejectNote}
                    disabled={pending}
                    onChange={(event) => setRejectNote(event.target.value)}
                    placeholder="Note (required for Other)"
                    rows={2}
                    className="w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink placeholder:text-muted"
                  />
                )}
                <Button variant="danger" disabled={rejectDisabled} onClick={handleReject}>
                  {pending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <XCircle className="mr-1.5 h-4 w-4" />
                  )}
                  Reject
                </Button>
              </div>

              <p className="mt-4 text-xs leading-relaxed text-muted">
                {selected.gate === "A"
                  ? "Gate A items never auto-approve. Approving locks the approved script snapshot for production."
                  : "Gate B items never auto-approve. Approving publishes the approved metadata snapshot, even if other drafts change later."}
              </p>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
