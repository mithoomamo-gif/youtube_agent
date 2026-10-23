/**
 * Agents Live data layer (Round 2 integration).
 *
 * Sources of truth:
 * - `GET /api/v1/agents` — the 35 role definitions merged with live state
 *   (backend `app/routers/agents.py`). Live state is currently always
 *   `"idle"` until the orchestrator lands; whatever the backend returns
 *   is rendered verbatim, never embellished.
 * - `GET /api/v1/projects` — one project = one video; used to group the
 *   screen by video first.
 * - `GET /api/v1/logs` — the persisted WebSocket event journal
 *   (`event_log` table). Each row's `message` is the full WS envelope
 *   serialised as JSON, so history entries are decoded with the exact
 *   same mapper as live frames and deduplicated by `event_id`.
 *
 * The shared `ApiClient` has no agents/logs methods, so reads here use a
 * small token-authenticated fetch that mirrors `client.ts` conventions
 * (same base URL, same `X-Local-Token` header, same error envelope →
 * `ApiError`). Job cancellation goes through `ApiClient.cancelJob`,
 * which does exist.
 *
 * Live updates: this module NEVER opens a socket. The app-level bridge
 * (`src/state/liveSync.ts`, owned by another worker) owns the single
 * `WsClient` and translates frames into React Query invalidations
 * (`agent.*` → `["agents"]` etc.) — it does not forward payloads. So
 * payload-level live state (per-agent status / step_label) reaches this
 * screen two honest ways, both without a second socket:
 *   1. the event journal (`GET /logs`, polled) — the hub persists every
 *      published frame, so journal rows decode into the same live
 *      states and feed entries as live frames;
 *   2. direct frame delivery, if the bridge (or any future owner of the
 *      shared client) forwards frames to {@link emitAgentWsEvent} or
 *      exposes a subscribe-style export that `AgentsLive` probes for.
 */

import { useQuery } from "@tanstack/react-query";
import { ApiClient } from "../client";
import { getBackendConnection } from "../tauri";
import { ApiError } from "../types";
import type { WsEvent } from "../types";

// ── Local types (per Round 2 convention: not in src/api/types.ts) ───────

export interface AgentDefinition {
  id: string;
  phase: string;
  name: string;
  /** Wire status from the backend catalogue, e.g. "idle". */
  status: string;
}

export interface ProjectSummary {
  id: string;
  title: string;
  status: string;
  /** Backend phase id (e.g. "scripting") or null when not started. */
  phase: string | null;
}

export type FeedSeverity = "info" | "success" | "warning" | "danger";

export interface ActivityEntry {
  /** WS `event_id` (or a log-row-derived id) — the dedup key. */
  id: string;
  /** ISO-8601 timestamp of the event. */
  ts: string;
  /** `HH:MM:SS` rendering of `ts`. */
  timeLabel: string;
  /** One plain-language sentence. */
  text: string;
  severity: FeedSeverity;
  /** True while a gate is waiting on the user (pinned "Needs you"). */
  needsYou?: boolean;
  approvalId?: string;
  projectId?: string;
  gate?: string;
  /** Set on gate.decided entries so the matching pin can be cleared. */
  decidedApprovalId?: string;
}

/** Per-agent live overlay built from `agent.status_changed` frames. */
export interface LiveAgentState {
  agentId: string;
  projectId: string | null;
  runId?: string;
  jobId?: string;
  status: string;
  stepLabel?: string;
  /** Determinate progress ONLY when a frame carried it (0–100). */
  progress?: number;
  /** e.g. "Retry 1" — only when a frame carried an attempt count > 1. */
  retryLabel?: string;
  /** Formatted cost — only when a frame carried a cost field. */
  costLabel?: string;
  updatedAt: string;
}

export interface NameContext {
  agentName: (id?: string | null) => string | undefined;
  projectTitle: (id?: string | null) => string | undefined;
}

// ── Wire helpers ────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * Token-authenticated GET against the sidecar, mirroring `ApiClient`'s
 * error handling (error envelope → {@link ApiError}; failures thrown).
 */
async function rawGet(path: string): Promise<unknown> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      headers: { "X-Local-Token": conn.token },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") throw err;
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: err instanceof Error ? err.message : "Network request failed",
      status: 0,
      retryable: true,
    });
  }
  const text = await res.text().catch(() => "");
  let body: unknown;
  try {
    body = text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    body = text;
  }
  const ok = typeof res.ok === "boolean" ? res.ok : res.status >= 200 && res.status < 300;
  if (!ok) {
    if (isRecord(body) && isRecord(body.error)) {
      const err = body.error;
      throw new ApiError({
        code: typeof err.code === "string" ? err.code : `HTTP_${res.status}`,
        message: typeof err.message === "string" ? err.message : `Request failed (${res.status})`,
        status: res.status,
        retryable: typeof err.retryable === "boolean" ? err.retryable : res.status >= 500,
      });
    }
    throw new ApiError({
      code: `HTTP_${res.status}`,
      message: `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status >= 500,
    });
  }
  return body;
}

function pageItems(raw: unknown): Record<string, unknown>[] {
  const list = Array.isArray(raw)
    ? raw
    : isRecord(raw) && Array.isArray(raw.items)
      ? raw.items
      : [];
  return list.filter(isRecord);
}

// ── Fetchers ────────────────────────────────────────────────────────────

export async function fetchAgents(): Promise<AgentDefinition[]> {
  const raw = await rawGet("/agents");
  return pageItems(raw)
    .map((r) => ({
      id: str(r.id) ?? "",
      phase: str(r.phase) ?? "",
      name: str(r.name) ?? "",
      status: str(r.status) ?? "idle",
    }))
    .filter((a) => a.id.length > 0);
}

export async function fetchProjects(): Promise<ProjectSummary[]> {
  const raw = await rawGet("/projects?limit=200");
  return pageItems(raw)
    .map((r) => {
      // The projects router returns raw snake_case rows; tolerate the
      // camelCase shape from src/api/types.ts as well.
      const phase = str(r.current_phase) ?? str(r.phase) ?? null;
      return {
        id: str(r.id) ?? "",
        title: str(r.topic) ?? str(r.title) ?? "Untitled video",
        status: str(r.status) ?? "draft",
        phase: phase && phase !== "null" ? phase : null,
      };
    })
    .filter((p) => p.id.length > 0);
}

interface RawLogEntry {
  id: string;
  type: string;
  message: string;
  createdAt: string | null;
}

async function fetchLogEntries(limit = 60): Promise<RawLogEntry[]> {
  const raw = await rawGet(`/logs?limit=${limit}`);
  return pageItems(raw).map((r) => ({
    id: str(r.id) ?? "",
    type: str(r.type) ?? "",
    message: typeof r.message === "string" ? r.message : "",
    createdAt: str(r.created_at) ?? null,
  }));
}

/** Cancels a job through the real endpoint (`POST /jobs/{id}/cancel`). */
export async function cancelJobRequest(jobId: string): Promise<void> {
  const conn = await getBackendConnection();
  const client = new ApiClient(conn);
  await client.cancelJob(jobId);
}

// ── Queries ─────────────────────────────────────────────────────────────

export function useAgentsCatalogue() {
  return useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
    retry: 1,
  });
}

export function useAgentProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    retry: 1,
    // Project phases move as pipelines run; a gentle poll keeps the
    // strip honest even between WS frames.
    refetchInterval: 20_000,
  });
}

/**
 * Activity history seeded from the event journal. The 15s poll doubles
 * as a near-live fallback: journal rows are the same events the socket
 * carries, so the feed still advances (deduped by event_id) even if the
 * WS bridge has not attached a handler yet.
 */
export function useActivityHistory() {
  return useQuery({
    queryKey: ["logs", "recent"],
    queryFn: () => fetchLogEntries(60),
    retry: 1,
    refetchInterval: 15_000,
  });
}

// ── WS event mapping (shared by live frames and journal history) ───────

export function formatTimeLabel(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function payloadOf(event: WsEvent): Record<string, unknown> {
  return isRecord(event.payload) ? event.payload : {};
}

/** Determinate progress from a payload — undefined when not provided. */
export function progressFromPayload(payload: Record<string, unknown>): number | undefined {
  const clamp = (v: number) => Math.min(100, Math.max(0, Math.round(v)));
  const pct = num(payload.progress_pct) ?? num(payload.progress_percent) ?? num(payload.percent);
  if (pct !== undefined) return clamp(pct);
  const current = num(payload.current) ?? num(payload.done);
  const total = num(payload.total);
  if (current !== undefined && total !== undefined && total > 0) {
    return clamp((current / total) * 100);
  }
  const p = num(payload.progress);
  if (p !== undefined) return clamp(p <= 1 ? p * 100 : p);
  return undefined;
}

function costLabelFromPayload(payload: Record<string, unknown>): string | undefined {
  const micros = num(payload.cost_micros) ?? num(payload.costMicros);
  if (micros !== undefined) return `$${(micros / 1_000_000).toFixed(2)}`;
  const usd = num(payload.cost_usd) ?? num(payload.costUsd);
  if (usd !== undefined) return `$${usd.toFixed(2)}`;
  return undefined;
}

/** Builds the live per-agent overlay from an `agent.status_changed` frame. */
export function wsEventToLiveState(event: WsEvent): LiveAgentState | null {
  if (event.type !== "agent.status_changed") return null;
  const payload = payloadOf(event);
  const agentId = str(payload.agent_id) ?? str(event.agent_id);
  const status = str(payload.status);
  if (!agentId || !status) return null;
  const attempt = num(payload.attempt) ?? num(payload.attempts);
  return {
    agentId,
    projectId: str(event.project_id) ?? str(payload.project_id) ?? null,
    runId: str(payload.run_id) ?? str(event.run_id),
    jobId: str(event.job_id) ?? str(payload.job_id),
    status,
    stepLabel: str(payload.step_label) ?? str(payload.stepLabel),
    progress: progressFromPayload(payload),
    retryLabel: attempt !== undefined && attempt > 1 ? `Retry ${attempt - 1}` : undefined,
    costLabel: costLabelFromPayload(payload),
    updatedAt: event.ts,
  };
}

/** Maps one WS frame to one plain-language feed entry, or null to skip. */
export function wsEventToActivity(event: WsEvent, ctx: NameContext): ActivityEntry | null {
  const payload = payloadOf(event);
  const base = {
    id: event.event_id,
    ts: event.ts,
    timeLabel: formatTimeLabel(event.ts),
  };
  const projectId = str(event.project_id) ?? str(payload.project_id);
  const agentId = str(payload.agent_id) ?? str(event.agent_id);
  const agentName = ctx.agentName(agentId) ?? agentId ?? "An agent";

  switch (event.type) {
    case "agent.status_changed": {
      const status = str(payload.status) ?? "updated";
      const stepLabel = str(payload.step_label) ?? str(payload.stepLabel);
      const severity: FeedSeverity =
        status === "failed"
          ? "danger"
          : status === "blocked"
            ? "warning"
            : status === "done"
              ? "success"
              : "info";
      return {
        ...base,
        text: stepLabel ? `${agentName} — ${stepLabel}` : `${agentName} is now ${status}.`,
        severity,
        projectId,
      };
    }
    case "agent.decision": {
      const decision = str(payload.decision) ?? str(payload.summary) ?? str(payload.message);
      const reason = str(payload.reason);
      const text = decision
        ? `${agentName} — ${decision}${reason && reason !== decision ? ` ${reason}` : ""}`
        : `${agentName} recorded a decision.`;
      return { ...base, text, severity: "info", projectId };
    }
    case "gate.pending": {
      const gate = str(payload.gate) ?? "";
      const title = ctx.projectTitle(projectId);
      return {
        ...base,
        text: `Gate ${gate} is waiting for your approval${title ? ` for "${title}"` : ""}.`.replace(
          "  ",
          " ",
        ),
        severity: "warning",
        needsYou: true,
        approvalId: str(payload.approval_id),
        projectId,
        gate: gate || undefined,
      };
    }
    case "gate.decided": {
      const gate = str(payload.gate) ?? "";
      const decision = str(payload.decision) ?? str(payload.status) ?? "";
      const approved = decision === "approve" || decision === "approved";
      const rejected = decision === "reject" || decision === "rejected";
      const title = ctx.projectTitle(projectId);
      return {
        ...base,
        text: `Gate ${gate} was ${approved ? "approved" : rejected ? "rejected" : "decided"}${
          title ? ` for "${title}"` : ""
        }.`.replace("  ", " "),
        severity: approved ? "success" : "warning",
        projectId,
        gate: gate || undefined,
        decidedApprovalId: str(payload.approval_id),
      };
    }
    case "budget.warning": {
      const spent = num(payload.spent_usd);
      const cap = num(payload.cap_usd);
      const scope = str(payload.scope);
      const action = str(payload.action);
      const money =
        spent !== undefined && cap !== undefined
          ? `$${spent.toFixed(2)} of $${cap.toFixed(2)} spent`
          : "spending is near its cap";
      return {
        ...base,
        text: `Budget warning${scope ? ` (${scope})` : ""} — ${money}${action === "paused" ? "; work was paused." : "."}`,
        severity: "warning",
        projectId,
      };
    }
    case "log.line": {
      const line = str(payload.message) ?? str(payload.line) ?? str(payload.text);
      if (!line) return null;
      return { ...base, text: line, severity: "info", projectId };
    }
    default: {
      // progress.*, job.*, pipeline.*, publication.* and anything else:
      // only surface a frame when it carries its own readable sentence —
      // never invent one.
      if (
        event.type.startsWith("job.") ||
        event.type.startsWith("pipeline.") ||
        event.type.startsWith("publication.")
      ) {
        const readable = str(payload.step_label) ?? str(payload.message);
        if (!readable) return null;
        const failed = event.type.endsWith(".failed") || str(payload.status) === "failed";
        return {
          ...base,
          text: readable,
          severity: failed ? "danger" : "info",
          projectId,
        };
      }
      return null;
    }
  }
}

/** Decodes one journal row's `message` (a persisted WS envelope) back into a frame. */
export function decodeLogEnvelope(row: RawLogEntry): WsEvent | null {
  let parsed: unknown;
  try {
    parsed = row.message ? (JSON.parse(row.message) as unknown) : undefined;
  } catch {
    parsed = undefined;
  }
  if (!isRecord(parsed) || typeof parsed.type !== "string") return null;
  const envelope = parsed as unknown as WsEvent;
  return {
    v: typeof envelope.v === "number" ? envelope.v : 1,
    type: envelope.type,
    event_id: str(envelope.event_id) ?? `log-${row.id}`,
    seq: typeof envelope.seq === "number" ? envelope.seq : 0,
    ts: str(envelope.ts) ?? row.createdAt ?? "",
    boot_id: str(envelope.boot_id) ?? "",
    project_id: str(envelope.project_id),
    job_id: str(envelope.job_id),
    run_id: str(envelope.run_id),
    agent_id: str(envelope.agent_id),
    payload: envelope.payload ?? {},
  };
}

/**
 * Decodes journal rows (`GET /logs`) into feed entries. Undecodable
 * rows and frames with no readable mapping are skipped rather than
 * shown raw.
 */
export function logEntriesToActivity(rows: RawLogEntry[], ctx: NameContext): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  for (const row of rows) {
    const event = decodeLogEnvelope(row);
    if (!event) continue;
    const entry = wsEventToActivity(event, ctx);
    if (entry) entries.push(entry);
  }
  return entries;
}

/**
 * Folds journal rows into per-agent live states (latest
 * `agent.status_changed` per project+agent wins). This is how live
 * status/step labels reach the screen while the socket bridge only
 * invalidates queries: the journal is the persisted copy of the same
 * frames.
 */
export function logEntriesToLiveStates(rows: RawLogEntry[]): Map<string, LiveAgentState> {
  const states = new Map<string, LiveAgentState>();
  // Rows arrive newest-first; walk oldest-first so newer frames win.
  for (const row of [...rows].reverse()) {
    const event = decodeLogEnvelope(row);
    if (!event) continue;
    const live = wsEventToLiveState(event);
    if (!live) continue;
    states.set(`${live.projectId ?? "*"}:${live.agentId}`, live);
  }
  return states;
}

// ── Shared WS handler registry (no socket is created here) ──────────────

type AgentWsHandler = (event: WsEvent) => void;

const agentWsHandlers = new Set<AgentWsHandler>();

/**
 * Registers a handler for frames forwarded from the app's single shared
 * `WsClient`. Returns an unregister function.
 */
export function registerAgentWsHandler(handler: AgentWsHandler): () => void {
  agentWsHandlers.add(handler);
  return () => {
    agentWsHandlers.delete(handler);
  };
}

/**
 * Entry point for the socket-owning bridge (`src/state/liveSync.ts`):
 * call this from its one `WsClient` `onEvent` and every registered
 * screen handler receives the frame. Safe to call when nobody is
 * registered.
 */
export function emitAgentWsEvent(event: WsEvent): void {
  for (const handler of [...agentWsHandlers]) {
    try {
      handler(event);
    } catch {
      // One bad consumer must never break event delivery for the rest.
    }
  }
}
