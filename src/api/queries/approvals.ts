/**
 * Approvals data layer for the Review screen (Round 2 — live binding).
 *
 * Wire reality (backend/app/routers/approvals.py):
 * - `GET /approvals` returns `{ items, next_cursor }`. There is NO
 *   `GET /approvals/{id}` route, so every detail field must come from
 *   the list payload. Items are raw rows (`SELECT *` dicts, snake_case)
 *   from an approvals table once the orchestrator lands one; until then
 *   the backend honestly returns an empty list. Every field below is
 *   therefore optional and normalised defensively — the UI renders
 *   exactly what the payload carries and honest empty states for the
 *   rest. Nothing is fabricated.
 * - Decisions: `POST /approvals/{id}/approve|reject` with body
 *   `{ reason_code?, note? }`. Reject REQUIRES a non-empty
 *   `reason_code` (else 422 `reason_code_required`). A second decision
 *   on the same id is 409 with error code `already_decided`.
 * - The list is the FULL record — decided rows stay in it (with
 *   `status: "decided"` and their `decision`). Anything presenting a
 *   pending QUEUE must filter with {@link isPendingApproval}; the raw
 *   list length is NOT a waiting count.
 * - There is no request-changes / edit endpoint; the screen keeps that
 *   affordance disabled rather than faking it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getSharedApiClient } from "../client";
import { ApiError } from "../types";
import type { ApprovalItem, DecideApprovalPayload } from "../types";

// ── Client singleton ──────────────────────────────────────────────────────

// Shared client, rebuilt automatically when the backend connection
// changes (restart = new port/token) — see getSharedApiClient.
function api() {
  return getSharedApiClient();
}

// ── Reject reason codes ───────────────────────────────────────────────────

/**
 * Structured reject taxonomy from the design (roadmap section 11).
 * The backend accepts any non-empty `reason_code` string and rejects an
 * empty one with 422 `reason_code_required`; these snake_case codes are
 * what we send on the wire.
 */
export const REJECT_REASONS = [
  { code: "wrong_facts", label: "Wrong facts" },
  { code: "weak_hook", label: "Weak hook" },
  { code: "off_topic", label: "Off-topic for channel" },
  { code: "quality_audio", label: "Quality — audio" },
  { code: "quality_visuals", label: "Quality — visuals" },
  { code: "thumbnail_title_misleading", label: "Thumbnail/title misleading" },
  { code: "policy_risk", label: "Policy risk" },
  { code: "duplicate_recent", label: "Duplicate of recent video" },
  { code: "not_good_enough", label: "Just not good enough" },
  { code: "other", label: "Other (note required)" },
] as const;

export type RejectReasonCode = (typeof REJECT_REASONS)[number]["code"];

// ── View model (normalised from the raw list payload) ─────────────────────

export type GateKind = "A" | "B" | "unknown";

export interface ApprovalCheck {
  label: string;
  status: string;
}

export interface ApprovalAnnotation {
  label?: string;
  text: string;
  tone: "ok" | "warn" | "danger" | "neutral";
}

export interface SceneVariant {
  id: string;
  label: string;
  durationLabel?: string;
  selected: boolean;
}

export interface ThumbnailVariant {
  id: string;
  label: string;
  imageUrl?: string;
  text?: string;
  score?: number;
  selected: boolean;
}

export interface ApprovalMetadata {
  title?: string;
  description?: string;
  tags: string[];
  /** Only present when the payload actually declares it. */
  syntheticMedia?: boolean;
}

export interface ApprovalItemView {
  id: string;
  projectId?: string;
  gate: GateKind;
  /** e.g. "Gate B · Pre-Publish"; falls back to the raw gate string. */
  gateLabel: string;
  title: string;
  channelLabel?: string;
  nicheLabel?: string;
  status?: string;
  /** The recorded decision (`approved` / `rejected`) once decided. */
  decision?: string;
  createdAtMs?: number;
  /** "2h 14m" style age in queue, computed from createdAtMs. */
  waitingLabel?: string;
  scriptLines: string[];
  scriptVersion?: string;
  checks: ApprovalCheck[];
  annotations: ApprovalAnnotation[];
  /** Only http(s)/root-relative URLs are treated as playable. */
  draftUrl?: string;
  draftPosterUrl?: string;
  draftVersion?: string;
  draftAgentLabel?: string;
  draftDurationLabel?: string;
  sceneVariants: SceneVariant[];
  thumbnails: ThumbnailVariant[];
  thumbnailVersion?: string;
  metadata?: ApprovalMetadata;
  metadataVersion?: string;
  costLabel?: string;
}

// ── Normalisation helpers ─────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Records pass through; JSON strings (`payload_json` style) are parsed. */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{")) {
      try {
        const parsed: unknown = JSON.parse(trimmed);
        return isRecord(parsed) ? parsed : null;
      } catch {
        return null;
      }
    }
  }
  return null;
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("[")) {
      try {
        const parsed: unknown = JSON.parse(trimmed);
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }
  }
  return [];
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

function num(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function bool(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (value === 1) return true;
  if (value === 0) return false;
  return undefined;
}

/** First defined value among the given keys of `bag`. */
function pick(bag: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = bag[key];
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

function parseTimestampMs(value: unknown): number | undefined {
  const asNumber = num(value);
  if (asNumber !== undefined) {
    // Schema convention is INTEGER epochs; accept seconds or millis.
    return asNumber < 1e12 ? asNumber * 1000 : asNumber;
  }
  const asString = str(value);
  if (asString) {
    const parsed = Date.parse(asString);
    return Number.isNaN(parsed) ? undefined : parsed;
  }
  return undefined;
}

function versionLabel(value: unknown): string | undefined {
  const asNumber = num(value);
  if (asNumber !== undefined) return `v${asNumber}`;
  const asString = str(value);
  if (!asString) return undefined;
  return /^v\d/i.test(asString) ? asString : `v${asString}`;
}

function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

function durationLabel(value: unknown): string | undefined {
  const asString = str(value);
  if (asString) return asString;
  const asNumber = num(value);
  return asNumber !== undefined ? formatDuration(asNumber) : undefined;
}

function waitingLabel(createdAtMs: number, nowMs: number): string {
  const mins = Math.max(0, Math.floor((nowMs - createdAtMs) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${String(mins % 60).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function playableUrl(value: unknown): string | undefined {
  const asString = str(value);
  if (!asString) return undefined;
  if (/^https?:\/\//i.test(asString) || asString.startsWith("/")) return asString;
  return undefined;
}

function gateKind(rawGate: string | undefined): GateKind {
  if (!rawGate) return "unknown";
  const g = rawGate.trim().toLowerCase();
  if (g === "a" || g === "gate_a" || g.includes("gate a")) return "A";
  if (g === "b" || g === "gate_b" || g.includes("gate b")) return "B";
  if (g.includes("script")) return "A";
  if (g.includes("pre-publish") || g.includes("prepublish") || g.includes("publish")) return "B";
  return "unknown";
}

function gateLabel(kind: GateKind, rawGate: string | undefined): string {
  if (kind === "A") return "Gate A · Script Lock";
  if (kind === "B") return "Gate B · Pre-Publish";
  return rawGate ?? "Approval";
}

function toneOf(value: unknown): ApprovalAnnotation["tone"] {
  const s = typeof value === "string" ? value.toLowerCase() : "";
  if (["red", "fail", "failed", "error", "danger", "blocked"].some((k) => s.includes(k))) {
    return "danger";
  }
  if (["amber", "warn", "warning", "unverifiable"].some((k) => s.includes(k))) return "warn";
  if (["green", "pass", "passed", "ok", "verified"].some((k) => s.includes(k))) return "ok";
  return "neutral";
}

// ── Normaliser ────────────────────────────────────────────────────────────

export function normalizeApprovalItem(raw: unknown): ApprovalItemView | null {
  const row = asRecord(raw);
  if (!row) return null;

  // Merge any nested payload/detail bag under the row itself (row wins).
  const nested =
    asRecord(pick(row, ["payload_json", "payload", "detail_json", "detail", "context_json", "context"])) ??
    {};
  const bag: Record<string, unknown> = { ...nested, ...row };

  const id = str(pick(bag, ["id", "approval_id", "approvalId"]));
  if (!id) return null;

  const projectId = str(pick(bag, ["project_id", "projectId"]));
  const rawGate = str(pick(bag, ["gate", "gate_type", "gate_kind", "gate_name"]));
  const gate = gateKind(rawGate);

  // Artefact versions: explicit map, artefact rows, or flat fields.
  const versions = asRecord(pick(bag, ["artefact_versions", "artefact_versions_json", "versions"])) ?? {};
  const artefactRows = asArray(pick(bag, ["artefacts", "artefact_versions_list"]));
  const versionFor = (typeKeys: string[], flatKeys: string[]): string | undefined => {
    for (const key of typeKeys) {
      const label = versionLabel(versions[key]);
      if (label) return label;
    }
    for (const entry of artefactRows) {
      const rec = asRecord(entry);
      if (!rec) continue;
      const type = str(pick(rec, ["artefact_type", "type", "kind"]));
      if (type && typeKeys.includes(type)) {
        const label = versionLabel(pick(rec, ["version"]));
        if (label) return label;
      }
    }
    return versionLabel(pick(bag, flatKeys));
  };

  // Metadata pack (may be a nested record or JSON string).
  const metadataRec = asRecord(pick(bag, ["metadata", "metadata_pack", "metadata_json"]));
  let metadata: ApprovalMetadata | undefined;
  if (metadataRec) {
    const tagsRaw = pick(metadataRec, ["tags", "tag_list"]);
    const tags = Array.isArray(tagsRaw)
      ? tagsRaw.filter((t): t is string => typeof t === "string")
      : typeof tagsRaw === "string"
        ? tagsRaw.split(",").map((t) => t.trim()).filter(Boolean)
        : [];
    metadata = {
      title: str(pick(metadataRec, ["title"])),
      description: str(pick(metadataRec, ["description"])),
      tags,
      syntheticMedia: bool(
        pick(metadataRec, ["synthetic_media", "syntheticMedia", "altered_content", "ai_disclosure"]),
      ),
    };
  }

  const title =
    str(pick(bag, ["title", "working_title", "video_title", "topic"])) ??
    metadata?.title ??
    (projectId ? `Project ${projectId}` : `Approval ${id}`);

  // Script: a string (split into lines) or an array of lines/sections.
  const scriptRaw = pick(bag, ["script_text", "script", "script_excerpt", "script_lines"]);
  let scriptLines: string[] = [];
  if (typeof scriptRaw === "string" && scriptRaw.trim() !== "") {
    scriptLines = scriptRaw.split(/\r?\n+/).map((l) => l.trim()).filter(Boolean);
  } else if (Array.isArray(scriptRaw)) {
    scriptLines = scriptRaw
      .map((entry) =>
        typeof entry === "string" ? entry : str(asRecord(entry)?.text ?? asRecord(entry)?.line),
      )
      .filter((l): l is string => typeof l === "string" && l.trim() !== "");
  }

  // QA / gate checks: a list, a checks map, or flat per-check status fields.
  const qaRec = asRecord(pick(bag, ["qa_report", "qa", "qa_json", "qa_reports"])) ?? {};
  const checks: ApprovalCheck[] = [];
  const checksRaw = pick(bag, ["checks", "gate_checks", "qa_checks"]) ?? pick(qaRec, ["checks"]);
  if (Array.isArray(checksRaw)) {
    for (const entry of checksRaw) {
      const rec = asRecord(entry);
      if (!rec) continue;
      const label = str(pick(rec, ["label", "name", "check", "id"]));
      const status = str(pick(rec, ["status", "verdict", "result"]));
      if (label && status) checks.push({ label, status });
    }
  } else if (isRecord(checksRaw)) {
    for (const [label, status] of Object.entries(checksRaw)) {
      const s = str(status) ?? (isRecord(status) ? str(pick(status, ["status", "verdict"])) : undefined);
      if (s) checks.push({ label, status: s });
    }
  }
  if (checks.length === 0) {
    const flat: Array<[string, string[]]> = [
      ["QC", ["qc_status", "qc"]],
      ["Copyright", ["copyright_status", "copyright"]],
      ["Policy", ["policy_status", "policy"]],
      ["Fact-Check", ["factcheck_status", "fact_check_status", "fact_check"]],
    ];
    for (const [label, keys] of flat) {
      const status = str(pick(bag, keys)) ?? str(pick(qaRec, keys));
      if (status) checks.push({ label, status });
    }
  }

  // QA annotations: explicit list, or nested inside the QA report.
  const annotations: ApprovalAnnotation[] = [];
  const annotationsRaw = pick(bag, ["annotations", "qa_annotations"]) ?? pick(qaRec, ["annotations", "notes"]);
  for (const entry of asArray(annotationsRaw)) {
    if (typeof entry === "string" && entry.trim() !== "") {
      annotations.push({ text: entry, tone: "neutral" });
      continue;
    }
    const rec = asRecord(entry);
    if (!rec) continue;
    const text = str(pick(rec, ["text", "note", "message", "detail"]));
    if (!text) continue;
    annotations.push({
      label: str(pick(rec, ["label", "source", "agent", "type"])),
      text,
      tone: toneOf(pick(rec, ["tone", "severity", "status", "verdict"])),
    });
  }

  // Scene variants (per-scene choices, when the payload carries them).
  const sceneVariants: SceneVariant[] = asArray(pick(bag, ["scene_variants", "scenes"]))
    .map((entry, index): SceneVariant | null => {
      const rec = asRecord(entry);
      if (!rec) return null;
      return {
        id: str(pick(rec, ["id"])) ?? `scene-${index}`,
        label:
          str(pick(rec, ["label", "name", "title"])) ?? `Variant ${String.fromCharCode(65 + index)}`,
        durationLabel: durationLabel(pick(rec, ["duration", "duration_label", "duration_seconds"])),
        selected: bool(pick(rec, ["selected", "is_selected"])) ?? index === 0,
      };
    })
    .filter((v): v is SceneVariant => v !== null);

  // Thumbnail variants: URL strings or variant records.
  const thumbnails: ThumbnailVariant[] = asArray(
    pick(bag, ["thumbnails", "thumbnail_variants", "thumbnail_urls"]),
  )
    .map((entry, index): ThumbnailVariant | null => {
      if (typeof entry === "string") {
        return {
          id: `thumb-${index}`,
          label: `Variant ${String.fromCharCode(65 + index)}`,
          imageUrl: playableUrl(entry),
          selected: index === 0,
        };
      }
      const rec = asRecord(entry);
      if (!rec) return null;
      return {
        id: str(pick(rec, ["id"])) ?? `thumb-${index}`,
        label: str(pick(rec, ["label", "name"])) ?? `Variant ${String.fromCharCode(65 + index)}`,
        imageUrl: playableUrl(pick(rec, ["image_url", "url", "thumbnail_url", "src"])),
        text: str(pick(rec, ["text", "overlay_text"])),
        score: num(pick(rec, ["score", "ctr_score"])),
        selected: bool(pick(rec, ["selected", "is_selected"])) ?? index === 0,
      };
    })
    .filter((v): v is ThumbnailVariant => v !== null);

  // Cost so far: micros (schema convention) or a plain USD figure.
  const costMicros = num(pick(bag, ["total_cost_micros", "cost_micros", "cost_so_far_micros"]));
  const costUsd = num(pick(bag, ["cost_usd", "cost_so_far_usd", "cost"]));
  const costLabel =
    costMicros !== undefined
      ? `$${(costMicros / 1_000_000).toFixed(2)}`
      : costUsd !== undefined
        ? `$${costUsd.toFixed(2)}`
        : undefined;

  const createdAtMs = parseTimestampMs(
    pick(bag, ["created_at", "createdAt", "queued_at", "requested_at"]),
  );

  return {
    id,
    projectId,
    gate,
    gateLabel: gateLabel(gate, rawGate),
    title,
    channelLabel: str(pick(bag, ["channel", "channel_name", "channel_title", "channel_id", "channelId"])),
    nicheLabel: str(pick(bag, ["niche", "niche_name", "niche_profile_name"])),
    status: str(pick(bag, ["status", "state"])),
    decision: str(pick(bag, ["decision"])),
    createdAtMs,
    waitingLabel: createdAtMs !== undefined ? waitingLabel(createdAtMs, Date.now()) : undefined,
    scriptLines,
    scriptVersion: versionFor(["script"], ["script_version", "script_v"]),
    checks,
    annotations,
    draftUrl: playableUrl(pick(bag, ["draft_url", "preview_url", "video_url", "draft_video_url"])),
    draftPosterUrl: playableUrl(pick(bag, ["draft_poster_url", "poster_url", "poster"])),
    draftVersion: versionFor(["draft", "rough_cut", "master_video"], ["draft_version", "draft_v"]),
    draftAgentLabel: str(pick(bag, ["draft_agent", "produced_by", "render_agent"])),
    draftDurationLabel: durationLabel(
      pick(bag, ["duration_label", "duration", "duration_seconds", "draft_duration"]),
    ),
    sceneVariants,
    thumbnails,
    thumbnailVersion: versionFor(["thumbnail"], ["thumbnail_version", "thumbnail_v"]),
    metadata,
    metadataVersion: versionFor(["metadata", "metadata_pack"], ["metadata_version", "metadata_v"]),
    costLabel,
  };
}

/**
 * True while an item is still waiting for a human decision.
 *
 * The backend list keeps decided rows (see the header note), so queue
 * views must apply this filter — an item leaves the pending queue for
 * good once the backend records its decision, across remounts,
 * refetches and other windows, not just in one session's local state.
 */
export function isPendingApproval(item: ApprovalItemView): boolean {
  if ((item.status ?? "").trim().toLowerCase() === "decided") return false;
  if ((item.decision ?? "").trim() !== "") return false;
  return true;
}

// ── Error classification (branch on the stable code, never the message) ──

/** 409 from a second decision — backend code `already_decided`. */
export function isAlreadyDecidedError(err: unknown): boolean {
  return err instanceof ApiError && (err.code === "already_decided" || err.status === 409);
}

/** Item vanished from the queue between load and decision. */
export function isApprovalGoneError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

/** Backend unreachable (fetch failed before any response). */
export function isOfflineError(err: unknown): boolean {
  return err instanceof ApiError && (err.code === "NETWORK_ERROR" || err.status === 0);
}

// ── Hooks ─────────────────────────────────────────────────────────────────

export function useApprovals() {
  return useQuery({
    queryKey: ["approvals"],
    queryFn: async (): Promise<ApprovalItemView[]> => {
      const client = await api();
      const page = await client.listApprovals();
      const rawItems: unknown[] = Array.isArray(page?.items) ? page.items : [];
      return rawItems
        .map(normalizeApprovalItem)
        .filter((item): item is ApprovalItemView => item !== null);
    },
  });
}

export interface DecideApprovalInput {
  id: string;
  decision: "approve" | "reject";
  /** Required for reject — one of {@link REJECT_REASONS}. */
  reasonCode?: RejectReasonCode;
  note?: string;
}

export function useDecideApproval() {
  const queryClient = useQueryClient();
  return useMutation<ApprovalItem, ApiError, DecideApprovalInput>({
    mutationFn: async (input) => {
      const client = await api();
      // The shared client sends the payload verbatim; the backend's
      // DecisionBody reads snake_case `reason_code`, so include both
      // spellings (the camelCase field alone would be dropped and a
      // reject would 422 with `reason_code_required`).
      const payload: DecideApprovalPayload & { reason_code?: string } = {
        decision: input.decision,
        reasonCode: input.reasonCode,
        note: input.note,
        reason_code: input.reasonCode,
      };
      return client.decideApproval(input.id, payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err) => {
      // Conflict / vanished item: the queue we rendered is stale — refresh it.
      if (isAlreadyDecidedError(err) || isApprovalGoneError(err)) {
        void queryClient.invalidateQueries({ queryKey: ["approvals"] });
      }
    },
  });
}
