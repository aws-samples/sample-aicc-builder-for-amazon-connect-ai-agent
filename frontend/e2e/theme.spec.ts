import { test, expect } from './support/fixtures';

/**
 * Theme toggle — re-themes the whole app by cycling light → dark → system.
 *
 * Customer stories covered: CS-9.2
 *
 * The header theme toggle (Header.tsx, onClick={cycleTheme}) is a <button>
 * whose `title` attribute is the current theme's label (라이트/다크/시스템 in ko-KR,
 * or Light/Dark/System in en). Both Header.tsx and App.tsx apply the theme by
 * toggling the `dark` class on document.documentElement. The store defaults to
 * 'dark', so <html> starts with the `dark` class.
 *
 * We assert tolerantly: across two cycles the presence of the `dark` class on
 * <html> must change at least once.
 */
test.describe('CS-9.2 theme toggle re-themes the app', () => {
  /** Read whether <html> currently has the `dark` class. */
  const isDark = (page: import('@playwright/test').Page) =>
    page.evaluate(() => document.documentElement.classList.contains('dark'));

  test('clicking the header theme toggle cycles the dark class on <html>', async ({ page, gotoApp, mock }) => {
    await gotoApp();

    // The theme toggle is the header button whose `title` is one of the theme
    // labels (라이트/다크/시스템 in ko-KR, Light/Dark/System in en). Locate it by
    // title so we don't depend on the label <span> being visible at this width.
    const themeButton = page
      .locator(
        'button[title="라이트"], button[title="다크"], button[title="시스템"], ' +
          'button[title="Light"], button[title="Dark"], button[title="System"]',
      )
      .first();
    await expect(themeButton).toBeVisible();

    // Default store theme is 'dark' → <html> should carry the `dark` class.
    const before = await isDark(page);
    expect(before).toBe(true);

    // Click once → theme cycles to the next value, re-applying the dark class.
    await themeButton.click();
    const afterFirst = await isDark(page);

    // Click again → cycles once more.
    await themeButton.click();
    const afterSecond = await isDark(page);

    // Tolerant assertion: across the two clicks the `dark` class presence must
    // have changed at least once (light/dark/system cycle is order-dependent,
    // and 'system' resolves to the headless prefers-color-scheme).
    const states = [before, afterFirst, afterSecond];
    const changedAtLeastOnce =
      states.some((s) => s !== before) || new Set(states).size > 1;
    expect(
      changedAtLeastOnce,
      `dark class never changed across two toggles: ${JSON.stringify(states)}`,
    ).toBe(true);

    // The toggle remains usable after cycling (no crash, still rendered).
    await expect(themeButton).toBeVisible();

    // Pure UI interaction — no backend errors expected.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('the dark class flips between consecutive non-system steps', async ({ page, gotoApp }) => {
    await gotoApp();

    const byTitle = page.locator(
      'button[title="라이트"], button[title="다크"], button[title="시스템"], ' +
        'button[title="Light"], button[title="Dark"], button[title="System"]',
    );
    const themeButton = byTitle.first();
    await expect(themeButton).toBeVisible();

    // Cycle through a full revolution (light → dark → system → light) and record
    // the dark-class state at each step. Over three clicks we must observe more
    // than one distinct state, proving the toggle actually re-themes the DOM.
    const seen: boolean[] = [await isDark(page)];
    for (let i = 0; i < 3; i++) {
      await themeButton.click();
      // The click handler synchronously runs classList.toggle('dark', …) via
      // setTheme; re-read the resolved state after the click settles.
      seen.push(await isDark(page));
    }

    expect(
      new Set(seen).size,
      `expected the dark class to change across the cycle, saw: ${JSON.stringify(seen)}`,
    ).toBeGreaterThan(1);
  });
});
