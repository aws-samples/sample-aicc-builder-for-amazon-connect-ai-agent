# AICC Builder — E2E tests

Two layers:

| Layer | What it drives | Backend | Cost | When |
|-------|----------------|---------|------|------|
| **Real (default)** | The **deployed app** at `$DEV_URL` (your CloudFront/ALB URL) | Real ECS + real Bedrock | Real (esp. full build) | Acceptance against production-like env |
| **Mock (offline)** | The same React app served locally | Mocked WebSocket + REST | None | Fast UI regression / CI / offline |

Customer stories these map to: [`docs/customer-stories.md`](../../docs/customer-stories.md).

---

## Real backend (deployed dev)

The deployed build uses **real Cognito**, so you log in once and the session is
reused. Your password is never seen or stored by the tooling — only the
resulting browser session state is saved (to `e2e/.auth/dev.json`, gitignored).

Set `DEV_URL` to your deployed app for all real-mode commands (find it in
`cdk-outputs-<stage>.json` → `FrontendUrl`):

```bash
export DEV_URL=https://<your-cloudfront>.cloudfront.net
```

### 1. Capture auth (one-time, and whenever it expires)

```bash
cd frontend
npm run test:e2e:auth      # opens a headed browser to $DEV_URL
```

Sign in in the window that opens. Once the app's start screen appears, the run
saves `e2e/.auth/dev.json` and exits. If tests later bounce to `/login`, the
session expired — just run this again.

### 2. Run

```bash
npm run test:e2e:smoke     # fast: smoke + protocol (≈no Bedrock cost)
npm run test:e2e:real      # everything incl. the full ~20-30m build (real Bedrock)
npm run test:e2e           # alias for test:e2e:real
npm run test:e2e:headed    # watch it run
npm run test:e2e:report    # open the HTML report
```

- **`smoke.spec.ts`** — authenticated load, mode cards, model selector, segment radios.
- **`protocol.spec.ts`** — taps the real WebSocket; asserts `session_created`
  echoes the generation `scope` and selected `model`. Minimal cost.
- **`full-build.spec.ts`** — plays the customer through the whole interview →
  6-asset generation. Because this is a real LLM conversation, the customer is
  **responsive**, not a fixed script:
  - Each turn it reconstructs what the agent actually said (from the WS frames)
    and a **Customer brain** (`support/customer.ts`) answers *that* message in
    character as the `ABC 호텔` persona. The brain calls **Claude Sonnet 4.6**
    (`global.anthropic.claude-sonnet-4-6`, override with `CUSTOMER_LLM_MODEL`;
    region `CUSTOMER_LLM_REGION`, default `us-east-1`). A keyword heuristic is the
    fallback if Bedrock is unreachable (`CUSTOMER_LLM=0` forces heuristic).
  - A **stall-breaker** detects a non-productive loop (≥3 turns, no new
    asset/phase) and sends an explicit "generate the rest incl. FAQ and package"
    instruction — this is what gets the optional FAQ/knowledge-base produced.
  - When the full bundle is present it **clicks "Package & Download Assets"**
    (the real user action; the agent never self-emits a download) and waits for
    the deploy modal / download_ready.
  - The whole dialogue is written to `test-results/transcripts/full-build.{md,json}`
    and attached to the report, so you can **read exactly what was said**.
  - Asserts the run **progresses, produces ≥4 asset families, shows the Contact
    Flow, and yields a downloadable bundle** — never exact LLM wording
    (non-deterministic). Budget: 80 min.

  Notes: a real run takes ~25–35 min and spends Bedrock on both the agent (Opus)
  and the customer (Sonnet). Killing the test client does NOT stop the
  server-side agent — let runs finish or expect a busy backend briefly after.

  ⚠️ **Run the full build with EXCLUSIVE use of the dev environment.** It's a
  single shared ECS task. If anyone is clicking around the dev URL (or another
  run/agent is active) at the same time, the concurrent sessions contend for the
  one backend and the test's turns get starved — the run stalls mid-interview
  (observed: the run sat with no spec progress while 4–5 other sessions ran
  generation). Don't open the dev app while a full-build run is in flight. The
  spec hardens against the transient WS connect/disconnect flap at "Start" by
  re-sending the kickoff if no real progress lands, but it can't beat sustained
  contention.

Target another environment with `DEV_URL=… npm run test:e2e:real` (re-capture
auth against that URL first). **Do not** point this at prod without intent —
real runs spend Bedrock and write real sessions.

---

## Mock backend (offline)

No cloud, no Bedrock, no login. Boots a local Vite dev server with the Cognito
bypass (`VITE_DEV_AUTH=1`) and intercepts the WebSocket + REST.

```bash
cd frontend
npm run test:e2e:mock          # hermetic UI suite + login-page suite
npx playwright test --project=hermetic e2e/single-segment.spec.ts   # one file
```

How it works: `e2e/support/mock-backend.ts` becomes the WebSocket server via
`page.routeWebSocket`, scripting `session_created` / `stream` / `asset_preview`
/ `phase_changed` / `download_ready` exactly as `useWebSocket.ts` expects.
`e2e/support/fixtures.ts` provides the `test`/`mock`/`gotoApp` fixtures and the
shared selectors. The `login` project runs a second server **without** the
bypass so `/login` actually renders (client-side i18n / policy / show-hide only).

---

## Files

```
e2e/
  real/
    auth.setup.ts        # interactive login → saves e2e/.auth/dev.json
    smoke.spec.ts        # deployed-app smoke
    protocol.spec.ts     # real WebSocket handshake / scope+model echo
    full-build.spec.ts   # full interview→generation (real Bedrock)
    support/real.ts      # WsTap (frame tap) + customer-simulator helpers
  support/               # hermetic harness (mock-backend, fixtures, data)
  *.spec.ts              # hermetic (offline) specs
```
