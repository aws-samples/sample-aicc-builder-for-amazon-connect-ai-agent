import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Full Build kickoff — Customer Stories CS-1.1, CS-1.2.
 *
 * CS-1.1: A customer describes their contact center and starts a full build.
 *         The app rotates to a fresh session (createNewSession, scope []) and
 *         sends the typed description as the kickoff sendMessage; the streamed
 *         assistant reply lands in the timeline.
 * CS-1.2: A customer starts a full build WITHOUT typing anything. The app still
 *         rotates the session and sends a kickoff sendMessage whose message is the
 *         localized default opener (ko-KR by default).
 */
test.describe('Full Build kickoff (CS-1.1, CS-1.2)', () => {
  test('CS-1.1: typed description starts a full build and streams a reply', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // Full Build is the default mode, but assert + (re)select it explicitly for robustness.
    await expect(page.getByRole('radio', { name: S.modeFull })).toHaveAttribute('aria-checked', 'true');
    await pickMode(page, 'full');

    const description = 'A claims-status voice agent for an insurance contact center.';
    await typeDescription(page, description);

    // Full Build: Start is always enabled.
    const startBtn = page.getByRole('button', { name: S.startButton });
    await expect(startBtn).toBeEnabled();
    await startBtn.click();

    // Session rotation: createNewSession with an empty scope (full build).
    const created = await mock.waitForAction('createNewSession');
    expect(Array.isArray(created.scope)).toBe(true);
    expect((created.scope as string[]).length).toBe(0);

    // Kickoff sendMessage carries the typed description verbatim.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('claims-status voice agent');
    // A kickoff always advertises a language so the backend localizes correctly.
    expect(typeof sent.language).toBe('string');

    // Stream an assistant reply and assert it renders in the timeline.
    await mock.streamAssistant('Great — let me ask a few questions about your contact center.');
    await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();

    // Clean happy path: no uncaught console errors.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-1.2: empty description still sends the localized default opener', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    await pickMode(page, 'full');

    // Deliberately do NOT type anything. Full Build keeps Start enabled.
    const startBtn = page.getByRole('button', { name: S.startButton });
    await expect(startBtn).toBeEnabled();
    await startBtn.click();

    // Still rotates to a fresh full-build session.
    const created = await mock.waitForAction('createNewSession');
    expect(Array.isArray(created.scope)).toBe(true);
    expect((created.scope as string[]).length).toBe(0);

    // A kickoff sendMessage is STILL sent, carrying the localized default opener.
    const sent = await mock.waitForAction('sendMessage');
    const message = String(sent.message);
    expect(message.trim().length).toBeGreaterThan(0);
    expect(message).toMatch(/시작할게요|Let's get started|guide me/);

    // Stream a reply to confirm the rotated session is live end-to-end.
    await mock.streamAssistant('좋아요 — 컨택센터에 대해 몇 가지 여쭤볼게요.');
    await expect(page.getByText(/몇 가지 여쭤볼게요/)).toBeVisible();

    // Clean happy path: no uncaught console errors.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
