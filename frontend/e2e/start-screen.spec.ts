/**
 * Mode-first start screen: rendering, Start-button gating, attachment validation.
 *
 * Customer stories covered:
 *  - CS-1.1  Full Build is the default entry point; Start is always available.
 *  - CS-2.3  Single-Segment gating: a segment must be chosen AND a description typed
 *            (or a file attached) before a run can start.
 *  - CS-3.5  Improve-Existing rejects unsupported file types inline and sends nothing.
 *
 * All selectors come from ./support/fixtures (S.*) or the verified bilingual ko/en
 * strings rendered by ChatEmptyState.tsx. No real backend — the mock fixture stands in.
 */
import { test, expect, S, pickMode, segmentRadio, typeDescription } from './support/fixtures';

test.describe('start screen — mode-first entry, gating & attachment validation', () => {
  test('CS-1.1 renders three mode cards with Full Build checked by default', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    await expect(page.getByRole('heading', { name: S.startHeading })).toBeVisible();

    const full = page.getByRole('radio', { name: S.modeFull });
    const segment = page.getByRole('radio', { name: S.modeSegment });
    const improve = page.getByRole('radio', { name: S.modeImprove });

    // All three mode cards are present...
    await expect(full).toBeVisible();
    await expect(segment).toBeVisible();
    await expect(improve).toBeVisible();

    // ...and Full Build is the one selected on first load.
    await expect(full).toHaveAttribute('aria-checked', 'true');
    await expect(segment).toHaveAttribute('aria-checked', 'false');
    await expect(improve).toHaveAttribute('aria-checked', 'false');

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-1.1 Full Build keeps Start enabled even with an empty description', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await pickMode(page, 'full');

    // No description typed, no file attached — Full Build still lets you begin
    // (the interview runs and fills in the gaps).
    const start = page.getByRole('button', { name: S.startButton });
    await expect(start).toBeVisible();
    await expect(start).toBeEnabled();

    // Single-segment radio row is NOT shown while in Full Build mode.
    await expect(page.getByRole('radio', { name: S.segContactFlow })).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-2.3 Single Segment reveals the segment radios and gates Start until segment + description', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await pickMode(page, 'segment');

    // Selecting "Single Segment" expands the Contact Flow / AI 프롬프트 / FAQ row.
    // Use exact-name segment helpers: the mode cards' descriptions also contain
    // these phrases, so a loose name matches 3 elements (strict-mode violation).
    const cf = segmentRadio(page, 'contact_flow');
    const prompt = segmentRadio(page, 'prompt');
    const faq = segmentRadio(page, 'faq');
    await expect(cf).toBeVisible();
    await expect(prompt).toBeVisible();
    await expect(faq).toBeVisible();

    const start = page.getByRole('button', { name: S.startButton });

    // (a) Segment chosen but no description/file → still disabled.
    await expect(start).toBeDisabled();
    await cf.click();
    await expect(cf).toHaveAttribute('aria-checked', 'true');
    await expect(start).toBeDisabled();

    // (b) Description typed but no segment → also disabled. Prove it by clearing the
    //     segment selection first (switch to AI Prompt back-and-forth is not "no segment",
    //     so instead test the description-only branch fresh below). Here, with a segment
    //     chosen, typing a description should ENABLE Start.
    await typeDescription(page, 'A contact flow that greets callers and routes by DTMF.');
    await expect(start).toBeEnabled();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-2.3 Single Segment with a description but no segment chosen keeps Start disabled', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await pickMode(page, 'segment');

    // Description present, but no segment radio chosen yet → gating still blocks Start.
    await typeDescription(page, 'Something to build, but I have not picked a segment.');

    const start = page.getByRole('button', { name: S.startButton });
    await expect(start).toBeDisabled();

    // Now choose a segment → Start unlocks.
    await page.getByRole('radio', { name: S.segFaq }).click();
    await expect(start).toBeEnabled();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-2.3 switching from Segment back to Full clears the segment and re-enables Start', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // Enter segment mode, pick a segment so Start would be gated on a description.
    await pickMode(page, 'segment');
    const cf = page.getByRole('radio', { name: S.segContactFlow });
    await cf.click();
    await expect(cf).toHaveAttribute('aria-checked', 'true');

    const start = page.getByRole('button', { name: S.startButton });
    await expect(start).toBeDisabled(); // gated: segment chosen but no description

    // Switch back to Full Build → segment row disappears, Start enabled regardless.
    await pickMode(page, 'full');
    await expect(page.getByRole('radio', { name: S.modeFull })).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('radio', { name: S.segContactFlow })).toHaveCount(0);
    await expect(start).toBeEnabled();

    // Re-entering segment mode shows NO segment pre-selected (the prior choice was cleared),
    // so Start is gated again until a fresh selection.
    await pickMode(page, 'segment');
    const cfAgain = segmentRadio(page, 'contact_flow');
    const promptAgain = segmentRadio(page, 'prompt');
    const faqAgain = segmentRadio(page, 'faq');
    await expect(cfAgain).toHaveAttribute('aria-checked', 'false');
    await expect(promptAgain).toHaveAttribute('aria-checked', 'false');
    await expect(faqAgain).toHaveAttribute('aria-checked', 'false');
    await expect(start).toBeDisabled();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-3.5 Improve Existing rejects an unsupported file inline and sends nothing', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await pickMode(page, 'improve');

    // Attach an unsupported binary (an .exe with an unknown/binary mime). The start
    // screen rejects it inline and stages nothing.
    await page.locator('input[type=file]').setInputFiles({
      name: 'malware.exe',
      mimeType: 'application/x-msdownload',
      buffer: Buffer.from([0x4d, 0x5a, 0x90, 0x00]), // "MZ" PE header bytes
    });

    // Inline rejection message appears (bilingual: ko/en/unsupported variants).
    await expect(
      page.getByText(/지원되지 않는 파일|Unsupported file|unsupported type/i),
    ).toBeVisible();

    // No description was typed and the file was rejected → Start stays disabled.
    const start = page.getByRole('button', { name: S.startButton });
    await expect(start).toBeDisabled();

    // The app never rotated the session nor sent a kickoff for this rejected attempt.
    expect(mock.inbound.some((m) => m.action === 'sendMessage')).toBe(false);
    expect(mock.inbound.some((m) => m.action === 'sendMessageWithAttachments')).toBe(false);
  });
});
