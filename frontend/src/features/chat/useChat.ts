import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { describeError } from "../../lib/api/client";
import {
  createConversation,
  getAgents,
  listConversations,
  listMessages,
  sendTurn,
} from "../../lib/api/endpoints";
import type {
  Agent,
  Conversation,
  ConversationMessage,
  Turn,
} from "../../lib/api/types";

const AGENT_KEY = "nexus.chat.agentId";

export interface ChatState {
  agents: Agent[];
  agentId: string | null;
  selectAgent: (id: string) => void;
  conversations: Conversation[];
  conversationsState: "loading" | "ready" | "error";
  conversationsError: string | null;
  reloadConversations: () => void;
  createNewConversation: (title?: string) => Promise<Conversation | null>;
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
  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;

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

  const loadConversations = useCallback(async () => {
    const current = agentIdRef.current;
    if (!current) {
      setConversations([]);
      setConversationsState("ready");
      return;
    }
    setConversationsState("loading");
    setConversationsError(null);
    try {
      const loaded = await listConversations(baseUrl, token, { agentId: current });
      setConversations(loaded);
      setConversationsState("ready");
    } catch (error) {
      setConversationsState("error");
      setConversationsError(describeError(error));
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations, agentId]);

  useEffect(() => subscribe("conversations", () => void loadConversations()), [subscribe, loadConversations]);

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
        setConversations((prev) => [created, ...prev]);
        return created;
      } catch {
        return null;
      } finally {
        setCreating(false);
      }
    },
    [baseUrl, token],
  );

  return {
    agents,
    agentId,
    selectAgent,
    conversations,
    conversationsState,
    conversationsError,
    reloadConversations: () => void loadConversations(),
    createNewConversation,
    creating,
  };
}

export interface ThreadState {
  messages: ConversationMessage[];
  threadState: "loading" | "ready" | "error";
  threadError: string | null;
  reload: () => void;
  send: (message: string) => Promise<Turn | null>;
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

  const send = useCallback(
    async (message: string): Promise<Turn | null> => {
      if (!conversationId || sending) return null;
      setSending(true);
      setSendError(null);
      try {
        const turn = await sendTurn(baseUrl, token, conversationId, message);
        // Refresh from the server: history is server-owned.
        const loaded = await listMessages(baseUrl, token, conversationId);
        setMessages(loaded);
        return turn;
      } catch (error) {
        setSendError(describeError(error));
        return null;
      } finally {
        setSending(false);
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
    sending,
    sendError,
  };
}
