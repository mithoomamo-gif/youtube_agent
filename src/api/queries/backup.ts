/**
 * React Query hooks for backup & restore (Round 6).
 *
 * Wire contract (backend/app/routers/backup.py):
 *
 * - `GET  /settings/backup`         -> BackupInfoWire (includes/excludes
 *   summary + the bundle format / DB schema versions this build uses).
 * - `GET  /settings/backup/export`  -> a zip file download
 *   (`youtube-agent-backup-<timestamp>.zip`). The bundle contains the
 *   database, channel configs, schedules and style profiles — never
 *   API keys or OAuth tokens (those stay in the credential store) and
 *   never competitor video files (metadata only).
 * - `POST /settings/backup/import`  -> multipart form field `file`;
 *   returns BackupRestoreWire. The backend validates everything
 *   before replacing any data, keeps a safety copy of the previous
 *   database, and refuses a backup made by a newer app version with
 *   409 `backup_from_newer_app` — its message is shown verbatim.
 *
 * Export is a binary download, so it cannot go through the JSON
 * helper; import is multipart, so it sets no Content-Type (the browser
 * generates the boundary). Both carry the launch token exactly like
 * the JSON helper in providers.ts and surface errors as ApiError from
 * the standard envelope.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export interface BackupInfoWire {
  bundle_format_version: number;
  supported_db_schema_version: number;
  database_present: boolean;
  includes: string[];
  excludes: string[];
}

export interface BackupRestoreWire {
  restored: boolean;
  bundle_format_version: number;
  db_schema_version: number;
  supported_db_schema_version: number;
  /** File name (under the data dir's backups folder) of the safety
   *  copy of the database that was in place before the restore. */
  safety_copy: string | null;
  counts: {
    channels: number;
    schedules: number;
    style_profiles: number;
  };
}

// ── Helpers (mirror providers.ts envelope handling) ────────────────────────

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

async function toApiError(res: Response): Promise<ApiError> {
  let parsed: unknown;
  try {
    const text = await res.text();
    parsed = text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    parsed = undefined;
  }
  if (isRecord(parsed) && isRecord(parsed.error)) {
    const err = parsed.error;
    return new ApiError({
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
  return new ApiError({
    code: `HTTP_${res.status}`,
    message: `Request failed (${res.status})`,
    status: res.status,
    retryable: res.status === 429 || res.status >= 500,
  });
}

async function fetchBackupInfo(): Promise<BackupInfoWire> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");
  const res = await fetch(`${baseUrl}/settings/backup`, {
    headers: { "X-Local-Token": conn.token },
  });
  if (!res.ok) throw await toApiError(res);
  const raw = (await res.json()) as Record<string, unknown>;
  return {
    bundle_format_version:
      typeof raw.bundle_format_version === "number"
        ? raw.bundle_format_version
        : 0,
    supported_db_schema_version:
      typeof raw.supported_db_schema_version === "number"
        ? raw.supported_db_schema_version
        : 0,
    database_present: raw.database_present === true,
    includes: Array.isArray(raw.includes)
      ? raw.includes.filter((v): v is string => typeof v === "string")
      : [],
    excludes: Array.isArray(raw.excludes)
      ? raw.excludes.filter((v): v is string => typeof v === "string")
      : [],
  };
}

export function useBackupInfo() {
  return useQuery({
    queryKey: ["settings", "backup"],
    queryFn: fetchBackupInfo,
    retry: 1,
  });
}

/** Downloads one backup bundle and saves it through the browser. */
export function useExportBackup() {
  return useMutation({
    mutationFn: async (): Promise<string> => {
      const conn = await getBackendConnection();
      const baseUrl = conn.baseUrl.replace(/\/+$/, "");
      const res = await fetch(`${baseUrl}/settings/backup/export`, {
        headers: { "X-Local-Token": conn.token },
      });
      if (!res.ok) throw await toApiError(res);
      const disposition = res.headers.get("Content-Disposition") ?? "";
      const match = /filename="?([^";]+)"?/.exec(disposition);
      const filename = match?.[1] ?? "youtube-agent-backup.zip";
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        URL.revokeObjectURL(url);
      }
      return filename;
    },
  });
}

/** Uploads a chosen bundle file and restores it (after UI confirm). */
export function useImportBackup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File): Promise<BackupRestoreWire> => {
      const conn = await getBackendConnection();
      const baseUrl = conn.baseUrl.replace(/\/+$/, "");
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${baseUrl}/settings/backup/import`, {
        method: "POST",
        headers: {
          "X-Local-Token": conn.token,
          "Idempotency-Key": randomId(),
        },
        body: form,
      });
      if (!res.ok) throw await toApiError(res);
      const raw = (await res.json()) as Record<string, unknown>;
      const counts = isRecord(raw.counts) ? raw.counts : {};
      const num = (v: unknown): number => (typeof v === "number" ? v : 0);
      return {
        restored: raw.restored === true,
        bundle_format_version: num(raw.bundle_format_version),
        db_schema_version: num(raw.db_schema_version),
        supported_db_schema_version: num(raw.supported_db_schema_version),
        safety_copy:
          typeof raw.safety_copy === "string" ? raw.safety_copy : null,
        counts: {
          channels: num(counts.channels),
          schedules: num(counts.schedules),
          style_profiles: num(counts.style_profiles),
        },
      };
    },
    onSuccess: () => {
      // A restore replaces essentially all server state.
      void queryClient.invalidateQueries();
    },
  });
}
