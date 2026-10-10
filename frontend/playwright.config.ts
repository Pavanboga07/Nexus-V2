import { defineConfig, devices } from "@playwright/test";

export const E2E_TOKEN = process.env.NEXUS_E2E_TOKEN ?? "e2e-frontend-token";

/** Preview serves the production build through the same same-origin
 *  /api proxy as dev, so E2E exercises the real deployment shape. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"] },
      testMatch: /responsive\.spec\.ts/,
    },
  ],
  webServer: {
    command: "npm run preview -- --port 5173 --strictPort --host 127.0.0.1",
    url: "http://127.0.0.1:5173/",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
