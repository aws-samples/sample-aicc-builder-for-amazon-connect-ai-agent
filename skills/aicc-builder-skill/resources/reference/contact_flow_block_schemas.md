# Verified Amazon Connect Contact Flow block schemas

> **Authored skill resource** (NOT auto-extracted). In the webapp, Contact-Flow
> block syntax comes from a curated Bedrock Knowledge Base (`retrieve_contact_flow_knowledge`,
> RAG against `CONTACT_FLOW_KB_ID`) plus an optional live AWS-docs web lookup. A CLI
> skill has neither, so this file vendors the **API-verified** ground truth so the
> contact-flow generator and the vision-import path produce import-safe JSON offline.
> When in doubt about a block not listed here, use **WebSearch / WebFetch against
> `docs.aws.amazon.com/connect`** — NOT the research agent (that is for company/API
> research, not flow-block syntax).

Validated against the real `aws connect create-contact-flow` API. The API is the
ground truth — official docs and the console block-name list are **not** enough to
know the JSON `Type` string or the required params.

## Console name → JSON `Type` (API-verified)

| Console block | JSON `Type` |
|---|---|
| Get customer input | `GetParticipantInput` |
| Store customer input | `GetParticipantInput` (`StoreInput=True`) |
| Play prompt / Send message | `MessageParticipant` |
| Connect assistant | `CreateWisdomSession` |
| Set callback number | `UpdateContactCallbackNumber` |
| Set contact attributes | `UpdateContactAttributes` |
| Set working queue | `UpdateContactTargetQueue` |
| Set voice | `UpdateContactTextToSpeechVoice` |
| Set logging behavior | `UpdateFlowLoggingBehavior` |
| Set recording/analytics | `UpdateContactRecordingBehavior` |
| Check hours of operation | `CheckHoursOfOperation` |
| Check contact attributes | `Compare` |
| Check staffing / Get metrics | `CheckMetricData` |
| AWS Lambda function | `InvokeLambdaFunction` |
| Invoke module | `InvokeFlowModule` |
| Transfer to queue | `TransferContactToQueue` |
| Disconnect | `DisconnectParticipant` |
| Loop | `Loop` · Wait → `Wait` · Resume contact → `ResumeContact` |

## INVALID `Type`s the model/KB tends to hallucinate (NEVER emit)

| Wrong | Correct |
|---|---|
| `CheckStaffing` | `CheckMetricData` |
| `SetLoggingBehavior` | `UpdateFlowLoggingBehavior` |
| `PlayPrompt` | `MessageParticipant` |
| `StoreUserInput` | `GetParticipantInput` (`StoreInput=True`) |
| `Trigger` / `EntryPoint` | none — flow starts at `StartAction`'s target |
| `InvokeAgentAction` | `ConnectParticipantWithLexBot` |
| `CheckCondition` | `Compare` |

## Per-block required params / errors (the ones most often gotten wrong)

- **GetParticipantInput — MENU mode** (DTMF branching via `Conditions`):
  `StoreInput=False`, **NO** `DTMFConfiguration`; errors =
  `NoMatchingCondition` + `InputTimeLimitExceeded` + `NoMatchingError`.
- **GetParticipantInput — STORE mode**: `StoreInput=True`, requires
  `InputValidation.CustomValidation.MaximumLength`; optional
  `DTMFConfiguration.DisableCancelKey` (**never** `InputTerminationSequence`);
  errors = `NoMatchingError` only.
- **Loop**: `LoopCount` param; `Conditions` operands are `DoneLooping` /
  `ContinueLooping` (NOT `Looping`/`Complete`).
- **TransferContactToQueue**: **NO** `QueueId` param (queue is set by a prior
  `UpdateContactTargetQueue`); errors = `QueueAtCapacity` + `NoMatchingError`.
- **UpdateContactAttributes**: requires `NoMatchingError`.
- **UpdateContactCallbackNumber**: only `NoMatchingError` (`InvalidNumber` /
  `NotDialable` are NOT valid here).
- **DisconnectParticipant**: terminal — **NO** `Transitions`/`Conditions`/`Errors`.
- **Compare**: only `NoMatchingCondition` error (never `NoMatchingError`);
  `ComparisonValue` must use a real JSONPath root.
- **CreateWisdomSession**: `WisdomAssistantArn` must be a real existing assistant
  ARN (use a `{{PLACEHOLDER}}` token until deploy time).

## AI bot ↔ Contact Flow tool-result contract

The `Compare` block that branches on the AI agent's outcome must compare on
`$.Lex.SessionAttributes.Tool`, and the AI prompt must teach the bot to set
**exactly** `Complete` or `Escalate` (plus the documented `Escalate*` / `*Complete`
extensions). See the canonical contract in
[`../sub-agents/_shared_rules.md`](../sub-agents/_shared_rules.md) →
"AI-BOT ↔ CONTACT-FLOW TOOL-RESULT CONTRACT". The bot and the flow MUST agree on
these exact values or they silently disagree at runtime.

## Still unknown — need a console export to confirm `Type`

Authenticate Customer, Get stored content, Contact tags, Hold customer or agent,
Change routing priority/age, Check call progress, Check queue status, Transfer to
flow, Transfer to phone number, Cases, Data Table, Show View, Set customer queue
flow, Set disconnect/hold/whisper/event flow, Set routing criteria, Set touchtone
buffer, Create task, Start/Stop media streaming. For these, confirm the `Type`
against a real console export or the verified list in
[`vision_import.md`](vision_import.md) before emitting.
