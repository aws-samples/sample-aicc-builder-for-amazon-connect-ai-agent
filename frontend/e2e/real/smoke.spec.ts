/**
 * REAL deployed dev app — smoke (fast, ~no Bedrock cost).
 *
 * Verifies the authenticated app loads and the core start-screen surfaces are
 * present and interactive on the live deployment. Uses the saved auth state
 * (e2e/.auth/dev.json) captured via `npm run test:e2e:auth`.
 *
 * Customer stories touched: CS-1.1 (start screen), CS-4.1 (model selector),
 * CS-7.1 (Korean default surface).
 */
import { test, expect } from '@playwright/test';
import { gotoRealApp, START_HEADING } from './support/real';

test.describe('real dev — smoke', () => {
  test('authenticated app loads to the mode-first start screen', async ({ page }) => {
    await gotoRealApp(page);
    await expect(page.getByRole('heading', { name: START_HEADING })).toBeVisible();

    // Three mode cards; Full Build selected by default.
    await expect(page.getByRole('radio', { name: /전체 빌드|Full Build/ }).first()).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('radio', { name: /단일 세그먼트|Single Segment/ }).first()).toBeVisible();
    await expect(page.getByRole('radio', { name: /기존 에셋 개선|Improve Existing/ }).first()).toBeVisible();
  });

  test('the chat header reports a live backend connection', async ({ page }) => {
    await gotoRealApp(page);
    // "연결됨"/"Connected" appears once the real WebSocket is open.
    await expect(page.getByText(/연결됨|Connected/).first()).toBeVisible({ timeout: 20_000 });
  });

  test('the model selector lists the Claude models and persists a choice', async ({ page }) => {
    await gotoRealApp(page);

    const trigger = page.getByRole('button', { name: /Claude 모델 선택|Select Claude model/ }).first();
    await trigger.click();
    await expect(page.getByRole('listbox')).toBeVisible();
    await expect(page.getByRole('option', { name: /Opus 4\.8/ })).toBeVisible();
    await expect(page.getByRole('option', { name: /Opus 4\.6/ })).toBeVisible();

    await page.getByRole('option', { name: /Opus 4\.6/ }).click();
    // setSelectedModel persists to localStorage asynchronously — poll, don't read once.
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('selectedModel')))
      .toBe('global.anthropic.claude-opus-4-6-v1');

    // Reset to default so we don't leave 4.6 selected for later runs.
    await trigger.click();
    await page.getByRole('option', { name: /Opus 4\.8/ }).click();
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('selectedModel')))
      .toBe('global.anthropic.claude-opus-4-8');
  });

  test('single-segment mode reveals the segment radios', async ({ page }) => {
    await gotoRealApp(page);
    await page.getByRole('radio', { name: /단일 세그먼트|Single Segment/ }).first().click();
    await expect(page.getByRole('radio', { name: 'Contact Flow', exact: true })).toBeVisible();
    await expect(page.getByRole('radio', { name: 'AI 프롬프트', exact: true })).toBeVisible();
    await expect(page.getByRole('radio', { name: 'FAQ', exact: true })).toBeVisible();
  });
});
