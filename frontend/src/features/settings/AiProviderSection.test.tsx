import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AiProviderSection } from "./AiProviderSection";
import { renderApp, seedAuth, stubFetch, TEST_TOKEN } from "../../test/helpers";

describe("AiProviderSection", () => {
  it("shows the unconfigured state and saves a configuration", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/settings/ai-provider": { body: { configured: false } },
      "PUT /api/v1/settings/ai-provider": {
        body: {
          configured: true,
          provider: "google_genai",
          model: "gemini-2.0-flash",
          updated_at: "2026-10-10T00:00:00Z",
        },
      },
    });
    const user = userEvent.setup();
    renderApp(<AiProviderSection />);
    expect(await screen.findByText(/no provider configured/i)).toBeInTheDocument();

    await user.type(
      screen.getByLabelText(/^API key$/i),
      "test-key-123",
    );
    await user.click(screen.getByRole("button", { name: /save configuration/i }));
    await waitFor(() => {
      expect(
        calls.filter((call) => call.method === "PUT"),
      ).toHaveLength(1);
    });
    const put = calls.find((call) => call.method === "PUT");
    expect(put?.body).toMatchObject({
      provider: "google_genai",
      model: "gemini-2.0-flash",
      api_key: "test-key-123",
    });
    // Key field cleared after save; status shows configured metadata.
    await waitFor(() => {
      expect(screen.getByLabelText(/replace api key/i)).toHaveValue("");
    });
    expect(await screen.findByText(/configured: google_genai/i)).toBeInTheDocument();
    // No secret in any request URL.
    for (const call of calls) {
      expect(call.url).not.toContain("test-key-123");
    }
    expect(TEST_TOKEN).toBeTruthy();
  });

  it("tests the saved configuration and reports the result", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/settings/ai-provider": {
        body: {
          configured: true,
          provider: "google_genai",
          model: "gemini-2.0-flash",
          updated_at: "2026-10-10T00:00:00Z",
        },
      },
      "POST /api/v1/settings/ai-provider/test": {
        body: {
          ok: true,
          provider: "google_genai",
          model: "gemini-2.0-flash",
          latency_ms: 412,
          reply_preview: "OK",
        },
      },
    });
    const user = userEvent.setup();
    renderApp(<AiProviderSection />);
    await user.click(
      await screen.findByRole("button", { name: /test connection/i }),
    );
    expect(await screen.findByText(/connected to google_genai/i)).toBeInTheDocument();
  });

  it("surfaces backend validation and provider failures safely", async () => {
    seedAuth();
    stubFetch({
      "GET /api/v1/settings/ai-provider": { body: { configured: false } },
      "PUT /api/v1/settings/ai-provider": {
        status: 422,
        body: { code: "INVALID_INPUT", message: "request input is invalid" },
      },
    });
    const user = userEvent.setup();
    renderApp(<AiProviderSection />);
    await user.type(screen.getByLabelText(/^API key$/i), "x");
    await user.click(screen.getByRole("button", { name: /save configuration/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "request input is invalid",
    );
  });

  it("removes the configuration after confirmation", async () => {
    seedAuth();
    const { calls } = stubFetch({
      "GET /api/v1/settings/ai-provider": {
        body: {
          configured: true,
          provider: "google_genai",
          model: "gemini-2.0-flash",
          updated_at: "2026-10-10T00:00:00Z",
        },
      },
      "DELETE /api/v1/settings/ai-provider": { body: { removed: true } },
    });
    const user = userEvent.setup();
    renderApp(<AiProviderSection />);
    await user.click(await screen.findByRole("button", { name: /^remove$/i }));
    await user.click(
      await screen.findByRole("button", { name: /confirm remove/i }),
    );
    await waitFor(() => {
      expect(
        calls.filter((call) => call.method === "DELETE"),
      ).toHaveLength(1);
    });
    expect(await screen.findByText(/no provider configured/i)).toBeInTheDocument();
  });
});
