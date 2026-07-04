# DisconnectParticipant Block

## Question
How do I use the DisconnectParticipant block to end a call in Amazon Connect Contact Flow?

## Answer
The DisconnectParticipant block ends the contact (disconnects the call). This is a TERMINAL block that should be the final step in every flow path.

### JSON Structure
```json
{
  "Identifier": "disconnect",
  "Type": "DisconnectParticipant",
  "Parameters": {},
  "Transitions": {}
}
```

### Required Parameters
None - Parameters MUST be empty `{}`

### Transitions
None - Transitions MUST be empty `{}` (terminal block)

### CRITICAL Requirements
1. `Parameters` MUST be exactly `{}`
2. `Transitions` MUST be exactly `{}`
3. Every flow path MUST eventually reach either DisconnectParticipant or a Transfer block
4. There should typically be only ONE DisconnectParticipant block per flow (all paths lead to it)

### WRONG vs CORRECT

#### WRONG (Has transitions)
```json
{
  "Type": "DisconnectParticipant",
  "Parameters": {},
  "Transitions": {
    "NextAction": "some-block"
  }
}
```

#### WRONG (Has parameters)
```json
{
  "Type": "DisconnectParticipant",
  "Parameters": {
    "SomeParam": "value"
  },
  "Transitions": {}
}
```

#### CORRECT
```json
{
  "Type": "DisconnectParticipant",
  "Parameters": {},
  "Transitions": {}
}
```

### Common Pattern: All Paths Lead to Disconnect
```json
{"Identifier": "goodbye", "Type": "MessageParticipant",
 "Parameters": {"Text": "Thank you for calling. Goodbye!"},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}

{"Identifier": "error-handler", "Type": "MessageParticipant",
 "Parameters": {"Text": "We experienced an error. Please try again later."},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}

{"Identifier": "disconnect", "Type": "DisconnectParticipant",
 "Parameters": {},
 "Transitions": {}}
```

### Terminal Blocks in Amazon Connect
Only these two block types can be terminal (Parameters `{}` and Transitions `{}`) in a standard contact flow:
1. **DisconnectParticipant** - End the contact
2. **EndFlowExecution** - End / return from flow execution

> API-verified. `EndFlowExecution` is the correct end/return terminal type. `EndFlowModuleExecution` is module-only — importing it in a standard contact flow fails with `Action ... is not supported in contact flow type contactFlow`.

The `Transfer*` blocks are **NOT** terminal — each requires a `NextAction` transition plus specific error branches, so even a "successful transfer" path still routes onward (typically to `DisconnectParticipant`):

- **TransferContactToQueue** - requires `NextAction` plus `QueueAtCapacity` and `NoMatchingError` error branches (takes no `QueueId` parameter).
- **TransferToFlow** - requires `Parameters.ContactFlowId` (a real flow ARN), a `NextAction` transition, and a `NoMatchingError` error branch.
- **TransferParticipantToThirdParty** (this is the correct API type name — `TransferToPhoneNumber` is **not** a valid `Type`) - requires a `NextAction` transition plus error branches (`ConnectionTimeLimitExceeded`, `CallFailed`, `NoMatchingError`).

### Metadata for DisconnectParticipant
```json
{
  "Metadata": {
    "ActionMetadata": {
      "disconnect": {
        "position": {"x": 280, "y": 2000},
        "isFriendlyName": true
      }
    }
  }
}
```

## Related Topics
- MessageParticipant (for goodbye messages before disconnect)
- TransferContactToQueue (alternative endpoint — note: not terminal, still needs a NextAction and error branches)
- EndFlowExecution (the other true terminal block in a standard flow)

---
**Metadata**
- Category: Control
- BlockType: DisconnectParticipant
- Keywords: disconnect, end call, terminate, terminal, hang up
