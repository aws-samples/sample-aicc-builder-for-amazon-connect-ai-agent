# SetCallbackNumber Block

## Question
How do I use the SetCallbackNumber block for queue callbacks in Amazon Connect Contact Flow?

## Answer
The SetCallbackNumber block sets the phone number to use when creating a callback contact. This is used with TransferContactToQueue to implement queue callback functionality.

### JSON Structure
```json
{
  "Identifier": "set-callback",
  "Type": "SetCallbackNumber",
  "Parameters": {
    "CallbackNumber": "$.CustomerEndpoint.Address"
  },
  "Transitions": {
    "NextAction": "transfer-callback",
    "Errors": [
      {"ErrorType": "InvalidNumber", "NextAction": "invalid-number-handler"},
      {"ErrorType": "NotDialable", "NextAction": "not-dialable-handler"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| CallbackNumber | String | The phone number for callback (can use JSONPath) |

### Error Types
- **InvalidNumber**: Phone number format is invalid
- **NotDialable**: Valid format but cannot be dialed (blocked, out of region, etc.)
- **NoMatchingError**: General error

### CRITICAL Requirements
1. MUST have all three error types handled for production flows
2. MUST be followed by `TransferContactToQueue` to create the callback
3. `CallbackNumber` typically uses `$.CustomerEndpoint.Address` (caller's number)

### WRONG vs CORRECT

#### WRONG (Missing required errors!)
```json
{
  "Transitions": {
    "NextAction": "transfer-callback",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

#### CORRECT (All three error types)
```json
{
  "Transitions": {
    "NextAction": "transfer-callback",
    "Errors": [
      {"ErrorType": "InvalidNumber", "NextAction": "invalid-number-handler"},
      {"ErrorType": "NotDialable", "NextAction": "not-dialable-handler"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Complete Callback Pattern
```json
{"Identifier": "callback-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "We'll call you back when an agent is available."},
 "Transitions": {"NextAction": "set-callback",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "set-callback"}]}}

{"Identifier": "set-callback", "Type": "SetCallbackNumber",
 "Parameters": {"CallbackNumber": "$.CustomerEndpoint.Address"},
 "Transitions": {"NextAction": "transfer-callback",
   "Errors": [
     {"ErrorType": "InvalidNumber", "NextAction": "invalid-callback"},
     {"ErrorType": "NotDialable", "NextAction": "invalid-callback"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}

{"Identifier": "transfer-callback", "Type": "TransferContactToQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "callback-confirmed",
   "Errors": [
     {"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}

{"Identifier": "callback-confirmed", "Type": "MessageParticipant",
 "Parameters": {"Text": "Your callback has been scheduled. Goodbye!"},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}

{"Identifier": "invalid-callback", "Type": "MessageParticipant",
 "Parameters": {"Text": "Sorry, we cannot call back this number. Please call back during business hours."},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}
```

### NON-EXISTENT Block Warning
There is NO `CreateCallbackContact` block type. Callbacks are created by:
1. SetCallbackNumber (set the number)
2. TransferContactToQueue (creates the callback in queue)

#### WRONG
```json
{"Type": "CreateCallbackContact"}  // DOES NOT EXIST!
```

#### CORRECT
```json
{"Type": "SetCallbackNumber"}
// followed by
{"Type": "TransferContactToQueue"}
```

## Related Topics
- TransferContactToQueue
- SetWorkingQueue
- Queue Overflow Handling Pattern

---
**Metadata**
- Category: Set
- BlockType: SetCallbackNumber
- Keywords: callback, queue callback, InvalidNumber, NotDialable, phone number
