import { test, expect, S, pickMode, typeDescription } from './support/fixtures';
import { SAMPLE_CONTACT_FLOW, SAMPLE_PROMPT_YAML } from './support/data';

/**
 * Customer stories covered:
 *   - CS-5.1  12-step / 4-phase progress tracking for a full build (counter, phase,
 *             scope-aware totals all visible because full build keeps all 12 lanes).
 *   - CS-1.3  Assets stream into the Asset Workspace and surface as per-type tabs.
 *
 * These exercise the Progress pane counter ("X / 12 완료") incrementing as
 * progress_update events arrive, and the Asset Workspace gaining a tab per
 * delivered asset type (Lambda, Contact Flow, AI 프롬프트).
 *
 * Every test starts from a started full build so scope = [] (totalCount = 12).
 */
test.describe('Full build — 12-step progress + asset streaming (CS-5.1, CS-1.3)', () => {
  /** Start a full build and wait until the kickoff has been sent + acked. */
  async function startFullBuild(page: Parameters<typeof typeDescription>[0], mock: { waitForAction: (a: string) => Promise<unknown> }) {
    await pickMode(page, 'full');
    await typeDescription(page, 'A claims-status voice agent for an insurance contact center.');
    await page.getByRole('button', { name: S.startButton }).click();

    // Session rotation: full build → createNewSession with empty scope, then kickoff.
    const created = (await mock.waitForAction('createNewSession')) as { scope?: unknown };
    expect(Array.isArray(created.scope)).toBe(true);
    expect((created.scope as string[]).length).toBe(0);
    await mock.waitForAction('sendMessage');
  }

  test('Progress pane shows the 12-step counter starting at 0 / 12 (CS-5.1)', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Progress is the default right-pane view, but click it to be explicit/robust.
    const progressTab = page.getByRole('button', { name: S.tabProgress }).first();
    await progressTab.click();
    await expect(progressTab).toHaveAttribute('aria-pressed', 'true');

    // Full build → all 12 in-scope steps, none complete yet.
    await expect(page.getByText(/0 \/ 12 (완료|complete)/)).toBeVisible();

    // No out-of-scope partition for a full build (everything runs).
    await expect(
      page.getByText(/이번 실행에 포함되지 않음|Not in this run/),
    ).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('progress_update events advance the counter and phase across the 4 phases (CS-5.1)', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    await page.getByRole('button', { name: S.tabProgress }).first().click();
    await expect(page.getByText(/0 \/ 12 (완료|complete)/)).toBeVisible();

    // Phase 1 → interview lanes complete.
    mock.progress('database', 'in_progress', 50);
    await expect(page.getByText(/0 \/ 12 (완료|complete)/)).toBeVisible();
    mock.progress('database', 'completed', 100);
    await expect(page.getByText(/1 \/ 12 (완료|complete)/)).toBeVisible();

    // Enter generation and complete several generation lanes.
    mock.phaseChanged('generation', 'interview');
    mock.progress('lambda', 'completed', 100);
    mock.progress('prompt', 'completed', 100);
    mock.progress('contact_flow', 'completed', 100);

    // Counter should now reflect 4 completed (database + 3 generation lanes).
    await expect(page.getByText(/[1-9] \/ 12 (완료|complete)/)).toBeVisible();
    await expect(page.getByText(/4 \/ 12 (완료|complete)/)).toBeVisible();

    // Still 12 total (full build never trims lanes).
    await expect(page.getByText(/\/ 12 (완료|complete)/)).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('delivered assets surface as per-type tabs in the Asset Workspace (CS-1.3)', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    // Drive into generation, then stream three complete assets.
    mock.phaseChanged('generation', 'interview');
    mock.subagentProgress('lambda_generator', 'completed');

    mock.deliverAsset({
      assetType: 'lambda',
      operationId: 'getClaimStatus',
      fileName: 'handler.py',
      language: 'python',
      content: 'def handler(event, context):\n    return {"statusCode": 200}\n',
    });
    mock.deliverAsset({
      assetType: 'contact_flow',
      fileName: 'contact_flow.json',
      language: 'json',
      content: SAMPLE_CONTACT_FLOW,
    });
    mock.deliverAsset({
      assetType: 'prompt',
      fileName: 'ai_agent_prompt.yaml',
      language: 'yaml',
      content: SAMPLE_PROMPT_YAML,
    });

    // Open the Assets pane (asset_preview sets activeAssetKey but does not force
    // the pane open — click the Assets switch).
    const assetsTab = page.getByRole('button', { name: S.tabAssets }).first();
    await assetsTab.click();
    await expect(assetsTab).toHaveAttribute('aria-pressed', 'true');

    // Workspace heading + tabs for each delivered asset type.
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Lambda$/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Contact Flow$/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /AI 프롬프트|AI Prompt/ })).toBeVisible();

    // Empty-state text must be gone now that assets exist.
    await expect(page.getByText(S.workspaceEmpty)).toHaveCount(0);

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('clicking the Contact Flow tab renders the JSON/Diagram preview (CS-1.3)', async ({ page, mock, gotoApp }) => {
    await gotoApp();
    await startFullBuild(page, mock);

    mock.phaseChanged('generation', 'interview');
    mock.deliverAsset({
      assetType: 'contact_flow',
      fileName: 'contact_flow.json',
      language: 'json',
      content: SAMPLE_CONTACT_FLOW,
    });

    await page.getByRole('button', { name: S.tabAssets }).first().click();

    const flowTab = page.getByRole('button', { name: /^Contact Flow$/ });
    await flowTab.click();
    await expect(flowTab).toHaveAttribute('aria-pressed', 'true');

    // ContactFlowPreview exposes Radix role=tab views; JSON parses → Diagram shows.
    // The flow renders in both the chat preview and the workspace, so each tab can
    // match twice — assert on the workspace instance (.first()).
    await expect(page.getByRole('tab', { name: /다이어그램|Diagram/ }).first()).toBeVisible();
    await expect(page.getByRole('tab', { name: /JSON 코드|JSON Code/ }).first()).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
