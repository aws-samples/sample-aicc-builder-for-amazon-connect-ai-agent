# InvokeLambdaFunction Block

## Question
How do I invoke a Lambda function in Amazon Connect Contact Flow?

## Answer
The InvokeLambdaFunction block calls an AWS Lambda function and can pass/receive contact attributes. The Lambda response is available via `$.External.*` attributes.

### JSON Structure
```json
{
  "Identifier": "invoke-lambda",
  "Type": "InvokeLambdaFunction",
  "Parameters": {
    "LambdaFunctionARN": "{{LAMBDA_ARN}}",
    "InvocationTimeLimitSeconds": "8",
    "ResponseValidation": {
      "ResponseType": "STRING_MAP"
    },
    "LambdaInvocationAttributes": {
      "customerId": "$.Attributes.customerId",
      "phoneNumber": "$.CustomerEndpoint.Address"
    }
  },
  "Transitions": {
    "NextAction": "process-result",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| LambdaFunctionARN | String | The ARN of the Lambda function |
| InvocationTimeLimitSeconds | String | Timeout in seconds (max 8) |
| ResponseValidation | Object | Must contain `ResponseType: "STRING_MAP"` or `"JSON"` |

### Optional Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| LambdaInvocationAttributes | Object | Key-value pairs to pass to Lambda |
| InvocationType | String | `"SYNCHRONOUS"` (default) or `"ASYNCHRONOUS"` |

### Error Types
- **NoMatchingError**: Lambda failed, timed out, or returned invalid response

### CRITICAL Requirements
1. `InvocationTimeLimitSeconds` MUST be a string (e.g., "8"), 1–8 inclusive
2. `ResponseType` MUST be `"STRING_MAP"` OR `"JSON"` — both are officially supported
   - Use `STRING_MAP` for flat key/value string maps (simpler, stricter validation)
   - Use `JSON` when the Lambda returns nested JSON that must preserve its structure
3. MUST have `Errors` array with `NoMatchingError`
4. Lambda response values are accessed via `$.External.{key}`

### STRING_MAP vs JSON — When to Use Which

| ResponseType | Lambda Return Format | Access Pattern | Notes |
|--------------|----------------------|----------------|-------|
| `STRING_MAP` | Flat `{key: "value"}` — all strings | `$.External.key` | Strict; rejects nested objects |
| `JSON` | Any valid JSON (nested OK) | `$.External.key`, `$.External.nested.field` | Preserves nested structure |

Both are valid. Prefer `STRING_MAP` unless you specifically need nested JSON.

#### Example: JSON response (nested OK)
```json
{
  "Parameters": {
    "ResponseValidation": {"ResponseType": "JSON"}
  }
}
```

#### Example: STRING_MAP response (flat only)
```json
{
  "Parameters": {
    "ResponseValidation": {"ResponseType": "STRING_MAP"}
  }
}
```

### Lambda Response Format
Your Lambda function MUST return a flat key-value map:

```python
def handler(event, context):
    return {
        "status": "success",
        "customerId": "12345",
        "customerName": "John Doe"
    }
```

Access in Contact Flow:
- `$.External.status`
- `$.External.customerId`
- `$.External.customerName`

### Common Pattern: Lambda with Result Check
```json
{"Identifier": "invoke-lambda", "Type": "InvokeLambdaFunction",
 "Parameters": {
   "LambdaFunctionARN": "{{LAMBDA_ARN}}",
   "InvocationTimeLimitSeconds": "8",
   "ResponseValidation": {"ResponseType": "STRING_MAP"}
 },
 "Transitions": {"NextAction": "check-result",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}

{"Identifier": "check-result", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.External.status"},
 "Transitions": {"NextAction": "error-handler",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["success"]}, "NextAction": "continue-flow"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "error-handler"}]}}
```

### Passing Contact Attributes to Lambda
```json
{
  "Parameters": {
    "LambdaFunctionARN": "{{LAMBDA_ARN}}",
    "InvocationTimeLimitSeconds": "8",
    "ResponseValidation": {"ResponseType": "STRING_MAP"},
    "LambdaInvocationAttributes": {
      "phoneNumber": "$.CustomerEndpoint.Address",
      "customerId": "$.Attributes.customerId",
      "intentName": "$.Lex.SessionAttributes.intentName"
    }
  }
}
```

## Related Topics
- Compare Block (for checking Lambda response)
- UpdateContactAttributes (for storing Lambda results)
- Contact Attributes Reference

---
**Metadata**
- Category: Integrate
- BlockType: InvokeLambdaFunction
- Keywords: lambda, function, integration, external, STRING_MAP, timeout
