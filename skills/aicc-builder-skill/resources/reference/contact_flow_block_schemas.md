# Verified Amazon Connect Contact Flow block schemas

> **Authored skill resource** (NOT auto-extracted). In the webapp, Contact-Flow
> block syntax comes from a curated Bedrock Knowledge Base (`retrieve_contact_flow_knowledge`,
> RAG against `CONTACT_FLOW_KB_ID`) plus an optional live AWS-docs web lookup. A CLI
> skill has neither, so this file vendors the **API-verified** ground truth so the
> contact-flow generator and the vision-import path produce import-safe JSON offline.
> When in doubt about a block not listed here, use **WebSearch / WebFetch against
> `docs.aws.amazon.com/connect`** — NOT the research agent (that is for company/API
> research, not flow-block syntax).

Validated against the real `aws connect create-contact-flow` API (all-blocks console
export, verified 2026-06-08; re-validated 2026-07-03 across ~700 API calls). The API is
the ground truth — official docs and the console block-name list are **not** enough to
know the JSON `Type` string or the required params. Guessed names such as
`TransferToAgent` or `CheckStaffing` fail import with
`InvalidContactFlowException: Invalid Action type`.

**Run the linter, don't trust your memory.** Everything below is enforced
executably by [`../scripts/lint_assets.py`](../scripts/lint_assets.py) (generated
from the webapp's `asset_linters.py`, so it can't drift from the webapp):

```bash
python3 resources/scripts/lint_assets.py <output_dir>          # report
python3 resources/scripts/lint_assets.py <output_dir> --fix     # apply deterministic fixes
```

It auto-renames every invalid `Type` in the table below, adds missing required error
branches, strips `Transitions` from terminal blocks, and normalizes the tool-result
values. Treat this doc as the explanation and the linter as the gate.

## Console name → JSON `Type` (API-verified)

| Console block | JSON `Type` |
|---|---|
| Get customer input / Store customer input | `GetParticipantInput` |
| Play prompt / Send message | `MessageParticipant` |
| Message iteratively | `MessageParticipantIteratively` |
| Render message template (Q in Connect) | `RenderMessageTemplate` |
| Connect assistant (Q in Connect) | `CreateWisdomSession` |
| Q in Connect AI bot | `ConnectParticipantWithLexBot` |
| Set callback number | `UpdateContactCallbackNumber` |
| Set contact attributes | `UpdateContactAttributes` |
| Set flow attributes | `UpdateFlowAttributes` |
| Set working queue | `UpdateContactTargetQueue` |
| Set voice | `UpdateContactTextToSpeechVoice` |
| Set logging behavior | `UpdateFlowLoggingBehavior` |
| Set recording / analytics | `UpdateContactRecordingAndAnalyticsBehavior` (legacy `UpdateContactRecordingBehavior` also imports) |
| Set event flow / hooks | `UpdateContactEventHooks` |
| Set routing criteria | `UpdateContactRoutingCriteria` |
| Change routing priority / age | `UpdateContactRoutingBehavior` |
| Media streaming (start / stop) | `UpdateContactMediaStreamingBehavior` |
| Update previous participant state | `UpdatePreviousContactParticipantState` |
| Check hours of operation | `CheckHoursOfOperation` |
| Check contact attributes | `Compare` |
| Check staffing / Get metrics | `CheckMetricData`, `GetMetricData` |
| Check call progress (outbound) | `CheckOutboundCallStatus` |
| Loop | `Loop` |
| Wait | `Wait` |
| Distribute by percentage | `DistributeByPercentage` |
| AWS Lambda function | `InvokeLambdaFunction` |
| Invoke module | `InvokeFlowModule` |
| Create task | `CreateTask` |
| Cases (create) | `CreateCase` |
| Create contact | `CreateContact` |
| Outbound email | `StartOutboundEmailContact` |
| Customer profiles | `GetCustomerProfile`, `GetCustomerProfileObject`, `AssociateContactToCustomerProfile` |
| Data table | `EvaluateDataTableValues` |
| Get stored content | `LoadContactContent` |
| Authenticate customer | `AuthenticateParticipant` |
| Show view | `ShowView` |
| Resume contact | `ResumeContact` |
| Contact tags (tag / untag) | `TagContact`, `UntagContact` |
| Persistent contact association | `CreatePersistentContactAssociation` |
| Transfer to queue | `TransferContactToQueue` |
| Dequeue + transfer to queue | `DequeueContactAndTransferToQueue` |
| Transfer to phone number / third party | `TransferParticipantToThirdParty` |
| Transfer to flow | `TransferToFlow` |
| Disconnect | `DisconnectParticipant` |
| End flow / resume | `EndFlowExecution` |
| Update contact data | `UpdateContactData` |

That is the complete verified set (51 `Type`s). Anything not in it should be
confirmed against a real console export before you emit it — the linter rejects
unknown `Type`s outright rather than guessing.

## INVALID `Type`s the model/KB tends to hallucinate (NEVER emit)

| Wrong (guessed) | Use instead |
|---|---|
| `PlayPrompt` | `MessageParticipant` |
| `Disconnect` / `EndFlow` | `DisconnectParticipant` |
| `GetUserInput` / `StoreUserInput` | `GetParticipantInput` (`StoreInput=True` for store mode) |
| `TransferToAgent` / `TransferToQueue` | `TransferContactToQueue` |
| `TransferToPhoneNumber` / `TransferToThirdParty` | `TransferParticipantToThirdParty` |
| `CheckStaffing` / `CheckQueueStatus` | `CheckMetricData` |
| `CheckCondition` / `CheckValue` / `CheckAttribute` / `Condition` | `Compare` |
| `Distribute` | `DistributeByPercentage` |
| `StartMediaStreaming` / `StopMediaStreaming` | `UpdateContactMediaStreamingBehavior` |
| `ReturnFromFlowModule` | `EndFlowExecution` |
| `InvokeAPI` | `InvokeLambdaFunction` |
| `InvokeAgentAction` / `InvokeBedrockAgent` / `InvokeQConnect` / `InvokeAmazonQConnect` / `GetParticipantWithLexV2Bot` | `ConnectParticipantWithLexBot` |
| `SetContactAttributes` | `UpdateContactAttributes` |
| `SetCallbackNumber` | `UpdateContactCallbackNumber` |
| `SetWorkingQueue` | `UpdateContactTargetQueue` |
| `SetVoice` | `UpdateContactTextToSpeechVoice` |
| `SetLoggingBehavior` | `UpdateFlowLoggingBehavior` |
| `SetRecordingBehavior` | `UpdateContactRecordingBehavior` |
| `SetRecordingAndAnalyticsBehavior` | `UpdateContactRecordingAndAnalyticsBehavior` |
| `UpdateContactRoutingData` | `UpdateContactRoutingCriteria` |
| `UpdateContactTextToSpeechManner` | `UpdateContactTextToSpeechVoice` |
| `PutCustomerProfile` / `UpdateCustomerProfileObject` | Customer Profiles integration / `UpdateContactData` |
| `CreateCallbackContact` | remove — use `UpdateContactCallbackNumber` + `TransferContactToQueue` |
| `Trigger` / `EntryPoint` | remove — flows start at `StartAction`'s target, there is no entry block |

## Per-block required params / errors (the ones most often gotten wrong)

- **GetParticipantInput** has TWO modes, and **both** require `InputTimeLimitSeconds`
  (omitting it fails with `Action is missing required property … Parameters.InputTimeLimitSeconds`):
  - **MENU mode** (DTMF branching via `Conditions`): `StoreInput: "False"`, **NO**
    `DTMFConfiguration`; errors = `NoMatchingCondition` + `InputTimeLimitExceeded` +
    `NoMatchingError`.
  - **STORE mode** (captures to an attribute): `StoreInput: "True"`, requires
    `InputValidation.CustomValidation.MaximumLength`; optional
    `DTMFConfiguration.DisableCancelKey` (**never** `InputTerminationSequence`);
    errors = `NoMatchingError` only.
- **Loop**: `LoopCount` param; `Conditions` operands are `ContinueLooping` /
  `DoneLooping` (NOT `Looping`/`Complete`), **and** the block still needs a
  `Transitions.NextAction` in addition to those two conditions.
- **TransferContactToQueue**: **NO** `QueueId` param (the queue is set by a prior
  `UpdateContactTargetQueue`); errors = `QueueAtCapacity` + `NoMatchingError`.
- **UpdateContactAttributes**: requires `NoMatchingError`.
- **UpdateContactCallbackNumber**: errors are `InvalidCallbackNumber` +
  `CallbackNumberNotDialable`; `NoMatchingError` / `InvalidNumber` / `NotDialable`
  are **NOT** valid here. `CallbackNumber` must be a valid E.164 number **or** a
  JSONPath (e.g. `$.CustomerEndpoint.Address`) — a bare literal was rejected on the
  verified instance.
- **DisconnectParticipant** / **EndFlowExecution**: terminal — **NO**
  `Transitions`/`Conditions`/`Errors`.
- **TransferContactToQueue** / **TransferToFlow**: terminal for the *contact*, but
  they DO carry error branches (unlike the two above).
- **Compare**: only `NoMatchingCondition` (never `NoMatchingError`).
  Operators are `Equals`, `TextContains`, `TextStartsWith`, `TextEndsWith`,
  `NumberGreaterThan`, `NumberLessThan`, `NumberGreaterOrEqualTo`,
  `NumberLessOrEqualTo`. Note the asymmetry: `>=`/`<=` drop the "Than"
  (`NumberGreaterOrEqualTo`, not `NumberGreaterThanOrEqualTo`), and there is **no**
  `NumberEquals` — use plain `Equals` for numeric equality.
  `ComparisonValue` must use a real JSONPath root: `$.Attributes.`, `$.Channel`,
  `$.CustomerEndpoint`, `$.SystemEndpoint`, `$.Lex.`, `$.Customer.`, `$.External.`,
  `$.StoredCustomerInput`, `$.Media.`, `$.ContactId`, `$.InitialContactId`,
  `$.Queue.`, `$.Metadata.`, `$.FlowAttributes.`.
- **CheckHoursOfOperation**: `NoMatchingError` only — `NoMatchingCondition` is invalid.
- **ConnectParticipantWithLexBot**: errors = `NoMatchingError` +
  `NoMatchingCondition`; `AgentError` is invalid.
- **CreateWisdomSession**: `WisdomAssistantArn` must be a real existing assistant
  ARN (use a `{{PLACEHOLDER}}` token until deploy time); requires `NoMatchingError`.
- **RenderMessageTemplate**: the Q-in-Connect message-template render block, **not**
  a generic templated message. Requires `WisdomKnowledgeBaseArn` +
  `WisdomMessageTemplateArn` (generic `Template` / `AttributeMap` params are rejected
  as invalid property names); errors = `TemplateRenderingError` + `NoMatchingError`.
- **UpdateContactRecordingAndAnalyticsBehavior**: real-time redaction is only
  supported for `de-DE`, `en-AU`, `en-GB`, `en-IN`, `en-US`, `es-US`, `fr-CA`,
  `fr-FR`, `it-IT`, `pt-BR`. Korean (`ko-KR`) flows must NOT request redaction —
  set the analytics language explicitly and leave redaction off.
- **AuthenticateParticipant**: errors = `NoMatchingError` + `TimeLimitExceeded`.
- **GetCustomerProfileObject**: errors = `NoMatchingError` + `NoneFoundError`.

## AI bot ↔ Contact Flow tool-result contract

The `Compare` block that branches on the AI agent's outcome must compare on
`$.Lex.SessionAttributes.Tool`, and the AI prompt must teach the bot to set
**exactly** `Complete` or `Escalate` (plus the documented `Escalate*` / `*Complete`
extensions). Near-miss values the linter normalizes — `Done`, `Finish(ed)`,
`EndCall`, `EndConversation`, `Hangup`, `Disconnect` → `Complete`; `Escalation`,
`Transfer`, `TransferToAgent`, `Handoff`, `Human`, `Agent` → `Escalate` — mean the
bot and the flow silently disagreed at runtime, so fix the source, not just the flow.
See the canonical contract in
[`../sub-agents/_shared_rules.md`](../sub-agents/_shared_rules.md) →
"AI-BOT ↔ CONTACT-FLOW TOOL-RESULT CONTRACT".

## AI prompt variable rule (qconnect `CreateAIPrompt`)

Each variable may appear inside `{{ }}` **only once** in the whole prompt; a second
`{{$.Custom.foo}}` fails validation. Reference it without braces after the first
use. `lint_assets.py` strips the braces from duplicates automatically.
