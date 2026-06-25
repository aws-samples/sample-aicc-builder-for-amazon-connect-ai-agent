# `update_q_session` — fixed Node.js Lambda (do NOT generate)

This is a **static, business-agnostic** Lambda that the AICC Builder bundles into
every package — it is **not** generated per project and is **not** part of the
OpenAPI spec or the per-tool CloudFormation fragments. It is vendored here verbatim
from the webapp's `asset_packager.py` (`UPDATE_Q_SESSION_LAMBDA_CODE`) so the CLI
skill ships the identical handler.

## What it does

Called directly from a Contact Flow (via `InvokeLambdaFunction`), it proxies
`UpdateSessionData` into the Amazon Connect AI agent (Q in Connect) session so
customer/session data set in the flow becomes available to the AI agent. Business
fields ride in via Contact Flow `LambdaInvocationAttributes` — the handler reads
every non-reserved parameter and forwards it.

- Runtime: **Node.js 18.x**, handler `index.handler`.
- Env vars: `AI_ASSISTANT_ID`, `CONNECT_INSTANCE_ID` (plus `AWS_REGION`).
- SDK deps: `@aws-sdk/client-qconnect`, `@aws-sdk/client-connect`.

## How the skill uses it

When packaging the output bundle, copy this `index.js` to
`assets/v1/lambda/update_q_session/index.js` and let `deploy_workshop.sh` zip and
deploy it like any other Lambda (it already special-cases `update_q_session` and
`customer_lookup`). Do **not** ask a generator to produce it and do **not** add it
to `openapi.yaml` — it is invoked by the flow, not by API Gateway.
