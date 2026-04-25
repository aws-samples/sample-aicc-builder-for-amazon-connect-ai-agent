# Loop Block

## Question
How do I use the Loop block for retry patterns in Amazon Connect Contact Flow?

## Answer
The Loop block repeats a section of the flow a specified number of times. It's used for retry patterns, re-prompting, and iterating through options.

### JSON Structure
```json
{
  "Identifier": "retry-loop",
  "Type": "Loop",
  "Parameters": {
    "LoopCount": "3"
  },
  "Transitions": {
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["Looping"]}, "NextAction": "action-to-retry"},
      {"Condition": {"Operator": "Equals", "Operands": ["Complete"]}, "NextAction": "max-retries-reached"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| LoopCount | String | Number of iterations (e.g., "3") |

### Condition Values
- **Looping**: Loop is still iterating (hasn't reached LoopCount)
- **Complete**: Loop has completed all iterations

### Error Types
- **NoMatchingError**: General error

### CRITICAL Requirements
1. MUST have `Conditions` for both "Looping" and "Complete"
2. `LoopCount` must be a string ("3"), not a number
3. MUST have `Errors` with `NoMatchingError`
4. The "Looping" path should eventually return to the Loop block
5. The "Complete" path handles max retries exceeded

### Loop Counter Access
You can access the current loop index:
- `$.Loop.LoopCounter` - Current iteration (0-indexed)

### WRONG vs CORRECT

#### WRONG (Missing Complete condition)
```json
{
  "Transitions": {
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["Looping"]}, "NextAction": "retry-action"}
    ]
  }
}
```

#### CORRECT
```json
{
  "Transitions": {
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["Looping"]}, "NextAction": "retry-action"},
      {"Condition": {"Operator": "Equals", "Operands": ["Complete"]}, "NextAction": "max-retries"}
    ],
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
  }
}
```

### Pattern: Input Retry Loop
```json
{"Identifier": "input-loop", "Type": "Loop",
 "Parameters": {"LoopCount": "3"},
 "Transitions": {
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["Looping"]}, "NextAction": "get-input"},
     {"Condition": {"Operator": "Equals", "Operands": ["Complete"]}, "NextAction": "max-attempts"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}

{"Identifier": "get-input", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Please enter your PIN number.",
   "InputTimeLimitSeconds": "10",
   "DTMFConfiguration": {"InputTerminationSequence": "#"}
 },
 "Transitions": {"NextAction": "validate-pin",
   "Errors": [
     {"ErrorType": "InputTimeLimitExceeded", "NextAction": "input-loop"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "validate-pin"},
     {"ErrorType": "NoMatchingError", "NextAction": "input-loop"}
   ]
 }}

{"Identifier": "validate-pin", "Type": "InvokeLambdaFunction",
 "Parameters": {
   "LambdaFunctionARN": "{{VALIDATE_PIN_LAMBDA}}",
   "InvocationTimeLimitSeconds": "8",
   "ResponseValidation": {"ResponseType": "STRING_MAP"},
   "LambdaInvocationAttributes": {"pin": "$.StoredCustomerInput"}
 },
 "Transitions": {"NextAction": "check-pin-result",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "input-loop"}]
 }}

{"Identifier": "check-pin-result", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.External.valid"},
 "Transitions": {"NextAction": "invalid-pin",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["true"]}, "NextAction": "pin-accepted"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "invalid-pin"}]
 }}

{"Identifier": "invalid-pin", "Type": "MessageParticipant",
 "Parameters": {"Text": "That PIN is invalid. Please try again."},
 "Transitions": {"NextAction": "input-loop",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "input-loop"}]}}

{"Identifier": "max-attempts", "Type": "MessageParticipant",
 "Parameters": {"Text": "You have exceeded the maximum number of attempts. Goodbye."},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}
```

### Pattern: Agent Availability Retry with Wait
```json
{"Identifier": "retry-loop", "Type": "Loop",
 "Parameters": {"LoopCount": "5"},
 "Transitions": {
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["Looping"]}, "NextAction": "wait-30s"},
     {"Condition": {"Operator": "Equals", "Operands": ["Complete"]}, "NextAction": "offer-callback"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}

{"Identifier": "wait-30s", "Type": "Wait",
 "Parameters": {"WaitTime": "30"},
 "Transitions": {"NextAction": "check-staffing",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "check-staffing"}]}}

{"Identifier": "check-staffing", "Type": "CheckStaffing",
 "Parameters": {},
 "Transitions": {
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "transfer-queue"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "retry-loop"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "retry-loop"}]
 }}
```

## Related Topics
- Wait Block
- GetParticipantInput
- CheckStaffing

---
**Metadata**
- Category: Control
- BlockType: Loop
- Keywords: loop, retry, repeat, iteration, Looping, Complete, LoopCount
