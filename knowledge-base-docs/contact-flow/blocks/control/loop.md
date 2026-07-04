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
    "NextAction": "action-to-retry",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "action-to-retry"},
      {"Condition": {"Operator": "Equals", "Operands": ["DoneLooping"]}, "NextAction": "max-retries-reached"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "max-retries-reached"}
    ]
  }
}
```

> ⚠️ **API-verified (2026-06-08):** a Loop block REQUIRES a top-level
> `Transitions.NextAction` in addition to its `Conditions`. Omitting it fails
> import with "Action is missing required property. Path:
> Actions[N].Transitions.NextAction". Point `NextAction` at the first action of
> the loop body (same target as the `ContinueLooping` condition).

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| LoopCount | String or Number | Number of iterations (e.g., "3"). Import accepts both the string `"3"` and the raw number `3`; the string form is recommended for consistency with the Connect console export. |

> ✅ **API-verified (2026-06-08):** operands are `ContinueLooping` / `DoneLooping`.
> The names `Looping`/`Complete` are WRONG and make the flow fail import.

### Condition Values
- **ContinueLooping**: Loop is still iterating (hasn't reached LoopCount)
- **DoneLooping**: Loop has completed all iterations

### Error Types
- **NoMatchingError**: General error (optional — see below)

### CRITICAL Requirements
1. MUST have a top-level `Transitions.NextAction` — a Loop block fails import without it
2. MUST have `Conditions` for both "ContinueLooping" and "DoneLooping"
3. The "ContinueLooping" path should eventually return to the Loop block
4. The "DoneLooping" path handles max retries exceeded

**Optional (recommended, not required):**
- An `Errors` array with `NoMatchingError` is **optional** for the Loop block — a Loop with a valid `NextAction` + `Conditions` and no `Errors` imports successfully. Including it is a good practice for defensive routing but is not enforced by import validation.
- `LoopCount` may be either the string `"3"` or the number `3`; both import. Prefer the string form for consistency with the console export.

### Loop Counter Access
You can reference the current loop index in an attribute string:
- `$.Loop.LoopCounter` - Current iteration

> Note: import validation accepts the `$.Loop.LoopCounter` reference syntax, but
> the exact runtime value (including whether it is 0-indexed) was not exercised
> at runtime and remains unconfirmed.

### WRONG vs CORRECT

#### WRONG (missing top-level NextAction and the DoneLooping condition)
```json
{
  "Transitions": {
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "retry-action"}
    ]
  }
}
```
> This form fails import with "Action is missing required property. Path:
> Actions[N].Transitions.NextAction" — a Loop needs a top-level `NextAction`,
> and it should also cover the `DoneLooping` condition.

#### CORRECT
```json
{
  "Transitions": {
    "NextAction": "retry-action",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "retry-action"},
      {"Condition": {"Operator": "Equals", "Operands": ["DoneLooping"]}, "NextAction": "max-retries"}
    ],
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "max-retries"}]
  }
}
```

### Pattern: Input Retry Loop
The `get-input` block below uses the verified `GetParticipantInput` **STORE**
mode (`StoreInput: "True"` with `InputValidation`, capturing the PIN to
`$.StoredCustomerInput`, only a `NoMatchingError` transition and no Conditions).
See the GetParticipantInput doc for the full MENU vs STORE schema before adapting
this — the two modes have different required properties.
```json
{"Identifier": "input-loop", "Type": "Loop",
 "Parameters": {"LoopCount": "3"},
 "Transitions": {
   "NextAction": "get-input",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "get-input"},
     {"Condition": {"Operator": "Equals", "Operands": ["DoneLooping"]}, "NextAction": "max-attempts"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "max-attempts"}]
 }}

{"Identifier": "get-input", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Please enter your PIN number.",
   "StoreInput": "True",
   "InputTimeLimitSeconds": "10",
   "DTMFConfiguration": {"DisableCancelKey": "False"},
   "InputValidation": {"CustomValidation": {"MaximumLength": "6"}}
 },
 "Transitions": {"NextAction": "validate-pin",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "input-loop"}]
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
The `wait-30s` block uses the verified `Wait` schema (`TimeLimitSeconds` plus a
`WaitCompleted` condition — **not** `WaitTime`), and staffing is checked with
`CheckMetricData` (there is no `CheckStaffing` Type). `CheckMetricData` returns a
numeric metric, so it branches with numeric operators against
`NumberOfAgentsAvailable`; set the working queue with `UpdateContactTargetQueue`
before this block.
```json
{"Identifier": "retry-loop", "Type": "Loop",
 "Parameters": {"LoopCount": "5"},
 "Transitions": {
   "NextAction": "wait-30s",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["ContinueLooping"]}, "NextAction": "wait-30s"},
     {"Condition": {"Operator": "Equals", "Operands": ["DoneLooping"]}, "NextAction": "offer-callback"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]
 }}

{"Identifier": "wait-30s", "Type": "Wait",
 "Parameters": {"TimeLimitSeconds": "30"},
 "Transitions": {"NextAction": "check-staffing",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["WaitCompleted"]}, "NextAction": "check-staffing"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "check-staffing"}]}}

{"Identifier": "check-staffing", "Type": "CheckMetricData",
 "Parameters": {"MetricType": "NumberOfAgentsAvailable"},
 "Transitions": {
   "NextAction": "retry-loop",
   "Conditions": [
     {"Condition": {"Operator": "NumberGreaterThan", "Operands": ["0"]}, "NextAction": "transfer-queue"}
   ],
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "retry-loop"},
     {"ErrorType": "NoMatchingError", "NextAction": "retry-loop"}
   ]
 }}
```

## Related Topics
- Wait Block
- GetParticipantInput
- CheckMetricData

---
**Metadata**
- Category: Control
- BlockType: Loop
- Keywords: loop, retry, repeat, iteration, ContinueLooping, DoneLooping, LoopCount
