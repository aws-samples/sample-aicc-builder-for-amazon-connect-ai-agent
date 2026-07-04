# MessageParticipant Block

## Question
How do I use the MessageParticipant block to play messages in Amazon Connect Contact Flow?

## Answer
The MessageParticipant block plays text-to-speech (TTS) messages or audio prompts to the customer. It's used for greetings, instructions, and any communication with the caller.

### JSON Structure (TTS)
```json
{
  "Identifier": "greeting",
  "Type": "MessageParticipant",
  "Parameters": {
    "Text": "Welcome to customer service. How can I help you today?"
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "next-block"}
    ]
  }
}
```

### JSON Structure (Audio Prompt)
```json
{
  "Identifier": "play-audio",
  "Type": "MessageParticipant",
  "Parameters": {
    "PromptId": "<PROMPT_ARN>"
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "next-block"}
    ]
  }
}
```
`PromptId` must be a **real, resolvable prompt ARN on the target instance** (format `arn:aws:connect:<region>:<account>:instance/<instance-id>/prompt/<prompt-id>`). A placeholder ARN, or one from a different region/instance, fails at import with `Failed to convert id: <arn>` — this is a resource-resolution error, not a structural one; the `PromptId` parameter name itself is valid.

### Parameters (Choose ONE)
| Parameter | Type | Description |
|-----------|------|-------------|
| Text | String | Plain-text TTS message to speak |
| SSML | String | SSML-markup message to speak |
| PromptId | String | ARN of pre-recorded audio prompt |
| Media | Object | S3 audio file reference |

The valid mutually-exclusive set is **Text | SSML | PromptId | Media** — exactly one is required. Supplying more than one (e.g. Text + PromptId, or Text + SSML) is rejected with `Only one of these properties may be defined`. Supplying none is rejected with `At least one of the following properties must be set. Properties: [Parameters.PromptId, Parameters.Text, Parameters.SSML, Parameters.Media]`.

### Error Types
- **NoMatchingError**: Message playback failed

### CRITICAL Requirements
1. Use exactly ONE of: Text, SSML, PromptId, or Media (not multiple)
2. SHOULD have `Errors` with `NoMatchingError` for robustness (the `Errors` array is optional — a Text-only block with no `Errors` still imports)
3. For TTS, set voice with `UpdateContactTextToSpeechVoice` first
4. All Parameter values must be JSON **strings** — e.g. `"SkipWhenDTMFBufferEnabled": "True"`, not the boolean `true`. A boolean value fails with a misleading `Invalid Action type` error rather than a clear type error. (`SkipWhenDTMFBufferEnabled` is itself a valid optional Parameter accepting the string values `"True"`/`"False"`.)

### SSML Support
SSML markup can be supplied two ways (they are mutually exclusive with each other and with Text):

**1. As the standalone `SSML` parameter** (preferred for full SSML documents):
```json
{
  "Parameters": {
    "SSML": "<speak>Welcome. <break time='500ms'/> Your account balance is <say-as interpret-as='currency'>$123.45</say-as></speak>"
  }
}
```

**2. Embedded as tags inside the `Text` parameter** (also accepted by the API):
```json
{
  "Parameters": {
    "Text": "<speak>Welcome. <break time='500ms'/> Your account balance is <say-as interpret-as='currency'>$123.45</say-as></speak>"
  }
}
```
Note: `SSML` is a first-class parameter — do not set both `Text` and `SSML` in the same block (`Only one of these properties may be defined. Properties: [Text, SSML]`).

### Using Contact Attributes in Text
```json
{
  "Parameters": {
    "Text": "Hello $.Customer.FirstName, thank you for calling."
  }
}
```

### Common Messages

#### Welcome Message
```json
{"Identifier": "welcome", "Type": "MessageParticipant",
 "Parameters": {"Text": "Thank you for calling {{COMPANY_NAME}}. How can I assist you today?"},
 "Transitions": {"NextAction": "ai-bot",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "ai-bot"}]}}
```

#### Transfer Message
```json
{"Identifier": "transfer-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "Please hold while I transfer you to an agent."},
 "Transitions": {"NextAction": "transfer-queue",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "transfer-queue"}]}}
```

#### Goodbye Message
```json
{"Identifier": "goodbye", "Type": "MessageParticipant",
 "Parameters": {"Text": "Thank you for contacting us. Goodbye!"},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}
```

#### Error Message
```json
{"Identifier": "error-message", "Type": "MessageParticipant",
 "Parameters": {"Text": "We're experiencing technical difficulties. Please try again later."},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}
```

#### Queue Full Message
```json
{"Identifier": "queue-full", "Type": "MessageParticipant",
 "Parameters": {"Text": "All agents are currently busy. Please try again later or leave a callback number."},
 "Transitions": {"NextAction": "disconnect",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}}
```

### Voice Setup Pattern
Always set voice before MessageParticipant:
```json
{"Identifier": "set-voice", "Type": "UpdateContactTextToSpeechVoice",
 "Parameters": {"TextToSpeechVoice": "Seoyeon", "TextToSpeechEngine": "Generative"},
 "Transitions": {"NextAction": "welcome",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "welcome"}]}}

{"Identifier": "welcome", "Type": "MessageParticipant",
 "Parameters": {"Text": "안녕하세요, 고객 서비스에 전화해 주셔서 감사합니다."},
 "Transitions": {"NextAction": "main-flow",
   "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "main-flow"}]}}
```

## Related Topics
- UpdateContactTextToSpeechVoice
- GetParticipantInput
- DisconnectParticipant

---
**Metadata**
- Category: Interact
- BlockType: MessageParticipant
- Keywords: message, TTS, text to speech, prompt, audio, greeting
