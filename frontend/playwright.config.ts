import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// ESM: no __dirname. Derive the config directory from import.meta.url.
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Playwright E2E config for the AICC Builder frontend.
 *
 * DEFAULT = REAL deployed backend (dev). Set RUN_REAL=1 (the npm scripts do this).
 * Drives the live dev app at DEV_URL through real Cognito (auth captured once via
 * `npm run test:e2e:auth`) and a real Bedrock-backed backend. No local server.
 *
 * Projects (real mode):
 *   - "setup": interactive auth capture (e2e/real/auth.setup.ts) → e2e/.auth/dev.json.
 *   - "real":  the deployed-app specs (e2e/real/*.spec.ts), reusing the saved auth.
 *
 * Optional OFFLINE layer (RUN_REAL unset): the hermetic mock-backend suite that
 * needs no cloud/Bedrock — run with `npm run test:e2e:mock`. Kept for fast CI /
 * offline UI regression; it mocks the WebSocket + REST.
 *
 * Run:
 *   npm run test:e2e:auth   # one-time / on expiry: log in, save session
 *   npm run test:e2e        # == test:e2e:real (deployed dev, real backend)
 *   npm run test:e2e:mock   # offline hermetic suite (no backend)
 */

// The deployed app URL to test against in real mode. Provide it via the DEV_URL
// env var (your CloudFront/ALB URL, e.g. from cdk-outputs-<stage>.json FrontendUrl).
// Not hardcoded so this stays environment-agnostic for a public repo.
const DEV_URL = process.env.DEV_URL || '';
const AUTH_FILE = path.resolve(__dirname, 'e2e/.auth/dev.json');
const RUN_REAL = process.env.RUN_REAL === '1';

if (RUN_REAL && !DEV_URL) {
  throw new Error(
    'RUN_REAL=1 but DEV_URL is not set. Point it at your deployed app, e.g.\n' +
      '  DEV_URL=https://<your-cloudfront>.cloudfront.net npm run test:e2e:auth',
  );
}

// Hermetic (offline) ports — only used when RUN_REAL is unset.
const HERMETIC_PORT = 5179;
const LOGIN_PORT = 5180;

const baseUse = {
  trace: 'on-first-retry' as const,
  screenshot: 'only-on-failure' as const,
  video: 'retain-on-failure' as const,
  locale: 'ko-KR',
};

export default defineConfig({
  testDir: './e2e',
  fullyParallel: !RUN_REAL, // real backend: keep it serial (shared account, cost, rate limits)
  forbidOnly: !!process.env.CI,
  retries: RUN_REAL ? 0 : process.env.CI ? 1 : 0,
  workers: RUN_REAL ? 1 : undefined,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],
  // Real full-build runs are long; the spec sets its own per-test timeout.
  timeout: RUN_REAL ? 90 * 60_000 : 30_000,
  expect: { timeout: RUN_REAL ? 20_000 : 7_000 },
  use: baseUse,

  projects: RUN_REAL
    ? [
        {
          name: 'setup',
          testMatch: /real\/auth\.setup\.ts$/,
          use: { ...devices['Desktop Chrome'], baseURL: DEV_URL, headless: false },
        },
        {
          // NOTE: intentionally NO `dependencies: ['setup']`. The setup project
          // is INTERACTIVE (waits for a human login) — wiring it as a dependency
          // would re-prompt login on every run. Instead `real` reuses the saved
          // storageState; capture/refresh it explicitly via `npm run test:e2e:auth`.
          // If the saved state is missing/expired, gotoRealApp throws a clear
          // "run test:e2e:auth" message.
          name: 'real',
          testMatch: /real\/.*\.spec\.ts$/,
          use: { ...devices['Desktop Chrome'], baseURL: DEV_URL, storageState: AUTH_FILE },
        },
      ]
    : [
        {
          name: 'hermetic',
          testDir: './e2e',
          testIgnore: [/real\//, /\.live\.spec\.ts$/, /login\.spec\.ts$/],
          use: { ...devices['Desktop Chrome'], baseURL: `http://localhost:${HERMETIC_PORT}` },
        },
        {
          name: 'login',
          testMatch: /login\.spec\.ts$/,
          use: { ...devices['Desktop Chrome'], baseURL: `http://localhost:${LOGIN_PORT}` },
        },
      ],

  // No webServer in real mode (we hit the deployed app). Offline mode boots Vite.
  webServer: RUN_REAL
    ? undefined
    : [
        {
          command: 'npm run dev:e2e',
          url: `http://localhost:${HERMETIC_PORT}`,
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        },
        {
          command: 'npm run dev:e2e-login',
          url: `http://localhost:${LOGIN_PORT}`,
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        },
      ],
});
