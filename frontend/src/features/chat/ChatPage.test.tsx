import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../../lib/auth/AuthProvider";
import { ToastProvider } from "../../components/ui/Toast";
import { EventProvider } from "../../app/EventBus";
import { ChatPage } from "./ChatPage";
import { seedAuth, stubFetch } from "../../test/helpers";

function renderChat(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <ToastProvider>
          <EventProvider>
            <Routes>
              <Route path="/chats" element={<ChatPage />} />
              <Route path="/chats/:id" element={<ChatPage />} />
            </Routes>
          </EventProvider>
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const AGENTS = [{ agent_id: "local-1", active: true, owned: true, public_key: "k", key_algorithm: "ed25519" }];

function conversation(id: string) {
  return { id, agent_id: "local-1", title: null, created_at: "2026-10-10T00:00:00Z" };
}

describe("ChatPage", () => {
  it("lists conversations and opens a thread with real history", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/conversations": { body: [conversation("conv-1")] },
      "GET /api/v1/conversations/conv-1/messages": {
        body: [
          { id: 1, role: "user", content: "Hello", created_at: "2026-10-10T00:00:00Z" },
          { id: 2, role: "agent", content: "Hi there", created_at: "2026-10-10T00:00:01Z" },
        ],
      },
    });
    const user = userEvent.setup();
    renderChat("/chats/conv-1");
    expect(await screen.findByText("Hello")).toBeInTheDocument();
    expect(await screen.findByText("Hi there")).toBeInTheDocument();
    // History loads from the messages endpoint.
    await waitFor(() => {
      expect(
        calls.some((call) => call.url.includes("/conversations/conv-1/messages")),
      ).toBe(true);
    });
    // Local chat never touches the remote-request endpoint.
    expect(calls.some((call) => call.url.includes("/api/v1/requests"))).toBe(false);
    expect(user).toBeTruthy();
  });

  it("sends a turn through the turns endpoint and refreshes history", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/conversations": { body: [conversation("conv-1")] },
      "GET /api/v1/conversations/conv-1/messages": { body: [] },
      "POST /api/v1/conversations/conv-1/turns": {
        body: {
          conversation_id: "conv-1",
          user_message_id: 1,
          assistant_message_id: 2,
          answer: "api answer",
          model: "test",
        },
      },
    });
    const user = userEvent.setup();
    renderChat("/chats/conv-1");
    await screen.findByLabelText(/message your assistant/i);
    await user.type(screen.getByLabelText(/message your assistant/i), "Ping");
    await user.click(screen.getByRole("button", { name: /send message/i }));
    await waitFor(() => {
      const turns = calls.filter((call) => call.url.includes("/turns"));
      expect(turns).toHaveLength(1);
    });
    const turn = calls.find((call) => call.url.includes("/turns"));
    expect(turn?.body).toEqual({ message: "Ping" });
  });

  it("shows the empty state when no conversations exist", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/conversations": { body: [] },
    });
    renderChat("/chats");
    expect(await screen.findByText(/no conversations yet/i)).toBeInTheDocument();
  });

  it("renders a recoverable error when the model is unconfigured", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/conversations": {
        status: 503,
        body: { code: "MODEL_NOT_CONFIGURED", message: "no language model is configured" },
      },
    });
    renderChat("/chats");
    expect(await screen.findByText(/no language model is configured/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
  });

  it("sends a remote request from the panel without touching the chat draft", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": {
        body: [{ peer_agent_id: "remote-1", status: "connected" }],
      },
      "GET /api/v1/conversations": { body: [conversation("conv-1")] },
      "GET /api/v1/conversations/conv-1/messages": { body: [] },
      "POST /api/v1/requests": {
        status: 201,
        body: { request_id: "req-panel-1", status: "QUEUED" },
      },
    });
    const user = userEvent.setup();
    renderChat("/chats/conv-1");
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));

    // The panel's Send button must belong to the panel's own form, not
    // the surrounding chat form: nested forms silently misroute the
    // submit (an enabled Send that fires nothing).
    const send = await screen.findByRole("button", { name: /send request/i });
    const owningForm = send.closest("form");
    expect(owningForm).not.toBeNull();
    expect(owningForm!.querySelector("textarea")).toHaveAttribute(
      "placeholder", "Describe the task for the remote agent");
    expect(owningForm!.textContent).not.toMatch(/message your assistant/i);

    await user.selectOptions(
      screen.getByLabelText(/connected agent/i), "remote-1");
    await user.type(
      screen.getByPlaceholderText(/describe the task/i), "Do the thing");
    await user.click(send);
    await waitFor(() => {
      const posts = calls.filter(
        (call) => call.method === "POST" && call.url.endsWith("/api/v1/requests"));
      expect(posts).toHaveLength(1);
    });
    const post = calls.find(
      (call) => call.method === "POST" && call.url.endsWith("/api/v1/requests"));
    expect(post?.body).toEqual({
      sender_agent_id: "local-1",
      recipient_agent_id: "remote-1",
      query: "Do the thing",
    });
    // No local turn fires as a side effect of the remote send.
    expect(calls.some((call) => call.url.includes("/turns"))).toBe(false);
    await waitFor(() => {
      const confirmations = screen.getAllByRole("status");
      expect(confirmations.some((node) =>
        node.textContent?.includes("req-panel-1"))).toBe(true);
    });
  });
});
