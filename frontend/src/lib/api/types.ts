/** Backend API types. Shapes mirror backend/api/schemas.py and the
 *  route implementations exactly; see docs/REST_API.md. */

export interface Agent {
  agent_id: string;
  active: boolean;
  owned: boolean;
  public_key: string;
  key_algorithm: string;
}

export interface Peer {
  peer_agent_id: string;
  status: string;
}

export interface Conversation {
  id: string;
  agent_id: string;
  title: string | null;
  created_at: string;
}

export interface ConversationMessage {
  id: number;
  role: string;
  content: string;
  created_at: string;
}

export interface Turn {
  conversation_id: string;
  user_message_id: number;
  assistant_message_id: number;
  answer: string;
  model: string;
}

/** All request states in backend/requests/states.py. */
export type RequestStatus =
  | "CREATED"
  | "QUEUED"
  | "SENT"
  | "DELIVERED"
  | "WAITING_APPROVAL"
  | "APPROVED"
  | "PROCESSING"
  | "COMPLETED"
  | "REJECTED"
  | "EXPIRED"
  | "FAILED"
  | "CANCELLED";

export interface RemoteRequest {
  request_id: string;
  sender_agent_id: string;
  recipient_agent_id: string;
  query: string;
  requested_capabilities: string[];
  status: RequestStatus;
  created_at: string;
  expires_at: string;
  completed_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
  response_message_id: string | null;
  answer: string | null;
}

export interface Execution {
  request_id: string;
  status: string;
  executed: boolean;
  response_message_id: string | null;
  detail: string;
}

export interface Ticket {
  ticket: string;
  expires_at: string;
}

export interface ProviderStatus {
  configured: boolean;
  provider: string | null;
  model: string | null;
  updated_at: string | null;
}

export interface ProviderTestResult {
  ok: boolean;
  provider: string;
  model: string;
  latency_ms: number;
  reply_preview: string;
}

export interface InvitationCreated {
  invitation_id: string;
  invitation: string;
  secret: string;
  expires_at: string;
}

export interface InvitationRow {
  invitation_id: string;
  issuer_agent_id: string;
  expires_at: string;
  used: boolean;
  revoked: boolean;
}

export interface InvitationInfo {
  agent_id: string;
  public_key: string;
  fingerprint: string;
  expires_at: string;
}

export type PairingStatus = "pending" | "completed" | "rejected" | "expired";

export interface Pairing {
  pairing_id: string;
  local_agent_id: string;
  remote_agent_id: string;
  status: PairingStatus;
  invitation_id: string | null;
  request_id: string | null;
}

export interface PairingCompletion {
  pairing_id: string;
  approved: boolean;
  message_id: string;
  ack: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
}

/** WebSocket event envelope from backend/api/events.py AppEvent.to_dict. */
export interface AppEvent {
  event_id: string;
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export const EVENT_TYPES = [
  "conversation.turn.completed",
  "remote.request.created",
  "remote.request.approval_required",
  "remote.response.received",
  "remote.request.updated",
  "execution.updated",
  "remote.pairing.updated",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];
