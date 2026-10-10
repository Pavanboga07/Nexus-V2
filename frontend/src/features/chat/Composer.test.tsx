import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";
import { renderApp, seedAuth, stubFetch, TEST_TOKEN, vi } from "../../test/helpers";

const AGENTS = [{ agent_id: "local-1", active: true, owned: true, public_key: "k", key_algorithm: "ed25519" }];
const PEERS = [{ peer_agent_id: "remote-1", status: "connected" }];

function stubNetwork() {
  return stubFetch({
    "GET /api/v1/agents": { body: AGENTS },
    "GET /api/v1/agents/local-1/peers": { body: PEERS },
  });
}

describe("Composer", () => {
  it("keeps the connected-agent panel collapsed initially", async () => {
    seedAuth();
    stubNetwork();
    renderApp(
      <Composer sending={false} sendError={null} onSend={() => {}} connectedAgentCount={1} />,
    );
    expect(screen.getByRole("button", { name: /ask a connected agent/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByLabelText(/connected agent/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send request/i })).not.toBeInTheDocument();
  });

  it("expands the panel on demand and collapses without side effects", async () => {
    seedAuth();
    const { calls } = stubNetwork();
    const user = userEvent.setup();
    renderApp(
      <Composer sending={false} sendError={null} onSend={() => {}} connectedAgentCount={1} />,
    );
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    expect(await screen.findByLabelText("Connected agent")).toBeInTheDocument();
    // Opening the panel issues no request-creation calls (only the
    // background event-stream ticket fetch, which is unrelated).
    expect(
      calls.filter((call) => call.method === "POST" && call.url.endsWith("/api/v1/requests")),
    ).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    expect(screen.queryByLabelText(/connected agent/i)).not.toBeInTheDocument();
    expect(
      calls.filter((call) => call.method === "POST" && call.url.endsWith("/api/v1/requests")),
    ).toHaveLength(0);
  });

  it("preserves the unsent remote draft across collapse and reopen", async () => {
    seedAuth();
    stubNetwork();
    const user = userEvent.setup();
    renderApp(
      <Composer sending={false} sendError={null} onSend={() => {}} connectedAgentCount={1} />,
    );
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    const task = await screen.findByPlaceholderText(/describe the task/i);
    await user.type(task, "Summarize the quarterly report");
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    expect(await screen.findByDisplayValue("Summarize the quarterly report")).toBeInTheDocument();
  });

  it("sends local chat with Enter and calls only the local turn API", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/agents": { body: AGENTS },
      "GET /api/v1/agents/local-1/peers": { body: PEERS },
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    renderApp(
      <Composer sending={false} sendError={null} onSend={onSend} connectedAgentCount={1} />,
    );
    const box = screen.getByLabelText(/message your assistant/i);
    await user.type(box, "Hello Nexus{enter}");
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("Hello Nexus");
    // Composer delegates to the hook; it performs no API fetch itself
    // (the background event-stream ticket fetch is unrelated).
    expect(
      calls.filter((call) => !call.url.endsWith("/api/v1/events/ticket")),
    ).toHaveLength(0);
    expect(box).toHaveValue("");
  });

  it("disables submit while a turn is pending", () => {
    seedAuth();
    stubNetwork();
    renderApp(
      <Composer sending sendError={null} onSend={() => {}} connectedAgentCount={1} />,
    );
    expect(screen.getByRole("button", { name: /send message/i })).toBeDisabled();
  });

  it("keeps the bearer token out of every request URL", async () => {
    seedAuth();
    const { calls } = stubNetwork();
    const user = userEvent.setup();
    renderApp(
      <Composer sending={false} sendError={null} onSend={() => {}} connectedAgentCount={1} />,
    );
    await user.click(screen.getByRole("button", { name: /ask a connected agent/i }));
    await screen.findByLabelText("Connected agent");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    for (const call of calls) {
      expect(call.url).not.toContain(TEST_TOKEN);
      expect(call.url).not.toContain("ticket=");
    }
  });
});
