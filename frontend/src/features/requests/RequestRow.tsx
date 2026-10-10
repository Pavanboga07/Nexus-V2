import { Link } from "react-router-dom";
import type { RemoteRequest } from "../../lib/api/types";
import { StatusBadge } from "../../components/ui/Feedback";
import { formatTime, statusLabel, statusTone } from "./status";

function shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 10)}...${id.slice(-6)}` : id;
}

/** True for pairing handshake requests (never ordinary tasks). */
export function isPairingRequest(item: Pick<RemoteRequest, "requested_capabilities">): boolean {
  return (item.requested_capabilities ?? []).includes("nexus.pair");
}

/** One request row: parties, persisted status, timestamps, result hint. */
export function RequestRow({ item }: { item: RemoteRequest }) {
  const pairing = isPairingRequest(item);
  return (
    <li>
      <Link
        to={`/requests/${encodeURIComponent(item.request_id)}`}
        className="block rounded-card border border-graphite-700 bg-graphite-900 p-4 transition-colors hover:border-ink-600"
        aria-label={`${pairing ? "Pairing request " : "Request "}${item.request_id}`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm font-medium text-ink-50">
            {shortId(item.request_id)}
          </span>
          {pairing ? (
            <StatusBadge tone="neutral" label="Pairing" />
          ) : null}
          <span className="ml-auto">
            <StatusBadge tone={statusTone(item.status)} label={statusLabel(item.status)} />
          </span>
        </div>
        <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-ink-300">
          {item.query}
        </p>
        <p className="mt-2 truncate font-mono text-xs text-ink-600">
          {item.sender_agent_id} → {item.recipient_agent_id} · {formatTime(item.created_at)}
        </p>
      </Link>
    </li>
  );
}
