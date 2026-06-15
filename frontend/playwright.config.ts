import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E configuration (testing-strategy §12; frontend-architecture §11.2).
 *
 * The suites drive the REAL console against the full Docker Compose stack
 * (postgres, redis, kafka, api, ws, worker, runner, buffer-writer, web + the
 * dev-only mailpit). The stack is brought up by the caller (CI `docker compose up
 * --wait`; locally `docker compose up -d --wait`), so there is NO `webServer`
 * block — Playwright assumes the stack is already healthy at the configured
 * base URLs.
 *
 * Lanes via grep tags (frontend-architecture §11.2 / testing-strategy §12):
 *   @smoke   — PR lane: auth.spec.ts + core-loop.spec.ts (the Phase-7 exit gate)
 *   @nightly — nightly lane: keys / stream-control / live-tail + @axe-core checks
 *
 * Selectors are accessible roles/labels/text first (which doubles as an a11y
 * exercise), with `data-testid` for the load-bearing dynamic elements
 * (status-badge, copy-field-value, tail-row).
 *
 * Env:
 *   E2E_BASE_URL    console origin (default http://localhost:5173)
 *   E2E_API_URL     api origin for direct probes (default http://localhost:8000)
 *   E2E_MAILPIT_URL mailpit REST API (default http://localhost:8025)
 */
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173';

export default defineConfig({
  testDir: './e2e',
  // The core loop has a 15-minute time-to-first-event budget (PRD §2.1/§8); give a
  // generous per-test cap and rely on intra-test waits for the tight assertions.
  timeout: 5 * 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  // No silent retries in CI — a flaky E2E is a real signal (testing-strategy §5.4).
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never' }], ['list']]
    : [['list']],
  use: {
    baseURL: BASE_URL,
    // Failure artifacts (testing-strategy §12): trace, video, console logs.
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
