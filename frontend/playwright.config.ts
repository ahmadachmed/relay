import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:8010",
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command:
      "python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8010",
    cwd: "..",
    url: "http://127.0.0.1:8010/ui/",
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
    env: { RELAY_DATA: "/tmp/relay-ui-e2e" },
  },
});
