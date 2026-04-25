# Agents / 에이전트

[English](#english) · [한국어](#한국어)

---

<a id="english"></a>

## Overview

AICC Builder uses 9 agents: 1 Orchestrator + 8 sub-agents. The Orchestrator registers each sub-agent as a **tool** (Agent-as-a-Tool pattern via Strands SDK). When the Orchestrator calls a sub-agent tool, the SDK creates a nested agent invocation with its own system prompt, model, and tools.

All sub-agents share the same base model (`global.anthropic.claude-opus-4-6-v1`) via the Agent Pool, which caches model instances by `(temperature, max_tokens)` tuple.

```
Orchestrator (temp=0.7, 128K tokens)
├── research_agent           — Web research via Brave API
├── faq_generator_agent      — Knowledge base documents
├── infrastructure_generator_agent — CloudFormation YAML
├── lambda_generator_agent   — Python Lambda handlers
├── openapi_generator_agent  — OpenAPI 3.0 specs
├── prompt_generator_agent   — AI agent prompts
├── contact_flow_generator_agent — Contact Flow JSON
└── reviewer_agent           — Cross-asset validation
```

---

## Agent Pool

**File**: `backend/src/agents/agent_pool.py`

Pre-creates `Agent` instances as singletons at startup, keyed by agent type. Models are shared across agents with identical `(temperature, max_tokens)` configuration.

| Agent | Temperature | Max Tokens | Rationale |
|-------|------------|------------|-----------|
| lambda_generator | 0.3 | 128,000 | Deterministic code generation |
| openapi_generator | 0.3 | 128,000 | Structured API specification |
| prompt_generator | 0.5 | 128,000 | Creative yet consistent prompts |
| contact_flow_generator | 0.3 | 128,000 | Structured flow definitions |
| infrastructure_generator | 0.3 | 128,000 | Deterministic CloudFormation |
| interviewer (legacy) | 0.7 | 4,096 | Conversational, adaptive |

Agents are created **without tools** at initialization (to avoid circular imports). Tools are attached per-call via `get_agent_with_tools()`.

**Key features:**
- `cache_tools="default"` — Bedrock tool definition caching
- `cachePoint` in system prompt — Bedrock prompt caching
- `boto_client_config=Config(read_timeout=600)` — 10-minute read timeout
- `agent.messages = []` must be called before each reuse

---

## 1. Orchestrator

**File**: `backend/agentcore/agent.py` (v1) · `backend/ecs/app.py` (v2) · `backend/src/prompts/system_prompt.py`

The central agent that directly interviews users and delegates generation tasks to sub-agents.

### Responsibilities
- **Interview**: Gather requirements through conversation (company, industry, operations, scenarios)
- **Orchestrate**: Call sub-agents in the correct 5-phase sequence
- **Validate**: Run cross-asset consistency checks between phases
- **Restore**: Recover session state from S3 Project Workspace / NFS on reconnect

### Tools (Utility)
| Tool | Description |
|------|-------------|
| `save_operation_spec` | Persist operation specification (S3/NFS + in-memory) |
| `get_operation_spec` | Retrieve a single operation spec |
| `list_operations` | List all saved operations |
| `get_all_operation_ids` | Get exact list of all saved operation IDs |
| `introspect_database` | Connect to and analyze existing DB schema |
| `save_requirement_document` | Save large requirement text to S3/NFS |
| `load_requirement_document` | Load saved requirement text from S3/NFS |
| `validate_parameter_consistency` | Cross-asset field name validation |
| `replace_asset_field` | Deterministic field rename in assets (no LLM) |
| `merge_infrastructure_fragments` | Deterministic YAML merge (no LLM) |
| `merge_openapi_fragments` | Deterministic OpenAPI merge (no LLM) |
| `asset_lookup` | Retrieve generated assets from S3/NFS |
| `stream_fallback_asset` | Recovery for sub-agent parsing failures |

### Tools (Workspace File — v2/ECS only)

These tools are available when running in ECS mode with S3 Files NFS. They give agents direct file system access to the session workspace.

| Tool | Description |
|------|-------------|
| `read_workspace_file` | Read file contents from workspace (binary detection, size fallback) |
| `write_workspace_file` | Write/overwrite file (atomic via temp file + rename, auto-mkdir) |
| `append_workspace_file` | Append content to file (creates if missing) |
| `patch_workspace_file` | Find-and-replace in file (literal string, all occurrences, atomic) |
| `list_workspace_dir` | List directory entries with metadata (name, type, size) |
| `find_workspace_files` | Recursive glob pattern matching (max 200 results) |
| `grep_workspace` | Text/regex search across files (max 100 results, 1MB/file) |

All workspace file tools are sandboxed within `/mnt/s3/sessions/{session_id}/` and reject path traversal attempts.

### Tools (Sub-Agents)
Each sub-agent is registered as a tool on the Orchestrator.

### Model Config
- Temperature: 0.7 (conversational, adaptive)
- Max tokens: 128,000
- Streaming: Enabled

### Key Rules (from system prompt)
1. **Gather requirements directly** — no separate interviewer sub-agent
2. **Present as one unified assistant** — don't mention internal architecture
3. **Phase separation** — each generation phase in a separate LLM turn (prevents WS timeout)
4. **Large text auto-save** — requirements >500 chars saved to S3, mandatory reload before use
5. **Scenario extraction** — preserve customer's exact wording in conversation scripts
6. **Outbound auto-activation** — auto-enable customer_phone_lookup for outbound calls
7. **Modification triage** — on every user turn the system injects a
   `<modification_state>` block. The orchestrator classifies the request as
   **spec-level** (domain rule → `update_operation_spec` /
   `save_infrastructure_spec` / `save_session_flow_config` first, then
   confirm downstream-asset plan with user before re-dispatching sub-agents)
   vs **asset-level** (single-file patch). Asset keywords such as
   `flow` / `플로우` map to `contact_flow.json`, `프롬프트` to
   `ai_agent_prompt.yaml`, etc. If the same keyword is requested ≥2 times and
   the previous patch claimed success, the orchestrator stops patching and
   asks for disambiguation. See
   `backend/src/context/modification_tracking.py` and the
   `HANDLING USER MODIFICATIONS` section of `backend/src/prompts/system_prompt.py`.

---

## 2. Research Agent

**File**: `backend/src/agents/research_agent/`

Web research via Brave Search API. Gathers company information, FAQ content, and API documentation from the web.

### Tools
| Tool | Description |
|------|-------------|
| `brave_web_search` | Brave Search API queries |
| `fetch_webpage` | Fetch and parse web page content |
| `save_research_result` | Save research findings to S3 |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `research_request` | Yes | What to research |
| `company_name` | Yes | Company name |
| `company_url` | No | Company website URL |
| `session_id` | Yes | Session identifier |
| `orchestrator_context` | No | Additional context from orchestrator |
| `research_depth` | No | "light" (~2min), "standard" (~5min), "deep" (~10min) |

### Output
```json
{
  "success": true,
  "research_results": "...",
  "searches_performed": 5,
  "pages_fetched": 12
}
```

### Notes
- Optional workflow — only called when user explicitly requests research
- Results saved to S3 for `faq_generator_agent` to consume later
- Supports API/service research (e.g., "카카오톡 API 사양 조사")

---

## 3. FAQ Generator Agent

**File**: `backend/src/agents/faq_generator/`

Generates Knowledge Base FAQ documents from research results. Reads research data from S3 automatically — no need to pass research results.

### Tools
| Tool | Description |
|------|-------------|
| `save_faq_document` | Save individual FAQ document |
| `list_generated_documents` | List generated FAQ files |
| `create_knowledge_base_package` | Package FAQ docs into ZIP |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `company_name` | Yes | Company name |
| `session_id` | Yes | Session identifier |
| `output_format` | No | Output format (default: markdown) |
| `auto_package` | No | Auto-create ZIP package (default: true) |

### Output
```json
{
  "success": true,
  "documents_generated": 15,
  "package": {"zip_base64": "..."}
}
```

### Notes
- Must be called **after** `research_agent` — reads research from S3
- Generates Markdown documents. Default deployment target is an **S3 bucket
  registered as a Knowledge Source on the Amazon Connect AI agents domain**.
  Amazon Bedrock Knowledge Base is also compatible but only needed for
  orchestration-type agents with on-contact retrieval.
- ZIP package ready for direct upload

---

## 4. Infrastructure Generator Agent

**File**: `backend/src/agents/infrastructure_generator/`

Generates AWS CloudFormation YAML templates. Supports three modes for scalable generation.

### Tools
| Tool | Description |
|------|-------------|
| `save_generated_code` | Save CloudFormation YAML to S3 |
| `get_operation_spec` | Load operation spec |
| `get_all_specs` | Load all operation specs |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_name` | Yes | Project identifier |
| `industry` | Yes | Business industry |
| `mode` | Yes | "base", "operation", or "full" |
| `operation_id` | Conditional | Required for mode="operation" |
| `db_schema` | No | Existing DB schema JSON |
| `include_sample_data` | No | Generate sample DynamoDB data |
| `include_customer_phone_lookup` | No | Add customer lookup Lambda resources |
| `modification_request` | No | Change request for regeneration |

### Modes
- **`base`**: Generates shared infrastructure (DynamoDB, API Gateway, IAM, S3, sample data). Returns `schema_json` summary.
- **`operation`**: Generates a single operation fragment (Lambda + API GW resources). Called in parallel for each operation.
- **`full`**: Legacy mode — complete template in one call.

### Workflow
```
base → operation (parallel, one per op) → merge_infrastructure_fragments
```

### Output
1. CloudFormation YAML (code block)
2. Schema Summary JSON (table definitions, GSIs, environment variables, data conventions)

### Key Features
- Schema registry: stores infrastructure schema for other agents to auto-load
- Fragment registry: stores base + operation fragments for deterministic merge
- S3 fallback: loads schema from Project Workspace if not in memory
- Parallel mode: Fan-out/Fan-in pattern for scalable generation
- Sample data: Generates realistic DynamoDB seed data

---

## 5. Lambda Generator Agent

**File**: `backend/src/agents/lambda_generator/`

Generates Python Lambda handler files for each business operation.

### Tools
| Tool | Description |
|------|-------------|
| `save_generated_code` | Save Lambda code to S3 + stream to frontend |
| `get_operation_spec` | Load operation spec (auto-load) |
| `get_all_specs` | Load all operation specs |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `operation_id` | Yes | Operation identifier |
| `db_type` | No | "dynamodb" (default) or "rds-postgresql"/"rds-mysql" |
| `modification_request` | No | Change request for regeneration |
| `orchestrator_context` | No | Additional context (mock mode, external integrations) |

### Output
- Complete `index.py` with dual-mode support (API Gateway + direct invocation)
- DynamoDB integration with GSI queries
- Input validation, error handling, CORS headers
- Response structure matching OpenAPI spec (no `data` wrapper)

### Key Features
- **Auto-load**: Automatically loads operation spec and infrastructure schema
- **S3 fallback**: Falls back to S3 Project Workspace if in-memory spec not found
- **Batching**: Orchestrator calls up to 6 lambdas in parallel per turn
- **Modification**: Loads existing code from S3 and modifies in-place
- **Special Lambdas**: `customer_lookup` (Contact Flow direct invoke, STRING_MAP response)

---

## 6. OpenAPI Generator Agent

**File**: `backend/src/agents/openapi_generator/`

Generates OpenAPI 3.0 specifications with Amazon Connect MCP Gateway extensions.

### Tools
| Tool | Description |
|------|-------------|
| `save_generated_code` | Save OpenAPI YAML to S3 + stream |
| `get_operation_spec` | Load operation spec |
| `get_all_specs` | Load all operation specs |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `api_title` | Yes | API title |
| `api_description` | Yes | API description |
| `mode` | Yes | "full", "base", or "chunk" |
| `chunk_operations` | Conditional | JSON array of operation IDs (for mode="chunk") |
| `modification_request` | No | Change request for regeneration |

### Modes
- **`full`**: Complete spec in one call (for ≤6 operations)
- **`base`**: Shared structure (info, servers, security, ErrorResponse, anchors)
- **`chunk`**: Paths + schemas for a batch of 5-6 operations (parallel)

### Workflow (>6 operations)
```
base → chunk (parallel, 5-6 ops per chunk) → merge_openapi_fragments
```

### Key Features
- `x-amazon-connect-tool-name` and `x-amazon-connect-tool-description` extensions
- Response schemas match Lambda output (no `data` wrapper)
- YAML anchor/alias patterns for DRY schemas
- Chunked mode for scalable generation with deterministic merge

---

## 7. Prompt Generator Agent

**File**: `backend/src/agents/prompt_generator/`

Generates AI agent prompt YAML for Amazon Connect AI agents.
(Terminology note: the product was previously called "Amazon Q in Connect". SDK/
API namespaces such as `amazon-q-connect` and flow action `CreateWisdomSession`
still use the legacy names for backward compatibility — those identifiers are
intentionally untouched.)

### Tools
| Tool | Description |
|------|-------------|
| `save_generated_code` | Save prompt YAML to S3 + stream |
| `get_operation_spec` | Load operation spec |
| `get_all_specs` | Load all operation specs |
| `load_requirement_document` | Load conversation scripts from S3 |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_name` | Yes | AI agent persona name |
| `company_name` | Yes | Company name |
| `industry` | Yes | Business industry |
| `language` | Yes | Target language (from session context) |
| `orchestrator_context` | No | DTMF, auth flow, call direction |
| `modification_request` | No | Change request for regeneration |

### Output
Complete prompt YAML with:
- Agent persona and personality
- Tool usage guides (RETRIEVE, ESCALATE, COMPLETE, MCP tools)
- Conversation flow with scenario steps
- Business rules and guardrails
- Voice-optimized response patterns

### Key Features
- **Scenario fidelity**: Preserves customer's exact wording from conversation scripts
- **callType branching**: `{{$.Custom.callType}}` for multi-operation scenarios
- **Thinking patterns**: Emotion analysis, input validation, state tracking
- **Domain adaptation**: Tone/patterns adjusted per industry
- **Voice optimization**: Email/phone/tracking number reading patterns
- **S3 script loading**: Loads conversation scripts from S3 when they exceed context

---

## 8. Contact Flow Generator Agent

**File**: `backend/src/agents/contact_flow_generator/`

Generates Amazon Connect Contact Flow JSON and Mermaid diagrams.

### Tools
| Tool | Description |
|------|-------------|
| `save_generated_code` | Save flow JSON + Mermaid to S3 + stream |
| `get_operation_spec` | Load operation spec |
| `get_all_specs` | Load all operation specs |
| `brave_web_search` | Search AWS docs for block syntax |
| `fetch_webpage` | Fetch AWS documentation pages |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `flow_name` | Yes | Contact flow name |
| `company_name` | Yes | Company name |
| `language` | Yes | Target language |
| `contact_flow_requirements` | No | JSON with custom requirements |
| `enable_web_search` | No | Enable AWS docs search |
| `modification_request` | No | Change request |

### Output
1. Contact Flow JSON (Amazon Connect importable format)
2. Mermaid diagram (visual flow representation)

### Key Features
- **RAG retrieval**: Can search AWS documentation for correct block syntax
- **Customer lookup chain**: `enable-logging → customer-lookup → update-q-session → SetVoice → Lex`
- **Barge-in prevention**: `x-amz-lex:allow-interrupt:*:*: false` by default
- **Outbound support**: Campaign trigger, AMD detection
- **Deduplication**: One flow per direction (inbound/outbound)

---

## 9. Reviewer Agent

**File**: `backend/src/agents/reviewer_agent/`

Reviews all generated assets for cross-asset consistency and validates dependencies.

### Tools
| Tool | Description |
|------|-------------|
| `lookup_assets` | List assets for a session (metadata only) |
| `get_asset_content` | Load full content of a single asset |
| `list_operations` | List all saved operation specs |
| `get_operation_spec` | Get full spec for an operation |
| `validate_parameter_consistency` | Automated cross-asset validation |
| `validate_openapi_schema` | OpenAPI YAML syntax validation |
| `check_field_consistency` | Cross-reference field names |

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `session_id` | Yes | Session identifier |
| `review_scope` | No | "all", "lambda", "openapi", etc. |
| `language` | Yes | Report language |
| `focus_items` | No | JSON array for targeted re-review |

### Review Checklist
1. **Completeness**: Every operation has Lambda, OpenAPI path, Prompt reference
2. **OpenAPI**: YAML syntax, MCP extensions, schema quality
3. **Lambda**: Env vars, GSI names, input validation, error handling
4. **Cross-asset**: Field names consistent across Lambda/OpenAPI/Prompt
5. **Infrastructure**: Table/GSI references match Lambda code

### Key Features
- **Max 1 fix cycle**: Review → Fix → Re-review. No 3rd cycle.
- **Targeted re-review**: `focus_items` to check only fixed assets
- **False positive awareness**: Ignores intentional patterns (e.g., `ApiKeyRequired: false`)
- **Heartbeat**: Sends periodic heartbeats to keep WebSocket alive during long reviews

---

## Generation Flow

Each phase runs in a **separate LLM turn** to prevent WebSocket timeout (~3 min per turn).

```
Phase 1:  infrastructure_generator (mode="base")
Phase 1.5: infrastructure_generator (mode="operation") × N [parallel]
Phase 1.6: merge_infrastructure_fragments [deterministic]
          ↓
Phase 2:  lambda_generator × N [parallel, batches of 6]
          ↓
Phase 3a: openapi_generator (chunked if >6 ops)
Phase 3b: prompt_generator
Phase 3c: validate_parameter_consistency [mandatory]
          ↓
Phase 4:  contact_flow_generator [separate turn, RAG retrieval]
          ↓
Phase 5:  reviewer_agent [optional]
```

### Auto-Load Pattern

All sub-agents automatically load operation specs and infrastructure schema from the in-memory cache, with S3 Project Workspace fallback:

```python
# In-memory cache → S3 fallback
spec = get_all_specs().get(operation_id)
if not spec:
    workspace = get_workspace()
    if workspace:
        spec_dict = workspace.load_spec(operation_id)
```

### Progress Mapping

Progress updates are sent automatically when sub-agents start/complete:

| Sub-Agent | Progress ID |
|-----------|-------------|
| `infrastructure_generator_agent` | `cdk` |
| `lambda_generator_agent` | `lambda` |
| `openapi_generator_agent` | `openapi` |
| `prompt_generator_agent` | `prompt` |
| `contact_flow_generator_agent` | `contact_flow` |
| `faq_generator_agent` | `knowledge_base` |
| `reviewer_agent` | `review` |

---

<a id="한국어"></a>

## 개요

AICC Builder는 9개 에이전트를 사용합니다: 1개 Orchestrator + 8개 서브 에이전트. Orchestrator는 각 서브 에이전트를 **tool**로 등록하여 (Strands SDK의 Agent-as-a-Tool 패턴) 필요한 시점에 호출합니다.

모든 서브 에이전트는 Agent Pool을 통해 동일한 기본 모델(`global.anthropic.claude-opus-4-6-v1`)을 공유합니다.

---

## Agent Pool

**파일**: `backend/src/agents/agent_pool.py`

시작 시 `Agent` 인스턴스를 싱글톤으로 사전 생성합니다. 모델은 `(temperature, max_tokens)` 튜플로 캐시됩니다.

| 에이전트 | Temperature | Max Tokens | 용도 |
|---------|------------|------------|------|
| lambda_generator | 0.3 | 128,000 | 결정적 코드 생성 |
| openapi_generator | 0.3 | 128,000 | 구조화된 API 스펙 |
| prompt_generator | 0.5 | 128,000 | 창의적이면서 일관된 프롬프트 |
| contact_flow_generator | 0.3 | 128,000 | 구조화된 플로우 정의 |
| infrastructure_generator | 0.3 | 128,000 | 결정적 CloudFormation |

에이전트는 초기화 시 tools 없이 생성됩니다 (순환 임포트 방지). tools는 `get_agent_with_tools()` 호출 시 동적으로 연결됩니다.

---

## 에이전트 상세

### 1. Orchestrator
- **역할**: 사용자 인터뷰 + 서브 에이전트 조율
- **파일**: `backend/agentcore/agent.py` (v1), `backend/ecs/app.py` (v2), `backend/src/prompts/system_prompt.py`
- **유틸리티 도구**: `save_operation_spec`, `validate_parameter_consistency`, `replace_asset_field`, `merge_infrastructure_fragments` 등 13개
- **워크스페이스 파일 도구 (v2)**: `read_workspace_file`, `write_workspace_file`, `patch_workspace_file`, `append_workspace_file`, `list_workspace_dir`, `find_workspace_files`, `grep_workspace` — NFS 직접 파일 접근
- **핵심 규칙**: Phase 분리 (WS 타임아웃 방지), 대형 텍스트 S3/NFS 자동 저장, 시나리오 원문 보존
- **수정 요청 처리 (Modification Triage)**: 매 사용자 턴마다 시스템이 `<modification_state>` 블록을 주입해서 이번 턴의 키워드(예: `flow`, `프롬프트`) → 대상 에셋 매핑과 반복 카운터를 제공합니다. Orchestrator는 수정 요청을 (1) **spec-level**(데이터 모델/운영 시간/슬롯 단위/녹음/인사 멘트 등 — `update_operation_spec` 또는 `save_infrastructure_spec` / `save_session_flow_config` 먼저 실행 후 영향 받는 에셋 플랜을 사용자에게 컨펌받고 재생성) vs (2) **asset-level**(단일 파일 문구 패치) 로 분류합니다. 같은 키워드가 ≥ 2회 반복되고 직전 수정이 성공으로 기록되어 있으면 패치 대신 파일 disambiguation 질문을 합니다. 참고: `backend/src/context/modification_tracking.py`, `backend/src/prompts/system_prompt.py`의 `HANDLING USER MODIFICATIONS` 섹션.

### 2. Research Agent
- **역할**: Brave Search API로 웹 리서치
- **도구**: `brave_web_search`, `fetch_webpage`, `save_research_result`
- **선택적**: 사용자가 명시적으로 요청할 때만 호출

### 3. FAQ Generator
- **역할**: 리서치 결과로 Knowledge Base용 FAQ 문서 생성
- **도구**: `save_faq_document`, `create_knowledge_base_package`
- **전제**: `research_agent` 완료 후 호출 (S3에서 자동 로드)

### 4. Infrastructure Generator
- **역할**: CloudFormation YAML 생성 (DynamoDB, API Gateway, Lambda, IAM)
- **모드**: `base` → `operation` (병렬) → `merge` (결정적)
- **핵심**: Schema Summary JSON 생성 → 모든 후속 에이전트가 참조

### 5. Lambda Generator
- **역할**: 각 operation별 Python Lambda 핸들러 생성
- **배치**: 턴당 최대 6개 병렬
- **자동 로드**: operation spec + infrastructure schema (in-memory → S3 fallback)

### 6. OpenAPI Generator
- **역할**: OpenAPI 3.0 스펙 + MCP Gateway 확장 필드
- **모드**: `full` (≤6 ops), `base` → `chunk` (병렬) → `merge` (>6 ops)
- **핵심**: `x-amazon-connect-tool-*` 확장 필드 포함

### 7. Prompt Generator
- **역할**: AI 에이전트 프롬프트 YAML 생성
- **핵심**: 시나리오 충실도, callType 분기, 음성 최적화, 도메인 적응
- **S3 연동**: 대화 시나리오 원문을 S3에서 로드

### 8. Contact Flow Generator
- **역할**: Amazon Connect Contact Flow JSON + Mermaid 다이어그램
- **핵심**: RAG 검색 (AWS 문서), 고객 조회 체인, barge-in 방지
- **단독 실행**: RAG 검색 시간 소요로 별도 턴에서 실행

### 9. Reviewer Agent
- **역할**: 전체 에셋 교차 검증 (필드명 일관성, 참조 무결성)
- **도구**: `lookup_assets`, `get_asset_content`, `validate_parameter_consistency`
- **제한**: 최대 1회 수정 사이클 (Review → Fix → Re-review)

---

## 생성 흐름

각 Phase는 **별도 LLM 턴**으로 실행됩니다 (WebSocket 타임아웃 방지, 턴당 ~3분):

```
Phase 1:   인프라 (base) → 오퍼레이션 (병렬) → 병합 (결정적)
Phase 2:   Lambda (배치 6개씩 병렬)
Phase 3a:  OpenAPI (청크 모드 >6 ops)
Phase 3b:  프롬프트
Phase 3c:  정합성 검증 (필수)
Phase 4:   Contact Flow (단독, RAG 검색)
Phase 5:   리뷰 (선택)
```

### 자동 로드 패턴

모든 서브 에이전트는 operation spec과 infrastructure schema를 자동 로드합니다:
- 1차: in-memory 캐시
- 2차: S3 Project Workspace fallback

### 진행률 자동 매핑

서브 에이전트 시작/완료 시 자동으로 진행률 업데이트가 전송됩니다:

| 서브 에이전트 | 진행률 ID |
|-------------|-----------|
| `infrastructure_generator_agent` | `cdk` |
| `lambda_generator_agent` | `lambda` |
| `openapi_generator_agent` | `openapi` |
| `prompt_generator_agent` | `prompt` |
| `contact_flow_generator_agent` | `contact_flow` |
| `faq_generator_agent` | `knowledge_base` |
| `reviewer_agent` | `review` |
