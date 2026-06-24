/**
 * Interactive auth capture for the REAL deployed app (dev).
 *
 * This is a Playwright "setup" project that runs HEADED so YOU can log in once
 * with your own Cognito credentials. It never sees or stores your password —
 * after you reach the app, it saves the authenticated browser state (cookies +
 * localStorage, including the Cognito tokens) to e2e/.auth/dev.json, which the
 * real-backend tests then reuse. That file is gitignored.
 *
 * Run it explicitly:   npm run test:e2e:auth
 * Re-run whenever the saved session expires (you'll see tests bounce to /login).
 *
 * It is NOT part of the default suite — only the `setup` project (auth-only) and
 * the real-backend project depend on it; both require RUN_REAL=1.
 */
import { test as setup, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const AUTH_FILE = path.resolve(HERE, '../.auth/dev.json');
const startHeading = /무엇을 만들고 싶으신가요\?|What would you like to build\?|何を構築しますか/;

setup('authenticate against the deployed dev app', async ({ page }) => {
  // Generous: a human is logging in (possibly new-password / MFA).
  setup.setTimeout(5 * 60_000);

  await page.goto('/');

  // If a previous session is still valid we may already be in the app.
  const alreadyIn = await page
    .getByRole('heading', { name: startHeading })
    .isVisible()
    .catch(() => false);

  if (!alreadyIn) {
    // Surface clear guidance in the headed window's console + terminal.
    console.log('\n⏳  Please sign in in the opened browser window…');
    console.log('    (the run continues automatically once the app loads)\n');
    // Wait for the human to finish login: the SPA leaves /login and renders the
    // mode-first start screen. 5-minute window covers new-password/MFA flows.
    await expect(page.getByRole('heading', { name: startHeading })).toBeVisible({ timeout: 5 * 60_000 });
  }

  fs.mkdirSync(path.dirname(AUTH_FILE), { recursive: true });
  await page.context().storageState({ path: AUTH_FILE });
  console.log(`\n✅  Saved authenticated state → ${AUTH_FILE}`);
  console.log('    The real-backend suite (npm run test:e2e:real) will reuse it.\n');
});
