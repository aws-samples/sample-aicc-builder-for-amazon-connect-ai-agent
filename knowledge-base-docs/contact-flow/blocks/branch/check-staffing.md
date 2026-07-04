# Check Staffing / Queue Metrics (CheckMetricData)

> ⛔ **API-verified correction (2026-06-08):** there is **NO `CheckStaffing`
> Type** — it fails import with "Invalid Action type". The real block is
> **`CheckMetricData`** (console "Check staffing" / "Get metrics").

## Question
How do I check agent availability / queue metrics in Amazon Connect Contact Flow?

## Answer
Use **`CheckMetricData`**: it reads a real-time numeric metric (`MetricType`) for
the working queue and branches via `Conditions` using **numeric** operators
(`NumberGreaterThan`, `NumberGreaterOrEqualTo`, etc.). Use before
`TransferContactToQueue` to handle the no-agents case — check
`NumberOfAgentsAvailable > 0`.

### JSON Structure (API-verified)
```json
{
  "Identifier": "check-staffing",
  "Type": "CheckMetricData",
  "Parameters": {
    "MetricType": "NumberOfAgentsAvailable"
  },
  "Transitions": {
    "NextAction": "no-agents",
    "Conditions": [
      {"Condition": {"Operator": "NumberGreaterThan", "Operands": ["0"]}, "NextAction": "agents-available"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingCondition", "NextAction": "no-agents"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

Real `MetricType` values (API-verified 2026-07-03, case-sensitive — exactly these five):
`NumberOfAgentsAvailable`, `NumberOfContactsInQueue`, `OldestContactInQueueAgeSeconds`,
`NumberOfAgentsStaffed`, `NumberOfAgentsOnline`. The shorthand `AgentsAvailable` /
`ContactsInQueue` are REJECTED. Required errors: `NoMatchingCondition` + `NoMatchingError`.

### Required Parameters
`MetricType` — one of `NumberOfAgentsAvailable`, `NumberOfContactsInQueue`,
`OldestContactInQueueAgeSeconds`, `NumberOfAgentsStaffed`, `NumberOfAgentsOnline`.
(Omitting it fails import with "Action is missing
required property. Path: Actions[N].Parameters.MetricType".) The metric is read
for the working queue set by `UpdateContactTargetQueue`.

### Condition Values
`CheckMetricData` returns a **numeric** metric value, not a True/False binary.
Branch on it with numeric operators, e.g. for `NumberOfAgentsAvailable`:
- **`NumberGreaterThan` `["0"]`**: At least one agent is available in the queue
- **`NoMatchingCondition`**: No condition matched (e.g. zero agents available)

### Error Types
- **NoMatchingCondition**: No `Conditions` entry matched the returned metric value
- **NoMatchingError**: Unable to read the metric (no working queue set, permissions issue)

Both `NoMatchingCondition` and `NoMatchingError` are **required** — omitting either
fails import ("missing required error"). `QueueAtCapacity` is **not** a valid error
for this block.

### CRITICAL Requirements
1. MUST call `UpdateContactTargetQueue` before using CheckMetricData
2. MUST supply a valid `MetricType` parameter
3. MUST have an `Errors` array with **both** `NoMatchingCondition` and `NoMatchingError`
4. `Conditions` use numeric operators against the metric value (operands are strings, e.g. `"0"`)

### WRONG vs CORRECT

#### WRONG (invalid metric name, no numeric condition, missing required errors)
```json
{
  "Type": "CheckMetricData",
  "Parameters": {"MetricType": "AgentsAvailable"},
  "Transitions": {
    "NextAction": "transfer-queue",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": [true]}, "NextAction": "agents-available"}
    ]
  }
}
```

#### CORRECT
```json
{
  "Type": "CheckMetricData",
  "Parameters": {"MetricType": "NumberOfAgentsAvailable"},
  "Transitions": {
    "NextAction": "no-agents",
    "Conditions": [
      {"Condition": {"Operator": "NumberGreaterThan", "Operands": ["0"]}, "NextAction": "agents-available"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingCondition", "NextAction": "no-agents"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Complete Pattern: Staffing Check with Fallback
```json
{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "check-staffing",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-staffing", "Type": "CheckMetricData",
 "Parameters": {"MetricType": "NumberOfAgentsAvailable"},
 "Transitions": {
   "NextAction": "no-agents-message",
   "Conditions": [
     {"Condition": {"Operator": "NumberGreaterThan", "Operands": ["0"]}, "NextAction": "transfer-queue"}
   ],
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "no-agents-message"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]
 }}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [
     {"ErrorType": "QueueAtCapacity", "NextAction": "no-agents-message"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}

{"Identifier": "no-agents-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "All our agents are currently busy. Would you like us to call you back?"},
 "Transitions": {"NextAction": "offer-callback",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]}}
```

### CheckMetricData vs GetQueueMetrics
| Feature | CheckMetricData | GetQueueMetrics |
|---------|-----------------|-----------------|
| Purpose | Read one real-time metric and branch on it | Detailed metrics |
| Speed | Faster | Slower |
| Output | Single numeric metric value | Multiple metric values |
| Use Case | Simple routing (e.g. agents available > 0) | Complex routing decisions |

### When to Use
- **CheckMetricData**: Simple "are agents available?" / "queue too deep?" check on one metric
- **GetQueueMetrics**: Need queue size, wait times, or other details together

## Related Topics
- UpdateContactTargetQueue
- TransferContactToQueue
- GetQueueMetrics
- UpdateContactCallbackNumber

---
**Metadata**
- Category: Branch
- BlockType: CheckMetricData
- Keywords: staffing, agents, availability, queue, metric, NumberOfAgentsAvailable, NumberOfContactsInQueue, OldestContactInQueueAgeSeconds
