import { test, expect, S, pickMode, pickSegment, typeDescription } from './support/fixtures';
import type { Page } from '@playwright/test';
import type { MockBackend } from './support/mock-backend';

/**
 * Single-segment scope + trimmed progress.
 *
 * Customer stories covered:
 *  - CS-2.1  Single Segment mode: choose one Connect asset (Contact Flow / AI Prompt / FAQ)
 *            and the kickoff carries the correct scope array to the backend.
 *  - CS-2.2  Per-segment scope mapping: Contact Flow → ['contact_flow'], AI Prompt → ['prompt'],
 *            FAQ → ['faq'].
 *  - CS-5.2  Trimmed progress: a single-segment run shows totalCount 7 ("0 / 7 완료") and renders
 *            an out-of-scope section ("이번 실행에 포함되지 않음").
 */

type SegmentKind = 'contact_flow' | 'prompt' | 'faq';

/**
 * Pick Segment mode, select a given segment radio, type a description, click Start,
 * then assert the backend received createNewSession with the expected scope array.
 * Returns the parsed createNewSession message.
 */
async function startSegment(
  page: Page,
  mock: MockBackend,
  opts: { segment: SegmentKind; expectedScope: SegmentKind[]; desc: string },
) {
  await pickMode(page, 'segment');

  // The segment radio row only appears once Segment mode is chosen. Use the
  // exact-name helper — the mode cards' descriptions also contain "AI 프롬프트"
  // etc., so a loose name matches 3 elements (strict-mode violation).
  const radio = await pickSegment(page, opts.segment);
  await expect(radio).toHaveAttribute('aria-checked', 'true');

  await typeDescription(page, opts.desc);

  // Start is enabled now that a segment is chosen AND a description is typed.
  const startBtn = page.getByRole('button', { name: S.startButton });
  await expect(startBtn).toBeEnabled();

  // Ignore the initial page-load createNewSession (scope []) the app fires before
  // Start: mark the inbound cursor, then read the createNewSession AFTER the click.
  const since = mock.mark();
  await startBtn.click();

  // The app rotates to a fresh session: createNewSession carries the segment scope.
  const created = await mock.waitForAction('createNewSession', { since });
  expect(Array.isArray(created.scope)).toBe(true);
  expect(created.scope as string[]).toEqual(opts.expectedScope);

  return created;
}

test.describe('single-segment scope (CS-2.1, CS-2.2, CS-5.2)', () => {
  test('Contact Flow segment → scope ["contact_flow"] and trimmed 0 / 7 progress', async ({
    page,
    mock,
    gotoApp,
  }) => {
    await gotoApp();

    await startSegment(page, mock, {
      segment: "contact_flow",
      expectedScope: ['contact_flow'],
      desc: 'A claims-status contact flow with a DTMF menu for an insurance line.',
    });

    // The kickoff message follows once the session is created.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('claims-status');

    // The default handler already emitted session_created with this scope; open Progress.
    // A collapsed mobile rail can duplicate the switch — scope to the desktop one.
    await page.getByRole('button', { name: S.tabProgress }).first().click();

    // Trimmed progress for a single-segment run: interview (4) + 1 lane + review + package = 7.
    await expect(page.getByText(/0\s*\/\s*7\s*(완료|complete)/)).toBeVisible();

    // Steps outside this run's scope render under the "Not in this run" section.
    await expect(page.getByText(/이번 실행에 포함되지 않음|Not in this run/)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('AI Prompt segment → scope ["prompt"]', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    await startSegment(page, mock, {
      segment: "prompt",
      expectedScope: ['prompt'],
      desc: 'An AI agent prompt that greets the caller and collects a policy number.',
    });

    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('policy number');

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('FAQ segment → scope ["faq"] reaches the backend', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    // startSegment asserts createNewSession.scope === ['faq'].
    await startSegment(page, mock, {
      segment: "faq",
      expectedScope: ['faq'],
      desc: 'A billing FAQ knowledge base for common policyholder questions.',
    });

    // Confirm the kickoff also flowed through after the rotated session was created.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('FAQ');

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
