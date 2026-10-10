import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RemoteRequestPanel } from "./RemoteRequestPanel";
import { renderApp, seedAuth, stubFetch, TEST_TOKEN } from "../../test/helpers";

const AGENTS = [{ agent_id: "local-1", active: true, owned: true, public_key: "k", key_algorithm: "ed25519" }];
const PEERS = [{ peer_agent_id: "remote-1", status: "connected" }];

function createdRequest() {
  return {
    request_id: "req-1",
    sender_agent_id: "local-1",
    recipient_agent_id: "remote-1",
    query: "Do the thing",
    requested_capabilities: [],
    status: "QUEUED",
    created_at: "2026-10-10T00:00:00Z",
    expires_at: "2026-10-10T01:00:00Z",
    completed_at: null,
    failure_code: null,
    failure_message: null,
    response_message_id: null,
    answer: null,
  };
}

describe("RemoteRequestPanel", () => {
  it("submits only to the remote-request endpoint and shows the real id", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": { body: PEERS },
      "POST /api/v1/requests": { status: 201, body: createdRequest() },
    });
    const user = userEvent.setup();
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);

    await user.selectOptions(await screen.findByLabelText("Connected agent"), "remote-1");
    await user.type(screen.getByPlaceholderText(/describe the task/i), "Do the thing");
    await user.click(screen.getByRole("button", { name: /send request/i }));

    await waitFor(() => {
      expect(
        calls.filter((call) => call.method === "POST" && call.url.endsWith("/api/v1/requests")),
      ).toHaveLength(1);
    });
    const post = calls.find(
      (call) => call.method === "POST" && call.url.endsWith("/api/v1/requests"),
    );
    expect(post?.body).toMatchObject({
      sender_agent_id: "local-1",
      recipient_agent_id: "remote-1",
      query: "Do the thing",
    });
    // Never touches the local conversation endpoint.
    expect(calls.some((call) => call.url.includes("/turns"))).toBe(false);
    expect(calls.some((call) => call.url.includes("/conversations"))).toBe(false);
    expect(
      screen.getByRole("link", { name: /view request/i }),
    ).toHaveAttribute("href", "/requests/req-1");
  });

  it("blocks duplicate submission while the creation call is pending", async () => {
    seedAuth();
    let resolvePost!: (value: Response) => void;
    const gate = new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });
    const calls: Array<{ url: string; method: string }> = [];
    const stub = async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });
      const path = new URL(url, "http://localhost:5173").pathname;
      if (method === "POST" && path === "/api/v1/requests") return gate;
      const body = method === "GET" && path === "/api/v1/agents" ? AGENTS : PEERS;
      return new Response(JSON.stringify(body), { status: 200 });
    };
    const { vi: vitest } = await import("../../test/helpers");
    vitest.stubGlobal("fetch", stub);
    const user = userEvent.setup();
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);
    await user.selectOptions(await screen.findByLabelText("Connected agent"), "remote-1");
    await user.type(screen.getByPlaceholderText(/describe the task/i), "Do it");
    const send = screen.getByRole("button", { name: /send request/i });
    await user.click(send);
    await user.click(send);
    const requestPosts = () =>
      calls.filter((call) => call.method === "POST" && call.url.endsWith("/api/v1/requests"));
    await waitFor(() => {
      expect(requestPosts()).toHaveLength(1);
    });
    resolvePost(
      new Response(JSON.stringify(createdRequest()), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    expect(
      await screen.findByRole("link", { name: /view request/i }),
    ).toHaveAttribute("href", "/requests/req-1");
  });

  it("preserves the draft on failure and shows a safe error", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": { body: PEERS },
      "POST /api/v1/requests": {
        status: 409,
        body: { code: "ALREADY_EXISTS", message: "identifier already exists" },
      },
    });
    const user = userEvent.setup();
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);
    await user.selectOptions(await screen.findByLabelText("Connected agent"), "remote-1");
    await user.type(screen.getByPlaceholderText(/describe the task/i), "Keep me");
    await user.click(screen.getByRole("button", { name: /send request/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("identifier already exists");
    expect(screen.getByDisplayValue("Keep me")).toBeInTheDocument();
  });

  it("offers navigation to Agents when no peer is available", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": { body: [] },
    });
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);
    expect(await screen.findByText(/no connected agent/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /pair an agent first/i })).toHaveAttribute("href", "/agents");
  });

  it("excludes pending peers from the recipient list", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": {
        body: [{ peer_agent_id: "remote-pending", status: "pending" }],
      },
    });
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);
    // A pending-only network is equivalent to no connected agent.
    expect(await screen.findByText(/no connected agent/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /pair an agent first/i }),
    ).toHaveAttribute("href", "/agents");
  });

  it("never places the token in a URL", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": { body: PEERS },
    });
    renderApp(<RemoteRequestPanel onCollapse={() => {}} />);
    await screen.findByLabelText("Connected agent");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    for (const call of calls) {
      expect(call.url).not.toContain(TEST_TOKEN);
    }
  });
});
