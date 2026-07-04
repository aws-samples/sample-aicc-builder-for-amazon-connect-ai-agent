# GetCustomerProfile Block

## Question
How do I use the GetCustomerProfile block in Amazon Connect Contact Flow?

## Answer
The GetCustomerProfile block retrieves customer profile data from Amazon Connect Customer Profiles. The retrieved data can be used to personalize the customer experience.

### JSON Structure
```json
{
  "Identifier": "get-profile",
  "Type": "GetCustomerProfile",
  "Parameters": {
    "ProfileRequestData": {
      "IdentifierName": "_phone",
      "IdentifierValue": "$.CustomerEndpoint.Address"
    },
    "ProfileResponseData": ["FirstName", "LastName", "EmailAddress"]
  },
  "Transitions": {
    "NextAction": "associate-profile",
    "Errors": [
      {"ErrorType": "MultipleFoundError", "NextAction": "handle-multiple"},
      {"ErrorType": "NoneFoundError", "NextAction": "no-profile-found"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| ProfileRequestData | Object | Contains IdentifierName and IdentifierValue |
| ProfileRequestData.IdentifierName | String | The identifier type ("_phone", "_email", etc.) |
| ProfileRequestData.IdentifierValue | String | The identifier value (can use JSONPath) |

### Optional Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| ProfileResponseData | Array | List of profile fields to retrieve |

### Required Error Types
All three error transitions below are **structurally required** — the flow will FAIL to import if any one is omitted (this is not merely a production recommendation). Each is individually mandatory: omitting any triggers `InvalidContactFlowException` with `Action is missing required error. Error: <Type>`.

- **MultipleFoundError**: Multiple profiles match the identifier
- **NoneFoundError**: No profile found for the identifier
- **NoMatchingError**: General error / catch-all (service unavailable, etc.) — required for import, not just an optional catch-all

### CRITICAL Requirements
1. MUST use `ProfileRequestData` wrapper object (NOT flat parameters!)
2. MUST wire ALL THREE error transitions (`MultipleFoundError`, `NoneFoundError`, `NoMatchingError`). These are individually required for import — omitting any one fails import with `Action is missing required error. Error: <Type>`.
3. Identifier values starting with "_" are reserved (e.g., "_phone", "_email")

### WRONG vs CORRECT

#### WRONG (Import will fail!)
```json
{
  "Parameters": {
    "IdentifierName": "_phone",
    "IdentifierValue": "$.CustomerEndpoint.Address"
  }
}
```

#### CORRECT
```json
{
  "Parameters": {
    "ProfileRequestData": {
      "IdentifierName": "_phone",
      "IdentifierValue": "$.CustomerEndpoint.Address"
    }
  }
}
```

### Common Identifier Types
| IdentifierName | Description | Example Value |
|----------------|-------------|---------------|
| _phone | Phone number | $.CustomerEndpoint.Address |
| _email | Email address | $.Attributes.customerEmail |
| _account | Account number | $.Attributes.accountNumber |

### Accessing Profile Data
After successful retrieval:
- `$.Customer.FirstName`
- `$.Customer.LastName`
- `$.Customer.EmailAddress`
- `$.Customer.ProfileId`

### Complete Pattern: Profile Lookup with Association
```json
{"Identifier": "get-profile", "Type": "GetCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "IdentifierName": "_phone",
     "IdentifierValue": "$.CustomerEndpoint.Address"
   },
   "ProfileResponseData": ["FirstName", "LastName", "EmailAddress"]
 },
 "Transitions": {"NextAction": "associate-profile",
   "Errors": [
     {"ErrorType": "MultipleFoundError", "NextAction": "set-default-name"},
     {"ErrorType": "NoneFoundError", "NextAction": "set-default-name"},
     {"ErrorType": "NoMatchingError", "NextAction": "set-default-name"}
   ]}}

{"Identifier": "associate-profile", "Type": "AssociateContactToCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "ProfileId": "$.Customer.ProfileId",
     "ContactId": "$.ContactId"
   }
 },
 "Transitions": {"NextAction": "update-attrs",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "update-attrs"}]}}

{"Identifier": "update-attrs", "Type": "UpdateContactAttributes",
 "Parameters": {
   "Attributes": {
     "customerFirstName": "$.Customer.FirstName",
     "customerLastName": "$.Customer.LastName"
   }
 },
 "Transitions": {"NextAction": "continue-flow",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "continue-flow"}]}}
```

### Voice vs Chat Lookup
```json
{"Identifier": "check-channel", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.Channel"},
 "Transitions": {"NextAction": "chat-lookup",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["VOICE"]}, "NextAction": "voice-lookup"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "chat-lookup"}]}}

{"Identifier": "voice-lookup", "Type": "GetCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "IdentifierName": "_phone",
     "IdentifierValue": "$.CustomerEndpoint.Address"
   }
 },
 "Transitions": {"NextAction": "greet-customer",
   "Errors": [
     {"ErrorType": "MultipleFoundError", "NextAction": "greet-customer"},
     {"ErrorType": "NoneFoundError", "NextAction": "greet-customer"},
     {"ErrorType": "NoMatchingError", "NextAction": "greet-customer"}
   ]}}

{"Identifier": "chat-lookup", "Type": "GetCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "IdentifierName": "_email",
     "IdentifierValue": "$.Attributes.customerEmail"
   }
 },
 "Transitions": {"NextAction": "greet-customer",
   "Errors": [
     {"ErrorType": "MultipleFoundError", "NextAction": "greet-customer"},
     {"ErrorType": "NoneFoundError", "NextAction": "greet-customer"},
     {"ErrorType": "NoMatchingError", "NextAction": "greet-customer"}
   ]}}
```

## Related Topics
- AssociateContactToCustomerProfile
- UpdateContactAttributes
- Compare Block

---
**Metadata**
- Category: Integrate
- BlockType: GetCustomerProfile
- Keywords: customer profile, lookup, ProfileRequestData, phone, email
