# ACXD runtime target — live validation log (2026-09-10)

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
| Backend API key | The generated API Gateway requires its key in the ACXD target; Data Requests send `x-api-key: {{secrets.BackendApiKey}}`; the runner fills the secret from the stack's `ApiKeyValue` output. Verified 2026-09-12: 403 without the key, 200 with it, Data Request stored with the secret reference | merge, Data Request builder, runner, D9-8 |
| Lambda names | The template names functions `${ProjectName}-${Environment}-<op-with-hyphens>`; the runner resolves them from the stack's `AWS::Lambda::Function` resources instead of a naming convention | runner |

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
- The Agentic CX block's Flow Language type is undocumented; the imported flow
  carries a placeholder block and `WIRING-GUIDE.md` explains the console step.
- Rotate any programmatic API key that was used from a shared machine.
