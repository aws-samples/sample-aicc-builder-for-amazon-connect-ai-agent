# ACXD runtime target — live validation log (2026-09-10, extended 2026-09-13 and 2026-09-14)

This log records what the first real deployments of generated ACXD bundles
taught us. Every item below was found by deploying a bundle that had already
passed the static gates (schema, D1–D9), then fixed at the source so that any
customer's bundle deploys one-shot through the bundled `deploy.sh`.

## Environment

- Builder: `acxd-target` branch on the dev stage (`./deploy.sh --stage dev`).
- Target: a Connect Customer instance + Agentic CX Designer workspace in a
  separate us-east-1 account, driven with a programmatic API key through the
  bundled runner (`runner.js deploy --manifest deploy-manifest.json`).
- Backend CloudFormation, Connect contact-flow import and the Agentic CX block
  wiring need AWS credentials for that account; they were exercised with a
  reachable placeholder webhook and are covered by the Classic deploy phases.

## Real deployments

| # | Bundle | Outcome |
|---|--------|---------|
| 1 | Hanbit Electronics (ko) | 6 flows, 3 data requests, 3 slot types, 2 context variables, KB (9 articles, published), 3 guardrails, application built and deployed to `development` — after the contract repairs below were applied to the bundle |
| 2 | Sunny Hotel (en) | Same resource set; first attempt failed on `metadata.knowledgeBase.name`, fixed in the runner and generator, then completed |
| 3 | Seoul Bright Eye Clinic (ko) | Packaged by the fixed backend and deployed **without any manual repair** |

## Full `./deploy.sh --target acxd` run (2026-09-10, workshop account)

With account credentials for the Connect Customer instance, the Harbor Bank
bundle's own `deploy.sh` ran non-interactively (`AUTO_CONFIRM=1
CONNECT_INSTANCE_ID=… ACXD_WORKSPACE_ID=… ACXD_API_KEY=…`):
CloudFormation stack → Lambda code → OpenAPI host substitution → Connect
instance attributes → ACXD resources with the stack's real API endpoint as the
Data Request webhook → application build → live `development` deployment →
Contact Flow imported and published. The deployed API answered the seeded
identity (`verified: true`, balance) through the same URL the Data Requests
call. Three blockers found on the way are listed under "Cross-asset contract
facts" (InstanceType check, deployment language codes / update path, contact
flow placeholder + Q in Connect block).

## 2026-09-13 — first end-to-end Connect chat, in a sandbox Connect Customer account

Everything above was about getting a bundle to DEPLOY. This round drove the
deployed application over real Connect chat, which is what surfaced the runtime
contract: an application can deploy cleanly, build green, and still not recognise
a single intent. Two scenarios were completed end to end (delivery lookup →
follow-up → agent request → Escalation branch; cleaning price quote → follow-up →
Success branch), and the facts below are what it took. They are recorded as the
contract the generators target in
[acxd-packaging.md](./acxd-packaging.md#runtime-contract-the-generators-target).

### Data Requests: the secret is resolved per environment

| Finding | Fix |
|---------|-----|
| A header value of `{{secrets.BackendApiKey}}` is sent to the backend VERBATIM — API Gateway answers 403. The runtime's own spelling is `{BackendApiKey:NLX.Secret}` | Data Request builder emits the reference syntax with `sensitive: true` |
| `dynamic: true` on a secret header means "the caller supplies this value at request time", so the header goes out empty. It is not a "resolve this" flag | never set on a secret header |
| A correct secret referenced only from the top-level `webhook.headers` is still 403. The runtime resolves it from `webhook.environments.{production,development}` | both environment blocks are emitted with the same URL and headers; the runner substitutes `{WEBHOOK_URL}` in all three places |
| A `data_request` node without an explicit `dataRequests[].payload` mapping sends no fields at all | flow generator emits `{field: '{slot:NLX.Slot}'}` / `'{ctx:NLX.Context}'` |
| A `data_request` edge condition of `node_status: error` is invalid — the turn produced NoMessages and fell into the fallback | edges use `success` / `failure` / `timeout` |

`WebhookConfig` in the SDK models does carry `environments?: WebhookEnvironments`
(`{production?, development?}`, each `{url, headers?}`), so the shape was
available all along — nothing in the types says the secret is unreachable
without it.

### The Agentic CX block's Alias is a deploymentKey that rotates

`AgentConfiguration.Alias` stores the ACXD **deploymentKey** — an opaque nanoid,
not the environment name and not the deployment id. Nothing in the public SDK
returns it. It comes from the console-internal endpoint, with a console session
and its `cxn` bearer token:

```
GET /acxd/api/cxn/flowResources?workspaceId=<ws>&applicationId=<app>&type=deployments
→ [{ deploymentId: "3f0c…", deploymentKey: "Kx9QmT2vR7pLw4Nc8Yb3D", alias: "Production" }]
```

`UpdateApplicationDeployment` fails server-side for a ko-KR application:
`ValidationException: A deployment requires at least one language code` without
`languageCodes`, and `InternalServerException: Failed to update deployment.` with
`["ko-KR"]` — including when the update targets a different build. So the runner
falls back to delete + create, and **that rotates the deploymentKey**. The old
key still resolves, so the published Contact Flow silently keeps serving the
previous build: no error in Connect, no error in ACXD, and a chat test that
"passes" against stale behaviour. Every one of the nine deploy cycles in this
session hit this path.

Now: the runner still tries the update first; on a replacement it writes
`aliasRotated: true` with the old and new deployment ids into
`.deploy-state.json` and prints the exact rebind steps; `deploy.sh` repeats the
warning in Phase 11 and in its summary and no longer claims the alias is bound;
and `./deploy.sh --rebind-alias <deploymentKey>` patches
`ConnectParticipantWithAgenticCX.Parameters.AgentConfiguration.Alias` on the
published flow via `update-contact-flow-content`, then reads the flow back to
confirm the value was stored.

### Testing over chat requires the participant WebSocket

A chat contact created with `StartChatContact` does not run the flow until the
customer participant CONNECTS: call `CreateParticipantConnection` and open the
returned WebSocket first. Without it the contact sits there and the transcript
stays empty, which reads exactly like a broken flow or an unbound alias. Send
messages with `SendMessage` on the participant connection and read the
application's replies from the WebSocket (or `GetTranscript`).

### One project name, one CloudFormation stack

`node runner.js deploy` run on its own used the manifest's own `project`
(`aicc-poc`) and created a second stack, `aicc-poc-stack`, beside the
`gaon-stack` that `deploy.sh` had deployed from the same bundle — with its own
API Gateway. The Data Requests then pointed at a backend that was not the one
under test. `deploy.sh` now exports `PROJECT_NAME` and `AICC_STACK_NAME` before
invoking the runner, the runner honours them (and logs the stack it uses, plus a
note when the manifest disagrees), and a runner-only deploy is told to export
`PROJECT_NAME` to share the backend.

## 2026-09-14 — three scenarios end to end on one backend build

The 09-13 round proved one scenario. This round regenerated three scenarios of
different shape on the same builder build — an appliance-service desk
(delivery lookup, price quote, cleaning reservation, agent hand-off), an
e-commerce returns desk (order lookup, return request with a policy decision,
return status by return number or by order number) and a hospital appointments
desk (identity lookup, booking, cancellation, FAQ) — downloaded each bundle,
ran its own `./deploy.sh` with no manual edit, bound the alias once, and drove
every operation over Connect chat against the generated backend. All three
completed their business conversations; the agent request reached the Contact
Flow's Escalation branch and went through the hours-of-operation and staffing
checks the generated flow carries. The bundles are also stamped
(`deploy-manifest.json` → `generatedAt`, `builder.build`) so a chat result can
be traced to the builder build that produced it.

Six more runtime-contract facts came out of the chats. Each is fixed at the
source (generator rule, packager or the Lambda boundary adapter) with a test.

| Finding | Fix |
|---------|-----|
| `NLX.Date` delivers an ISO date (`2026-09-18`), but `NLX.Time` delivers a **timezone-shifted UTC instant**: a typed `10:00` reached the Data Request as `2026-09-14T14:00:00.000Z`. A time typed into an `NLX.AlphaNumeric` slot arrives as compact digits (`1000`, `930`) | rule S9 converts date-shaped regex slots to `NLX.Date` but keeps time slots on `NLX.AlphaNumeric` with `^[0-9]{3,4}$`; the boundary adapter restores `HH:MM` (left-padding `930` → `09:30`); D9-4 accepts the compact shape |
| A Data Request payload that names a slot **not filled on the path that reached the node** fails before any HTTP call — the webhook is never invoked and the turn takes the failure edge. Seen on a status lookup reachable by "return number" or by "order number": the shared request referenced both slots, so neither path could succeed | rule P1 keeps a payload to the slots every incoming path is guaranteed to have captured; when paths differ the node is cloned per incoming edge, and a slot no path captures is dropped from the request |
| The reply is validated against the whole `responseSchema`, `enum` and `pattern` included. A legitimate "not found" answer (`status: ""`) failed the enum for `status`, and the customer was sent to an agent | reply schemas carry **types only**; enum values for result branches come from the OperationSpec (`field_enums`) into rule D5, and `required` names only the envelope |
| A handler that answered with `found` but no `success` field took the failure edge on every call | the adapter derives `success` from the HTTP status, `errorCode` and `found` when the handler omits it |
| The `captured_flow` context variable is populated **only by `user_input`**. A `user_choice` node over the flow-choice slot never sets it, so the follow-up flow's "not yes/no" answer never routed | the FollowUp flow answers "무엇을 도와드릴까요?" and hands the next utterance to the shared `user_input` listen; a request named directly in the follow-up costs one extra turn |
| The `PhoneNumber` built-in delivered its value with separators in one deployment and without in another | the format restorer accepts both |

Also observed, and not something a bundle can fix:

- `knowledge_base` and generative nodes need a generative model configured in
  the ACXD **workspace**. A workspace without one accepts the deployment and
  the published knowledge base, routes the FAQ utterance to the FAQ flow — and
  the node emits nothing, so the turn is silent. Check the workspace's model
  settings before testing FAQ answers.
- `description` / `aiDescription` are ASCII-only, so routing descriptions are
  written in English even for a Korean application; two Korean utterances that
  are close in meaning ("예약 조회" vs. identity lookup) can still route to the
  neighbouring flow. Spell the distinguishing words out in the flow plan's
  description during the interview.
- Every redeploy that has to replace the application deployment rotates the
  alias key (see above); `./deploy.sh --rebind-alias <key>` was needed after
  each of the redeploys in this round.

## 2026-09-15 — a brand-new session, interview to chat

The earlier rounds regenerated sessions that were days old. This one started
from an empty session with a requirements document for the e-commerce returns
desk, went through the interview, generation and review, and deployed the
bundle untouched: order lookup, a change-of-mind return (fee consent, refund
amount computed by the backend, auto-approval), a high-value defective-item
return (approval pending, no fee question) and an out-of-window return
(rejected with the backend's reason, then the agent hand-off) all completed
over chat. What the fresh path exposed was on the builder side and is fixed:

| Finding | Fix |
|---------|-----|
| A requirements document that lists a backend-computed value (the refund amount) as an input was followed as written, so the caller was asked for it | the interview proposes the derivation instead and `save_operation_spec` warns about amount / price / status / decision inputs |
| Moving a field out of an operation's inputs left it on the primary tool, so the Data Request demanded a slot no flow collected (D3) | `update_operation_spec` aligns the tools' field lists with the operation |
| The primary tool's hand-written field list was a subset of the operation's (`orderDate`, `errorCode` missing): OpenAPI and Data Request came from the tool, the Lambda from the operation, and review blocked on D9-3 / parity | the primary tool's contract is the operation's field list, at save time and at projection time |
| A branch on `status == "rejected"` (a value the enum never holds) passed review with zero blocking findings | the spec's enum values now reach the D9 gate as well as the generator (D5 reports it in both places) |
| The `unknown` default behaviour was the only path to the knowledge base, so "what is your return policy?" was routed to the return-request flow | a knowledge base gets a routable `FaqFlow` unless the interview planned one |
| The regex attached to a built-in slot does **not** gate capture: an order number typed at the return-number prompt was accepted into `returnId` (as `GC20260902`) and the lookup escalated | rule F1 follows every capture of a pattern-bearing slot with a `matches_regex` check — a wrong shape clears the slot, says so and re-asks; when the flow has another question to fall back to it moves on after one miss, otherwise after two (verified live: the same input now gets "말씀하신 값이 형식에 맞지 않습니다" and the re-ask, and a valid value passes). The Lambda boundary adapter still moves a value that fits exactly one other empty field's shape onto that field |

Two facts about the deploy step are documented rather than changed:

- ACXD resource names (application, secret, guardrails) come from the company
  slug fixed at generation time; the deployment name given to `./deploy.sh`
  prefixes the CloudFormation stack and the Contact Flow only. Deploying the
  same company twice into one workspace under different deployment names
  therefore updates the same application in place — the newest stack wins.
- A request named directly in answer to "anything else?" still costs one turn:
  the yes/no capture recognises the intent (the conversation history shows it)
  but cannot route from a `user_choice` node; only the `user_input` listen that
  follows can.
- "Connect me to an agent" said **while a value is being collected** is
  handled by rule E2, after four other designs were tried live: the User choice
  node did not route the utterance although the documentation says it can;
  `choice.associatedSlotTypeIds` is accepted and discarded by the service (a
  `GetFlow` readback after `UpdateFlow`, by slot name and by identifier); a
  `user_input` listen in front of the capture routes the agent request but does
  not fill the flow's slot from a plain value, so every caller would answer twice
  (and with unconditional edges the listen does not even wait for input); and a
  `contains` test on the utterance matched only with the operand
  `{"type": "system", "name": "System.utterance"}` — the spellings
  `system/utterance`, `variable/System.utterance` and the `{…:NLX.System}`
  template were stored but never matched. E2 therefore puts a choice on every
  capture node's 'not captured' edge that checks the utterance for a
  language-specific agent-request word and redirects to the agent-request flow;
  anything else continues to the node's own recovery.

## 2026-09-16 — a second, empty account: from nothing to a working chat

The sandbox account was replaced overnight, so the same bundle lineage was
deployed into an account that had **no Connect instance, no agentic CX designer
workspace and no API key**. Setting the account up took the console only:
create a Connect Customer instance (about four minutes to `ACTIVE`), open the
instance's emergency-access admin site, create a workspace, and in *Admin Hub →
Users → API access* create a programmatic user (account admin) and generate its
one-time `acxd_live_…` key (63 characters, contains dots). With
`CONNECT_INSTANCE_ID`, `ACXD_WORKSPACE_ID` and `ACXD_API_KEY` exported, the
downloaded bundle's untouched `./deploy.sh` created the CloudFormation backend,
the application with all of its resources, the build, the deployment and the
published Contact Flow in twelve minutes; after `--rebind-alias` the first chat
worked (order lookup → templated result → follow-up → end).

Two generator defects showed up in the regenerated flows and were fixed at the
source, then re-proven live:

- **A missed value was skipped, not re-asked.** The regenerated `RequestReturn`
  wired every capture node's 'not captured' edge to the *next* question, so an
  unrecognised order number went straight to the return reason and the intake
  request was later sent without it (P1 then trimmed the payload — a request the
  backend cannot serve). Rule **R8** re-asks when the request's schema lists the
  slot as required; the alternative-identifier pattern (return number OR order
  number) is left alone because that request requires neither. Live: "글쎄요 잘
  모르겠어요" at the order-number prompt → the flow's own recovery wording → the
  prompt again → the value → the next question.
- **Crossed success tests.** The choice after the intake request carried
  `requestReturn.success neq true` on the branch that announces the result and
  `eq true` on the failure branch; the backend accepted the return (the record
  was written) while the caller heard "this order cannot be returned" and was
  escalated. Rule **D6** walks each branch to its first customer-facing message
  and swaps the two conditions when the result-naming branch sits behind the
  negative test. Live after the fix: "반품이 접수되었습니다. 반품번호는 RT-…,
  승인 상태는 자동승인, … 128000원".

And the mid-capture agent request was solved by design (rule **E2** above)
after the probes listed there: "상담원 연결해 주세요" at the order-number prompt
and "사람이랑 이야기하고 싶어요" at the delivery-lookup prompt both reached the
Contact Flow's Escalation branch in one turn, while a plain value at the same
prompt was captured as before.

## 2026-09-17 — a generative journey collects the values (probe series on the sandbox)

The generated applications had carried no generative node at all: the interview
labelled every step deterministic, the generator preferred a templated `basic`,
and unconfirmed generative nodes were demoted. Before making `generative_journey`
the default shape of an operation flow, its mechanics were measured on the
sandbox account: the greencart return flow was hand-converted so that the return
*reason* is collected by ONE journey node (order number and phone stay
`user_choice`), redeployed six times with one change each, and driven over
Connect chat. Node-level facts come from `QueryLogs` (NodeTraversal,
GenerativeJourney*, AgenticDataCapture, ConditionEvaluated, Error).

| Probe | Change | Result |
|---|---|---|
| 1 | journey with `dataCapture.data=[{reason, slot, required, enum schema}]`, exits `gjConditionIndex 0/1` + timeout/failure, `modelType` haiku | the journey **spoke** and, on the free description "상자가 찌그러져서 왔고 제품도 긁혔어요", `AgenticDataCapture {"reason":"상품불량"}` filled the slot — then `GenerativeJourneySucceeded`, **no edge matched** (`gjConditionIndex` 0/1 false, timeout/failure false), `Error NoMessages`, and the caller landed in the Fallback flow |
| 2 | + `node_status eq success` edge | also false — the capture exit sets no status |
| 3 | + `slot reason exists` edge, first | **works end to end**: reason captured → confirmation `basic` → phone (`user_choice`) → data request → result → follow-up |
| 4 (chat) | side question "반품 배송비는 얼마인가요?" inside the journey | `AgenticToolStart knowledgeBase` + `KbInvoked` — the journey answered from the FAQ and returned to the reason |
| 5 (chat) | "아 그냥 상담원이랑 이야기할게요" inside the journey | `gjConditionIndex eq 1` (the appended `agentRequested` exit condition) → RequestAgentFlow → Escalation → `escalate`; the contact reached the queue in 1.2 s |
| 6 | result announcement as `generative_text` | `Error IntegrationNotFound`, nothing spoken — the workspace has no default generative model and `generative_text` has no `modelType` of its own; the flow went on to the follow-up |
| 7 | result announcement as a zero-turn journey (`enableZeroTurnMode`, `modelType`) | the model wrote the sentence (`AgentEnded response: "반품 신청이 완료되었습니다! 반품번호 RT-…"`, `GenerativeJourneyZeroTurnCompleted`) but the runtime **dropped it**: a journey's reply is delivered only while the journey keeps the turn; the node left on its unconditioned edge and only the follow-up question reached the caller |

What the runtime contract does with this (rules J2–J5, `tools/acxd_runtime_contract.py`):
the plan's `captures` become the node's `dataCapture` (schema from the slot type's
values or the field's regex, `exitEnabled: true`); the captured edge is the FIRST
child edge and tests every captured slot with `exists`; an `agentRequested` exit
condition is appended and routed to the agent-request flow; a `knowledgeBase` tool
is attached when the plan asks for it; `mcpFlow` tools are refused (they fail on
invocation) — `dataRequest` tools were refused here too, until the 2026-09-21 probes
showed them stored, built and invoked (rule J8); `maxSteps` and a
node-level `modelType` are defaulted. A journey never captures a strict-format
value — those stay `user_choice` with F1's format re-ask — and the interview drops
such a slot from `captures` and says so.

Two things a journey does not give: the reply it composes on the turn it exits is
never delivered (the confirmation "파손으로 접수하는 게 맞나요?" was lost when the
value was captured on the same turn, so the plan puts a deterministic
confirmation or the next question right after the journey), and a generative
*result* sentence still depends on the workspace's default model (`generative_text`).

## 2026-09-21 — a journey carries the operation (customer workspace, voice logs + test-panel probes)

Two sources this time. First, the QueryLogs of three real **voice** calls a
customer made against a bundle generated that afternoon (the mixed shape:
journey for the described values, `user_choice` for the strict ones, then
read-back, `data_request` and result nodes). Second, a probe application in
the same workspace with hand-written flows, driven from the studio's test
panel with the debug trace open.

What the voice calls showed:

| Finding | Fix |
|---------|-----|
| The greeting was spoken **five times in 23 seconds**: the voice channel re-entered `WelcomeFlow` with `structured` requests while the greeting played, and the router sent "안녕하세요" / "뭐 해줄 수 있어요?" back to it although the flow is `untrained` — its `aiDescription` said it "greets the customer" | `WelcomeFlow` greets once per session (a `welcomeGreeted` context variable skips the greeting on re-entry) and every untrained system flow carries a routing text that describes nothing a caller might say |
| After "이번 주 금요일이에요" (a `user_choice` date capture) the flow entered the journey **on the same turn**; with no utterance of its own the journey's capture extractor returned `{"acType":"벽걸이형 실내기","address":"서울시 강남구 역삼로 100","customerName":"홍길동","quantity":"1"}` — none of it said by the caller (the name came from an earlier lookup result) — `exitEnabled` ended the node at once, the journey's own reply was dropped, and the read-back node confirmed the invented values aloud | a carrying journey (below) keeps `exitEnabled` off, marks captures optional, and reads back itself; the interview no longer plans deterministic captures in front of a journey |
| The reservation `data_request` took its failure edge with **no HTTP call** (`DataRequestsRequested` never logged): the request schema's `phoneNumber.pattern` was the dashed display format while the runtime delivers the slot without separators (`quantity` was a string against `integer` as well); the caller was escalated | request schemas carry types and enums only (`fields_to_json_schema`); the format stays in the description |
| `generative_text` result nodes failed with `IntegrationNotFound` on every call — the workspace has no default generative model; the templated fallback (M2) is what the caller heard | documented; a carrying journey announces the result itself with its node-level model |
| "세척 예약도 해주실 수 있나요 근데 세척요금이 어떻게 돼요" at the follow-up yes/no prompt cost an incomprehension count and an extra turn; after a booking "음 바꿔도 되나요?" landed in the fallback | `FollowUpFlow` listens first (below) and a carrying journey keeps handling changes, cancellations and questions after the result |

What the probes established (`ProbeJourneyTools`, `ProbeReservationJ`,
`ProbeHalluc` on a separate application; every step read from the debug trace):

| Probe | Result |
|---|---|
| CreateFlow → GetFlow with a journey carrying `{type: dataRequest, dataRequest: {dataRequestId, payload}}`, `flow` and `knowledgeBase` tools, `dataCapture` schemas with `pattern`/`enum`/`integer`, `exitEnabled: false`, `modelType: anthropic.claude-sonnet-5`, and a `generative_task` node | everything stored as sent; the build is `BUILT`; the deployment serves it. A `payload` of `{}` and no `payload` at all are stored too. The 2026-09-08 observation that the id is dropped no longer holds |
| "벽걸이형 두 대예요. 얼마예요?" | `Tool invoked getCleaningPrice` with `toolInput {"acType": "벽걸이형 실내기", "quantity": 2}` — **composed by the model** (an integer, not the slot string), `DataRequestsRequested/Returned 200`, `Tool responded` with the backend body, then the journey spoke "벽걸이형 실내기 세척 가격은 1대당 100,000원이며, 2대 기준 총 200,000원입니다. 더 필요하신 사항이 있으신가요?" **while it still held the turn** |
| Consent `basic` (verbatim) + `user_choice` yes/no → journey with `getCleaningPrice` and `createCleaningReservation` tools, five values in two utterances | the journey asked for what was missing, read everything back with the price ("이대로 접수할까요?"), called `createCleaningReservation` on "네 접수해 주세요" with `toolInput {"quantity": 2, "phoneNumber": "010-4114-9537", …}` — the phone in the request schema's dashed format — and announced "예약이 접수되었습니다. 예약번호는 CR-…, 방문예정일은 2026-09-25, 총 금액은 200,000원입니다. 변경사항이 있으시면 세척일 전에 미리 연락드립니다." |
| Exit conditions | are **tools the model calls** (`Tool invoked toolName: done, toolType: exitCondition`) → `System.gjConditionIndex eq i`; "아 그냥 상담원이랑 이야기할게요" reached `RequestAgentFlow` → `EscalationFlow` in one turn |
| `dataCapture` on the same turns | the extractor is a **separate model call**: it stored `phoneNumber: 01011148371` while the journey read back 010-4114-9537, returned `{}` on turns where three values were given, and with Sonnet 5 errored with "User messages cannot contain reasoning content" — a slot filled by a journey is not a value to build on |
| `generative_task` (`agenticTask`, zero-turn) after the `done` exit | no message (`Error NoMessages` → fallback), like the zero-turn journey of 2026-09-17: the node after a journey must speak or redirect |
| A wrong-shape phone ("010 4114 953") | the journey re-asked with the format ("010으로 시작하는 11자리 숫자로 다시 한번 말씀해 주세요") — format handling by prompt works |
| Haiku vs Sonnet 5 on the same flow | Haiku: 1.4–3.3 s per turn, but "이번 주 금요일" became 2026-09-26 (a Saturday) and the result was read as "예약번호: … 방문일: … 총 금액: …"; Sonnet 5: 3.8–9.8 s per turn, the right date, sentences. Carrying journeys default to Sonnet 5; the prompt tells the model to confirm a relative date with the customer instead of computing weekdays |
| `CreateApplicationDeployment` | `500 Failed to create deployment` **without** `languageCodes` and success **with** `["ko-KR"]` — the opposite of 2026-09-12; the runner's try-both stays |
| Replicating the hallucination with the customer's configuration (Haiku, `required: true`, `exitEnabled: true`) on a short transcript | not reproduced — the extractor answered `{}`; the invented values needed the longer voice transcript (an earlier lookup result, a routing loop). The mitigation does not depend on reproducing it: nothing downstream reads the extractor's slots |

Source: rule J8 in `tools/acxd_runtime_contract.py` (dataRequest tools kept and
normalised, plan `journey_tools` `data_request[:id]` added as tools, captures
optional with `exitEnabled: false`, `done` / `anotherRequest` exits, no J6
read-back, Sonnet 5 and 16 steps, tool-use rules appended to the prompt),
`journey_carries_request` in `tools/acxd_flow_spec.py` (plan validation: no
`data_request` step that repeats the journey's request, no strict-format
`user_choice` requirement, a journey decides a hand-off), the interview and
generator prompts (mandated wording verbatim, no dropped fields, the carrying
shape), `fields_to_json_schema` (no request-side pattern / length), and the
system flows (`WelcomeFlow` guard and routing text, listen-first `FollowUpFlow`).

## 2026-09-22 — six fresh interviews on the journey-carries-the-operation build

Three Korean and three English requirement documents (a home-appliance
cleaning service, an e-commerce returns desk, a hospital, an order-tracking
shop, a restaurant reservation line, a clinic) were interviewed end to end on
the dev builder, each to a downloaded bundle. Every operation came out as one
7-node flow whose generative journey holds the `dataRequest` tools, exits
through `done` / `agentRequested` / `anotherRequest`, keeps its captures
optional and carries the `[tool use]` and `[conversation style]` rules; the
mandated sentences (a consent text with its yes/no gate, a refund notice, a
911 line) were verbatim; request schemas carried no `pattern`. What the runs
caught, and where it went:

| Finding | Where it showed | Source |
|---|---|---|
| The generator wrote journey `done` exits with `left.type: "variable"` for `System.gjConditionIndex`; such an edge never matches at runtime. | All six bundles before the fix. | Rule J retypes `System.*` operands to `system`. |
| The interview saved an OperationSpec for FAQ (`answer_faq`, `department_faq`). The Lambda-count, parity and missing-asset gates then demanded a FAQ Lambda and path; one run built them, another was stuck for an hour because no tool deletes a spec. | Two Korean runs. | `is_kb_native_spec` / `get_backend_specs`: a spec answered by the knowledge base (`data_source.db_type: knowledge_base`, or every tool flagged `generate_lambda/openapi: false`) counts for nothing — no tool id, no Data Request, and a stale or empty request file is dropped by the bundle loader. |
| A journey tool payload omitted a field the request marks `required` (`action`, `patientName`); every call would have been a `VALIDATION_ERROR`, and only a reviewer advisory noticed. | Hospital run, twice. | J8 fills a missing required field from a same-named capture or reports it; `JOURNEY_PAYLOAD_MISSING_REQUIRED` in the consistency check. |
| The generator pinned Haiku on 7 of 16 carrying journeys — the model the probes showed mis-computing weekdays and reading results as colon lists. | Four bundles. | J8 overrides a fast model on a carrying journey (it composes the backend's arguments). |
| "'AC-' + 8 digits" derived `pattern ^\d{8}$` and length 8 for an 11-character id, contradicting the slot regex — a D9-4 blocking finding. | Three runs. | `_enforce_exact_length_phrase` honours a literal prefix next to the length phrase and keeps a prefixed pattern the interviewer wrote. |
| The hospital document's emergency rule (trigger words → "응급 상황이면 119 또는 응급실(24시간)로 연락해 주세요" → hand-off) reached no flow, guardrail or prompt; it survived only inside a FAQ article. | Hospital run. | `ACXDGuardrailPlan.message`; the interview records safety hand-offs as keyword `route` guardrails with the sentence; rule J9 writes the sentence into every journey's rules (said verbatim, then `agentRequested`). The guardrail path itself still lands in the escalation flow's generic line — not yet verified live. |
| `read_acxd_asset` / `patch_acxd_asset` returned `{status, content}`; strands takes such a dict as a preformed ToolResult, so the file text reached Bedrock as a bare block and the turn died (`content_type=<{> unsupported type`). | One run, mid-review. | Payload keyed `file_content`; the message sanitizers coerce stray string blocks. |
| The Korean journey tool rules carried an example from one customer's domain into another project's prompts. | Hospital bundle. | Example removed. |

Wall time per run, interview to download: 40–45 minutes when the review was
clean on the first round; 99–166 minutes for the two runs that hit the FAQ-spec
gates before the fix (including three backend rollovers).

## Service contract facts (not in the SDK types, learned from the API)

| Area | Fact | Where it is enforced now |
|------|------|--------------------------|
| Flow node ids | Must be RFC 4122 **version 4** shaped (`xxxxxxxx-xxxx-4xxx-[89ab]xxx-…`). Other shapes are accepted by `CreateFlow` but the stored flow has no nodes and every `UpdateFlow` fails with `nodes is not in the expected format` | generator repair, `flow.schema.json`, D9-1 |
| `redirect` | `metadata.redirect.type` is required (`flow` / `page` / `parent_application`) | generator repair, schema |
| `define` | `metadata.define.value` must be an Operand object (`{type: constant, value}`) | generator repair, schema |
| `escalate` | `FlowNodeMetadata` has no `escalate` config; messages live on the node, the node is terminal | generator repair, schema |
| `knowledge_base` | `metadata.knowledgeBase.name` is **required**; `scopeTags` is not a field | generator, schema, runner transform, bundle-load normalization |
| Context variables | `UpdateContextVariable` keys by `contextVariableIdentifier` | runner |
| Data Request webhook | The service checks that the URL is reachable at create time | documented; `deploy.sh` supplies the real API endpoint |
| Application name | Must be ASCII; a CJK company name now derives it from the project slug | resource builder |
| Secrets | `CreateSecret` / `UpdateSecret` take the value as **`secretValue`** (+ `isSensitive`); a `value` key is dropped by the SDK serializer and the service answers `InternalServerException: Failed to create secret` (2026-09-12) | runner |
| Deployment language | `CreateApplicationDeployment` with `languageCodes: ["ko-KR"]` answered `InternalServerException: Failed to create deployment`; the same request without `languageCodes` was accepted and the deployment uses the application's language settings (`UpdateApplicationDeployment` without them answers `A deployment requires at least one language code`) (2026-09-12) | runner: send the codes, fall back without |
| Backend API key | The generated API Gateway requires its key in the ACXD target; Data Requests send `x-api-key: {<slug>BackendApiKey:NLX.Secret}` (the developer guide's `{{secrets.<name>}}` spelling is forwarded verbatim and gets 403 — see 2026-09-13); the runner fills the secret from the stack's `ApiKeyValue` output. Verified 2026-09-12/13: 403 without the key, 200 with it, Data Request stored with the secret reference | merge, Data Request builder, runner, D9-8 |
| Lambda names | The template names functions `${ProjectName}-${Environment}-<op-with-hyphens>`; the runner resolves them from the stack's `AWS::Lambda::Function` resources instead of a naming convention | runner |
| Application language | `CreateApplication` silently ignores `settings.languageCode` / `languageCodes` / `languageSettings` and creates the application as `en-US`; the build then snapshots `en-US` while every flow is `ko-KR`, and the Agentic CX block fails with "NLX Chat Streaming Failed" on the first contact. `UpdateApplication` honours the fields | runner re-reads the application after create and re-applies the languages |
| Yes/no comparisons | A condition on a slot attached as the `yesNo` type must compare against that type's own values (`예` / `아니요`, …); `"yes"` never matches, so a consent question read every answer as a refusal | runtime contract S8 |
| Message nodes | A `basic` node that also carries `metadata.redirect` (or clears the slots its own message renders) never shows its message — the contact falls into the fallback re-guide | runtime contract M3 |
| Result conditions | A branch on `<dataRequest>.<field>` only works for a field the Data Request's `responseSchema` declares; an undeclared field (`accepted` where the API returns `success`) sends every success down the failure branch | runtime contract D5 |
| Custom slot types | A slot type exists to enumerate a menu. A one-value type built for an open value (an order number, a phone) is auto-selected without asking; open values are collected as an NLX built-in with the field's regex, and `user_choice` on such a slot is the working single-value capture | slot-type builder, D9-4, bundle loader |
| Guardrail messages | An output guardrail's replacement message is spoken verbatim; the service has no default in the caller's language | guardrail builder localises the default |
| Slot values at the webhook | Built-in slot values reach the Data Request without their separators (`010-1111-2222` → `01011112222`, `2026-09-16` → `20260916`, `10:00` → `1000`) | ACXD bundles wrap each Lambda handler with a format restorer built from the OperationSpec patterns |
| Webhook reply | A Data Request succeeds only on HTTP 200 with a body that matches its `responseSchema`; a `201` from a create operation or `errorCode: null` where the schema says string takes the failure branch | the same wrapper returns 200 and coerces the envelope |
| Output guardrails | A derived output rule (LLM judge, generalised literal) that rewrites messages fired on the greeting itself and on the bot's own format hint | derived output rules stay advisory (`flag`), and the rule's `description` says why — a reviewer read the flag as a defect once |
| Journey data capture | `generativeJourney.dataCapture.data[{name, type: slot, required, schema}]` fills the flow's attached slot as the journey talks (a free description was classified onto the enum); when every required value is captured the node ends and evaluates its edges with **no** `System.gjConditionIndex` and **no** `node_status` set — a journey without a slot-test edge logs `Error NoMessages` and the caller lands in the fallback flow | J2/J3: dataCapture from the plan's `captures`, the captured edge (`slot <name> exists` per capture) first |
| Journey exit conditions | `exitConditions[i]` fire as `System.gjConditionIndex eq i` (an LLM judgement per turn); "상담원이랑 이야기할게요" reached the queue in 1.2 s through an appended `agentRequested` condition; a KB tool call (`AgenticToolStart knowledgeBase`, `KbInvoked`) happens inside the turn | J4/J5: agent exit appended and routed, `knowledgeBase` tool attached, `mcpFlow` refused; a `dataRequest` tool is kept and invoked by the journey (J8, 2026-09-21) |
| Journey `dataRequest` tools | `{type: dataRequest, dataRequest: {dataRequestId, payload}}` is stored as sent, builds, and the journey invokes it with arguments the model composes against the request schema (an integer quantity, a phone in the schema's dashed format); the backend's reply comes back as the tool result and the journey announces it while it holds the turn (2026-09-21) | J8: kept and normalised, plan `journey_tools` `data_request[:id]` added, `exitEnabled: false`, optional captures, `done` / `anotherRequest` exits, Sonnet 5 |
| Journey capture extractor | A separate model call: it stored `01011148371` while the journey read back 010-4114-9537, returned `{}` on turns with three values, and errored with Sonnet 5 ("User messages cannot contain reasoning content"); with `required: true` on a long voice transcript it invented four values (2026-09-21) | nothing downstream depends on a carrying journey's captured slots; the request is a tool call |
| Request schema validation | The request body is validated against `requestSchema` BEFORE the HTTP call: a dashed phone `pattern` against the separator-stripped slot value took the failure edge with no `DataRequestsRequested` event (voice, 2026-09-21) | request schemas carry types and enums only |
| Welcome re-entry | The voice channel re-entered `WelcomeFlow` with `structured` requests while the greeting played, and the router sent greetings and "what can you do?" to the untrained welcome flow because its `aiDescription` described greeting (2026-09-21) | greet once per session (`welcomeGreeted`); untrained system flows carry a neutral routing text |
| Journey replies on exit | The sentence a journey composes on the turn it exits is not delivered (a zero-turn announcement journey wrote the result and the caller heard only the next node) | the node after a journey speaks; result announcements are not journeys |
| `generative_text` without a workspace model | `Error IntegrationNotFound`, `node_status` failure, silence — `generative_text` has no `modelType` of its own; a `failure` edge to a templated `basic` speaks | M2 keeps the confirmed node and adds the fallback edge instead of replacing it |
| LLM-judged input `route` guardrail | A rule derived from the escalation policy ("refund/claim requests go to an agent", threshold 0.8) fired on a customer describing a cleaning order — "냄새가 나서 … 종합으로 세척받고 싶어요" — and `RequestOverridden actor: guardrail` sent the call to the escalation flow before the journey collected anything | an llmJudge input rule with `route`/`block` is kept as `flag`; keyword and regex rules keep their action; hand-off by topic belongs to the journey's exit conditions |
| Output `mask` on a phone regex | Redacted the bot's own hint: "010-1234-5678 형식으로" → "[REDACTED] 형식으로" (second time, now with a regex written on purpose). The same regex as an **input** mask left the slot capture intact — "010-2345-6789" reached the phone slot and the reservation went through | a `mask` planned on `output` runs on `input` unless the plan sets `mask_bot_output` |
| `NLX.PhoneNumber` value vs. a dashed regex | "010-2345-6789" was refused by the F1 format check against `^01[0-9]-[0-9]{3,4}-[0-9]{4}$` — the runtime delivered the value without dashes and the variable-width run kept the pattern from being loosened | `runtime_regex` makes every literal separator optional even when the shape is not fixed |
| Empty context-variable wrapper | `{"contextVariables": []}` (a project with no variable) was packaged as a variable and the runner failed at step 5 with "key is required"; no gate had looked at it | the loader unwraps the wrapper, the D9 schema gate validates context variables, and the schema accepts the SDK's `string` type |
| A generated journey, end to end | One free-text turn — "스탠드형 두 대 … 종합으로 … 김민수 … 다음 주 화요일 오전 … 테헤란로 123 …" — filled six captures (two of them enums) at once; the delivery flow's journey took name and address after a miss, the phone step stayed deterministic, A2 found the order, the reservation flow priced 2 × 150,000 and created RSV-…; both ended in FollowUp → end | the generated bundle of a real interview (2026-09-17) behaves as designed |
| 200 with `success: false` | The reservation request refused (consent not recognised) with HTTP 200 and the flow, branching on `node_status` alone, read back "예약번호 , 요청일 , 총 금액 , 원 상태 입니다." — every placeholder empty | D7: `success eq true` is tested before a result is announced; otherwise the API's `message` is spoken and the node's failure edge (the plan's hand-off) is followed |
| Slot values in a request body | A `yesNo` slot posted `privacyConsent: "예"` and `quantity: "2"` although the request schema declares boolean/integer; the handler's bool parser knew English words only | the Lambda adapter coerces request fields to their declared types (yes/no words in ko/en/ja) |
| Second generated bundle, end to end | With the rules above the interview's first plan was approved as written, the review round reported 0 blockers, and both operations ran on the first deploy: consent → journey (seven captures, "다음 주 화요일 오전" normalised to a date) → read-back → phone → price → reservation R-…; a lookup miss → journey → phone → not found → agent queue; consent declined → agent queue | — |
| `modify` guardrails | `CreateGuardrail` rejects a `modify` rule that has no `behavior.message` or `behavior.prompt` (one of the two, per the SDK) — with `ValidationException: rules[0].enforcement.action is not a supported value`, which names neither the guardrail nor the real cause. Live (2026-09-16): a hand-edited action passed packaging and the deploy died at the third guardrail, after two were created | the guardrail schema requires exactly one of `message`/`prompt` for `modify` (so patching and packaging refuse it), and the runner pre-flights every guardrail file before creating any and names the guardrail in the error |
| Contact language | The Agentic CX block fails every contact with "NLX Chat Streaming Failed" unless the contact's language has been set (an `UpdateContactData` block with `LanguageCode` matching one of the application's languages) before the block | the Contact Flow binding inserts the block when the flow has none |
| Workspace-level names | Secrets, guardrails and slot types are keyed by name across the whole workspace: two projects using `BackendApiKey` overwrote each other's API key on every deploy | the backend key secret and guardrails are named per project |
| Built-in date / time slots | `NLX.Date` → ISO date; `NLX.Time` → a timezone-shifted UTC instant, unusable as a wall-clock time | S9 uses `NLX.Date`; time stays `NLX.AlphaNumeric` `^[0-9]{3,4}$`, restored to `HH:MM` by the adapter |
| Payload placeholders | A `{slot:…}` placeholder for a slot not filled on the reaching path fails the Data Request **before the HTTP call** | runtime contract P1 (per-path payloads, node cloned per incoming edge) |
| Reply validation | The reply must satisfy the full `responseSchema` including `enum` / `pattern`; a valid "not found" answer can fail it | reply schemas carry types only; result-branch enums come from the OperationSpec into D5 |
| `captured_flow` | Populated by `user_input` only, never by a `user_choice` over the flow-choice slot | FollowUp flow re-listens through `user_input` |
| Generative / KB nodes | Need a generative model configured on the workspace; without one the node is silent although the deployment and knowledge base succeed | documented prerequisite (`WIRING-GUIDE.md`) |
| Routing text | `description` / `aiDescription` are ASCII-only; non-ASCII is rejected on create | system flows and the generator write English routing text; the bundle loader strips non-ASCII |
| Slot regex | The `regex` attached to a built-in slot is not enforced at capture: a value of the wrong shape is stored (an order number in the return-number slot) and reaches the Data Request. A `matches_regex` condition on the slot IS evaluated, against the delivered (separator-stripped) value | runtime contract F1: a `matches_regex` guard after every pattern-bearing capture, separators optional, letters either case; adapter reassigns a value that fits exactly one other field |
| Associated slots | `choice.associatedSlotTypeIds` is accepted by the API and silently discarded (by slot name and by identifier); a capture node listens on one slot only | no second slot per node; E2 tests the utterance instead |
| Utterance conditions | `{System.utterance}` is testable in a condition only as `{"left": {"type": "system", "name": "System.utterance"}, "operator": "contains", …}`; `system/utterance`, `variable/System.utterance` and the `{System.utterance:NLX.System}` template are stored but never match | E2 uses the working operand |
| User choice routing | The documented fallback from a User choice node to another flow's routing description did not fire (ko-KR, two accounts): an unmatched answer takes the No match path | E2 on the No match path |
| user_input semantics | A `user_input` whose edges are `captured_flow exists / not_exists` waits for the next utterance; one with an unconditional edge is evaluated at once against the utterance that entered the flow. Routing by a `user_input` does NOT fill the current flow's attached slots from a plain value | no listen in front of a capture |
| Routing utterance | The utterance that routes to a flow also fills that flow's slots when a built-in type accepts it — into the first matching slot, regardless of its regex | keep alternative identifiers on distinct paths and let the backend accept either |

## Cross-asset contract facts

| Finding | Fix |
|---------|-----|
| Data Requests targeted `/tools/<operation_id>` while Lambda/OpenAPI expose `/tools/<tool_id>` (e.g. `check_balance` vs `verify_and_get_balance`) | the generation context resolves the path from the OperationSpec tool, then the OpenAPI contract |
| OpenAPI puts the `/tools` prefix in `servers[0].url` or uses kebab-case keys | D9-3 indexes every legitimate spelling |
| `^\d{8}$` vs `^[0-9]{8}$`, `phonePin` vs `phone_pin` | D9-4 compares canonical regexes and name variants; slot types are derived with the same tolerance |
| Japanese length phrases (`数字10桁`, `英数字8桁`) and English phrases with modifiers (`10 alphanumeric characters`) were stored as patterns / turned into garbage regexes | `spec_manager` derives real regexes and refuses to treat a sentence as a format mask |
| A system role planned twice (`WelcomeFlow` + `Welcome`) shipped nine flows | one flow per system role, `remove_acxd_flow_plan`, readiness check |
| A slot type named after a field with digits (`cardLast4`) or a generative node without a prompt burned all LLM attempts | deterministic repairs |
| A wrong FieldSpec could not be corrected after the interview | `update_operation_spec` is exposed to the ACXD orchestrator after the interview; the overlay says to regenerate afterwards |
| `describe-instance` returns no `InstanceType` for a working Connect Customer instance; `deploy.sh` aborted after CloudFormation | warn and continue |
| A deployment needs `languageCodes`; `UpdateApplicationDeployment` answers 500 for every payload; a second `CreateApplicationDeployment` per environment is refused | runner takes the languages from the application and promotes a build by delete + create |
| `MessageParticipant` cannot carry `Transitions.Conditions`; the Classic `CreateWisdomSession` block has an ARN ACXD never provisions | placeholder → `Compare` on `$.Attributes.AgenticCXBranch`; Q in Connect block spliced out |

## Runtime / session facts

- strands ≥ 1.5x calls the public `format_request`; the Bedrock message
  sanitizer now overrides it (it previously never ran). A turn cut by a restart
  left a trailing assistant message (rejected as prefill), and a tool call
  written as text left an empty user message; both are repaired.
- A cancelled stream can leave a lone UTF-16 surrogate; history writes now
  encode with replacement instead of failing.
- The orchestrator occasionally narrates tool results it never received. The
  ACXD overlay carries an explicit tool-honesty rule; the progress panel and
  the packaging gate are driven by real tool completions and remain the source
  of truth.

## Still manual

- Backend CloudFormation, Lambda code upload, contact-flow import and phone
  number claim run through the Classic phases of the bundled `deploy.sh` and
  need credentials for the Connect account.
- The Agentic CX block's alias (deploymentKey) cannot be read with the public
  SDK, so binding it is either `ACXD_ALIAS_ID` / `--rebind-alias` with a key
  taken from the console-internal `flowResources` endpoint, or a click in the
  block's dropdown followed by Publish. `WIRING-GUIDE.md` explains the console
  step. A re-run of `./deploy.sh` keeps the alias already bound to the flow;
  when the run had to replace the application deployment the key rotates and
  the script says so — re-bind once more.
- Two projects deployed into one Connect instance share that instance's single
  Q in Connect assistant (Classic target): the most recently deployed project
  owns the assistant's default AI agent. Use one instance per Classic project
  when both must answer at the same time.
- Rotate any programmatic API key that was used from a shared machine.
