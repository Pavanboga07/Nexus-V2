import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { describeError } from "../../lib/api/client";
import { revokePeer } from "../../lib/api/endpoints";
import { useNetwork } from "./useNetwork";
import { ConnectAgentPanel } from "./ConnectAgentPanel";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/Feedback";
import { PageHeader } from "../../components/ui/PageHeader";
import { useToast } from "../../components/ui/Toast";

function shortKey(publicKey: string): string {
  if (publicKey.length <= 24) return publicKey;
  return `${publicKey.slice(0, 12)}...${publicKey.slice(-8)}`;
}

/** Local agents and their known remote peers, from live API data.
 *  Pairing (invitations + acceptance) is the only way peers are added
 *  through the UI; revoking removes local authorization immediately. */
export function AgentsPage() {
  const network = useNetwork();
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const onRevokePeer = async (peerAgentId: string) => {
    setRevoking(peerAgentId);
    setRevokeError(null);
    try {
      const result = await revokePeer(baseUrl, token, peerAgentId);
      notify(
        result.revoked > 0
          ? `Authorization for ${peerAgentId} revoked.`
          : `${peerAgentId} was not authorized here.`,
        "info",
      );
      network.reload();
    } catch (caught) {
      setRevokeError(describeError(caught));
    } finally {
      setRevoking(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <PageHeader
        title="Agents"
        subtitle="Your local agents and the remote peers they know about."
      />
      <ConnectAgentPanel
        agents={network.agents}
        onChanged={network.reload}
      />
      {revokeError ? (
        <p role="alert" className="mb-4 text-sm text-signal-red">{revokeError}</p>
      ) : null}
      {network.networkState === "loading" ? (
        <div className="flex flex-col gap-3" role="status">
          <span className="sr-only">Loading agents</span>
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
      ) : null}
      {network.networkState === "error" ? (
        <ErrorState
          title="Agents unavailable"
          body={network.networkError ?? "Try again."}
          onRetry={network.reload}
        />
      ) : null}
      {network.networkState === "ready" && network.agents.length === 0 ? (
        <EmptyState
          icon="inbox"
          title="No agents yet"
          body="No local agents are registered for this owner. Create an identity on the backend to get started."
        />
      ) : null}
      <ul className="flex flex-col gap-4">
        {network.agents.map((agent) => (
          <li
            key={agent.agent_id}
            className="rounded-card border border-graphite-700 bg-graphite-900 p-5"
          >
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-mono text-[15px] font-semibold text-ink-50">
                {agent.agent_id}
              </h2>
              <span
                className={
                  agent.active
                    ? "inline-flex items-center gap-1.5 text-xs text-signal-teal"
                    : "inline-flex items-center gap-1.5 text-xs text-ink-500"
                }
              >
                <span
                  aria-hidden="true"
                  className={
                    agent.active
                      ? "h-2 w-2 rounded-full bg-signal-teal"
                      : "h-2 w-2 rounded-full bg-ink-600"
                  }
                />
                {agent.active ? "Active" : "Inactive"}
              </span>
            </div>
            <p className="mt-1 font-mono text-xs text-ink-500">
              {agent.key_algorithm} · {shortKey(agent.public_key)}
            </p>
            <div className="mt-4 border-t border-graphite-800 pt-3">
              <h3 className="text-[13px] font-medium text-ink-300">
                Connected peers ({agent.peers.length})
              </h3>
              {agent.peersState === "error" ? (
                <p className="mt-1 text-sm text-signal-red">
                  Peer list failed to load for this agent.
                </p>
              ) : null}
              {agent.peers.length === 0 && agent.peersState === "ready" ? (
                <p className="mt-1 text-sm text-ink-500">
                  No known peers. Pair this agent with a remote peer to ask it
                  for remote work.
                </p>
              ) : null}
              <ul className="mt-2 flex flex-col gap-1.5">
                {agent.peers.map((peer) => (
                  <li
                    key={peer.peer_agent_id}
                    className="flex items-center justify-between gap-3 rounded-control bg-graphite-950 px-3 py-2 text-sm"
                  >
                    <span className="truncate font-mono text-ink-100">
                      {peer.peer_agent_id}
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="text-xs text-ink-500">{peer.status}</span>
                      {peer.status !== "revoked" ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          busy={revoking === peer.peer_agent_id}
                          onClick={() => void onRevokePeer(peer.peer_agent_id)}
                          aria-label={`Revoke authorization for ${peer.peer_agent_id}`}
                        >
                          Revoke
                        </Button>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </li>
        ))}
      </ul>
      {network.networkState === "ready" && network.connectedCount > 0 ? (
        <p className="mt-6 text-sm text-ink-500">
          {network.connectedCount} connected{" "}
          {network.connectedCount === 1 ? "agent" : "agents"} available for
          remote requests.{" "}
          <Link to="/chats" className="font-medium text-accent-300 hover:text-accent-400">
            Ask one from any chat
            <ArrowRight className="ml-1 inline h-3.5 w-3.5" aria-hidden="true" />
          </Link>
        </p>
      ) : null}
    </div>
  );
}
