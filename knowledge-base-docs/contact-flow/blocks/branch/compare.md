# Compare Block

## Question
How do I use the Compare block to evaluate conditions in Amazon Connect Contact Flow?

## Answer
The Compare block evaluates a contact attribute value against conditions. It's used for routing decisions based on Lex results, Lambda responses, or custom attributes.

### JSON Structure
```json
{
  "Identifier": "check-result",
  "Type": "Compare",
  "Parameters": {
    "ComparisonValue": "$.Lex.SessionAttributes.toolResult"
  },
  "Transitions": {
    "NextAction": "default-action",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["SUCCESS"]}, "NextAction": "success-path"},
      {"Condition": {"Operator": "Equals", "Operands": ["ERROR"]}, "NextAction": "error-path"},
      {"Condition": {"Operator": "Equals", "Operands": ["ESCALATION"]}, "NextAction": "escalate"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingCondition", "NextAction": "default-action"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| ComparisonValue | String | The value to compare (use JSONPath for attributes) |

### Comparison Operators
| Operator | Description | Example |
|----------|-------------|---------|
| Equals | Exact string match (also used for numeric equality) | `{"Operator": "Equals", "Operands": ["VALUE"]}` |
| TextContains | String contains | `{"Operator": "TextContains", "Operands": ["substring"]}` |
| TextStartsWith | String starts with | `{"Operator": "TextStartsWith", "Operands": ["prefix"]}` |
| TextEndsWith | String ends with | `{"Operator": "TextEndsWith", "Operands": ["suffix"]}` |
| NumberGreaterThan | Numeric > | `{"Operator": "NumberGreaterThan", "Operands": ["10"]}` |
| NumberLessThan | Numeric < | `{"Operator": "NumberLessThan", "Operands": ["100"]}` |
| NumberGreaterOrEqualTo | Numeric >= | `{"Operator": "NumberGreaterOrEqualTo", "Operands": ["0"]}` |
| NumberLessOrEqualTo | Numeric <= | `{"Operator": "NumberLessOrEqualTo", "Operands": ["99"]}` |

> **Note:** There is no `NumberEquals` operator. Use plain `Equals` for numeric
> equality (e.g. `{"Operator": "Equals", "Operands": ["5"]}`). Also note the
> asymmetry: the `>=` operator is `NumberGreaterOrEqualTo` and the `<=` operator
> is `NumberLessOrEqualTo` (both drop the "Than").

### Error Types
- **NoMatchingCondition**: No condition matched the value

### CRITICAL Requirements
1. MUST have `Errors` array with `NoMatchingCondition`
2. `NextAction` in Transitions is the default fallback (also handles NoMatchingCondition)
3. Operands are always arrays: `["VALUE"]` not `"VALUE"` (a bare string is rejected)
4. Pass numbers as strings: `["10"]` (recommended). A bare int `[10]` also passes structural validation, but the quoted-string form is the safe convention for runtime.

### Common JSONPath Values
| Source | JSONPath | Description |
|--------|----------|-------------|
| Lex Session | $.Lex.SessionAttributes.{key} | Lex session attribute |
| Lex Intent | $.Lex.IntentName | Name of matched intent |
| Lex Slots | $.Lex.Slots.{slotName} | Slot value |
| Lambda | $.External.{key} | Lambda response |
| Custom | $.Attributes.{name} | Custom contact attribute |
| Channel | $.Channel | VOICE, CHAT, or TASK |

> **Queue metrics:** To branch on queue depth or agent availability, use the
> `CheckMetricData` block (it branches on the metric value directly) rather than a
> Compare on a `$.Metrics.*` path. See the Queue Size Check pattern below.

### Pattern: Lex Bot Result Routing
```json
{"Identifier": "lex-bot", "Type": "ConnectParticipantWithLexBot",
 "Parameters": {"Text": "Welcome", "LexV2Bot": {"AliasArn": "{{LEX_BOT_ALIAS_ARN}}"}},
 "Transitions": {"NextAction": "check-result",
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "error-handler"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}

{"Identifier": "check-result", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.toolResult"},
 "Transitions": {"NextAction": "lex-bot",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["ESCALATION"]}, "NextAction": "transfer"},
     {"Condition": {"Operator": "Equals", "Operands": ["COMPLETE"]}, "NextAction": "goodbye"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "lex-bot"}]}}
```

### Pattern: Channel-Based Routing
```json
{"Identifier": "check-channel", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.Channel"},
 "Transitions": {"NextAction": "chat-flow",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["VOICE"]}, "NextAction": "voice-flow"},
     {"Condition": {"Operator": "Equals", "Operands": ["CHAT"]}, "NextAction": "chat-flow"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "chat-flow"}]}}
```

### Pattern: Queue Size Check
There is **no** `GetQueueMetrics` block type (it fails import with "Invalid Action
type"). To branch on queue depth, use **`CheckMetricData`** — it reads one
real-time metric (`MetricType`) for the working queue and branches on the value
itself via `Conditions`. There is no need for a separate Compare block on
`$.Metrics.Queue.Size`. Set the working queue with `UpdateContactTargetQueue`
first, and use `MetricType: NumberOfContactsInQueue`.

```json
{"Identifier": "set-queue", "Type": "UpdateContactTargetQueue",
 "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
 "Transitions": {"NextAction": "check-queue-size",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-queue-size", "Type": "CheckMetricData",
 "Parameters": {"MetricType": "NumberOfContactsInQueue"},
 "Transitions": {"NextAction": "queue-busy",
   "Conditions": [
     {"Condition": {"Operator": "NumberLessThan", "Operands": ["5"]}, "NextAction": "transfer-queue"}
   ],
   "Errors": [
     {"ErrorType": "NoMatchingCondition", "NextAction": "queue-busy"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]}}
```

## Related Topics
- CheckContactAttributes (alternative for attribute checks)
- CheckMetricData (queue/staffing metrics — the real block; no `GetQueueMetrics` type exists)
- ConnectParticipantWithLexBot
- InvokeLambdaFunction

---
**Metadata**
- Category: Branch
- BlockType: Compare
- Keywords: compare, condition, routing, Equals, TextContains, TextStartsWith, TextEndsWith, NumberLessOrEqualTo, NumberGreaterOrEqualTo, NumberLessThan, NoMatchingCondition
