# AICC Builder Skill

A portable version of [AICC Builder](../../) that runs as a **Claude Skill**
or **Kiro Skill** — same prompts, same sub-agents, same asset bundle, but
without the webapp.

The original AICC Builder is an ECS Fargate + FastAPI + Strands Agents web
app that interviews a customer for ~15 minutes and emits a fully customized
Amazon Connect AI agent PoC (Lambda + OpenAPI + Prompt + Contact Flow + CDK
+ FAQ). This skill packages the same behavior so users can invoke it
directly inside Claude Code or Kiro — no infrastructure to deploy.

## What you get

```
skills/aicc-builder-skill/
├── claude/SKILL.md         # Claude Skills entry point (YAML frontmatter)
├── kiro/SKILL.md           # Kiro Skills entry point (same body, Kiro frontmatter)
└── resources/              # Shared payload — both SKILL.md files reference these
    ├── orchestrator/
    │   ├── system_prompt.md           # Full orchestrator prompt (COMMON + TERMINOLOGY
    │   │                              #   + all phases + ATTACHMENT_HANDLING + REGENERATION)
    │   ├── interview_agent.md         # Interview persona (full + scoped)
    │   ├── document_analysis.md       # Raw-requirements-doc entry mode
    │   └── operation_spec_template.md # OperationSpec authoring template
    ├── sub-agents/
    │   ├── _shared_rules.md        # 17 golden rules (incl. BUSINESS_OUTCOME_200_RULE)
    │   │                          #   + Complete/Escalate tool-result contract
    │   ├── infrastructure_generator.md
    │   ├── lambda_generator.md
    │   ├── openapi_generator.md
    │   ├── prompt_generator.md
    │   ├── contact_flow_generator.md
    │   ├── faq_generator.md
    │   ├── research_agent.md
    │   └── reviewer_agent.md
    ├── reference/                     # Authored (NOT auto-extracted)
    │   ├── vision_import.md           # Flow-image → Contact Flow JSON contract
    │   └── contact_flow_block_schemas.md  # API-verified block Types + per-block params
    ├── schemas/
    │   ├── OperationSpec.schema.json
    │   ├── InfrastructureSpec.schema.json
    │   └── ... (12 more JSON Schemas, incl. SessionFlowConfig / ContactFlowSpec / FlowBehavior)
    ├── scripts/
    │   ├── lint_assets.py           # AUTO-GENERATED from backend tools/asset_linters.py:
    │   │                            #   Lambda syntax + BUSINESS_OUTCOME_200, cfn-lint,
    │   │                            #   OpenAPI, Contact Flow, AI prompt (+ --fix)
    │   ├── validate_consistency.py  # 18-check cross-asset validator (incl. IAM, RDS, SQL)
    │   ├── shape_parity.py          # spec ↔ OpenAPI shape parity HARD GATE
    │   ├── check_spec_complete.py   # Interview-completion gate
    │   └── clues_format.py          # CLUES response helper
    ├── templates/
    │   ├── pre_questionnaire_template.md
    │   ├── deploy_workshop.sh
    │   └── update_q_session/index.js  # FIXED Node.js Lambda — bundled, not generated
    └── examples/
        └── sample_*.md              # Complete + partial input examples
```

## Install (Claude Code)

Claude Code requires the skill's enclosing folder name to match the
`name:` in the SKILL.md frontmatter — so the install steps below rename
`claude/` → `aicc-builder/` and promote `SKILL.md` to the top of that
folder.

The one-shot installer is the recommended path — it does exactly the manual
steps below (promote `SKILL.md`, drop the sibling-platform dir, drop the
dual-platform helper `scripts/` and the top-level `README.md`) so the
installed skill is identical either way:

```bash
skills/aicc-builder-skill/scripts/install.sh claude user      # ~/.claude/skills/
skills/aicc-builder-skill/scripts/install.sh claude project   # ./.claude/skills/
```

Equivalent manual steps (these mirror `install.sh` exactly):

```bash
# Personal install
mkdir -p ~/.claude/skills
cp -r skills/aicc-builder-skill ~/.claude/skills/aicc-builder
mv ~/.claude/skills/aicc-builder/claude/SKILL.md ~/.claude/skills/aicc-builder/SKILL.md
rm -rf ~/.claude/skills/aicc-builder/claude ~/.claude/skills/aicc-builder/kiro \
       ~/.claude/skills/aicc-builder/scripts
rm -f  ~/.claude/skills/aicc-builder/README.md   # skill-internal docs live in SKILL.md

# Or project-scoped (commit to your repo): same, under ./.claude/skills/
```

> The `resources/` tree (incl. `reference/` and `templates/`) is kept; only the
> re-sync `scripts/` (`extract_prompts.sh`, `install.sh`) and the top-level
> `README.md` are dropped, since they're for maintaining the skill, not running it.

Then in Claude Code:
```
/skills
# you should see "aicc-builder" in the list
```

Trigger with: `/aicc-builder` or any of the natural-language triggers in
the frontmatter description.

## Install (Kiro)

```bash
skills/aicc-builder-skill/scripts/install.sh kiro user      # ~/.kiro/skills/
skills/aicc-builder-skill/scripts/install.sh kiro project   # ./.kiro/skills/
```

Equivalent manual steps:

```bash
# Kiro skills live at ~/.kiro/skills/ by default
mkdir -p ~/.kiro/skills
cp -r skills/aicc-builder-skill ~/.kiro/skills/aicc-builder
mv ~/.kiro/skills/aicc-builder/kiro/SKILL.md ~/.kiro/skills/aicc-builder/SKILL.md
rm -rf ~/.kiro/skills/aicc-builder/claude ~/.kiro/skills/aicc-builder/kiro \
       ~/.kiro/skills/aicc-builder/scripts
rm -f  ~/.kiro/skills/aicc-builder/README.md
```

Then verify: restart Kiro (or start a new `kiro-cli chat` session) and confirm
`aicc-builder` appears in the loaded-skills list. Trigger it with any of the
natural-language triggers in the frontmatter description ("amazon connect",
"aicc", …).

## Requirements

- **Python 3.9+** for the bundled validator scripts
- **PyYAML** (`pip install pyyaml`) — the consistency validator parses
  the generated OpenAPI YAML
- **Optional, but recommended for full parity with the webapp:**
  `pip install cfn-lint openapi-spec-validator`. The ECS image has both, so the
  webapp always validates the CloudFormation template and the OpenAPI document.
  Without them `lint_assets.py` still applies its deterministic autofixes but
  **skips those two validations** — it logs a warning saying so rather than
  failing, which is easy to miss in a long run.
- No AWS credentials needed to run the skill itself — you only need them to
  `aws cloudformation deploy` the generated template.

## How it works

When you invoke the skill, Claude/Kiro reads `SKILL.md`, detects the user's
language, and picks a **mode** (the same three the webapp offers):

- **Full build** — the full interview, then all 6 asset packages.
- **Single segment** — a focused interview + just one of Contact Flow / AI
  Prompt / FAQ (scoped run; out-of-scope generators are skipped).
- **Improve existing** — the user supplies an existing Contact Flow JSON, AI
  Prompt YAML, or a flow-diagram image; the skill lints/repairs (or
  vision-transcribes) it, seeds it, and switches to patch-only edits.

It can also accept **attachments** at any point — requirements docs (PDF/Word/
Markdown/CSV/XLSX), images, or pasted JSON/YAML — and `Read`s them to shortcut
the interview.

Within a run there are two states based on whether `<output_dir>/state/specs/`
or any generated asset exists yet:

1. **Interview** — Claude loads `resources/orchestrator/interview_agent.md` as
   its active persona (full, scoped, or `document_analysis.md` for a raw doc)
   and saves each `OperationSpec` to `state/specs/<op>.json`.

2. **Generation** — Claude runs the in-scope phases one-per-turn
   (infrastructure → lambda *(one per tool)* → openapi → prompt → contact flow →
   faq), loading the matching `resources/sub-agents/*.md` persona each phase,
   merging fragments, and running the deterministic gates before moving on.

Three non-LLM gates carry the webapp's safety net, all of them ports of what the
ECS backend runs:

| Script | Gate | When |
|---|---|---|
| `lint_assets.py` | per-asset syntax + import-safety, with auto-fixes (`--fix`) | after every generation phase |
| `validate_consistency.py` | 18 cross-asset checks | after Phase 3 and Phase 6 |
| `shape_parity.py` | spec ↔ OpenAPI shape parity (reviewer HARD GATE) | after Phase 3, again at Phase 7 |

`validate_consistency.py` covers the original spec↔asset checks (field names, HTTP
methods, path prefixes, GSI names, env vars, response wrapper, counts) **plus** the
IAM permission check (D2), the RDS Data API env/usage contract (D3) and the four SQL
checks against the scanned schema (D4/D5). All three take `<output_dir>`, accept
`--json`, and exit 1 on findings.

See `claude/SKILL.md` for the full workflow.

## Relationship to the webapp

| Webapp component | Skill equivalent |
|---|---|
| Orchestrator agent (Strands) | `SKILL.md` + Claude reading sub-agent prompts |
| Mode picker (Full / Segment / Improve) | `SKILL.md` "Mode selection" + `state/project.json.scope` |
| 8 specialized sub-agents | `resources/sub-agents/*.md` |
| `spec_manager.py` (Pydantic + S3) | JSON files in `<output_dir>/state/` + 14 JSON Schemas |
| Conversational attachments (multimodal) | `Read` on local file paths (images/PDF/docs natively) |
| `import_uploaded_asset_tool` / vision import | `SKILL.md` "Import an existing asset" + `resources/reference/vision_import.md` |
| `web_search` / `fetch_webpage` (AgentCore Gateway) | Native `WebSearch` / `WebFetch` |
| Contact-Flow RAG KB | `resources/reference/contact_flow_block_schemas.md` + WebSearch on docs.aws.amazon.com |
| `merge_*_fragments` + cfn-lint / OpenAPI gates | String-merge at anchor + `resources/scripts/lint_assets.py` |
| Fixed `update_q_session` Node.js Lambda | `resources/templates/update_q_session/index.js` (copied, not generated) |
| `workspace_file_tools.py` | Claude's `Read` / `Write` / `Edit` |
| `s3_asset_storage.py` | Local filesystem under `<output_dir>/assets/v1/` |
| `validate_consistency.py` (Strands tool) | `resources/scripts/validate_consistency.py` (stdlib + PyYAML) |
| `shape_parity.py` (reviewer HARD GATE) | `resources/scripts/shape_parity.py` |
| `asset_linters.py` (`@tool` wrappers over S3) | `resources/scripts/lint_assets.py` (auto-generated, wrappers stripped) |
| `introspect_database` / `convert_to_infrastructure_schema` | `SKILL.md` "PHASE 0 — existing database": AWS CLI scan or schema-as-document → `state/infrastructure_schema.json` |
| `<generation_state>` injected block | `state/progress.json`, re-read each turn |
| 4-phase / 12-step progress sidebar | Printed phase headers + ticking checklist |
| WebSocket streaming UI | Your normal streamed responses |
| Download-All ZIP + deploy modal | Files on disk + printed package tree + deploy.sh steps |
| Session versioning (v1/, v2/) | Same directory convention, local |

The generated artifacts are equivalent — same paths, same contracts.

## Re-syncing from the webapp

When you edit prompts in `backend/ecs/src/prompts/` or
`backend/ecs/src/agents/*/system_prompt.py`, change a Pydantic spec model, or
change a lint rule in `backend/ecs/src/tools/asset_linters.py`, re-run:

```bash
./scripts/extract_prompts.sh           # regenerate resources/
./scripts/extract_prompts.sh --check   # CI / pre-commit drift + coverage gate
```

**Auto-extracted — never hand-edit:**

| Output | Source |
|---|---|
| `resources/orchestrator/*.md` | `prompts/system_prompt.py`, `prompts/interview_agent_prompt.py` |
| `resources/sub-agents/*.md` | `agents/*/system_prompt.py` + `agents/_consistency_rules.py` |
| `resources/schemas/*.json` | `tools/spec_manager.py` (Pydantic → JSON Schema) |
| `resources/scripts/lint_assets.py` | `tools/asset_linters.py` — the `strands` import and every `@tool` wrapper (they need the S3/session runtime) are stripped, and `scripts/_lint_assets_cli.py` is appended as the CLI driver |

Extracting the linters rather than hand-copying them is deliberate: that module
carries ~1600 lines of API-verified Contact Flow block/error tables that change
often, and a stale copy in the skill would reject flows the webapp accepts.

The `--check` gate diffs against the backend **and** fails if a NEW prompt
section or spec model isn't covered by the extractor — so the skill can't
silently fall out of sync. Review the diff, commit, done.

> **Authored / hand-maintained:** `resources/reference/*`, `resources/templates/*`
> (incl. `update_q_session/index.js`, `deploy_workshop.sh`), `resources/examples/*`,
> `resources/scripts/{validate_consistency,shape_parity,check_spec_complete,clues_format}.py`,
> and both `SKILL.md` files. `validate_consistency.py` / `shape_parity.py` are hand
> ports of `tools/validate_consistency.py` / `tools/shape_parity.py` — when a check
> changes there, port it here and re-run the smoke tests:
>
> ```bash
> python3 scripts/smoke_test_validators.py   # requires PyYAML
> ```
>
> Also update by hand when
> `agents/contact_flow_generator/vision_import.py` or `tools/asset_packager.py`
> change.

## License

Inherits the MIT-0 license from the parent [AICC Builder](../../) project.
