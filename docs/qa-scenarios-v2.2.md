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

## Execution log

Run: 2026-06-18, deployed dev (`d3olm94xcw70gf.cloudfront.net`), Playwright + CloudWatch + Connect API.

| Scenario | Env | Result | Evidence | Notes |
|----------|-----|--------|----------|-------|
| A1 model dropdown | deployed | ✅ PASS | header + start-screen show 4.8/4.7/4.6; 4.8 default checked | screenshot qa-A1 |
| A2 model→backend | deployed | ✅ PASS | log `[selected_model] … -> global.anthropic.claude-opus-4-6-v1` (exact `-v1`) | |
| A3 temperature branch | deployed | ✅ PASS | 4.6 run completed, no Bedrock 400/ValidationException | |
| A4 model persists | deployed | ✅ PASS | header chip stayed Opus 4.6 across the run | |
| B full-build progress | deployed | ✅ PASS (after fix) | new chat shows **0/12** with interview+review restored | regression found+fixed: full build had echoed the full asset set as a "scope" → showed 0/7. Fixed `session_created` to echo `[]` for full builds. |
| C1 scoped interview | deployed | ✅ PASS | flow-only run asked ONLY flow questions; log `scope=['contact_flow']`, scoped interview prompt | |
| C4 segment requires desc | deployed | ✅ PASS | 시작 disabled until description typed | |
| C5 / G4 segment asset visible | deployed | ✅ PASS | generated flow streamed into right Asset Workspace (tabs, copy, download, fullscreen) | |
| C5 flow → Connect API | deployed | ⚠️→✅ | **real bug found & fixed**: generated menu `GetParticipantInput` had `DTMFConfiguration` + no `StoreInput` (also `Text`+`SSML` dual-def, nested `QueueId`) → `CreateContactFlow` rejected. The linter repairs ALL of these correctly, but its output wasn't reaching the UI: the frontend `updateAssetPreview` guard rejected the linted content because it was *shorter* than the raw streamed flow (the guard meant for late-delta races). **Final verification on the fully-fixed stack (task def :26): the flow downloaded straight from the deployed UI imported into the real `CreateContactFlow` API with ONLY the expected `{{BASIC_QUEUE_ARN}}` substitution — no manual lint** (`ContactFlowId` c719e0c3 created → deleted, instance clean). | 3 commits: 7b3b7da (prompt), 8725b06 (frontend authoritative-full guard). NOTE: prompt fix alone did NOT stop the model emitting the broken shape — the linter+delivery path is the guaranteed safety net. |
| G1 scoped progress count | deployed | ✅ PASS (after fix) | flow-only shows **0/7** (interview 4 + flow + review + package); out-of-scope lanes under "이번 실행에 포함되지 않음" | reverted earlier over-trim that hid interview/review |
| F1 dark timeline | deployed | ✅ PASS | bubbles/tools/assets theme-consistent | screenshot qa-00 |
| F6 collapsed tool calls | deployed | ✅ PASS | tool calls render collapsed with one-line summary | |
| KB ingestion (deploy) | deployed | ✅ PASS (after fix) | `sync-kb-docs.sh` had committed merge-conflict markers → KB sync failed every deploy. Fixed (commit d916f01); 21 docs synced, ingestion COMPLETE. | |
| C2 AI prompt scope + real API | deployed | ✅ PASS (+ new lint) | 3 real generated prompts (free/sky/card) import via real qconnect `CreateAIPrompt`. **Verified the variable-once rule live**: `{{$.Custom.firstName}}` twice → `ValidationException: "Each variable may only appear once."`; first `{{var}}` + later bare token → ACCEPTED. Added `lint_ai_prompt` (strips braces from duplicates) wired into generator + import; deliberately-dup prompt → auto-fixed → ACCEPTED. | commit 03996d3 |
| C3 FAQ scope (knowledge_base) | deployed | ⚠️→✅ | scope→`knowledge_base`, 8-item FAQ generated to `assets/knowledge-base/*.md`, completes. **Bug found**: FAQ showed in left File Explorer + chat but right Asset Workspace was EMPTY (orchestrator wrote via a workspace-file tool, not `save_faq_document`, so no canonical `faq` asset_preview; `knowledge-base/` folder wasn't in the canonical map). Fixed (commit, map `knowledge-base`/`knowledge_base`→`faq`); re-verified: FAQ tab now renders in the right pane. | |
| D1 import contact_flow.json | deployed | ✅ PASS | asset shown in right pane + chat | |
| D2 import prompt.yaml (dup var) | deployed | ✅ PASS | imported prompt with duplicate `{{$.Custom.firstName}}` → auto-fixed on import (brace stripped) → shown in right pane → auto-fixed content ACCEPTED by real qconnect API | exercises new import-lint |
| D3 wrong file type (.txt) | deployed | ✅ PASS | inline rejection "지원되지 않는 파일입니다…"; nothing sent to backend | |
| D4 broken flow (PlayPrompt) | deployed | ✅ PASS | lint auto-repaired `PlayPrompt`→`MessageParticipant` + added required error; shown in right pane; repaired flow ACCEPTED by real Connect API (753eb09f, deleted) | |
| D5 edit after import | deployed | ✅ PASS | patch-only edit ("첫 인사만 패치") → right Asset Workspace reloaded the LATEST file (new greeting present, old gone) — verifies the asset-reload fix | |
| E1 whiteboard → flow | deployed | ✅ PASS | hand-drawn diagram transcribed to 9-action flow (welcome→menu→CheckHoursOfOperation→Voicemail/Lex, transfer→queue→callback), auto-linted, shown in right pane | |
| E2 non-flow image | deployed | ✅ PASS (graceful) | UI screenshot → vision produced a valid (auto-linted) flow rather than erroring. No crash. Note: vision import is permissive — it extracts *a* flow from any image rather than hard-rejecting non-flow images. | |
| E3 whiteboard flow → Connect API | deployed | ✅ PASS | E1 output, after substituting real queue/hours/Lex-alias ARNs → ACCEPTED by real `CreateContactFlow` (76cd4a6d, deleted) | |

**Asset-preview right-pane audit (all generators).** Every generator both (a) `stream_asset`s its canonical type directly AND (b) writes via `write_workspace_file`→`_emit_file_preview`, which emits a canonical asset_preview when the folder is in `_ASSET_FOLDER_TO_TYPE`. All folders are now covered: `contact_flow`, `prompt`, `lambda`, `openapi`, `cloudformation`/`cdk`, `faq`, `knowledge-base`/`knowledge_base`. So full-build and every single-segment asset reaches the right Asset Workspace. The FAQ gap (knowledge-base folder unmapped, written by the orchestrator not the faq sub-agent) was the only miss and is fixed.
