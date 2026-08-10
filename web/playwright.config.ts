import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  webServer: {
    command: "uv run python scripts/run_playwright_server.py --port 8771",
    cwd: "..",
    url: "http://127.0.0.1:8771/api/v1/health",
    reuseExistingServer: true,
    timeout: 120_000,
  },
  use: {
    baseURL: process.env.TOPOFORGE_WEB_URL ?? "http://127.0.0.1:8771",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 960 },
        deviceScaleFactor: 1.5,
      },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
