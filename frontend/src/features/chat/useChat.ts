import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { describeError } from "../../lib/api/client";
import {
  createConversation,
  deleteConversation,
  getAgents,
  listConversations,
  listMessages,
  sendTurnStream,
  updateConversation,
} from "../../lib/api/endpoints";
import type {
  Agent,
  Conversation,
  ConversationMessage,
  Turn,
} from "../../lib/api/types";

const AGENT_KEY = "nexus.chat.agentId";

function recency(row: Conversation): number {
  return Date.parse(row.updated_at ?? row.created_at) || 0;
}

/** Pinned first, then most recently active (mirrors the server order). */
function orderConversations(rows: Conversation[]): Conversation[] {
  return [...rows].sort(
    (a, b) => Number(b.pinned) - Number(a.pinned) || recency(b) - recency(a),
  );
}

export interface ChatState {
  agents: Agent[];
  agentId: string | null;
  selectAgent: (id: string) => void;
  conversations: Conversation[];
  conversationsState: "loading" | "ready" | "error";
  conversationsError: string | null;
  query: string;
  setQuery: (query: string) => void;
  reloadConversations: () => void;
  createNewConversation: (title?: string) => Promise<Conversation | null>;
  renameConversation: (id: string, title: string) => Promise<void>;
  setPinned: (id: string, pinned: boolean) => Promise<void>;
  removeConversation: (id: string) => Promise<void>;
  creating: boolean;
}

export function useChatAgents(): ChatState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(AGENT_KEY);
    } catch {
      return null;
    }
  });
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsState, setConversationsState] = useState<"loading" | "ready" | "error">("loading");
  const [conversationsError, setConversationsError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;
  const queryRef = useRef(query);
  queryRef.current = query;
  const requestSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await getAgents(baseUrl, token);
        if (cancelled) return;
        setAgents(loaded);
        if (loaded.length > 0) {
          const current = agentIdRef.current;
          const stillThere = loaded.some((agent) => agent.agent_id === current);
          if (!stillThere) {
            const first = loaded[0].agent_id;
            setAgentId(first);
            try {
              localStorage.setItem(AGENT_KEY, first);
            } catch {
              // Agent preference is non-critical.
            }
          }
        }
      } catch {
        if (!cancelled) {
          setConversationsState("error");
          setConversationsError("Could not load your agents.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token]);

  const loadConversations = useCallback(async (search: string) => {
    const current = agentIdRef.current;
    if (!current) {
      setConversations([]);
      setConversationsState("ready");
      return;
    }
    const seq = ++requestSeq.current;
    setConversationsState("loading");
    setConversationsError(null);
    try {
      const loaded = await listConversations(baseUrl, token, {
        agentId: current,
        query: search,
      });
      if (seq !== requestSeq.current) return;
      setConversations(orderConversations(loaded));
      setConversationsState("ready");
    } catch (error) {
      if (seq !== requestSeq.current) return;
      setConversationsState("error");
      setConversationsError(describeError(error));
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void loadConversations("");
  }, [loadConversations, agentId]);

  // Search is server-side (title or message text) and debounced so each
  // keystroke does not become a request.
  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length === 0) return;
    const timer = window.setTimeout(() => {
      void loadConversations(trimmed);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query, loadConversations]);

  useEffect(
    () => subscribe("conversations", () => void loadConversations(queryRef.current)),
    [subscribe, loadConversations],
  );

  const selectAgent = useCallback((id: string) => {
    setAgentId(id);
    try {
      localStorage.setItem(AGENT_KEY, id);
    } catch {
      // Ignore persistence failures.
    }
  }, []);

  const createNewConversation = useCallback(
    async (title?: string): Promise<Conversation | null> => {
      const current = agentIdRef.current;
      if (!current) return null;
      setCreating(true);
      try {
        const created = await createConversation(baseUrl, token, {
          agent_id: current,
          title: title ?? null,
        });
        setConversations((prev) => orderConversations([created, ...prev]));
        return created;
      } catch {
        return null;
      } finally {
        setCreating(false);
      }
    },
    [baseUrl, token],
  );

  const replaceConversation = useCallback((updated: Conversation) => {
    setConversations((prev) =>
      orderConversations(prev.map((row) => (row.id === updated.id ? updated : row))),
    );
  }, []);

  const renameConversation = useCallback(
    async (id: string, title: string) => {
      const updated = await updateConversation(baseUrl, token, id, { title });
      replaceConversation(updated);
    },
    [baseUrl, token, replaceConversation],
  );

  const setPinned = useCallback(
    async (id: string, pinned: boolean) => {
      const updated = await updateConversation(baseUrl, token, id, { pinned });
      replaceConversation(updated);
    },
    [baseUrl, token, replaceConversation],
  );

  const removeConversation = useCallback(
    async (id: string) => {
      await deleteConversation(baseUrl, token, id);
      setConversations((prev) => prev.filter((row) => row.id !== id));
    },
    [baseUrl, token],
  );

  const changeQuery = useCallback((next: string) => {
    setQuery(next);
    if (next.trim().length === 0) void loadConversations("");
  }, [loadConversations]);

  return {
    agents,
    agentId,
    selectAgent,
    conversations,
    conversationsState,
    conversationsError,
    query,
    setQuery: changeQuery,
    reloadConversations: () => void loadConversations(queryRef.current),
    createNewConversation,
    renameConversation,
    setPinned,
    removeConversation,
    creating,
  };
}

export interface ThreadState {
  messages: ConversationMessage[];
  threadState: "loading" | "ready" | "error";
  threadError: string | null;
  reload: () => void;
  send: (message: string) => Promise<Turn | null>;
  stop: () => void;
  sending: boolean;
  sendError: string | null;
}

/** Single conversation thread: history plus turn submission. The answer
 *  always comes from the backend; nothing is simulated in the browser. */
export function useThread(conversationId: string | null): ThreadState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [threadState, setThreadState] = useState<"loading" | "ready" | "error">("loading");
  const [threadError, setThreadError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!conversationId) {
      setMessages([]);
      setThreadState("ready");
      return;
    }
    setThreadState("loading");
    setThreadError(null);
    try {
      const loaded = await listMessages(baseUrl, token, conversationId);
      setMessages(loaded);
      setThreadState("ready");
    } catch (error) {
      setThreadState("error");
      setThreadError(describeError(error));
    }
  }, [baseUrl, token, conversationId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(
    () =>
      subscribe("conversations", () => {
        if (conversationId) void load();
      }),
    [subscribe, load, conversationId],
  );

  const abortRef = useRef<AbortController | null>(null);
  const tempId = useRef(-1);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const send = useCallback(
    async (message: string): Promise<Turn | null> => {
      if (!conversationId || sending) return null;
      const controller = new AbortController();
      abortRef.current = controller;
      const userTemp = tempId.current--;
      const assistantTemp = tempId.current--;
      const now = new Date().toISOString();
      setMessages((prev) => [
        ...prev,
        { id: userTemp, role: "user", content: message, created_at: now },
        { id: assistantTemp, role: "agent", content: "", created_at: now },
      ]);
      setSending(true);
      setSendError(null);
      try {
        const turn = await sendTurnStream(
          baseUrl,
          token,
          conversationId,
          message,
          {
            onDelta: (text) => {
              setMessages((prev) =>
                prev.map((row) =>
                  row.id === assistantTemp
                    ? { ...row, content: row.content + text }
                    : row,
                ),
              );
            },
          },
          controller.signal,
        );
        // History is server-owned: the completed turn replaces optimism.
        const loaded = await listMessages(baseUrl, token, conversationId);
        setMessages(loaded);
        return turn;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          // Stop: keep the partial bubble on screen; the next load is
          // authoritative. The server stores the partial answer.
          return null;
        }
        setSendError(describeError(error));
        try {
          const loaded = await listMessages(baseUrl, token, conversationId);
          setMessages(loaded);
        } catch {
          // Keep the optimistic transcript if the reload also fails.
        }
        return null;
      } finally {
        setSending(false);
        abortRef.current = null;
      }
    },
    [baseUrl, token, conversationId, sending],
  );

  return {
    messages,
    threadState,
    threadError,
    reload: () => void load(),
    send,
    stop,
    sending,
    sendError,
  };
}
