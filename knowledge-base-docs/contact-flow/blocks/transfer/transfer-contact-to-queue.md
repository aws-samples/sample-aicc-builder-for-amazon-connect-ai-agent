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
  "Parameters": {
    "QueueId": "{{QUEUE_ARN}}"
  },
  "Transitions": {
    "NextAction": "disconnect",
    "Errors": [
      {"ErrorType": "QueueAtCapacity", "NextAction": "queue-full-handler"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| QueueId | String | The ARN or ID of the target queue (optional if SetWorkingQueue was called first) |

### Error Types
- **QueueAtCapacity**: The queue has reached its maximum contact limit
- **NoMatchingError**: General error (e.g., queue doesn't exist, permissions issue)

### CRITICAL Requirements
1. MUST have `NextAction` in Transitions - this is where the flow continues after successful transfer
2. MUST handle `QueueAtCapacity` error for production flows
3. MUST handle `NoMatchingError` for robustness
4. If `QueueId` is not specified, you MUST call `SetWorkingQueue` before this block
5. The `NextAction` typically points to a `DisconnectParticipant` block

### Common Patterns

#### With SetWorkingQueue (Recommended)
```json
{"Identifier": "set-queue", "Type": "SetWorkingQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "transfer-queue", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

#### Direct Queue Specification
```json
{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {"QueueId": "arn:aws:connect:us-east-1:123456789012:instance/xxx/queue/yyy"},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

## Related Topics
- SetWorkingQueue
- CheckStaffing
- GetQueueMetrics
- SetCallbackNumber (for callback when queue is full)

---
**Metadata**
- Category: Transfer
- BlockType: TransferContactToQueue
- Keywords: queue, transfer, agent, routing, QueueAtCapacity
