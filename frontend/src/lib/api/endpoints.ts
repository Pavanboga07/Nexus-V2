import { ApiError, publicGet, request } from "./client";
import type {
  Agent,
  Conversation,
  ConversationMessage,
  Execution,
  InvitationCreated,
  InvitationInfo,
  InvitationRow,
  Pairing,
  PairingCompletion,
  Peer,
  ProviderStatus,
  ProviderTestResult,
  RemoteRequest,
  Ticket,
  Turn,
} from "./types";

/** One function per backend route. No invented endpoints. */

export function getAgents(baseUrl: string, token: string): Promise<Agent[]> {
  return request<Agent[]>(baseUrl, token, "/agents");
}

export function getPeers(
  baseUrl: string,
  token: string,
  agentId: string,
): Promise<Peer[]> {
  return request<Peer[]>(
    baseUrl,
    token,
    `/agents/${encodeURIComponent(agentId)}/peers`,
  );
}

export interface ListConversationsParams {
  agentId: string;
  limit?: number;
  offset?: number;
  query?: string;
}

export function listConversations(
  baseUrl: string,
  token: string,
  params: ListConversationsParams,
): Promise<Conversation[]> {
  const search = new URLSearchParams({ agent_id: params.agentId });
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  if (params.query && params.query.trim().length > 0) {
    search.set("q", params.query.trim());
  }
  return request<Conversation[]>(
    baseUrl,
    token,
    `/conversations?${search.toString()}`,
  );
}

export function updateConversation(
  baseUrl: string,
  token: string,
  conversationId: string,
  patch: { title?: string; pinned?: boolean },
): Promise<Conversation> {
  return request<Conversation>(
    baseUrl,
    token,
    `/conversations/${encodeURIComponent(conversationId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
}

export function deleteConversation(
  baseUrl: string,
  token: string,
  conversationId: string,
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    baseUrl,
    token,
    `/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE" },
  );
}

export function createConversation(
  baseUrl: string,
  token: string,
  body: { agent_id: string; title?: string | null },
): Promise<Conversation> {
  return request<Conversation>(baseUrl, token, "/conversations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listMessages(
  baseUrl: string,
  token: string,
  conversationId: string,
  limit = 50,
  offset = 0,
): Promise<ConversationMessage[]> {
  return request<ConversationMessage[]>(
    baseUrl,
    token,
    `/conversations/${encodeURIComponent(conversationId)}/messages?limit=${limit}&offset=${offset}`,
  );
}

export function sendTurn(
  baseUrl: string,
  token: string,
  conversationId: string,
  message: string,
  signal?: AbortSignal,
): Promise<Turn> {
  return request<Turn>(
    baseUrl,
    token,
    `/conversations/${encodeURIComponent(conversationId)}/turns`,
    { method: "POST", body: JSON.stringify({ message }), signal },
  );
}

export interface TurnStreamHandlers {
  onUser?: (userMessageId: number) => void;
  onDelta: (text: string) => void;
}

/** Stream one turn over Server-Sent Events; resolves with the final turn. */
export async function sendTurnStream(
  baseUrl: string,
  token: string,
  conversationId: string,
  message: string,
  handlers: TurnStreamHandlers,
  signal?: AbortSignal,
): Promise<Turn> {
  const response = await fetch(
    `${baseUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/turns/stream`,
    {
      method: "POST",
      signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ message }),
    },
  );
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const record =
      body !== null && typeof body === "object"
        ? (body as Record<string, unknown>)
        : null;
    throw new ApiError(
      response.status,
      typeof record?.code === "string" ? record.code : "HTTP_ERROR",
      typeof record?.message === "string"
        ? record.message
        : "The request failed.",
    );
  }
  if (!response.body) {
    throw new ApiError(0, "NETWORK_ERROR", "The response could not be read.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done: Turn | null = null;

  const handle = (raw: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length === 0) return;
    const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
    if (event === "user" && typeof payload.user_message_id === "number") {
      handlers.onUser?.(payload.user_message_id);
    } else if (event === "delta" && typeof payload.text === "string") {
      handlers.onDelta(payload.text);
    } else if (event === "done") {
      done = payload as unknown as Turn;
    } else if (event === "error") {
      throw new ApiError(
        500,
        typeof payload.code === "string" ? payload.code : "INTERNAL_ERROR",
        typeof payload.message === "string"
          ? payload.message
          : "The answer could not be completed.",
      );
    }
  };

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        handle(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim().length > 0) handle(buffer);
  } finally {
    reader.releaseLock();
  }
  if (done === null) {
    throw new ApiError(0, "STREAM_INCOMPLETE", "The answer ended unexpectedly.");
  }
  return done;
}

export interface CreateRequestBody {
  sender_agent_id: string;
  recipient_agent_id: string;
  query: string;
  requested_capabilities?: string[];
  expires_in_seconds?: number;
}

export function createRemoteRequest(
  baseUrl: string,
  token: string,
  body: CreateRequestBody,
): Promise<RemoteRequest> {
  return request<RemoteRequest>(baseUrl, token, "/requests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface ListRequestsParams {
  role: "outbound" | "inbound";
  status?: string;
  limit?: number;
  offset?: number;
}

export function listRequests(
  baseUrl: string,
  token: string,
  params: ListRequestsParams,
): Promise<RemoteRequest[]> {
  const search = new URLSearchParams({ role: params.role });
  if (params.status) search.set("status", params.status);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  return request<RemoteRequest[]>(
    baseUrl,
    token,
    `/requests?${search.toString()}`,
  );
}

export function getRequest(
  baseUrl: string,
  token: string,
  requestId: string,
): Promise<RemoteRequest> {
  return request<RemoteRequest>(
    baseUrl,
    token,
    `/requests/${encodeURIComponent(requestId)}`,
  );
}

export function listPendingApprovals(
  baseUrl: string,
  token: string,
  limit = 50,
  offset = 0,
): Promise<RemoteRequest[]> {
  return request<RemoteRequest[]>(
    baseUrl,
    token,
    `/approvals/pending?limit=${limit}&offset=${offset}`,
  );
}

export function approveRequest(
  baseUrl: string,
  token: string,
  requestId: string,
  reason?: string | null,
): Promise<RemoteRequest> {
  return request<RemoteRequest>(
    baseUrl,
    token,
    `/requests/${encodeURIComponent(requestId)}/approve`,
    { method: "POST", body: JSON.stringify({ reason: reason ?? null }) },
  );
}

export function rejectRequest(
  baseUrl: string,
  token: string,
  requestId: string,
  reason?: string | null,
): Promise<RemoteRequest> {
  return request<RemoteRequest>(
    baseUrl,
    token,
    `/requests/${encodeURIComponent(requestId)}/reject`,
    { method: "POST", body: JSON.stringify({ reason: reason ?? null }) },
  );
}

export function executeRequest(
  baseUrl: string,
  token: string,
  requestId: string,
): Promise<Execution> {
  return request<Execution>(
    baseUrl,
    token,
    `/requests/${encodeURIComponent(requestId)}/execute`,
    { method: "POST" },
  );
}

export function recoverRequest(
  baseUrl: string,
  token: string,
  requestId: string,
  action: "retry" | "resolve",
): Promise<RemoteRequest> {
  return request<RemoteRequest>(
    baseUrl,
    token,
    `/requests/${encodeURIComponent(requestId)}/recover`,
    { method: "POST", body: JSON.stringify({ action }) },
  );
}

export function issueTicket(baseUrl: string, token: string): Promise<Ticket> {
  return request<Ticket>(baseUrl, token, "/events/ticket", { method: "POST" });
}

export function getProviderStatus(
  baseUrl: string,
  token: string,
): Promise<ProviderStatus> {
  return request<ProviderStatus>(baseUrl, token, "/settings/ai-provider");
}

export function saveProviderConfig(
  baseUrl: string,
  token: string,
  body: { provider: string; model: string; api_key: string },
): Promise<ProviderStatus> {
  return request<ProviderStatus>(baseUrl, token, "/settings/ai-provider", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function deleteProviderConfig(
  baseUrl: string,
  token: string,
): Promise<{ removed: boolean }> {
  return request<{ removed: boolean }>(baseUrl, token, "/settings/ai-provider", {
    method: "DELETE",
  });
}

export function testProviderConfig(
  baseUrl: string,
  token: string,
  body: { provider?: string; model?: string; api_key?: string },
): Promise<ProviderTestResult> {
  return request<ProviderTestResult>(
    baseUrl, token, "/settings/ai-provider/test",
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function createInvitation(
  baseUrl: string,
  token: string,
  body: { agent_id: string; ttl_seconds?: number },
): Promise<InvitationCreated> {
  return request<InvitationCreated>(
    baseUrl, token, "/pairings/invitations",
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function listInvitations(
  baseUrl: string,
  token: string,
  agentId: string,
): Promise<InvitationRow[]> {
  return request<InvitationRow[]>(
    baseUrl, token,
    `/pairings/invitations?agent_id=${encodeURIComponent(agentId)}`,
  );
}

export function revokeInvitation(
  baseUrl: string,
  token: string,
  invitationId: string,
): Promise<{ revoked: boolean }> {
  return request<{ revoked: boolean }>(
    baseUrl, token,
    `/pairings/invitations/${encodeURIComponent(invitationId)}/revoke`,
    { method: "POST" },
  );
}

export function validateInvitation(
  baseUrl: string,
  token: string,
  invitation: string,
): Promise<InvitationInfo> {
  return request<InvitationInfo>(baseUrl, token, "/pairings/validate", {
    method: "POST",
    body: JSON.stringify({ invitation }),
  });
}

export function acceptPairing(
  baseUrl: string,
  token: string,
  body: { invitation: string; sender_agent_id: string },
): Promise<Pairing> {
  return request<Pairing>(baseUrl, token, "/pairings/accept", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listPairings(
  baseUrl: string,
  token: string,
  agentId: string,
): Promise<Pairing[]> {
  return request<Pairing[]>(
    baseUrl, token,
    `/pairings?agent_id=${encodeURIComponent(agentId)}`,
  );
}

export function completePairing(
  baseUrl: string,
  token: string,
  requestId: string,
): Promise<PairingCompletion> {
  return request<PairingCompletion>(baseUrl, token, "/pairings/complete", {
    method: "POST",
    body: JSON.stringify({ request_id: requestId }),
  });
}

export function revokePeer(
  baseUrl: string,
  token: string,
  peerAgentId: string,
): Promise<{ revoked: number }> {
  return request<{ revoked: number }>(
    baseUrl, token,
    `/peers/${encodeURIComponent(peerAgentId)}/revoke`,
    { method: "POST" },
  );
}

export function checkLive(baseUrl: string): Promise<{ status: string }> {
  return publicGet<{ status: string }>(baseUrl, "/health/live");
}

export function checkReady(baseUrl: string): Promise<{ status: string }> {
  return publicGet<{ status: string }>(baseUrl, "/health/ready");
}
