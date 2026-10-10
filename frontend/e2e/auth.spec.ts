import { expect, test } from "@playwright/test";
import { E2E_TOKEN } from "../playwright.config";
import { signIn, signOut } from "./helpers";

test("first-run gate connects with a valid token", async ({ page }) => {
  await signIn(page);
  // Token lives in session storage, never local storage.
  const sessionToken = await page.evaluate(() => sessionStorage.getItem("nexus.api.token"));
  expect(sessionToken).toBe(E2E_TOKEN);
  const localToken = await page.evaluate(() => localStorage.getItem("nexus.api.token"));
  expect(localToken).toBeNull();
  await signOut(page);
  const cleared = await page.evaluate(() => sessionStorage.getItem("nexus.api.token"));
  expect(cleared).toBeNull();
});

test("wrong token shows a connection failure, not a crash", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel(/api bearer token/i).fill("wrong-token");
  await page.getByRole("button", { name: /^connect$/i }).click();
  await expect(page.getByText(/connection failed/i).first()).toBeVisible({ timeout: 15000 });
  // Still on the gate (the URL may retain /chats from the checking phase,
  // but the connection gate is what renders).
  await expect(page.getByRole("heading", { name: /connect to nexus/i })).toBeVisible();
});

test("no token or ticket ever appears in a URL", async ({ page }) => {
  const seen: string[] = [];
  page.on("request", (request) => seen.push(request.url()));
  await signIn(page);
  await page.getByRole("link", { name: /^agents$/i }).click();
  await expect(page.getByRole("heading", { name: /^agents$/i })).toBeVisible();
  for (const url of seen) {
    expect(url).not.toContain(E2E_TOKEN);
    expect(url).not.toContain("ticket=");
  }
});
