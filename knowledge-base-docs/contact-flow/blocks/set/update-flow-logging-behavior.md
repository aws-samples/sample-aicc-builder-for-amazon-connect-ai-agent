# UpdateFlowLoggingBehavior Block

## Question
How do I enable flow logging in Amazon Connect Contact Flow?

## Answer
The UpdateFlowLoggingBehavior block enables or disables flow logging for debugging and monitoring. This is a CRITICAL block often missed in Contact Flow generation.

### JSON Structure
```json
{
  "Identifier": "enable-logging",
  "Type": "UpdateFlowLoggingBehavior",
  "Parameters": {
    "FlowLoggingBehavior": "Enabled"
  },
  "Transitions": {
    "NextAction": "next-block"
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| FlowLoggingBehavior | String | "Enabled" or "Disabled" |

### CRITICAL Requirements
1. Block type is `UpdateFlowLoggingBehavior` (NOT `SetLoggingBehavior` - this is wrong!)
2. Parameter name is `FlowLoggingBehavior` (NOT `LoggingBehavior` - this is wrong!)
3. Value is `"Enabled"` or `"Disabled"` (NOT `"Enable"` or `"Disable"`)

### WRONG vs CORRECT

#### WRONG (Import will fail!)
```json
{
  "Type": "SetLoggingBehavior",
  "Parameters": {
    "LoggingBehavior": "Enable"
  }
}
```

#### CORRECT
```json
{
  "Type": "UpdateFlowLoggingBehavior",
  "Parameters": {
    "FlowLoggingBehavior": "Enabled"
  }
}
```

### Best Practices
1. Place this block at the START of your flow (after voice setup)
2. Enable logging during development and testing
3. Consider disabling when handling sensitive PII to avoid logging it
4. No error handling needed - this block always succeeds

### Complete Example
```json
{
  "Identifier": "enable-logging",
  "Type": "UpdateFlowLoggingBehavior",
  "Parameters": {
    "FlowLoggingBehavior": "Enabled"
  },
  "Transitions": {
    "NextAction": "set-voice"
  }
}
```

## Related Topics
- UpdateContactRecordingBehavior
- Best Practices for Contact Flow Design

---
**Metadata**
- Category: Set
- BlockType: UpdateFlowLoggingBehavior
- Keywords: logging, debug, flow logging, monitoring
