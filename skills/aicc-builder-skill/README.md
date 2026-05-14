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
├── kiro/SKILL.md           # Kiro Skills entry point (same content, Kiro frontmatter)
└── resources/              # Shared payload — both SKILL.md files reference these
    ├── orchestrator/
    │   ├── system_prompt.md        # Full orchestrator prompt (fallback)
    │   └── interview_agent.md      # Interview persona
    ├── sub-agents/
    │   ├── _shared_rules.md        # Cross-generator golden rules
    │   ├── infrastructure_generator.md
    │   ├── lambda_generator.md
    │   ├── openapi_generator.md
    │   ├── prompt_generator.md
    │   ├── contact_flow_generator.md
    │   ├── faq_generator.md
    │   ├── research_agent.md
    │   └── reviewer_agent.md
    ├── schemas/
    │   ├── OperationSpec.schema.json
    │   ├── InfrastructureSpec.schema.json
    │   └── ... (7 more JSON Schemas)
    ├── scripts/
    │   ├── validate_consistency.py  # 9-check cross-asset validator
    │   ├── check_spec_complete.py   # Interview-completion gate
    │   └── clues_format.py          # CLUES response helper
    ├── templates/
    │   ├── pre_questionnaire_template.md
    │   └── deploy_workshop.sh
    └── examples/
        └── sample_*.md              # Complete + partial input examples
```

## Install (Claude Code)

Claude Code requires the skill's enclosing folder name to match the
`name:` in the SKILL.md frontmatter — so the install steps below rename
`claude/` → `aicc-builder/` and promote `SKILL.md` to the top of that
folder.

```bash
# Personal install
mkdir -p ~/.claude/skills
cp -r skills/aicc-builder-skill ~/.claude/skills/aicc-builder
mv ~/.claude/skills/aicc-builder/claude/SKILL.md ~/.claude/skills/aicc-builder/SKILL.md
rm -rf ~/.claude/skills/aicc-builder/claude ~/.claude/skills/aicc-builder/kiro

# Or project-scoped (commit to your repo)
mkdir -p .claude/skills
cp -r skills/aicc-builder-skill .claude/skills/aicc-builder
mv .claude/skills/aicc-builder/claude/SKILL.md .claude/skills/aicc-builder/SKILL.md
rm -rf .claude/skills/aicc-builder/claude .claude/skills/aicc-builder/kiro
```

Or use the one-shot installer:

```bash
skills/aicc-builder-skill/scripts/install.sh claude user      # ~/.claude/skills/
skills/aicc-builder-skill/scripts/install.sh claude project   # ./.claude/skills/
```

Then in Claude Code:
```
/skills
# you should see "aicc-builder" in the list
```

Trigger with: `/aicc-builder` or any of the natural-language triggers in
the frontmatter description.

## Install (Kiro)

```bash
# Kiro skills live at ~/.kiro/skills/ by default
mkdir -p ~/.kiro/skills
cp -r skills/aicc-builder-skill ~/.kiro/skills/aicc-builder
mv ~/.kiro/skills/aicc-builder/kiro/SKILL.md ~/.kiro/skills/aicc-builder/SKILL.md
rm -rf ~/.kiro/skills/aicc-builder/claude
```

## Requirements

- **Python 3.9+** for the bundled validator scripts
- **PyYAML** (`pip install pyyaml`) — the consistency validator parses
  the generated OpenAPI YAML
- That's it. No AWS credentials needed to run the skill itself — you only
  need them to `aws cloudformation deploy` the generated template.

## How it works

When you invoke the skill, Claude/Kiro reads `SKILL.md` and enters one of
two modes based on whether `<output_dir>/state/specs/` already has specs:

1. **Interview mode** — Claude loads
   `resources/orchestrator/interview_agent.md` as its active persona and
   conducts the 15-minute structured interview, saving each
   `OperationSpec` to `state/specs/<op>.json`.

2. **Generation mode** — Claude runs 6 phases in strict order
   (infrastructure → lambda → openapi → prompt → contact flow → faq).
   For each phase it loads the corresponding sub-agent prompt from
   `resources/sub-agents/` as its system prompt, reads the specs,
   writes the asset, and validates before moving on.

After Phase 3 and Phase 6, Claude runs
`resources/scripts/validate_consistency.py` which enforces the same 9
cross-asset rules the ECS webapp enforces (field names, HTTP methods,
path prefixes, GSI names, env vars, etc.).

See `claude/SKILL.md` for the full workflow.

## Relationship to the webapp

| Webapp component | Skill equivalent |
|---|---|
| Orchestrator agent (Strands) | `SKILL.md` + Claude reading sub-agent prompts |
| 9 specialized sub-agents | `resources/sub-agents/*.md` |
| `spec_manager.py` (Pydantic + S3) | JSON files in `<output_dir>/state/` + JSON Schemas |
| `workspace_file_tools.py` | Claude's `Read` / `Write` / `Edit` |
| `s3_asset_storage.py` | Local filesystem under `<output_dir>/assets/v1/` |
| `validate_consistency.py` (Strands tool) | `resources/scripts/validate_consistency.py` (stdlib + PyYAML) |
| WebSocket streaming UI | Plain text updates between phases |
| Session versioning (v1/, v2/) | Same directory convention, local |

The generated artifacts are byte-for-byte equivalent.

## Re-syncing from the webapp

When you edit prompts in `backend/ecs/src/prompts/` or
`backend/ecs/src/agents/*/system_prompt.py`, re-run:

```bash
./scripts/extract_prompts.sh
```

That regenerates `resources/orchestrator/*.md` and
`resources/sub-agents/*.md` from the Python source strings, and
regenerates `resources/schemas/*.json` from the Pydantic models. Review
the diff, commit, done.

## License

Inherits the MIT-0 license from the parent [AICC Builder](../../) project.
