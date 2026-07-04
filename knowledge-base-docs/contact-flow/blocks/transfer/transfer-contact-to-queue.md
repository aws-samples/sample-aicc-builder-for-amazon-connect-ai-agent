# TransferContactToQueue Block

## Question
How do I use the TransferContactToQueue block in Amazon Connect Contact Flow?

## Answer
The TransferContactToQueue block transfers the contact to a queue for agent handling. This is one of the most commonly used blocks for routing customers to live agents.

### JSON Structure
```json
{
  "Identifier": "transfer-queue",
  "Type": "TransferContactToQueue",
  "Parameters": {},
  "Transitions": {
    "NextAction": "disconnect",
    "Errors": [
      {"ErrorType": "QueueAtCapacity", "NextAction": "queue-full-handler"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Parameters
TransferContactToQueue accepts **no parameters** — its `Parameters` object must be empty (`{}`). Supplying a `QueueId` (or any other property) fails import with `Invalid Action property name. Path: Actions[0].Parameters.QueueId`.

The target queue is **not** set on this block. You MUST set it beforehand with an `UpdateContactTargetQueue` block (see Common Patterns below).

### Error Types
Both of the following error types are **required** on this block (see CRITICAL Requirements). No other error types are permitted — adding a third type such as `QueueDoesNotExist` fails import with `Invalid Action error. Error: QueueDoesNotExist`.
- **QueueAtCapacity**: The queue has reached its maximum contact limit
- **NoMatchingError**: General error (e.g., queue doesn't exist, permissions issue)

### CRITICAL Requirements
1. MUST have `NextAction` in Transitions - this is where the flow continues after successful transfer. Omitting it fails import with `Action is missing required property. Path: Actions[0].Transitions.NextAction`.
2. MUST handle the `QueueAtCapacity` error — this is enforced at import, not just a best practice. Without it, import fails with `Action is missing required error. Error: QueueAtCapacity, Path: Actions[0]`.
3. MUST handle the `NoMatchingError` error — also enforced at import. Without it, import fails with `Action is missing required error. Error: NoMatchingError, Path: Actions[0]`.
4. This block takes NO parameters. The target queue MUST be set by a preceding `UpdateContactTargetQueue` block; there is no `QueueId` (or other) parameter form.
5. The `NextAction` typically points to a `DisconnectParticipant` block

### Common Patterns

#### Set Queue with UpdateContactTargetQueue, then Transfer (the only valid form)
`UpdateContactTargetQueue` sets the queue; `TransferContactToQueue` then transfers to it with empty `Parameters`. There is no direct-`QueueId` form on the transfer block — this is the only pattern that imports.
```json
{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "transfer-queue", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

## Related Topics
- UpdateContactTargetQueue
- CheckStaffing
- GetQueueMetrics
- UpdateContactCallbackNumber (for callback when queue is full)

---
**Metadata**
- Category: Transfer
- BlockType: TransferContactToQueue
- Keywords: queue, transfer, agent, routing, QueueAtCapacity
