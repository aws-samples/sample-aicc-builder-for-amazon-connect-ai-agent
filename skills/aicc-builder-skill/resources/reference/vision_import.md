# Vision Import — flow-diagram image → Contact Flow JSON

> **Authored skill resource** (NOT auto-extracted from the backend). This is the
> CLI-native port of `backend/ecs/src/agents/contact_flow_generator/vision_import.py`
> (`VISION_IMPORT_SYSTEM_PROMPT` + `draft_flow_from_image`). The webapp stashes the
> image bytes across turns; in a CLI you already have the file path, so you just
> `Read` the image directly when the user confirms.

Use this when the user provides an **image** of a contact flow — a whiteboard
sketch, a hand-drawn diagram, a screenshot of another tool, or a photo of a flow
chart — and wants it turned into an importable Amazon Connect Contact Flow.

## Workflow (CLI)

1. **Acknowledge + describe, then ASK before converting.** Never silently
   transcribe. e.g. (in the user's language): "이 다이어그램을 Amazon Connect
   Contact Flow로 만들어 드릴까요?" / "Want me to turn this sketch into an
   importable Contact Flow?"
2. **On confirmation, `Read` the image file.** Claude Code / Kiro read PNG, JPEG,
   GIF, and WebP natively. Tell the user you're reading the diagram first.
3. **Adopt the transcription contract below** as your active instruction and emit
   the flow JSON.
4. **Lint + repair** the draft with the deterministic linter before trusting it —
   the draft is best-effort and MUST pass import-safety:
   ```bash
   python3 resources/scripts/lint_assets.py <output_dir>          # report
   python3 resources/scripts/lint_assets.py <output_dir> --fix     # apply fixes
   ```
   `lint_assets.py` carries the same `lint_contact_flow` the webapp runs (it is
   generated from `backend/ecs/src/tools/asset_linters.py`), so it renames invalid
   `Type`s, adds required error branches, and strips `Transitions` from terminal
   blocks. See
   [`resources/reference/contact_flow_block_schemas.md`](contact_flow_block_schemas.md)
   for what each rule means. The repaired JSON may be **shorter** than the draft
   (the linter strips invalid `DTMFConfiguration`, duplicate SSML, etc.) — that is
   expected and correct.
5. **Seed it as an imported asset** under a stable id and enter patch-only mode:
   `assets/v1/contact_flow/imported_flow/contact_flow.json`. Subsequent edits use
   `Edit` (or re-call `contact_flow_generator` with `flow_name="imported_flow"` +
   a `modification_request`) — **never regenerate from scratch.**
6. **Print a one-line import summary**: `{errors, warnings, fixesApplied}`.

## Transcription contract (give this to yourself before emitting JSON)

> You are an Amazon Connect Contact Flow architect. Read the diagram and produce a
> best-effort **Amazon Connect Contact Flow JSON** that captures the logic shown
> (greeting, menus/DTMF, conditions, transfers, disconnects, etc.).
>
> **OUTPUT CONTRACT — follow exactly:**
> - Output ONLY the JSON object. No prose, no markdown fences, no explanation.
> - Top-level keys: `"Version"` (always `"2019-10-30"`), `"StartAction"`, `"Actions"`.
> - Each action: `"Identifier"` (unique kebab-case string), `"Type"`, `"Parameters"`,
>   `"Transitions"` (`{"NextAction": ..., "Errors": [...], "Conditions": [...]}`).
> - Use ONLY these block `Type`s (anything else is invalid):
>   `AssociateContactToCustomerProfile, AuthenticateParticipant, CheckHoursOfOperation,
>   CheckMetricData, CheckOutboundCallStatus, Compare, ConnectParticipantWithLexBot,
>   CreateCase, CreateContact, CreatePersistentContactAssociation, CreateTask,
>   CreateWisdomSession, DequeueContactAndTransferToQueue, DisconnectParticipant,
>   DistributeByPercentage, EndFlowExecution, EvaluateDataTableValues,
>   GetCustomerProfile, GetCustomerProfileObject, GetMetricData, GetParticipantInput,
>   InvokeFlowModule, InvokeLambdaFunction, LoadContactContent, Loop,
>   MessageParticipant, MessageParticipantIteratively, RenderMessageTemplate,
>   ResumeContact, ShowView, StartOutboundEmailContact, TagContact,
>   TransferContactToQueue, TransferParticipantToThirdParty, TransferToFlow,
>   UntagContact, UpdateContactAttributes, UpdateContactCallbackNumber,
>   UpdateContactData, UpdateContactEventHooks,
>   UpdateContactMediaStreamingBehavior,
>   UpdateContactRecordingAndAnalyticsBehavior, UpdateContactRecordingBehavior,
>   UpdateContactRoutingBehavior, UpdateContactRoutingCriteria,
>   UpdateContactTargetQueue, UpdateContactTextToSpeechVoice,
>   UpdateFlowAttributes, UpdateFlowLoggingBehavior,
>   UpdatePreviousContactParticipantState, Wait`
> - Map common diagram intent to blocks: greeting/announcement → `MessageParticipant`;
>   menu/press-a-digit → `GetParticipantInput`; if/branch/condition → `Compare`;
>   business hours → `CheckHoursOfOperation`; "transfer to \<queue\>" → set the queue
>   with `UpdateContactTargetQueue` then `TransferContactToQueue` (the transfer block
>   takes NO queue parameter); hang up/end → `DisconnectParticipant`.
> - For anything ambiguous in the drawing, choose a reasonable default and keep the
>   flow structurally valid (every `NextAction` points to a real `Identifier`;
>   include a terminal `DisconnectParticipant`). It's fine to be approximate — the
>   user refines it afterward.
> - Use `{{PLACEHOLDER}}` tokens (e.g. `{{FRONT_DESK_QUEUE_ARN}}`, `{{LAMBDA_ARN}}`)
>   for any ARN/id the diagram references but doesn't provide.

The webapp builds that `Type` list at import time from
`VALID_CONTACT_FLOW_ACTION_TYPES`, so the copy above can go stale. Print the
authoritative list instead of trusting it:

```bash
python3 -c "import importlib.util as u; s=u.spec_from_file_location('la','resources/scripts/lint_assets.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(', '.join(sorted(m.VALID_CONTACT_FLOW_ACTION_TYPES)))"
```

If the user supplied a company name or a note about the diagram, pass both as
context alongside the image — the webapp prefixes the transcription request with
`Company: <name>.` and `User note: <hint>.`, and they measurably improve queue/prompt
naming.

See [`contact_flow_block_schemas.md`](contact_flow_block_schemas.md) for the
per-block required parameters and error lists the linter enforces.
