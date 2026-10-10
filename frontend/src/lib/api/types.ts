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
] as const;

export type EventType = (typeof EVENT_TYPES)[number];
