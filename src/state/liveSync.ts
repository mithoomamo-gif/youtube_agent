/**
 * WebSocket → React Query bridge (Round 2 live sync).
 *
 * Mounted ONCE from src/App.tsx via {@link useLiveSync}. It owns:
 *
 * - the WsClient lifecycle (src/api/ws.ts — REST ws-ticket exchange,
 *   `client.hello` as the first frame, jittered-backoff reconnect and
 *   30s dead-socket detection all live in that client; this hook does
 *   not reimplement any of it),
 * - the REST health monitor (src/state/connection.ts),
 * - translation of WS events into React Query invalidations, using the
 *   shared query-key convention (first element = resource name).
 *
 * The socket is a notification channel only — REST stays the source
 * of truth, so events only ever INVALIDATE queries; they never write
 * data into the cache.
 *
 * Invalidation map (by event-type namespace):
 *   agent.* / pipeline.*  → ["agents"], ["projects"], ["queue"]
 *   project.*             → ["projects"]
 *   gate.*                → ["approvals"], ["projects"]
 *   cost.* / quota.*      → ["usage"]
 *   publication.*         → ["projects"], ["publications"]
 *   anything else         → ignored (additive evolution rule)
 *
 * Full resync — refetch ALL active queries — happens on:
 *   system.snapshot_required (via the client's onSnapshotRequired), or
 *   a boot_id change (backend restarted under us; detected from the
 *   WS hello_ack / any event envelope, the monitor's system/info
 *   poll, or the client's own cursor — all funnel into the store).
 *
 * Socket open/close also feeds the connection store's WS signal, from
 * which the honest status (ready / degraded / down) is derived.
 */

import { useEffect } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useBackendBootstrapStore } from "../api/bootstrap";
import { getBackendConnection } from "../api/tauri";
import { emitAgentWsEvent } from "../api/queries/agents";
import type { WsEvent } from "../api/types";
import { WsClient, type WsConnectionState } from "../api/ws";
import { startConnectionMonitor, useConnectionStore } from "./connection";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Query keys to invalidate for an event type; [] = ignore the event. */
export function invalidationsForEventType(type: string): string[][] {
  const namespace = type.split(".", 1)[0];
  switch (namespace) {
    case "agent":
    case "pipeline":
      return [["agents"], ["projects"], ["queue"]];
    case "project":
      return [["projects"]];
    case "gate":
      return [["approvals"], ["projects"]];
    case "cost":
    case "quota":
      return [["usage"]];
    case "publication":
      return [["projects"], ["publications"]];
    default:
      return [];
  }
}

function refetchAllActive(queryClient: QueryClient): void {
  void queryClient.invalidateQueries();
  void queryClient.refetchQueries({ type: "active" });
}

function handleWsEvent(queryClient: QueryClient, event: WsEvent): void {
  // Record the backend boot_id from the envelope (every frame carries
  // one) or, for hello_ack, from its payload. A CHANGE is acted on by
  // the store subscription in useLiveSync (full refetch) so the REST
  // monitor and the socket share one code path.
  const payload = isRecord(event.payload) ? event.payload : undefined;
  const bootId =
    typeof event.boot_id === "string" && event.boot_id
      ? event.boot_id
      : payload && typeof payload.boot_id === "string" && payload.boot_id
        ? payload.boot_id
        : undefined;
  if (bootId && useConnectionStore.getState().bootId !== bootId) {
    useConnectionStore.getState().setBootId(bootId);
  }

  // snapshot_required is handled via onSnapshotRequired; hello_ack is
  // pure handshake bookkeeping (boot_id handled above).
  if (event.type === "system.snapshot_required" || event.type === "system.hello_ack") {
    return;
  }

  for (const queryKey of invalidationsForEventType(event.type)) {
    void queryClient.invalidateQueries({ queryKey });
  }
}

function toWsLinkState(state: WsConnectionState) {
  return state === "open" ? "open" : state === "connecting" ? "connecting" : "closed";
}

/**
 * Starts live sync for the app. Call once, near the root, INSIDE the
 * React Query provider. Cleans up the socket and monitor on unmount.
 */
export function useLiveSync(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const stopMonitor = startConnectionMonitor();

    // Boot change (backend restarted) → everything cached may be
    // stale: refetch all active queries. The first boot_id learned
    // (prev === null) is not a change and does not refetch.
    const unsubscribe = useConnectionStore.subscribe((state, prev) => {
      if (prev.bootId && state.bootId && prev.bootId !== state.bootId) {
        refetchAllActive(queryClient);
      }
    });

    const client = new WsClient(getBackendConnection, {
      onEvent: (event) => {
        handleWsEvent(queryClient, event);
        // Forward the payload to registered screen-level consumers
        // (Agents Live's handler registry in api/queries/agents.ts) so
        // per-agent live state updates instantly instead of waiting for
        // the event journal's next poll. This is the only WsClient in
        // the app — forwarding shares its frames, it does not open a
        // second socket.
        emitAgentWsEvent(event);
      },
      onStateChange: (state) => {
        useConnectionStore.getState().setWsState(toWsLinkState(state));
      },
      onSnapshotRequired: () => {
        refetchAllActive(queryClient);
      },
    });

    // Backend restart (shell pushed a new connection — new port /
    // token / boot_id): the old socket belongs to a dead process, so
    // drop it and reconnect against the new connection. Query resync
    // by snapshot is owned by the bootstrap (it invalidates/refetches
    // all queries on the same connection change).
    const unsubscribeConn = useBackendBootstrapStore.subscribe(
      (state, prev) => {
        if (prev.connectionVersion !== state.connectionVersion) {
          client.disconnect();
          void client.connect();
        }
      },
    );

    // Subscribing BEFORE connect puts "all" into the client.hello
    // frame itself, so no event class is missed between hello and a
    // later subscribe. ("all" receives every topic; system frames
    // reach every connection regardless.)
    client.subscribe(["all"]);
    void client.connect();

    return () => {
      unsubscribe();
      unsubscribeConn();
      client.disconnect();
      stopMonitor();
    };
  }, [queryClient]);
}
