# Check Staffing / Queue Metrics (CheckMetricData)

> ⛔ **API-verified correction (2026-06-08):** there is **NO `CheckStaffing`
> Type** — it fails import with "Invalid Action type". The real block is
> **`CheckMetricData`** (console "Check staffing" / "Get metrics").

## Question
How do I check agent availability / queue metrics in Amazon Connect Contact Flow?

## Answer
Use **`CheckMetricData`**: it reads a real-time metric (`MetricType`) for the
working queue and branches via `Conditions`. Use before `TransferContactToQueue`
to handle the no-agents case.

### JSON Structure (API-verified)
```json
{
  "Identifier": "check-staffing",
  "Type": "CheckMetricData",
  "Parameters": {
    "MetricType": "AgentsAvailable"
  },
  "Transitions": {
    "NextAction": "agents-available",
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

Real `MetricType` values: `AgentsAvailable`, `OldestContactInQueueAgeSeconds`,
`ContactsInQueue`. Required errors: `NoMatchingCondition` + `NoMatchingError`.

### Required Parameters
None - uses the working queue set by UpdateContactTargetQueue

### Condition Values
- **True**: At least one agent is available in the queue
- **False**: No agents are available (all busy, offline, or not staffed)

### Error Types
- **NoMatchingError**: Unable to check staffing (no working queue set, permissions issue)

### CRITICAL Requirements
1. MUST call `UpdateContactTargetQueue` before using CheckStaffing
2. MUST have `Conditions` array with both "True" and "False" conditions
3. MUST have `Errors` array with `NoMatchingError`
4. Condition values are strings: `"True"` and `"False"` (not booleans)

### WRONG vs CORRECT

#### WRONG (Missing conditions, using booleans)
```json
{
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
  "Transitions": {
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "agents-available"},
      {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "no-agents"}
    ],
    "Errors": [
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

{"Identifier": "check-staffing", "Type": "CheckStaffing",
 "Parameters": {},
 "Transitions": {
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "transfer-queue"},
     {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "no-agents-message"}
   ],
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
 }}

{"Identifier": "transfer-queue", "Type": "TransferContactToQueue",
 "Parameters": {},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [
     {"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}

{"Identifier": "no-agents-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "All our agents are currently busy. Would you like us to call you back?"},
 "Transitions": {"NextAction": "offer-callback",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]}}
```

### CheckStaffing vs GetQueueMetrics
| Feature | CheckStaffing | GetQueueMetrics |
|---------|---------------|-----------------|
| Purpose | Binary check (available/not) | Detailed metrics |
| Speed | Faster | Slower |
| Output | True/False condition | Multiple metric values |
| Use Case | Simple routing | Complex routing decisions |

### When to Use
- **CheckStaffing**: Simple "are agents available?" check
- **GetQueueMetrics**: Need queue size, wait times, or other details

## Related Topics
- UpdateContactTargetQueue
- TransferContactToQueue
- GetQueueMetrics
- UpdateContactCallbackNumber

---
**Metadata**
- Category: Branch
- BlockType: CheckStaffing
- Keywords: staffing, agents, availability, queue, True, False
