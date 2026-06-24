import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Model selection — Claude model picker persistence + propagation to the backend.
 *
 * Customer stories covered: CS-4.1
 *
 * Verified facts this spec leans on (see ModelSelector.tsx / builderStore.ts):
 *  - The header ModelSelector trigger is a <button> with aria-label "Claude 모델 선택"
 *    (S.modelSelector), aria-haspopup="listbox". Opening renders a role=listbox with
 *    role=option items "Opus 4.8" / "Opus 4.7" / "Opus 4.6".
 *  - MODELS: Opus 4.8 → 'global.anthropic.claude-opus-4-8',
 *            Opus 4.7 → 'global.anthropic.claude-opus-4-7',
 *            Opus 4.6 → 'global.anthropic.claude-opus-4-6-v1'. Default = Opus 4.8.
 *  - setSelectedModel persists to localStorage key 'selectedModel'.
 *  - The trigger's title is `${label}: ${current.label}`, so it contains the model label.
 *  - On Start the app sends createNewSession {scope, model} then a kickoff sendMessage
 *    {message, language, model}. Mid-session chat re-sends the (possibly changed) model.
 *
 * NOTE: the start screen renders TWO ModelSelectors (header + composer toolbar) sharing
 * the same aria-label, so we always scope to the banner landmark.
 */

const OPUS_48 = 'global.anthropic.claude-opus-4-8';
const OPUS_46 = 'global.anthropic.claude-opus-4-6-v1';

/** The header ModelSelector trigger (scoped to avoid the composer's duplicate). */
const headerSelector = (page: import('@playwright/test').Page) =>
  page.getByRole('banner').getByRole('button', { name: S.modelSelector });

test.describe('CS-4.1 — Claude model selection persistence + backend propagation', () => {
  test('selecting a model persists to localStorage and survives reload', async ({ page, gotoApp }) => {
    await gotoApp();

    // Default model is Opus 4.8 — the trigger title reflects it.
    const trigger = headerSelector(page);
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveAttribute('title', /Opus 4\.8/);

    // Open the listbox and assert all three options are present.
    await trigger.click();
    const listbox = page.getByRole('listbox', { name: S.modelSelector }).first();
    await expect(listbox).toBeVisible();
    await expect(page.getByRole('option', { name: 'Opus 4.8' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Opus 4.7' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Opus 4.6' })).toBeVisible();

    // Pick Opus 4.6.
    await page.getByRole('option', { name: 'Opus 4.6' }).click();
    await expect(listbox).toBeHidden();
    await expect(trigger).toHaveAttribute('title', /Opus 4\.6/);

    // Persisted to localStorage under 'selectedModel'.
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('selectedModel')))
      .toBe(OPUS_46);

    // Reload — the choice persists and the trigger still shows Opus 4.6.
    await page.reload();
    await expect(page.getByRole('heading', { name: S.startHeading })).toBeVisible({ timeout: 15_000 });
    await expect(headerSelector(page)).toHaveAttribute('title', /Opus 4\.6/);
    await expect(
      await page.evaluate(() => localStorage.getItem('selectedModel')),
    ).toBe(OPUS_46);
  });

  test('the selected model reaches createNewSession and the kickoff sendMessage', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // Choose Opus 4.6 in the header.
    await headerSelector(page).click();
    await page.getByRole('option', { name: 'Opus 4.6' }).click();
    await expect(headerSelector(page)).toHaveAttribute('title', /Opus 4\.6/);

    // Start a full build. Mark the inbound cursor so we read the rotated session's
    // createNewSession (carrying the picked model), not the initial page-load one
    // which was fired with the default model before Opus 4.6 was selected.
    await pickMode(page, 'full');
    await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
    const since = mock.mark();
    await page.getByRole('button', { name: S.startButton }).click();

    // The session rotation carries the chosen model…
    const created = await mock.waitForAction('createNewSession', { since });
    expect((created.scope as string[]).length).toBe(0); // full build → empty scope
    expect(created.model).toBe(OPUS_46);

    // …and so does the kickoff message.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('claims-status');
    expect(sent.model).toBe(OPUS_46);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('switching the model mid-session applies to the next chat message', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // Default (Opus 4.8) — start a full build to leave the empty state.
    await pickMode(page, 'full');
    await typeDescription(page, 'Build a billing-inquiry agent.');
    await page.getByRole('button', { name: S.startButton }).click();

    const created = await mock.waitForAction('createNewSession');
    expect(created.model).toBe(OPUS_48);
    const kickoff = await mock.waitForAction('sendMessage');
    expect(kickoff.model).toBe(OPUS_48);

    // Drive the timeline non-empty so the in-chat composer renders.
    await mock.streamAssistant('Sure — let me ask a few questions to get started.');
    await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();

    // Switch the header model to Opus 4.8 explicitly via the menu (re-asserts default id).
    await headerSelector(page).click();
    await page.getByRole('option', { name: 'Opus 4.8' }).click();
    await expect(headerSelector(page)).toHaveAttribute('title', /Opus 4\.8/);

    // Count sendMessages so far, then send another message via the in-chat composer.
    const before = mock.inbound.filter((m) => m.action === 'sendMessage').length;
    const composer = page.locator('textarea').last();
    await composer.fill('Yes, customers call about billing disputes.');
    await composer.press('Enter');

    // A new sendMessage arrives carrying the currently-selected model.
    await expect
      .poll(() => mock.inbound.filter((m) => m.action === 'sendMessage').length)
      .toBeGreaterThan(before);
    const next = [...mock.inbound].reverse().find((m) => m.action === 'sendMessage');
    expect(String(next?.message)).toContain('billing disputes');
    expect(next?.model).toBe(OPUS_48);
  });
});
