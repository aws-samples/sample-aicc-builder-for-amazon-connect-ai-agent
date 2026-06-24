import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Multi-session basics via the Session Sidebar.
 *
 * Customer stories covered:
 *   - CS-6.1  Start a new chat from the sidebar; the app rotates to a fresh
 *             session (new createNewSession), the mode-first start screen
 *             returns, and the previous session's streamed reply is gone.
 */
test.describe('CS-6.1 — multi-session basics (Session Sidebar)', () => {
  test('New Chat rotates to a fresh session and resets the timeline', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // ── Session A: start a full build so the timeline becomes non-empty. ──────
    await pickMode(page, 'full');
    await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
    await page.getByRole('button', { name: S.startButton }).click();

    // The Start click rotates to a fresh session: createNewSession (full build → scope []).
    const firstCreated = await mock.waitForAction('createNewSession');
    expect(Array.isArray(firstCreated.scope)).toBe(true);
    expect((firstCreated.scope as string[]).length).toBe(0);

    // Then the kickoff message is sent once the session is ready.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('claims-status');

    // Stream an assistant reply and confirm it lands in the timeline.
    const replyA = 'Session A reply — let me ask a few questions about your contact center.';
    await mock.streamAssistant(replyA);
    await expect(page.getByText(/let me ask a few questions about your contact center/i)).toBeVisible();

    // The start screen is gone now that the timeline is non-empty.
    await expect(page.getByRole('heading', { name: S.startHeading })).toBeHidden();

    // Record how many createNewSession actions we've seen so far (at least one).
    const createCountBefore = mock.inbound.filter((m) => m.action === 'createNewSession').length;
    expect(createCountBefore).toBeGreaterThanOrEqual(1);

    // ── Click "새 대화" / New Chat in the sidebar. ─────────────────────────────
    await page.getByRole('button', { name: /새 대화|New Chat/ }).click();

    // The app rotates again: a brand-new createNewSession is sent.
    await expect
      .poll(() => mock.inbound.filter((m) => m.action === 'createNewSession').length, { timeout: 10_000 })
      .toBeGreaterThan(createCountBefore);

    // The fresh session resets to the mode-first start screen.
    await expect(page.getByRole('heading', { name: S.startHeading })).toBeVisible();
    // Full Build is the default mode again on the fresh start screen.
    await expect(page.getByRole('radio', { name: S.modeFull })).toHaveAttribute('aria-checked', 'true');

    // The previous session's streamed assistant text is no longer visible.
    await expect(page.getByText(/let me ask a few questions about your contact center/i)).toBeHidden();

    // No uncaught console errors on this happy path.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
