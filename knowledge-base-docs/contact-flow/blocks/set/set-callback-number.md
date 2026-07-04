# UpdateContactCallbackNumber Block

## Question
How do I use the UpdateContactCallbackNumber block for queue callbacks in Amazon Connect Contact Flow?

## Answer
The UpdateContactCallbackNumber block sets the phone number to use when creating a callback contact. This is used with TransferContactToQueue to implement queue callback functionality.

### JSON Structure
```json
{
  "Identifier": "set-callback",
  "Type": "UpdateContactCallbackNumber",
  "Parameters": {
    "CallbackNumber": "$.CustomerEndpoint.Address"
  },
  "Transitions": {
    "NextAction": "transfer-callback",
    "Errors": [
      {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-number-handler"},
      {"ErrorType": "CallbackNumberNotDialable", "NextAction": "not-dialable-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| CallbackNumber | String (JSONPath) | The phone number for callback. Must be a JSONPath / contact-attribute reference (e.g. `$.CustomerEndpoint.Address`), NOT a hard-coded literal number. Required. |

### Error Types
UpdateContactCallbackNumber requires exactly these two error branches (and rejects any others, including `NoMatchingError`):
- **InvalidCallbackNumber**: Phone number format is invalid
- **CallbackNumberNotDialable**: Valid format but cannot be dialed (blocked, out of region, etc.)

### CRITICAL Requirements
1. MUST handle exactly the two error types `InvalidCallbackNumber` and `CallbackNumberNotDialable`. Do NOT add a `NoMatchingError` branch — the API rejects it on this block.
2. MUST be followed by `TransferContactToQueue` to create the callback (the target queue must already be set on the contact — e.g. via `UpdateContactTargetQueue`)
3. `CallbackNumber` must be a JSONPath reference (typically `$.CustomerEndpoint.Address`, the caller's number); a literal number string is rejected

### WRONG vs CORRECT

#### WRONG (NoMatchingError is forbidden, and both required errors are missing!)
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
The API rejects `NoMatchingError` on this block and reports `InvalidCallbackNumber` and `CallbackNumberNotDialable` as missing.

#### CORRECT (exactly the two required error types, nothing else)
```json
{
  "Transitions": {
    "NextAction": "transfer-callback",
    "Errors": [
      {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-number-handler"},
      {"ErrorType": "CallbackNumberNotDialable", "NextAction": "not-dialable-handler"}
    ]
  }
}
```

### Complete Callback Pattern
Set the target queue first (TransferContactToQueue transfers to the queue already set on the contact and takes no `QueueId` parameter).
```json
{"Identifier": "callback-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "We'll call you back when an agent is available."},
 "Transitions": {"NextAction": "set-queue",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "set-queue"}]}}

{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "set-callback",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "set-callback", "Type": "UpdateContactCallbackNumber",
 "Parameters": {"CallbackNumber": "$.CustomerEndpoint.Address"},
 "Transitions": {"NextAction": "transfer-callback",
   "Errors": [
     {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-callback"},
     {"ErrorType": "CallbackNumberNotDialable", "NextAction": "invalid-callback"}
   ]}}

{"Identifier": "transfer-callback", "Type": "TransferContactToQueue",
 "Parameters": {},
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

### Recommended Pattern vs. CreateCallbackContact
`CreateCallbackContact` IS a valid block type. If you use it, it requires three properties — `InitialCallDelaySeconds`, `MaximumConnectionAttempts`, and `RetryDelaySeconds`:
```json
{"Type": "CreateCallbackContact",
 "Parameters": {
   "InitialCallDelaySeconds": "10",
   "MaximumConnectionAttempts": "3",
   "RetryDelaySeconds": "60"
 }}
```

For queue callbacks, the recommended pattern in most flows is to set the callback number and transfer to the (already-set) queue:
```json
{"Type": "UpdateContactCallbackNumber"}
// followed by
{"Type": "TransferContactToQueue"}
```

## Related Topics
- TransferContactToQueue
- UpdateContactTargetQueue
- CreateCallbackContact
- Queue Overflow Handling Pattern

---
**Metadata**
- Category: Set
- BlockType: UpdateContactCallbackNumber
- Keywords: callback, queue callback, InvalidCallbackNumber, CallbackNumberNotDialable, CreateCallbackContact, phone number
