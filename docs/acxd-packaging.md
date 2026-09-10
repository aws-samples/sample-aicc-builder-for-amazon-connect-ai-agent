# ACXD packaging and deployment

ACXD is a runtime target of the Classic Full pipeline. A single download is produced by `tools.asset_packager`: Classic backend assets remain in their normal locations, while ACXD assets are added under `assets/acxd/`. The package omits `prompts/ai_agent_prompt.yaml` for the ACXD target.

## Archive contract

An ACXD archive contains `deploy-manifest.json`, `runner.js`, `package.json`, `lib/*.js`, `WIRING-GUIDE.md`, and these generated resources:

- `assets/acxd/flows/*.json`, `slot-types/*.json`, `data-requests/*.json`, `guardrails/*.json`, `knowledge-bases/*.json`, and `secrets/*.json`
- `assets/acxd/application.json` and `assets/acxd/context-variables.json`
- the Classic `contact-flow/contact_flow.json`, CloudFormation, Lambda, and OpenAPI assets

The packager runs D9 before archive creation. Any D9 error, manifest-schema failure, coverage gap, or missing manifest-referenced backend file rejects packaging. Secret values are never written to the archive; secret declarations name an environment variable that the runner reads at deploy time.

## Deploy

```bash
./deploy.sh --dry-run          # runtime target is detected from the bundle; --target overrides
./deploy.sh
./deploy.sh status --target acxd
./deploy.sh cleanup --target acxd
```

Classic remains the default target. ACXD deployment runs the shared CloudFormation, Lambda, OpenAPI, Connect instance, Lambda-environment, and phone-number phases. FAQ-to-S3, Q in Connect, AgentCore Gateway/MCP, Lex, and AI Prompt/Agent/security-profile phases are skipped. The static runner deploys ACXD resources in manifest dependency order and receives `WEBHOOK_URL` from the CloudFormation API Gateway output.

The deploy script prompts for `ACXD_WORKSPACE_ID` and `ACXD_API_KEY` unless they are already exported, and (interactively) offers to take `ACXD_ALIAS_ID` — the application alias the Agentic CX block binds to. None of these values is saved to the archive or either deployment state file.

## Agentic CX block

The generated Contact Flow carries the real block — `"Type": "ConnectParticipantWithAgenticCX"`, taken from an Amazon Connect console export and verified to re-import through `CreateContactFlow` (reference: `knowledge-base-docs/contact-flow/_reference-console-export-agentic-cx-block.json`). Its outputs are wired by the deterministic binding pass: **Default** (`NextAction`) → disconnect, **Escalation** (`Conditions` entry `Equals "Escalation"`) → queue transfer, **Error** (`NoMatchingError`) → fallback message, **Idle chat timeout** (`InputTimeLimitExceeded`) → disconnect. Speech recognition is `AMAZON_AGENTIC_VOICE` when the application uses agentic voice, and the audio filler is configured.

The bundle keeps `{ACXD_WORKSPACE_ID}`, `{ACXD_APPLICATION_ID}` and `{ACXD_ALIAS_ID}` placeholders in the block; `./deploy.sh` substitutes the workspace and the application it just deployed at import time. Connect does not validate these ids on import. The alias is an opaque ACXD identifier the SDK does not list: export `ACXD_ALIAS_ID` before deploying to have it written into the block, or pick it in the block's dropdown in the Connect designer afterwards and publish — that is the only manual step, as `WIRING-GUIDE.md` in the bundle explains. The Agentic CX block requires a Connect Customer instance.
