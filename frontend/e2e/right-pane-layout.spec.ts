/**
 * Right-pane layout & view switching — CS-5.4, CS-5.5
 *
 * CS-5.4: The right pane offers two peer views (Progress · Assets) that the
 *         builder can switch between; each is a button with aria-pressed.
 * CS-5.5: Switching views preserves view state — a delivered asset's tab stays
 *         put when the builder flips Progress ↔ Assets.
 *
 * The right pane (RightPane.tsx) defaults to the Progress view, which renders a
 * "X / N 완료" completion counter (full build → N = 12). The Assets view hosts
 * the Asset Workspace (heading "에셋 워크스페이스") — empty until an asset arrives.
 *
 * NOTE on selectors: the "진행 상황" label also appears on the inner
 * ProgressSidebar tab, but the RightPane view-switch buttons come FIRST in DOM
 * order, so .first() resolves to the switcher (matching the proven sibling
 * specs full-build-progress.spec.ts / asset-workspace-stream.spec.ts).
 */
import type { Page } from '@playwright/test';
import { test, expect, S, pickMode, typeDescription } from './support/fixtures';
import type { MockBackend } from './support/mock-backend';
import { SAMPLE_CONTACT_FLOW } from './support/data';

// The two RightPane view-switch buttons (first match wins over inner duplicates).
const progressSwitch = (page: Page) => page.getByRole('button', { name: S.tabProgress }).first();
const assetsSwitch = (page: Page) => page.getByRole('button', { name: S.tabAssets }).first();

// Progress completion counter for a full build: "X / 12 완료" / "X / 12 complete".
const fullBuildCounter = /\d+ \/ 12 (완료|complete)/;

/** Start a full build so a live session (and the right pane) is mounted. */
async function startFullBuild(page: Page, mock: MockBackend) {
  await pickMode(page, 'full');
  await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
  await page.getByRole('button', { name: S.startButton }).click();

  // Session rotates (scope [] for full build) then the kickoff message is sent.
  const created = await mock.waitForAction('createNewSession');
  expect((created.scope as string[]).length).toBe(0);
  await mock.waitForAction('sendMessage');

  // Stream a reply so the chat timeline is non-empty and the right pane is live.
  await mock.streamAssistant('Great — let me ask a few questions about your contact center.');
  await expect(page.getByText(/let me ask a few questions/i)).toBeVisible();
}

test.describe('right-pane layout & view switching (CS-5.4, CS-5.5)', () => {
  test('CS-5.4: defaults to Progress, switches to Assets and back', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Progress is the default view: the switch is pressed and the counter shows.
    await expect(progressSwitch(page)).toHaveAttribute('aria-pressed', 'true');
    await expect(assetsSwitch(page)).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByText(fullBuildCounter)).toBeVisible();

    // Switch to Assets → Assets pressed, Progress not; workspace heading + empty state show.
    await assetsSwitch(page).click();
    await expect(assetsSwitch(page)).toHaveAttribute('aria-pressed', 'true');
    await expect(progressSwitch(page)).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
    await expect(page.getByText(S.workspaceEmpty)).toBeVisible();
    // The counter belongs to the Progress view, so it should no longer be mounted.
    await expect(page.getByText(fullBuildCounter)).toHaveCount(0);

    // Switch back to Progress → counter returns, workspace heading gone.
    await progressSwitch(page).click();
    await expect(progressSwitch(page)).toHaveAttribute('aria-pressed', 'true');
    await expect(assetsSwitch(page)).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByText(fullBuildCounter)).toBeVisible();
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-5.5: a delivered asset tab survives Progress ↔ Assets switching', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Backend delivers a complete contact-flow asset.
    mock.deliverAsset({
      assetType: 'contact_flow',
      operationId: 'check-claim-status',
      fileName: 'contact_flow.json',
      language: 'json',
      content: SAMPLE_CONTACT_FLOW,
    });

    // Open the Assets pane (asset_preview sets activeAssetKey but doesn't force the pane open).
    await assetsSwitch(page).click();
    await expect(assetsSwitch(page)).toHaveAttribute('aria-pressed', 'true');

    // The Contact Flow asset tab is present and selected.
    const contactFlowTab = page.getByRole('button', { name: S.segContactFlow });
    await expect(contactFlowTab).toBeVisible();
    await expect(contactFlowTab).toHaveAttribute('aria-pressed', 'true');
    // The workspace is no longer empty.
    await expect(page.getByText(S.workspaceEmpty)).toHaveCount(0);

    // Flip to Progress and back to Assets — the tab state must be preserved.
    await progressSwitch(page).click();
    await expect(progressSwitch(page)).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText(fullBuildCounter)).toBeVisible();

    await assetsSwitch(page).click();
    await expect(assetsSwitch(page)).toHaveAttribute('aria-pressed', 'true');

    // Contact Flow tab is STILL there and still active after the round-trip.
    await expect(contactFlowTab).toBeVisible();
    await expect(contactFlowTab).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
