/**
 * REAL deployed dev app — FULL BUILD end-to-end (long, real Bedrock).
 *
 * Drives a complete interview → 6-asset generation against the live dev backend,
 * acting as the "customer". Because this is a real LLM conversation (not a fixed
 * script), the customer is RESPONSIVE: each turn we reconstruct what the agent
 * actually said from the WebSocket frames, and a Customer brain (Claude Haiku,
 * with a heuristic fallback) answers THAT message as a concrete persona
 * (ABC 호텔). The agent runs one phase per turn and confirms between phases
 * (system_prompt.py Rule 8b), so the customer answers questions during the
 * interview and approves "proceed" during generation.
 *
 * Everything is recorded to test-results/transcripts/<name>.{md,json} so the
 * dialogue can be reviewed — this is a conversation with an LLM, so seeing what
 * was said matters as much as the pass/fail.
 *
 * Assertions check the run PROGRESSES and PRODUCES assets, never exact LLM
 * wording (non-deterministic). Customer stories: CS-1.1..CS-1.4, CS-5.1, CS-5.3.
 */
import { test, expect } from '@playwright/test';
import { gotoRealApp, WsTap, sendChat, waitForAgentIdle, composer } from './support/real';
import { Customer, PERSONA } from './support/customer';
import { Transcript } from './support/transcript';

const ASSET_FAMILIES = ['lambda', 'openapi', 'prompt', 'contact_flow', 'cdk', 'cloudformation', 'faq', 'knowledge_base'];

test.describe('real dev — full build', () => {
  test('interview → generation produces a downloadable asset bundle', async ({ page }, testInfo) => {
    test.setTimeout(80 * 60_000);

    const tap = new WsTap(page).install();
    const customer = new Customer();
    const transcript = new Transcript('full-build');

    await gotoRealApp(page);

    // Start a Full Build with the complete use case as the opening message.
    await page.getByRole('radio', { name: /전체 빌드|Full Build/ }).first().click();
    await page.locator('textarea').first().fill(PERSONA.kickoff);
    await page.getByRole('button', { name: /^시작$|^Start$/ }).click();

    await expect
      .poll(() => [...tap.sent].reverse().find((f) => f.action === 'createNewSession')?.scope, { timeout: 30_000 })
      .toEqual([]);
    transcript.add({ index: 0, agent: '(run started)', customer: PERSONA.kickoff, via: 'kickoff' });

    // Kickoff landing. The app rotates sessions on Start and the deployed WS can
    // FLAP under that rotation (observed: connect→disconnect code 1005, active:2),
    // so the kickoff sendMessage sometimes lands on a socket that's closing and
    // the agent never really starts. A bare "first token" isn't enough proof —
    // a half-turn can stream then die. Require REAL progress (the agent saved a
    // spec / requirement, or produced a substantial reply), and if it doesn't
    // materialize, re-send the kickoff on the now-settled socket (up to twice).
    const progressed = async (ms: number) =>
      tap
        .waitForFrame(
          (f) =>
            (f.type === 'asset_preview' &&
              ['operation_spec', 'requirement', 'workspace_update', 'cloudformation', 'lambda'].includes(
                String((f.assetPreview as { assetType?: string })?.assetType),
              )) ||
            (f.type === 'tool_start') ||
            (f.type === 'phase_changed'),
          ms,
        )
        .then(() => true)
        .catch(() => false);

    // Let the post-Start session rotation settle (the flap is transient, ~3s):
    // require the header to read "Connected" steadily before judging the kickoff.
    await page.waitForTimeout(4000);
    await expect(page.getByText(/연결됨|Connected/).first()).toBeVisible({ timeout: 30_000 }).catch(() => {});

    // Wait for the first token first (cheap signal the turn began), then for
    // genuine progress; re-kick if the socket flap swallowed the opener.
    await tap.waitForFrame((f) => f.type === 'stream' || f.type === 'message', 90_000).catch(() => {});
    let attempts = 0;
    while (!(await progressed(90_000)) && attempts < 3) {
      attempts++;
      // eslint-disable-next-line no-console
      console.log(`[full-build] kickoff didn't take hold (attempt ${attempts}) — re-sending on the settled socket.`);
      // Re-send the kickoff via the in-chat composer once the socket has settled.
      // (We avoid page.reload() as a recovery: the backend's history write is
      // currently broken on Korean surrogate chars, so a reload can lose the
      // session and bounce to the start screen. See backend_surrogate_write_bug.)
      try {
        await sendChat(page, PERSONA.kickoff, 60_000);
      } catch {
        /* composer not editable yet; loop re-checks progress */
      }
    }
    expect(
      await progressed(120_000),
      'agent never made real progress (socket flap / no spec saved) after re-kicking',
    ).toBeTruthy();

    // Stub window.open so the eventual download_ready (which auto-opens the ZIP
    // URL + shows a modal) doesn't navigate the page away mid-test.
    await page.evaluate(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).open = () => null;
    });

    // ── Responsive customer loop ───────────────────────────────────────────
    const MAX_TURNS = 40;
    const downloadReady = () => tap.byType('download_ready').length > 0;
    const phasesSeen = () => new Set(tap.byType('phase_changed').map((f) => String(f.phase)));
    // Core families that signify a complete bundle. knowledge_base/faq is the
    // optional one the agent only makes on request; cdk may surface as
    // 'cloudformation'. Generation is "done enough" to package once these are in.
    const coreReady = () => {
      const s = tap.assetTypesSeen();
      const has = (...xs: string[]) => xs.some((x) => s.has(x));
      return has('lambda') && has('openapi') && has('prompt') && has('contact_flow') && has('cdk', 'cloudformation') && has('faq', 'knowledge_base');
    };
    // Click the real "Package & Download Assets" control (Progress pane footer).
    // It packages the bundle via REST GET /api/assets/{id}/download (and shows the
    // deploy modal); the agent never self-emits a download. We confirm the click
    // by waiting for that REST response, retrying the click a few times since the
    // Progress pane / button can need a beat to mount after generation.
    const clickDownload = async (): Promise<boolean> => {
      for (let i = 0; i < 4; i++) {
        await page.getByRole('button', { name: /진행 상황|Progress/ }).first().click().catch(() => {});
        const btn = page
          .getByRole('button', { name: /에셋 패키징 및 다운로드|에셋 다운로드|Package & Download Assets|Download Assets/ })
          .first();
        if (await btn.count()) {
          // Click and watch for the packaging REST call to actually fire.
          const respPromise = page
            .waitForResponse((r) => /\/api\/assets\/.+\/download/.test(r.url()), { timeout: 90_000 })
            .catch(() => null);
          await btn.click().catch(() => {});
          const resp = await respPromise;
          if (resp) {
            // eslint-disable-next-line no-console
            console.log(`[full-build] download REST fired → HTTP ${resp.status()}`);
            return true;
          }
          // eslint-disable-next-line no-console
          console.log(`[full-build] download click ${i + 1} didn't fire the REST call — retrying.`);
        }
        await page.waitForTimeout(3000);
      }
      return false;
    };
    let triedDownload = false;
    let downloadConfirmed = false;
    let contactFlowChecked = false;
    let contactFlowVisible = false;

    // Frame cursor: text the agent produces AFTER this index belongs to the
    // current turn. Initialize past the kickoff's first reply boundary later.
    let cursor = 0;
    let turn = 1;
    let done = false;
    // Stall detection: if many turns pass with no new asset/phase, the dialogue
    // is looping (the canned-nudge version got stuck here). Escalate to an
    // explicit "finish and package" instruction to break it.
    let lastProgressKey = '';
    let stallTurns = 0;
    // Hard wall-clock budget for the whole conversation loop, so a dead socket /
    // perpetual non-progress can never hang the run (it must end and assert on
    // whatever was produced). 60 min leaves headroom under the 80-min test cap.
    const loopDeadline = Date.now() + 60 * 60_000;
    let consecutiveNoIdle = 0;

    for (; turn <= MAX_TURNS && !done; turn++) {
      if (Date.now() > loopDeadline) {
        // eslint-disable-next-line no-console
        console.log('[full-build] loop wall-clock budget exhausted — stopping and asserting on produced assets.');
        break;
      }
      // Mark where this turn's agent output begins.
      const turnStart = tap.frames.length;

      // Wait for the agent to finish its turn (idle = our turn) or terminal.
      const idle = await waitForAgentIdle(page, { quietMs: 5000, maxMs: 12 * 60_000 });
      if (downloadReady()) {
        done = true;
      }

      // Reconstruct what the agent said during/just before this idle window.
      // Use the wider [cursor, end) window so tool-only turns still capture any
      // surrounding narration the agent streamed.
      const agentText = Transcript.agentTextSince(tap.frames, Math.min(cursor, turnStart));
      cursor = tap.frames.length;

      const phase = [...phasesSeen()].slice(-1)[0];
      const assets = [...tap.assetTypesSeen()];

      if (done) {
        transcript.add({ index: turn, agent: agentText, customer: '(download ready — done)', phase, assets, via: 'terminal' });
        break;
      }
      if (!idle) {
        consecutiveNoIdle++;
        // eslint-disable-next-line no-console
        console.log(`[full-build] turn ${turn}: agent busy >12m (#${consecutiveNoIdle}). phases=${[...phasesSeen()].join(',')}`);
        transcript.add({ index: turn, agent: agentText, customer: '(still waiting — agent busy)', phase, assets, via: 'wait' });
        // If we've waited the max repeatedly with no resolution, the socket is
        // likely dead — stop rather than loop. (3 × 12m = 36m of pure waiting.)
        if (consecutiveNoIdle >= 3) {
          // eslint-disable-next-line no-console
          console.log('[full-build] agent never settled across repeated max-waits — stopping.');
          break;
        }
        turn--; // don't consume a turn budget slot for a pure wait
        continue;
      }
      consecutiveNoIdle = 0;

      // Verify the Contact Flow is viewable in the right pane while the UI is
      // still clean — do this BEFORE the download (the deploy modal opens on
      // download and would intercept the Assets-pane click). Once only.
      if (!contactFlowChecked && tap.assetTypesSeen().has('contact_flow')) {
        contactFlowChecked = true;
        await page.getByRole('button', { name: /에셋|Assets/ }).first().click().catch(() => {});
        contactFlowVisible = await page
          .getByRole('button', { name: /^Contact Flow$/ })
          .first()
          .isVisible({ timeout: 20_000 })
          .catch(() => false);
        // Return to Progress so the download button is reachable.
        await page.getByRole('button', { name: /진행 상황|Progress/ }).first().click().catch(() => {});
      }

      // If the full bundle is generated, do what a real user does: click
      // "Package & Download Assets". The agent never self-emits a download — it's
      // the UI action that packages via REST GET /api/assets/{id}/download.
      if (coreReady() && !triedDownload) {
        triedDownload = true;
        // eslint-disable-next-line no-console
        console.log('[full-build] core bundle ready → clicking Package & Download.');
        transcript.add({ index: turn, agent: agentText, customer: '(click: Package & Download Assets)', phase, assets, via: 'download-click' });
        // clickDownload returns true once the packaging REST call has fired and
        // responded — that IS the download success for the REST path.
        const clicked = await clickDownload();
        if (clicked || downloadReady()) {
          downloadConfirmed = clicked || downloadReady();
          done = true;
          break;
        }
      }

      // Track progress to detect a non-productive loop.
      const progressKey = `${[...phasesSeen()].join('|')}::${assets.join('|')}`;
      if (progressKey === lastProgressKey) stallTurns++;
      else {
        stallTurns = 0;
        lastProgressKey = progressKey;
      }

      // It's our turn: answer the agent's ACTUAL latest message.
      let text: string;
      let via: string;
      if (stallTurns >= 3) {
        // Break the loop with an explicit, unambiguous instruction. The agent
        // sometimes waits on a confirmation our generic affirmatives don't
        // satisfy (e.g. optional research/FAQ, or "package now?").
        text =
          '추가 질문 없이 지금까지 정의된 내용 그대로 남은 에셋(FAQ 포함)까지 모두 생성하고, ' +
          '최종 패키지로 묶어서 다운로드까지 완료해 주세요. 더 이상 확인은 필요 없습니다.';
        via = 'stall-break';
      } else {
        const r = await customer.reply(agentText, { phase, turn });
        text = r.text;
        via = r.via;
      }
      // eslint-disable-next-line no-console
      console.log(
        `[full-build] turn ${turn} [${via}] phase=${phase ?? '-'} assets=${assets.join(',')}\n` +
          `   agent: ${agentText.slice(0, 160).replace(/\s+/g, ' ')}\n   cust : ${text.replace(/\s+/g, ' ')}`,
      );
      transcript.add({ index: turn, agent: agentText, customer: text, phase, assets, via });

      try {
        await sendChat(page, text, 60_000);
      } catch {
        if (downloadReady()) {
          done = true;
          break;
        }
        // Composer not editable (terminal/modal) — try once more next loop.
      }
    }

    // ── Persist transcript + assertions ────────────────────────────────────
    const assetsSeen = tap.assetTypesSeen();
    const phases = phasesSeen();
    const out = transcript.write({
      turns: transcript.turns.length,
      phases: [...phases],
      assets: [...assetsSeen],
      downloadReady: downloadReady(),
    });
    await testInfo.attach('transcript.md', { path: out.md, contentType: 'text/markdown' });
    await testInfo.attach('transcript.json', { path: out.json, contentType: 'application/json' });
    // eslint-disable-next-line no-console
    console.log(`[full-build] transcript → ${out.md}`);
    console.log(`[full-build] done after ${turn} turns. phases=${[...phases].join(',')} assets=${[...assetsSeen].join(',')} download=${downloadReady()}`);

    // The run must have advanced past the interview into generation.
    expect(phases.has('generation'), `phases seen: ${[...phases].join(',')}`).toBeTruthy();

    // It must have produced multiple real asset families (streamed to the UI).
    const produced = ASSET_FAMILIES.filter((a) => assetsSeen.has(a));
    expect(produced.length, `expected several asset families; saw: ${[...assetsSeen].join(',')}`).toBeGreaterThanOrEqual(4);

    // The contact flow should have been viewable in the right pane (checked
    // mid-run, before the download modal could intercept the Assets click).
    if (assetsSeen.has('contact_flow')) {
      expect(contactFlowVisible, 'Contact Flow asset should be viewable in the right pane').toBeTruthy();
    }

    // Strongest signal: a packaged, downloadable bundle. The download is a UI
    // action — clicking "Package & Download Assets" packages via REST and either
    // flips the button to "에셋 다운로드"/"Download Assets" + shows the deploy
    // modal, OR (legacy path) emits a download_ready WS frame. Accept either.
    const downloadButtonReady = await page
      .getByRole('button', { name: /에셋 다운로드|Download Assets/ })
      .first()
      .isVisible()
      .catch(() => false);
    const modalShown = await page
      .getByText(/배포 가이드|Deployment Guide|deploy\.sh|다운로드되었습니다|downloaded/i)
      .first()
      .isVisible()
      .catch(() => false);
    // downloadConfirmed = the packaging REST call fired & responded during the loop.
    const packaged = downloadConfirmed || downloadReady() || downloadButtonReady || modalShown;
    if (!packaged) {
      // eslint-disable-next-line no-console
      console.warn('[full-build] bundle was generated but packaging/download was not confirmed within budget.');
    }
    expect(packaged, 'expected a packaged, downloadable bundle (REST package call, download_ready frame, Download button, or deploy modal)').toBeTruthy();
  });
});
