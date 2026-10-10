import { useState, type FormEvent, type KeyboardEvent } from "react";
import { ChevronDown, Send } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Spinner } from "../../components/ui/Feedback";
import { RemoteRequestPanel } from "./RemoteRequestPanel";

export interface ComposerProps {
  disabled?: boolean;
  sending: boolean;
  sendError: string | null;
  onSend: (message: string) => void;
  connectedAgentCount: number;
  /** Thread the remote panel links (Q&A mirrored into chat). */
  conversationId?: string | null;
}

/** Local chat composer first; the connected-agent request panel is a
 *  secondary disclosure, collapsed by default, draft-preserving. */
export function Composer({
  disabled = false,
  sending,
  sendError,
  onSend,
  connectedAgentCount,
  conversationId = null,
}: ComposerProps) {
  const [draft, setDraft] = useState("");
  const [panelOpen, setPanelOpen] = useState(false);
  const canSend = draft.trim().length > 0 && !sending && !disabled;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const message = draft.trim();
    if (message.length === 0 || sending || disabled) return;
    setDraft("");
    onSend(message);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-graphite-800 bg-graphite-950 px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-3">
      <form onSubmit={submit} className="mx-auto flex max-w-3xl flex-col gap-2">
        <label htmlFor="chat-composer" className="sr-only">
          Message your assistant
        </label>
        <div className="flex items-end gap-2 rounded-card border border-graphite-700 bg-graphite-900 p-2 focus-within:border-accent-500">
          <textarea
            id="chat-composer"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={disabled || sending}
            placeholder="Message your assistant"
            aria-describedby={sendError ? "composer-error" : undefined}
            className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-2 py-2 text-[15px] leading-relaxed text-ink-50 placeholder:text-ink-600 focus:outline-none disabled:opacity-60"
          />
          <Button
            variant="primary"
            size="icon"
            type="submit"
            disabled={!canSend}
            aria-label="Send message"
          >
            {sending ? (
              <Spinner label="Sending" />
            ) : (
              <Send className="h-4 w-4" aria-hidden="true" />
            )}
          </Button>
        </div>
        <p className="text-xs text-ink-600">
          Enter sends, Shift+Enter adds a line.
        </p>
        {sendError ? (
          <p id="composer-error" role="alert" className="text-sm text-signal-red">
            {sendError} Your draft was kept above.
          </p>
        ) : null}
      </form>

      {/* The remote-request panel carries its own <form>: it must stay a
        sibling of the chat form, never nested inside it. Nested forms are
        a parse error — the inner form is dropped and its submit button
        silently submits the OUTER form instead (a click on an enabled
        "Send request" doing nothing, or sending the chat draft). */}
      <div className="rounded-control border border-graphite-800 bg-graphite-900/60">
        <button
          type="button"
          onClick={() => setPanelOpen((open) => !open)}
          aria-expanded={panelOpen}
          aria-controls="remote-request-panel"
          className="flex min-h-[44px] w-full cursor-pointer items-center gap-2 px-3 text-left text-sm text-ink-300 hover:text-ink-50"
        >
          <ChevronDown
            aria-hidden="true"
            className={`h-4 w-4 transition-transform ${panelOpen ? "rotate-180" : ""}`}
          />
          Ask a connected agent
          <span className="ml-auto text-xs text-ink-600">
            {connectedAgentCount > 0
              ? `${connectedAgentCount} available`
              : "none connected"}
          </span>
        </button>
        {panelOpen ? (
          <div id="remote-request-panel" className="border-t border-graphite-800 p-3">
            <RemoteRequestPanel
              onCollapse={() => setPanelOpen(false)}
              conversationId={conversationId}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
