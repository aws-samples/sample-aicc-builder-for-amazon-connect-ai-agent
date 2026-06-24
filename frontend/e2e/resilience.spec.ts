import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Error + resilience paths.
 *
 * Customer stories covered:
 *   CS-8.1 — the app surfaces backend errors, recovers gracefully from a
 *            "still processing" deadlock (reset-session affordance), and
 *            survives a dropped WebSocket without crashing.
 *
 * NOTE: these tests INTENTIONALLY emit `error` events and drop the socket, so
 * they do NOT assert `mock.consoleErrors` is empty — only that the app stays
 * functional and shows the right recovery UI.
 */

/** Start a full build so a session + in-chat timeline exists, then return. */
async function startFullBuild(page: import('@playwright/test').Page, mock: import('./support/mock-backend').MockBackend) {
  await pickMode(page, 'full');
  await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
  await page.getByRole('button', { name: S.startButton }).click();
  // Rotate to the fresh session and complete the kickoff handshake.
  await mock.waitForAction('createNewSession');
  await mock.waitForAction('sendMessage');
}

test.describe('CS-8.1 resilience: backend errors & reconnect', () => {
  test('a backend error is surfaced in the timeline and the composer stays usable', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Backend reports a failure mid-flight.
    mock.error('Something broke on the server');

    // The error text lands in the timeline as a system/error message.
    await expect(page.getByText(/Something broke on the server/i)).toBeVisible();

    // The chat composer is re-enabled — the in-chat textarea is present and editable,
    // and a follow-up message can still be sent over the wire.
    const composer = page.locator('textarea').first();
    await expect(composer).toBeVisible();
    await expect(composer).toBeEditable();

    // Mark first so we read the follow-up sendMessage, not the earlier kickoff.
    const since = mock.mark();
    await composer.fill('Can you retry that step?');
    await composer.press('Enter');
    const retry = await mock.waitForAction('sendMessage', { since });
    expect(String(retry.message)).toContain('retry');
  });

  test('two "still processing" errors reveal the reset-session affordance', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // One "still processing" error is not yet enough to trip the guard.
    mock.error('Agent is still processing your previous request');
    await expect(
      page.getByText(/이전 요청이 여전히 처리 중입니다|still stuck/i),
    ).toHaveCount(0);

    // The second hit (stillProcessingCount >= 2) surfaces the red banner + reset button.
    mock.error('Agent is still processing your previous request');

    await expect(
      page.getByText(/이전 요청이 여전히 처리 중입니다|still stuck/i),
    ).toBeVisible();
    const resetButton = page.getByRole('button', { name: /세션 재설정|Reset session/ });
    await expect(resetButton).toBeVisible();

    // Clicking Reset rotates to a brand-new session → a fresh createNewSession is sent.
    const before = mock.inbound.filter((m) => m.action === 'createNewSession').length;
    await resetButton.click();

    await expect
      .poll(() => mock.inbound.filter((m) => m.action === 'createNewSession').length, { timeout: 10_000 })
      .toBeGreaterThan(before);

    // The reset session is a full build (scope []), and the banner is cleared.
    const created = await mock.waitForAction('createNewSession');
    expect(Array.isArray(created.scope)).toBe(true);
    await expect(
      page.getByText(/이전 요청이 여전히 처리 중입니다|still stuck/i),
    ).toHaveCount(0);
  });

  test('a dropped WebSocket does not crash the app and it recovers', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Stream something so the timeline is populated before the drop.
    await mock.streamAssistant('Let me gather a few details about your contact center.');
    await expect(page.getByText(/gather a few details/i)).toBeVisible();

    // Simulate the backend dropping the socket.
    await mock.dropConnection();

    // ── Survives the drop without crashing ────────────────────────────────────
    // The composer stays mounted (the app didn't unmount/crash). While the socket
    // is down it is intentionally disabled, so we only require it to be PRESENT
    // here — recovery (editable again) is asserted after reload below.
    const composer = page.locator('textarea').first();
    await expect(composer).toBeVisible({ timeout: 15_000 });

    // No fatal page error / crash: the previously rendered timeline content
    // (or the start heading, if the view reset) is still on screen.
    const survived =
      (await page.getByText(/gather a few details/i).count()) > 0 ||
      (await page.getByRole('heading', { name: S.startHeading }).count()) > 0;
    expect(survived).toBe(true);

    // ── Recovers ──────────────────────────────────────────────────────────────
    // Reload re-establishes the WebSocket through the same-origin connect path
    // (the mock's routeWebSocket re-installs on the fresh navigation).
    await page.reload();
    await expect(page.getByRole('heading', { name: S.startHeading })).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => mock.connected, { timeout: 15_000 }).toBe(true);

    // Once reconnected the composer is usable again.
    const recovered = page.locator('textarea').first();
    await expect(recovered).toBeVisible({ timeout: 15_000 });
    await expect(recovered).toBeEditable({ timeout: 15_000 });

    // No fatal crash after recovery either: the start surface is back.
    const stillFunctional =
      (await page.getByText(/gather a few details/i).count()) > 0 ||
      (await page.getByRole('heading', { name: S.startHeading }).count()) > 0;
    expect(stillFunctional).toBe(true);
  });
});
