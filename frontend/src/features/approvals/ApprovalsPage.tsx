import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../lib/auth/AuthProvider";
import { ApiError, describeError } from "../../lib/api/client";
import { approveRequest, executeRequest, rejectRequest } from "../../lib/api/endpoints";
import type { RemoteRequest } from "../../lib/api/types";
import { usePendingApprovals } from "../requests/useRequests";
import { formatTime } from "../requests/status";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorState, Skeleton, Spinner, StatusBadge } from "../../components/ui/Feedback";
import { PageHeader } from "../../components/ui/PageHeader";
import { useToast } from "../../components/ui/Toast";

type CardPhase =
  | { kind: "idle" }
  | { kind: "working"; action: "Approving" | "Rejecting" | "Executing" };

export interface DecisionNotice {
  requestId: string;
  kind: "approved-pending" | "failed";
  error: string;
}

function ApprovalCard({
  item,
  onChanged,
  onNotice,
}: {
  item: RemoteRequest;
  onChanged: () => void;
  onNotice: (notice: DecisionNotice) => void;
}) {
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [phase, setPhase] = useState<CardPhase>({ kind: "idle" });
  const [confirmingReject, setConfirmingReject] = useState(false);
  const busy = phase.kind === "working";

  const runExecution = async (): Promise<boolean> => {
    setPhase({ kind: "working", action: "Executing" });
    try {
      const outcome = await executeRequest(baseUrl, token, item.request_id);
      onChanged();
      if (outcome.status === "IN_PROGRESS") {
        notify("Execution is already running. Waiting for updates.", "info");
      } else if (outcome.status === "COMPLETED") {
        notify("Request approved and executed.", "success");
      } else {
        notify(`Approved. Execution ended with status ${outcome.status}.`, "info");
      }
      setPhase({ kind: "idle" });
      return true;
    } catch (error) {
      const message = describeError(error);
      // Approval already succeeded: lift a persistent notice (the card
      // unmounts when the reload drops the decided item) and offer an
      // execution-only retry.
      onChanged();
      onNotice({ requestId: item.request_id, kind: "approved-pending", error: message });
      setPhase({ kind: "idle" });
      return false;
    }
  };

  const approveAndRun = async () => {
    if (busy) return;
    setPhase({ kind: "working", action: "Approving" });
    try {
      await approveRequest(baseUrl, token, item.request_id);
    } catch (error) {
      const message = describeError(error);
      onChanged();
      if (error instanceof ApiError && error.isConflict) {
        // Someone else decided first: refresh to the true state.
        onNotice({
          requestId: item.request_id,
          kind: "failed",
          error: `Already decided by someone else: ${message}`,
        });
      } else {
        onNotice({ requestId: item.request_id, kind: "failed", error: message });
      }
      setPhase({ kind: "idle" });
      return;
    }
    await runExecution();
  };

  const reject = async () => {
    if (busy) return;
    setPhase({ kind: "working", action: "Rejecting" });
    try {
      await rejectRequest(baseUrl, token, item.request_id);
      notify("Request rejected.", "info");
      onChanged();
      setPhase({ kind: "idle" });
      setConfirmingReject(false);
    } catch (error) {
      onChanged();
      onNotice({ requestId: item.request_id, kind: "failed", error: describeError(error) });
      setPhase({ kind: "idle" });
      setConfirmingReject(false);
    }
  };

  return (
    <li className="rounded-card border border-graphite-700 bg-graphite-900 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-medium text-ink-50">
          {item.request_id.length > 24 ? `${item.request_id.slice(0, 16)}...` : item.request_id}
        </span>
        <span className="ml-auto">
          <StatusBadge tone="pending" label="Waiting approval" />
        </span>
      </div>
      <p className="mt-1 text-xs text-ink-500">
        From <span className="font-mono">{item.sender_agent_id}</span> · {formatTime(item.created_at)}
      </p>
      <p className="mt-3 whitespace-pre-wrap text-[15px] leading-relaxed text-ink-100">
        {item.query}
      </p>

      <div className="mt-4 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        {confirmingReject ? (
          <>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirmingReject(false)}>
              Keep request
            </Button>
            <Button variant="danger" size="sm" busy={busy} onClick={() => void reject()}>
              {busy ? <Spinner label="Rejecting" /> : "Confirm reject"}
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => setConfirmingReject(true)}
            >
              Reject
            </Button>
            <Button variant="primary" size="sm" busy={busy} onClick={() => void approveAndRun()}>
              {busy ? <Spinner label="Working" /> : "Approve & run"}
            </Button>
          </>
        )}
      </div>
      <p className="mt-3 text-xs text-ink-600">
        <Link to={`/requests/${encodeURIComponent(item.request_id)}`} className="text-accent-300 hover:text-accent-400">
          Inspect request details
        </Link>
      </p>
    </li>
  );
}

/** Focused approval inbox. Approve & Run sequences the two backend
 *  operations and reports the real persisted outcome of each. Decision
 *  notices live at page level so they survive the inbox reload that
 *  removes decided items. */
export function ApprovalsPage() {
  const pending = usePendingApprovals();
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [notices, setNotices] = useState<DecisionNotice[]>([]);
  const [retrying, setRetrying] = useState<string | null>(null);

  const addNotice = useCallback((notice: DecisionNotice) => {
    setNotices((current) => {
      const withoutSame = current.filter(
        (entry) =>
          !(entry.requestId === notice.requestId && entry.kind === notice.kind),
      );
      return [...withoutSame, notice];
    });
  }, []);

  const dismissNotice = useCallback((requestId: string) => {
    setNotices((current) => current.filter((entry) => entry.requestId !== requestId));
  }, []);

  // Notices for items that reappear as pending are stale: the decision
  // did not stick, so the inbox card takes over again.
  const visibleNotices = notices.filter((notice) =>
    notice.kind === "failed"
      ? true
      : !pending.items.some((item) => item.request_id === notice.requestId),
  );

  const retryExecution = useCallback(
    async (requestId: string) => {
      if (retrying) return;
      setRetrying(requestId);
      try {
        const outcome = await executeRequest(baseUrl, token, requestId);
        pending.reload();
        if (outcome.status === "IN_PROGRESS") {
          notify("Execution is already running. Waiting for updates.", "info");
        } else if (outcome.status === "COMPLETED") {
          notify("Execution completed.", "success");
        } else {
          notify(`Execution ended with status ${outcome.status}.`, "info");
        }
        dismissNotice(requestId);
      } catch (error) {
        addNotice({ requestId, kind: "approved-pending", error: describeError(error) });
      } finally {
        setRetrying(null);
      }
    },
    [baseUrl, token, pending, notify, dismissNotice, addNotice, retrying],
  );

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
      <PageHeader
        title="Approvals"
        subtitle="Incoming requests waiting for your decision. Nothing here runs until you approve it."
      />
      {visibleNotices.map((notice) =>
        notice.kind === "approved-pending" ? (
          <div
            key={notice.requestId}
            className="mb-4 rounded-card border border-signal-amber/40 bg-graphite-900 p-4"
          >
            <p role="status" className="text-sm text-signal-amber">
              {notice.requestId} is approved, execution pending. {notice.error}
            </p>
            <div className="mt-2 flex gap-2">
              <Button
                variant="primary"
                size="sm"
                busy={retrying === notice.requestId}
                onClick={() => void retryExecution(notice.requestId)}
              >
                {retrying === notice.requestId ? (
                  <Spinner label="Retrying" />
                ) : (
                  "Retry execution"
                )}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => dismissNotice(notice.requestId)}>
                Dismiss
              </Button>
            </div>
          </div>
        ) : (
          <p key={notice.requestId} role="alert" className="mb-4 text-sm text-signal-red">
            {notice.error}{" "}
            <button
              type="button"
              onClick={() => dismissNotice(notice.requestId)}
              className="cursor-pointer underline hover:text-ink-100"
            >
              Dismiss
            </button>
          </p>
        ),
      )}
      {pending.pendingState === "loading" ? (
        <div className="flex flex-col gap-3" role="status">
          <span className="sr-only">Loading approvals</span>
          <Skeleton className="h-36" />
          <Skeleton className="h-36" />
        </div>
      ) : null}
      {pending.pendingState === "error" ? (
        <ErrorState
          title="Approvals unavailable"
          body={pending.pendingError ?? "Try again."}
          onRetry={pending.reload}
        />
      ) : null}
      {pending.pendingState === "ready" && pending.items.length === 0 ? (
        <EmptyState
          icon="inbox"
          title="All caught up"
          body="No incoming requests are waiting for approval."
        />
      ) : null}
      <ul className="flex flex-col gap-4">
        {pending.items.map((item) => (
          <ApprovalCard
            key={item.request_id}
            item={item}
            onChanged={pending.reload}
            onNotice={addNotice}
          />
        ))}
      </ul>
    </div>
  );
}
