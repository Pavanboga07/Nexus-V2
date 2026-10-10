import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { signIn } from "./helpers";

test("desktop has no overflow and passes axe", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("link", { name: /^agents$/i }).click();
  await expect(page.getByRole("heading", { name: /^agents$/i })).toBeVisible();

  const overflow = await page.evaluate(
    () =>
      [...document.querySelectorAll("*")]
        .filter(
          (element) =>
            element.getBoundingClientRect().right > document.documentElement.clientWidth + 1,
        )
        .slice(0, 10)
        .map((element) => `${element.tagName}.${element.className}`),
  );
  expect(overflow).toEqual([]);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("mobile chat fits the viewport with a working drawer", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: /open navigation/i }).click();
  await expect(page.getByRole("dialog", { name: /navigation/i })).toBeVisible();
  await page.getByRole("link", { name: /^settings$/i }).click();
  await expect(page.getByRole("heading", { name: /^settings$/i })).toBeVisible();

  const overflow = await page.evaluate(
    () =>
      [...document.querySelectorAll("*")]
        .filter(
          (element) =>
            element.getBoundingClientRect().right > document.documentElement.clientWidth + 1,
        )
        .slice(0, 10)
        .map((element) => `${element.tagName}.${element.className}`),
  );
  expect(overflow).toEqual([]);

  const results = await new AxeBuilder({ page })
    .disableRules(["color-contrast"])
    .analyze();
  expect(results.violations).toEqual([]);
});
