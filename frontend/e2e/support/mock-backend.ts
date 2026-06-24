/**
 * Hermetic mock backend for AICC Builder E2E.
 *
 * Drives the REAL React app while standing in for the Python/WebSocket backend:
 *  - WebSocket (`/ws`): intercepted with page.routeWebSocket. The mock acts as the
 *    server — it never connects to a real socket. It auto-answers the handshake
 *    actions the app emits (createNewSession → session_created, ping → pong,
 *    injectHistory → history_injected) and lets tests script any backend event
 *    (stream, asset_preview, progress_update, phase_changed, download_ready, …).
 *  - REST (`/api/**`, `/ping`): answered with safe empties so nothing hits cloud.
 *
 * Outbound event shapes mirror frontend/src/hooks/useWebSocket.ts exactly (the
 * `handleMessage` switch) and frontend/src/types/index.ts (WebSocketMessage).
 */
import type { Page, WebSocketRoute } from '@playwright/test';

export interface AssetPreviewEvent {
  assetType: string;
  operationId?: string;
  fileName?: string;
  content: string;
  isComplete: boolean;
  language?: string;
  isDelta?: boolean;
  s3Key?: string;
  messageIndex?: number;
}

type InboundMsg = { action?: string; [k: string]: unknown };
type ActionHandler = (msg: InboundMsg, mock: MockBackend) => void | Promise<void>;

export class MockBackend {
  private route: WebSocketRoute | null = null;
  private handlers = new Map<string, ActionHandler>();
  /** Every message the app sent us (server-inbound), parsed. */
  readonly inbound: InboundMsg[] = [];
  /** Console errors collected from the page (for the "no uncaught errors" gate). */
  readonly consoleErrors: string[] = [];
  /** Last sessionId the app asked us to create / connect. */
  lastSessionId: string | null = null;

  constructor(private page: Page) {}

  /** Wire up WS + REST interception. Must run before page.goto(). */
  async install() {
    // Collect console errors (excluding known dev noise per qa-scenarios pass criteria).
    this.page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const t = m.text();
      if (/\/history\b|cognito|localhost:8080|Failed to load resource|favicon/i.test(t)) return;
      this.consoleErrors.push(t);
    });
    this.page.on('pageerror', (e) => this.consoleErrors.push(`pageerror: ${e.message}`));

    // REST: safe empties. The app's session service already early-returns when
    // VITE_SESSION_API_URL is empty, so these cover only same-origin /api + /ping.
    await this.page.route(/\/api\//, async (r) => {
      const url = r.request().url();
      let body: unknown = {};
      if (/\/workspace\/.+\/tree/.test(url)) body = { tree: [] };
      else if (/\/message-log\//.test(url)) body = { entries: [], isAgentActive: false };
      else if (/\/debug\/nfs/.test(url)) body = { ok: true };
      await r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await this.page.route(/\/ping\b/, (r) =>
      r.fulfill({ status: 200, contentType: 'text/plain', body: 'ok' }),
    );

    // WebSocket: become the server. Match the app's wss?://host/ws?token=…&sessionId=…
    await this.page.routeWebSocket(/\/ws(\?|$)/, (ws) => {
      this.route = ws;
      const url = new URL(ws.url());
      this.lastSessionId = url.searchParams.get('sessionId');

      ws.onMessage((raw) => {
        let msg: InboundMsg;
        try {
          msg = JSON.parse(typeof raw === 'string' ? raw : raw.toString());
        } catch {
          return;
        }
        this.inbound.push(msg);
        const action = (msg.action as string) || '';
        const custom = this.handlers.get(action);
        if (custom) {
          void custom(msg, this);
          return;
        }
        this.defaultHandle(action, msg);
      });
    });
  }

  /** Default auto-answers for the handshake/keepalive actions. */
  private defaultHandle(action: string, msg: InboundMsg) {
    switch (action) {
      case 'createNewSession':
        // Echo scope + model exactly as the app expects (session_created handler).
        this.sessionCreated({
          scope: Array.isArray(msg.scope) ? (msg.scope as string[]) : [],
          model: typeof msg.model === 'string' ? (msg.model as string) : undefined,
        });
        break;
      case 'injectHistory':
        this.send({ type: 'history_injected', success: true, injectedCount: 0, phase: 'interview' });
        break;
      case 'ping':
        this.send({ type: 'pong' });
        break;
      // sendMessage / sendMessageWithAttachments / importAsset / downloadAssets /
      // getProgress / getHistory / cancelGeneration: tests opt in via on()/helpers.
      default:
        break;
    }
  }

  /** Register/override a handler for an inbound action. */
  on(action: string, handler: ActionHandler) {
    this.handlers.set(action, handler);
    return this;
  }

  /** Raw server→client send. */
  send(event: Record<string, unknown>) {
    if (!this.route) throw new Error('WebSocket not connected yet — await appReady(page) first');
    this.route.send(JSON.stringify(event));
  }

  get connected() {
    return this.route !== null;
  }

  /** Current inbound message count — use as a marker before an interaction so a
   *  later waitForAction({ since }) ignores actions sent earlier (e.g. the
   *  initial page-load createNewSession the app fires before the user clicks Start). */
  mark(): number {
    return this.inbound.length;
  }

  /**
   * Wait until the app has sent an action. Options:
   *  - since: only consider inbound[index >= since] (ignore earlier sends).
   *  - where: predicate the matching message must satisfy.
   *  - timeout: ms.
   * Backward compatible: waitForAction('foo') and waitForAction('foo', 5000) work.
   * Returns the FIRST match at/after `since` (chronological) so it reflects the
   * action the just-performed interaction caused.
   */
  async waitForAction(
    action: string,
    opts: number | { timeout?: number; since?: number; where?: (m: InboundMsg) => boolean } = {},
  ): Promise<InboundMsg> {
    const o = typeof opts === 'number' ? { timeout: opts } : opts;
    const timeout = o.timeout ?? 10_000;
    const since = o.since ?? 0;
    const where = o.where;
    const start = Date.now();
    for (;;) {
      const hit = this.inbound
        .slice(since)
        .find((m) => m.action === action && (!where || where(m)));
      if (hit) return hit;
      if (Date.now() - start > timeout) {
        throw new Error(
          `Timed out waiting for inbound action "${action}"${since ? ` since #${since}` : ''}. Saw: ${this.inbound
            .map((m, i) => `${i}:${m.action}`)
            .join(', ')}`,
        );
      }
      await this.page.waitForTimeout(50);
    }
  }

  // ── Scenario helpers (server→client events) ──────────────────────────────
  sessionCreated(opts: { scope?: string[]; model?: string; phase?: string } = {}) {
    this.send({
      type: 'session_created',
      sessionId: this.lastSessionId,
      phase: opts.phase ?? 'interview',
      scope: opts.scope ?? [],
      ...(opts.model ? { selectedModel: opts.model } : {}),
    });
  }

  connectedEvent(opts: { scope?: string[]; model?: string; phase?: string } = {}) {
    this.send({
      type: 'connected',
      sessionId: this.lastSessionId,
      phase: opts.phase ?? 'interview',
      ...(opts.scope ? { scope: opts.scope } : {}),
      ...(opts.model ? { selectedModel: opts.model } : {}),
    });
  }

  typing(on = true) {
    this.send({ type: 'typing', status: on ? 'start' : 'stop' });
  }

  /** Stream an assistant message in chunks, then stream_end. */
  async streamAssistant(text: string, opts: { messageId?: string; chunk?: number } = {}) {
    const messageId = opts.messageId ?? `m-${this.inbound.length}-${text.length}`;
    const size = opts.chunk ?? Math.max(8, Math.ceil(text.length / 4));
    for (let i = 0; i < text.length; i += size) {
      this.send({ type: 'stream', content: text.slice(i, i + size), message_id: messageId });
      await this.page.waitForTimeout(15);
    }
    this.send({ type: 'stream_end', message_id: messageId });
  }

  /** A single complete assistant message (no streaming). */
  message(content: string, role: 'assistant' | 'user' = 'assistant') {
    this.send({ type: 'message', role, content });
  }

  systemMessage(content: string) {
    this.send({ type: 'message', role: 'assistant', content });
  }

  phaseChanged(phase: string, previousPhase?: string) {
    this.send({ type: 'phase_changed', phase, ...(previousPhase ? { previousPhase } : {}) });
  }

  progress(itemId: string, status: 'pending' | 'in_progress' | 'completed', progressPercent?: number) {
    this.send({ type: 'progress_update', itemId, status, ...(progressPercent != null ? { progressPercent } : {}) });
  }

  toolStart(tool: string, toolUseId: string, input: Record<string, unknown> = {}) {
    this.send({ type: 'tool_start', tool, toolUseId, input });
  }

  toolEnd(tool: string, toolUseId: string, result: unknown = 'ok') {
    this.send({ type: 'tool_end', tool, toolUseId, result, status: 'completed' });
  }

  subagentProgress(subagent: string, status: 'started' | 'running' | 'completed' | 'error', content?: string) {
    this.send({ type: 'subagent_progress', subagent, status, ...(content ? { content } : {}) });
  }

  /** Stream one asset into the Asset Workspace (preview + complete). */
  assetPreview(p: AssetPreviewEvent) {
    this.send({ type: 'asset_preview', assetPreview: p });
  }

  assetComplete(p: AssetPreviewEvent) {
    this.send({ type: 'asset_complete', assetPreview: { ...p, isComplete: true } });
  }

  /** Convenience: deliver a complete asset in one call. */
  deliverAsset(p: Omit<AssetPreviewEvent, 'isComplete'>) {
    this.assetPreview({ ...p, isComplete: true });
  }

  assetImported(opts: {
    assetType: 'contact_flow' | 'prompt';
    fileName?: string;
    operationId?: string;
    lint?: { ok: boolean; errors: string[]; warnings: string[]; fixesApplied: number };
    phase?: string;
  }) {
    this.send({
      type: 'asset_imported',
      assetType: opts.assetType,
      fileName: opts.fileName ?? '',
      operationId: opts.operationId ?? 'imported',
      lint: opts.lint ?? { ok: true, errors: [], warnings: [], fixesApplied: 0 },
      phase: opts.phase ?? 'post_generation',
    });
  }

  downloadReady(downloadUrl: string, opts: { expiresAt?: string; s3Key?: string } = {}) {
    this.send({
      type: 'download_ready',
      downloadUrl,
      expiresAt: opts.expiresAt ?? null,
      s3Key: opts.s3Key ?? 'mock/key.zip',
    });
  }

  error(content: string, debug?: Record<string, unknown>) {
    this.send({ type: 'error', content, ...(debug ? { debug } : {}) });
  }

  /** Simulate the backend dropping the socket (resilience tests). */
  async dropConnection(code = 1006) {
    if (this.route) await this.route.close({ code });
    this.route = null;
  }
}
