import { test, expect, S, pickMode, typeDescription } from './support/fixtures';

/**
 * Localization — Korean (ko-KR) default surface.
 * Covers customer story: CS-7.1 (the experience is localized; ko-KR is the default
 * language and the UI chrome — start screen, mode cards, right-pane, chat header —
 * renders consistently in Korean).
 *
 * Language-switching UI lives on the login page / header and is out of scope for the
 * hermetic suite; this spec asserts the ko-KR surface is internally consistent.
 */
test.describe('CS-7.1 localization (ko-KR default surface)', () => {
  test('start screen renders the Korean heading and Korean mode labels by default', async ({ page, gotoApp }) => {
    await gotoApp();

    // Korean start heading (literal ko string, not the bilingual regex) proves ko-KR default.
    await expect(page.getByRole('heading', { name: '무엇을 만들고 싶으신가요?' })).toBeVisible();

    // Three mode cards (role=radio) with their Korean labels.
    const fullCard = page.getByRole('radio', { name: '전체 빌드' });
    const segmentCard = page.getByRole('radio', { name: '단일 세그먼트' });
    const improveCard = page.getByRole('radio', { name: '기존 에셋 개선' });

    await expect(fullCard).toBeVisible();
    await expect(segmentCard).toBeVisible();
    await expect(improveCard).toBeVisible();

    // Full Build is selected by default.
    await expect(fullCard).toHaveAttribute('aria-checked', 'true');

    // The Korean Start button label is present on the shared composer.
    await expect(page.getByRole('button', { name: /^시작$/ })).toBeVisible();
  });

  test('the right-pane view switcher shows the Korean "진행 상황" Progress button', async ({ page, gotoApp }) => {
    await gotoApp();

    // The Progress view-switch is a button (aria-pressed), labelled in Korean.
    // A collapsed mobile rail can duplicate the switch — scope to the desktop one.
    const progressBtn = page.getByRole('button', { name: S.tabProgress }).first();
    await expect(progressBtn).toBeVisible();
    await expect(progressBtn).toHaveText(/진행 상황/);
    // It is the default-selected right-pane view on a fresh app.
    await expect(progressBtn).toHaveAttribute('aria-pressed', 'true');
  });

  test('after starting a full build the chat header shows Korean "연결됨"', async ({ page, mock, gotoApp }) => {
    await gotoApp();

    await pickMode(page, 'full');
    await typeDescription(page, '보험 청구 상태를 안내하는 AI 음성 상담원을 만들고 싶어요.');
    await page.getByRole('button', { name: S.startButton }).click();

    // App rotates to a fresh session (full build → empty scope), then sends the kickoff.
    const created = await mock.waitForAction('createNewSession');
    expect(Array.isArray(created.scope)).toBe(true);
    expect((created.scope as string[]).length).toBe(0);
    await mock.waitForAction('sendMessage');

    // Stream a reply so the timeline becomes non-empty and the in-chat header renders.
    await mock.streamAssistant('컨택센터에 대해 몇 가지 여쭤볼게요.');
    await expect(page.getByText(/몇 가지 여쭤볼게요/)).toBeVisible();

    // The chat header connection indicator reads the Korean "연결됨" while connected.
    await expect(page.getByText('연결됨').first()).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
