# Callback Pattern

## Question
How do I implement queue callbacks in Amazon Connect Contact Flow?

## Answer
The callback pattern allows customers to receive a callback instead of waiting on hold. When the queue is full or agents are unavailable, offer the customer a callback option.

## Key Components

1. **UpdateContactTargetQueue** - Sets the queue the contact will be transferred to (takes the queue ARN)
2. **TransferContactToQueue** - Transfers the contact into the queue already set on it (takes **no** QueueId)
3. **UpdateContactCallbackNumber** - Sets the phone number for the callback
4. **CreateCallbackContact** - Creates the callback contact in the queue

## How Callbacks Are Created
`CreateCallbackContact` is a real, valid block. Set the callback number with `UpdateContactCallbackNumber`, then create the callback with `CreateCallbackContact` (which requires `InitialCallDelaySeconds`, `MaximumConnectionAttempts`, and `RetryDelaySeconds`).

## Complete Pattern Implementation

```json
{
  "Version": "2019-10-30",
  "StartAction": "set-queue",
  "Metadata": {
    "entryPointPosition": {"x": 40, "y": 40},
    "ActionMetadata": {
      "set-queue": {"position": {"x": 280, "y": 40}, "isFriendlyName": true},
      "transfer-queue": {"position": {"x": 0, "y": 300}, "isFriendlyName": true},
      "offer-callback": {"position": {"x": 560, "y": 560}, "isFriendlyName": true},
      "callback-confirm": {"position": {"x": 280, "y": 820}, "isFriendlyName": true},
      "set-callback": {"position": {"x": 280, "y": 1080}, "isFriendlyName": true},
      "create-callback": {"position": {"x": 280, "y": 1340}, "isFriendlyName": true},
      "callback-scheduled": {"position": {"x": 280, "y": 1600}, "isFriendlyName": true},
      "invalid-callback": {"position": {"x": 560, "y": 1340}, "isFriendlyName": true},
      "error-handler": {"position": {"x": 840, "y": 560}, "isFriendlyName": true},
      "disconnect": {"position": {"x": 280, "y": 1860}, "isFriendlyName": true}
    },
    "name": "Callback Pattern Flow",
    "type": "contactFlow",
    "status": "DRAFT",
    "hash": {}
  },
  "Actions": [
    {
      "Identifier": "set-queue",
      "Type": "UpdateContactTargetQueue",
      "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
      "Transitions": {
        "NextAction": "transfer-queue",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
      }
    },
    {
      "Identifier": "transfer-queue",
      "Type": "TransferContactToQueue",
      "Parameters": {},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [
          {"ErrorType": "QueueAtCapacity", "NextAction": "offer-callback"},
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "offer-callback",
      "Type": "GetParticipantInput",
      "Parameters": {
        "Text": "Press 1 to receive a callback when an agent is available, or press 2 to continue waiting.",
        "StoreInput": "False",
        "InputTimeLimitSeconds": "10"
      },
      "Transitions": {
        "NextAction": "disconnect",
        "Conditions": [
          {"Condition": {"Operator": "Equals", "Operands": ["1"]}, "NextAction": "callback-confirm"},
          {"Condition": {"Operator": "Equals", "Operands": ["2"]}, "NextAction": "transfer-queue"}
        ],
        "Errors": [
          {"ErrorType": "InputTimeLimitExceeded", "NextAction": "transfer-queue"},
          {"ErrorType": "NoMatchingCondition", "NextAction": "offer-callback"},
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "callback-confirm",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "We will call you back at your current number when an agent is available."},
      "Transitions": {
        "NextAction": "set-callback",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "set-callback"}]
      }
    },
    {
      "Identifier": "set-callback",
      "Type": "UpdateContactCallbackNumber",
      "Parameters": {"CallbackNumber": "$.CustomerEndpoint.Address"},
      "Transitions": {
        "NextAction": "create-callback",
        "Errors": [
          {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-callback"},
          {"ErrorType": "CallbackNumberNotDialable", "NextAction": "invalid-callback"}
        ]
      }
    },
    {
      "Identifier": "create-callback",
      "Type": "CreateCallbackContact",
      "Parameters": {
        "InitialCallDelaySeconds": "20",
        "MaximumConnectionAttempts": "3",
        "RetryDelaySeconds": "60"
      },
      "Transitions": {
        "NextAction": "callback-scheduled",
        "Errors": [
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "callback-scheduled",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "Your callback has been scheduled. We will call you shortly. Goodbye!"},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]
      }
    },
    {
      "Identifier": "invalid-callback",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "Sorry, we cannot schedule a callback to this number. Please try calling back later."},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]
      }
    },
    {
      "Identifier": "error-handler",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "We're experiencing technical difficulties. Please try again later."},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]
      }
    },
    {
      "Identifier": "disconnect",
      "Type": "DisconnectParticipant",
      "Parameters": {},
      "Transitions": {}
    }
  ]
}
```

## UpdateContactCallbackNumber Error Handling

CRITICAL: UpdateContactCallbackNumber requires exactly TWO error types — `InvalidCallbackNumber` and `CallbackNumberNotDialable`. `NoMatchingError` is NOT allowed on this block (the API rejects it), and there are no `InvalidNumber`/`NotDialable` error types.

```json
{
  "Errors": [
    {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-callback"},
    {"ErrorType": "CallbackNumberNotDialable", "NextAction": "invalid-callback"}
  ]
}
```

| Error Type | Description | Common Cause |
|------------|-------------|--------------|
| InvalidCallbackNumber | Format is invalid | Missing country code, wrong format |
| CallbackNumberNotDialable | Valid but can't dial | Blocked numbers, out of service area |

## Creating the Callback Contact

`CreateCallbackContact` IS a valid block Type. It requires three parameters — `InitialCallDelaySeconds`, `MaximumConnectionAttempts`, and `RetryDelaySeconds`:

```json
{
  "Identifier": "create-callback",
  "Type": "CreateCallbackContact",
  "Parameters": {
    "InitialCallDelaySeconds": "20",
    "MaximumConnectionAttempts": "3",
    "RetryDelaySeconds": "60"
  },
  "Transitions": {
    "NextAction": "callback-scheduled",
    "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
  }
}
```

## Queue Transfer Note

`TransferContactToQueue` takes NO `QueueId` parameter — it transfers the contact to the queue already set on it. Set the queue first with `UpdateContactTargetQueue` (which takes the queue ARN as `QueueId`), then call `TransferContactToQueue` with empty `Parameters`.

## Callback with Custom Number Collection

If you want to allow customers to enter a different callback number:

When you store input, `GetParticipantInput` runs in **store mode**: set `StoreInput` to `"True"`, supply an `InputValidation` block, and the only error type allowed is `NoMatchingError` (store mode does not accept `InputTimeLimitExceeded` or `NoMatchingCondition`). There is no `DTMFConfiguration` property on this block.

```json
{
  "Identifier": "get-callback-number",
  "Type": "GetParticipantInput",
  "Parameters": {
    "Text": "Please enter the 10-digit phone number where you'd like to receive a callback, followed by the pound key.",
    "StoreInput": "True",
    "InputTimeLimitSeconds": "30",
    "InputValidation": {"CustomValidation": {"MaximumLength": "10"}}
  },
  "Transitions": {
    "NextAction": "set-custom-callback",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}

{
  "Identifier": "set-custom-callback",
  "Type": "UpdateContactCallbackNumber",
  "Parameters": {"CallbackNumber": "$.StoredCustomerInput"},
  "Transitions": {
    "NextAction": "create-callback",
    "Errors": [
      {"ErrorType": "InvalidCallbackNumber", "NextAction": "invalid-number-retry"},
      {"ErrorType": "CallbackNumberNotDialable", "NextAction": "invalid-number-retry"}
    ]
  }
}
```

## Related Topics
- UpdateContactCallbackNumber
- CreateCallbackContact
- UpdateContactTargetQueue
- TransferContactToQueue
- GetParticipantInput

---
**Metadata**
- Category: Pattern
- Keywords: callback, queue callback, UpdateContactCallbackNumber, CreateCallbackContact, InvalidCallbackNumber, CallbackNumberNotDialable
