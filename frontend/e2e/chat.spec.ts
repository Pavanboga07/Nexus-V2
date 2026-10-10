import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";

test.setTimeout(90000);

/** The E2E backend has no language model configured, so the conversation
 *  endpoints honestly report MODEL_NOT_CONFIGURED. This exercises the
 *  real degraded path: no crash, a clear message, and a working retry.
 *  Turn success paths are covered by mocked unit tests. */
test("chat surfaces the unconfigured model honestly", async ({ page }) => {
  await signIn(page);
  await expect(
    page.getByText(/no language model is configured/i).first(),
  ).toBeVisible({ timeout: 15000 });
  const retry = page.getByRole("button", { name: /^retry$/i }).first();
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(
    page.getByText(/no language model is configured/i).first(),
  ).toBeVisible({ timeout: 15000 });
});

test("remote request created via API appears in the requests list", async ({ page }) => {
  await signIn(page);
  const createdId = await page.evaluate(async () => {
    const token = sessionStorage.getItem("nexus.api.token") ?? "";
    const agents = (await (
      await fetch("/api/v1/agents", {
        headers: { Authorization: `Bearer ${token}` },
      })
    ).json()) as Array<{ agent_id: string }>;
    const peers = (await (
      await fetch(`/api/v1/agents/${agents[0].agent_id}/peers`, {
        headers: { Authorization: `Bearer ${token}` },
      })
    ).json()) as Array<{ peer_agent_id: string }>;
    const response = await fetch("/api/v1/requests", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        sender_agent_id: agents[0].agent_id,
        recipient_agent_id: peers[0].peer_agent_id,
        query: `E2E list probe ${Date.now()}`,
      }),
    });
    const body = (await response.json()) as { request_id: string };
    return body.request_id;
  });

  await page.getByRole("link", { name: /^requests$/i }).click();
  await expect(page.getByRole("link", { name: new RegExp(createdId) }).first()).toBeVisible({
    timeout: 15000,
  });
  await page.getByRole("link", { name: new RegExp(createdId) }).first().click();
  await expect(page.getByRole("heading", { name: /request detail/i })).toBeVisible({
    timeout: 15000,
  });
});
