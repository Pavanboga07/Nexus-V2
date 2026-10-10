import { useEffect, useRef } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, MessageSquarePlus } from "lucide-react";
import { useChatAgents, useThread } from "./useChat";
import { useNetwork } from "../agents/useNetwork";
import { Composer } from "./Composer";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorState, Skeleton, Spinner } from "../../components/ui/Feedback";
import { SelectControl } from "../../components/ui/Field";
import { cn } from "../../components/ui/cn";
import type { ConversationMessage } from "../../lib/api/types";

function MessageBubble({ message }: { message: ConversationMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[85%] rounded-card px-4 py-3 sm:max-w-[75%]",
          isUser
            ? "bg-accent-600 text-white"
            : "border border-graphite-700 bg-graphite-900 text-ink-100",
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap text-[15px] leading-relaxed">{message.content}</p>
        ) : (
          <div className="prose-nexus text-[15px] leading-relaxed">
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

export function ChatPage() {
  const chat = useChatAgents();
  const { id } = useParams();
  const navigate = useNavigate();
  const activeId = id ?? null;

  const createAndOpen = async () => {
    const created = await chat.createNewConversation();
    if (created) {
      navigate(`/chats/${encodeURIComponent(created.id)}`);
    }
  };

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] md:h-dvh">
      <aside
        aria-label="Conversations"
        className={cn(
          "w-full flex-col border-r border-graphite-800 bg-graphite-950 sm:w-72 sm:shrink-0",
          activeId ? "hidden sm:flex" : "flex",
        )}
      >
        <div className="flex items-center justify-between gap-2 px-4 pb-2 pt-5">
          <h1 className="text-base font-semibold text-ink-50">Chats</h1>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void createAndOpen()}
            disabled={chat.creating || !chat.agentId}
            aria-label="New conversation"
          >
            <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
            New
          </Button>
        </div>
        <div className="px-4 pb-2">
          <label htmlFor="chat-agent" className="sr-only">
            Chat as agent
          </label>
          <SelectControl
            id="chat-agent"
            value={chat.agentId ?? ""}
            onChange={(event) => chat.selectAgent(event.target.value)}
            aria-label="Chat as agent"
          >
            {chat.agents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>
                {agent.agent_id}
              </option>
            ))}
          </SelectControl>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-4">
          {chat.conversationsState === "loading" ? (
            <div className="flex flex-col gap-2 px-2 pt-2">
              <Skeleton className="h-12" />
              <Skeleton className="h-12" />
            </div>
          ) : null}
          {chat.conversationsState === "error" ? (
            <ErrorState
              title="Conversations unavailable"
              body={chat.conversationsError ?? "Try again."}
              onRetry={chat.reloadConversations}
            />
          ) : null}
          {chat.conversationsState === "ready" && chat.conversations.length === 0 ? (
            <EmptyState
              icon="chat"
              title="No conversations yet"
              body="Create your first conversation to start chatting with your assistant."
              action={
                <Button variant="primary" size="sm" onClick={() => void createAndOpen()}>
                  New conversation
                </Button>
              }
            />
          ) : null}
          <ul className="flex flex-col gap-1">
            {chat.conversations.map((conversation) => {
              const active = conversation.id === activeId;
              return (
                <li key={conversation.id}>
                  <Link
                    to={`/chats/${encodeURIComponent(conversation.id)}`}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "block rounded-control px-3 py-2.5 text-sm transition-colors",
                      active
                        ? "bg-graphite-800 text-ink-50"
                        : "text-ink-300 hover:bg-graphite-900 hover:text-ink-50",
                    )}
                  >
                    <span className="block truncate font-medium">
                      {conversation.title ?? "Untitled conversation"}
                    </span>
                    <span className="block truncate text-xs text-ink-600">
                      {new Date(conversation.created_at).toLocaleString()}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      </aside>

      <section
        aria-label={activeId ? "Conversation" : "No conversation selected"}
        className={cn(
          "min-w-0 flex-1 flex-col",
          activeId ? "flex" : "hidden sm:flex",
        )}
      >
        {!activeId ? (
          <div className="grid flex-1 place-items-center px-4">
            <EmptyState
              icon="chat"
              title="Pick a conversation"
              body="Select a chat on the left, or start a new one."
              action={
                <Button variant="primary" size="sm" onClick={() => void createAndOpen()}>
                  New conversation
                </Button>
              }
            />
          </div>
        ) : (
          <>
            <ThreadViewInner conversationId={activeId} />
          </>
        )}
      </section>
    </div>
  );
}

function ThreadViewInner({ conversationId }: { conversationId: string }) {
  const thread = useThread(conversationId);
  const network = useNetwork();
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  useEffect(() => {
    if (stickRef.current) {
      // scrollIntoView is absent in some test DOMs; guard it.
      bottomRef.current?.scrollIntoView?.({ behavior: "auto", block: "end" });
    }
  }, [thread.messages.length, thread.sending]);

  return (
    <>
      <div className="border-b border-graphite-800 px-4 py-2 sm:hidden">
        <Link
          to="/chats"
          className="inline-flex min-h-[44px] items-center gap-1.5 text-sm text-ink-300 hover:text-ink-50"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          All chats
        </Link>
      </div>
      <div
        className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-4 overflow-y-auto px-4 py-6"
        onScroll={(event) => {
          const target = event.currentTarget;
          stickRef.current =
            target.scrollHeight - target.scrollTop - target.clientHeight < 120;
        }}
      >
        {thread.threadState === "loading" ? (
          <div className="flex flex-col gap-3" role="status">
            <span className="sr-only">Loading conversation</span>
            <Skeleton className="h-20" />
            <Skeleton className="h-24 w-4/5 self-end" />
            <Skeleton className="h-20" />
          </div>
        ) : null}
        {thread.threadState === "error" ? (
          <ErrorState
            title="Could not load this conversation"
            body={thread.threadError ?? "Try again."}
            onRetry={thread.reload}
          />
        ) : null}
        {thread.threadState === "ready" && thread.messages.length === 0 ? (
          <EmptyState
            icon="chat"
            title="Start the conversation"
            body="Send the first message below. The assistant replies through your local model."
          />
        ) : null}
        {thread.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {thread.sending ? <Spinner label="Assistant is thinking" /> : null}
        <div ref={bottomRef} aria-hidden="true" />
      </div>
      <Composer
        sending={thread.sending}
        sendError={thread.sendError}
        onSend={(message) => void thread.send(message)}
        connectedAgentCount={network.connectedCount}
      />
    </>
  );
}
