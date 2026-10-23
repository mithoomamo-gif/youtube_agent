/**
 * WebSocket client for the local backend (protocol section 31).
 *
 * - Auth: the launch token NEVER goes in the URL. Each (re)connect first
 *   mints a single-use 60s ticket over REST (`POST /api/v1/ws-ticket`),
 *   then sends it in the `client.hello` frame — the first frame, always.
 * - The socket is a notification channel; REST is the source of truth.
 *   Unknown event types are passed through untouched (additive evolution).
 * - Liveness: server heartbeats arrive every ~15s; if NO frame of any
 *   type arrives for 30s the socket is treated as dead and reconnected
 *   with jittered backoff (0.5 → 1 → 2 → 4 → 8s, capped at 15s, ±20%).
 * - Resync: `(last_boot_id, last_seq)` is replayed in every hello. When
 *   the server answers `system.snapshot_required`, the caller's
 *   `onSnapshotRequired` fires and it must refetch state via REST.
 */

import { fetchWsTicket } from "./client";
import type { BackendConnection, WsEvent } from "./types";

export type WsConnectionState = "connecting" | "open" | "closed";

export interface WsClientHandlers {
  onEvent: (event: WsEvent) => void;
  onStateChange?: (state: WsConnectionState) => void;
  /** The caller must refetch state via REST (socket = notification only). */
  onSnapshotRequired?: (reason: string) => void;
}

export interface WsCommandInput {
  name: "pause_job" | "cancel_job" | "pause_all";
  jobId?: string;
  jobIds?: string[];
  args?: Record<string, unknown>;
  idempotencyKey?: string;
}

const PING_INTERVAL_MS = 20_000;
const DEAD_AFTER_MS = 30_000;
const BASE_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15_000;

function randomId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export class WsClient {
  private readonly getConn: () => Promise<BackendConnection>;
  private readonly handlers: WsClientHandlers;

  private socket: WebSocket | null = null;
  private state: WsConnectionState = "closed";
  private stopped = true;
  private opening = false;

  private readonly subscriptions = new Set<string>();
  private pendingFrames: string[] = [];

  private lastBootId: string | null = null;
  private lastSeq: number | null = null;
  private lastFrameAt = 0;

  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    getConn: () => Promise<BackendConnection>,
    handlers: WsClientHandlers,
  ) {
    this.getConn = getConn;
    this.handlers = handlers;
  }

  get connectionState(): WsConnectionState {
    return this.state;
  }

  getLastBootId(): string | null {
    return this.lastBootId;
  }

  getLastSeq(): number | null {
    return this.lastSeq;
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────

  /** Starts connecting. Never rejects; failures schedule a reconnect. */
  async connect(): Promise<void> {
    if (this.opening) return;
    if (this.socket && (this.state === "connecting" || this.state === "open")) {
      return;
    }
    this.stopped = false;
    this.clearReconnectTimer();
    await this.openSocket();
  }

  disconnect(): void {
    this.stopped = true;
    this.opening = false;
    this.clearReconnectTimer();
    this.stopLivenessTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      try {
        socket.close();
      } catch {
        // Already closing/closed — nothing to do.
      }
    }
    this.pendingFrames = [];
    this.setState("closed");
  }

  // ── Client → server frames ──────────────────────────────────────────────

  subscribe(topics: string[]): void {
    for (const topic of topics) this.subscriptions.add(topic);
    this.sendFrame({
      v: 1,
      type: "client.subscribe",
      event_id: randomId(),
      ts: new Date().toISOString(),
      topics,
      payload: { topics },
    });
  }

  unsubscribe(topics: string[]): void {
    for (const topic of topics) this.subscriptions.delete(topic);
    this.sendFrame({
      v: 1,
      type: "client.unsubscribe",
      event_id: randomId(),
      ts: new Date().toISOString(),
      topics,
      payload: { topics },
    });
  }

  command(input: WsCommandInput): void {
    const payload: Record<string, unknown> = {
      command_id: randomId(),
      name: input.name,
      idempotency_key: input.idempotencyKey ?? randomId(),
    };
    if (input.jobId) payload.job_id = input.jobId;
    if (input.jobIds) payload.job_ids = input.jobIds;
    if (input.args) payload.args = input.args;
    this.sendFrame({
      v: 1,
      type: "client.command",
      event_id: randomId(),
      ts: new Date().toISOString(),
      payload,
    });
  }

  // ── Connection internals ────────────────────────────────────────────────

  private async openSocket(): Promise<void> {
    if (this.stopped || this.opening) return;
    this.opening = true;
    this.setState("connecting");

    let conn: BackendConnection;
    let ticket: string;
    try {
      conn = await this.getConn();
      ticket = await fetchWsTicket(conn);
    } catch {
      this.opening = false;
      this.setState("closed");
      this.scheduleReconnect();
      return;
    }
    if (this.stopped) {
      this.opening = false;
      return;
    }

    let socket: WebSocket;
    try {
      // NOTE: bare wsUrl — the token must never be placed in the URL.
      socket = new WebSocket(conn.wsUrl);
    } catch {
      this.opening = false;
      this.setState("closed");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    this.opening = false;

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.lastFrameAt = Date.now();
      // client.hello MUST be the first frame on the socket.
      socket.send(JSON.stringify(this.buildHelloFrame(ticket)));
      this.flushPending(socket);
      this.startLivenessTimers(socket);
      this.setState("open");
    };
    socket.onmessage = (message: MessageEvent) => {
      this.handleMessage(message.data);
    };
    socket.onclose = () => {
      this.handleClose(socket);
    };
    socket.onerror = () => {
      try {
        socket.close();
      } catch {
        // handleClose performs the reconnect bookkeeping.
      }
    };
  }

  private buildHelloFrame(ticket: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      ticket,
      ws_schema_versions: [1],
      subscriptions: [...this.subscriptions],
    };
    if (this.lastBootId) payload.last_boot_id = this.lastBootId;
    if (this.lastSeq !== null) payload.last_seq = this.lastSeq;
    return {
      v: 1,
      type: "client.hello",
      event_id: randomId(),
      ts: new Date().toISOString(),
      // Mirrored at the top level as well as inside `payload` so either
      // reading convention finds the ticket and schema offer.
      ticket,
      ws_schema_versions: [1],
      payload,
    };
  }

  private handleMessage(data: unknown): void {
    this.lastFrameAt = Date.now();
    this.resetDeadTimer();

    if (typeof data !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(data) as unknown;
    } catch {
      return; // Unparseable frame — ignore, never crash the client.
    }
    if (!isRecord(parsed) || typeof parsed.type !== "string") return;
    const event = parsed as unknown as WsEvent;
    const payload = isRecord(event.payload) ? event.payload : undefined;

    // Track the (boot_id, seq) resync cursor.
    const bootId =
      typeof event.boot_id === "string"
        ? event.boot_id
        : typeof payload?.boot_id === "string"
          ? payload.boot_id
          : undefined;
    const seq =
      typeof event.seq === "number"
        ? event.seq
        : typeof payload?.last_seq === "number"
          ? payload.last_seq
          : typeof payload?.seq === "number"
            ? payload.seq
            : undefined;
    if (bootId) this.lastBootId = bootId;
    if (seq !== undefined) this.lastSeq = seq;

    if (event.type === "system.hello_ack") {
      this.reconnectAttempts = 0;
      this.setState("open");
    }

    if (event.type === "system.snapshot_required") {
      const reason =
        typeof payload?.reason === "string" ? payload.reason : "snapshot_required";
      this.handlers.onSnapshotRequired?.(reason);
    }

    // Unknown types are passed through untouched — callers ignore them.
    this.handlers.onEvent(event);
  }

  private handleClose(socket: WebSocket): void {
    if (this.socket !== socket) return;
    this.socket = null;
    this.stopLivenessTimers();
    this.setState("closed");
    if (!this.stopped) this.scheduleReconnect();
  }

  private forceReconnect(socket: WebSocket): void {
    if (this.socket !== socket) return;
    this.socket = null;
    this.stopLivenessTimers();
    try {
      socket.close();
    } catch {
      // Dead already — reconnect bookkeeping below is what matters.
    }
    this.setState("closed");
    if (!this.stopped) this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    const base = Math.min(BASE_BACKOFF_MS * 2 ** this.reconnectAttempts, MAX_BACKOFF_MS);
    this.reconnectAttempts += 1;
    const delay = base * (0.8 + Math.random() * 0.4); // ±20% jitter
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.openSocket();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // ── Liveness ────────────────────────────────────────────────────────────

  private startLivenessTimers(socket: WebSocket): void {
    this.stopLivenessTimers();
    this.resetDeadTimer();
    this.pingTimer = setInterval(() => {
      if (this.socket === socket && socket.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({
            v: 1,
            type: "client.ping",
            event_id: randomId(),
            ts: new Date().toISOString(),
            payload: {},
          }),
        );
      }
    }, PING_INTERVAL_MS);
  }

  private resetDeadTimer(): void {
    if (this.deadTimer !== null) clearTimeout(this.deadTimer);
    this.deadTimer = null;
    const socket = this.socket;
    if (!socket || this.stopped) return;
    this.deadTimer = setTimeout(() => {
      this.deadTimer = null;
      // No frame of any type for 30s → the connection is dead.
      if (Date.now() - this.lastFrameAt >= DEAD_AFTER_MS) {
        this.forceReconnect(socket);
      }
    }, DEAD_AFTER_MS);
  }

  private stopLivenessTimers(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.deadTimer !== null) {
      clearTimeout(this.deadTimer);
      this.deadTimer = null;
    }
  }

  // ── Sending ─────────────────────────────────────────────────────────────

  private sendFrame(frame: Record<string, unknown>): void {
    const text = JSON.stringify(frame);
    const socket = this.socket;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(text);
    } else {
      // Flushed after the hello frame once the socket opens.
      this.pendingFrames.push(text);
    }
  }

  private flushPending(socket: WebSocket): void {
    const pending = this.pendingFrames;
    this.pendingFrames = [];
    for (const text of pending) socket.send(text);
  }

  private setState(state: WsConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.handlers.onStateChange?.(state);
  }
}
