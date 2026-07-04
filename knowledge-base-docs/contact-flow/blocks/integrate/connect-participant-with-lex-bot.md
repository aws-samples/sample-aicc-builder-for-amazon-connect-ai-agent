# ConnectParticipantWithLexBot Block

## Question
How do I use the ConnectParticipantWithLexBot block for AI interactions in Amazon Connect Contact Flow?

## Answer
The ConnectParticipantWithLexBot block connects the contact to a Lex V2 bot for natural language processing. This is used for Q in Connect AI agents and conversational IVR.

### JSON Structure
```json
{
  "Identifier": "lex-bot",
  "Type": "ConnectParticipantWithLexBot",
  "Parameters": {
    "Text": "Hello! How can I help you today?",
    "LexV2Bot": {
      "AliasArn": "{{LEX_BOT_ALIAS_ARN}}"
    }
  },
  "Transitions": {
    "NextAction": "check-result",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "error-handler"},
      {"ErrorType": "NoMatchingCondition", "NextAction": "error-handler"}
    ]
  }
}
```

### Required Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| Text / SSML / PromptId / Media / LexInitializationData | one of | Initial message. Provide exactly ONE of these |
| LexV2Bot.AliasArn *(recommended)* | String | ARN of the Lex V2 bot alias |
| LexBot *(legacy, still supported)* | Object | Lex V1 — `{Name, Region, Alias}` |

The initial-message set is mutually exclusive: supplying `Text` together with
`LexInitializationData` is rejected (`Only one of these properties may be defined.
Properties: [Text, LexInitializationData]`). Use `LexInitializationData.InitialMessage`
**instead of** `Text`, not alongside it. `LexInitializationData` alone (no `Text`) imports.

### Optional Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| LexSessionAttributes | Object (string→string) | Session attributes passed to Lex |
| LexTimeoutSeconds.Text | Number | Chat-only: timer for inactive customer (see caveat below) |

> **LexTimeoutSeconds caveat (unverified):** On a plain `CONTACT_FLOW` import, every literal
> value form tested for `LexTimeoutSeconds.Text` (number `10`, string `"10"`, ISO-8601 `"PT10S"`)
> is rejected with `Invalid Action property value. Path: Actions[0].Parameters.LexTimeoutSeconds.Text`.
> The property *name* is accepted, but no literal value validated on a standard contact flow —
> the accepted value format may be channel-specific (chat) or require an attribute JSONPath rather
> than a literal. Confirm the exact accepted format before relying on it.

### Error Types
- **NoMatchingError**: Bot invocation failed (invalid ARN, permissions, bot error)
- **NoMatchingCondition**: Conditions were declared but none matched the bot Intent result
- **InputTimeLimitExceeded**: No response before `LexTimeoutSeconds.Text` elapsed (chat)

### CRITICAL Requirements
1. **Use Lex V2 via `LexV2Bot.AliasArn` for new flows**. Lex V1 (`LexBot` object) is still
   supported by the action but is legacy — prefer V2.
2. Provide exactly ONE of `Text` | `SSML` | `PromptId` | `Media` | `LexInitializationData`
   (not multiple).
3. MUST include BOTH `NoMatchingError` and `NoMatchingCondition` in `Errors` — both are
   unconditionally required error types (omitting either fails import). Add
   `InputTimeLimitExceeded` only when using `LexTimeoutSeconds`.
4. For voice, set up `UpdateContactTextToSpeechVoice` BEFORE this block.
5. For voice, set `UpdateContactRecordingBehavior` with Voice `AnalyticsModes: ["RealTime"]`
   (preferred for AI-agent / Q in Connect flows — live transcript + sentiment give the
   assistant and any escalated human real-time context). Voice `AnalyticsModes` takes
   EXACTLY ONE mode: `["RealTime"]` OR `["PostContact"]`, never both (the combined array
   fails import). `RealTime` requires real-time Contact Lens enabled on the instance
   (enforced at runtime, not at import); if it is not provisioned, use `["PostContact"]`.

### Lex V2 Bot Alias ARN Format
```
arn:aws:lex:us-east-1:123456789012:bot-alias/BOT_ID/ALIAS_ID

# Example
arn:aws:lex:us-east-1:123456789012:bot-alias/ABCD1234/TSTALIASID

# Placeholder for generated flows
{{LEX_BOT_ALIAS_ARN}}
```

### Accessing Lex Results
After the Lex interaction, these attributes are available:

| Attribute | JSONPath | Description |
|-----------|----------|-------------|
| Intent Name | $.Lex.IntentName | The matched intent |
| Session Attributes | $.Lex.SessionAttributes.{key} | Bot session attributes |
| Slot Values | $.Lex.Slots.{name} | Collected slot values |

### Common Pattern: Q in Connect AI Loop
```json
{"Identifier": "lex-bot", "Type": "ConnectParticipantWithLexBot",
 "Parameters": {
   "Text": "{{WELCOME_MESSAGE}}",
   "LexV2Bot": {"AliasArn": "{{LEX_BOT_ALIAS_ARN}}"}
 },
 "Transitions": {
   "NextAction": "check-result",
   "Errors": [
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "error-handler"}
   ]
 }}

{"Identifier": "check-result", "Type": "Compare",
 "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.toolResult"},
 "Transitions": {
   "NextAction": "lex-bot",
   "Conditions": [
     {"Condition": {"Operator": "Equals", "Operands": ["ESCALATION"]}, "NextAction": "transfer"},
     {"Condition": {"Operator": "Equals", "Operands": ["COMPLETE"]}, "NextAction": "goodbye"}
   ],
   "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "lex-bot"}]
 }}
```

### Voice Setup (Required for Q in Connect)
```json
{"Identifier": "set-voice", "Type": "UpdateContactTextToSpeechVoice",
 "Parameters": {"TextToSpeechVoice": "Seoyeon", "TextToSpeechEngine": "Generative"},
 "Transitions": {"NextAction": "set-recording"}}

{"Identifier": "set-recording", "Type": "UpdateContactRecordingBehavior",
 "Parameters": {
   "RecordingBehavior": {"RecordedParticipants": ["Agent", "Customer"], "IVRRecordingBehavior": "Enabled"},
   "AnalyticsBehavior": {"Enabled": "True", "AnalyticsLanguage": "en-US",
     "ChannelConfiguration": {"Chat": {"AnalyticsModes": []}, "Voice": {"AnalyticsModes": ["RealTime"]}}}
 },
 "Transitions": {"NextAction": "lex-bot"}}

{"Identifier": "lex-bot", "Type": "ConnectParticipantWithLexBot",
 "Parameters": {
   "Text": "Hello! How can I help you today?",
   "LexV2Bot": {"AliasArn": "{{LEX_BOT_ALIAS_ARN}}"}
 },
 "Transitions": {
   "NextAction": "check-result",
   "Errors": [
     {"ErrorType": "NoMatchingError", "NextAction": "error-handler"},
     {"ErrorType": "NoMatchingCondition", "NextAction": "error-handler"}
   ]
 }}
```

### Passing Attributes to Lex
Use `UpdateContactAttributes` before Lex to pass context:

```json
{"Identifier": "set-context", "Type": "UpdateContactAttributes",
 "Parameters": {
   "Attributes": {
     "customerName": "$.Customer.FirstName",
     "customerId": "$.Customer.ProfileId"
   }
 },
 "Transitions": {"NextAction": "lex-bot"}}
```

The Lex bot can access these via the session event.

### Language-Specific TTS Voices
| Language | Voice | Engine |
|----------|-------|--------|
| ko-KR | Seoyeon | Generative |
| en-US | Matthew, Joanna | Generative |
| en-GB | Amy, Brian | Generative |
| ja-JP | Takumi, Kazuha | Generative |
| zh-CN | Zhiyu | Generative |

### Amazon Q in Connect Session (separate action)
Amazon Q in Connect is attached to the contact via the `CreateWisdomSession`
flow action (NOT an "InvokeAmazonQConnect" action — that does not exist as a
Flow Language action type). Place `CreateWisdomSession` early in the flow, then
use `ConnectParticipantWithLexBot` with a Q-in-Connect-enabled Lex V2 bot.

```json
{"Identifier": "create-q-session", "Type": "CreateWisdomSession",
 "Parameters": {"WisdomAssistantArn": "{{Q_ASSISTANT_ARN}}"},
 "Transitions": {"NextAction": "lex-bot",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "error-handler"}]}}
```

## Related Topics
- UpdateContactTextToSpeechVoice
- UpdateContactRecordingBehavior
- Compare Block

---
**Metadata**
- Category: Integrate
- BlockType: ConnectParticipantWithLexBot
- Keywords: Lex, bot, AI, Q in Connect, LexV2Bot, natural language, voice
