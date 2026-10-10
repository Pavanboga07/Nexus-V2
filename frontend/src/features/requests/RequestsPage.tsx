import { useState } from "react";
import { useRequestList, type RequestRole } from "./useRequests";
import { RequestRow } from "./RequestRow";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/Feedback";
import { PageHeader } from "../../components/ui/PageHeader";
import { cn } from "../../components/ui/cn";

const roles: Array<{ value: RequestRole; label: string }> = [
  { value: "outbound", label: "Sent by me" },
  { value: "inbound", label: "Sent to me" },
];

/** Outgoing and incoming remote requests with direction filter. */
export function RequestsPage() {
  const [role, setRole] = useState<RequestRole>("outbound");
  const list = useRequestList(role);

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <PageHeader
        title="Requests"
        subtitle="Remote work between your agents and connected peers, with the actual persisted status."
      />
      <div role="tablist" aria-label="Request direction" className="mb-4 flex gap-1 rounded-control border border-graphite-700 bg-graphite-900 p-1">
        {roles.map((option) => (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={role === option.value}
            onClick={() => setRole(option.value)}
            className={cn(
              "min-h-[40px] flex-1 cursor-pointer rounded-[6px] px-3 text-sm font-medium transition-colors",
              role === option.value
                ? "bg-graphite-700 text-ink-50"
                : "text-ink-500 hover:text-ink-100",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>

      {list.listState === "loading" ? (
        <div className="flex flex-col gap-3" role="status">
          <span className="sr-only">Loading requests</span>
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      ) : null}
      {list.listState === "error" ? (
        <ErrorState
          title="Requests unavailable"
          body={list.listError ?? "Try again."}
          onRetry={list.reload}
        />
      ) : null}
      {list.listState === "ready" && list.items.length === 0 ? (
        <EmptyState
          icon="inbox"
          title={role === "outbound" ? "No outgoing requests" : "No incoming requests"}
          body={
            role === "outbound"
              ? "Ask a connected agent from any chat to create your first remote request."
              : "Incoming remote work from peers will appear here."
          }
        />
      ) : null}
      <ul className="flex flex-col gap-3">
        {list.items.map((item) => (
          <RequestRow key={item.request_id} item={item} />
        ))}
      </ul>
    </div>
  );
}
