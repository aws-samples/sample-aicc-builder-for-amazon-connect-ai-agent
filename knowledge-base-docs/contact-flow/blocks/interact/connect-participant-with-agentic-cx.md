# ConnectParticipantWithAgenticCX Block (Agentic CX, Connect Customer)

## Question
How do I hand a contact to an Agentic CX Designer (ACXD) application from an Amazon Connect Contact Flow? What is the Flow Language JSON of the "Agentic CX" block, and how do its Default / Escalation / Error / Idle chat timeout branches map?

## Answer
The Agentic CX block is `"Type": "ConnectParticipantWithAgenticCX"`. It replaces the Lex block (`ConnectParticipantWithLexBot`) when the runtime target is ACXD: the block connects the participant to a deployed ACXD application (workspace + application + alias) and returns control to the flow on one of four outputs.

> ✅ **Verified 2026-09-10.** Shape taken from an Amazon Connect console export of
> a flow built in the designer, then re-imported through `CreateContactFlow`
> (import accepted for both `SAVED` and `PUBLISHED`). Connect does **not**
> validate `WorkspaceId` / `ApplicationId` / `Alias` at import time.
> Raw export: `_reference-console-export-agentic-cx-block.json` (same folder).

### Parameters
- `AgentConfiguration.WorkspaceId` — ACXD workspace id (UUID).
- `AgentConfiguration.ApplicationId` — ACXD application id (UUID).
- `AgentConfiguration.Alias` — ACXD application alias id (opaque, ~21 chars, e.g. the alias shown as "Production" in the console). The ACXD SDK does not list aliases; pick it in the block's dropdown or pass it through `ACXD_ALIAS_ID` to the bundled runner.
- `AgentConfiguration.ContextVariables` — map of ACXD context variable name → JSONPath source, e.g. `{"customerPhone": "$.CustomerEndpoint.Address"}`. At most 10 variables; read them back after the block as `$.AgenticCX.ContextVariables.<name>`.
- `SpeechRecognitionConfiguration.SpeechRecognitionEngine` — `"AMAZON_AGENTIC_VOICE"` for Amazon Connect agentic voice (the console's recommended engine). Omit the object to keep the instance default.
- `AudioFillerConfiguration` — optional filler while the agent thinks: `{"Enabled": true, "AudioType": "MELODY_CHIPPER_CHIME", "StartDelayInMilliseconds": 2500, "MinimumPlayDurationInMilliseconds": 3000, "ResponseDeliveryDelayInMilliseconds": 500}`.

### Branch mapping (console label → Flow Language)
| Console output | Flow Language |
|---|---|
| Default (conversation ended normally) | `Transitions.NextAction` |
| Escalation (agent asked for a human) | `Transitions.Conditions[]` with `{"Operator": "Equals", "Operands": ["Escalation"]}` |
| Error | `Errors[]` → `"ErrorType": "NoMatchingError"` |
| Idle chat timeout | `Errors[]` → `"ErrorType": "InputTimeLimitExceeded"` |
| (no branch matched) | `Errors[]` → `"ErrorType": "NoMatchingCondition"` — required |

All three error types are required for import. The block carries `Conditions`; it is NOT a `MessageParticipant` (that block rejects `Conditions` with "Action does not support conditions").

```json
{
  "Identifier": "AgenticCX",
  "Type": "ConnectParticipantWithAgenticCX",
  "Parameters": {
    "AgentConfiguration": {
      "WorkspaceId": "{ACXD_WORKSPACE_ID}",
      "ApplicationId": "{ACXD_APPLICATION_ID}",
      "Alias": "{ACXD_ALIAS_ID}",
      "ContextVariables": {"customerPhone": "$.CustomerEndpoint.Address"}
    },
    "SpeechRecognitionConfiguration": {"SpeechRecognitionEngine": "AMAZON_AGENTIC_VOICE"},
    "AudioFillerConfiguration": {
      "Enabled": true,
      "AudioType": "MELODY_CHIPPER_CHIME",
      "StartDelayInMilliseconds": 2500,
      "MinimumPlayDurationInMilliseconds": 3000,
      "ResponseDeliveryDelayInMilliseconds": 500
    }
  },
  "Transitions": {
    "NextAction": "disconnect",
    "Conditions": [
      {"NextAction": "transfer-queue", "Condition": {"Operator": "Equals", "Operands": ["Escalation"]}}
    ],
    "Errors": [
      {"NextAction": "fallback-message", "ErrorType": "NoMatchingError"},
      {"NextAction": "disconnect", "ErrorType": "NoMatchingCondition"},
      {"NextAction": "disconnect", "ErrorType": "InputTimeLimitExceeded"}
    ]
  }
}
```

### What NOT to put in an ACXD flow
- No `ConnectParticipantWithLexBot` — the Agentic CX block is the conversation.
- No `CreateWisdomSession` / Q in Connect session — FAQ answers come from the ACXD knowledge base; an unresolved `WisdomAssistantArn` placeholder fails the import ("Invalid Action property value").
- No `$.Lex.SessionAttributes.*` reads after the block — use `$.AgenticCX.ContextVariables.<name>`.

### Typical ACXD inbound flow
1. `UpdateFlowLoggingBehavior` → 2. optional `InvokeLambdaFunction` (customer lookup) + `UpdateContactAttributes` → 3. `UpdateContactTextToSpeechVoice` → 4. `UpdateContactRecordingAndAnalyticsBehavior` → 5. **`ConnectParticipantWithAgenticCX`** → Default `DisconnectParticipant`; Escalation `UpdateContactTargetQueue` + `TransferContactToQueue`; Error `MessageParticipant` (apology) → disconnect; idle timeout → disconnect.
