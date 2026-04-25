# UpdateContactAttributes Block

## Question
How do I use the UpdateContactAttributes block in Amazon Connect Contact Flow?

## Answer
The UpdateContactAttributes block sets or updates custom contact attributes. These attributes can be used for routing decisions, passed to agents, or used in other parts of the flow.

### JSON Structure
```json
{
  "Identifier": "set-attrs",
  "Type": "UpdateContactAttributes",
  "Parameters": {
    "Attributes": {
      "customerId": "12345",
      "orderNumber": "$.External.orderId",
      "customerName": "$.Customer.FirstName"
    }
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| Attributes | Object | Key-value pairs of attributes to set |

### Error Types
- **NoMatchingError**: Failed to set attributes (32KB limit exceeded, invalid values)

### CRITICAL Requirements
1. Attribute names must be valid (alphanumeric, underscores)
2. Total contact attributes cannot exceed 32KB
3. Values can be static strings or JSONPath references

### JSONPath References
You can reference other attributes using JSONPath:

| Source | JSONPath | Example |
|--------|----------|---------|
| Customer Profile | $.Customer.{field} | $.Customer.FirstName |
| Lambda Response | $.External.{key} | $.External.status |
| Lex Session | $.Lex.SessionAttributes.{key} | $.Lex.SessionAttributes.intent |
| Lex Slots | $.Lex.Slots.{name} | $.Lex.Slots.date |
| Customer Input | $.StoredCustomerInput | DTMF input |
| System | $.CustomerEndpoint.Address | Phone number |
| System | $.Channel | VOICE, CHAT, TASK |
| System | $.ContactId | Contact ID |

### Common Patterns

#### Store Customer Info
```json
{
  "Identifier": "store-customer-info",
  "Type": "UpdateContactAttributes",
  "Parameters": {
    "Attributes": {
      "customerFirstName": "$.Customer.FirstName",
      "customerLastName": "$.Customer.LastName",
      "customerPhone": "$.CustomerEndpoint.Address",
      "profileId": "$.Customer.ProfileId"
    }
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "next-block"}]
  }
}
```

#### Store Lambda Results
```json
{
  "Identifier": "store-lambda-result",
  "Type": "UpdateContactAttributes",
  "Parameters": {
    "Attributes": {
      "accountStatus": "$.External.status",
      "accountBalance": "$.External.balance",
      "lastPaymentDate": "$.External.lastPayment"
    }
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "next-block"}]
  }
}
```

#### Store Escalation Context
```json
{
  "Identifier": "set-escalation-context",
  "Type": "UpdateContactAttributes",
  "Parameters": {
    "Attributes": {
      "customerIntent": "$.Lex.SessionAttributes.customerIntent",
      "escalationReason": "$.Lex.SessionAttributes.escalationReason",
      "conversationSummary": "$.Lex.SessionAttributes.conversationSummary",
      "callerPhoneNumber": "$.CustomerEndpoint.Address"
    }
  },
  "Transitions": {
    "NextAction": "transfer-queue",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "transfer-queue"}]
  }
}
```

### NON-EXISTENT Block Warning
There is NO `SetContactAttributes` block type!

WRONG:
```json
{"Type": "SetContactAttributes"}  // DOES NOT EXIST!
```

CORRECT:
```json
{"Type": "UpdateContactAttributes"}  // Use this instead
```

### Accessing Stored Attributes
After setting attributes, access them with:
- `$.Attributes.{name}` in flow blocks
- Contact Trace Records (CTR) for reporting
- Agent desktop applications

### Size Limits
- Total contact attributes: 32KB
- Individual attribute value: No specific limit, but counts toward total

## Related Topics
- InvokeLambdaFunction
- GetCustomerProfile
- Compare Block

---
**Metadata**
- Category: Set
- BlockType: UpdateContactAttributes
- Keywords: attributes, contact attributes, custom, store, set attributes
