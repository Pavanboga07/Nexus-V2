import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalsPage } from "./ApprovalsPage";
import { renderApp, seedAuth, stubFetch } from "../../test/helpers";

function pendingItem() {
  return {
    request_id: "req-9",
    sender_agent_id: "remote-1",
    recipient_agent_id: "local-1",
    query: "Summarize this",
    requested_capabilities: [],
    status: "WAITING_APPROVAL",
    created_at: "2026-10-10T00:00:00Z",
    expires_at: "2026-10-10T01:00:00Z",
    completed_at: null,
    failure_code: null,
    failure_message: null,
    response_message_id: null,
    answer: null,
  };
}

describe("ApprovalsPage", () => {
  it("sequences approval then execution, in order", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/approvals/pending": { body: [pendingItem()] },
      "POST /api/v1/requests/req-9/approve": {
        body: { ...pendingItem(), status: "APPROVED" },
      },
      "POST /api/v1/requests/req-9/execute": {
        body: {
          request_id: "req-9",
          status: "COMPLETED",
          executed: true,
          response_message_id: "msg-1",
          detail: "COMPLETED",
        },
      },
      "GET /api/v1/requests": { body: [] },
    });
    const user = userEvent.setup();
    renderApp(<ApprovalsPage />);
    await user.click(await screen.findByRole("button", { name: /approve & run/i }));

    await waitFor(() => {
      const posts = calls.filter(
        (call) => call.method === "POST" && !call.url.endsWith("/api/v1/events/ticket"),
      );
      expect(posts.map((call) => call.url)).toEqual([
        expect.stringContaining("/api/v1/requests/req-9/approve"),
        expect.stringContaining("/api/v1/requests/req-9/execute"),
      ]);
    });
    expect(await screen.findByText(/approved and executed/i)).toBeInTheDocument();
  });

  it("reports approved-pending when execution fails to start, with execute-only retry", async () => {
    seedAuth();
    let pendingCalls = 0;
    const pendingStub = async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      const path = new URL(url, "http://localhost:5173").pathname;
      const routes: Record<string, { status?: number; body?: unknown }> = {
        "GET /api/v1/approvals/pending": {
          // After the decision lands, the real list no longer contains it.
          body: pendingCalls++ === 0 ? [pendingItem()] : [],
        },
        "POST /api/v1/requests/req-9/approve": {
          body: { ...pendingItem(), status: "APPROVED" },
        },
        "POST /api/v1/requests/req-9/execute": {
          status: 503,
          body: { code: "MODEL_UNAVAILABLE", message: "model invocation failed" },
        },
      };
      const route = routes[`${method} ${path}`];
      const calls = pendingStubCalls;
      calls.push({ url, method, body: null, auth: null });
      return new Response(JSON.stringify(route?.body ?? null), {
        status: route?.status ?? 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const pendingStubCalls: Array<{ url: string; method: string; body: unknown; auth: string | null }> = [];
    const { vi: vitest } = await import("../../test/helpers");
    vitest.stubGlobal("fetch", pendingStub);
    const user = userEvent.setup();
    renderApp(<ApprovalsPage />);
    await user.click(await screen.findByRole("button", { name: /approve & run/i }));
    expect(await screen.findByText(/approved, execution pending/i)).toBeInTheDocument();

    const approveCalls = () =>
      pendingStubCalls.filter((call) => call.url.endsWith("/req-9/approve"));
    const executeCalls = () =>
      pendingStubCalls.filter((call) => call.url.endsWith("/req-9/execute"));
    expect(approveCalls()).toHaveLength(1);
    expect(executeCalls()).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: /retry execution/i }));
    await waitFor(() => expect(executeCalls()).toHaveLength(2));
    // Retry never repeats the approval.
    expect(approveCalls()).toHaveLength(1);
  });

  it("handles a conflicting decision without fabricating success", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/approvals/pending": { body: [pendingItem()] },
      "POST /api/v1/requests/req-9/approve": {
        status: 409,
        body: { code: "ILLEGAL_TRANSITION", message: "operation not allowed in the current state" },
      },
      "GET /api/v1/requests": { body: [] },
    });
    const user = userEvent.setup();
    renderApp(<ApprovalsPage />);
    await user.click(await screen.findByRole("button", { name: /approve & run/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already decided/i);
    expect(screen.queryByText(/approved and executed/i)).not.toBeInTheDocument();
  });

  it("rejects only after explicit confirmation", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/approvals/pending": { body: [pendingItem()] },
      "POST /api/v1/requests/req-9/reject": {
        body: { ...pendingItem(), status: "REJECTED" },
      },
      "GET /api/v1/requests": { body: [] },
    });
    const user = userEvent.setup();
    renderApp(<ApprovalsPage />);
    await user.click(await screen.findByRole("button", { name: /^reject$/i }));
    // First click only arms the confirmation; nothing sent yet.
    expect(calls.filter((call) => call.url.endsWith("/req-9/reject"))).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: /confirm reject/i }));
    await waitFor(() => {
      expect(
        calls.filter((call) => call.url.endsWith("/req-9/reject")),
      ).toHaveLength(1);
    });
    const card = screen.getByRole("listitem");
    expect(within(card).queryByRole("button", { name: /approve & run/i })).not.toBeNull();
  });

  it("shows the cleared inbox state when nothing is pending", async () => {
    seedAuth();
    stubFetch({ "GET /api/v1/approvals/pending": { body: [] } });
    renderApp(<ApprovalsPage />);
    expect(await screen.findByText(/all caught up/i)).toBeInTheDocument();
  });
});
