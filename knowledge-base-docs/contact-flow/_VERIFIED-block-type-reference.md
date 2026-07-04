# Amazon Connect Flow Block Type Reference (API-VERIFIED)

## Question
What are the valid Amazon Connect contact-flow block `Type` strings, and which
console block name maps to which JSON `Type`?

## Answer
This list is the GROUND TRUTH. Every `Type` below was confirmed to import via the
real `CreateContactFlow` API (verified 2026-06-08 against a console export of all
blocks). Do NOT invent or guess `Type` names — guessed names like `TransferToAgent`
or `CheckStaffing` FAIL import with `InvalidContactFlowException: Invalid Action type`.

### Console name → JSON `Type` (verified)
| Console block | JSON `Type` |
|---|---|
| Get customer input / Store customer input | `GetParticipantInput` |
| Play prompt / Send message | `MessageParticipant` |
| Render message template (Q in Connect / Wisdom) | `RenderMessageTemplate` |
| Connect assistant (Q in Connect) | `CreateWisdomSession` |
| Set callback number | `UpdateContactCallbackNumber` |
| Set contact attributes | `UpdateContactAttributes` |
| Set working queue | `UpdateContactTargetQueue` |
| Set voice | `UpdateContactTextToSpeechVoice` |
| Set logging behavior | `UpdateFlowLoggingBehavior` |
| Set recording / analytics | `UpdateContactRecordingAndAnalyticsBehavior` (also legacy `UpdateContactRecordingBehavior`) |
| Set event flow / hooks | `UpdateContactEventHooks` |
| Set routing criteria | `UpdateContactRoutingCriteria` |
| Change routing priority / age | `UpdateContactRoutingBehavior` |
| Set flow attributes | `UpdateFlowAttributes` |
| Media streaming (start/stop) | `UpdateContactMediaStreamingBehavior` |
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
| Contact tags (tag/untag) | `TagContact`, `UntagContact` |
| Create persistent contact association | `CreatePersistentContactAssociation` |
| Q in Connect AI bot | `ConnectParticipantWithLexBot` |
| Message iteratively | `MessageParticipantIteratively` |
| Update previous participant state | `UpdatePreviousContactParticipantState` |
| Transfer to queue | `TransferContactToQueue` |
| Transfer to phone number / third party | `TransferParticipantToThirdParty` |
| Transfer to flow | `TransferToFlow` |
| Disconnect | `DisconnectParticipant` |
| End flow / resume | `EndFlowExecution` |

### ❌ INVALID Types — never emit (they fail import)
| Wrong (guessed) | Use instead |
|---|---|
| `TransferToAgent` | `TransferContactToQueue` |
| `TransferToPhoneNumber` / `TransferToThirdParty` | `TransferParticipantToThirdParty` |
| `CheckStaffing` / `CheckQueueStatus` | `CheckMetricData` |
| `Distribute` | `DistributeByPercentage` |
| `StartMediaStreaming` / `StopMediaStreaming` | `UpdateContactMediaStreamingBehavior` |
| `ReturnFromFlowModule` | `EndFlowExecution` |
| `InvokeAPI` | `InvokeLambdaFunction` |
| `GetParticipantWithLexV2Bot` | `ConnectParticipantWithLexBot` |
| `PutCustomerProfile` / `UpdateCustomerProfileObject` | Customer Profiles integration |
| `UpdateContactRoutingData` | `UpdateContactRoutingCriteria` |
| `UpdateContactTextToSpeechManner` | `UpdateContactTextToSpeechVoice` |
| `SetLoggingBehavior` | `UpdateFlowLoggingBehavior` |
| `PlayPrompt` | `MessageParticipant` |
| `StoreUserInput` | `GetParticipantInput` (StoreInput=True) |
| `Trigger` / `EntryPoint` | none — flow starts at `StartAction`'s target |
| `InvokeAgentAction` / `InvokeBedrockAgent` | `ConnectParticipantWithLexBot` |
| `CheckCondition` / `CheckValue` | `Compare` |

### Key per-block schema gotchas (API-verified)
- **GetParticipantInput** has TWO modes. BOTH modes require `InputTimeLimitSeconds`
  (omitting it fails: `Action is missing required property ... Parameters.InputTimeLimitSeconds`).
  - *Menu* (branches via Conditions): `StoreInput:"False"`, `InputTimeLimitSeconds`,
    NO `DTMFConfiguration`;
    errors = `NoMatchingCondition` + `InputTimeLimitExceeded` + `NoMatchingError`.
  - *Store* (captures to attribute): `StoreInput:"True"`, `InputTimeLimitSeconds`, requires
    `InputValidation.CustomValidation.MaximumLength`, optional
    `DTMFConfiguration.DisableCancelKey` (NEVER `InputTerminationSequence`);
    error = `NoMatchingError`.
- **Loop**: Conditions operands are `ContinueLooping` / `DoneLooping` (NOT Looping/Complete),
  AND the block also requires a `Transitions.NextAction` in addition to those two Conditions
  (omitting it fails: `Action is missing required property ... Transitions.NextAction`).
- **TransferContactToQueue**: NO `QueueId` param (queue is set by a prior
  `UpdateContactTargetQueue`); errors `QueueAtCapacity` + `NoMatchingError`.
- **UpdateContactAttributes**: requires `NoMatchingError`.
- **UpdateContactCallbackNumber**: requires errors `InvalidCallbackNumber` + `CallbackNumberNotDialable`;
  `NoMatchingError` is NOT allowed. `CallbackNumber` must be a valid E.164 number or a
  JSONPath (e.g. `$.CustomerEndpoint.Address`) — a bare literal like `+15551234567` was
  rejected as an invalid property value on the verified instance.
- **Compare**: only `NoMatchingCondition` error; `ComparisonValue` must use a real
  JSONPath root (`$.Attributes.*`, `$.Channel`, `$.Lex.SessionAttributes.*`, etc.).
- **DisconnectParticipant** / **EndFlowExecution**: terminal — NO Transitions.
- **CreateWisdomSession**: `WisdomAssistantArn` must be a real existing assistant ARN.
- **RenderMessageTemplate**: this is the Q in Connect / Wisdom message-template render block,
  NOT a generic templated message. Requires `WisdomKnowledgeBaseArn` + `WisdomMessageTemplateArn`
  (generic `Template` / `AttributeMap` params are rejected as invalid property names) and errors
  `TemplateRenderingError` + `NoMatchingError`.

---
**Metadata**
- Category: Reference
- Keywords: block types, action type, valid blocks, invalid blocks, import error, console mapping, verified
