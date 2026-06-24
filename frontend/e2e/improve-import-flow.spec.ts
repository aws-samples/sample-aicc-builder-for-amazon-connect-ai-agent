import { test, expect, S, pickMode } from './support/fixtures';
import { SAMPLE_CONTACT_FLOW } from './support/data';

/**
 * Improve-Existing import flow + patch-only follow-up.
 *
 * Customer stories covered:
 *  - CS-3.1: Import an existing Contact Flow JSON (scope derived from the .json
 *            extension) and have the backend acknowledge + lint-repair it.
 *  - CS-3.6: After import, request a patch-only edit through the in-chat composer
 *            (a normal sendMessage carrying the natural-language change request).
 */
test.describe('Improve Existing: import Contact Flow + patch edit (CS-3.1, CS-3.6)', () => {
  test('imports a flow.json, shows it in the Assets pane, then sends a patch edit', async ({
    page,
    mock,
    gotoApp,
  }) => {
    await gotoApp();

    // CS-3.1 — choose "Improve Existing" and attach a Contact Flow JSON.
    await pickMode(page, 'improve');
    await page.locator('input[type=file]').setInputFiles({
      name: 'flow.json',
      mimeType: 'application/json',
      buffer: Buffer.from(SAMPLE_CONTACT_FLOW),
    });

    // The staged attachment surfaces as a chip showing the file name.
    await expect(page.getByText('flow.json')).toBeVisible();

    // Start the run. Mark the inbound cursor first so we ignore the initial
    // page-load createNewSession (scope []) the app fires before Start.
    const since = mock.mark();
    await page.getByRole('button', { name: S.startButton }).click();

    // The app rotates to a fresh session. Improve mode derives scope from the
    // .json extension → ['contact_flow'].
    const created = await mock.waitForAction('createNewSession', { since });
    expect(Array.isArray(created.scope)).toBe(true);
    expect(created.scope as string[]).toEqual(['contact_flow']);

    // Simulate the backend import result: first deliver the asset content, then
    // acknowledge the import with a clean lint summary (2 auto-fixes applied).
    mock.deliverAsset({
      assetType: 'contact_flow',
      fileName: 'flow.json',
      language: 'json',
      content: SAMPLE_CONTACT_FLOW,
    });
    mock.assetImported({
      assetType: 'contact_flow',
      fileName: 'flow.json',
      lint: { ok: true, errors: [], warnings: [], fixesApplied: 2 },
    });

    // The right pane auto-switches to Assets (asset_imported flips rightPaneView).
    const assetsBtn = page.getByRole('button', { name: S.tabAssets }).first();
    await expect(assetsBtn).toHaveAttribute('aria-pressed', 'true');

    // The Asset Workspace renders with the Contact Flow tab visible.
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
    await expect(page.getByRole('button', { name: S.segContactFlow }).first()).toBeVisible();

    // The import is summarized in the chat timeline as a system message.
    await expect(page.getByText(/가져오기|Imported/).first()).toBeVisible();

    // CS-3.6 — patch-only edit through the in-chat composer. Once the timeline is
    // non-empty the in-chat textarea is shown; pressing Enter (no shift) sends.
    const editText = '첫 인사만 더 친근하게 바꿔줘';
    const composer = page.getByPlaceholder(/메시지를 입력하세요|Type your message/);
    await expect(composer).toBeVisible();
    // Mark first so we read the patch-edit sendMessage, not the earlier import kickoff.
    const sinceEdit = mock.mark();
    await composer.fill(editText);
    await composer.press('Enter');

    // The edit request is sent as a normal sendMessage carrying the typed text.
    const sent = await mock.waitForAction('sendMessage', { since: sinceEdit });
    expect(String(sent.message)).toContain(editText);
  });
});
