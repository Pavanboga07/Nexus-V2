import { useEffect, useRef, useState } from "react";
import { issueTicket } from "../api/endpoints";
import type { AppEvent } from "../api/types";

/** Subprotocol slot for tickets. Query-string tickets are not supported:
 *  server access logs record request paths, which would persist a
 *  bearer-equivalent credential. See docs/WEBSOCKET_EVENTS.md. */
export const TICKET_SUBPROTOCOL_PREFIX = "nexus-ticket.";

/** Fixed subprotocol the server selects on accept. Browsers fail the
 *  handshake when they offered protocols but the server selects none,
 *  so the ticket offer alone can never connect a real browser. This
 *  name carries no ticket material. Must match NEGOTIATED_SUBPROTOCOL
 *  in backend/api/routes/events.py. */
export const NEGOTIATED_SUBPROTOCOL = "nexus-events";

export type StreamState = "idle" | "connecting" | "connected" | "disconnected";

export interface StreamEvent {
  event: AppEvent;
  /** True when this event_id was already seen this session. */
  duplicate: boolean;
}

function isValidEnvelope(value: unknown): value is AppEvent {
  if (value === null || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.event_id === "string" &&
    typeof record.type === "string" &&
    typeof record.timestamp === "string" &&
    record.data !== null &&
    typeof record.data === "object"
  );
}

function wsUrlFor(baseUrl: string): string {
  const url = new URL("/api/v1/events/ws", baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

const MAX_BACKOFF_MS = 30_000;

export interface UseEventStreamOptions {
  baseUrl: string;
  token: string;
  enabled: boolean;
  onEvent?: (item: StreamEvent) => void;
  onSyncNeeded?: () => void;
}

/** Authenticated WebSocket lifecycle: ticket, subprotocol handshake,
 *  envelope validation, duplicate suppression, bounded reconnect with a
 *  fresh ticket, and REST resynchronization after reconnect. Events are
 *  notifications only; REST remains the source of truth. */
export function useEventStream(options: UseEventStreamOptions) {
  const { baseUrl, token, enabled, onEvent, onSyncNeeded } = options;
  const [state, setState] = useState<StreamState>("idle");
  const [lastError, setLastError] = useState<string | null>(null);
  const seenIds = useRef<Set<string>>(new Set());
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const stoppedRef = useRef(false);
  const callbacksRef = useRef({ onEvent, onSyncNeeded });
  callbacksRef.current = { onEvent, onSyncNeeded };

  useEffect(() => {
    if (!enabled || token.length === 0) {
      setState("idle");
      return;
    }
    stoppedRef.current = false;
    seenIds.current = new Set();
    let socket: WebSocket | null = null;

    const cleanupTimer = () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (stoppedRef.current) return;
      const attempt = retryRef.current;
      retryRef.current += 1;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      setState("disconnected");
      cleanupTimer();
      timerRef.current = window.setTimeout(() => {
        if (!stoppedRef.current) connect();
      }, delay);
    };

    const connect = async () => {
      if (stoppedRef.current) return;
      setState("connecting");
      setLastError(null);
      try {
        const issued = await issueTicket(baseUrl, token);
        if (stoppedRef.current) return;
        // The ticket travels in the subprotocol header, never the URL.
        // Both protocols are offered: the ticket for authentication and
        // the fixed name for the server to select (browsers require the
        // server to pick one of the offered protocols).
        socket = new WebSocket(wsUrlFor(baseUrl), [
          `${TICKET_SUBPROTOCOL_PREFIX}${issued.ticket}`,
          NEGOTIATED_SUBPROTOCOL,
        ]);
        socketRef.current = socket;
      } catch {
        if (!stoppedRef.current) {
          setLastError("Could not open the event stream. Will retry.");
          scheduleReconnect();
        }
        return;
      }

      let opened = false;
      socket.onopen = () => {
        opened = true;
        const resync = retryRef.current > 0;
        retryRef.current = 0;
        setState("connected");
        setLastError(null);
        if (resync) {
          // Events are not replayed: refresh from REST after any gap.
          callbacksRef.current.onSyncNeeded?.();
        }
      };
      socket.onmessage = (message) => {
        let parsed: unknown = null;
        try {
          parsed = JSON.parse(String(message.data)) as unknown;
        } catch {
          return;
        }
        if (!isValidEnvelope(parsed)) return;
        const duplicate = seenIds.current.has(parsed.event_id);
        if (!duplicate) {
          seenIds.current.add(parsed.event_id);
        }
        // Duplicates are reported but never trigger side effects twice;
        // consumers check `duplicate` before refetching.
        callbacksRef.current.onEvent?.({ event: parsed, duplicate });
      };
      socket.onerror = () => {
        // Browsers hide WS failure detail; treat as a reconnect signal.
        try {
          socket?.close();
        } catch {
          // Already closing; the close handler drives the retry.
        }
      };
      socket.onclose = () => {
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
        if (stoppedRef.current) return;
        if (!opened) {
          // Auth failures (4401) close immediately; ticket fetch already
          // succeeded, so retry with backoff rather than spinning.
          setLastError("The event stream closed. Reconnecting.");
        }
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      stoppedRef.current = true;
      cleanupTimer();
      try {
        socket?.close();
      } catch {
        // Closing an already-closed socket is fine.
      }
      socketRef.current = null;
    };
  }, [baseUrl, token, enabled]);

  return { state, lastError };
}

/** Helpers exported for tests. */
export const __wsInternals = { isValidEnvelope, wsUrlFor };
