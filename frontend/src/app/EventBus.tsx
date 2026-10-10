import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "../lib/auth/AuthProvider";
import { useEventStream, type StreamEvent } from "../lib/websocket/useEventStream";
import { useToast } from "../components/ui/Toast";

export type SyncScope = "conversations" | "requests" | "approvals" | "agents";

interface EventBusValue {
  streamState: "idle" | "connecting" | "connected" | "disconnected";
  streamError: string | null;
  subscribe: (scope: SyncScope, handler: () => void) => () => void;
}

const EventBusContext = createContext<EventBusValue | null>(null);

export function useEventBus(): EventBusValue {
  const value = useContext(EventBusContext);
  if (value === null) throw new Error("useEventBus must be used inside EventProvider");
  return value;
}

const scopeForType: Record<string, SyncScope[]> = {
  "conversation.turn.completed": ["conversations"],
  "remote.request.created": ["requests"],
  "remote.request.approval_required": ["requests", "approvals"],
  "remote.response.received": ["requests"],
  "remote.request.updated": ["requests", "approvals"],
  "execution.updated": ["requests", "approvals"],
  "remote.pairing.updated": ["requests", "approvals", "agents"],
};

/** Owns the authenticated event stream for the whole app. Events fan out
 *  to subscribed scopes as refetch signals (never as canonical data),
 *  duplicates are ignored, and every reconnect triggers a REST resync. */
export function EventProvider({ children }: { children: ReactNode }) {
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const handlers = useRef(new Map<SyncScope, Set<() => void>>());
  const [notifiedApprovalIds] = useState(() => new Set<string>());
  const enabled = token.length > 0;

  const fire = useCallback((scope: SyncScope) => {
    handlers.current.get(scope)?.forEach((handler) => {
      try {
        handler();
      } catch {
        // A failing subscriber must not break fan-out to the rest.
      }
    });
  }, []);

  const handleEvent = useCallback(
    (item: StreamEvent) => {
      if (item.duplicate) return;
      const scopes = scopeForType[item.event.type] ?? [];
      scopes.forEach(fire);
      if (item.event.type === "remote.request.approval_required") {
        const requestId = item.event.data.request_id;
        if (typeof requestId === "string" && !notifiedApprovalIds.has(requestId)) {
          notifiedApprovalIds.add(requestId);
          notify(`Approval needed for request ${requestId}.`, "info");
        }
      }
    },
    [fire, notify, notifiedApprovalIds],
  );

  const handleSync = useCallback(() => {
    (["conversations", "requests", "approvals", "agents"] as SyncScope[]).forEach(fire);
  }, [fire]);

  const { state, lastError } = useEventStream({
    baseUrl,
    token,
    enabled,
    onEvent: handleEvent,
    onSyncNeeded: handleSync,
  });

  const subscribe = useCallback((scope: SyncScope, handler: () => void) => {
    let set = handlers.current.get(scope);
    if (!set) {
      set = new Set();
      handlers.current.set(scope, set);
    }
    set.add(handler);
    return () => {
      set.delete(handler);
    };
  }, []);

  const value = useMemo<EventBusValue>(
    () => ({ streamState: state, streamError: lastError, subscribe }),
    [state, lastError, subscribe],
  );
  return <EventBusContext.Provider value={value}>{children}</EventBusContext.Provider>;
}
