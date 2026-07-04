# CheckHoursOfOperation Block

## Question
How do I use the CheckHoursOfOperation block in Amazon Connect Contact Flow?

## Answer
The CheckHoursOfOperation block checks if the current time is within defined business hours. Use this for after-hours routing and holiday handling.

### JSON Structure
```json
{
  "Identifier": "check-hours",
  "Type": "CheckHoursOfOperation",
  "Parameters": {
    "HoursOfOperationId": "{{HOURS_ARN}}"
  },
  "Transitions": {
    "NextAction": "in-hours",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "in-hours"},
      {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "out-of-hours"}
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
| HoursOfOperationId | String | ARN of the Hours of Operation configuration |

### Condition Values
- **True**: Current time is within business hours (In Hours)
- **False**: Current time is outside business hours (Out of Hours)

### Error Types
- **NoMatchingError**: Unable to check hours (invalid ARN, permissions)

### CRITICAL Requirements
1. MUST have a top-level `NextAction` in `Transitions` (the default branch) — without it import fails with `Action is missing required property. Path: Actions[0].Transitions.NextAction`
2. MUST have `Conditions` for both "True" and "False"
3. MUST have `Errors` with `NoMatchingError` (`NoMatchingCondition` is explicitly rejected on this block)
4. Condition values are strings: `"True"` and `"False"` (not booleans)
5. HoursOfOperationId must be an ARN, not a name — a non-ARN value (e.g. `"Basic Hours"`) is rejected by the service with `Not a supported id format`

### Valid HoursOfOperationId Formats
```
# ARN format (required)
arn:aws:connect:us-east-1:123456789012:instance/xxx/operating-hours/yyy

# Placeholder for generated flows
{{HOURS_ARN}}
```

### Complete Pattern: Hours Check with Callback
```json
{"Identifier": "check-hours", "Type": "CheckHoursOfOperation",
 "Parameters": {"HoursOfOperationId": "{{HOURS_ARN}}"},
 "Transitions": {
   "NextAction": "in-hours-flow",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "in-hours-flow"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "closed-message"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}

{"Identifier": "closed-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "We are currently closed. Our business hours are Monday to Friday, 9 AM to 6 PM."},
 "Transitions": {"NextAction": "offer-callback",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]}}

{"Identifier": "offer-callback", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Press 1 to leave a message or press 2 to receive a callback when we open.",
   "StoreInput": "False",
   "InputTimeLimitSeconds": "5"
 },
 "Transitions": {"NextAction": "disconnect",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "voicemail"},
     {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "schedule-callback"}
   ],
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "disconnect"},
     {"ErrorType": "InputTimeLimitExceeded", "NextAction": "disconnect"},
     {"ErrorType": "NoMatchingError", "NextAction": "disconnect"}
   ]
 }}
```

### Without HoursOfOperationId (Using Queue's Hours)
If you omit the HoursOfOperationId, it uses the hours configured on the working queue:
```json
{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "check-queue-hours",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-queue-hours", "Type": "CheckHoursOfOperation",
 "Parameters": {},
 "Transitions": {
   "NextAction": "transfer",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "transfer"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "closed"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}
```

## Related Topics
- UpdateContactTargetQueue
- GetParticipantInput
- UpdateContactCallbackNumber

---
**Metadata**
- Category: Branch
- BlockType: CheckHoursOfOperation
- Keywords: hours, business hours, after hours, closed, True, False
