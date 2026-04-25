# GetParticipantInput Block

## Question
How do I use the GetParticipantInput block to collect DTMF input in Amazon Connect Contact Flow?

## Answer
The GetParticipantInput block plays a message and collects DTMF (touch-tone) input from the customer. Use this for menu selections, account numbers, and other numeric input.

### JSON Structure
```json
{
  "Identifier": "get-menu-input",
  "Type": "GetParticipantInput",
  "Parameters": {
    "Text": "Press 1 for sales, press 2 for support, or press 3 to speak with an agent.",
    "InputTimeLimitSeconds": "5",
    "DTMFConfiguration": {
      "InputTerminationSequence": "#",
      "DisableCancelKey": false
    }
  },
  "Transitions": {
    "NextAction": "invalid-input",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "sales-queue"},
      {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "support-queue"},
      {"Condition": {"Operator": "Equals", "Operands": ["3"]}, "NextAction": "agent-transfer"}
    ],
    "Errors": [
      {"ErrorType": "InputTimeLimitExceeded", "NextAction": "timeout-handler"},
      {"ErrorType": "NoMatchingCondition", "NextAction": "invalid-input"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| Text | String | Message to play before collecting input |
| InputTimeLimitSeconds | String | Seconds to wait for input (1-180) |
| DTMFConfiguration | Object | DTMF input settings |

### DTMFConfiguration Options
| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| InputTerminationSequence | String | Key to end input (e.g., "#") | None |
| DisableCancelKey | Boolean | Disable * key cancel | false |

### Error Types
- **InputTimeLimitExceeded**: Customer didn't provide input within timeout
- **NoMatchingCondition**: Input didn't match any condition
- **NoMatchingError**: General error (playback failed, etc.)

### CRITICAL Requirements
1. MUST have all THREE error types for production flows
2. `InputTimeLimitSeconds` must be a string ("5"), not a number
3. MUST have `Conditions` array for expected inputs
4. Input is stored in `$.StoredCustomerInput` for later use

### Accessing Customer Input
After this block, the input is available at:
- `$.StoredCustomerInput` - The digits entered

### Pattern: IVR Menu
```json
{"Identifier": "main-menu", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Main menu. Press 1 for account balance, press 2 for recent transactions, press 3 for customer service.",
   "InputTimeLimitSeconds": "10",
   "DTMFConfiguration": {"InputTerminationSequence": "#", "DisableCancelKey": false}
 },
 "Transitions": {"NextAction": "repeat-menu",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "account-balance"},
     {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "recent-transactions"},
     {"Condition": {"Operator": "Equals", "Operands": ["3"]}, "NextAction": "customer-service"}
   ],
   "Errors": [
     {"ErrorType": "InputTimeLimitExceeded", "NextAction": "timeout-message"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "invalid-option"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]
 }}

{"Identifier": "invalid-option", "Type": "MessageParticipant",
 "Parameters": {"Text": "That's not a valid option. Let me repeat the menu."},
 "Transitions": {"NextAction": "main-menu",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "main-menu"}]}}

{"Identifier": "timeout-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "I didn't receive any input. Transferring you to an agent."},
 "Transitions": {"NextAction": "agent-transfer",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "agent-transfer"}]}}
```

### Pattern: Account Number Collection
```json
{"Identifier": "collect-account", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Please enter your 10-digit account number, followed by the pound key.",
   "InputTimeLimitSeconds": "30",
   "DTMFConfiguration": {"InputTerminationSequence": "#", "DisableCancelKey": false}
 },
 "Transitions": {"NextAction": "validate-account",
   "Errors": [
     {"ErrorType": "InputTimeLimitExceeded", "NextAction": "input-timeout"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "validate-account"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]
 }}

{"Identifier": "validate-account", "Type": "InvokeLambdaFunction",
 "Parameters": {
   "LambdaFunctionARN": "{{VALIDATE_ACCOUNT_LAMBDA}}",
   "InvocationTimeLimitSeconds": "8",
   "ResponseValidation": {"ResponseType": "STRING_MAP"},
   "LambdaInvocationAttributes": {"accountNumber": "$.StoredCustomerInput"}
 },
 "Transitions": {"NextAction": "check-validation",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

### Pattern: Callback Confirmation
```json
{"Identifier": "confirm-callback", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "We will call you back at this number. Press 1 to confirm, or press 2 to enter a different number.",
   "InputTimeLimitSeconds": "5",
   "DTMFConfiguration": {"DisableCancelKey": false}
 },
 "Transitions": {"NextAction": "use-current-number",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "use-current-number"},
     {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "get-callback-number"}
   ],
   "Errors": [
     {"ErrorType": "InputTimeLimitExceeded", "NextAction": "use-current-number"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "use-current-number"},
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
   ]
 }}
```

## Related Topics
- MessageParticipant
- StoreUserInput
- Compare Block
- InvokeLambdaFunction

---
**Metadata**
- Category: Interact
- BlockType: GetParticipantInput
- Keywords: DTMF, input, menu, touch-tone, IVR, InputTimeLimitExceeded, NoMatchingCondition
