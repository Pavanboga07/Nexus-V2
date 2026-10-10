import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConnectAgentPanel } from "./ConnectAgentPanel";
import { renderApp, seedAuth, stubFetch } from "../../test/helpers";

const AGENTS = [
  { agent_id: "local-1", active: true, owned: true, public_key: "k", key_algorithm: "ed25519" },
];

describe("ConnectAgentPanel", () => {
  it("starts on the invite tab with no code shown", () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/pairings/invitations": { body: [] },
      "GET /api/v1/pairings": { body: [] },
    });
    renderApp(<ConnectAgentPanel agents={AGENTS} onChanged={() => {}} />);
    expect(
      screen.getByRole("tab", { name: /invite a partner/i, selected: true }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/invitation code/i)).not.toBeInTheDocument();
  });

  it("creates an invitation and shows the one-time code", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/pairings/invitations": { body: [] },
      "GET /api/v1/pairings": { body: [] },
      "POST /api/v1/pairings/invitations": {
        status: 201,
        body: {
          invitation_id: "inv-1",
          invitation: "inv.code.secret",
          secret: "secret",
          expires_at: "2026-10-10T01:00:00Z",
        },
      },
    });
    const user = userEvent.setup();
    renderApp(<ConnectAgentPanel agents={AGENTS} onChanged={() => {}} />);
    await user.click(screen.getByRole("button", { name: /create invitation/i }));
    expect(await screen.findByLabelText(/invitation code/i)).toHaveValue(
      "inv.code.secret",
    );
  });

  it("validates an invitation and shows the identity preview", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/pairings/invitations": { body: [] },
      "GET /api/v1/pairings": { body: [] },
      "POST /api/v1/pairings/validate": {
        body: {
          agent_id: "remote-9",
          public_key: "abc",
          fingerprint: "AB12-CD34-EF56-7890",
          expires_at: "2026-10-10T01:00:00Z",
        },
      },
    });
    const user = userEvent.setup();
    renderApp(<ConnectAgentPanel agents={AGENTS} onChanged={() => {}} />);
    await user.click(screen.getByRole("tab", { name: /accept invitation/i }));
    await user.type(screen.getByPlaceholderText(/inv\./i), "inv.code.secret");
    await user.click(screen.getByRole("button", { name: /validate invitation/i }));
    expect(await screen.findByText("remote-9")).toBeInTheDocument();
    expect(screen.getByText("AB12-CD34-EF56-7890")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /send pairing request/i }),
    ).toBeInTheDocument();
  });

  it("rejects a bad invitation with a safe error and no preview", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/pairings/invitations": { body: [] },
      "GET /api/v1/pairings": { body: [] },
      "POST /api/v1/pairings/validate": {
        status: 422,
        body: { code: "PAIRING_REJECTED", message: "the pairing material is invalid" },
      },
    });
    const user = userEvent.setup();
    renderApp(<ConnectAgentPanel agents={AGENTS} onChanged={() => {}} />);
    await user.click(screen.getByRole("tab", { name: /accept invitation/i }));
    await user.type(screen.getByPlaceholderText(/inv\./i), "garbage");
    await user.click(screen.getByRole("button", { name: /validate invitation/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the pairing material is invalid",
    );
    expect(
      screen.queryByRole("button", { name: /send pairing request/i }),
    ).not.toBeInTheDocument();
  });

  it("accepts after validation and links to the pairing request", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/pairings/invitations": { body: [] },
      "GET /api/v1/pairings": {
        body: [
          {
            pairing_id: "pair-1",
            local_agent_id: "local-1",
            remote_agent_id: "remote-9",
            status: "pending",
            invitation_id: "inv-1",
            request_id: "req-7",
          },
        ],
      },
      "POST /api/v1/pairings/validate": {
        body: {
          agent_id: "remote-9",
          public_key: "abc",
          fingerprint: "AB12-CD34-EF56-7890",
          expires_at: "2026-10-10T01:00:00Z",
        },
      },
      "POST /api/v1/pairings/accept": {
        status: 201,
        body: {
          pairing_id: "pair-1",
          local_agent_id: "local-1",
          remote_agent_id: "remote-9",
          status: "pending",
          invitation_id: "inv-1",
          request_id: "req-7",
        },
      },
    });
    const user = userEvent.setup();
    renderApp(<ConnectAgentPanel agents={AGENTS} onChanged={() => {}} />);
    await user.click(screen.getByRole("tab", { name: /accept invitation/i }));
    await user.type(screen.getByPlaceholderText(/inv\./i), "inv.code.secret");
    await user.click(screen.getByRole("button", { name: /validate invitation/i }));
    await user.click(
      await screen.findByRole("button", { name: /send pairing request/i }),
    );
    await waitFor(() => {
      expect(screen.getByRole("link", { name: /view request/i })).toHaveAttribute(
        "href",
        "/requests/req-7",
      );
    });
  });
});
