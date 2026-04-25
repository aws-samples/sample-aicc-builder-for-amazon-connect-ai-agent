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
| Create Callback | CreateCallbackContact | SetCallbackNumber + TransferContactToQueue |
| Check Metrics | CheckMetricData | GetQueueMetrics + Compare |

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
| Queue | SetWorkingQueue |
| Transfer | TransferContactToQueue |
| Staffing | CheckStaffing |
| Hours | CheckHoursOfOperation |
| Metrics | GetQueueMetrics |
| Lambda | InvokeLambdaFunction |
| Profile | GetCustomerProfile |
| Associate | AssociateContactToCustomerProfile |
| Callback | SetCallbackNumber |
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
