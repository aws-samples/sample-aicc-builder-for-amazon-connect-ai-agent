/**
 * Asset fullscreen "browse all assets" + FAQ labeling.
 *
 * Two UI fixes verified here (hermetic):
 *  1. FAQ docs all stream with the SAME synthetic operationId 'knowledge_base'
 *     but distinct fileNames. The per-file selector must show the FILENAME
 *     (06_breakfast.md, …), not "knowledge_base" repeated for every doc.
 *  2. The Expand/fullscreen view is a two-pane browser: a left list of ALL
 *     assets grouped by type, each file clickable, so the user can switch
 *     between assets without closing the modal (previously it was locked to
 *     the single asset that was expanded).
 *
 * Happy path: no error events → assert no uncaught console errors at the end.
 */
import { test, expect, S, pickMode, typeDescription } from './support/fixtures';
import type { Page, Locator } from '@playwright/test';
import type { MockBackend } from './support/mock-backend';

/** Scope to the right-pane Asset Workspace (assets also render in the chat). */
function workspace(page: Page): Locator {
  return page
    .getByRole('heading', { name: S.workspaceHeading })
    .locator('xpath=ancestor::div[contains(@class,"flex-col")][1]');
}

/** The fullscreen modal portal (rendered at document.body, z-[9999]). */
function modal(page: Page): Locator {
  return page.locator('div.fixed.inset-0.z-\\[9999\\]');
}

/** The modal's right-hand content pane (the selected asset). */
function modalContent(page: Page): Locator {
  return page.getByTestId('asset-fullscreen-content');
}

/** The modal's left-hand asset list (all assets, grouped by type). */
function modalNav(page: Page): Locator {
  return page.getByTestId('asset-fullscreen-nav');
}

const FAQ_FILES = [
  { fileName: '06_breakfast.md', content: '# 조식 안내\n조식은 6:30~10:00, 2층 레스토랑입니다.\nFAQ_BREAKFAST_MARKER\n' },
  { fileName: '07_parking.md', content: '# 주차 안내\n발렛 주차 가능, 1박당 2만원.\nFAQ_PARKING_MARKER\n' },
  { fileName: '08_wifi.md', content: '# 와이파이\n전 객실 무료 와이파이.\nFAQ_WIFI_MARKER\n' },
];

const LAMBDA_PY = 'def handler(event, context):\n    return "FAQ_SPEC_LAMBDA_MARKER"\n';

async function openAssetsPane(page: Page) {
  const btn = page.getByRole('button', { name: S.tabAssets }).first();
  await btn.click();
  await expect(btn).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
}

async function startFullBuild(page: Page, mock: MockBackend) {
  await pickMode(page, 'full');
  await typeDescription(page, 'A reservation-management voice agent for a hotel contact center.');
  await page.getByRole('button', { name: S.startButton }).click();
  await mock.waitForAction('createNewSession');
  await mock.waitForAction('sendMessage');
  await mock.streamAssistant('Let me ask a few questions about your contact center.');
  await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();
}

/** Deliver several FAQ docs (all operationId 'knowledge_base') + one Lambda. */
function deliverHotelAssets(mock: MockBackend) {
  for (const f of FAQ_FILES) {
    mock.deliverAsset({
      assetType: 'faq',
      operationId: 'knowledge_base',
      fileName: f.fileName,
      language: 'markdown',
      content: f.content,
    });
  }
  mock.deliverAsset({ assetType: 'lambda', fileName: 'identify_customer.py', language: 'python', content: LAMBDA_PY });
}

test.describe('FAQ labels + fullscreen browse', () => {
  test('FAQ per-file selector shows filenames, not repeated "knowledge_base"', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);
    deliverHotelAssets(mock);
    await openAssetsPane(page);

    // Open the FAQ tab.
    const faqTab = page.getByRole('button', { name: /^FAQ/ });
    await expect(faqTab).toBeVisible();
    await faqTab.click();

    const ws = workspace(page);
    // Each FAQ file is a distinct, pickable chip BY FILENAME.
    for (const f of FAQ_FILES) {
      await expect(page.getByRole('button', { name: f.fileName })).toBeVisible();
    }
    // The bug was: every chip read "knowledge_base". There must be NO chip
    // labeled knowledge_base anymore.
    await expect(page.getByRole('button', { name: 'knowledge_base' })).toHaveCount(0);
    // And the asset header must NOT show the "(knowledge_base)" operationId parenthetical.
    await expect(ws.getByText('(knowledge_base)')).toHaveCount(0);

    // Picking a chip swaps the body to that file's content. FAQ bubbles are
    // "title-only" (collapsed by default) — expand to reveal the markdown.
    await page.getByRole('button', { name: '07_parking.md' }).click();
    await ws.getByRole('button', { name: /펼치기|Expand/ }).first().click();
    await expect(ws.getByText(/FAQ_PARKING_MARKER/)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('Expand opens a two-pane browser; user switches assets without closing', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);
    deliverHotelAssets(mock);
    await openAssetsPane(page);

    // Open the FAQ tab and expand it (the fullscreen control is in the workspace header).
    await page.getByRole('button', { name: /^FAQ/ }).click();
    await page.getByRole('button', { name: /전체 화면|Fullscreen/ }).click();

    const m = modal(page);
    await expect(m).toBeVisible();
    const nav = modalNav(page);
    const content = modalContent(page);

    // The left list shows BOTH families (FAQ + Lambda) — proof it's a browser,
    // not a single-asset view. Every FAQ file appears as its own item.
    await expect(nav.getByRole('button', { name: 'identify_customer.py' })).toBeVisible();
    for (const f of FAQ_FILES) {
      await expect(nav.getByRole('button', { name: f.fileName })).toBeVisible();
    }

    // Switch to the Lambda asset from WITHIN the modal — no close needed. Lambda
    // renders in "preview" mode so its first lines are visible immediately.
    await nav.getByRole('button', { name: 'identify_customer.py' }).click();
    await expect(content.getByText(/FAQ_SPEC_LAMBDA_MARKER/)).toBeVisible();

    // Switch to a FAQ doc, still inside the modal. FAQ is "title-only" — expand
    // to reveal its body, proving the content pane swapped to the FAQ asset.
    await nav.getByRole('button', { name: '08_wifi.md' }).click();
    await content.getByRole('button', { name: /펼치기|Expand/ }).first().click();
    await expect(content.getByText(/FAQ_WIFI_MARKER/)).toBeVisible();
    // And the Lambda content is no longer in the pane.
    await expect(content.getByText(/FAQ_SPEC_LAMBDA_MARKER/)).toHaveCount(0);

    // Capture for visual review (attached to the HTML report).
    await m.screenshot({ path: 'test-results/asset-fullscreen-browse.png' });

    // Escape closes it.
    await page.keyboard.press('Escape');
    await expect(m).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
