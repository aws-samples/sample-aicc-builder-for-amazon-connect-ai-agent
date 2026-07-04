# GetParticipantInput Block

## Question
How do I use the GetParticipantInput block to collect DTMF input in Amazon Connect Contact Flow?

## Answer
The GetParticipantInput block plays a message and collects DTMF (touch-tone) input from the customer. Use this for menu selections, account numbers, and other numeric input.

> ✅ **API-verified (2026-06-08).** `GetParticipantInput` has TWO DISTINCT modes
> with different required schemas. Mixing them fails import (often with a
> misleading "Invalid Action type" message).

### MODE 1 — MENU (branch on the pressed key via Conditions)
- `StoreInput` MUST be `"False"`.
- MUST NOT include `DTMFConfiguration`.
- Required errors: `NoMatchingCondition` + `InputTimeLimitExceeded` + `NoMatchingError`.

```json
{
  "Identifier": "get-menu-input",
  "Type": "GetParticipantInput",
  "Parameters": {
    "Text": "Press 1 for sales, press 2 for support, or press 3 to speak with an agent.",
    "StoreInput": "False",
    "InputTimeLimitSeconds": "5"
  },
  "Transitions": {
    "NextAction": "invalid-input",
    "Conditions": [
      {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "sales-queue"},
      {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "support-queue"},
      {"Condition": {"Operator": "Equals", "Operands": ["3"]}, "NextAction": "agent-transfer"}
    ],
    "Errors": [
      {"ErrorType": "NoMatchingCondition", "NextAction": "invalid-input"},
      {"ErrorType": "InputTimeLimitExceeded", "NextAction": "timeout-handler"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

### MODE 2 — STORE (capture digits to a contact attribute)
- `StoreInput` MUST be `"True"`.
- Requires `InputValidation.CustomValidation.MaximumLength`.
- `DTMFConfiguration` may carry `InputTerminationSequence` (the terminator key, e.g. `"#"`) and/or `DisableCancelKey`. `DisableCancelKey` MUST be the string `"True"`/`"False"`, never a JSON boolean.
- Only error: `NoMatchingError`. No Conditions.

```json
{
  "Identifier": "collect-account",
  "Type": "GetParticipantInput",
  "Parameters": {
    "Text": "Please enter your 6-digit account number.",
    "StoreInput": "True",
    "InputTimeLimitSeconds": "8",
    "DTMFConfiguration": {"DisableCancelKey": "False"},
    "InputValidation": {"CustomValidation": {"MaximumLength": "6"}}
  },
  "Transitions": {
    "NextAction": "lookup-account",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
  }
}
```

### Error Types
- **InputTimeLimitExceeded**: no input within timeout (menu mode)
- **NoMatchingCondition**: input matched no condition (menu mode)
- **NoMatchingError**: general error (both modes)

### CRITICAL Requirements
1. `StoreInput` is a **required** property in BOTH modes (string `"True"`/`"False"`) — omitting it fails import with `Action is missing required property. Path: ...Parameters.StoreInput`.
2. In MENU mode MUST have all THREE error types (`NoMatchingCondition` + `InputTimeLimitExceeded` + `NoMatchingError`); in STORE mode the ONLY allowed error is `NoMatchingError`.
3. `DisableCancelKey` MUST be the string `"True"`/`"False"`, never a JSON boolean. A boolean value produces a misleading top-level `Invalid Action type. Type: GetParticipantInput` error.
4. `InputTimeLimitSeconds` — the API accepts both a string (`"5"`) and a JSON number (`5`); prefer the string for consistency with `StoreInput`/`DisableCancelKey`.
5. MENU mode uses a `Conditions` array to branch on the pressed key; STORE mode must NOT use `Conditions`.
6. In STORE mode, input is stored in `$.StoredCustomerInput` for later use.

### Accessing Customer Input
After this block, the input is available at:
- `$.StoredCustomerInput` - The digits entered

### Pattern: IVR Menu
```json
{"Identifier": "main-menu", "Type": "GetParticipantInput",
 "Parameters": {
   "Text": "Main menu. Press 1 for account balance, press 2 for recent transactions, press 3 for customer service.",
   "StoreInput": "False",
   "InputTimeLimitSeconds": "10"
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
   "StoreInput": "True",
   "InputTimeLimitSeconds": "30",
   "DTMFConfiguration": {"InputTerminationSequence": "#", "DisableCancelKey": "False"},
   "InputValidation": {"CustomValidation": {"MaximumLength": "10"}}
 },
 "Transitions": {"NextAction": "validate-account",
   "Errors": [
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
   "StoreInput": "False",
   "InputTimeLimitSeconds": "5"
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
