import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../lib/auth/AuthProvider";
import { describeError } from "../../lib/api/client";
import { createRemoteRequest, getAgents, getPeers } from "../../lib/api/endpoints";
import type { Agent, Peer } from "../../lib/api/types";
import { Button } from "../../components/ui/Button";
import { Field, SelectControl, TextArea } from "../../components/ui/Field";
import { Spinner } from "../../components/ui/Feedback";
import { useToast } from "../../components/ui/Toast";

const DRAFT_KEY = "nexus.remote.draft";
const RECIPIENT_KEY = "nexus.remote.recipient";

interface EligibleAgent extends Agent {
  peers: Peer[];
}

function loadDraft(): { recipient: string; query: string } {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { recipient?: string; query?: string };
      return {
        recipient: typeof parsed.recipient === "string" ? parsed.recipient : "",
        query: typeof parsed.query === "string" ? parsed.query : "",
      };
    }
  } catch {
    // Corrupt draft: start fresh rather than failing the panel.
  }
  try {
    return { recipient: localStorage.getItem(RECIPIENT_KEY) ?? "", query: "" };
  } catch {
    return { recipient: "", query: "" };
  }
}

/** Collapsible connected-agent composer. Never sends on collapse; the
 *  unsent draft survives collapse and reopening. Submits only to the
 *  remote-request endpoint, never to the local conversation. */
export function RemoteRequestPanel({ onCollapse }: { onCollapse: () => void }) {
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [agents, setAgents] = useState<EligibleAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initial] = useState(loadDraft);
  const [senderId, setSenderId] = useState("");
  const [recipientId, setRecipientId] = useState(initial.recipient);
  const [query, setQuery] = useState(initial.query);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState<{ requestId: string; status: string } | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const owned = await getAgents(baseUrl, token);
        const withPeers = await Promise.all(
          owned.map(async (agent) => {
            try {
              const peers = await getPeers(baseUrl, token, agent.agent_id);
              return { ...agent, peers };
            } catch {
              return { ...agent, peers: [] as Peer[] };
            }
          }),
        );
        if (!cancelled) {
          setAgents(withPeers);
          if (withPeers.length > 0 && !senderId) {
            setSenderId(withPeers[0].agent_id);
          }
          setLoading(false);
        }
      } catch (error) {
        if (!cancelled) {
          setLoading(false);
          setLoadError(describeError(error));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, token]);

  // Preserve the unsent draft across collapse/reopen and reloads.
  useEffect(() => {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ recipient: recipientId, query }));
      if (recipientId) localStorage.setItem(RECIPIENT_KEY, recipientId);
    } catch {
      // Draft persistence is best-effort.
    }
  }, [recipientId, query]);

  const sender = agents.find((agent) => agent.agent_id === senderId) ?? agents[0];
  // Only connected peers are eligible: pending peers have not
  // completed mutual pairing, revoked ones lost authorization.
  const recipientOptions = (sender?.peers ?? []).filter(
    (peer) => peer.status === "connected");
  const recipientKnown = recipientOptions.some((peer) => peer.peer_agent_id === recipientId);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting || !sender || !recipientKnown || query.trim().length === 0) return;
    setSubmitting(true);
    setSubmitError(null);
    setSuccess(null);
    try {
      const created = await createRemoteRequest(baseUrl, token, {
        sender_agent_id: sender.agent_id,
        recipient_agent_id: recipientId,
        query: query.trim(),
      });
      setSuccess({ requestId: created.request_id, status: created.status });
      setQuery("");
      try {
        sessionStorage.removeItem(DRAFT_KEY);
      } catch {
        // Draft already cleared in state; ignore storage failure.
      }
      notify(`Request ${created.request_id} created.`, "success");
    } catch (error) {
      // Failure keeps the draft intact for retry.
      setSubmitError(describeError(error));
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <Spinner label="Loading connected agents" />;
  }
  if (loadError) {
    return (
      <p role="alert" className="text-sm text-signal-red">
        {loadError}
      </p>
    );
  }
  if (agents.length === 0 || recipientOptions.length === 0) {
    return (
      <div className="flex flex-col gap-2 text-sm">
        <p className="text-ink-300">
          No connected agent is available right now. Remote requests need
          a completed pairing — pending or revoked peers cannot receive
          work.
        </p>
        <div>
          <Link to="/agents" className="font-medium text-accent-300 hover:text-accent-400">
            Pair an agent first
          </Link>
        </div>
        <div>
          <Button variant="ghost" size="sm" onClick={onCollapse}>
            Close panel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3">
      <Field label="Send as">
        <SelectControl
          value={sender?.agent_id ?? ""}
          onChange={(event) => {
            setSenderId(event.target.value);
            setRecipientId("");
          }}
        >
          {agents.map((agent) => (
            <option key={agent.agent_id} value={agent.agent_id}>
              {agent.agent_id}
            </option>
          ))}
        </SelectControl>
      </Field>
      <Field
        label="Connected agent"
        hint={
          recipientKnown
            ? `This task goes to ${recipientId}. It will not appear as a chat message.`
            : "Choose which agent receives the request."
        }
      >
        <SelectControl
          value={recipientKnown ? recipientId : ""}
          onChange={(event) => setRecipientId(event.target.value)}
        >
          <option value="" disabled>
            Select an agent
          </option>
          {recipientOptions.map((peer) => (
            <option key={peer.peer_agent_id} value={peer.peer_agent_id}>
              {peer.peer_agent_id} ({peer.status})
            </option>
          ))}
        </SelectControl>
      </Field>
      <Field
        label="Task"
        hint="What should the remote agent do? Be specific."
        error={undefined}
      >
        <TextArea
          value={query}
          onChange={(event) => setQuery(event.target.value.slice(0, 65536))}
          placeholder="Describe the task for the remote agent"
          maxLength={65536}
        />
      </Field>

      {submitError ? (
        <p role="alert" className="text-sm text-signal-red">
          {submitError} Your draft was kept.
        </p>
      ) : null}
      {success ? (
        <p role="status" className="text-sm text-signal-green">
          Request {success.requestId} created ({success.status}).{" "}
          <Link
            to={`/requests/${encodeURIComponent(success.requestId)}`}
            className="font-medium text-accent-300 hover:text-accent-400"
          >
            View request
          </Link>
        </p>
      ) : null}

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button variant="ghost" size="sm" onClick={onCollapse}>
          Close panel
        </Button>
        <Button
          variant="primary"
          size="sm"
          type="submit"
          busy={submitting}
          disabled={!recipientKnown || query.trim().length === 0}
        >
          {submitting ? <Spinner label="Sending request" /> : "Send request"}
        </Button>
      </div>
    </form>
  );
}
