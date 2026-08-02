import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  webServer: {
    command:
      "cd .. && (test -f /tmp/topoforge-playwright-input-v0.10.0.tif || " +
      "uv run topoforge synthetic --output /tmp/topoforge-playwright-input-v0.10.0.tif " +
      "--terrain saddle --rows 12 --columns 16 --pixel-size-m 20) && " +
      "uv run topoforge web --host 127.0.0.1 --port 8771 " +
      "--state-dir /tmp/topoforge-playwright-state " +
      "--workspace-root /tmp/topoforge-playwright-workspaces --input-root /tmp --no-open",
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
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 960 } },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
