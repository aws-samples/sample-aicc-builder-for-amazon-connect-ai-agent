# Common Contact Flow Import Errors

## Question
What are the common errors when importing Contact Flows into Amazon Connect?

## Answer
Contact Flow import failures are usually caused by incorrect JSON syntax, missing required fields, or using non-existent block types. Here are the most common errors and their solutions.

## Error Categories

### 1. Block Type Errors

#### Wrong Block Type Name
| Error | Wrong | Correct |
|-------|-------|---------|
| Logging | SetLoggingBehavior | UpdateFlowLoggingBehavior |
| Set Attributes | SetContactAttributes | UpdateContactAttributes |
| Store Input | StoreCustomerInput | StoreUserInput |
| Create Callback | CreateCallbackContact | UpdateContactCallbackNumber + TransferContactToQueue |
| Queue metrics | GetQueueMetrics | CheckMetricData |
| Staffing | CheckStaffing | CheckMetricData (MetricType: NumberOfAgentsAvailable) |
| Entry/trigger | Trigger / EntryPoint | (none — flow starts at StartAction's target) |
| AI agent | InvokeAgentAction / InvokeBedrockAgent | ConnectParticipantWithLexBot |
| Condition | CheckCondition / CheckValue | Compare |

Fix: Use the correct block type names as listed in AWS documentation.

### 2. Parameter Errors

#### Missing ProfileRequestData Wrapper
**Error**: "Invalid parameter format for GetCustomerProfile"

WRONG:
```json
{
  "Type": "GetCustomerProfile",
  "Parameters": {
    "IdentifierName": "_phone",
    "IdentifierValue": "$.CustomerEndpoint.Address"
  }
}
```

CORRECT:
```json
{
  "Type": "GetCustomerProfile",
  "Parameters": {
    "ProfileRequestData": {
      "IdentifierName": "_phone",
      "IdentifierValue": "$.CustomerEndpoint.Address"
    }
  }
}
```

Same applies to: AssociateContactToCustomerProfile

#### ResponseType value
**Note**: Both `STRING_MAP` and `JSON` are officially supported `ResponseType` values in the Amazon Connect Flow Language. Earlier versions of this guide incorrectly listed `JSON` as wrong — this has been corrected. Use `STRING_MAP` for flat key/value string maps (stricter, simpler) and `JSON` when the Lambda must return nested JSON.

```json
// Both are valid:
"ResponseValidation": {"ResponseType": "STRING_MAP"}
"ResponseValidation": {"ResponseType": "JSON"}
```

#### Wrong LoggingBehavior Parameter
**Error**: "Invalid parameter for UpdateFlowLoggingBehavior"

WRONG:
```json
{
  "Type": "UpdateFlowLoggingBehavior",
  "Parameters": {"LoggingBehavior": "Enable"}
}
```

CORRECT:
```json
{
  "Type": "UpdateFlowLoggingBehavior",
  "Parameters": {"FlowLoggingBehavior": "Enabled"}
}
```

### 2b. API-Validated Parameter Name Errors (exact property names)

These are the EXACT `InvalidContactFlowException` problems Amazon Connect's
`CreateContactFlow` API returns. The API validator is stricter than the console
preview — a flow that looks fine can still be rejected for these. Each fix below
was confirmed by a real successful import.

#### UpdateContactRecordingBehavior — `Agent`/`Customer` are not valid
**API error**: `Invalid Action property name. Path: …Parameters.Agent` /
`Action is missing required property. Path: …Parameters.RecordingBehavior`

WRONG:
```json
{"Type": "UpdateContactRecordingBehavior", "Parameters": {"Agent": "Enabled", "Customer": "Enabled"}}
```
CORRECT:
```json
{"Type": "UpdateContactRecordingBehavior",
 "Parameters": {
   "RecordingBehavior": {"RecordedParticipants": ["Agent", "Customer"], "IVRRecordingBehavior": "Enabled"},
   "AnalyticsBehavior": {"Enabled": "True", "AnalyticsLanguage": "ko-KR",
     "ChannelConfiguration": {"Chat": {"AnalyticsModes": ["ContactLens"]}, "Voice": {"AnalyticsModes": ["PostContact"]}}}}}
```

#### UpdateContactTextToSpeechVoice — `VoiceId`/`Engine`/`LanguageCode` are not valid
**API error**: `Invalid Action property name. Path: …Parameters.VoiceId` /
`Action is missing required property. Path: …Parameters.TextToSpeechVoice`

WRONG: `{"VoiceId": "Seoyeon", "Engine": "Generative", "LanguageCode": "ko-KR"}`
CORRECT: `{"TextToSpeechVoice": "Seoyeon", "TextToSpeechEngine": "Generative"}`
(Set the language in `ActionMetadata`, not in Parameters.)

#### UpdateContactTargetQueue — `QueueId` must be a string, not nested
**API error**: `Invalid Action property value. Path: …Parameters.QueueId`

WRONG: `{"Queue": "arn:…"}` or `{"QueueId": {"QueueId": "arn:…"}}`
CORRECT: `{"QueueId": "arn:aws:connect:…:queue/…"}`

#### InvokeLambdaFunction — `RequestAttributes` is not valid
**API error**: `Invalid Action property name. Path: …Parameters.RequestAttributes`

WRONG: `{"RequestAttributes": {"phone": "$.CustomerEndpoint.Address"}}`
CORRECT: `{"LambdaInvocationAttributes": {"phone": "$.CustomerEndpoint.Address"}}`
Also: `ResponseValidation.ResponseType` must be `STRING_MAP`.

#### ConnectParticipantWithLexBot — only `LexV2Bot.AliasArn` + one message
**API errors**: `Invalid Action property name. …Parameters.BotAliasArn` /
`…ParticipantRole` / `…SessionAttributes`; `Action is missing required property.
…Parameters.LexBot`; `At least one of [Text, SSML, PromptId, Media, LexInitializationData] must be set`.

WRONG: `{"BotAliasArn": "arn:…", "ParticipantRole": "…", "SessionAttributes": {…}}`
or `{"LexBot": {"AliasArn": "arn:…"}}`
CORRECT:
```json
{"Type": "ConnectParticipantWithLexBot",
 "Parameters": {"Text": "안녕하세요…", "LexV2Bot": {"AliasArn": "arn:aws:lex:…:bot-alias/…/…"},
                "LexSessionAttributes": {"key": "value"}},
 "Transitions": {"NextAction": "check-result",
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "check-result"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```
Its valid Error types are `NoMatchingError` and `NoMatchingCondition` — NEVER `AgentError`.

#### Compare — `NoMatchingError` is not a valid Error; ComparisonValue needs a real root
**API errors**: `Invalid Action error. Error: NoMatchingError, Path: …` /
`Invalid Action property value. Path: …Parameters.ComparisonValue`

- `Compare` branches via `Conditions`; its ONLY Error is `NoMatchingCondition`.
- `ComparisonValue` MUST use a real JSONPath root: `$.Attributes.X`, `$.Channel`,
  `$.Lex.SessionAttributes.X`, `$.CustomerEndpoint.Address`, `$.External.X`,
  `$.StoredCustomerInput`. There is NO `$.Agent.*` namespace — read AI/bot
  results from `$.Lex.SessionAttributes.*` or a contact attribute you set.

#### CheckHoursOfOperation — needs id + BOTH True/False branches
**API errors**: `Invalid Action property value. …Transitions.Conditions` /
`Action is missing required error. Error: NoMatchingError`

- `HoursOfOperationId` must be a non-null ARN/id.
- `Transitions.Conditions` MUST include BOTH a `True` and a `False` operand.
- The only valid Error is `NoMatchingError` (NOT `NoMatchingCondition`).

```json
{"Type": "CheckHoursOfOperation",
 "Parameters": {"HoursOfOperationId": "arn:aws:connect:…:operating-hours/…"},
 "Transitions": {"NextAction": "after-hours",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]},  "NextAction": "open"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "after-hours"}],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "after-hours"}]}}
```

#### Terminal blocks carry NOTHING but Parameters
`DisconnectParticipant` / `EndFlowExecution` / `ReturnFromFlowModule` must NOT
have `Transitions`, `Conditions`, OR `Errors` (not even empty ones as stray
top-level keys). **API error**: `Action does not support transitions. Path: …`

#### MessageParticipant — exactly ONE message property
**API error**: `Only one of these properties may be defined. Properties: [Text, SSML]`

Provide exactly ONE of `Text` / `SSML` / `PromptId` / `Media` — never both
`Text` and `SSML` on the same block.

### 3. Transition Errors

#### Missing Transitions Object
**Error**: "Every action must have a Transitions property"

WRONG:
```json
{
  "Identifier": "my-action",
  "Type": "MessageParticipant",
  "Parameters": {"Text": "Hello"}
}
```

CORRECT:
```json
{
  "Identifier": "my-action",
  "Type": "MessageParticipant",
  "Parameters": {"Text": "Hello"},
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "next-block"}]
  }
}
```

#### Wrong DisconnectParticipant Transitions
**Error**: "DisconnectParticipant must have empty transitions"

WRONG:
```json
{
  "Type": "DisconnectParticipant",
  "Parameters": {},
  "Transitions": {"NextAction": "some-block"}
}
```

CORRECT:
```json
{
  "Type": "DisconnectParticipant",
  "Parameters": {},
  "Transitions": {}
}
```

#### Missing Required Error Types
**Error**: "GetParticipantInput requires InputTimeLimitExceeded error"

WRONG:
```json
{
  "Type": "GetParticipantInput",
  "Transitions": {
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error"}]
  }
}
```

CORRECT:
```json
{
  "Type": "GetParticipantInput",
  "Transitions": {
    "Errors": [
      {"ErrorType": "InputTimeLimitExceeded", "NextAction": "timeout"},
      {"ErrorType": "NoMatchingCondition", "NextAction": "invalid"},
      {"ErrorType": "NoMatchingError", "NextAction": "error"}
    ]
  }
}
```

### 4. Metadata Errors

#### Missing ActionMetadata
**Error**: "ActionMetadata missing for action [identifier]"

Every action's Identifier MUST have a corresponding entry in Metadata.ActionMetadata:

```json
{
  "Metadata": {
    "ActionMetadata": {
      "my-action-id": {
        "position": {"x": 280, "y": 40},
        "isFriendlyName": true
      }
    }
  },
  "Actions": [
    {
      "Identifier": "my-action-id",
      ...
    }
  ]
}
```

#### Missing Required Metadata Fields
**Error**: "Missing required metadata field"

Required Metadata fields:
```json
{
  "Metadata": {
    "entryPointPosition": {"x": 40, "y": 40},
    "ActionMetadata": {...},
    "name": "Flow Name",
    "type": "contactFlow",
    "status": "DRAFT",
    "hash": {}
  }
}
```

### 5. Reference Errors

#### Invalid NextAction Reference
**Error**: "NextAction references non-existent action"

Check that all NextAction values match existing Identifiers:
- Case-sensitive: "My-Action" ≠ "my-action"
- No typos
- No missing actions

#### StartAction Mismatch
**Error**: "StartAction does not match any action Identifier"

```json
{
  "StartAction": "first-action",  // Must match exactly
  "Actions": [
    {"Identifier": "first-action", ...}  // Must exist
  ]
}
```

## Validation Checklist

Before importing, verify:
- [ ] All block types are spelled correctly
- [ ] ProfileRequestData wrapper used for Customer Profile blocks
- [ ] ResponseType is "STRING_MAP" for Lambda
- [ ] FlowLoggingBehavior (not LoggingBehavior) for logging
- [ ] Every action has Transitions (even if empty {})
- [ ] DisconnectParticipant has Parameters: {} and Transitions: {}
- [ ] All required error types are present
- [ ] ActionMetadata exists for every Identifier
- [ ] All metadata fields present (entryPointPosition, name, type, status, hash)
- [ ] StartAction matches an existing Identifier
- [ ] All NextAction values reference existing Identifiers

## Quick Reference: Block Type Names

| Category | Correct Block Type |
|----------|-------------------|
| Logging | UpdateFlowLoggingBehavior |
| Recording | UpdateContactRecordingBehavior |
| Voice | UpdateContactTextToSpeechVoice |
| Attributes | UpdateContactAttributes |
| Queue | UpdateContactTargetQueue |
| Transfer | TransferContactToQueue |
| Staffing / Metrics | CheckMetricData |
| Hours | CheckHoursOfOperation |
| Lambda | InvokeLambdaFunction |
| Profile | GetCustomerProfile |
| Associate | AssociateContactToCustomerProfile |
| Callback | UpdateContactCallbackNumber |
| Input | GetParticipantInput |
| Store | StoreUserInput |
| Message | MessageParticipant |
| Compare | Compare |
| Lex | ConnectParticipantWithLexBot |
| Loop | Loop |
| Wait | Wait |
| End | DisconnectParticipant |

## Related Topics
- Error Handling Best Practices
- Block Type Reference
- Contact Flow JSON Structure

---
**Metadata**
- Category: Troubleshooting
- Keywords: import error, validation, troubleshooting, fix, common errors
