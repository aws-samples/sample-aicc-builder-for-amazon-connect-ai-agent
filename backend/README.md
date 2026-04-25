# AICC Builder Backend

<div align="center">

**Multi-Agent AI Engine for Contact Center Asset Generation**

[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat&logo=python)](https://python.org/)
[![Strands SDK](https://img.shields.io/badge/Strands-Agents-purple?style=flat)](https://strandsagents.com/)
[![AWS](https://img.shields.io/badge/AWS-Bedrock-orange?style=flat&logo=amazonaws)](https://aws.amazon.com/bedrock/)

</div>

---

## Overview

The backend is a **multi-agent system** built with the
[Strands Agents SDK](https://strandsagents.com/) and running on **AWS
ECS Fargate** behind an ALB. One Orchestrator agent interviews the
customer and delegates generation work to 8 specialized sub-agents via
the Agent-as-a-Tool pattern.

> 📖 **Methodology:** how the system keeps customer requirements intact
> end-to-end — see [docs/agentic-ai.md](../docs/agentic-ai.md).
>
> 📖 **Other docs:**
> - [docs/architecture.md](../docs/architecture.md) — Runtime architecture, WebSocket, data flow
> - [docs/architecture-asset-flow.md](../docs/architecture-asset-flow.md) — Asset read/write paths
> - [docs/agents.md](../docs/agents.md) — Per-agent roles, tools, configs
> - [docs/development.md](../docs/development.md) — Local setup, deploy.sh reference

---

## Directory Structure

```
backend/
└── ecs/                              # ECS Fargate entry point (source of truth)
    ├── app.py                        # FastAPI (WebSocket + HTTP, Cognito JWT, SIGTERM)
    ├── Dockerfile                    # ARM64 Python 3.11, uvicorn
    ├── requirements.txt
    ├── healthcheck.py                # ALB health check endpoint
    ├── local-dev.sh                  # Local dev entry
    ├── tests/                        # Pytest unit tests
    └── src/
        ├── agents/                   # 9 specialized sub-agents
        │   ├── agent_pool.py            # Singleton warm instance management
        │   ├── streaming_handler.py     # Sub-agent streaming utilities
        │   ├── research_agent/          # Web search (Brave API)
        │   ├── faq_generator/           # Knowledge base documents
        │   ├── lambda_generator/        # Python Lambda code
        │   ├── openapi_generator/       # OpenAPI 3.0 spec (chunked)
        │   ├── prompt_generator/        # AI agent prompt YAML
        │   ├── contact_flow_generator/  # Contact Flow JSON + Mermaid
        │   ├── infrastructure_generator/# CloudFormation YAML (chunked)
        │   └── reviewer_agent/          # Cross-asset consistency audit
        ├── tools/                    # Shared utility tools
        │   ├── spec_manager.py          # OperationSpec CRUD + NFS sync
        │   ├── project_workspace.py     # NFS-backed project state
        │   ├── workspace_file_tools.py  # NFS file read/write/patch for agents
        │   ├── workspace_tools_for_subagent.py  # Patch-mode tools
        │   ├── validate_consistency.py  # Cross-asset validation (9 checks)
        │   ├── s3_asset_storage.py      # S3 + NFS dual-write
        │   ├── clues_format.py          # CLUES response format
        │   ├── merge_infrastructure.py  # Deterministic YAML merge
        │   ├── merge_openapi.py         # Deterministic OpenAPI merge
        │   ├── db_introspector.py       # DynamoDB/RDS schema discovery
        │   └── ...
        ├── context/                  # 3-tier session store
        │   ├── s3files_store.py         # memory → NFS → DynamoDB
        │   ├── shared_state.py          # Cross-agent shared state
        │   └── structured_notes.py      # Structured note-taking
        ├── prompts/
        │   ├── system_prompt.py         # Orchestrator system prompt (~60KB)
        │   └── prompt_loader.py         # Hot-reload from NFS
        └── templates/                # Questionnaire templates + samples
```

---

## Runtime

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  ALB (sticky,   │─────▶│  ECS Fargate     │─────▶│     Bedrock     │
│  4h idle)       │◀─────│  FastAPI + WS    │◀─────│  Claude Opus    │
└─────────────────┘      └──────────────────┘      └─────────────────┘
         ▲                        │
         │                        ▼
    Cognito JWT        ┌─────────────────────┐
                       │  /mnt/s3 (NFS)      │
                       │  sessions/ state/   │
                       │  assets/  prompts/  │
                       └─────────────────────┘
```

- **Entry point**: `backend/ecs/app.py`
- **Endpoints**: `/health` (ALB), `/ws` (WebSocket), session management HTTP
- **Session lifetime**: bounded by ALB idle timeout (4h) + auto-scaling
- **Persistence**: NFS (`/mnt/s3/`) for live state; S3 durable backup; DynamoDB for session index

---

## NFS Project Workspace

All structured state persists to NFS (backed by S3) at
`/mnt/s3/sessions/{session_id}/`. This survives WebSocket disconnects,
container restarts, and ALB failovers.

```
/mnt/s3/sessions/{session_id}/
├── state/                            # Structured state
│   ├── project.json                  # Industry, company, mode
│   ├── progress.json                 # Per-phase completion
│   ├── specs/{op_id}.json            # OperationSpec (the contract)
│   └── schemas/infrastructure.json   # Frozen CloudFormation schema summary
├── assets/v1/                        # Generated asset bundle
│   ├── lambda/{op_id}/*.py
│   ├── openapi/openapi.yaml
│   ├── prompt/ai_agent_prompt.yaml
│   ├── contact_flow/*.json
│   ├── infrastructure/template.yaml
│   └── faq/*.md
├── context/                          # Conversation + shared agent state
└── workspace/                        # Requirement fragments
```

**Data flow**: in-memory hot path → NFS (durable) → S3 (backup) → DynamoDB (session index).

---

## Agents Summary

| Agent | Role | Temp | Output |
|-------|------|------|--------|
| **Orchestrator** | Interview + delegation + validation | 0.7 | Streaming text |
| **Research** | Web search via Brave API | — | Structured findings |
| **FAQ Generator** | Knowledge base documents | — | Markdown + ZIP |
| **Lambda Generator** | Python Lambda per operation | 0.3 | Python code |
| **OpenAPI Generator** | API spec (chunked) | 0.3 | OpenAPI 3.0 YAML |
| **Prompt Generator** | AI agent prompt with scenario fidelity | 0.5 | Prompt YAML |
| **Contact Flow Generator** | Connect flows + Mermaid | 0.3 | Flow JSON |
| **Infrastructure Generator** | CloudFormation (chunked) | 0.3 | CFn YAML |
| **Reviewer** | Cross-asset consistency audit | — | Validation report |

### Generation Flow

```
Phase 1: Infrastructure            → emits schema summary (pinned)
Phase 2: Lambda + OpenAPI + FAQ    (parallel, read frozen schema)
Phase 3: Prompt                    → validate_parameter_consistency
Phase 4: Contact Flow
Phase 5: Reviewer Agent            (semantic audit of full bundle)
```

Each phase runs in its own LLM turn to keep the WebSocket responsive
and avoid request timeouts.

> 📖 Full agent details: [docs/agents.md](../docs/agents.md)

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BEDROCK_MODEL_ID` | Bedrock model identifier | `global.anthropic.claude-opus-4-6-v1` |
| `AWS_REGION` | AWS region | `ap-northeast-2` |
| `ASSETS_BUCKET_NAME` | S3 bucket for generated assets | (from CDK outputs) |
| `SESSION_STORE_BACKEND` | Session store (`s3files` \| `memory`) | `s3files` |
| `S3FILES_MOUNT_PATH` | NFS mount path | `/mnt/s3` |
| `BRAVE_API_KEY` | Brave Search API key (Research Agent) | (optional) |
| `CONTACT_FLOW_KB_ID` | Bedrock Knowledge Base ID for Contact Flow RAG | (optional) |

---

## Dependencies

Installed via `backend/ecs/requirements.txt`:

| Package | Purpose |
|---------|---------|
| strands-agents | AI agent framework |
| strands-agents-tools | Built-in tools |
| boto3 | AWS SDK |
| fastapi + uvicorn | HTTP + WebSocket server |
| pydantic | Data validation |
| PyYAML | YAML processing |
| requests | HTTP client (Brave Search) |

---

<div align="center">

**Built with [Strands Agents SDK](https://strandsagents.com/)**

</div>
