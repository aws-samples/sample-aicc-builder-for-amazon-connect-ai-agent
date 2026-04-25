# CheckStaffing Block

## Question
How do I use the CheckStaffing block to check agent availability in Amazon Connect Contact Flow?

## Answer
The CheckStaffing block checks if agents are available in the working queue. Use this before TransferContactToQueue to provide better customer experience when no agents are available.

### JSON Structure
```json
{
  "Identifier": "check-staffing",
  "Type": "CheckStaffing",
  "Parameters": {},
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

### Required Parameters
None - uses the working queue set by SetWorkingQueue

### Condition Values
- **True**: At least one agent is available in the queue
- **False**: No agents are available (all busy, offline, or not staffed)

### Error Types
- **NoMatchingError**: Unable to check staffing (no working queue set, permissions issue)

### CRITICAL Requirements
1. MUST call `SetWorkingQueue` before using CheckStaffing
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
{"Identifier": "set-queue", "Type": "SetWorkingQueue",
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
- SetWorkingQueue
- TransferContactToQueue
- GetQueueMetrics
- SetCallbackNumber

---
**Metadata**
- Category: Branch
- BlockType: CheckStaffing
- Keywords: staffing, agents, availability, queue, True, False
