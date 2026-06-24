import { test, expect, S, pickMode } from './support/fixtures';
import type { Page } from '@playwright/test';
import type { MockBackend } from './support/mock-backend';
import { SAMPLE_CONTACT_FLOW, tinyPngBuffer } from './support/data';

/** Parsed shape of the messages the app sends the mock backend. */
type InboundMsg = { action?: string; [k: string]: unknown };

/**
 * Customer stories covered:
 *   CS-3.3 — "Improve Existing" from a whiteboard / flow-diagram PHOTO: attach an
 *            image, start a run, and have the generated Contact Flow land in the
 *            Asset Workspace.
 *   CS-3.4 — Graceful failure of the photo path: the model can't read the image,
 *            replies with an apology, and the app keeps working (no crash).
 *
 * Both tests exercise the image-attachment ("whiteboard") import path on the
 * mode-first start screen. An image is a binary attachment, so the app sends it
 * via the multimodal attachment path (sendMessageWithAttachments for small files,
 * or sendMessageWithS3Attachments for large ones). The tiny PNG used here is well
 * under the S3 threshold, but we stay tolerant and accept either action name.
 */
test.describe('CS-3.3 / CS-3.4 — Improve Existing from a flow-diagram photo', () => {
  /** Stage a sketch.png on the Improve start screen and assert the chip shows. */
  async function attachSketch(page: Page) {
    await pickMode(page, 'improve');
    await page.locator('input[type=file]').setInputFiles({
      name: 'sketch.png',
      mimeType: 'image/png',
      buffer: tinyPngBuffer(),
    });
    // AttachmentPreview renders a chip showing the file name.
    await expect(page.getByText('sketch.png')).toBeVisible();
  }

  /**
   * Wait until at least one of the attachment send actions reaches the backend.
   * The image rides sendMessageWithAttachments (base64) or sendMessageWithS3Attachments;
   * be tolerant and accept either. Returns the parsed inbound message.
   */
  async function waitForAnySend(mock: MockBackend, timeout = 10_000): Promise<InboundMsg> {
    const ATTACH_ACTIONS = [
      'sendMessageWithAttachments',
      'sendMessageWithS3Attachments',
      'sendMessage',
    ];
    const start = Date.now();
    for (;;) {
      const hit = [...mock.inbound]
        .reverse()
        .find((m) => ATTACH_ACTIONS.includes(m.action as string));
      if (hit) return hit;
      if (Date.now() - start > timeout) {
        throw new Error(
          `Timed out waiting for a send action. Saw: ${mock.inbound.map((m) => m.action).join(', ')}`,
        );
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  }

  test('CS-3.3: photo import → run starts and the generated Contact Flow appears in Assets', async ({
    page,
    mock,
    gotoApp,
  }) => {
    await gotoApp();
    await attachSketch(page);

    // Start the run. Improve mode is enabled once a file is attached. Mark the
    // inbound cursor so we read the rotated session, not the page-load one (scope []).
    const since = mock.mark();
    await page.getByRole('button', { name: S.startButton }).click();

    // The app rotates to a fresh session first.
    const created = await mock.waitForAction('createNewSession', { since });
    // Improve + image (non-yaml) → scope derived as ['contact_flow'].
    expect(Array.isArray(created.scope)).toBe(true);
    expect(created.scope as string[]).toContain('contact_flow');

    // SOMETHING reaches the backend over the attachment path (tolerant of action name).
    const sent = await waitForAnySend(mock);
    expect(sent.action).toBeTruthy();

    // Happy path: the backend generates a Contact Flow and imports it.
    mock.deliverAsset({
      assetType: 'contact_flow',
      fileName: 'flow.json',
      language: 'json',
      content: SAMPLE_CONTACT_FLOW,
    });
    mock.assetImported({ assetType: 'contact_flow', fileName: 'flow.json' });

    // Open the Assets pane (asset_imported also flips the view, but be explicit).
    await page.getByRole('button', { name: S.tabAssets }).first().click();

    // The Asset Workspace shows with the Contact Flow tab.
    await expect(page.getByRole('heading', { name: S.workspaceHeading })).toBeVisible();
    await expect(page.getByRole('button', { name: S.segContactFlow }).first()).toBeVisible();

    // The Contact Flow renders via ContactFlowPreview (Radix role=tab JSON view always
    // present). It also renders in the chat preview, so each tab can match twice.
    await expect(page.getByRole('tab', { name: /JSON 코드|JSON Code/ }).first()).toBeVisible();

    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });

  test('CS-3.4: photo the model cannot read → graceful apology, app stays alive', async ({
    page,
    mock,
    gotoApp,
  }) => {
    await gotoApp();
    await attachSketch(page);

    await page.getByRole('button', { name: S.startButton }).click();

    // The run still rotates the session and emits a send action.
    await mock.waitForAction('createNewSession');
    await waitForAnySend(mock);

    // The backend cannot extract a flow from the image and apologizes (no error event).
    const apology = '이미지에서 흐름을 읽지 못했어요. 더 선명한 사진을 올려주시거나 직접 설명해 주세요.';
    await mock.streamAssistant(apology);

    // The apology lands in the timeline.
    await expect(page.getByText(/이미지에서 흐름을 읽지 못했어요/)).toBeVisible();

    // The app did not crash: an in-chat composer is available to keep going.
    // Once the timeline is non-empty, the in-chat composer shows with the default placeholder.
    await expect(
      page.getByPlaceholder(/메시지를 입력하세요|Type a message/),
    ).toBeVisible();

    // No uncaught console / page errors even on this graceful-failure path.
    expect(mock.consoleErrors, mock.consoleErrors.join('\n')).toEqual([]);
  });
});
