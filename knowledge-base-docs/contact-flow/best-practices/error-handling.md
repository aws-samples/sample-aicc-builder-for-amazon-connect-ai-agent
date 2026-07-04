# Contact Flow Error Handling Best Practices

## Question
What are the best practices for error handling in Amazon Connect Contact Flows?

## Answer
Proper error handling ensures customers aren't left stranded when issues occur. Every block should have appropriate error transitions, and every flow path should lead to a terminal block.

## Required Error Types by Block

### Blocks with Single Error Type (NoMatchingError)
| Block Type | Required Errors |
|------------|-----------------|
| MessageParticipant | NoMatchingError (optional but recommended) |
| UpdateContactAttributes | NoMatchingError |
| UpdateContactTextToSpeechVoice | (NoMatchingError optional — imports with or without it) |
| UpdateContactRecordingBehavior | (Success only — NoMatchingError forbidden; requires a `RecordingBehavior` object parameter) |
| UpdateFlowLoggingBehavior | (Success only — NoMatchingError forbidden) |
| UpdateContactTargetQueue | NoMatchingError |
| InvokeLambdaFunction | NoMatchingError |
| Wait | NoMatchingError (also requires a `Transitions.Conditions` branch and a `NextAction` — see below) |

### Blocks with Multiple Error Types
| Block Type | Required Errors |
|------------|-----------------|
| TransferContactToQueue | QueueAtCapacity, NoMatchingError |
| Compare | NoMatchingCondition |
| CheckHoursOfOperation | NoMatchingError (+ True/False conditions) |
| GetParticipantInput | InputTimeLimitExceeded, NoMatchingCondition, NoMatchingError (all three required; also needs `InputTimeLimitSeconds` and, in a MENU/`StoreInput: False` pattern, no `DTMFConfiguration`) |
| UpdateContactCallbackNumber | InvalidCallbackNumber, CallbackNumberNotDialable |
| GetCustomerProfile | MultipleFoundError, NoneFoundError, NoMatchingError |

### Terminal Blocks (No Errors)
| Block Type | Notes |
|------------|-------|
| DisconnectParticipant | Parameters: {}, Transitions: {} — the terminal block for standard Contact Flows |
| EndFlowModuleExecution | Parameters: {}, Transitions: {} — **module-only**: valid only inside a Contact Flow Module (flow type `contactFlowModule`), rejected in a standard Contact Flow |

## Error Handling Patterns

### Pattern 1: Centralized Error Handler
Route all errors to a single handler:

```json
{
  "Identifier": "error-handler",
  "Type": "MessageParticipant",
  "Parameters": {
    "Text": "We're experiencing technical difficulties. Please try again later."
  },
  "Transitions": {
    "NextAction": "disconnect",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]
  }
}
```

### Pattern 2: Graceful Degradation
Handle errors contextually to continue the flow:

```json
{
  "Identifier": "get-profile",
  "Type": "GetCustomerProfile",
  "Parameters": {
    "ProfileRequestData": {"IdentifierName": "_phone", "IdentifierValue": "$.CustomerEndpoint.Address"}
  },
  "Transitions": {
    "NextAction": "use-profile",
    "Errors": [
      {"ErrorType": "MultipleFoundError", "NextAction": "continue-without-profile"},
      {"ErrorType": "NoneFoundError", "NextAction": "continue-without-profile"},
      {"ErrorType": "NoMatchingError", "NextAction": "continue-without-profile"}
    ]
  }
}
```

### Pattern 3: Retry with Loop
Use Loop block for retryable errors:

The Loop block requires a `NextAction` in its `Transitions` in addition to the `ContinueLooping` / `DoneLooping` conditions:

```json
{
  "Identifier": "retry-loop",
  "Type": "Loop",
  "Parameters": {"LoopCount": "3"},
  "Transitions": {
    "NextAction": "max-retries",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "retry-action"},
      {"Condition": {"Operator": "Equals", "Operands": ["DoneLooping"]}, "NextAction": "max-retries"}
    ]
  }
}
```

## Common Mistakes to Avoid

### Mistake 1: Missing Error Arrays
WRONG:
```json
{
  "Type": "Compare",
  "Transitions": {
    "NextAction": "default",
    "Conditions": [...]
  }
}
```

CORRECT:
```json
{
  "Type": "Compare",
  "Transitions": {
    "NextAction": "default",
    "Conditions": [...],
    "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "default"}]
  }
}
```

### Mistake 2: Missing Required Error Types
WRONG (UpdateContactCallbackNumber — `NoMatchingError` is not a valid error type on this block, and both required errors are missing):
```json
{
  "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error"}]
}
```

CORRECT:
```json
{
  "Errors": [
    {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid"},
    {"ErrorType": "CallbackNumberNotDialable", "NextAction": "invalid"}
  ]
}
```

### Mistake 3: Orphaned Error Paths
All error handlers must eventually reach a terminal block (DisconnectParticipant or Transfer).

### Mistake 4: Infinite Loops
Ensure error paths don't create loops without exit conditions:

WRONG:
```json
{
  "Identifier": "action-a",
  "Transitions": {
    "NextAction": "action-b",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "action-b"}]
  }
}
{
  "Identifier": "action-b",
  "Transitions": {
    "NextAction": "action-a",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "action-a"}]
  }
}
```

## Error Message Best Practices

1. **Be apologetic but professional**
   - Good: "We're experiencing technical difficulties. Please try again later."
   - Bad: "Error occurred!"

2. **Provide next steps**
   - Good: "Please call back during business hours or visit our website."
   - Bad: "Goodbye."

3. **Don't expose technical details**
   - Good: "We couldn't process your request."
   - Bad: "Lambda function timeout error."

4. **Consider alternatives**
   - Offer callback when queue is full
   - Offer self-service when agents unavailable
   - Provide website/email alternatives

## Validation Checklist

Before deploying a Contact Flow, verify:
- [ ] Every block has required error types
- [ ] All error paths lead to terminal blocks
- [ ] No orphaned blocks exist
- [ ] No infinite loops possible
- [ ] Error messages are customer-friendly
- [ ] Graceful degradation implemented where possible

## Related Topics
- DisconnectParticipant
- Loop Block
- GetParticipantInput
- UpdateContactCallbackNumber

---
**Metadata**
- Category: Best Practices
- Keywords: error handling, NoMatchingError, NoMatchingCondition, InvalidCallbackNumber, CallbackNumberNotDialable, best practices
