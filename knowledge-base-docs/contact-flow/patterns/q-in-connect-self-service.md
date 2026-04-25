# Q in Connect Self-Service Pattern

## Question
How do I implement a Q in Connect self-service pattern in Amazon Connect Contact Flow?

## Answer
This pattern implements AI-powered self-service using Amazon Q in Connect (formerly Amazon Connect Wisdom) with Lex V2 integration. The customer interacts with an AI agent for self-service, with escalation to live agents when needed.

## Required Block Sequence

1. **UpdateFlowLoggingBehavior** - Enable logging (REQUIRED)
2. **CreateWisdomSession** - Create Connect Assistant session (REQUIRED)
3. **UpdateContactData** - Set Wisdom session ARN on contact (REQUIRED, paired with CreateWisdomSession)
4. **Compare** - Check channel (VOICE vs CHAT)
5. **UpdateContactRecordingBehavior** - Enable recording and Contact Lens
6. **GetCustomerProfile** - Lookup customer
7. **AssociateContactToCustomerProfile** - Link contact to profile
8. **UpdateContactAttributes** - Store customer info
9. **InvokeLambdaFunction** - Update Q session with customer data
10. **UpdateContactTextToSpeechVoice** - Set TTS voice
11. **ConnectParticipantWithLexBot** - Q in Connect AI interaction
12. **Compare** - Check Lex result for escalation
13. **UpdateContactAttributes** - Set context for agent
14. **TransferContactToQueue** - Escalate to agent
15. **DisconnectParticipant** - End contact

## Complete JSON Implementation

```json
{
  "Version": "2019-10-30",
  "StartAction": "enable-logging",
  "Metadata": {
    "entryPointPosition": {"x": 40, "y": 40},
    "ActionMetadata": {
      "enable-logging": {"position": {"x": 280, "y": 40}, "isFriendlyName": true},
      "create-assistant-session": {
        "position": {"x": 280, "y": 170},
        "isFriendlyName": true,
        "children": ["update-contact-data"],
        "parameters": {"WisdomAssistantArn": {"displayName": ""}},
        "fragments": {"SetContactData": "update-contact-data"}
      },
      "update-contact-data": {"position": {"x": 280, "y": 170}, "dynamicParams": []},
      "check-channel": {"position": {"x": 280, "y": 300}, "isFriendlyName": true},
      "voice-recording": {"position": {"x": 0, "y": 560}, "isFriendlyName": true},
      "chat-recording": {"position": {"x": 560, "y": 560}, "isFriendlyName": true},
      "get-profile": {"position": {"x": 280, "y": 820}, "isFriendlyName": true},
      "associate-profile": {"position": {"x": 280, "y": 1080}, "isFriendlyName": true},
      "update-attrs": {"position": {"x": 280, "y": 1340}, "isFriendlyName": true},
      "update-q-session": {"position": {"x": 280, "y": 1600}, "isFriendlyName": true},
      "set-voice": {"position": {"x": 280, "y": 1860}, "isFriendlyName": true},
      "lex-bot": {"position": {"x": 280, "y": 2120}, "isFriendlyName": true},
      "check-result": {"position": {"x": 280, "y": 2380}, "isFriendlyName": true},
      "set-context": {"position": {"x": 0, "y": 2640}, "isFriendlyName": true},
      "transfer-message": {"position": {"x": 0, "y": 2900}, "isFriendlyName": true},
      "transfer-queue": {"position": {"x": 0, "y": 3160}, "isFriendlyName": true},
      "goodbye": {"position": {"x": 560, "y": 2640}, "isFriendlyName": true},
      "error-handler": {"position": {"x": 840, "y": 1600}, "isFriendlyName": true},
      "disconnect": {"position": {"x": 280, "y": 3420}, "isFriendlyName": true}
    },
    "name": "Q in Connect Self-Service Flow",
    "type": "contactFlow",
    "status": "DRAFT",
    "hash": {}
  },
  "Actions": [
    {
      "Identifier": "enable-logging",
      "Type": "UpdateFlowLoggingBehavior",
      "Parameters": {"FlowLoggingBehavior": "Enabled"},
      "Transitions": {"NextAction": "create-assistant-session"}
    },
    {
      "Identifier": "create-assistant-session",
      "Type": "CreateWisdomSession",
      "Parameters": {"WisdomAssistantArn": "{{WISDOM_ASSISTANT_ARN}}"},
      "Transitions": {
        "NextAction": "update-contact-data",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "check-channel"}]
      }
    },
    {
      "Identifier": "update-contact-data",
      "Type": "UpdateContactData",
      "Parameters": {"WisdomSessionArn": "$.Wisdom.SessionArn"},
      "Transitions": {
        "NextAction": "check-channel",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "check-channel"}]
      }
    },
    {
      "Identifier": "check-channel",
      "Type": "Compare",
      "Parameters": {"ComparisonValue": "$.Channel"},
      "Transitions": {
        "NextAction": "chat-recording",
        "Conditions": [
          {"Condition": {"Operator": "Equals", "Operands": ["VOICE"]}, "NextAction": "voice-recording"}
        ],
        "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "chat-recording"}]
      }
    },
    {
      "Identifier": "voice-recording",
      "Type": "UpdateContactRecordingBehavior",
      "Parameters": {
        "RecordingBehavior": {"RecordedParticipants": ["Agent", "Customer"]},
        "AnalyticsBehavior": {"Enabled": "True", "AnalyticsMode": "RealTime"}
      },
      "Transitions": {"NextAction": "get-profile"}
    },
    {
      "Identifier": "chat-recording",
      "Type": "UpdateContactRecordingBehavior",
      "Parameters": {
        "RecordingBehavior": {"RecordedParticipants": []},
        "AnalyticsBehavior": {"Enabled": "True", "AnalyticsMode": "PostContact"}
      },
      "Transitions": {"NextAction": "get-profile"}
    },
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
          {"ErrorType": "MultipleFoundError", "NextAction": "update-attrs"},
          {"ErrorType": "NoneFoundError", "NextAction": "update-attrs"},
          {"ErrorType": "NoMatchingError", "NextAction": "update-attrs"}
        ]
      }
    },
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
        "NextAction": "update-attrs",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "update-attrs"}]
      }
    },
    {
      "Identifier": "update-attrs",
      "Type": "UpdateContactAttributes",
      "Parameters": {
        "Attributes": {
          "customerFirstName": "$.Customer.FirstName",
          "customerLastName": "$.Customer.LastName",
          "profileId": "$.Customer.ProfileId"
        }
      },
      "Transitions": {
        "NextAction": "update-q-session",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "update-q-session"}]
      }
    },
    {
      "Identifier": "update-q-session",
      "Type": "InvokeLambdaFunction",
      "Parameters": {
        "LambdaFunctionARN": "{{UPDATE_Q_SESSION_LAMBDA}}",
        "InvocationTimeLimitSeconds": "8",
        "ResponseValidation": {"ResponseType": "STRING_MAP"},
        "LambdaInvocationAttributes": {
          "firstName": "$.Customer.FirstName",
          "lastName": "$.Customer.LastName",
          "phoneNumber": "$.CustomerEndpoint.Address"
        }
      },
      "Transitions": {
        "NextAction": "set-voice",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "set-voice"}]
      }
    },
    {
      "Identifier": "set-voice",
      "Type": "UpdateContactTextToSpeechVoice",
      "Parameters": {
        "TextToSpeechVoice": "Seoyeon",
        "TextToSpeechEngine": "Generative"
      },
      "Transitions": {
        "NextAction": "lex-bot",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "lex-bot"}]
      }
    },
    {
      "Identifier": "lex-bot",
      "Type": "ConnectParticipantWithLexBot",
      "Parameters": {
        "Text": "{{WELCOME_MESSAGE}}",
        "LexV2Bot": {"AliasArn": "{{LEX_BOT_ALIAS_ARN}}"}
      },
      "Transitions": {
        "NextAction": "check-result",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]
      }
    },
    {
      "Identifier": "check-result",
      "Type": "Compare",
      "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.toolResult"},
      "Transitions": {
        "NextAction": "lex-bot",
        "Conditions": [
          {"Condition": {"Operator": "Equals", "Operands": ["ESCALATION"]}, "NextAction": "set-context"},
          {"Condition": {"Operator": "Equals", "Operands": ["COMPLETE"]}, "NextAction": "goodbye"}
        ],
        "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "lex-bot"}]
      }
    },
    {
      "Identifier": "set-context",
      "Type": "UpdateContactAttributes",
      "Parameters": {
        "Attributes": {
          "customerIntent": "$.Lex.SessionAttributes.customerIntent",
          "escalationReason": "$.Lex.SessionAttributes.escalationReason",
          "conversationSummary": "$.Lex.SessionAttributes.conversationSummary"
        }
      },
      "Transitions": {
        "NextAction": "transfer-message",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "transfer-message"}]
      }
    },
    {
      "Identifier": "transfer-message",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "Please hold while I transfer you to an agent."},
      "Transitions": {
        "NextAction": "transfer-queue",
        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "transfer-queue"}]
      }
    },
    {
      "Identifier": "transfer-queue",
      "Type": "TransferContactToQueue",
      "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
      "Transitions": {
        "NextAction": "disconnect",
        "Errors": [
          {"ErrorType": "QueueAtCapacity", "NextAction": "goodbye"},
          {"ErrorType": "NoMatchingError", "NextAction": "error-handler"}
        ]
      }
    },
    {
      "Identifier": "goodbye",
      "Type": "MessageParticipant",
      "Parameters": {"Text": "Thank you for using our service. Goodbye!"},
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

## Key Implementation Notes

### Voice vs Chat Differences
| Feature | VOICE | CHAT |
|---------|-------|------|
| Recording | Agent + Customer | None |
| Contact Lens | RealTime | PostContact |
| Profile Lookup | _phone | _email |

### Lex Session Attributes for Escalation
The Lex bot should set these attributes:
- `toolResult`: "ESCALATION", "COMPLETE", or "CONTINUE"
- `customerIntent`: What the customer wanted
- `escalationReason`: Why escalating to agent
- `conversationSummary`: Summary for agent

### Required Placeholders
- `{{WISDOM_ASSISTANT_ARN}}`: Connect Assistant (Wisdom) domain ARN
- `{{LEX_BOT_ALIAS_ARN}}`: Q in Connect Lex bot alias ARN
- `{{QUEUE_ARN}}`: Target queue for escalation
- `{{UPDATE_Q_SESSION_LAMBDA}}`: Lambda to update Q session data
- `{{WELCOME_MESSAGE}}`: Welcome message for customers

## Related Topics
- UpdateContactRecordingBehavior
- GetCustomerProfile
- ConnectParticipantWithLexBot
- Compare Block

---
**Metadata**
- Category: Pattern
- Keywords: Q in Connect, self-service, Lex, AI, escalation, voice, chat
