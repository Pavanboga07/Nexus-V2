import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import {
  ArrowLeft,
  Check,
  MessageSquarePlus,
  Pencil,
  Pin,
  PinOff,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useChatAgents, useThread } from "./useChat";
import { useNetwork } from "../agents/useNetwork";
import { Composer } from "./Composer";
import { Button } from "../../components/ui/Button";
import {
  EmptyState,
  ErrorState,
  Skeleton,
  Spinner,
} from "../../components/ui/Feedback";
import { Dialog, DialogActions } from "../../components/ui/Dialog";
import { SelectControl, TextInput } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { cn } from "../../components/ui/cn";
import { describeError } from "../../lib/api/client";
import type { Conversation, ConversationMessage } from "../../lib/api/types";

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const minutes = Math.floor((Date.now() - then) / 60_000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}

function conversationLabel(conversation: Conversation): string {
  return conversation.title ?? "Untitled conversation";
}

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

interface ConversationRowProps {
  conversation: Conversation;
  active: boolean;
  renaming: boolean;
  busy: boolean;
  onStartRename: () => void;
  onCancelRename: () => void;
  onSubmitRename: (title: string) => void;
  onTogglePin: () => void;
  onDelete: () => void;
}

function ConversationRow({
  conversation,
  active,
  renaming,
  busy,
  onStartRename,
  onCancelRename,
  onSubmitRename,
  onTogglePin,
  onDelete,
}: ConversationRowProps) {
  const [draft, setDraft] = useState(conversation.title ?? "");
  const label = conversationLabel(conversation);

  useEffect(() => {
    if (renaming) setDraft(conversation.title ?? "");
  }, [renaming, conversation.title]);

  if (renaming) {
    const submit = (event: FormEvent) => {
      event.preventDefault();
      const next = draft.trim();
      if (next.length > 0) onSubmitRename(next);
    };
    return (
      <form onSubmit={submit} className="flex items-center gap-1 px-1 py-1">
        <TextInput
          value={draft}
          autoFocus
          aria-label="Conversation title"
          maxLength={256}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") onCancelRename();
          }}
          className="h-9 min-w-0 flex-1"
        />
        <button
          type="submit"
          aria-label="Save conversation title"
          disabled={busy || draft.trim().length === 0}
          className="grid h-9 w-9 shrink-0 cursor-pointer place-items-center rounded-control text-signal-green hover:bg-graphite-800 disabled:text-ink-600"
        >
          <Check className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onCancelRename}
          aria-label="Cancel rename"
          className="grid h-9 w-9 shrink-0 cursor-pointer place-items-center rounded-control text-ink-500 hover:bg-graphite-800 hover:text-ink-100"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </form>
    );
  }

  return (
    <div
      className={cn(
        "group flex items-center gap-1 rounded-control transition-colors",
        active ? "bg-graphite-800" : "hover:bg-graphite-900",
      )}
    >
      <Link
        to={`/chats/${encodeURIComponent(conversation.id)}`}
        aria-current={active ? "page" : undefined}
        className="min-w-0 flex-1 rounded-control px-3 py-2.5"
      >
        <span className="flex items-center gap-1.5">
          {conversation.pinned ? (
            <Pin className="h-3 w-3 shrink-0 text-ink-500" aria-hidden="true" />
          ) : null}
          <span
            className={cn(
              "block truncate text-sm font-medium",
              active ? "text-ink-50" : "text-ink-300",
            )}
          >
            {label}
          </span>
        </span>
        <span className="block truncate text-xs text-ink-600">
          {relativeTime(conversation.updated_at ?? conversation.created_at)}
        </span>
      </Link>
      <div className="flex shrink-0 items-center gap-0.5 pr-1 sm:opacity-0 sm:transition-opacity sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
        <button
          type="button"
          onClick={onTogglePin}
          disabled={busy}
          aria-label={conversation.pinned ? `Unpin ${label}` : `Pin ${label}`}
          title={conversation.pinned ? "Unpin" : "Pin"}
          className="grid h-11 w-11 cursor-pointer place-items-center rounded-control text-ink-500 hover:bg-graphite-800 hover:text-ink-100 disabled:opacity-50 sm:h-8 sm:w-8"
        >
          {conversation.pinned ? (
            <PinOff className="h-4 w-4" aria-hidden="true" />
          ) : (
            <Pin className="h-4 w-4" aria-hidden="true" />
          )}
        </button>
        <button
          type="button"
          onClick={onStartRename}
          aria-label={`Rename ${label}`}
          title="Rename"
          className="grid h-11 w-11 cursor-pointer place-items-center rounded-control text-ink-500 hover:bg-graphite-800 hover:text-ink-100 sm:h-8 sm:w-8"
        >
          <Pencil className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Delete ${label}`}
          title="Delete"
          className="grid h-11 w-11 cursor-pointer place-items-center rounded-control text-ink-500 hover:bg-graphite-800 hover:text-signal-red sm:h-8 sm:w-8"
        >
          <Trash2 className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

export function ChatPage() {
  const chat = useChatAgents();
  const { id } = useParams();
  const navigate = useNavigate();
  const { notify } = useToast();
  const activeId = id ?? null;
  const activeConversation = chat.conversations.find((row) => row.id === activeId) ?? null;
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const createAndOpen = async () => {
    const created = await chat.createNewConversation();
    if (created) {
      navigate(`/chats/${encodeURIComponent(created.id)}`);
    }
  };

  const rename = async (conversation: Conversation, title: string) => {
    setBusyId(conversation.id);
    try {
      await chat.renameConversation(conversation.id, title);
      setRenamingId(null);
      notify("Conversation renamed", "success");
    } catch (error) {
      notify(describeError(error), "error");
    } finally {
      setBusyId(null);
    }
  };

  const togglePin = async (conversation: Conversation) => {
    setBusyId(conversation.id);
    try {
      await chat.setPinned(conversation.id, !conversation.pinned);
      notify(conversation.pinned ? "Conversation unpinned" : "Conversation pinned", "success");
    } catch (error) {
      notify(describeError(error), "error");
    } finally {
      setBusyId(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await chat.removeConversation(deleteTarget.id);
      if (deleteTarget.id === activeId) navigate("/chats");
      notify("Conversation deleted", "success");
      setDeleteTarget(null);
    } catch (error) {
      notify(describeError(error), "error");
    } finally {
      setDeleting(false);
    }
  };

  const pinned = chat.conversations.filter((row) => row.pinned);
  const recent = chat.conversations.filter((row) => !row.pinned);
  const searching = chat.query.trim().length > 0;

  const renderRows = (rows: Conversation[]) =>
    rows.map((conversation) => (
      <li key={conversation.id}>
        <ConversationRow
          conversation={conversation}
          active={conversation.id === activeId}
          renaming={renamingId === conversation.id}
          busy={busyId === conversation.id}
          onStartRename={() => setRenamingId(conversation.id)}
          onCancelRename={() => setRenamingId(null)}
          onSubmitRename={(title) => void rename(conversation, title)}
          onTogglePin={() => void togglePin(conversation)}
          onDelete={() => setDeleteTarget(conversation)}
        />
      </li>
    ));

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] md:h-dvh">
      <aside
        aria-label="Conversations"
        className={cn(
          "w-full flex-col border-r border-graphite-800 bg-graphite-950 sm:w-80 sm:shrink-0",
          activeId ? "hidden sm:flex" : "flex",
        )}
      >
        <div className="flex items-center justify-between gap-2 px-4 pb-3 pt-5">
          <div>
            <h1 className="text-base font-semibold text-ink-50">Chats</h1>
            <p className="text-xs text-ink-600">Your conversations</p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void createAndOpen()}
            disabled={chat.creating || !chat.agentId}
            aria-label="New conversation"
          >
            <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
            New
          </Button>
        </div>

        <div className="px-3 pb-2">
          <label htmlFor="chat-search" className="sr-only">
            Search conversations
          </label>
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-600"
              aria-hidden="true"
            />
            <TextInput
              id="chat-search"
              type="search"
              value={chat.query}
              onChange={(event) => chat.setQuery(event.target.value)}
              placeholder="Search chats"
              className="h-9 pl-9"
            />
          </div>
        </div>

        <div className="px-3 pb-3">
          <label htmlFor="chat-agent" className="sr-only">
            Chat as agent
          </label>
          <SelectControl
            id="chat-agent"
            value={chat.agentId ?? ""}
            onChange={(event) => chat.selectAgent(event.target.value)}
            aria-label="Chat as agent"
            className="h-9 text-xs"
          >
            {chat.agents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>
                Chat as {agent.agent_id.slice(0, 18)}…
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
            searching ? (
              <EmptyState
                icon="search"
                title="No matching chats"
                body={`Nothing matched “${chat.query.trim()}”. Try another word.`}
                action={
                  <Button variant="secondary" size="sm" onClick={() => chat.setQuery("")}>
                    Clear search
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon="chat"
                title="No conversations yet"
                body="Start a chat with your assistant. Your history stays here."
                action={
                  <Button variant="primary" size="sm" onClick={() => void createAndOpen()}>
                    New conversation
                  </Button>
                }
              />
            )
          ) : null}
          {pinned.length > 0 ? (
            <section aria-label="Pinned conversations" className="mb-2">
              <h2 className="px-3 pb-1 pt-1 text-[11px] font-semibold uppercase tracking-wide text-ink-600">
                Pinned
              </h2>
              <ul className="flex flex-col gap-0.5">{renderRows(pinned)}</ul>
            </section>
          ) : null}
          {recent.length > 0 ? (
            <section aria-label="Recent conversations">
              <h2 className="px-3 pb-1 pt-1 text-[11px] font-semibold uppercase tracking-wide text-ink-600">
                {searching ? "Results" : "Recent"}
              </h2>
              <ul className="flex flex-col gap-0.5">{renderRows(recent)}</ul>
            </section>
          ) : null}
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
              title="What would you like to talk about?"
              body="Pick a chat on the left, or start a new one. Ask a question, draft something, or think out loud."
              action={
                <Button variant="primary" size="sm" onClick={() => void createAndOpen()}>
                  New conversation
                </Button>
              }
            />
          </div>
        ) : (
          <ThreadViewInner
            conversationId={activeId}
            title={activeConversation ? conversationLabel(activeConversation) : null}
          />
        )}
      </section>

      <Dialog
        open={deleteTarget !== null}
        onClose={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        title="Delete conversation?"
        description={
          deleteTarget
            ? `“${conversationLabel(deleteTarget)}” and its messages will be permanently deleted. This cannot be undone.`
            : undefined
        }
        busy={deleting}
      >
        <DialogActions>
          <Button
            variant="ghost"
            onClick={() => setDeleteTarget(null)}
            disabled={deleting}
          >
            Cancel
          </Button>
          <Button variant="danger" onClick={() => void confirmDelete()} busy={deleting}>
            Delete conversation
          </Button>
        </DialogActions>
      </Dialog>
    </div>
  );
}

function ThreadViewInner({
  conversationId,
  title,
}: {
  conversationId: string;
  title: string | null;
}) {
  const thread = useThread(conversationId);
  const network = useNetwork();
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const lastLength = thread.messages[thread.messages.length - 1]?.content.length ?? 0;

  useEffect(() => {
    if (stickRef.current) {
      // scrollIntoView is absent in some test DOMs; guard it.
      bottomRef.current?.scrollIntoView?.({ behavior: "auto", block: "end" });
    }
  }, [thread.messages.length, lastLength, thread.sending]);

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
      <div className="border-b border-graphite-800 px-4 py-3">
        <div className="mx-auto flex w-full max-w-3xl items-center justify-between gap-3">
          <h2 className="truncate text-sm font-semibold text-ink-50">
            {title ?? "Conversation"}
          </h2>
          <span className="shrink-0 text-xs text-ink-600">
            {thread.messages.length} {thread.messages.length === 1 ? "message" : "messages"}
          </span>
        </div>
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
            body="Send the first message below. The assistant replies through your configured model."
          />
        ) : null}
        {thread.messages.map((message) =>
          message.role === "agent" && message.content.length === 0 ? null : (
            <MessageBubble key={message.id} message={message} />
          ),
        )}
        {thread.sending &&
        thread.messages[thread.messages.length - 1]?.content.length === 0 ? (
          <Spinner label="Assistant is thinking" />
        ) : null}
        <div ref={bottomRef} aria-hidden="true" />
      </div>
      <Composer
        sending={thread.sending}
        sendError={thread.sendError}
        onSend={(message) => void thread.send(message)}
        onStop={thread.stop}
        connectedAgentCount={network.connectedCount}
      />
    </>
  );
}
