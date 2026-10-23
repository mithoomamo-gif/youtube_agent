/**
 * Growth data layer (Round 5 — W6: Growth view surfaces).
 *
 * Two of the three Growth surfaces read from the learning team's
 * LANDED HTTP seam (backend/app/routers/learning.py +
 * app.learning.service / app.learning.consumption); the third
 * (comment themes, questions, reply drafts) reuses the comments
 * team's own data layer (`src/api/queries/comments.ts`) — see
 * CommentInsightsSection. Contracts wired here:
 *
 * - `GET /channels/{channelId}/learning/rules?niche={niche}`
 *     -> `{ channel_id, niche, items, count, next_cursor }`.
 *     REAL endpoint. `niche` is REQUIRED by the backend (422
 *     `niche_required` without it): learning rules are scoped
 *     `(channel_id, niche)` and never pooled across niches. The
 *     niche scope for a channel is its default niche profile's
 *     `key` (falling back to the profile id — builtins have
 *     id == key). Items are the consumption layer's normalised
 *     rules: `{ rule_id, version, kind, statement, statement_family,
 *     evidence_count, confidence, effect_value, effect_unit,
 *     is_thin_evidence }` — the store's thin-evidence label is
 *     surfaced verbatim; thin rules are shown, never hidden.
 *
 * - `GET /channels/{channelId}/learning/publish-window`
 *     -> REAL endpoint; the service returns its eligibility
 *     structure verbatim:
 *     `{ channel_id, eligible, status, reason, suggestions,
 *        evidence, source }`. Eligible form: `eligible: true`,
 *     `status: "eligible"`, `reason: null`, one suggestion per niche
 *     derived from that niche's best ACTIVE `publish_window` rule
 *     (`{ niche, window, statement, effect_value, effect_unit,
 *        evidence_count, confidence, is_thin_evidence, rule_id,
 *        version }`). Not-enough-data form: `eligible: false`,
 *     `status: "insufficient_data"`, `reason: "not_enough_data"` (a
 *     CODE, humanised by the UI), `suggestions: []`, and `evidence`
 *     carrying the observed counts (`active_publish_window_rules`,
 *     `candidate_publish_window_rules`, `niches_considered`,
 *     `minimum_required.active_publish_window_rules`). The UI never
 *     upgrades an ineligible / unreported state into an actionable
 *     suggestion.
 *
 * - `GET /niches?limit=200` — used only to resolve a channel's
 *   default niche profile to its learning scope key (the niches
 *   router's rows carry both `id` and `key`).
 *
 * Honesty rules (project-wide):
 * - A 404 from a seam means that route is not served yet (or the
 *   channel does not exist). It is surfaced as a distinct "not
 *   available yet" state ({@link SeamResult.available} = false),
 *   never as empty data and never as an error banner.
 * - Empty really means empty: no rule or window is ever invented
 *   client-side. Missing optional fields render as absent.
 * - Timestamps follow the backend convention (epoch-ms numbers or
 *   ISO strings); both are tolerated.
 *
 * This module uses its own small fetch helpers (same token header +
 * error envelope as `ApiClient`), following the styleLab.ts /
 * channels.ts / comments.ts precedent for team-owned seams.
 */

import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Seam result ───────────────────────────────────────────────────────────

/**
 * Every Growth seam resolves to one of these. `available: false` is
 * reserved for "the backend does not serve this route" (HTTP 404);
 * genuine failures still throw and surface as query errors.
 */
export type SeamResult<T> =
  | { available: true; data: T }
  | { available: false; data: null };

// ── Wire types (backend snake_case, exactly as emitted) ──────────────────

export interface LearningRuleWire {
  id: string;
  kind: string;
  statement_family: string | null;
  version: number | null;
  /** The evidence-backed statement itself — rendered verbatim. */
  statement: string;
  effect_value: number | null;
  effect_unit: string | null;
  evidence_count: number;
  /** 0–1, or null when the store recorded no confidence. */
  confidence: number | null;
  /** Store-computed thin-evidence flag (below minimum evidence). */
  thin_evidence: boolean;
  created_at: string | number | null;
}

export interface PublishWindowWire {
  /** The suggestion's window label (the rule's statement family). */
  label: string | null;
  /** The learned rule statement behind the suggestion. */
  statement: string | null;
  niche: string | null;
  days: string[];
  start_local: string | null;
  end_local: string | null;
  evidence_count: number | null;
  confidence: number | null;
  thin_evidence: boolean;
}

export interface PublishWindowsWire {
  channel_id: string | null;
  /** null = the seam did not report eligibility; never assumed true. */
  eligible: boolean | null;
  /** e.g. "eligible" | "insufficient_data", verbatim from the seam. */
  status: string | null;
  /** Reason CODE from the seam (e.g. "not_enough_data"), or null. */
  reason_code: string | null;
  windows: PublishWindowWire[];
  active_rule_count: number | null;
  candidate_rule_count: number | null;
  min_active_rules: number | null;
  niches_considered: string[];
  source: string | null;
}

export interface NicheProfileWire {
  id: string;
  key: string | null;
  name: string | null;
}

// ── Minimal request helpers (mirror ApiClient's envelope handling) ────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function toApiErrorFromBody(status: number, parsed: unknown): ApiError {
  if (isRecord(parsed) && isRecord(parsed.error)) {
    const err = parsed.error;
    return new ApiError({
      code: typeof err.code === "string" ? err.code : `HTTP_${status}`,
      message:
        typeof err.message === "string"
          ? err.message
          : `Request failed (${status})`,
      status,
      retryable:
        typeof err.retryable === "boolean"
          ? err.retryable
          : status === 429 || status >= 500,
      requestId: typeof err.request_id === "string" ? err.request_id : undefined,
      details: err.details,
    });
  }
  return new ApiError({
    code: `HTTP_${status}`,
    message: `Request failed (${status})`,
    status,
    retryable: status === 429 || status >= 500,
  });
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function requestRaw(path: string): Promise<unknown> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method: "GET",
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
  const parsed = await parseBody(res);
  if (!res.ok) throw toApiErrorFromBody(res.status, parsed);
  return parsed;
}

/**
 * GET one seam. HTTP 404 (route not served) resolves to
 * `{ available: false }`; every other failure throws as an ApiError.
 */
async function fetchSeam<T>(
  path: string,
  normalise: (raw: unknown) => T,
): Promise<SeamResult<T>> {
  try {
    const raw = await requestRaw(path);
    return { available: true, data: normalise(raw) };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return { available: false, data: null };
    }
    throw err;
  }
}

/** List responses: `{ items }` envelope assumed; bare arrays tolerated. */
function listItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw) && Array.isArray(raw.items)) return raw.items;
  return [];
}

// ── Field helpers ─────────────────────────────────────────────────────────

function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function timeOrNull(value: unknown): string | number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return strOrNull(value);
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

// ── Normalisers ───────────────────────────────────────────────────────────

function toLearningRule(raw: unknown): LearningRuleWire | null {
  if (!isRecord(raw)) return null;
  // The landed endpoint emits normalised rules keyed by `rule_id`;
  // raw Learning Store rows (`id`, 0/1 `thin_evidence`) are also
  // accepted so the seam survives either shape.
  const id = strOrNull(raw.rule_id ?? raw.id);
  const statement = strOrNull(raw.statement);
  if (!id || !statement) return null;
  const thin =
    typeof raw.is_thin_evidence === "boolean"
      ? raw.is_thin_evidence
      : raw.thin_evidence === 1 || raw.thin_evidence === true;
  const evidence = numOrNull(raw.evidence_count);
  return {
    id,
    kind: strOrNull(raw.kind) ?? "",
    statement_family: strOrNull(raw.statement_family),
    version: numOrNull(raw.version),
    statement,
    effect_value: numOrNull(raw.effect_value),
    effect_unit: strOrNull(raw.effect_unit),
    evidence_count: evidence ?? 0,
    confidence: numOrNull(raw.confidence),
    thin_evidence: thin,
    created_at: timeOrNull(raw.created_at ?? raw.updated_at),
  };
}

function toPublishWindow(raw: unknown): PublishWindowWire | null {
  if (!isRecord(raw)) return null;
  const days = stringList(raw.days ?? raw.weekdays ?? raw.day_names);
  const singleDay = strOrNull(raw.weekday ?? raw.day);
  const thin =
    typeof raw.is_thin_evidence === "boolean"
      ? raw.is_thin_evidence
      : raw.thin_evidence === 1 || raw.thin_evidence === true;
  return {
    label: strOrNull(raw.window ?? raw.label ?? raw.name),
    statement: strOrNull(raw.statement),
    niche: strOrNull(raw.niche),
    days: days.length > 0 ? days : singleDay ? [singleDay] : [],
    start_local: strOrNull(
      raw.start_local ?? raw.start_time_local ?? raw.start_time ?? raw.start,
    ),
    end_local: strOrNull(
      raw.end_local ?? raw.end_time_local ?? raw.end_time ?? raw.end,
    ),
    evidence_count: numOrNull(raw.evidence_count),
    confidence: numOrNull(raw.confidence),
    thin_evidence: thin,
  };
}

function toPublishWindows(raw: unknown): PublishWindowsWire {
  const r = isRecord(raw) ? raw : {};
  const evidence = isRecord(r.evidence) ? r.evidence : {};
  const minimum = isRecord(evidence.minimum_required)
    ? evidence.minimum_required
    : {};
  const windowsRaw = Array.isArray(r.suggestions)
    ? r.suggestions
    : Array.isArray(r.windows)
      ? r.windows
      : [];
  // `reason` is a plain string in the landed contract (a code like
  // "not_enough_data", or null when eligible). Some sibling shapes
  // may carry prose instead — anything containing a space is treated
  // as prose and kept out of the code slot.
  const reasonRaw = strOrNull(r.reason ?? r.eligibility_reason);
  return {
    channel_id: strOrNull(r.channel_id),
    eligible: typeof r.eligible === "boolean" ? r.eligible : null,
    status: strOrNull(r.status),
    reason_code:
      reasonRaw && !reasonRaw.includes(" ") ? reasonRaw : null,
    windows: windowsRaw
      .map(toPublishWindow)
      .filter((w): w is PublishWindowWire => w !== null),
    active_rule_count: numOrNull(
      evidence.active_publish_window_rules ?? r.active_rule_count,
    ),
    candidate_rule_count: numOrNull(
      evidence.candidate_publish_window_rules ?? r.candidate_rule_count,
    ),
    min_active_rules: numOrNull(
      minimum.active_publish_window_rules ?? r.min_active_rules,
    ),
    niches_considered: stringList(evidence.niches_considered),
    source: strOrNull(r.source),
  };
}

// ── Hooks ─────────────────────────────────────────────────────────────────

/** Niche profiles (id + key + name) for learning-scope resolution. */
export function useNicheProfiles() {
  return useQuery({
    queryKey: ["growth", "niche-profiles"],
    retry: 1,
    queryFn: async (): Promise<NicheProfileWire[]> => {
      const raw = await requestRaw("/niches?limit=200");
      return listItems(raw)
        .filter(isRecord)
        .map((item) => ({
          id: strOrNull(item.id) ?? "",
          key: strOrNull(item.key),
          name: strOrNull(item.name),
        }))
        .filter((p) => p.id.length > 0);
    },
  });
}

/**
 * Active learning rules for exactly (channelId, niche). Disabled
 * until a niche scope is known — the backend refuses unscoped reads
 * (422), and this layer never guesses a scope.
 */
export function useLearningRules(
  channelId: string | null,
  niche: string | null,
) {
  return useQuery({
    queryKey: ["growth", "learning-rules", channelId, niche],
    enabled: channelId !== null && niche !== null,
    retry: 1,
    queryFn: (): Promise<SeamResult<LearningRuleWire[]>> =>
      fetchSeam(
        `/channels/${encodeURIComponent(channelId ?? "")}/learning/rules?niche=${encodeURIComponent(niche ?? "")}`,
        (raw) =>
          listItems(raw)
            .map(toLearningRule)
            .filter((r): r is LearningRuleWire => r !== null),
      ),
  });
}

export function usePublishWindows(channelId: string | null) {
  return useQuery({
    queryKey: ["growth", "publish-windows", channelId],
    enabled: channelId !== null,
    retry: 1,
    queryFn: (): Promise<SeamResult<PublishWindowsWire>> =>
      fetchSeam(
        `/channels/${encodeURIComponent(channelId ?? "")}/learning/publish-window`,
        toPublishWindows,
      ),
  });
}
