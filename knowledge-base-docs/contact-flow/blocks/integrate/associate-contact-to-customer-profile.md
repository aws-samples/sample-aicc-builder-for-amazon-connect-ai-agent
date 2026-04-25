# AssociateContactToCustomerProfile Block

## Question
How do I use the AssociateContactToCustomerProfile block in Amazon Connect Contact Flow?

## Answer
The AssociateContactToCustomerProfile block links the current contact to a customer profile, enabling features like Contact Lens customer analytics and Q in Connect context.

### JSON Structure
```json
{
  "Identifier": "associate-profile",
  "Type": "AssociateContactToCustomerProfile",
  "Parameters": {
    "ProfileRequestData": {
      "ProfileId": "$.Customer.ProfileId",
      "ContactId": "$.ContactId"
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
| ProfileRequestData | Object | Contains ProfileId and ContactId |
| ProfileRequestData.ProfileId | String | The customer profile ID (from GetCustomerProfile) |
| ProfileRequestData.ContactId | String | The current contact ID |

### Error Types
- **NoMatchingError**: Association failed (invalid ProfileId, permissions, etc.)

### CRITICAL Requirements
1. MUST use `ProfileRequestData` wrapper object (NOT flat parameters!)
2. MUST be called AFTER GetCustomerProfile to have $.Customer.ProfileId available
3. `$.ContactId` is a system attribute always available in Contact Flows

### WRONG vs CORRECT

#### WRONG (Import will fail!)
```json
{
  "Parameters": {
    "ProfileId": "$.Customer.ProfileId",
    "ContactId": "$.ContactId"
  }
}
```

#### CORRECT
```json
{
  "Parameters": {
    "ProfileRequestData": {
      "ProfileId": "$.Customer.ProfileId",
      "ContactId": "$.ContactId"
    }
  }
}
```

### Common Pattern: Full Profile Association Flow
```json
{"Identifier": "get-profile", "Type": "GetCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "IdentifierName": "_phone",
     "IdentifierValue": "$.CustomerEndpoint.Address"
   },
   "ProfileResponseData": ["FirstName", "LastName"]
 },
 "Transitions": {"NextAction": "associate-profile",
   "Errors": [
     {"ErrorType": "MultipleFoundError", "NextAction": "skip-association"},
     {"ErrorType": "NoneFoundError", "NextAction": "skip-association"},
     {"ErrorType": "NoMatchingError", "NextAction": "skip-association"}
   ]}}

{"Identifier": "associate-profile", "Type": "AssociateContactToCustomerProfile",
 "Parameters": {
   "ProfileRequestData": {
     "ProfileId": "$.Customer.ProfileId",
     "ContactId": "$.ContactId"
   }
 },
 "Transitions": {"NextAction": "continue-flow",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "continue-flow"}]}}
```

### Benefits of Association
1. **Contact Lens**: Customer analytics tied to profile
2. **Q in Connect**: AI can access customer context
3. **Agent Workspace**: Agents see customer history
4. **Reporting**: Contact data linked to customer profile

## Related Topics
- GetCustomerProfile
- UpdateContactAttributes
- Contact Attributes Reference

---
**Metadata**
- Category: Integrate
- BlockType: AssociateContactToCustomerProfile
- Keywords: customer profile, associate, link, ProfileRequestData, ContactId
