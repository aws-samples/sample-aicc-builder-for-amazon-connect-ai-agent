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
    "PromptId": "arn:aws:connect:us-east-1:123456789012:instance/xxx/prompt/yyy"
  },
  "Transitions": {
    "NextAction": "next-block",
    "Errors": [
      {"ErrorType": "NoMatchingError", "NextAction": "next-block"}
    ]
  }
}
```

### Parameters (Choose ONE)
| Parameter | Type | Description |
|-----------|------|-------------|
| Text | String | TTS message to speak |
| PromptId | String | ARN of pre-recorded audio prompt |
| Media | Object | S3 audio file reference |

### Error Types
- **NoMatchingError**: Message playback failed

### CRITICAL Requirements
1. Use ONE of: Text, PromptId, or Media (not multiple)
2. SHOULD have `Errors` with `NoMatchingError` for robustness
3. For TTS, set voice with `UpdateContactTextToSpeechVoice` first

### SSML Support in Text
You can use SSML tags for advanced TTS control:
```json
{
  "Parameters": {
    "Text": "<speak>Welcome. <break time='500ms'/> Your account balance is <say-as interpret-as='currency'>$123.45</say-as></speak>"
  }
}
```

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
