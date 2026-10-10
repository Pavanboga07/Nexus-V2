import { expect, type Page } from "@playwright/test";
import { E2E_TOKEN } from "../playwright.config";

/** Sign in through the real first-run gate with the E2E backend token. */
export async function signIn(page: Page, token: string = E2E_TOKEN): Promise<void> {
  await page.goto("/");
  const heading = page.getByRole("heading", { name: /connect to nexus/i });
  await expect(heading).toBeVisible();
  await page.getByLabel(/api bearer token/i).fill(token);
  await page.getByRole("button", { name: /^connect$/i }).click();
  await expect(page.getByRole("heading", { name: /^chats$/i })).toBeVisible({ timeout: 15000 });
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByRole("heading", { name: /connect to nexus/i })).toBeVisible();
}
