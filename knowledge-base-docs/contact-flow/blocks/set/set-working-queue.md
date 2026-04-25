# SetWorkingQueue Block

## Question
How do I use the SetWorkingQueue block in Amazon Connect Contact Flow?

## Answer
The SetWorkingQueue block sets the active queue for subsequent queue-related operations like TransferContactToQueue and CheckStaffing.

### JSON Structure
```json
{
  "Identifier": "set-queue",
  "Type": "SetWorkingQueue",
  "Parameters": {
    "QueueId": "{{QUEUE_ARN}}"
  },
  "Transitions": {
    "NextAction": "check-staffing",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| QueueId | String | The ARN or UUID of the target queue (NOT the queue name!) |

### Error Types
- **NoMatchingError**: Queue doesn't exist or permissions issue

### CRITICAL Requirements
1. QueueId MUST be a valid ARN or UUID format, NOT a queue name
2. MUST be called BEFORE `TransferContactToQueue` or `CheckStaffing` if those blocks don't specify QueueId
3. MUST have `Errors` array with `NoMatchingError`

### Valid QueueId Formats
```
# ARN format (recommended)
arn:aws:connect:us-east-1:123456789012:instance/xxx/queue/yyy

# UUID format
12345678-1234-1234-1234-123456789012

# Placeholder for generated flows
{{QUEUE_ARN}}
```

### Invalid QueueId Formats (NEVER USE)
```
# Queue name - WRONG!
"Customer Service"
"Sales Queue"
```

### Common Pattern: Queue with Staffing Check
```json
{"Identifier": "set-queue", "Type": "SetWorkingQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "check-staffing", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-staffing", "Type": "CheckStaffing",
 "Parameters": {},
 "Transitions": {
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "transfer-queue"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "no-agents"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

## Related Topics
- TransferContactToQueue
- CheckStaffing
- GetQueueMetrics

---
**Metadata**
- Category: Set
- BlockType: SetWorkingQueue
- Keywords: queue, working queue, routing, set queue
