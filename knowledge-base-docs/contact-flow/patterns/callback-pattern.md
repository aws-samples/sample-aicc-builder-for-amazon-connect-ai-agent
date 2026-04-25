# Callback Pattern

## Question
How do I implement queue callbacks in Amazon Connect Contact Flow?

## Answer
The callback pattern allows customers to receive a callback instead of waiting on hold. When the queue is full or agents are unavailable, offer the customer a callback option.

## Key Components

1. **SetCallbackNumber** - Sets the phone number for callback
2. **TransferContactToQueue** - Creates the callback contact in the queue

## CRITICAL: There is NO CreateCallbackContact Block!
Callbacks are created by calling SetCallbackNumber followed by TransferContactToQueue.

## Complete Pattern Implementation

```json
{
  "Version": "2019-10-30",
  "StartAction": "check-staffing",
  "Metadata": {
    "entryPointPosition": {"x": 40, "y": 40},
    "ActionMetadata": {
      "check-staffing": {"position": {"x": 280, "y": 40}, "isFriendlyName": true},
      "transfer-queue": {"position": {"x": 0, "y": 300}, "isFriendlyName": true},
      "no-agents": {"position": {"x": 560, "y": 300}, "isFriendlyName": true},
      "offer-callback": {"position": {"x": 560, "y": 560}, "isFriendlyName": true},
      "callback-confirm": {"position": {"x": 280, "y": 820}, "isFriendlyName": true},
      "set-callback": {"position": {"x": 280, "y": 1080}, "isFriendlyName": true},
      "create-callback": {"position": {"x": 280, "y": 1340}, "isFriendlyName": true},
      "callback-scheduled": {"position": {"x": 280, "y": 1600}, "isFriendlyName": true},
      "invalid-callback": {"position": {"x": 560, "y": 1340}, "isFriendlyName": true},
      "queue-full": {"position": {"x": -280, "y": 560}, "isFriendlyName": true},
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
      "Identifier": "check-staffing",
      "Type": "CheckStaffing",
      "Parameters": {},
      "Transitions": {
        "Conditions": [
          {"Condition": {"Operator": "Equals", "Operands": ["True"]}, "NextAction": "transfer-queue"},
          {"Condition": {"Operator": "Equals", "Operands": ["False"]}, "NextAction": "no-agents"}
        ],
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
      }
    },
    {
      "Identifier": "transfer-queue",
      "Type": "TransferContactToQueue",
      "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [
          {"ErrorType": "QueueAtCapacity", "NextAction": "queue-full"},
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "no-agents",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "All our agents are currently busy."},
      "Transitions": {
        "NextAction": "offer-callback",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]
      }
    },
    {
      "Identifier": "queue-full",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "We are experiencing high call volume."},
      "Transitions": {
        "NextAction": "offer-callback",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "offer-callback"}]
      }
    },
    {
      "Identifier": "offer-callback",
      "Type": "GetParticipantInput",
      "Parameters": {
        "Text": "Press 1 to receive a callback when an agent is available, or press 2 to continue waiting.",
        "InputTimeLimitSeconds": "10",
        "DTMFConfiguration": {"DisableCancelKey": false}
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
      "Type": "SetCallbackNumber",
      "Parameters": {"CallbackNumber": "$.CustomerEndpoint.Address"},
      "Transitions": {
        "NextAction": "create-callback",
        "Errors": [
          {"ErrorType": "InvalidNumber", "NextAction": "invalid-callback"},
          {"ErrorType": "NotDialable", "NextAction": "invalid-callback"},
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "create-callback",
      "Type": "TransferContactToQueue",
      "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
      "Transitions": {
        "NextAction": "callback-scheduled",
        "Errors": [
          {"ErrorType": "QueueAtCapacity", "NextAction": "invalid-callback"},
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

## SetCallbackNumber Error Handling

CRITICAL: SetCallbackNumber requires handling THREE error types:

```json
{
  "Errors": [
    {"ErrorType": "InvalidNumber", "NextAction": "invalid-callback"},
    {"ErrorType": "NotDialable", "NextAction": "invalid-callback"},
    {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
  ]
}
```

| Error Type | Description | Common Cause |
|------------|-------------|--------------|
| InvalidNumber | Format is invalid | Missing country code, wrong format |
| NotDialable | Valid but can't dial | Blocked numbers, out of service area |
| NoMatchingError | General failure | Permissions, service issue |

## NON-EXISTENT Block Warning

WRONG:
```json
{"Type": "CreateCallbackContact"}  // DOES NOT EXIST!
```

CORRECT:
```json
{"Type": "SetCallbackNumber"}  // Step 1: Set the number
// followed by
{"Type": "TransferContactToQueue"}  // Step 2: Creates the callback
```

## Callback with Custom Number Collection

If you want to allow customers to enter a different callback number:

```json
{
  "Identifier": "get-callback-number",
  "Type": "GetParticipantInput",
  "Parameters": {
    "Text": "Please enter the 10-digit phone number where you'd like to receive a callback, followed by the pound key.",
    "InputTimeLimitSeconds": "30",
    "DTMFConfiguration": {"InputTerminationSequence": "#"}
  },
  "Transitions": {
    "NextAction": "set-custom-callback",
    "Errors": [
      {"ErrorType": "InputTimeLimitExceeded", "NextAction": "use-current-number"},
      {"ErrorType": "NoMatchingCondition", "NextAction": "set-custom-callback"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}

{
  "Identifier": "set-custom-callback",
  "Type": "SetCallbackNumber",
  "Parameters": {"CallbackNumber": "$.StoredCustomerInput"},
  "Transitions": {
    "NextAction": "create-callback",
    "Errors": [
      {"ErrorType": "InvalidNumber", "NextAction": "invalid-number-retry"},
      {"ErrorType": "NotDialable", "NextAction": "invalid-number-retry"},
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
    ]
  }
}
```

## Related Topics
- SetCallbackNumber
- TransferContactToQueue
- CheckStaffing
- GetParticipantInput

---
**Metadata**
- Category: Pattern
- Keywords: callback, queue callback, SetCallbackNumber, InvalidNumber, NotDialable
