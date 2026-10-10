import { publicGet, request } from "./client";
import type {
  Agent,
  Conversation,
  ConversationMessage,
  Execution,
  Peer,
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
}

export function listConversations(
  baseUrl: string,
  token: string,
  params: ListConversationsParams,
): Promise<Conversation[]> {
  const search = new URLSearchParams({ agent_id: params.agentId });
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  return request<Conversation[]>(
    baseUrl,
    token,
    `/conversations?${search.toString()}`,
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

export function issueTicket(baseUrl: string, token: string): Promise<Ticket> {
  return request<Ticket>(baseUrl, token, "/events/ticket", { method: "POST" });
}

export function checkLive(baseUrl: string): Promise<{ status: string }> {
  return publicGet<{ status: string }>(baseUrl, "/health/live");
}

export function checkReady(baseUrl: string): Promise<{ status: string }> {
  return publicGet<{ status: string }>(baseUrl, "/health/ready");
}
