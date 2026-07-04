# UpdateContactTargetQueue Block

## Question
How do I use the UpdateContactTargetQueue block in Amazon Connect Contact Flow?

## Answer
The UpdateContactTargetQueue block sets the active queue for subsequent queue-related operations like TransferContactToQueue and CheckMetricData.

### JSON Structure
```json
{
  "Identifier": "set-queue",
  "Type": "UpdateContactTargetQueue",
  "Parameters": {
    "QueueId": "arn:aws:connect:us-east-1:123456789012:instance/xxx/queue/yyy"
  },
  "Transitions": {
    "NextAction": "check-metric",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
UpdateContactTargetQueue requires **at least one** of the following properties (either sets the target queue for subsequent blocks):

| Parameter | Type | Description |
|-----------|------|-------------|
| QueueId | String | The ARN or UUID of the target queue (NOT the queue name!) |
| AgentId | String | The ARN of an agent — targets that specific agent's personal queue instead of a standard queue |

If neither is set, the API rejects the block with: `At least one of the following properties must be set. Properties: [Parameters.QueueId, Parameters.AgentId]`.

### Error Types
- **NoMatchingError**: Queue doesn't exist or permissions issue

### CRITICAL Requirements
1. QueueId MUST be a valid ARN or UUID format, NOT a queue name
2. Either `QueueId` OR `AgentId` MUST be set (at least one is required)
3. MUST be called BEFORE `TransferContactToQueue` or `CheckMetricData` if those blocks don't specify a queue
4. MUST have `Errors` array with `NoMatchingError`

### Valid QueueId Formats
```
# ARN format (recommended)
arn:aws:connect:us-east-1:123456789012:instance/xxx/queue/yyy

# UUID format
12345678-1234-1234-1234-123456789012
```

> **Generation-time placeholder note:** `{{QUEUE_ARN}}` is a templating token used by generated flows — it is NOT a value the CreateContactFlow API accepts. Importing a flow with the literal string `{{QUEUE_ARN}}` fails with `Failed to convert id: {{QUEUE_ARN}}`. It MUST be substituted with a real queue ARN or UUID before the flow is imported.

### Invalid QueueId Formats (NEVER USE)
```
# Queue name - WRONG! ("Failed to convert id: Customer Service")
"Customer Service"
"Sales Queue"
```

### Common Pattern: Queue with Agent-Availability Check

There is no `CheckStaffing` block — the API rejects that Type (`Invalid Action type. Type: CheckStaffing`). Use **`CheckMetricData`** instead, which reads a real-time queue metric and branches with a numeric `Conditions` comparison. `CheckMetricData` requires:
- `Parameters.MetricType` — one of `NumberOfAgentsAvailable`, `NumberOfContactsInQueue`, `OldestContactInQueueAgeSeconds`
- `Transitions.NextAction` (the default/no-match branch), a `Transitions.Conditions` array, and BOTH the `NoMatchingCondition` and `NoMatchingError` error handlers

Numeric conditions use operators such as `NumberGreaterThan`, `NumberLessThan`, and `NumberEquals` (note: bare `True`/`False` conditions are NOT valid for this block).

```json
{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "arn:aws:connect:us-east-1:123456789012:instance/xxx/queue/yyy"},
 "Transitions": {"NextAction": "check-metric", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-metric", "Type": "CheckMetricData",
 "Parameters": {"MetricType": "NumberOfAgentsAvailable"},
 "Transitions": {
   "NextAction": "no-agents",
   "Conditions": [
     {"Condition": {"Operator": "NumberGreaterThan", "Operands": ["0"]}, "NextAction": "transfer-queue"}
   ],
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "no-agents"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]
 }}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
              {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

> **TransferContactToQueue error handling:** this block requires BOTH the `QueueAtCapacity` AND `NoMatchingError` error handlers — dropping either one fails the import (`Action is missing required error. Error: QueueAtCapacity` / `Error: NoMatchingError`). It takes NO `QueueId` parameter; it transfers to the queue set by the preceding UpdateContactTargetQueue block.

## Related Topics
- TransferContactToQueue
- CheckMetricData
- GetQueueMetrics

---
**Metadata**
- Category: Set
- BlockType: UpdateContactTargetQueue
- Keywords: queue, working queue, routing, set queue
