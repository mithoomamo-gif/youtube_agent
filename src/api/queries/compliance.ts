/**
 * React Query hooks for the Compliance Gate (Round 6).
 *
 * Wire shapes mirror the backend exactly (snake_case):
 *
 * - `GET  /settings/compliance`             -> `ComplianceWire`: the
 *   stored checklist (per item: status, evidence, note, who/when
 *   stamps), the DERIVED overall state, the practical `effect`, and
 *   the recent transition history.
 * - `POST /settings/compliance/transitions` body
 *   `{ item_key, to_status, actor, evidence?, note? }` -> the full
 *   new `ComplianceWire`. This is the ONLY write path: there is no
 *   free-form state edit and no "mark passed" flip — a `passed`
 *   transition without evidence is refused by the backend with
 *   422 `compliance_evidence_required`, and every accepted
 *   transition is appended to the audit history with who/when.
 *
 * While `overall` is not `"passed"`, Gate C forces uploads to
 * Private and refuses public publishes (typed reason
 * `compliance_not_passed`); the wire `effect` states this plainly.
 *
 * Like providers.ts, this module uses its own small fetch helper
 * (same token header + error envelope as `ApiClient`).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export type ComplianceItemStatusWire =
  | "pending"
  | "in_progress"
  | "passed"
  | "failed";

export type ComplianceOverallWire =
  | "not_started"
  | "in_progress"
  | "passed";

export interface ComplianceItemWire {
  key: string;
  label: string;
  description: string;
  status: ComplianceItemStatusWire;
  evidence: string | null;
  note: string | null;
  updated_by: string | null;
  /** Epoch milliseconds; null until the item first transitions. */
  updated_at: number | null;
  created_at: number | null;
}

export interface ComplianceTransitionWire {
  id: string;
  item_key: string;
  from_status: ComplianceItemStatusWire;
  to_status: ComplianceItemStatusWire;
  actor: string;
  evidence: string | null;
  note: string | null;
  /** Epoch milliseconds. */
  created_at: number;
}

export interface ComplianceWire {
  overall: ComplianceOverallWire;
  passed: boolean;
  public_publishing_allowed: boolean;
  forced_private: boolean;
  reason_code: string | null;
  /** Plain-language statement of the practical effect. */
  effect: string;
  items: ComplianceItemWire[];
  history: ComplianceTransitionWire[];
}

export interface ComplianceTransitionInput {
  item_key: string;
  to_status: ComplianceItemStatusWire;
  actor: string;
  evidence?: string;
  note?: string;
}

// ── Minimal request helper (mirrors providers.ts / ApiClient) ──────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function randomId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");

  const headers: Record<string, string> = { "X-Local-Token": conn.token };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method.toUpperCase() === "POST") headers["Idempotency-Key"] = randomId();

  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
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

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let parsed: unknown;
  if (text) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    if (isRecord(parsed) && isRecord(parsed.error)) {
      const err = parsed.error;
      throw new ApiError({
        code: typeof err.code === "string" ? err.code : `HTTP_${res.status}`,
        message:
          typeof err.message === "string"
            ? err.message
            : `Request failed (${res.status})`,
        status: res.status,
        retryable:
          typeof err.retryable === "boolean"
            ? err.retryable
            : res.status === 429 || res.status >= 500,
        requestId: typeof err.request_id === "string" ? err.request_id : undefined,
        details: err.details,
      });
    }
    throw new ApiError({
      code: `HTTP_${res.status}`,
      message: `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status === 429 || res.status >= 500,
    });
  }

  return parsed as T;
}

// ── Hooks ──────────────────────────────────────────────────────────────────

export function useCompliance() {
  return useQuery({
    queryKey: ["settings", "compliance"],
    queryFn: async (): Promise<ComplianceWire> =>
      request<ComplianceWire>("GET", "/settings/compliance"),
    retry: 1,
  });
}

export function useComplianceTransition() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ComplianceTransitionInput): Promise<ComplianceWire> =>
      request<ComplianceWire>("POST", "/settings/compliance/transitions", input),
    onSuccess: (state) => {
      queryClient.setQueryData(["settings", "compliance"], state);
      void queryClient.invalidateQueries({ queryKey: ["settings", "compliance"] });
    },
  });
}
