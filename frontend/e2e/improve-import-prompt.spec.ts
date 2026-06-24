// Customer stories covered: CS-3.2 — Improve Existing: import an AI Prompt (YAML),
// scope the run to the prompt lane, surface it in the Asset Workspace, and show the
// lint/import summary in the chat timeline.
//
// Flow under test (verified against ChatWindow.handleStart + useWebSocket "asset_imported"):
//   improve mode + a .yaml attachment → scope ['prompt'] on createNewSession; the YAML is
//   inlined into the kickoff and sent via sendMessage. The backend then streams the asset
//   (asset_preview/asset_complete) and an asset_imported ack carrying the lint summary,
//   which auto-opens the Assets pane and appends a system message to the timeline.
import { test, expect, S, pickMode } from './support/fixtures';
import { SAMPLE_PROMPT_YAML } from './support/data';

test.describe('CS-3.2 — Improve Existing: import an AI Prompt (YAML)', () => {
  test('attaching a YAML in Improve mode scopes to prompt, imports it, and reveals it in the workspace', async ({
    page,
    mock,
    gotoApp,
  }) => {
    await gotoApp();

    // 1) Improve Existing mode.
    await pickMode(page, 'improve');
    await expect(page.getByRole('radio', { name: S.modeImprove })).toHaveAttribute('aria-checked', 'true');

    // 2) Stage the AI Prompt YAML via the hidden file input. The chip should reflect the name.
    await page.locator('input[type=file]').setInputFiles({
      name: 'agent.yaml',
      mimeType: 'application/x-yaml',
      buffer: Buffer.from(SAMPLE_PROMPT_YAML),
    });
    await expect(page.getByText('agent.yaml')).toBeVisible();

    // 3) Start — enabled now that a file is attached. This rotates the session.
    const startBtn = page.getByRole('button', { name: S.startButton });
    await expect(startBtn).toBeEnabled();
    // Mark the inbound cursor so we read the rotated session's createNewSession,
    // not the initial page-load one (scope []).
    const since = mock.mark();
    await startBtn.click();

    // 4) createNewSession carries scope ['prompt'] (derived from the .yaml extension).
    const created = await mock.waitForAction('createNewSession', { since });
    expect(created.scope).toEqual(['prompt']);

    // 5) The kickoff is sent via sendMessage with the YAML inlined as a fenced block.
    const sent = await mock.waitForAction('sendMessage');
    expect(String(sent.message)).toContain('agent.yaml');
    expect(String(sent.message)).toContain('claims-status-agent');

    // 6) Backend delivers the prompt asset, then acknowledges the import (lint clean, 1 auto-fix).
    mock.deliverAsset({ assetType: 'prompt', language: 'yaml', content: SAMPLE_PROMPT_YAML });
    mock.assetImported({
      assetType: 'prompt',
      fileName: 'agent.yaml',
      lint: { ok: true, errors: [], warnings: [], fixesApplied: 1 },
    });

    // 7) The import summary system message lands in the chat timeline (ko-KR default copy).
    await expect(page.getByText('AI 프롬프트 가져오기 및 검증 완료 (자동 수정 1건)')).toBeVisible();

    // 8) asset_imported auto-opens the Assets view. Confirm the workspace + the AI 프롬프트 tab.
    await expect(page.getByRole('button', { name: S.tabAssets }).first()).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();

    // The workspace tab strip uses the exact label "AI 프롬프트"; scope to it to avoid
    // matching the chat-side preview header that may share the same text.
    const promptTab = page.getByRole('button', { name: S.segPrompt }).first();
    await expect(promptTab).toBeVisible();
    await expect(promptTab).toHaveAttribute('aria-pressed', 'true');

    // The empty-state placeholder must be gone now that an asset is present.
    await expect(page.getByText(S.workspaceEmpty)).toHaveCount(0);

    // 9) Clean happy path — no uncaught console errors.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
