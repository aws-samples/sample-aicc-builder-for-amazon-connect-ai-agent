# AICC Builder — Customer Stories

AICC Builder turns a ~1-hour AI interview into a customized Amazon Connect AI-agent PoC bundle (Lambda, OpenAPI, AI Prompt, Contact Flow, Infrastructure/CloudFormation, FAQ/Knowledge Base). The frontend is a thin React/Vite/Zustand client over a WebSocket protocol; the backend runs a multi-agent generation pipeline on Bedrock Claude. This document consolidates per-persona PM drafts into a single backlog, organized into nine epics, with stable story ids, Given/When/Then acceptance criteria, QA traceability back to the existing manual scenarios (A1..G4), and an E2E automation traceability table. Near-duplicate stories across personas have been merged, keeping the strongest acceptance criteria.

## Personas

- **Soo-jin — CC Solutions Architect.** Builds Amazon Connect PoCs to demo to stakeholders. Cares about producing the complete 6-asset bundle quickly, watching it take shape, and controlling Bedrock token cost across repeated runs.
- **Daniel — Connect Developer.** Already operates a contact center. Wants to generate or refine a *single* asset (a flow, a prompt, an FAQ) and drop it into a live instance with minimal fuss; needs output that the real Connect API accepts.
- **Mai — Non-technical CX Manager.** Works in Korean, does not write JSON. Wants to turn whiteboard sketches and plain-language requests into real Connect starting points and *see* the result, never hit a dead end.
- **Ken — Power User / Evaluator.** Stress-tests the tool: A/Bs models, juggles many sessions, exercises theme/layout/resilience/auth surfaces, and demands honest progress reporting and graceful recovery paths.

---

## Epic E1 — Full Build

### CS-1.1 — Start a Full Build from a described use case
**As a** contact center solutions architect **I want** to choose Full Build, type my use case, and start the interview **so that** the agent runs the complete interview and produces the full 6-asset bundle I can demo.

Soo-jin opens a fresh chat. The mode-first start screen asks "무엇을 만들고 싶으신가요?" with three mode cards. She picks the Full Build (전체 빌드) card, types a paragraph describing a policyholder claims-status and payment voice agent into the shared composer, and clicks 시작. The interview begins, scoped to all six assets.

Acceptance criteria:
- Given a fresh chat, When the start screen renders, Then three mode cards (Full Build / Single Segment / Improve Existing) show with Full Build selected by default (aria-checked).
- Given Full Build is selected, When I type a use-case description, Then Start becomes enabled.
- Given a description is typed, When I click Start, Then the empty-state composer disappears, my message appears in the timeline, and the agent begins replying (typing indicator shows).
- Given the run started, When I open the Progress tab, Then the counter shows 0 / 12 (all steps in scope, none trimmed).
- Given the interview is running, When I provide company and industry, Then the requirements checklist chips (Company / Industry / Operations) tick to done.

Maps to QA: B1. Priority: must.

### CS-1.2 — Start a Full Build with an empty description (default opener)
**As a** contact center solutions architect **I want** to click Start in Full Build without typing anything and still begin **so that** I can dive straight into the guided interview rather than write a brief upfront.

Soo-jin selects Full Build, leaves the box empty, and clicks Start. Instead of dead-ending, the agent kicks off with a localized default opener and starts asking interview questions.

Acceptance criteria:
- Given Full Build selected and description empty, When the start screen renders, Then Start is still enabled (unlike Single Segment, which stays disabled).
- Given an empty description, When I click Start, Then a localized default opener (e.g. Korean "시작할게요 — 컨택센터 구축을 안내해 주세요.") is sent on my behalf and appears as my first message.
- Given the opener is sent, When the agent responds, Then the interview begins (not a silent no-op).
- Given the run started with no description, When I open the Progress tab, Then it still shows the full 0 / 12 step set.

Maps to QA: B2. Priority: must.

### CS-1.3 — Watch all six assets stream into the Asset Workspace
**As a** contact center solutions architect **I want** each generated asset to stream into the right-side Asset Workspace as tabs **so that** I can watch the bundle take shape and inspect each type without leaving the chat.

As the pipeline runs, asset markers appear in chat and the right pane flips to the Asset Workspace (에셋 워크스페이스). Tabs appear in Connect-first order and Soo-jin clicks through each as it completes.

Acceptance criteria:
- Given generation is running, When an asset begins streaming, Then the right pane shows the Asset Workspace and a tab appears for that asset type.
- Given multiple assets generated, When I view the tab strip, Then I see Contact Flow, AI Prompt, FAQ, OpenAPI, Lambda, Infrastructure in that stable order.
- Given an asset type produced multiple files (e.g. one Lambda per operation), When that tab is active, Then a per-file sub-selector with a count badge lets me switch files.
- Given an asset is open, When I look at the workspace header, Then Fullscreen and Close controls are present.
- Given I switch to Progress and back to Assets, When the workspace re-opens, Then the previously active asset stays selected.

Maps to QA: B3. Priority: must.

### CS-1.4 — Package and download the complete asset bundle
**As a** contact center solutions architect **I want** to package all generated assets into a single downloadable bundle with deploy guidance **so that** I can take the PoC into CloudShell and run deploy.sh.

With all six assets generated, Soo-jin clicks "Package & Download Assets" (에셋 패키징 및 다운로드). The button shows "Packaging…" while the ZIP builds, the download starts, and a completion modal walks through the deploy.sh steps.

Acceptance criteria:
- Given at least one asset generated, When I view the Progress pane footer, Then "Package & Download Assets" is enabled.
- Given I click it, When packaging runs, Then the label changes to "Packaging…" with a spinner and the bundle ZIP download is triggered.
- Given packaging succeeds, When the download completes, Then a completion modal appears with deploy.sh CloudShell instructions and a workshop guide link.
- Given packaging fails, When the error surfaces, Then the button shows a "Retry" (다시 시도) state and a toast explains the failure.
- Given I want individual files, When I view "Generated Assets", Then per-asset download buttons (Lambda, AI Prompt, OpenAPI, Contact Flow, CloudFormation, Knowledge Base) are available for completed assets.

Maps to QA: B3. Priority: should.

---

## Epic E2 — Single-Segment Generation

### CS-2.1 — Generate only a Contact Flow as a single segment
**As an** Amazon Connect developer who already runs a contact center **I want** to pick Single Segment, choose Contact Flow, describe what I need, and get just one Contact Flow asset **so that** I can drop a new flow into my instance without a full 6-asset interview.

Daniel clicks the Single Segment (단일 세그먼트) card; the Contact Flow / AI Prompt / FAQ radio row appears. He selects Contact Flow, types a short description, and clicks 시작. A scoped interview asks only flow-relevant questions; the run is recorded with scope `['contact_flow']`.

Acceptance criteria:
- Given the start screen, When Daniel clicks Single Segment, Then a radio row with Contact Flow / AI Prompt / FAQ appears below the mode cards.
- Given Single Segment active and Contact Flow selected, When he has typed a description, Then Start becomes enabled.
- Given he clicks Start, When the run begins, Then the backend run is scoped to `['contact_flow']` and the interview asks only Contact-Flow-relevant questions.
- Given the scoped run, When the Progress tab is open, Then out-of-scope lanes (Lambda, OpenAPI, Infrastructure, etc.) appear under "이번 실행에 포함되지 않음" and only the Contact Flow lane plus interview/review/package steps count toward X / N.
- Given generation completes, When Daniel opens the right pane, Then the Contact Flow streams in as an interactive diagram plus a JSON Code tab.

Maps to QA: C1, C5, G1. Priority: must.

### CS-2.2 — Generate only an AI Prompt or only an FAQ
**As an** Amazon Connect developer (or CX manager) who needs one non-flow asset **I want** to scope a run to just the AI Prompt or just the FAQ **so that** I can refresh the agent prompt or knowledge base without regenerating everything else.

*(Merged: Daniel's prompt/FAQ segment story + Mai's FAQ-only story — strongest criteria retained.)* Daniel picks AI Prompt (AI 프롬프트) for a `['prompt']` run; another day Mai picks FAQ in Korean and gets a knowledge_base-lane run. Only the relevant asset is produced and shown in the workspace.

Acceptance criteria:
- Given Single Segment, When the radio row appears, Then it offers Contact Flow, AI 프롬프트, and FAQ.
- Given AI Prompt selected and started, When the run begins, Then it is scoped to `['prompt']` and a prompt-focused interview runs; an "AI Prompt" tab later renders the prompt in the Asset Workspace.
- Given FAQ selected and started, When the run begins, Then it maps to the Knowledge Base lane and a "FAQ" tab renders the FAQ in the Asset Workspace (not just in chat).
- Given FAQ selected but description empty and nothing attached, When viewing the composer, Then 시작 stays disabled (no silent no-op).
- Given a generated FAQ, When complete, Then it can be packaged and downloaded via the Download flow.

Maps to QA: C2, C3, G1. Priority: must.

### CS-2.3 — Start stays disabled until a segment and input are provided
**As an** Amazon Connect developer generating one asset at a time **I want** Start to stay disabled until I've chosen a segment and given input **so that** I never trigger a silent no-op run.

Daniel selects Single Segment but hasn't picked a type or typed anything; Start is visibly disabled. Once he picks Contact Flow and types a one-line description (or attaches a file), Start lights up.

Acceptance criteria:
- Given Single Segment with no segment chosen and an empty composer, When Daniel looks, Then Start is disabled (greyed, not clickable).
- Given a segment chosen but description empty and no attachment, When he looks, Then Start is still disabled.
- Given a segment chosen, When he types any text OR attaches a file, Then Start becomes enabled.
- Given Start is disabled, When he clicks it, Then nothing happens (no run starts, no message sent).

Maps to QA: C4. Priority: must.

---

## Epic E3 — Improve Existing (import & refine)

### CS-3.1 — Import an existing contact_flow.json and see it linted/auto-repaired
**As an** Amazon Connect developer with a hand-built flow **I want** to upload my contact_flow.json in Improve Existing mode and have it imported, linted, and auto-repaired **so that** I can start from my real flow and trust issues are fixed before I edit.

Daniel clicks Improve Existing (기존 에셋 개선), attaches his .json, and clicks Start. The backend imports, lints (repairing e.g. invalid DTMFConfiguration or duplicate keys), and shows a repair summary. The repaired flow appears as a diagram + JSON, lands in the file explorer, and the session enters patch-only edit mode.

Acceptance criteria:
- Given the start screen, When Daniel clicks Improve Existing and attaches a .json, Then scope `['contact_flow']` is derived from the file and Start becomes enabled.
- Given he starts the import, When the backend finishes, Then a lint/repair summary indicating fixes applied is shown.
- Given import succeeds, When the right pane opens, Then the auto-repaired flow shows as an interactive diagram + JSON Code tab, like a freshly generated flow.
- Given import succeeds, When Daniel checks the left file explorer, Then the imported flow file is present in the tree.
- Given the source had repairable issues, When the workspace shows the flow, Then it shows the repaired (shorter/valid) JSON, not the original broken content.

Maps to QA: D1, D4, G4. Priority: must.

### CS-3.2 — Import an existing AI Prompt YAML with duplicate-variable auto-fix
**As an** Amazon Connect developer maintaining an AI agent prompt **I want** to upload my ai_agent_prompt.yaml in Improve Existing mode and have it imported and lint-fixed **so that** I can refine it and trust it satisfies the qconnect CreateAIPrompt variable rules.

Daniel attaches a .yaml and starts; it maps to scope `['prompt']`. The import linter fixes prompt issues (e.g. each variable may appear only once inside double braces). The repaired prompt shows in the workspace, ready for a patch-only edit.

Acceptance criteria:
- Given Improve Existing, When Daniel attaches a .yaml/.yml file, Then scope `['prompt']` is derived from the extension.
- Given he starts the import, When the backend finishes, Then a lint summary is shown and the prompt appears under the "AI Prompt" tab.
- Given a duplicate variable inside {{ }}, When import lint runs, Then the duplicate is auto-fixed so the real qconnect CreateAIPrompt API would accept it.
- Given import succeeds, When Daniel checks the session, Then it is in patch-only edit mode ready for a follow-up modification request.

Maps to QA: D2. Priority: should.

### CS-3.3 — Turn a whiteboard photo into a draft Contact Flow
**As a** non-technical CX manager **I want** to upload a photo of my hand-drawn call flow and have it transcribed into a Contact Flow I can see **so that** I can turn my whiteboard sketch into a real Connect starting point without writing JSON.

Mai sketches her IVR, snaps a .png, opens AICC Builder in Korean, picks 기존 에셋 개선, attaches the photo, and clicks 시작. The vision step reads the sketch, drafts a flow, lints/repairs it, and the flow appears as an interactive diagram in the right pane.

Acceptance criteria:
- Given the 기존 에셋 개선 card selected, When it renders, Then a shared composer appears with a placeholder about attaching a file and saying what to do with it.
- Given the card selected, When Mai attaches a .png via paperclip or drag-and-drop, Then a thumbnail preview appears above the composer and 시작 becomes enabled.
- Given a .png attached, When Mai clicks 시작, Then a new chat begins, the photo is sent as her first message, and the agent indicates it is reading/transcribing the sketch.
- When the draft is ready, Then a Contact Flow asset appears in the right pane as an interactive React Flow diagram (not just a chat toast), with a JSON Code tab.
- Given the draft flow is shown, Then the run is treated as a contact_flow-scoped improve run (right pane focused on Contact Flow; Fullscreen/Close visible).

Maps to QA: E1, E3. Priority: must.

### CS-3.4 — Gracefully handle an unreadable / non-flow photo
**As a** non-technical CX manager **I want** a clear, non-crashing outcome when I upload a photo that isn't a clean flow diagram **so that** I never hit a blank dead end and always know what to do next.

Mai accidentally uploads a blurry wall photo, picks 기존 에셋 개선, attaches it, and clicks 시작. The app does not crash; the agent replies in Korean explaining what it could and couldn't read and either produces a best-effort draft or asks for a clearer sketch.

Acceptance criteria:
- Given a non-flow/low-quality photo and 시작 clicked, When the agent processes it, Then no uncaught error appears and the composer stays responsive.
- When the image can't be cleanly interpreted, Then the agent replies in Mai's UI language (Korean) describing what it saw and needs, not failing silently.
- Given a best-effort draft is produced, Then that draft flow still appears in the Asset Workspace so Mai sees a result, not just a message.
- Given Mai wants to retry, Then she can attach a new photo in the same composer and re-send without resetting the session.
- Given the photo was processed, Then Mai is never left with no next action (a reply, an asset, or a re-upload prompt is always present).

Maps to QA: E2. Priority: must.

### CS-3.5 — Reject a wrong file type inline before anything is sent
**As a** developer or CX manager who may grab the wrong file **I want** an immediate inline rejection for unsupported file types (and oversized images) **so that** I get clear feedback instead of a failed run, and nothing wrong is sent to the backend.

*(Merged: Daniel's .txt-as-flow rejection + Mai's wrong-file rejection — strongest criteria retained.)* Mai/Daniel attaches a .txt note, .exe, or oversized photo; the composer surfaces a localized inline error, the bad file is not staged, and nothing is sent.

Acceptance criteria:
- Given an unsupported file (e.g. .exe, or a flow saved as .txt), When rejected, Then an inline localized "Unsupported file…" / "지원되지 않는 파일입니다…" error shows and the file is not staged.
- Given an image larger than the 3.75 MB limit, When attached, Then an inline size-limit error shows and the oversized image is not staged.
- Given a wrong file was rejected, Then no run starts and nothing is sent to the backend; the user can immediately attach a correct file.
- Given the max of 5 attachments is staged, When the user tries to add another, Then the attach button is disabled and a "You can attach up to N files" message is shown.
- Given a valid supported type (.json flow, .yaml/.yml prompt, image, or supported docs), When attached, Then it is staged with a preview/removal and no error; Start enables.

Maps to QA: D3. Priority: must.

### CS-3.6 — Refine an imported asset with a patch-only edit
**As a** developer or CX manager refining an imported asset **I want** to ask for a small change in plain language and have only that part patched **so that** the rest of my carefully built flow is preserved.

*(Merged: Daniel's "make the greeting friendlier" + Mai's "첫 인사만 더 친근하게" — same patch-only behavior.)* After import, the user types a small change. The orchestrator patches via read_current_file / patch_file rather than regenerating; the workspace reloads to show the change in both diagram and JSON.

Acceptance criteria:
- Given an imported asset in patch-only edit mode, When the user sends a modification request, Then the asset is updated via a patch (not a full regeneration).
- Given the patch completes, When the Asset Workspace refreshes, Then it shows the latest file with the requested change applied and the prior content replaced.
- Given only the greeting changed, When the user inspects the rest, Then the other blocks/nodes are unchanged; the change is visible in both the diagram and JSON Code tab.
- Given the modification request, When the run finishes, Then scope stays on the single imported asset (no other asset types are generated).
- Given an edit is in progress, Then the user sees activity (typing/progress) and can continue further plain-language edits in the same chat without re-uploading.

Maps to QA: D5. Priority: should.

### CS-3.7 — Generated/repaired Contact Flow imports into the real Connect API
**As an** Amazon Connect developer who must deploy to a live instance **I want** the flow I generate or repair to download as valid JSON the real CreateContactFlow API accepts **so that** I can import it with only ARN substitution and no manual JSON fixing.

Daniel finishes a Contact-Flow-only run or an Improve import, uses the workspace copy/download/fullscreen controls and Package & Download, and runs CreateContactFlow against his workshop instance — accepted after only ARN substitution.

Acceptance criteria:
- Given a completed Contact-Flow run, When Daniel opens the workspace, Then copy, download, and fullscreen controls are available.
- Given the flow asset, When he clicks Package & Download Assets, Then the button shows "Packaging…" then "Download Assets" and a downloadable bundle is produced.
- Given a packaging error, When it occurs, Then a "Retry" action surfaces rather than failing silently.
- Given the downloaded flow JSON, When he calls the real CreateContactFlow API with only ARN substitution, Then the flow is accepted (no manual block-type repair required).
- Given an imported-and-repaired flow (e.g. invalid DTMFConfiguration or PlayPrompt in source), When downloaded, Then the repaired version is delivered and passes CreateContactFlow.

Maps to QA: C5, D4, E3. Priority: must.

---

## Epic E4 — Model Selection

### CS-4.1 — Pick a Claude model that persists across reload and can switch mid-session
**As a** solutions architect / power user evaluating cost, quality, and robustness **I want** to choose Opus 4.8 / 4.7 / 4.6 from the header (and start screen), have it persist across reload, and switch mid-conversation **so that** I can trade off cost vs. quality and A/B model behavior without restarting the session.

*(Merged: Soo-jin's cost/quality story + Ken's persist-and-switch story — strongest criteria retained.)* The header model chip reads "Opus 4.8" by default. The user switches to 4.6 for a cheaper run, reloads to confirm it sticks, and mid-interview switches back to 4.8 to compare the next turn.

Acceptance criteria:
- Given the app loads with no prior selection, When viewing the header/start screen, Then the model chip shows Opus 4.8 (default) and the dropdown lists Opus 4.8, Opus 4.7, Opus 4.6.
- Given the dropdown open, When the user selects Opus 4.6, Then the chip label updates to "Opus 4.6" and the option shows a check mark.
- Given Opus 4.6 selected, When the user reloads, Then the chip still reads "Opus 4.6" (persisted via localStorage).
- Given the start-screen and header selectors, When the model is changed in one, Then both reflect the same choice.
- Given a conversation in progress, When the user changes the model, Then the chip updates immediately and the next turn uses the new model (no session restart); the selected model id is carried with messages and the run proceeds without a model error.

Maps to QA: A1, A2, A3, A4. Priority: must.

---

## Epic E5 — Workspace & Progress UX

### CS-5.1 — Track the 12-step, 4-phase progress through a full run
**As a** solutions architect **I want** a first-class Progress tab with a completion counter and phase grouping **so that** I always know which phase the build is in and how much is left while I narrate the demo.

Soo-jin keeps the Progress (진행 상황) tab open: a 4-phase stepper (interview → generation → review → post_generation), 12 steps grouped under those phases, and an "X / N 완료" counter that climbs as lanes complete.

Acceptance criteria:
- Given a Full Build running, When I open the Progress tab, Then I see an "X / 12 완료" counter and a 4-phase stepper highlighting the current phase.
- Given the steps render, When I look at the list, Then they are grouped under the four phase headers.
- Given a step is active, When the pipeline works on it, Then it shows an animated in-progress bar and switches to a green check on completion, incrementing the counter.
- Given a full build (no scope), When I review the list, Then no "이번 실행에 포함되지 않음" section appears.
- Given I collapse the right pane to its rail, When generation continues, Then the vertical status meter and X/N count remain visible.

Maps to QA: F3. Priority: must.

### CS-5.2 — See an honest X/N counter that trims out-of-scope steps for scoped runs
**As a** power user evaluating progress accuracy **I want** the Progress tab to count only steps that will actually run, listing the rest under "Not in this run" **so that** the X/N matches reality for full builds and single-segment runs.

A full build reads 0/12; a Contact-Flow-only run shows roughly 0/7 while out-of-scope lanes appear struck-through under "이번 실행에 포함되지 않음".

Acceptance criteria:
- Given a full build (no scope), When the Progress tab opens, Then the denominator is the full set (12) grouped under the 4 phases.
- Given a single-segment Contact Flow run, When the Progress tab opens, Then the counter shows the in-scope set (~7: interview-gathering + contact_flow + review + package), not 1–2.
- Given a scoped run, When I scroll the list, Then out-of-scope generation lanes are listed under "이번 실행에 포함되지 않음", muted and struck-through.
- Given an FAQ-only run, When I inspect the in-scope steps, Then FAQ maps to the Knowledge Base lane (not a missing/duplicate step).
- Given steps complete, When I watch the counter, Then "X / N 완료" increments only for in-scope steps and reaches N at completion.

Maps to QA: B1, C1, C3, G1. Priority: should.

### CS-5.3 — Review the generated Contact Flow as an interactive diagram
**As a** solutions architect **I want** to view the generated Contact Flow as an interactive React Flow diagram alongside its JSON **so that** I can visually validate routing logic before claiming it's Connect-ready.

When the flow completes, its tab renders as a node-and-edge diagram derived from validated JSON, with a separate JSON Code tab. Soo-jin drags nodes for a screenshot and opens fullscreen.

Acceptance criteria:
- Given the Contact Flow asset is complete, When I select its tab, Then it renders as an interactive diagram (nodes + auto-routed edges), not raw text.
- Given the diagram is shown, When I switch tabs within the Contact Flow view, Then I can also see a JSON Code view of the same validated flow.
- Given the diagram is shown, When I drag a node, Then it moves and edges re-route automatically.
- Given I want a larger view, When I click Fullscreen, Then the flow opens in a fullscreen modal.
- Given the flow rendered, When I later download it, Then the JSON is the validated/linted version that imports into the real CreateContactFlow API.

Maps to QA: C5, G4. Priority: should.

### CS-5.4 — See generated/imported assets in the right pane (not just a toast)
**As a** non-technical CX manager **I want** every flow or FAQ I create to show up visibly in the right-side workspace **so that** I can review the result with my eyes instead of trusting a confirmation message.

The right Asset Workspace opens automatically focused on the new asset; Mai switches diagram/JSON, expands fullscreen, and toggles 진행 상황/에셋 tabs without losing what she was viewing.

Acceptance criteria:
- Given an asset is imported or generated, When it completes, Then the right pane reveals the Asset Workspace focused on that asset (rightPaneView flips to Assets).
- Given a Contact Flow asset is active, Then Mai sees both the interactive diagram and a JSON Code tab, plus copy/download/Fullscreen controls.
- Given Mai clicks Fullscreen (전체 화면), Then the asset opens in an enlarged modal she can close.
- Given Mai toggles between 진행 상황 and 에셋, When switching back, Then the previously active asset selection is preserved.
- Given no asset has been produced yet, When Mai opens the 에셋 tab, Then she sees an empty-state message (아직 생성된 에셋이 없습니다), not a broken/blank pane.

Maps to QA: E1, C5, G2, G4. Priority: must.

### CS-5.5 — Switch, resize, and collapse the right pane without losing state
**As a** power user evaluating the redesigned split-view **I want** to flip the right pane between Progress and Assets, drag-resize it, and collapse/expand the progress rail while keeping the completion meter visible **so that** the right pane is a first-class, predictable surface.

Ken flips between 진행 상황 and 에셋, drags the resize handle, collapses the rail, and selects an asset marker in chat to auto-flip the pane.

Acceptance criteria:
- Given the right pane is visible, When Ken clicks 진행 상황 and 에셋 tabs, Then the pane switches between ProgressSidebar and Asset Workspace and the active tab is highlighted (aria-pressed).
- Given assets exist and Assets view is open with nothing selected, When Ken clicks the Assets tab, Then the most recent asset is auto-selected and shown.
- Given the Asset Workspace is open, When Ken drags the vertical resize handle, Then the pane width changes (clamped between min/max) and stays at the chosen width.
- Given Ken clicks an asset marker in the chat timeline, When activated, Then the right pane flips to Assets focused on that asset.
- Given the Progress rail is collapsed, When Ken views it, Then the vertical completion meter and completed/total count remain visible.

Maps to QA: F3, G2, G3. Priority: should.

### CS-5.6 — Navigate long generations with jump-to-latest and collapsed tool calls
**As a** power user reviewing a long generation transcript **I want** tool calls collapsed with a one-line summary I can expand, plus a floating "Jump to latest" pill with a new-message count **so that** a long multi-agent run stays readable and I can return to the live edge quickly.

Acceptance criteria:
- Given the agent emits tool calls, When they render, Then each is collapsed by default showing a one-line summary (errors auto-expand).
- Given a collapsed tool call, When Ken clicks its expand control, Then details expand and aria-expanded/label toggles accordingly.
- Given the chat is long and Ken scrolled up, When new messages arrive, Then a floating "Jump to latest" / "최신으로" pill appears showing a new-message count.
- Given the pill is shown, When Ken clicks it, Then the view smooth-scrolls to the newest message and the count resets to zero.
- Given Ken is near the bottom, When new messages stream in, Then the view auto-scrolls to follow and the pill stays hidden.

Maps to QA: F5, F6. Priority: could.

### CS-5.7 — Download a generated flow / FAQ to use in Connect
**As a** non-technical CX manager **I want** to package and download the asset I created (with a popup-blocked fallback) **so that** my whiteboard sketch or FAQ becomes a deployable file for my team.

Acceptance criteria:
- Given at least one asset generated/imported, Then the Progress-pane download button is enabled and labeled 에셋 패키징 및 다운로드.
- Given Mai clicks it, When packaging starts, Then the button shows 패키징 중… then resolves to 에셋 다운로드 with the file delivered.
- Given packaging fails, Then the button changes to a 다시 시도 (Retry) state with an error toast.
- Given the browser blocks the download popup, Then a localized message tells her to use the provided link instead.
- Given the download completes, Then a completion modal with next-step guidance appears and can be closed.

Maps to QA: net-new. Priority: could.

---

## Epic E6 — Sessions & Persistence

### CS-6.1 — Manage multiple chats from the Session Sidebar
**As a** power user running many evaluation passes **I want** to create new chats, switch, rename, delete one, and delete all — with the list surviving a refresh **so that** I can keep runs organized and clean up test sessions without losing real work.

Acceptance criteria:
- Given the Session Sidebar is visible, When Ken clicks New Chat / 새 대화, Then a fresh empty chat opens showing the mode-first start screen.
- Given an existing session, When Ken double-clicks its title (or clicks the pencil), types a new name and presses Enter, Then the title updates in the list.
- Given Ken clicks a different session, When it loads, Then the chat timeline and right pane switch to that session and the selected row is highlighted.
- Given Ken clicks a session's delete (trash) button once, When it turns red showing "삭제하려면 다시 클릭", Then the session is removed only after a second click within the timeout.
- Given multiple chats, When Ken clicks Delete All / 전체 삭제 and confirms the "Delete all N chats?" prompt, Then all sessions are removed and a new empty chat is started.
- Given saved sessions, When Ken refreshes, Then the list reloads with the same chats (count shown at the bottom).

Maps to QA: net-new. Priority: must.

---

## Epic E7 — Localization & Accessibility

### CS-7.1 — Use the whole experience in Korean (and switch languages persistently)
**As a** non-technical CX manager who works in Korean **I want** the start screen, composer, progress, asset tabs, and error messages all in Korean, with a persistent language switch **so that** I can confidently navigate the tool in my own language end to end.

Acceptance criteria:
- Given the default UI language, When Mai loads the start screen, Then 무엇을 만들고 싶으신가요? and the three mode cards (전체 빌드 / 단일 세그먼트 / 기존 에셋 개선) render in Korean.
- Given 기존 에셋 개선 mode, Then the composer placeholder and 시작 label are Korean, and the attach hint reads about flow JSON / prompt YAML / flow image / docs in Korean.
- Given a run in progress, When Mai views the Progress tab, Then phase/step labels and the X / N 완료 counter are Korean.
- Given Mai changes language via the header globe selector, When she reloads, Then her choice persists.
- When an error is shown (e.g. unsupported file), Then it is presented in Korean (지원되지 않는 파일입니다…).

Maps to QA: F4. Priority: should.

---

## Epic E8 — Errors & Resilience

### CS-8.1 — Surface backend errors, a reconnect banner, and a "still processing" reset affordance
**As a** power user stress-testing resilience **I want** clear connection-status/reconnection banners, a dismissible backend connection-error message, a "Reset session" button after the backend reports it is still processing twice, and a stop control **so that** I never get stuck staring at a dead chat and always have a recovery path.

Acceptance criteria:
- Given the WebSocket drops, When reconnection starts, Then a "Reconnecting..." / "재연결 중..." banner appears and the header connection chip shows Connecting/Offline.
- Given reconnection succeeds, When the socket is restored, Then a "Reconnected" / "재연결 완료" banner shows briefly and the prior conversation/assets are restored.
- Given the backend returns a connection error, When received, Then an amber connection-error banner appears above the composer with a "Dismiss" / "닫기" action that clears it.
- Given the backend reports "Agent is still processing" at least twice, When Ken views the composer, Then a red strip with a "Reset session" / "세션 재설정" button appears.
- Given the strip is shown, When Ken clicks Reset session, Then a fresh session starts and the strip and processing counter are cleared.
- Given generation is streaming, When Ken clicks the stop (square) button, Then generation is cancelled and the send button returns.

Maps to QA: net-new. Priority: must.

---

## Epic E9 — Auth / Login

### CS-9.1 — Sign in via a localized, accessible login page with show/hide and a live policy checklist
**As a** power user evaluating the auth entry point **I want** a violet/zinc login page switchable between en/ko/ja, with a show/hide password toggle, accessible error announcements, and a live password-policy checklist on first-login set **so that** I can confirm the auth experience is polished, localized, and accessible.

Acceptance criteria:
- Given Ken is on /login, When he changes the language picker to ko-KR or ja-JP, Then title, subtitle, field labels and button text re-localize (and the choice is shared app-wide).
- Given the password field, When Ken clicks the eye / eye-off button, Then the password toggles masked/visible and the aria-label switches between "Show password" and "Hide password".
- Given email or password empty, When the form validates, Then a localized error ("Please enter email and password" / "이메일과 비밀번호를 입력하세요") is shown in a role=alert, aria-live region.
- Given the first-login "Set New Password" screen, When Ken types, Then the four policy items (8+ chars, uppercase, lowercase, number) update live from X to a green check.
- Given not all items satisfied, When Ken views "Set Password & Continue", Then it is disabled until the policy passes.
- Given the dark default theme, When Ken views the page, Then it uses the violet/zinc palette consistent with the rest of the app.

Maps to QA: F4. Priority: should.

### CS-9.2 — Toggle light/dark/system theme and re-theme the entire timeline cleanly
**As a** power user evaluating UI polish **I want** to cycle Light/Dark/System and have every surface re-theme consistently with no leftover bright cards **so that** I can verify the redesign is theme-consistent and dark mode (the default) looks first-class.

*(Placed in E9 alongside login as the cross-cutting visual-polish story; touches all surfaces including login.)*

Acceptance criteria:
- Given the app first loads with no stored theme, When Ken inspects it, Then the UI is in Dark mode by default.
- Given the header theme button, When Ken clicks repeatedly, Then it cycles Light, Dark, System with matching icon and label (라이트/다크/시스템).
- Given an active chat with messages, tool calls and streamed assets, When Ken switches to Light, Then bubbles, tool-call cards and the asset workspace all render in the light palette with no bright cards stranded in a dark layout.
- Given Light theme active, When Ken looks at the top header bar, Then it remains the dark navy treatment.
- Given Ken picks a theme, When he reloads, Then the chosen theme persists (localStorage).

Maps to QA: F1, F2. Priority: should.

---

## Traceability → E2E

The hermetic E2E suite drives the React frontend against a **mocked WebSocket backend** (scripted protocol frames) — it asserts UI state, scope/progress math, gating, persistence, and localization, **not** LLM output quality or real-API acceptance. Stories whose value depends on the real Bedrock LLM or the real Amazon Connect `CreateContactFlow` API are **not** hermetically automatable; they are marked `no` (live dependency dominates) or `partial` (UI/protocol shell is hermetic, the substantive check is not).

| Story ID | Title | E2E spec file | Automatable hermetically |
|---|---|---|---|
| CS-1.1 | Full Build from a described use case | e2e/full-build.spec.ts | partial (UI/scope/progress yes; interview content needs LLM) |
| CS-1.2 | Full Build with empty description (default opener) | e2e/full-build.spec.ts | partial (opener send + enabled-Start yes; agent reply needs LLM) |
| CS-1.3 | Six assets stream into the Asset Workspace | e2e/asset-workspace-stream.spec.ts | partial (tab order/sub-selector/controls via mocked frames; real asset content no) |
| CS-1.4 | Package and download the bundle | e2e/package-download.spec.ts | partial (button states/modal/Retry yes; real ZIP contents no) |
| CS-2.1 | Single-segment Contact Flow only | e2e/single-segment.spec.ts | partial (scope `['contact_flow']`/trimmed progress yes; flow content needs LLM) |
| CS-2.2 | Single-segment AI Prompt or FAQ | e2e/single-segment.spec.ts | partial (scope/tab/KB-lane mapping yes; content needs LLM) |
| CS-2.3 | Start disabled until segment + input | e2e/start-gating.spec.ts | yes |
| CS-3.1 | Import contact_flow.json, lint/auto-repair | e2e/improve-import-flow.spec.ts | partial (upload→scope→workspace/file-tree via mocked repair; real lint/repair no) |
| CS-3.2 | Import AI Prompt YAML, duplicate-var fix | e2e/improve-import-prompt.spec.ts | partial (upload→scope→tab yes; real qconnect lint no) |
| CS-3.3 | Whiteboard photo → draft Contact Flow | e2e/improve-photo.spec.ts | partial (image attach/preview/scope/diagram render via mock; vision transcription no) |
| CS-3.4 | Graceful unreadable/non-flow photo | e2e/improve-photo-errors.spec.ts | partial (no-crash/composer-stays-usable yes; agent vision reply no) |
| CS-3.5 | Reject wrong file type inline | e2e/attachment-validation.spec.ts | yes |
| CS-3.6 | Patch-only refine after import | e2e/patch-only-edit.spec.ts | partial (patch-vs-regen protocol/workspace reload yes; edit quality needs LLM) |
| CS-3.7 | Flow imports into real Connect API | e2e/connect-api-import.spec.ts | no (real CreateContactFlow API + LLM-generated content) |
| CS-4.1 | Model select: persist + mid-session switch | e2e/model-selection.spec.ts | yes (selection/persist/sync/model-id-on-frame; LLM diff not asserted) |
| CS-5.1 | 12-step / 4-phase progress | e2e/progress-full.spec.ts | yes (driven by mocked progress frames) |
| CS-5.2 | Honest scoped X/N counter | e2e/progress-scoped.spec.ts | yes |
| CS-5.3 | Contact Flow interactive diagram | e2e/contact-flow-diagram.spec.ts | partial (render/drag/JSON-tab/fullscreen via fixture JSON; validity for real API no) |
| CS-5.4 | Assets show in right pane, not just toast | e2e/right-pane-reveal.spec.ts | yes |
| CS-5.5 | Right pane switch/resize/collapse | e2e/right-pane-layout.spec.ts | yes |
| CS-5.6 | Jump-to-latest + collapsed tool calls | e2e/long-chat-nav.spec.ts | yes |
| CS-5.7 | Download generated flow / FAQ (popup fallback) | e2e/package-download.spec.ts | partial (states/fallback/modal yes; real bundle no) |
| CS-6.1 | Multi-session lifecycle | e2e/sessions.spec.ts | yes |
| CS-7.1 | Korean UI throughout + persistent switch | e2e/localization.spec.ts | yes |
| CS-8.1 | Errors, reconnect, reset, stop | e2e/resilience.spec.ts | yes (socket drop/reconnect/"still processing"/stop simulated on mock) |
| CS-9.1 | Localized, accessible login page | e2e/login.spec.ts | partial (UI/i18n/policy-checklist/a11y yes; real Cognito auth no) |
| CS-9.2 | Theme toggle across timeline | e2e/theme.spec.ts | yes |