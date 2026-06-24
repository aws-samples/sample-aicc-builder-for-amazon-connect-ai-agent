/**
 * Asset Workspace streaming — "see it, not just a toast".
 *
 * Customer stories covered:
 *   CS-1.3  — Generated assets stream into the right-pane Asset Workspace where
 *             the builder can actually open and read them (per asset type).
 *   CS-5.4  — Multiple files of the same asset type (e.g. one Lambda per
 *             operation) are individually selectable via a per-file sub-selector.
 *
 * These are HAPPY-PATH tests: no error events are emitted, so we assert that the
 * page produced no uncaught console errors at the end.
 */
import { test, expect, S, pickMode, typeDescription } from './support/fixtures';
import type { Page, Locator } from '@playwright/test';

/** Scope to the Asset Workspace container (the right pane). Assets render via the
 *  SAME AssetPreviewBubble in both the chat timeline AND the workspace, so marker
 *  text matches twice — restrict marker assertions to the workspace root. */
function workspace(page: Page): Locator {
  return page
    .getByRole('heading', { name: S.workspaceHeading })
    .locator('xpath=ancestor::div[contains(@class,"flex-col")][1]');
}

// Short, single-screen asset bodies so the streamed text is visible in the
// workspace's collapsed preview (Lambda preview = first 8 lines, OpenAPI = 10)
// without needing to expand the panel.
const LAMBDA_PY = [
  'import json',
  '',
  'def handler(event, context):',
  '    # AICC_LAMBDA_MARKER reservationId lookup',
  '    return {"statusCode": 200, "body": json.dumps({"ok": True})}',
].join('\n');

const OPENAPI_YAML = [
  'openapi: 3.0.1',
  'info:',
  '  title: AICC_OPENAPI_MARKER reservation-api',
  '  version: 1.0.0',
  'paths:',
  '  /reservations:',
  '    get:',
  '      operationId: listReservations',
].join('\n');

/** Click the right-pane "에셋"/Assets view-switch button and confirm it's pressed. */
async function openAssetsPane(page: import('@playwright/test').Page) {
  const assetsBtn = page.getByRole('button', { name: S.tabAssets }).first();
  await assetsBtn.click();
  await expect(assetsBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
}

/** Start a full build so the main 3-pane layout (incl. right pane) is mounted. */
async function startFullBuild(page: import('@playwright/test').Page, mock: import('./support/mock-backend').MockBackend) {
  await pickMode(page, 'full');
  await typeDescription(page, 'A reservation-management voice agent for a hotel contact center.');
  await page.getByRole('button', { name: S.startButton }).click();

  const created = await mock.waitForAction('createNewSession');
  expect((created.scope as string[]) ?? []).toEqual([]);
  await mock.waitForAction('sendMessage');
  // A streamed reply makes the timeline non-empty so the chat + right pane render.
  await mock.streamAssistant('Let me ask a few questions about your contact center.');
  await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();
}

test.describe('CS-1.3 / CS-5.4 — assets stream into the Asset Workspace', () => {
  test('Assets pane starts empty before any asset is delivered', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    await openAssetsPane(page);

    // Nothing delivered yet → the empty-state copy is shown.
    await expect(page.getByText(S.workspaceEmpty)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('a Lambda and an OpenAPI asset each get their own tab and render their content', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Deliver two distinct asset types (complete in one shot).
    mock.deliverAsset({ assetType: 'lambda', fileName: 'handler.py', language: 'python', content: LAMBDA_PY });
    mock.deliverAsset({ assetType: 'openapi', fileName: 'openapi.yaml', language: 'yaml', content: OPENAPI_YAML });

    await openAssetsPane(page);

    // Both asset-type tabs are present (buttons with aria-pressed in the tab strip).
    const lambdaTab = page.getByRole('button', { name: /^Lambda$/ });
    const openapiTab = page.getByRole('button', { name: /^OpenAPI$/ });
    await expect(lambdaTab).toBeVisible();
    await expect(openapiTab).toBeVisible();

    // The empty-state copy is gone now that a tab exists.
    await expect(page.getByText(S.workspaceEmpty)).toHaveCount(0);

    // Select the Lambda tab → its streamed content is readable in the body.
    // Scope marker assertions to the workspace (the chat bubble also renders them).
    const ws = workspace(page);
    await lambdaTab.click();
    await expect(lambdaTab).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText('handler.py').first()).toBeVisible();
    await expect(ws.getByText(/AICC_LAMBDA_MARKER/)).toBeVisible();

    // Switch to the OpenAPI tab → it shows its own distinct content.
    await openapiTab.click();
    await expect(openapiTab).toHaveAttribute('aria-pressed', 'true');
    await expect(lambdaTab).toHaveAttribute('aria-pressed', 'false');
    await expect(ws.getByText(/AICC_OPENAPI_MARKER/)).toBeVisible();
    // The OpenAPI body is active in the workspace, not the Lambda body.
    await expect(ws.getByText(/AICC_LAMBDA_MARKER/)).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('two Lambda files share one tab and expose a per-file sub-selector', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Same assetType, different fileName → two separate previews under one tab.
    mock.deliverAsset({
      assetType: 'lambda',
      fileName: 'create_reservation.py',
      language: 'python',
      content: 'def handler(event, context):\n    return "AICC_CREATE_MARKER"\n',
    });
    mock.deliverAsset({
      assetType: 'lambda',
      fileName: 'cancel_reservation.py',
      language: 'python',
      content: 'def handler(event, context):\n    return "AICC_CANCEL_MARKER"\n',
    });

    await openAssetsPane(page);

    // Exactly one Lambda asset-type tab (the two files collapse into it).
    const lambdaTab = page.getByRole('button', { name: /^Lambda/ });
    await expect(lambdaTab).toBeVisible();
    await lambdaTab.click();

    // The per-file mono sub-selector exposes BOTH file names as pickable chips.
    const fileOne = page.getByRole('button', { name: 'create_reservation.py' });
    const fileTwo = page.getByRole('button', { name: 'cancel_reservation.py' });
    await expect(fileOne).toBeVisible();
    await expect(fileTwo).toBeVisible();

    // Pick each file and confirm the body swaps to that file's content. Scope marker
    // assertions to the workspace (the chat timeline renders the same previews).
    const ws = workspace(page);
    await fileTwo.click();
    await expect(fileTwo).toHaveAttribute('aria-pressed', 'true');
    await expect(ws.getByText(/AICC_CANCEL_MARKER/)).toBeVisible();

    await fileOne.click();
    await expect(fileOne).toHaveAttribute('aria-pressed', 'true');
    await expect(fileTwo).toHaveAttribute('aria-pressed', 'false');
    await expect(ws.getByText(/AICC_CREATE_MARKER/)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
