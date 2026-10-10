import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";

test("seeded approval is visible and approve works end to end", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: /^approvals$/i }).click();
  await expect(
    page.getByText(/E2E approval probe/i).first(),
  ).toBeVisible({ timeout: 15000 });

  await page.getByRole("button", { name: /approve & run/i }).first().click();
  // No model is configured on the E2E backend, so execution honestly
  // reports pending with a retry; the approval itself must have landed.
  await expect(page.getByText(/approved, execution pending/i)).toBeVisible({ timeout: 15000 });

  // Decided items leave the inbox: verify the persisted status through
  // the requests list instead.
  await page.getByRole("link", { name: /^requests$/i }).click();
  await page.getByRole("tab", { name: /sent to me/i }).click();
  await expect(page.getByText(/E2E approval probe/i).first()).toBeVisible({ timeout: 15000 });
  await page.getByText(/E2E approval probe/i).first().click();
  await expect(page.getByText(/approved/i).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText(/retry execution/i)).toBeVisible();
});

test("request detail shows persisted data and safe failure info", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: /^requests$/i }).click();
  await page.getByRole("tab", { name: /sent to me/i }).click();
  await expect(page.getByText(/E2E approval probe/i).first()).toBeVisible({ timeout: 15000 });
  await page.getByText(/E2E approval probe/i).first().click();
  await expect(page.getByRole("heading", { name: /request detail/i })).toBeVisible({ timeout: 15000 });
  await expect(page.getByText(/E2E approval probe: summarize/i)).toBeVisible();
});
