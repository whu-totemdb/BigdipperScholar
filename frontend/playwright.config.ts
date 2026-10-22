import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:18197", viewport: { width: 1600, height: 1000 }, screenshot: "only-on-failure" },
  webServer: [
    { command: "../.venv/bin/python -m uvicorn tests.browser_fixture:app --host 127.0.0.1 --port 18092", cwd: "../backend", url: "http://127.0.0.1:18092/api/health", reuseExistingServer: false },
    { command: "npm run dev -- --host 127.0.0.1 --port 18197 --strictPort", url: "http://127.0.0.1:18197", env: { BIGDIPPER_API_TARGET: "http://127.0.0.1:18092", VITE_ENABLE_MOCK: "false" }, reuseExistingServer: false },
  ],
});
