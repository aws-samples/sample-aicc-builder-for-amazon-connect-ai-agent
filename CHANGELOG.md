# Changelog

This file lists the user-visible changes of each AICC Builder release — what you can build,
what the generated bundle does, and what now works without a hand edit.

## [3.0.0] - 2026-09-15

### Added

**Agentic CX Designer (ACXD) target**

- Pick where your agent runs on the start screen: **Classic** (Lex bot + Amazon Connect AI
  agent + AgentCore Gateway MCP tools) or **ACXD** (an Agentic CX Designer application behind
  the Agentic CX block, on a Connect Customer instance). The interview, the review and the
  six-asset bundle are the same for both; only the last mile differs.
- A complete ACXD application is generated from your interview — conversation flows, slot
  types, data requests, guardrails, knowledge bases, context variables and secrets — delivered
  under `assets/acxd/` next to the Lambda, OpenAPI, CloudFormation, FAQ and Contact Flow
  assets. The AI Prompt asset is replaced by the application.
- Every operation gets a flow plan you confirm step by step during the interview, with
  escalation rules and redirect targets named before anything is generated. Operation specs
  stay editable afterwards, so a wrong field is corrected at the source and the assets
  regenerated from it.
- Welcome, Fallback, FollowUp, RequestAgent and Escalation flows are built deterministically
  from a contract verified against the live service: greet, listen, hand the utterance to the
  recognised flow, ask "anything else?" after each answer, re-ask what was not understood, and
  hand off to a human on the third miss or an explicit request for an agent.
- The greeting is the one your customer approved; when the interview recorded none, the
  assistant introduces itself by naming the operations it handles.
- Operations a customer never asks for directly can be marked internal: they stay out of the
  greeting and re-guide menus and are generated untrained, so they are reachable only by
  redirect from another flow.
- Data requests call your generated API Gateway directly with the API key held as an ACXD
  Secret, in both the development and production environments, and post the slots the reaching
  path collected. On the ACXD path every non-OPTIONS API Gateway method requires that key, so
  the backend is not left publicly callable.
- Workspace-level resources are named per project (`<project>BackendApiKey`,
  `<project>-<guardrail>`), so several projects can share one ACXD workspace without
  overwriting each other's key and rules.
- The generated Contact Flow carries the real Agentic CX block
  (`ConnectParticipantWithAgenticCX`) with its Default, Escalation, Error and idle
  chat-timeout branches wired, and sets the contact language before it, so the caller reaches
  your application on the first import.
- Generated Python handlers in an ACXD bundle are wrapped with a boundary adapter that
  restores the formats the runtime strips and normalises the reply to the shared envelope.
- The bundled `./deploy.sh` recognises an ACXD bundle with no flags and runs the whole chain:
  CloudFormation, Lambda, API, the ACXD resources pointed at the deployed endpoint, build,
  deployment, and the published Contact Flow with the real workspace and application ids.
  Re-runs are idempotent, FAQ documents are uploaded into the Q in Connect knowledge base as
  content, the manifest records which build produced the bundle, and `status`, `cleanup`,
  `--dry-run`, `--rebind-alias` and `ACXD_ALIAS_ID` are included.

**Validation gates**

- ACXD consistency gates check the application against the rest of the bundle before you can
  download it: data request paths, fields, formats and webhook URLs against the OpenAPI spec
  and the Lambdas, and every flow against the runtime contract the service enforces but does
  not document.
- The blocking set is computed in code from the deterministic gates and has stable finding
  ids, so a re-review states what was fixed and what is new. The reviewer agent's findings are
  returned as advisory.
- The interview refuses to hand off while generation would have to guess: missing output
  types, enum values, array and object item types, slots that name no real input field,
  escalation conditions and the payload handed to an agent are reported as exact gaps to ask
  about.
- A spec operation with no Lambda, no OpenAPI operation or no ACXD data request blocks
  packaging and names the generator to run; an asset with no spec behind it blocks as well.
  Each Lambda is also checked against its spec as it is written, so a handler that ignores a
  field is fixed before the next asset.
- Sample records supplied in a requirements document are stored verbatim and gated: a template
  that drops or alters a supplied value blocks.
- Turns that narrate a tool result no tool call produced are flagged with a visible
  verification notice.

**Deterministic repairs**

- Blocking findings are repaired in code rather than by another model attempt: the OpenAPI spec
  is re-projected from the operation specs, slot types and data requests are re-derived from
  the confirmed plans, duplicate copies of an asset are collapsed, unreachable flow nodes are
  pruned, and each repair re-runs the gate and reports per file what actually changed.
- A flow canonicalizer maps every shape a model produces onto the SDK's own contract before
  validation, patching and packaging — and on bundle load, so bundles generated by an earlier
  build deploy too.

**Web app**

- ACXD flow diagrams read like flows: each node card shows what it does (message text, the
  slot it collects, the data request it calls, branch names, target flow, variable assignment)
  instead of a UUID, the canvas fills its pane, and a flow can be opened fullscreen or
  previewed in the chat with a JSON tab, copy and download.
- The progress panel lists the ACXD application as a downloadable asset, and a refused
  download shows why and which problems block it.

**Docs**

- New guides: ACXD bundle contents and deploy phases, what was verified in a real Connect
  Customer account, the ACXD design, and the agentic-AI methodology behind requirement
  fidelity. README's "What's New" is available in English, Korean and Japanese.

### Changed

**Review & generation**

- The review separates blocking findings (deterministic, must be zero to package) from
  recommendations, and counts findings rather than the symbols in its own report.
- One flow per system role: a duplicate welcome, fallback or escalation plan is refused, and a
  plan can be removed.
- Node types are normalised to the runtime's own vocabulary, and correcting a node type inside
  the same determinism class no longer re-opens a confirmation the customer already gave.
- The interview only asks the caller for values the caller knows — amounts, prices, statuses
  and generated ids are output fields, not slots — and spoken result labels are short, derived
  from the field description, and stay in the caller's language.
- Guardrails are safer by default: a sample value in a rule becomes a class of the same width
  instead of matching only itself, replacement messages follow the project language, and a
  derived output rule that would rewrite what the caller hears is kept as a flag.
- Generated AI prompts always state when to search the FAQ knowledge base, so a policy
  question the knowledge base answers is not escalated.
- Each asset file has exactly one home in the workspace and Contact Flows are de-duplicated by
  name, so a bundle can no longer ship two flows. Bundle load also drops slot types,
  guardrails and secrets nothing references, and re-applies the linter and the Agentic CX
  binding to the Contact Flow.
- In an ACXD project the application speaks and the Contact Flow stays silent: greetings before
  the block and messages on the Default branch are removed, and a pre-block prompt is a
  blocking finding.
- Classic projects benefit from the same contract work: OpenAPI request and response schemas
  are projected from the spec with one shared envelope, every CORS header an integration response
  maps is declared, and the cross-asset gate reads secondary indexes and the environment
  contract from the CloudFormation template as well as the schema registry.

**Session & reliability**

- A turn in flight is no longer interrupted by scaling: running turns hold task scale-in
  protection, scaling policies no longer fight each other, and a fresh task takes traffic only
  once its shared storage is visible.
- A reply that finished while your tab was away is replayed when you reopen the session, a
  reconnect during a running turn resumes cleanly, and the sidebar message count follows the
  live conversation.
- A restart during generation no longer pushes a session back to the interview phase, the
  interview-to-generation boundary runs exactly once per session, and a turn cut off by a
  restart is reported as such so the next turn verifies state instead of assuming its tool
  calls ran.
- Concurrent interview tool calls in one turn can no longer clobber each other's spec updates.

### Fixed

**Deploy**

- Application languages are re-applied after creation, so a non-English application builds in
  its own language and contacts reach it instead of failing to start. Secrets, deployment
  promotion, Lambda names and the region are resolved the way the service actually expects, so
  a first-time deploy no longer stops on an internal error.
- The backend API key is resolved on every entry path (environment, the project's secret, the
  stack output), so data requests are no longer refused with 403, and flow-invoked Lambdas are
  associated with Connect on the ACXD path as well.
- A flow placeholder naming a function the stack does not contain is skipped and its block
  removed, instead of being bound to an unrelated Lambda; each deploy run uses its own scratch
  directory, so two runs on one machine cannot swap handler code.
- An AI Prompt with an unknown variable is rejected before deploy, and a failed AI Prompt fails
  the deploy instead of reporting success.
- A re-run keeps the application alias already bound to the flow and finds the previously
  published flow by name when the bundle carries no state; a Contact Flow that Connect refuses
  leaves the resolved flow next to the bundle so you can see what was rejected.

**What the caller experiences**

- Time slots collected by an ACXD flow reach the backend as `HH:MM` even though the service
  delivers them as compact digits; dates and phone numbers likewise arrive with their
  separators restored.
- Date, time and identifier fields use slot types the runtime can actually recognise, so a
  spoken date is captured instead of being retried into the fallback. Slot types are per field
  rather than shared by a generic type name, and an open value attaches a built-in slot type
  with the field's own pattern instead of a one-item menu the runtime selects on the caller's
  behalf.
- A yes/no answer is compared against the slot type's own values, so consent is read as
  consent rather than a refusal.
- Branch conditions name fields the data request really returns, and a constant a field's enum
  can never hold is repaired or reported instead of leaving a branch permanently dead.
- A not-found or refused outcome is told to the caller: the reply schema requires only the
  shared envelope, reply values are coerced to the declared types, and a missing success flag
  is derived from the response.
- A data request sends only the slots the reaching path has captured, so a slot the caller was
  never asked for cannot fail the call, and a failure or timeout branch always lands somewhere
  that speaks or escalates.
- A message node only sends its message, and a result step with nothing to template announces
  the fields the upstream data request returned instead of going quiet — with no second
  announcement after the answer was already given.
- An answer to "anything else?" that names another operation is routed there; only a genuinely
  unrecognised answer becomes a fallback.
- Length phrases such as "10 alphanumeric characters", including their CJK equivalents,
  produce a usable pattern instead of an unmatchable mask, and an application built for a
  company with a CJK name deploys under its own name.

**Web app & backend**

- Downloads are validated against the session that owns them, so a valid bundle is no longer
  refused, and a refusal returns a readable problem list.
- ACXD assets reappear in the Asset Workspace when a session is restored, instead of being
  dropped and then saved back as missing.
- Choosing ACXD on the start screen applies to the session you are about to run, and the
  sidebar follows the session that Start actually opens.
- Shared storage is mounted into the application container and created at startup, so a new
  task becomes healthy and sessions persist as intended.

### Known limitations

- **Alias key rotation.** When a redeploy has to replace the application deployment, the
  Agentic CX block's alias key changes. The deploy script reports the new key, and
  `./deploy.sh --rebind-alias <key>` re-points the published Contact Flow without opening the
  console.
- **Generative model on the workspace.** Knowledge-base and generative nodes answer nothing
  unless a generative model is configured on the ACXD workspace. Configure it before the first
  contact.
- **ASCII routing descriptions.** The routing descriptions ACXD uses to match an utterance to
  a flow are ASCII-only, so two Korean requests worded very similarly can occasionally route
  to a neighbouring flow.
- **Channel coverage.** The ACXD target has been verified over Amazon Connect chat. Voice was
  not exercised in this release.

Earlier releases are summarised in the README's "What's New" sections.
