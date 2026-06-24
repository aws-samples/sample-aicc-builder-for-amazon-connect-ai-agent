/**
 * Playwright fixtures for the hermetic suite.
 *
 * `test` extends base with a `mock` MockBackend that is installed BEFORE the
 * page navigates, and a `gotoApp` helper that navigates + waits for the app to
 * be interactive (start screen visible). After each test we assert no uncaught
 * console errors slipped through (the qa-scenarios pass criterion #1).
 */
import { test as base, expect, type Page } from '@playwright/test';
import { MockBackend } from './mock-backend';

export const test = base.extend<{ mock: MockBackend; gotoApp: () => Promise<void> }>({
  mock: async ({ page }, use) => {
    const mock = new MockBackend(page);
    await mock.install();
    await use(mock);
  },

  gotoApp: async ({ page, mock }, use) => {
    const go = async () => {
      await page.goto('/');
      // DEV_AUTH_BYPASS makes the app authenticated immediately; the WS connects
      // on mount and flips isLoadingSession=false on open → start screen renders.
      await expect(page.getByRole('heading', { name: /무엇을 만들고 싶으신가요\?|What would you like to build\?/ })).toBeVisible({ timeout: 15_000 });
      // The socket should now be established server-side.
      await expect.poll(() => mock.connected, { timeout: 5_000 }).toBe(true);
    };
    await use(go);
  },
});

export { expect };

/** Centralized, real selectors harvested from the components (bilingual regexes). */
export const S = {
  // Start screen (ChatEmptyState.tsx)
  startHeading: /무엇을 만들고 싶으신가요\?|What would you like to build\?/,
  modeFull: /전체 빌드|Full Build/,
  modeSegment: /단일 세그먼트|Single Segment/,
  modeImprove: /기존 에셋 개선|Improve Existing/,
  segContactFlow: /^Contact Flow$/,
  segPrompt: /AI 프롬프트|AI Prompt/,
  segFaq: /^FAQ$/,
  startButton: /^시작$|^Start$/,

  // Right pane (RightPane.tsx) — tabs are buttons with aria-pressed
  tabProgress: /진행 상황|Progress/,
  tabAssets: /에셋|Assets/,

  // Asset workspace (AssetWorkspace.tsx)
  workspaceHeading: /에셋 워크스페이스|Asset Workspace/,
  workspaceEmpty: /아직 생성된 에셋이 없습니다|No assets generated yet/,

  // Header (Header.tsx)
  modelSelector: /Claude 모델 선택|Select Claude model/,

  // Chat composer (ChatWindow.tsx)
  sendButton: /전송|Send|메시지 보내기/,
};

/** Pick a start mode card on the empty state. Mode cards are role=radio whose
 *  accessible name = title + description, so we match by the leading title text. */
export async function pickMode(page: Page, mode: 'full' | 'segment' | 'improve') {
  const name = mode === 'full' ? S.modeFull : mode === 'segment' ? S.modeSegment : S.modeImprove;
  // The mode cards are the first 3 radios; their names start with the title.
  await page.getByRole('radio', { name }).first().click();
}

/** The segment radios (Contact Flow / AI 프롬프트 / FAQ) use EXACT accessible
 *  names, unlike the mode cards whose descriptions also contain these phrases.
 *  Always select segments with exact:true to avoid strict-mode collisions. */
export const SEGMENT_NAME: Record<'contact_flow' | 'prompt' | 'faq', string> = {
  contact_flow: 'Contact Flow',
  prompt: 'AI 프롬프트',
  faq: 'FAQ',
};

export function segmentRadio(page: Page, seg: 'contact_flow' | 'prompt' | 'faq') {
  return page.getByRole('radio', { name: SEGMENT_NAME[seg], exact: true });
}

export async function pickSegment(page: Page, seg: 'contact_flow' | 'prompt' | 'faq') {
  const radio = segmentRadio(page, seg);
  await radio.click();
  return radio;
}

/** Type the description into the shared composer textarea on the start screen. */
export async function typeDescription(page: Page, text: string) {
  // The start-screen textarea is the only textarea while the empty state shows.
  await page.locator('textarea').first().fill(text);
}

/** Open the right-pane Assets view. The Progress/Assets switchers are buttons
 *  with aria-pressed; scope to the desktop right pane (.first()) since a
 *  collapsed mobile rail can duplicate the control. */
export async function openAssetsPane(page: Page) {
  const btn = page.getByRole('button', { name: S.tabAssets }).first();
  await btn.click();
  await expect(btn).toHaveAttribute('aria-pressed', 'true');
  return btn;
}

export async function openProgressPane(page: Page) {
  const btn = page.getByRole('button', { name: S.tabProgress }).first();
  await btn.click();
  return btn;
}
