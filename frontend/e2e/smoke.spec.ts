import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Harness smoke test — proves the whole hermetic pipeline works:
 * dev server boot + VITE_DEV_AUTH bypass + WS mock handshake + real selectors.
 * If this passes, the rest of the suite is built on solid ground.
 */
test.describe('harness smoke', () => {
  test('app loads past auth to the mode-first start screen', async ({ page, gotoApp }) => {
    await gotoApp();
    await expect(page.getByRole('heading', { name: S.startHeading })).toBeVisible();
    // Three mode cards, Full Build selected by default.
    await expect(page.getByRole('radio', { name: S.modeFull })).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('radio', { name: S.modeSegment })).toBeVisible();
    await expect(page.getByRole('radio', { name: S.modeImprove })).toBeVisible();
  });

  test('starting a full build rotates the session and sends a kickoff message', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await pickMode(page, 'full');
    await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
    await page.getByRole('button', { name: S.startButton }).click();

    // The app rotates to a fresh session → createNewSession with scope [] (full build).
    const created = await mock.waitForAction('createNewSession');
    expect(Array.isArray(created.scope)).toBe(true);
    expect((created.scope as string[]).length).toBe(0);

    // Then it sends the kickoff message once the session is ready.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('claims-status');

    // Stream a reply and assert it lands in the timeline.
    await mock.streamAssistant('Great — let me ask a few questions about your contact center.');
    await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
