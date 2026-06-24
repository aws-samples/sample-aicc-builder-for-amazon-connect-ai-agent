/**
 * REAL deployed dev app — WebSocket protocol/contract (low cost).
 *
 * Taps the real WebSocket and asserts the handshake the frontend depends on:
 * the backend echoes the generation `scope` and the selected `model` on
 * session_created when a run starts. This sends a real createNewSession but
 * does NOT wait for generation, so Bedrock cost is minimal.
 *
 * Customer stories touched: CS-2.1/CS-2.2 (scope), CS-4.1 (model propagation).
 */
import { test, expect } from '@playwright/test';
import { gotoRealApp, START_HEADING, WsTap } from './support/real';

test.describe('real dev — protocol', () => {
  test('starting a single-segment run echoes scope + model on session_created', async ({ page }) => {
    const tap = new WsTap(page).install();
    await gotoRealApp(page);

    // Single Segment → AI Prompt → describe → Start (real createNewSession{scope:['prompt'], model}).
    await page.getByRole('radio', { name: /단일 세그먼트|Single Segment/ }).first().click();
    await page.getByRole('radio', { name: 'AI 프롬프트', exact: true }).click();
    await page.locator('textarea').first().fill('A concise greeting prompt for a billing line.');
    await page.getByRole('button', { name: /^시작$|^Start$/ }).click();

    // The client must send createNewSession with the chosen scope — this is the
    // deterministic contract (mark since gotoApp's initial [] session).
    await expect
      .poll(() => tap.sent.some((f) => f.action === 'createNewSession' && JSON.stringify(f.scope) === '["prompt"]'), {
        timeout: 30_000,
      })
      .toBe(true);

    // And the backend echoes that scope on a session_created. A WS flap at
    // rotation can interleave a later default-scope session_created, so assert
    // that SOME session_created carried ['prompt'] (not strictly the last one),
    // and that it echoed the selected model.
    await expect
      .poll(
        () => tap.byType('session_created').some((f) => JSON.stringify(f.scope) === '["prompt"]'),
        { timeout: 30_000 },
      )
      .toBe(true);
    const promptSc = tap.byType('session_created').find((f) => JSON.stringify(f.scope) === '["prompt"]')!;
    expect(String(promptSc.selectedModel ?? '')).toContain('claude-opus-4');
    expect(tap.byType('connected').length).toBeGreaterThan(0);
  });

  test('a full-build start sends an empty scope (all assets)', async ({ page }) => {
    const tap = new WsTap(page).install();
    await gotoRealApp(page);

    await page.getByRole('radio', { name: /전체 빌드|Full Build/ }).first().click();
    await page.locator('textarea').first().fill('A reservation voice agent for a small hotel.');
    await page.getByRole('button', { name: /^시작$|^Start$/ }).click();

    const lastCreate = async () =>
      (await expect
        .poll(() => [...tap.sent].reverse().find((f) => f.action === 'createNewSession')?.scope, {
          timeout: 30_000,
        })
        .toEqual([]));
    await lastCreate();
  });
});
