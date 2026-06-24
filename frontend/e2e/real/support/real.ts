/**
 * Helpers for driving the REAL deployed app (dev) over its real WebSocket.
 *
 * Unlike the hermetic suite, nothing here mocks the backend. We attach a
 * passive TAP to whatever WebSocket the app opens and record the frames the
 * server sends, so tests can assert on the real protocol (connected /
 * session_created / asset_preview / phase_changed / download_ready) and a
 * "customer simulator" can drive the long interview→generation run.
 */
import type { Page, WebSocket } from '@playwright/test';
import { expect } from '@playwright/test';

export const START_HEADING = /무엇을 만들고 싶으신가요\?|What would you like to build\?|何を構築しますか/;

export interface Frame {
  type?: string;
  [k: string]: unknown;
}

/**
 * Taps every WebSocket the page opens and collects parsed server→client frames.
 * Install BEFORE page.goto so the initial socket is captured. The app rotates
 * sockets on session switch; we tap them all and keep a flat, ordered list.
 */
export class WsTap {
  readonly frames: Frame[] = [];
  readonly sockets: string[] = [];
  /** Outbound (client→server) messages, parsed — useful to confirm scope/model sent. */
  readonly sent: Frame[] = [];

  constructor(private page: Page) {}

  install() {
    this.page.on('websocket', (ws: WebSocket) => {
      this.sockets.push(ws.url());
      ws.on('framereceived', (f) => this.push(this.frames, f.payload));
      ws.on('framesent', (f) => this.push(this.sent, f.payload));
    });
    return this;
  }

  private push(arr: Frame[], payload: string | Buffer) {
    try {
      const d = JSON.parse(typeof payload === 'string' ? payload : payload.toString());
      if (d && typeof d === 'object') arr.push(d);
    } catch {
      /* ignore keepalive / non-JSON */
    }
  }

  /** All received frames of a given type. */
  byType(type: string): Frame[] {
    return this.frames.filter((f) => f.type === type);
  }

  /** The most recent received frame of a type (or undefined). */
  last(type: string): Frame | undefined {
    return [...this.frames].reverse().find((f) => f.type === type);
  }

  /** Distinct asset types seen via asset_preview/asset_complete events. */
  assetTypesSeen(): Set<string> {
    const out = new Set<string>();
    for (const f of this.frames) {
      if ((f.type === 'asset_preview' || f.type === 'asset_complete') && f.assetPreview) {
        const at = (f.assetPreview as { assetType?: string }).assetType;
        if (at) out.add(at);
      }
    }
    return out;
  }

  /** Wait until a received frame satisfies the predicate; returns it. */
  async waitForFrame(pred: (f: Frame) => boolean, timeoutMs: number): Promise<Frame> {
    const start = Date.now();
    for (;;) {
      const hit = this.frames.find(pred);
      if (hit) return hit;
      if (Date.now() - start > timeoutMs) {
        throw new Error(
          `Timed out (${timeoutMs}ms) waiting for a frame. Types seen: ${this.frames
            .map((f) => f.type)
            .join(',')}`,
        );
      }
      await this.page.waitForTimeout(250);
    }
  }
}

/** Navigate to the app and wait for the authenticated start screen. Throws a
 *  clear message if we bounced to /login (saved auth expired → re-run auth). */
export async function gotoRealApp(page: Page) {
  await page.goto('/');
  if (/\/login/.test(page.url())) {
    throw new Error(
      'Redirected to /login — saved auth is missing or expired. Run `npm run test:e2e:auth` to re-capture.',
    );
  }
  // Tests share one saved storageState (and thus the per-user session id in
  // localStorage), so a previous run's session can bleed in and load history
  // instead of the start screen. Force a fresh chat so Start-based tests always
  // begin on the mode-first start screen.
  const onStart = await page
    .getByRole('heading', { name: START_HEADING })
    .isVisible()
    .catch(() => false);
  if (!onStart) {
    if (/\/login/.test(page.url())) {
      throw new Error('Saved auth expired (landed on /login). Run `npm run test:e2e:auth`.');
    }
    // Click "새 대화"/New Chat to rotate to a fresh, empty session.
    await page.getByRole('button', { name: /새 대화|New Chat/ }).first().click().catch(() => {});
  }
  await expect(page.getByRole('heading', { name: START_HEADING })).toBeVisible({ timeout: 30_000 }).catch(() => {
    if (/\/login/.test(page.url())) {
      throw new Error('Saved auth expired (landed on /login). Run `npm run test:e2e:auth`.');
    }
    throw new Error('App did not reach the start screen within 30s (a prior session may not have cleared).');
  });
}

/** The in-chat composer textarea (present once the timeline is non-empty). */
export function composer(page: Page) {
  return page.locator('textarea').first();
}

/** Send a chat message via the composer (Enter to submit). Waits until the
 *  composer is editable (it disables while the session loads / agent runs). */
export async function sendChat(page: Page, text: string, timeoutMs = 120_000) {
  const ta = composer(page);
  await expect(ta).toBeEditable({ timeout: timeoutMs });
  await ta.fill(text);
  await ta.press('Enter');
}

/** True if the chat header shows a live connection ("연결됨"/"Connected"). */
export async function isConnected(page: Page): Promise<boolean> {
  return (await page.getByText(/연결됨|Connected/).first().count()) > 0;
}

/** True while the agent is actively producing output (typing indicator / stop button).
 *  IMPORTANT: a disabled composer only counts as "busy" when we're CONNECTED. If
 *  the socket dropped, the composer is disabled because it's broken, not because
 *  the agent is working — treating that as "busy" makes waitForAgentIdle loop
 *  forever (observed: an 80-min hang). */
async function agentBusy(page: Page): Promise<boolean> {
  // The Stop button (title 생성 중지 / Stop generation) shows only while typing.
  const stop = page.getByRole('button', { name: /생성 중지|Stop generation/ });
  if (await stop.count()) return true;
  const ta = composer(page);
  if (await ta.count()) {
    const disabled = await ta.isDisabled().catch(() => false);
    if (disabled && (await isConnected(page))) return true;
  }
  return false;
}

/** Wait until the agent has gone quiet (not busy) for `quietMs`, i.e. it's the
 *  user's turn again. Returns false if it never settles within `maxMs`. */
export async function waitForAgentIdle(page: Page, opts: { quietMs?: number; maxMs?: number } = {}) {
  const quietMs = opts.quietMs ?? 4000;
  const maxMs = opts.maxMs ?? 8 * 60_000;
  const start = Date.now();
  let quietSince: number | null = null;
  for (;;) {
    const busy = await agentBusy(page);
    const now = Date.now();
    if (busy) {
      quietSince = null;
    } else {
      if (quietSince == null) quietSince = now;
      if (now - quietSince >= quietMs) return true;
    }
    if (now - start > maxMs) return false;
    await page.waitForTimeout(1000);
  }
}
