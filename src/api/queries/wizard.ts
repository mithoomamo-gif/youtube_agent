/**
 * React Query layer for the New Video wizard (Round 2 live binding).
 *
 * Wire shapes are defined locally here (per Round 2 conventions) and are
 * taken from the backend routers, which are the source of truth:
 *
 * - GET  /niches   backend/app/routers/niches.py
 *     `{ items: [{ id, name, source: "builtin" | "other", idea?, … }],
 *        next_cursor }` — 8 builtins merged with persisted user profiles.
 * - POST /niches   body `{ name, idea }` → 201 profile.
 *     Validation: name 2–40 chars, idea 10–500 chars; failures are 422
 *     with `details: { field: "name" | "idea", length }`.
 * - GET  /channels backend/app/routers/channels.py
 *     Raw `channels` table rows (snake_case: `title`, `handle`,
 *     `status`, `language`, …) with credential columns stripped.
 * - POST /projects backend/app/routers/projects.py
 *     Body `{ channel_id, niche_profile_id, topic_mode, topic?,
 *     target_minutes, publish_mode }` → 201 project. Requires an
 *     `Idempotency-Key` header (400 when missing).
 *
 * NOTE on field naming: the backend speaks snake_case end to end, while
 * `src/api/types.ts` declares camelCase payload types. `ApiClient` sends
 * the body verbatim (it only JSON-stringifies), so the payloads built
 * here use the backend's snake_case keys and are cast at the call site.
 *
 * NOTE on idempotency: `ApiClient.request()` adds an
 * `Idempotency-Key` header to EVERY POST automatically
 * (`headers["Idempotency-Key"] = opts.idempotencyKey ?? randomId()` in
 * client.ts), so the backend's key requirement is satisfied. The
 * public methods `createProject` / `createNiche` also accept an
 * optional caller-supplied key (coordinator integration patch,
 * Round 2): the launch mutation below generates one UUID per attempt
 * and forwards it, so retrying the same attempt replays the original
 * response instead of duplicating the project; the niche mutation
 * derives a content-stable key from the draft (name+idea) for the
 * same reason. A single-flight flag + the Launch button being
 * disabled while pending additionally guard against a second POST
 * being issued at all.
 */

import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiClient } from "../client";
import { getBackendConnection } from "../tauri";
import { ApiError } from "../types";
import type { CreateProjectPayload } from "../types";

// ── Local wire types ──────────────────────────────────────────────────────

export interface WizardNiche {
  id: string;
  name: string;
  source: "builtin" | "other";
  /** The user's one-line idea; present on `source: "other"` profiles. */
  idea: string | null;
  status: string | null;
}

export interface WizardChannel {
  id: string;
  displayName: string;
  handle: string | null;
  status: string | null;
  language: string | null;
}

export interface WizardProject {
  id: string;
  status: string | null;
}

export interface CreateNicheInput {
  name: string;
  idea: string;
}

export interface CreateProjectInput {
  channelId: string;
  nicheProfileId: string;
  topicMode: "auto" | "manual";
  /** Required when `topicMode` is "manual"; omitted for "auto". */
  topic?: string;
  targetMinutes: number;
  /** Wire value: "review_first" | "auto". */
  publishMode: string;
}

// ── Helpers ───────────────────────────────────────────────────────────────

async function getClient(): Promise<ApiClient> {
  const conn = await getBackendConnection();
  return new ApiClient(conn);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function normalizeNiche(raw: unknown): WizardNiche | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    name: str(raw.name) ?? str(raw.label) ?? str(raw.key) ?? id,
    source: raw.source === "builtin" ? "builtin" : "other",
    idea: str(raw.idea) ?? str(raw.user_idea),
    status: str(raw.status),
  };
}

function normalizeChannel(raw: unknown): WizardChannel | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const handle = str(raw.handle);
  return {
    id,
    displayName: str(raw.title) ?? str(raw.name) ?? handle ?? id,
    handle,
    status: str(raw.status) ?? str(raw.state),
    language: str(raw.language),
  };
}

function normalizeProject(raw: unknown): WizardProject | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return { id, status: str(raw.status) };
}

/** Human-readable message from the standard error envelope. */
export function getErrorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Request failed";
}

export interface NicheFieldErrors {
  name?: string;
  idea?: string;
  form?: string;
}

/**
 * Client-side validation mirroring niches.py exactly:
 * name 2–40 chars, idea 10–500 chars (both after trimming).
 * Returns null when the draft is valid.
 */
export function validateOtherNiche(name: string, idea: string): NicheFieldErrors | null {
  const errors: NicheFieldErrors = {};
  const trimmedName = name.trim();
  const trimmedIdea = idea.trim();
  if (trimmedName.length < 2 || trimmedName.length > 40) {
    errors.name = `Niche name 2–40 characters ka hona chahiye (abhi ${trimmedName.length}).`;
  }
  if (trimmedIdea.length < 10 || trimmedIdea.length > 500) {
    errors.idea = `Niche idea 10–500 characters ka hona chahiye (abhi ${trimmedIdea.length}).`;
  }
  return Object.keys(errors).length > 0 ? errors : null;
}

/**
 * Maps a failed POST /niches onto inline field errors. Handles both
 * envelope shapes the backend produces: the router's own
 * `details: { field, length }` and FastAPI's request-validation
 * `details: { errors: [{ loc, msg }] }`. Anything else becomes a
 * form-level error carrying the envelope message.
 */
export function parseNicheFieldErrors(err: unknown): NicheFieldErrors {
  if (!(err instanceof ApiError)) {
    return { form: getErrorMessage(err) };
  }
  const details = err.details;
  if (isRecord(details)) {
    const field = details.field;
    if (field === "name" || field === "idea") {
      return { [field]: err.message };
    }
    if (Array.isArray(details.errors)) {
      const errors: NicheFieldErrors = {};
      for (const entry of details.errors) {
        if (!isRecord(entry) || !Array.isArray(entry.loc)) continue;
        const loc: unknown = entry.loc[entry.loc.length - 1];
        if (loc === "name" || loc === "idea") {
          errors[loc] = typeof entry.msg === "string" ? entry.msg : err.message;
        }
      }
      if (Object.keys(errors).length > 0) return errors;
    }
  }
  return { form: err.message };
}

// ── Queries ───────────────────────────────────────────────────────────────

export function useWizardNiches() {
  return useQuery({
    queryKey: ["niches"],
    retry: 1,
    queryFn: async (): Promise<WizardNiche[]> => {
      const client = await getClient();
      const page = await client.listNiches();
      const items = (page.items as unknown[]).map(normalizeNiche);
      return items.filter((n): n is WizardNiche => n !== null);
    },
  });
}

export function useWizardChannels() {
  return useQuery({
    queryKey: ["channels"],
    retry: 1,
    queryFn: async (): Promise<WizardChannel[]> => {
      const client = await getClient();
      const page = await client.listChannels();
      const items = (page.items as unknown[]).map(normalizeChannel);
      return items.filter((c): c is WizardChannel => c !== null);
    },
  });
}

// ── Mutations ─────────────────────────────────────────────────────────────

export function useCreateNicheProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateNicheInput): Promise<WizardNiche> => {
      const client = await getClient();
      // Content-stable idempotency key: re-saving the SAME draft (e.g.
      // a retry after a network failure) replays the original 201
      // instead of creating a duplicate profile; editing the draft
      // changes the key. (FNV-1a over name+idea.)
      let hash = 0x811c9dc5;
      const seed = `${input.name.trim()}\n${input.idea.trim()}`;
      for (let i = 0; i < seed.length; i++) {
        hash ^= seed.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
      }
      const raw: unknown = await client.createNiche(
        {
          name: input.name,
          idea: input.idea,
        },
        { idempotencyKey: `niche-${(hash >>> 0).toString(16)}` },
      );
      const profile = normalizeNiche(raw);
      if (!profile) {
        throw new ApiError({
          code: "BAD_RESPONSE",
          message: "Backend returned an unexpected niche profile response",
          status: 201,
          retryable: false,
        });
      }
      return profile;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["niches"] });
    },
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  // One UUID per launch attempt + a single-flight flag. The UUID is
  // forwarded to the client as the POST's Idempotency-Key, so a retry
  // of the SAME attempt (e.g. after a network failure) replays the
  // original response instead of creating a duplicate project; the
  // flag additionally guards against a second POST being issued at all
  // while one is in flight.
  const attemptKeyRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);

  const mutation = useMutation({
    mutationFn: async (input: CreateProjectInput): Promise<WizardProject> => {
      if (inFlightRef.current) {
        throw new ApiError({
          code: "LAUNCH_IN_PROGRESS",
          message: "A launch is already in progress",
          status: 0,
          retryable: false,
        });
      }
      inFlightRef.current = true;
      attemptKeyRef.current =
        typeof globalThis.crypto?.randomUUID === "function"
          ? globalThis.crypto.randomUUID()
          : `launch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
      try {
        const client = await getClient();
        // Backend wire shape (projects.py ProjectCreate): snake_case.
        const body: Record<string, unknown> = {
          channel_id: input.channelId,
          niche_profile_id: input.nicheProfileId,
          topic_mode: input.topicMode,
          target_minutes: input.targetMinutes,
          publish_mode: input.publishMode,
        };
        if (input.topicMode === "manual" && input.topic) {
          body.topic = input.topic;
        }
        const raw: unknown = await client.createProject(
          body as unknown as CreateProjectPayload,
          { idempotencyKey: attemptKeyRef.current ?? undefined },
        );
        const project = normalizeProject(raw);
        if (!project) {
          throw new ApiError({
            code: "BAD_RESPONSE",
            message: "Backend returned an unexpected project response",
            status: 201,
            retryable: false,
          });
        }
        return project;
      } finally {
        inFlightRef.current = false;
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  return mutation;
}
