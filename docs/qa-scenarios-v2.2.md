# AICC Builder v2.2 — E2E QA Scenarios

Systematic, scenario-based QA for the v2.2 changes (model selection, segmented
generation, import / whiteboard-import, redesigned UI). Each scenario has: intent,
steps, expected result, and how to verify (UI screenshot + backend log / CloudWatch).

**Environments**
- **Local dev**: `backend/ecs/local-dev.sh` (or uvicorn on :8080, RAG via `CONTACT_FLOW_KB_ID`) + `npm run dev` (`VITE_DEV_AUTH=1`). ⚠️ Known dev-only instability: React StrictMode double-mounts `useWebSocket` and the Vite `/ws` proxy can reset the socket during a session switch — timing-sensitive sends (large imports) may be dropped. Treat the **deployed** env as source of truth for those.
- **Deployed dev**: CloudFront URL (`https://d3olm94xcw70gf.cloudfront.net`), real Cognito, ECS task on :8080 behind ALB. Verify backend via CloudWatch log group `/ecs/aiccbuilderecs-dev`.

**Verification tooling**
- UI: Playwright (`browser_navigate`, `browser_click`, `browser_file_upload`, `browser_take_screenshot`, `browser_console_messages`).
- Backend (local): `tail /tmp/aicc-backend.log`. Backend (deployed): `aws logs filter-log-events --log-group-name /ecs/aiccbuilderecs-dev`.
- Connect API: `CreateContactFlow` on workshop instance `aws-demo-workshop-sukwonie` (`5bf5005e…`, ap-northeast-2), create→inspect→delete.

**Pass criteria (apply to every scenario)**
1. No uncaught console errors (ReferenceError, etc.) — stale localhost/cognito/`/history` 404 noise excluded.
2. The user can SEE the result (asset visible in the right pane and/or chat), not just a toast.
3. Backend state matches the UI claim (scope, phase, asset recorded, file seeded).
4. Generated Contact Flows pass the real `CreateContactFlow` API.

---

## A. Model selection

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| A1 | Select each model | Header dropdown → pick 4.8 / 4.7 / 4.6 | Header chip updates; selection persists across reload (localStorage) | UI chip; localStorage |
| A2 | Model reaches backend | Pick 4.6 → start any run | Backend `[selected_model] → …-4-6-v1` (exact `-v1`) | log |
| A3 | Temperature branch | Pick 4.6 vs 4.8 | 4.6 → `temperature` sent; 4.7/4.8 → omitted; no 400 | `build_model_kwargs` unit + no Bedrock 400 in log |
| A4 | Mid-session switch | Change model mid-conversation | Next turn uses new model id | log on next turn |

## B. Full Build

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| B1 | Full build with description | 전체 빌드 → type desc → 시작 | Interview starts; backend `scope=full` (all 6 assets); 12-step progress | screenshot + log |
| B2 | Full build, empty description | 전체 빌드 → 시작 (no text) | Interview starts via localized default opener (NOT a silent dead-end) | screenshot + log |
| B3 | Generation streams to right pane | Continue B1 to a generation phase | Each asset STREAMS into the right asset workspace and is visible | screenshot of asset pane |

## C. Single Segment

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| C1 | Contact Flow only | 단일 세그먼트 → Contact Flow → desc → 시작 | `scope=['contact_flow']`; scoped interview asks ONLY flow questions; progress trimmed (≈7 steps) | screenshot + log |
| C2 | AI Prompt only | …→ AI 프롬프트 → desc → 시작 | `scope=['prompt']`; prompt-focused interview | log |
| C3 | FAQ only | …→ FAQ → desc → 시작 | scope maps to `knowledge_base`; FAQ flow completes | log |
| C4 | Segment requires description | Pick segment, leave desc empty | Start DISABLED (no silent no-op) | UI (button disabled) |
| C5 | **Segment asset visible** | Complete a Flow-only generation | Generated flow STREAMS into right asset workspace; flow validates on Connect API | asset pane + Connect API |

## D. Improve Existing — file import

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| D1 | Import contact_flow.json | 기존 에셋 개선 → upload .json | Lint/repair summary; **asset shown in right pane**; phase=post_generation; file in left explorer | screenshot + log + file tree |
| D2 | Import ai_agent_prompt.yaml | upload .yaml | Same, prompt asset shown | screenshot + log |
| D3 | Wrong file type | upload .txt | Rejected inline ("지원되지 않는 파일") — nothing sent to backend | UI error |
| D4 | Broken flow JSON | upload a flow with bad block types | Lint reports/repairs; surfaces remaining errors | log + UI |
| D5 | Edit after import | After D1, send an edit request | Orchestrator patches via modification flow (patch-only, no full regen) | log + asset diff |

## E. Improve Existing — whiteboard / sketch image (NEW)

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| E1 | Whiteboard photo → flow | 기존 에셋 개선 → upload .png sketch | Vision transcribes → draft flow → lint/repair; **flow shown in right pane**; file seeded; phase=post_generation | screenshot + log + Connect API |
| E2 | Unreadable image | upload a non-flow photo | Graceful "couldn't read a flow" error, no crash | UI error + log |
| E3 | Drafted flow imports to Connect | Take E1 output, substitute ARNs | `CreateContactFlow` ACCEPTED | Connect API |

## F. Redesign surfaces

| ID | Scenario | Steps | Expected | Verify |
|----|----------|-------|----------|--------|
| F1 | Dark timeline | Run a chat in dark mode | Bubbles/tools/assets theme-consistent (no bright light cards) | screenshot |
| F2 | Light theme | Toggle theme | Whole app re-themes cleanly; header stays navy | screenshot |
| F3 | Progress tab | Right pane | "Progress" is a first-class tab; 12 steps grouped under 4 phases | screenshot |
| F4 | Login reskin | /login | Violet/zinc theme, language selector, localized, password toggle | screenshot |
| F5 | Jump-to-latest | Long chat, scroll up | Floating "jump to latest" pill appears | screenshot |
| F6 | Collapsed tool calls | During generation | Tool calls collapsed w/ one-line summary; expand on click | screenshot |

## G. Open UX questions (raised in review — need decision + test)

These are **not yet resolved**; document the intended behavior, then test it.

- **G1 — Progress bar in single-asset mode.** When only one asset is generated, what should "X / N 완료" count? Currently trims to in-scope steps (≈7 for flow-only incl. interview/review/package). *Decision needed:* should it show just the 1 generation lane + its phases, or the full scoped set? Test that the count is honest (matches what actually runs).
- **G2 — Asset Workspace (right) vs File Explorer (left): how do they differ?** Intended: **left File Explorer** = the raw S3-files/NFS workspace tree (all files, browse/open any); **right Asset Workspace** = a focused, rendered view of the *generated/imported assets* (Contact Flow diagram+JSON, prompt, FAQ) with tabs + fullscreen. *Test:* both reflect the same underlying files; opening a file left vs selecting an asset right are consistent (no "missing" asset on one side).
- **G3 — Right-pane minimize button is not intuitive.** Current collapse affordance is unclear. *Decision needed:* clearer minimize/expand control + label; test that collapse/expand preserves the active view (Progress vs Assets) and the status meter stays visible when collapsed.
- **G4 — Import parity with normal generation.** Imported assets must appear in the right Asset Workspace and stream-like, same as generated ones (fixed in v2.2: import now emits `asset_preview` + flips `rightPaneView` to assets). *Test E1/D1 explicitly verify this.*

---

## Execution log (fill per run)

| Scenario | Env | Result | Evidence | Notes |
|----------|-----|--------|----------|-------|
| (to be filled during the QA pass) | | | | |
