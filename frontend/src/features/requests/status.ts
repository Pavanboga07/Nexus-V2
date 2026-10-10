import type { RequestStatus } from "../../lib/api/types";
import type { StatusTone } from "../../components/ui/Feedback";

export const STATUS_LABELS: Record<RequestStatus, string> = {
  CREATED: "Created",
  QUEUED: "Queued",
  SENT: "Sent",
  DELIVERED: "Delivered",
  WAITING_APPROVAL: "Waiting approval",
  APPROVED: "Approved",
  PROCESSING: "Processing",
  COMPLETED: "Completed",
  REJECTED: "Rejected",
  EXPIRED: "Expired",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

const STATUS_TONES: Record<RequestStatus, StatusTone> = {
  CREATED: "neutral",
  QUEUED: "neutral",
  SENT: "neutral",
  DELIVERED: "neutral",
  WAITING_APPROVAL: "pending",
  APPROVED: "pending",
  PROCESSING: "active",
  COMPLETED: "ok",
  REJECTED: "bad",
  EXPIRED: "bad",
  FAILED: "bad",
  CANCELLED: "neutral",
};

export function statusTone(status: RequestStatus): StatusTone {
  return STATUS_TONES[status];
}

export function statusLabel(status: RequestStatus): string {
  return STATUS_LABELS[status];
}

/** Terminal states: the backend allows no outgoing transitions. */
export function isTerminal(status: RequestStatus): boolean {
  return (
    status === "COMPLETED" ||
    status === "REJECTED" ||
    status === "EXPIRED" ||
    status === "FAILED" ||
    status === "CANCELLED"
  );
}

export function canDecide(status: RequestStatus): boolean {
  return status === "WAITING_APPROVAL";
}

export function canExecute(status: RequestStatus): boolean {
  return status === "APPROVED" || status === "PROCESSING";
}

export function formatTime(value: string | null): string {
  if (value === null) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}
