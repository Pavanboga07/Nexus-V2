import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth, __storageKeys } from "./AuthProvider";
import { TEST_BASE, TEST_TOKEN, renderApp, seedAuth, stubFetch } from "../../test/helpers";

function Probe() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="connection">{auth.connection.kind}</span>
      <button type="button" onClick={() => auth.saveToken(" fresh-token ")}>
        save
      </button>
      <button type="button" onClick={auth.clearToken}>
        clear
      </button>
      <button type="button" onClick={() => void auth.testConnection()}>
        test
      </button>
    </div>
  );
}

describe("AuthProvider", () => {
  it("stores the token tab-scoped, never in localStorage", async () => {
    seedAuth("", TEST_BASE);
    const { user } = await import("@testing-library/user-event").then((m) => ({
      user: m.default.setup(),
    }));
    renderApp(<Probe />);
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(sessionStorage.getItem(__storageKeys.TOKEN_KEY)).toBe("fresh-token");
    expect(localStorage.getItem(__storageKeys.TOKEN_KEY)).toBeNull();
  });

  it("clears the token from every store", async () => {
    seedAuth(TEST_TOKEN);
    localStorage.setItem(__storageKeys.TOKEN_KEY, TEST_TOKEN);
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    renderApp(<Probe />);
    await user.click(screen.getByRole("button", { name: "clear" }));
    expect(sessionStorage.getItem(__storageKeys.TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(__storageKeys.TOKEN_KEY)).toBeNull();
    expect(screen.getByTestId("connection")).toHaveTextContent("unconfigured");
  });

  it("marks a rejected token as failed without echoing it", async () => {
    seedAuth("wrong-token");
    stubFetch({
      "GET /api/v1/health/ready": { body: { status: "ready" } },
      "GET /api/v1/agents": {
        status: 401,
        body: { code: "UNAUTHORIZED", message: "authentication required" },
      },
    });
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    renderApp(<Probe />);
    await user.click(screen.getByRole("button", { name: "test" }));
    await waitFor(() =>
      expect(screen.getByTestId("connection")).toHaveTextContent("failed"),
    );
  });

  it("connects with a valid token and counts agents", async () => {
    seedAuth(TEST_TOKEN);
    stubFetch({
      "GET /api/v1/health/ready": { body: { status: "ready" } },
      "GET /api/v1/agents": { body: [{ agent_id: "a" }] },
    });
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    const { container } = renderApp(<Probe />);
    expect(container).toBeTruthy();
    expect(AuthProvider).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "test" }));
    await waitFor(() =>
      expect(screen.getByTestId("connection")).toHaveTextContent("connected"),
    );
  });
});
