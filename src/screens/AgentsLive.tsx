import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle,
  Bot,
  Check,
  Clapperboard,
  Image,
  Lock,
  Mic,
  Pause,
  PenLine,
  Pin,
  RefreshCw,
  Search,
  ShieldCheck,
  TrendingUp,
  Upload,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ProgressBar } from "../components/ui/ProgressBar";
import type { WsEvent } from "../api/types";
import {
  cancelJobRequest,
  logEntriesToActivity,
  logEntriesToLiveStates,
  registerAgentWsHandler,
  useActivityHistory,
  useAgentProjects,
  useAgentsCatalogue,
  wsEventToActivity,
  wsEventToLiveState,
} from "../api/queries/agents";
import type {
  ActivityEntry,
  AgentDefinition,
  LiveAgentState,
  NameContext,
  ProjectSummary,
} from "../api/queries/agents";
import {
  useEngineStatus,
  usePauseAll,
  useResumeEngine,
  useStopAll,
} from "../api/queries/engine";

// ── Pipeline phases (backend ids, MASTER_ROADMAP §5.2 order) ───────────

interface PipelinePhase {
  id: string;
  label: string;
  /** Gate letter shown in the lock chip after this phase. */
  gateAfter?: "A" | "B";
}

const PHASES: PipelinePhase[] = [
  { id: "research", label: "Research" },
  { id: "scripting", label: "Scripting", gateAfter: "A" },
  { id: "production", label: "Production" },
  { id: "editing", label: "Editing" },
  { id: "packaging", label: "Packaging", gateAfter: "B" },
  { id: "publishing", label: "Publishing" },
  { id: "growth", label: "Growth" },
  { id: "safety_qa", label: "Safety & QA" },
];

const PHASE_ICONS: Record<string, LucideIcon> = {
  research: Search,
  scripting: PenLine,
  production: Mic,
  editing: Clapperboard,
  packaging: Image,
  publishing: Upload,
  growth: TrendingUp,
  safety_qa: ShieldCheck,
};

// ── Status presentation (wire status → label/styles) ────────────────────

const STATUS_STYLES: Record<string, { dot: string; text: string; label: string }> = {
  running: { dot: "bg-ok animate-pulse", text: "text-ok", label: "Running" },
  done: { dot: "bg-accent2", text: "text-accent2", label: "Done" },
  completed: { dot: "bg-accent2", text: "text-accent2", label: "Done" },
  queued: { dot: "bg-warn", text: "text-warn", label: "Queued" },
  blocked: { dot: "bg-warn", text: "text-warn", label: "Blocked" },
  paused: { dot: "bg-warn", text: "text-warn", label: "Paused" },
  failed: { dot: "bg-danger", text: "text-danger", label: "Failed" },
  idle: { dot: "bg-muted", text: "text-muted", label: "Idle" },
  waiting: { dot: "bg-muted", text: "text-muted", label: "Waiting" },
  cancelled: { dot: "bg-muted", text: "text-muted", label: "Cancelled" },
};

function statusStyle(status: string): { dot: string; text: string; label: string } {
  return (
    STATUS_STYLES[status.toLowerCase()] ?? {
      dot: "bg-muted",
      text: "text-muted",
      label: status.charAt(0).toUpperCase() + status.slice(1),
    }
  );
}

const SEVERITY_DOT: Record<ActivityEntry["severity"], string> = {
  info: "bg-muted",
  success: "bg-ok",
  warning: "bg-warn",
  danger: "bg-danger",
};

const FEED_CAP = 200;

// ── Live-sync bridge attach (never opens a socket) ──────────────────────

/**
 * Best-effort registration with the app-level live-sync bridge
 * (`src/state/liveSync.ts`), which owns the single shared WsClient.
 * Probes the module for a subscribe-style export and registers
 * `handler` there. Returns a detach function, `{}` when the bridge
 * accepted the handler without one, or null when no bridge/export is
 * available yet — in that case the screen still receives events via
 * {@link registerAgentWsHandler} (if the bridge forwards through
 * `emitAgentWsEvent`) and via the journal poll. This screen NEVER
 * constructs its own WsClient, so it can never open a second socket.
 */
async function probeLiveSyncBridge(
  handler: (event: WsEvent) => void,
): Promise<{ detach?: () => void } | null> {
  try {
    const specifier = "../state/liveSync";
    const mod = (await import(/* @vite-ignore */ specifier)) as Record<string, unknown>;
    const candidates: unknown[] = [mod, mod.default];
    const names = [
      "subscribeWsEvents",
      "subscribeToWsEvents",
      "subscribeAgentEvents",
      "onWsEvent",
      "addWsEventListener",
      "registerWsHandler",
    ];
    for (const candidate of candidates) {
      if (typeof candidate !== "object" || candidate === null) continue;
      const record = candidate as Record<string, unknown>;
      for (const name of names) {
        const fn = record[name];
        if (typeof fn !== "function") continue;
        const result = (fn as (h: (e: WsEvent) => void) => unknown)(handler);
        return typeof result === "function" ? { detach: result as () => void } : {};
      }
    }
    return null;
  } catch {
    return null;
  }
}

// ── Agent card ───────────────────────────────────────────────────────────

function AgentCard({
  agent,
  live,
  cancelling,
  onCancel,
}: {
  agent: AgentDefinition;
  live?: LiveAgentState;
  cancelling: boolean;
  onCancel: (jobId: string) => void;
}) {
  const Icon = PHASE_ICONS[agent.phase] ?? Bot;
  const status = live?.status ?? agent.status;
  const style = statusStyle(status);
  const detail =
    live?.stepLabel ?? (status.toLowerCase() === "idle" ? "Idle — no work right now" : style.label);
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <Icon className="h-5 w-5 shrink-0 text-accent2" />
          <span className="truncate text-sm font-semibold">{agent.name}</span>
        </span>
        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
      </div>

      <p className={`mt-1.5 text-xs font-medium ${style.text}`}>{style.label}</p>

      {/* Determinate bar only when a live frame actually carried progress. */}
      {live?.progress !== undefined && (
        <div className="mt-3">
          <ProgressBar value={live.progress} />
        </div>
      )}

      <p className="mt-3 text-sm text-ink2">{detail}</p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {live?.costLabel && (
          <span className="tnum rounded-md bg-raised px-2 py-0.5 font-mono text-xs text-ink">
            {live.costLabel}
          </span>
        )}
        {live?.retryLabel && <Badge tone="amber">{live.retryLabel}</Badge>}
        {live?.jobId && status.toLowerCase() === "running" && (
          <Button
            variant="ghost"
            size="sm"
            disabled={cancelling}
            onClick={() => onCancel(live.jobId!)}
          >
            {cancelling ? "Cancelling…" : "Cancel run"}
          </Button>
        )}
      </div>
    </Card>
  );
}

// ── Screen ───────────────────────────────────────────────────────────────

export default function AgentsLive() {
  const navigate = useNavigate();
  const agentsQuery = useAgentsCatalogue();
  // Engine controls (real since v22 — POST /engine/*).
  const engineStatus = useEngineStatus();
  const pauseAll = usePauseAll();
  const stopAll = useStopAll();
  const resumeEngine = useResumeEngine();
  const engineStopped = engineStatus.data?.stopAll === true;
  const enginePaused = !engineStopped && engineStatus.data?.pauseAll === true;
  const projectsQuery = useAgentProjects();
  const historyQuery = useActivityHistory();

  const agents = useMemo(() => agentsQuery.data ?? [], [agentsQuery.data]);
  const projects = useMemo(() => projectsQuery.data ?? [], [projectsQuery.data]);

  // Name lookups for plain-language event sentences.
  const agentNameById = useMemo(() => new Map(agents.map((a) => [a.id, a.name])), [agents]);
  const projectTitleById = useMemo(
    () => new Map(projects.map((p) => [p.id, p.title])),
    [projects],
  );
  const nameCtx = useMemo<NameContext>(
    () => ({
      agentName: (id) => (id ? agentNameById.get(id) : undefined),
      projectTitle: (id) => (id ? projectTitleById.get(id) : undefined),
    }),
    [agentNameById, projectTitleById],
  );
  const nameCtxRef = useRef(nameCtx);
  nameCtxRef.current = nameCtx;

  // Live overlay + live feed (fed by WS frames; see effect below).
  const [liveByKey, setLiveByKey] = useState<Map<string, LiveAgentState>>(new Map());
  const [liveFeed, setLiveFeed] = useState<ActivityEntry[]>([]);
  const [wsAttached, setWsAttached] = useState(false);
  const [cancelNotice, setCancelNotice] = useState<string | null>(null);

  const mountedRef = useRef(true);
  const seenEventIdsRef = useRef<Set<string>>(new Set());

  const handleWsEvent = useCallback((event: WsEvent) => {
    if (!mountedRef.current) return;
    if (event.event_id) {
      if (seenEventIdsRef.current.has(event.event_id)) return;
      seenEventIdsRef.current.add(event.event_id);
      if (seenEventIdsRef.current.size > 2000) seenEventIdsRef.current.clear();
    }
    const live = wsEventToLiveState(event);
    if (live) {
      const key = `${live.projectId ?? "*"}:${live.agentId}`;
      setLiveByKey((prev) => {
        const next = new Map(prev);
        next.set(key, live);
        return next;
      });
    }
    const entry = wsEventToActivity(event, nameCtxRef.current);
    if (entry) {
      setLiveFeed((prev) => [entry, ...prev.filter((e) => e.id !== entry.id)].slice(0, FEED_CAP));
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    // Path 1: the bridge forwards frames through the shared registry.
    const unregister = registerAgentWsHandler(handleWsEvent);
    // Path 2: register directly on the bridge module if it exposes a
    // subscribe-style export. Either path uses the bridge's one socket.
    let detach: (() => void) | undefined;
    let cancelled = false;
    void probeLiveSyncBridge(handleWsEvent).then((result) => {
      if (cancelled) {
        result?.detach?.();
        return;
      }
      if (result) {
        detach = result.detach;
        setWsAttached(true);
      }
    });
    return () => {
      mountedRef.current = false;
      cancelled = true;
      unregister();
      detach?.();
    };
  }, [handleWsEvent]);

  // ── Feed: journal history + live frames, deduped, pins resolved ──────
  const historyEntries = useMemo(
    () => (historyQuery.data ? logEntriesToActivity(historyQuery.data, nameCtx) : []),
    [historyQuery.data, nameCtx],
  );

  const feed = useMemo(() => {
    const byId = new Map<string, ActivityEntry>();
    // Live entries are inserted last so they win over their journal copy.
    for (const entry of historyEntries) byId.set(entry.id, entry);
    for (const entry of liveFeed) byId.set(entry.id, entry);
    const decided = new Set<string>();
    for (const entry of byId.values()) {
      if (entry.decidedApprovalId) decided.add(entry.decidedApprovalId);
    }
    const merged = [...byId.values()].map((entry) =>
      entry.needsYou && entry.approvalId && decided.has(entry.approvalId)
        ? { ...entry, needsYou: false }
        : entry,
    );
    merged.sort((a, b) => (Date.parse(b.ts) || 0) - (Date.parse(a.ts) || 0));
    return merged.slice(0, FEED_CAP);
  }, [historyEntries, liveFeed]);

  const pinned = feed.find((entry) => entry.needsYou);
  const feedList = pinned ? feed.filter((entry) => entry.id !== pinned.id) : feed;

  const pendingGateFor = useCallback(
    (projectId: string, gate: string) =>
      feed.some(
        (entry) => entry.needsYou && entry.projectId === projectId && entry.gate === gate,
      ),
    [feed],
  );

  // ── Live lookups ───────────────────────────────────────────────────────
  // The socket bridge only invalidates queries (it never forwards
  // payloads), so per-agent live state is folded from the event journal
  // — the persisted copy of the same frames — and overlaid with any
  // frames delivered directly; the newer timestamp wins per key.
  const journalLiveByKey = useMemo(
    () => (historyQuery.data ? logEntriesToLiveStates(historyQuery.data) : new Map()),
    [historyQuery.data],
  );
  const mergedLiveByKey = useMemo(() => {
    const merged = new Map(journalLiveByKey);
    for (const [key, value] of liveByKey) {
      const existing = merged.get(key);
      if (!existing || value.updatedAt >= existing.updatedAt) merged.set(key, value);
    }
    return merged;
  }, [journalLiveByKey, liveByKey]);

  const liveForProject = useCallback(
    (projectId: string, agentId: string) => mergedLiveByKey.get(`${projectId}:${agentId}`),
    [mergedLiveByKey],
  );
  const liveForCatalogue = useCallback(
    (agentId: string): LiveAgentState | undefined => {
      let best: LiveAgentState | undefined;
      for (const [key, value] of mergedLiveByKey) {
        if (key.endsWith(`:${agentId}`) && (!best || value.updatedAt > best.updatedAt)) {
          best = value;
        }
      }
      return best;
    },
    [mergedLiveByKey],
  );

  const runningCount = useMemo(() => {
    let count = 0;
    for (const agent of agents) {
      const status = (liveForCatalogue(agent.id)?.status ?? agent.status).toLowerCase();
      if (status === "running") count += 1;
    }
    return count;
  }, [agents, liveForCatalogue]);

  // ── Cancel (real endpoint: POST /jobs/{id}/cancel) ────────────────────
  const cancelMutation = useMutation({
    mutationFn: cancelJobRequest,
    onSuccess: (_data, jobId) => {
      setLiveByKey((prev) => {
        const next = new Map(prev);
        for (const [key, value] of [...next.entries()]) {
          if (value.jobId === jobId) {
            next.set(key, { ...value, status: "cancelled", stepLabel: "Cancellation requested" });
          }
        }
        return next;
      });
    },
    onError: () => {
      setCancelNotice("That run could not be cancelled — the backend rejected the request.");
    },
  });
  const cancellingJobId = cancelMutation.isPending ? cancelMutation.variables : undefined;
  const handleCancel = useCallback(
    (jobId: string) => {
      setCancelNotice(null);
      cancelMutation.mutate(jobId);
    },
    [cancelMutation],
  );

  // ── Deep link from the New Video wizard (`/agents?projectId=<id>`) ────
  const [searchParams] = useSearchParams();
  const requestedProjectId = searchParams.get("projectId");
  const requestedProjectExists =
    requestedProjectId !== null && projects.some((p) => p.id === requestedProjectId);
  const highlightedProjectId = requestedProjectExists ? requestedProjectId : null;
  const scrolledForRef = useRef<string | null>(null);

  // ── Pipeline strip (per selected video) ───────────────────────────────
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);

  // When the wizard's projectId is present AND exists in the projects
  // data: preselect it in the pipeline strip and scroll its section
  // into view (once per id). Absent/unknown id → behaviour unchanged.
  useEffect(() => {
    if (!requestedProjectId || !requestedProjectExists) return;
    setSelectedProjectId((prev) => (prev === requestedProjectId ? prev : requestedProjectId));
    if (scrolledForRef.current === requestedProjectId) return;
    const el = document.getElementById(`project-section-${requestedProjectId}`);
    if (el) {
      scrolledForRef.current = requestedProjectId;
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [requestedProjectId, requestedProjectExists, agentsQuery.isSuccess, projects]);
  const stripProject: ProjectSummary | undefined =
    projects.find((p) => p.id === selectedProjectId) ?? projects[0];
  const stripPhaseIndex = stripProject
    ? PHASES.findIndex((p) => p.id === stripProject.phase)
    : -1;
  const stripAllDone = stripProject
    ? ["done", "complete", "completed", "published"].includes(stripProject.status.toLowerCase())
    : false;

  const headerBadge = agentsQuery.isError ? (
    <Badge tone="red">Backend offline</Badge>
  ) : agentsQuery.isPending ? (
    <Badge tone="gray">Connecting…</Badge>
  ) : runningCount > 0 ? (
    <Badge tone="green">{runningCount} running</Badge>
  ) : (
    <Badge tone="gray">Idle</Badge>
  );

  return (
    <div className="min-h-full space-y-5 bg-app p-6 text-ink">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight">Agents Live</h1>
            {headerBadge}
          </div>
          <p className="mt-1 text-sm text-ink2">
            Real-time monitoring of active agents for current projects
          </p>
        </div>
        <Button
          variant="outline"
          disabled={engineStopped || pauseAll.isPending || resumeEngine.isPending}
          title={
            enginePaused
              ? "Resume the engine — new jobs will be claimed again."
              : "Pause the engine — running jobs finish, no new jobs start."
          }
          onClick={() => (enginePaused ? resumeEngine.mutate() : pauseAll.mutate())}
        >
          <Pause className="mr-1.5 h-4 w-4" />
          {enginePaused ? "Resume" : "Pause All"}
        </Button>
        <Button
          variant="danger"
          disabled={stopAll.isPending || resumeEngine.isPending}
          title={
            engineStopped
              ? "Resume the engine — clears the stop flag and restarts it."
              : "Stop the whole engine now — running jobs are cancelled and waiting jobs are dropped."
          }
          onClick={() => (engineStopped ? resumeEngine.mutate() : stopAll.mutate())}
        >
          {engineStopped ? "RESUME" : "STOP ALL"}
        </Button>
      </header>

      {agentsQuery.isPending ? (
        <>
          <Card className="p-5">
            <div className="h-16 animate-pulse rounded-xl bg-raised" />
          </Card>
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[2fr_1fr]">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {[0, 1, 2, 3].map((i) => (
                <Card key={i} className="p-4">
                  <div className="h-24 animate-pulse rounded-xl bg-raised" />
                </Card>
              ))}
            </div>
            <Card className="h-fit p-5">
              <div className="h-40 animate-pulse rounded-xl bg-raised" />
            </Card>
          </div>
        </>
      ) : agentsQuery.isError ? (
        <Card className="p-6">
          <p className="flex items-center gap-2 text-base font-semibold text-danger">
            <AlertTriangle className="h-5 w-5" />
            Can&apos;t reach the backend
          </p>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink2">
            The agent catalogue is unavailable, so no agents are shown. Check that the app
            backend is running, then try again.
          </p>
          <div className="mt-4">
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                void agentsQuery.refetch();
                void projectsQuery.refetch();
              }}
            >
              <RefreshCw className="mr-1.5 h-4 w-4" />
              Retry
            </Button>
          </div>
        </Card>
      ) : (
        <>
          {/* Pipeline strip — per video, with gate lock chips */}
          <Card className="p-5">
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <p className="min-w-0 flex-1 truncate text-sm font-semibold">
                {stripProject ? stripProject.title : "No video projects yet"}
              </p>
              {projects.length > 1 && (
                <label className="flex items-center gap-2 text-xs text-muted">
                  Video
                  <select
                    className="rounded-lg border border-line bg-raised px-2 py-1 text-xs text-ink"
                    value={stripProject?.id ?? ""}
                    onChange={(e) => setSelectedProjectId(e.target.value)}
                  >
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.title}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            {!stripProject && (
              <p className="mb-4 text-sm text-ink2">
                The pipeline will appear here once a video is created.
              </p>
            )}
            <div className="flex items-start overflow-x-auto pb-1">
              {PHASES.map((phase, index) => {
                const state = stripAllDone
                  ? "done"
                  : index < stripPhaseIndex
                    ? "done"
                    : index === stripPhaseIndex
                      ? "active"
                      : "upcoming";
                const gatePending =
                  phase.gateAfter && stripProject
                    ? pendingGateFor(stripProject.id, phase.gateAfter)
                    : false;
                const gatePassed =
                  phase.gateAfter !== undefined &&
                  (stripAllDone || index < stripPhaseIndex);
                return (
                  <div key={phase.id} className="flex items-start">
                    <div className="flex w-20 shrink-0 flex-col items-center gap-1.5">
                      <span
                        className={
                          state === "done"
                            ? "flex h-9 w-9 items-center justify-center rounded-full bg-accent text-white"
                            : state === "active"
                              ? "flex h-9 w-9 items-center justify-center rounded-full bg-accent font-semibold text-white ring-4 ring-accent/30 animate-pulse"
                              : "flex h-9 w-9 items-center justify-center rounded-full border border-line bg-raised text-xs font-medium text-muted"
                        }
                      >
                        {state === "done" ? <Check className="h-4 w-4" /> : index + 1}
                      </span>
                      <span
                        className={
                          state === "upcoming"
                            ? "text-xs whitespace-nowrap text-muted"
                            : "text-xs font-medium whitespace-nowrap text-ink"
                        }
                      >
                        {phase.label}
                      </span>
                      {state === "active" && (
                        <span className="text-[11px] text-accent2">Working now</span>
                      )}
                    </div>

                    {index < PHASES.length - 1 &&
                      (phase.gateAfter ? (
                        <span
                          className={`mx-1 mt-1 flex shrink-0 flex-col items-center gap-1 ${
                            gatePending ? "text-warn" : "text-muted"
                          }`}
                        >
                          <span
                            className={`flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-medium whitespace-nowrap ${
                              gatePending
                                ? "border-warn/50 bg-raised text-warn"
                                : "border-line bg-raised"
                            }`}
                          >
                            {gatePassed ? (
                              <Check className="h-3 w-3" />
                            ) : (
                              <Lock className="h-3 w-3" />
                            )}
                            Gate {phase.gateAfter}
                          </span>
                          <span className="text-[10px] whitespace-nowrap text-muted">
                            {gatePending ? "Needs you" : "Approval"}
                          </span>
                        </span>
                      ) : (
                        <span className="mx-1 mt-4 h-px w-6 shrink-0 bg-line" />
                      ))}
                  </div>
                );
              })}
            </div>
          </Card>

          {cancelNotice && (
            <p className="flex items-center gap-2 text-sm text-danger">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {cancelNotice}
            </p>
          )}

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[2fr_1fr]">
            {/* Agent cards — grouped by video project first */}
            <div className="space-y-6">
              {projects.map((project) => {
                const projectAgents = agents.filter(
                  (agent) =>
                    liveForProject(project.id, agent.id) !== undefined ||
                    (project.phase !== null && agent.phase === project.phase),
                );
                return (
                  <section
                    key={project.id}
                    id={`project-section-${project.id}`}
                    className={
                      highlightedProjectId === project.id
                        ? "-m-3 scroll-mt-6 rounded-2xl bg-accent/5 p-3 ring-1 ring-accent/60"
                        : "scroll-mt-6"
                    }
                  >
                    <div className="mb-3 flex items-center gap-2">
                      <h2 className="truncate text-base font-semibold">{project.title}</h2>
                      <Badge tone="gray">{project.status}</Badge>
                      {highlightedProjectId === project.id && (
                        <Badge tone="violet">Just created</Badge>
                      )}
                    </div>
                    {projectAgents.length === 0 ? (
                      <p className="text-sm text-ink2">
                        Pipeline not started for this video yet — its agents are idle.
                      </p>
                    ) : (
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                        {projectAgents.map((agent) => (
                          <AgentCard
                            key={agent.id}
                            agent={agent}
                            live={liveForProject(project.id, agent.id)}
                            cancelling={cancellingJobId === liveForProject(project.id, agent.id)?.jobId}
                            onCancel={handleCancel}
                          />
                        ))}
                      </div>
                    )}
                  </section>
                );
              })}

              {/* Full catalogue — all 35 roles with their real status */}
              <section>
                <h2 className="mb-3 text-base font-semibold">
                  All agents · catalogue
                </h2>
                {agents.length === 0 ? (
                  <p className="text-sm text-ink2">The backend returned no agents.</p>
                ) : (
                  PHASES.map((phase) => {
                    const phaseAgents = agents.filter((a) => a.phase === phase.id);
                    if (phaseAgents.length === 0) return null;
                    return (
                      <div key={phase.id} className="mb-5">
                        <h3 className="mb-2 text-xs font-semibold tracking-wide text-muted uppercase">
                          {phase.label}
                        </h3>
                        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                          {phaseAgents.map((agent) => (
                            <AgentCard
                              key={agent.id}
                              agent={agent}
                              live={liveForCatalogue(agent.id)}
                              cancelling={cancellingJobId === liveForCatalogue(agent.id)?.jobId}
                              onCancel={handleCancel}
                            />
                          ))}
                        </div>
                      </div>
                    );
                  })
                )}
              </section>
            </div>

            {/* Activity feed */}
            <Card className="h-fit p-5">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-base font-semibold">Activity</h2>
                {wsAttached ? (
                  <span className="flex items-center gap-1.5 text-xs text-ok">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ok" />
                    live
                  </span>
                ) : (
                  <span
                    className="flex items-center gap-1.5 text-xs text-muted"
                    title="Live socket not attached yet — this feed refreshes from the backend event journal every few seconds."
                  >
                    <span className="h-1.5 w-1.5 rounded-full bg-muted" />
                    journal
                  </span>
                )}
              </div>

              {pinned && (
                <div className="mb-4 rounded-xl border border-warn/60 bg-warn/10 p-3">
                  <p className="flex items-center gap-1.5 text-xs font-semibold text-warn">
                    <Pin className="h-3.5 w-3.5" />
                    Needs you
                    <AlertTriangle className="h-3.5 w-3.5" />
                  </p>
                  <p className="mt-1.5 text-sm leading-relaxed">{pinned.text}</p>
                  <div className="mt-3">
                    <Button variant="primary" size="sm" onClick={() => navigate("/review")}>
                      Review now
                    </Button>
                  </div>
                </div>
              )}

              {feedList.length === 0 && !pinned ? (
                <p className="text-sm leading-relaxed text-ink2">
                  {historyQuery.isPending
                    ? "Loading activity…"
                    : "Waiting for events… — agent and gate updates will appear here as they happen."}
                </p>
              ) : (
                <ul className="space-y-4">
                  {feedList.map((item) => (
                    <li key={item.id}>
                      <p className="tnum flex items-center gap-2 font-mono text-xs text-muted">
                        <span
                          className={`h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_DOT[item.severity]}`}
                        />
                        {item.timeLabel}
                      </p>
                      <p className="mt-0.5 text-sm leading-relaxed text-ink2">{item.text}</p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
