import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { describeError } from "../../lib/api/client";
import { executeRequest } from "../../lib/api/endpoints";
import { useRequestDetail } from "./useRequests";
import { canExecute, formatTime, statusLabel, statusTone } from "./status";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorState, Skeleton, Spinner, StatusBadge } from "../../components/ui/Feedback";
import { PageHeader } from "../../components/ui/PageHeader";
import { useToast } from "../../components/ui/Toast";

/** Request detail: persisted participants, task, response or failure as
 *  the API exposes it, plus a retry-execution action for the approved
 *  but unexecuted state. */
export function RequestDetailPage() {
  const { id = "" } = useParams();
  const detail = useRequestDetail(id);
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [executing, setExecuting] = useState(false);
  const [executeError, setExecuteError] = useState<string | null>(null);

  const retryExecution = async () => {
    if (executing || !detail.item || !canExecute(detail.item.status)) return;
    setExecuting(true);
    setExecuteError(null);
    try {
      const outcome = await executeRequest(baseUrl, token, detail.item.request_id);
      detail.reload();
      if (outcome.status === "IN_PROGRESS") {
        notify("Execution is already running. Waiting for updates.", "info");
      } else if (outcome.status === "COMPLETED") {
        notify("Execution completed.", "success");
      } else {
        notify(`Execution ended with status ${outcome.status}.`, "info");
      }
    } catch (error) {
      setExecuteError(describeError(error));
    } finally {
      setExecuting(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
      <Link
        to="/requests"
        className="inline-flex min-h-[44px] items-center gap-1.5 text-sm text-ink-300 hover:text-ink-50"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        Back to requests
      </Link>
      <div className="mt-2">
        <PageHeader title="Request detail" />
      </div>

      {detail.detailState === "loading" ? (
        <div className="flex flex-col gap-3" role="status">
          <span className="sr-only">Loading request</span>
          <Skeleton className="h-32" />
          <Skeleton className="h-24" />
        </div>
      ) : null}
      {detail.detailState === "error" ? (
        <ErrorState
          title="Request not found"
          body={detail.detailError ?? "It may have been removed, or you may lack access."}
          onRetry={detail.reload}
        />
      ) : null}
      {detail.detailState === "ready" && !detail.item ? (
        <EmptyState icon="search" title="Nothing here" body="This request does not exist." />
      ) : null}

      {detail.item ? (
        <article className="flex flex-col gap-5 rounded-card border border-graphite-700 bg-graphite-900 p-5 sm:p-6">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="break-all font-mono text-sm font-semibold text-ink-50">
              {detail.item.request_id}
            </h2>
            <span className="ml-auto">
              <StatusBadge tone={statusTone(detail.item.status)} label={statusLabel(detail.item.status)} />
            </span>
          </div>

          <section aria-label="Task">
            <h3 className="text-[13px] font-medium text-ink-500">Task</h3>
            <p className="mt-1 whitespace-pre-wrap text-[15px] leading-relaxed text-ink-100">
              {detail.item.query}
            </p>
          </section>

          <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-[13px] text-ink-500">From</dt>
              <dd className="mt-0.5 break-all font-mono text-ink-100">{detail.item.sender_agent_id}</dd>
            </div>
            <div>
              <dt className="text-[13px] text-ink-500">To</dt>
              <dd className="mt-0.5 break-all font-mono text-ink-100">{detail.item.recipient_agent_id}</dd>
            </div>
            <div>
              <dt className="text-[13px] text-ink-500">Created</dt>
              <dd className="mt-0.5 text-ink-100">{formatTime(detail.item.created_at)}</dd>
            </div>
            <div>
              <dt className="text-[13px] text-ink-500">Expires</dt>
              <dd className="mt-0.5 text-ink-100">{formatTime(detail.item.expires_at)}</dd>
            </div>
            {detail.item.completed_at ? (
              <div>
                <dt className="text-[13px] text-ink-500">Completed</dt>
                <dd className="mt-0.5 text-ink-100">{formatTime(detail.item.completed_at)}</dd>
              </div>
            ) : null}
            {detail.item.response_message_id ? (
              <div>
                <dt className="text-[13px] text-ink-500">Response message</dt>
                <dd className="mt-0.5 break-all font-mono text-ink-100">{detail.item.response_message_id}</dd>
              </div>
            ) : null}
          </dl>

          {detail.item.answer ? (
            <section aria-label="Result">
              <h3 className="text-[13px] font-medium text-ink-500">Result</h3>
              <p className="mt-1 whitespace-pre-wrap text-[15px] leading-relaxed text-ink-100">
                {detail.item.answer}
              </p>
            </section>
          ) : null}
          {detail.item.failure_code ?? detail.item.failure_message ? (
            <section aria-label="Failure">
              <h3 className="text-[13px] font-medium text-ink-500">Failure</h3>
              <p className="mt-1 text-sm text-signal-red">
                {[detail.item.failure_code, detail.item.failure_message]
                  .filter(Boolean)
                  .join(": ")}
              </p>
            </section>
          ) : null}

          {canExecute(detail.item.status) ? (
            <div className="flex flex-col gap-2 border-t border-graphite-800 pt-4">
              <p className="text-sm text-ink-300">
                Approved but execution has not finished. Retry calls only the
                execution endpoint; approval is never repeated.
              </p>
              {executeError ? (
                <p role="alert" className="text-sm text-signal-red">{executeError}</p>
              ) : null}
              <div>
                <Button variant="primary" size="sm" onClick={() => void retryExecution()} busy={executing}>
                  {executing ? <Spinner label="Executing" /> : "Retry execution"}
                </Button>
              </div>
            </div>
          ) : null}
        </article>
      ) : null}
    </div>
  );
}
