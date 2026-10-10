import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { describeError } from "../../lib/api/client";
import { getAgents, getPeers } from "../../lib/api/endpoints";
import type { Agent, Peer } from "../../lib/api/types";

export interface AgentWithPeers extends Agent {
  peers: Peer[];
  peersState: "ready" | "error";
}

export interface NetworkState {
  agents: AgentWithPeers[];
  networkState: "loading" | "ready" | "error";
  networkError: string | null;
  reload: () => void;
  connectedCount: number;
}

/** Local agents plus their known peers. Peer failures degrade per
 *  agent rather than failing the whole network view. */
export function useNetwork(): NetworkState {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const [agents, setAgents] = useState<AgentWithPeers[]>([]);
  const [networkState, setNetworkState] = useState<"loading" | "ready" | "error">("loading");
  const [networkError, setNetworkError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setNetworkState("loading");
    setNetworkError(null);
    try {
      const owned = await getAgents(baseUrl, token);
      const withPeers = await Promise.all(
        owned.map(async (agent): Promise<AgentWithPeers> => {
          try {
            const peers = await getPeers(baseUrl, token, agent.agent_id);
            return { ...agent, peers, peersState: "ready" };
          } catch {
            return { ...agent, peers: [], peersState: "error" };
          }
        }),
      );
      setAgents(withPeers);
      setNetworkState("ready");
    } catch (error) {
      setNetworkState("error");
      setNetworkError(describeError(error));
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => subscribe("agents", () => void load()), [subscribe, load]);

  const connectedCount = agents.reduce((sum, agent) => sum + agent.peers.length, 0);

  return { agents, networkState, networkError, reload: () => void load(), connectedCount };
}
