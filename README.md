# AICC Builder

<div align="center">

**Turn a 15-minute AI conversation into a fully customized Amazon Connect PoC**

[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-2.x-orange?style=flat&logo=amazonaws)](https://aws.amazon.com/cdk/)
[![React](https://img.shields.io/badge/React-18.3-blue?style=flat&logo=react)](https://reactjs.org/)
[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat&logo=python)](https://python.org/)
[![Strands Agents](https://img.shields.io/badge/Strands-Agents-purple?style=flat)](https://strandsagents.com/)

[English](#english) · [한국어](#한국어) · [日本語](#日本語)

</div>

---

## Demo

**Part 1 — Build an Amazon Connect AI agent in ~1 hour**

https://github.com/user-attachments/assets/074983eb-43ed-4b04-a873-720795db847f

**Part 2 — Live call with the generated agent**

https://github.com/user-attachments/assets/64b4cd24-4653-4fed-86f9-4cd62866e1e2

---

<a id="english"></a>

## What is AICC Builder?

AICC Builder is an **open-source Agentic AI sample application** that generates a
customized Amazon Connect asset bundle (Lambda, OpenAPI, AI prompt,
Contact Flow, CDK infrastructure, FAQ) from a ~1 hour conversation
with a customer. A single Orchestrator agent interviews the user,
distills the conversation into a structured `OperationSpec`, and calls
specialized sub-agents to produce each asset — with deterministic
cross-asset validation between phases so the bundle is internally
consistent before it is delivered.

**Who is it for?** Anyone who wants to build Amazon Connect AI Agent 
fast in order to end with a deployable PoC for the customer's actual
business.

> 📖 **How it enforces customer requirements end-to-end:** see
> [docs/agentic-ai.md](./docs/agentic-ai.md) for the full methodology
> — OperationSpec-as-contract, 9-check deterministic validation,
> patch-only modification, and the NFS-backed workspace that keeps
> requirements durable across container restarts.

---

## Input → Output

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────────────┐
│                  │         │                  │         │                         │
│   💬 INPUT       │         │  🤖 AICC Builder  │         │   📦 OUTPUT              │
│                  │  ────▶  │                  │  ────▶  │                         │
│  AI Conversation │         │  9 Specialized   │         │  6 Production-Ready     │
│  (~15 min)       │         │  Agents          │         │  Asset Packages         │
│                  │         │                  │         │                         │
└─────────────────┘         └──────────────────┘         └─────────────────────────┘

  • Industry & company       Orchestrator             ✅ Lambda Functions
  • Business operations      Research Agent           ✅ OpenAPI Spec (MCP Gateway)
  • Rules & policies         FAQ Generator            ✅ AI Prompt
  • Conversation scenarios   Lambda Generator         ✅ Contact Flows
  • Tone & language          OpenAPI Generator        ✅ CDK Infrastructure
  • Edge cases               Prompt Generator         ✅ FAQ / Knowledge Base
                             Contact Flow Generator
                             Infrastructure Generator
                             Reviewer Agent
```

---

## Why?

| | Before (Standard Workshop) | After (With AICC Builder) |
|---|---|---|
| **Scenario** | Fixed hotel reservation demo | Customer's own business |
| **Assets** | Generic, pre-built | Auto-generated, customized |
| **Workshop outcome** | Educational knowledge | Deployable PoC |
| **Post-workshop value** | "That was interesting" | "We can pilot this next week" |
| **Time to PoC** | Weeks of hand-coding | 15-minute conversation |

---

## How It Works

### Step 1 — Deploy (~10 min)

```bash
git clone <repository-url> && cd aicc-builder
./deploy.sh
```

### Step 2 — Conversation (~15 min)

The customer chats with the AI agent through a web interface:

```
🤖  What industry is your company in?
👤  E-commerce — we sell electronics online.

🤖  What operations should your AI assistant handle?
👤  Order tracking, returns, and warranty claims.

🤖  What are your return policies?
👤  Auto-approve under $500, manager approval above. 30-day window.

🤖  Generating your customized assets...
```

### Step 3 — Generated Assets

The system produces a complete set of workshop-ready artifacts:

| Generated Asset | What It Is | Used In Workshop Module |
|---|---|---|
| **Lambda Functions** | Python handlers for each business operation (e.g., `process_return`, `track_order`) | Module 2: MCP Server Setup |
| **OpenAPI Spec** | API definitions for Amazon Connect MCP Gateway integration | Module 2: MCP Gateway |
| **AI Prompt** | Customized personality, tone, business rules, and guardrails | Module 2: AI Agent Prompt |
| **Contact Flows** | Amazon Connect flow configurations with visual Mermaid diagrams | Module 2: Flow Builder |
| **CDK Infrastructure** | Complete AWS CDK project (Lambda, API Gateway, DynamoDB) | Module 2: Deploy |
| **FAQ Documents** | Knowledge base articles for common customer questions | Module 3: Knowledge Base |

### Step 4 — Workshop

Customers use their generated assets throughout the workshop, ending with a **deployable PoC for their actual business**.

---

## Architecture

```
                        ┌──────────────────────────┐
                        │   CloudFront + S3         │
                        │   React Web App           │
                        └────────────┬─────────────┘
                                     │ WebSocket (Cognito JWT)
                        ┌────────────▼─────────────┐
                        │   ALB (idle 4h, sticky)   │
                        └────────────┬─────────────┘
                        ┌────────────▼─────────────┐
                        │   ECS Fargate (ARM64)     │
                        │   FastAPI + Uvicorn       │
                        │                           │
                        │   ┌───────────────────┐   │
                        │   │   Orchestrator    │   │
                        │   │   (Claude Sonnet) │   │
                        │   └───────┬───────────┘   │
                        │           │ Agent-as-Tool  │
                        │   ┌───────▼───────────┐   │
                        │   │  9 Sub-Agents     │   │
                        │   │  (specialized)    │   │
                        │   └───────────────────┘   │
                        │           │               │
                        │   ┌───────▼───────────┐   │
                        │   │  /mnt/s3 (NFS)    │   │
                        │   │  S3 Files Mount   │   │
                        │   └───────────────────┘   │
                        └────────────┬─────────────┘
                                     │
                 ┌───────────┬───────┼───────┬──────────┐
                 │           │       │       │          │
              DynamoDB    Bedrock    S3    Cognito   CloudWatch
                                    │                  (X-Ray)
                             ┌──────┴──────┐
                             │  S3 Files   │
                             │  (NFS)      │
                             └─────────────┘
```

Runtime highlights:

- Runtime: ECS Fargate (ARM64 Graviton) running FastAPI + Uvicorn
- WebSocket: ALB with Cognito JWT (sticky sessions, 4h idle timeout), proxied same-origin through CloudFront
- Session storage: 3-tier — in-memory → S3 Files NFS (`/mnt/s3/`) → DynamoDB
- File I/O: Direct NFS access via `/mnt/s3/` — agents read/write/patch files like a local filesystem
- Scaling: Auto-scaling (1–10 tasks) on the `ActiveWebSocketConnections` CloudWatch metric

**S3 Files NFS** (`/mnt/s3/`) provides direct file system access to S3, enabling:
- 3-tier session storage (in-memory → NFS → DynamoDB metadata)
- NFS-backed operation specs and fragment registries (survives container restarts)
- Asset versioning (v1/, v2/ on regeneration)
- System prompt hot-reload
- Workspace file tools for agents (read/write/patch files directly)

> For detailed architecture documentation, see [docs/architecture.md](./docs/architecture.md)

---

## Quick Start

### Prerequisites

AWS CLI 2.x (>= 2.34.27 for `s3files` support) · Node.js 18+ · Python 3.11+ · Docker · AWS CDK 2.x

### Deploy

```bash
git clone https://github.com/aws-samples/sample-aicc-builder-for-amazon-connect-ai-agent.git
cd aicc-builder

# Full deployment (default: Tokyo ap-northeast-1)
./deploy.sh

# Deploy to a different region
AWS_DEFAULT_REGION=us-east-1 ./deploy.sh

# Named stage (separate stacks, e.g. for staging alongside prod)
./deploy.sh --stage prod
AWS_DEFAULT_REGION=ap-northeast-2 ./deploy.sh --stage prod  # deploy prod stack to Seoul
```

**Selective deployment:**

```bash
./deploy.sh --backend-only    # Redeploy backend (ECS) only
./deploy.sh --frontend-only   # Rebuild + deploy frontend only
./deploy.sh --infra-only      # Redeploy CDK infrastructure only
./deploy.sh --force           # Force full rebuild (bypass hash checks)
```

> Full deploy.sh reference: [docs/development.md](./docs/development.md#deploysh-reference)

### Local Development

```bash
mkdir -p /tmp/s3files/sessions /tmp/s3files/prompts
export S3FILES_MOUNT_PATH=/tmp/s3files SESSION_STORE_BACKEND=s3files
cd backend/ecs && uvicorn app:app --port 8080
# wscat -c "ws://localhost:8080/ws?sessionId=test-1"
```

### Create Admin User

```bash
# UserPoolId is printed by deploy.sh
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username <email> \
  --user-attributes Name=email,Value=<email> \
  --temporary-password "TempPass123!" \
  --message-action SUPPRESS \
  --region ap-northeast-1
```

---

## Cost

| | Approximate Cost |
|---|---|
| **Per conversation session** | ~$1.55 (Bedrock tokens) |
| **Monthly infrastructure (idle)** | ~$45 (Fargate ~$25, ALB ~$10, DynamoDB ~$5, S3+CloudFront ~$5) |

---

## Tech Stack

| Layer | Technologies |
|---|---|
| **AI** | Strands Agents SDK · Claude (Bedrock) · Context Engineering (CLUES format) |
| **Frontend** | React 18 · TypeScript · Vite · Tailwind CSS · Zustand · Mermaid.js |
| **Backend** | Python 3.11 · FastAPI · Uvicorn · S3 Files NFS · DynamoDB |
| **Infra** | AWS CDK · CloudFront · Cognito · ECS Fargate · ALB · X-Ray |

---

## Project Structure

```
├── backend/
│   └── ecs/                     # ECS Fargate entry point (source of truth)
│       ├── app.py               # FastAPI (WebSocket + HTTP, Cognito JWT, SIGTERM)
│       ├── Dockerfile           # ARM64 Python 3.11, uvicorn
│       ├── requirements.txt
│       ├── healthcheck.py       # ALB health check
│       └── src/
│           ├── agents/              # 9 specialized sub-agents
│           │   ├── research_agent/      # Web search (Brave API)
│           │   ├── faq_generator/       # Knowledge base documents
│           │   ├── lambda_generator/    # Python Lambda handlers
│           │   ├── openapi_generator/   # OpenAPI 3.0 specs (chunked)
│           │   ├── prompt_generator/    # AI agent prompts
│           │   ├── contact_flow_generator/  # Connect flows + Mermaid
│           │   ├── infrastructure_generator/ # CloudFormation YAML (chunked)
│           │   └── reviewer_agent/      # Asset consistency validation
│           ├── tools/                   # Utility tools
│           │   ├── project_workspace.py     # NFS-backed state persistence
│           │   ├── spec_manager.py          # OperationSpec CRUD + NFS sync
│           │   ├── workspace_file_tools.py  # NFS file read/write/patch for agents
│           │   ├── workspace_tools_for_subagent.py  # Patch-mode tools for modification requests
│           │   ├── s3_asset_storage.py      # S3 + NFS dual-write asset storage
│           │   ├── clues_format.py          # CLUES response format (context engineering)
│           │   ├── validate_consistency.py  # Cross-asset validation (9 checks)
│           │   └── ...
│           ├── context/                 # Session context (3-tier s3files store)
│           │   ├── __init__.py
│           │   ├── s3files_store.py     # memory → NFS → DynamoDB
│           │   ├── shared_state.py      # Cross-agent shared state
│           │   └── structured_notes.py  # Structured note-taking
│           └── prompts/
│               ├── system_prompt.py     # Orchestrator system prompt (~60KB)
│               └── prompt_loader.py     # Hot-reload from NFS
├── frontend/                    # React 18 chat interface
│   └── src/
│       ├── components/          # UI components (mobile-responsive)
│       ├── hooks/               # useWebSocket (ALB + CloudFront same-origin)
│       ├── stores/              # authStore, builderStore, sessionStore
│       └── services/            # auth, sessions API
├── infrastructure/              # AWS CDK
│   └── lib/
│       ├── aicc-builder-stack.ts    # Main stack (Cognito, S3, CloudFront, DynamoDB)
│       ├── ecs-stack.ts             # ECS Fargate stack (VPC, ALB, Fargate, auto-scaling)
│       ├── knowledge-base-stack.ts  # Bedrock Knowledge Base (optional; default FAQ path is S3 + Connect AI agents domain)
│       └── app.ts                   # CDK entry point
├── docs/                        # Detailed documentation
└── deploy.sh                    # Full deployment pipeline
```

---

## Runtime Details

### S3 Files NFS Mount Layout

```
/mnt/s3/
  sessions/{session_id}/
    state/          # project.json, progress.json, specs/*.json, schemas/
    assets/v1/      # lambda/, openapi/, prompt/, contact_flow/, infrastructure/, faq/
    assets/v2/      # On regeneration
    context/        # conversation_history.json, shared_state.json, all_results.txt
    workspace/      # requirements/, fragments/
  prompts/          # Hot-reloadable system prompts
  config/           # Hot-reloadable model config
```

### Key Capabilities

- **Graceful Shutdown**: SIGTERM flushes active sessions to S3 Files, closes WebSocket with 1001
- **Auto-scaling**: Step scaling on `ActiveWebSocketConnections` CloudWatch metric (1–10 tasks)
- **Observability**: Container Insights + X-Ray sidecar
- **Workspace File Tools**: Agents can directly read/write/patch files on NFS (like a local file system)
- **Patch-only Modifications**: When an asset is regenerated via `modification_request`, sub-agents must use workspace tools (`read_current_file`, `patch_file`) to make minimal edits — full-file regeneration is refused
- **Fragment Registry**: NFS-backed for infrastructure and OpenAPI generators — survives container restarts
- **Context Engineering**: CLUES response format reduces sub-agent token consumption; SummarizingConversationManager for long-running agents

---

## Documentation

| Doc | Description |
|---|---|
| [docs/agentic-ai.md](./docs/agentic-ai.md) | **How the multi-agent system keeps customer requirements intact end-to-end** — OperationSpec contract, deterministic validation, patch-only modification |
| [docs/architecture.md](./docs/architecture.md) | Runtime architecture, WebSocket protocol, data flow |
| [docs/architecture-asset-flow.md](./docs/architecture-asset-flow.md) | Asset read/write/stream paths, dual-write to NFS + S3 |
| [docs/agents.md](./docs/agents.md) | 9 agents: roles, tools, model configs, generation sequence |
| [docs/development.md](./docs/development.md) | Local setup, adding agents, deploy.sh reference, debugging |
| [backend/README.md](./backend/README.md) | Backend overview and directory structure |
| [frontend/README.md](./frontend/README.md) | Frontend components and state management |
| [infrastructure/README.md](./infrastructure/README.md) | CDK stacks, resources, and deployment |

---

## Security

> ⚠️ **Disclaimer — workshop / demo tool, not production**
>
> AICC Builder is intended as a **workshop and proof-of-concept generator**
> for anyone who wants to build a self-service AI agent on Amazon Connect fast
> — in about an hour. The assets it produces (Lambda code, CloudFormation
> templates, prompts, Contact Flows, FAQ docs) are **starting points**, not
> hardened production artifacts, and are generated by a large language model —
> you must review them before deploying to any environment that handles real
> customer data.
>
> Specifically, before using generated output in production you should, at a
> minimum:
> - Review and tighten IAM roles, security groups, and resource policies
>   produced by the Infrastructure Generator.
> - Rotate/scope the API Gateway API Key (workshop templates deliberately set
>   `ApiKeyRequired: false` on methods for simplicity).
> - Enable server-side access logging, object versioning, and lifecycle rules
>   on any S3 buckets that store customer data (the app's own buckets are
>   configured with `blockPublicAccess: BLOCK_ALL` and `enforceSSL: true`;
>   S3 server access logging is **not** enabled by default and should be
>   turned on for production).
> - Validate generated Lambda code against your organization's secure coding
>   standards (input validation, secrets handling, dependency scanning).
> - Enable AWS WAF / throttling on the public ALB + CloudFront distribution.
> - Review the generated AI prompt for prompt-injection resistance against
>   your specific threat model.
>
> The app itself stores conversation transcripts and generated assets in S3
> (via S3 Files NFS) and DynamoDB within the deploying AWS account. Do not
> enter real PII or confidential data during a workshop session.

To report a security issue in **AICC Builder itself**, see
[CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications).

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

## Contributing

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

---

<a id="한국어"></a>

# 한국어

## AICC Builder란?

AICC Builder는 **오픈소스 Agentic AI 애플리케이션**으로, 고객과의 약 1시간
대화만으로 Amazon Connect 맞춤형 자산 번들(Lambda, OpenAPI, AI 프롬프트,
Contact Flow, CDK 인프라, FAQ)을 자동 생성합니다. 단일 오케스트레이터
에이전트가 고객을 인터뷰하고, 대화를 구조화된 `OperationSpec`으로 증류한 뒤,
각 자산을 담당하는 전문 서브 에이전트를 호출합니다. 단계 사이마다 **결정론적
교차 자산 검증**이 실행되어, 전달 전 번들의 내부 일관성이 보장됩니다.

**대상**: Amazon Connect의 셀프 서비스 AI Agent를 빠르게 만들어보고자 하는 사람 누구나. 
데모가 아닌 **고객 실제 비즈니스에 배포 가능한 PoC**로 만들고 싶은 경우.

> 📖 **고객 요구사항을 끝까지 지키는 메커니즘**은
> [docs/agentic-ai.md](./docs/agentic-ai.md)를 참조하세요. OperationSpec을
> 계약으로 삼는 방식, 9개 결정론적 검증, 패치 전용 수정, 컨테이너 재시작을
> 견디는 NFS workspace 구조를 자세히 설명합니다.

---

## 입력 → 출력

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────────────┐
│                  │         │                  │         │                         │
│   💬 입력        │         │  🤖 AICC Builder  │         │   📦 출력                │
│                  │  ────▶  │                  │  ────▶  │                         │
│  AI 대화         │         │  9개 전문 에이전트 │         │  6종 프로덕션 에셋       │
│  (~15분)         │         │                  │         │                         │
│                  │         │                  │         │                         │
└─────────────────┘         └──────────────────┘         └─────────────────────────┘

  • 업종 및 회사 정보         오케스트레이터          ✅ Lambda 함수
  • 업무 프로세스             리서치 에이전트        ✅ OpenAPI 스펙 (MCP Gateway)
  • 비즈니스 룰/정책          FAQ 생성기            ✅ AI 프롬프트
  • 대화 시나리오             Lambda 생성기         ✅ Contact Flow
  • 톤앤매너/언어             OpenAPI 생성기        ✅ CDK 인프라
  • 예외 케이스              프롬프트 생성기        ✅ FAQ / 지식 베이스
                             Contact Flow 생성기
                             인프라 생성기
                             리뷰어 에이전트
```

---

## 왜 필요한가?

| | 기존 워크숍 | AICC Builder 적용 후 |
|---|---|---|
| **시나리오** | 고정된 호텔 예약 데모 | 고객 실제 비즈니스 |
| **에셋** | 범용, 사전 제작 | 자동 생성, 맞춤형 |
| **워크숍 결과** | 교육적 이해 | 배포 가능한 PoC |
| **워크숍 후 반응** | "재미있었네요" | "다음 주에 파일럿 해봅시다" |
| **PoC까지 소요 시간** | 수 주 수작업 | 15분 대화 |

---

## 빠른 시작

### 사전 요구사항

AWS CLI 2.x (>= 2.34.27, `s3files` 지원) · Node.js 18+ · Python 3.11+ · Docker · AWS CDK 2.x

### 배포

```bash
git clone <repository-url>
cd aicc-builder

# 전체 배포 (기본: 도쿄 ap-northeast-1)
./deploy.sh

# 다른 리전에 배포 (예: 서울)
AWS_DEFAULT_REGION=ap-northeast-2 ./deploy.sh

# 이름이 있는 스테이지로 배포 (prod, staging 등)
./deploy.sh --stage prod
```

**부분 배포:**

```bash
./deploy.sh --backend-only    # ECS 백엔드만 재배포
./deploy.sh --frontend-only   # 프론트엔드만 빌드/배포
./deploy.sh --infra-only      # CDK 인프라만 재배포
./deploy.sh --force           # 해시 무시하고 강제 전체 재빌드
```

**리전 선택:** 모든 스택이 동일 리전에 배포됩니다. 기본 리전은 도쿄(`ap-northeast-1`)이며, `AWS_DEFAULT_REGION`으로 변경 가능합니다.

### 로컬 개발

```bash
mkdir -p /tmp/s3files/sessions /tmp/s3files/prompts
export S3FILES_MOUNT_PATH=/tmp/s3files SESSION_STORE_BACKEND=s3files
cd backend/ecs && uvicorn app:app --port 8080
# wscat -c "ws://localhost:8080/ws?sessionId=test-1"
```

---

## 비용

| | 대략적인 비용 |
|---|---|
| **대화 세션당** | ~$1.55 (Bedrock 토큰) |
| **월 인프라 (유휴)** | ~$45 (Fargate ~$25, ALB ~$10, DynamoDB ~$5, S3+CF ~$5) |

---

## 기술 스택

| 레이어 | 기술 |
|---|---|
| **AI** | Strands Agents SDK · Claude (Bedrock) · Context Engineering (CLUES 형식) |
| **프론트엔드** | React 18 · TypeScript · Vite · Tailwind CSS · Zustand · Mermaid.js |
| **백엔드** | Python 3.11 · FastAPI · Uvicorn · S3 Files NFS · DynamoDB |
| **인프라** | AWS CDK · CloudFront · Cognito · ECS Fargate · ALB · X-Ray |

---

## 런타임 상세

### S3 Files NFS 마운트 구조

```
/mnt/s3/
  sessions/{session_id}/
    state/          # project.json, progress.json, specs/*.json, schemas/
    assets/v1/      # lambda/, openapi/, prompt/, contact_flow/, infrastructure/, faq/
    assets/v2/      # 재생성 시
    context/        # conversation_history.json, shared_state.json, all_results.txt
    workspace/      # requirements/, fragments/
  prompts/          # 핫리로드 가능한 시스템 프롬프트
  config/           # 핫리로드 가능한 모델 설정
```

### 주요 기능

- **Graceful Shutdown**: SIGTERM 시 활성 세션을 S3 Files에 플러시, WebSocket 1001 코드로 종료
- **오토스케일링**: `ActiveWebSocketConnections` CloudWatch 메트릭 기반 (1–10 태스크)
- **관측성**: Container Insights + X-Ray 사이드카
- **워크스페이스 파일 도구**: 에이전트가 NFS 파일을 직접 읽기/쓰기/패치 가능
- **Patch-only 수정**: `modification_request`로 에셋 재생성 시, 서브 에이전트는 workspace 도구(`read_current_file`, `patch_file`)로 최소한의 변경만 수행합니다. 전체 파일 재생성은 거부됩니다.
- **Fragment Registry**: 인프라/OpenAPI 생성기의 프래그먼트를 NFS에 백업 — 컨테이너 재시작 시 복원
- **Context Engineering**: CLUES 응답 형식으로 서브에이전트 토큰 소모 절감; SummarizingConversationManager로 장기 실행 에이전트 보호

---

## 보안

> ⚠️ **고지 — 워크숍/데모 도구이며 프로덕션용이 아닙니다**
>
> AICC Builder는 Amazon Connect 위에 셀프서비스 AI 에이전트를 빠르게
> (약 1시간 안에) 만들고자 하는 누구나 사용할 수 있는 **워크숍 및 PoC 생성기**
> 입니다. 생성되는 에셋(Lambda 코드, CloudFormation 템플릿, 프롬프트,
> Contact Flow, FAQ)은 **시작점**일 뿐 프로덕션 수준의 아티팩트가 아니며,
> LLM으로 생성되므로 실제 고객 데이터가 있는 환경에 배포하기 전에 반드시
> 검토해야 합니다.
>
> 생성된 산출물을 프로덕션에 사용하기 전 최소한 다음을 수행하십시오:
> - 인프라 제너레이터가 만든 IAM 역할, 보안 그룹, 리소스 정책을 검토·축소
> - API Gateway API Key 로테이션 및 범위 재조정 (워크숍 템플릿은 단순화를 위해
>   의도적으로 `ApiKeyRequired: false`로 설정됨)
> - 고객 데이터를 담는 S3 버킷에 서버 액세스 로깅, 오브젝트 버저닝, 수명주기
>   정책을 활성화 (앱 자체 버킷은 `blockPublicAccess: BLOCK_ALL`,
>   `enforceSSL: true`로 설정되어 있으나 **S3 server access logging은
>   기본값으로 비활성화**되어 있으며 프로덕션에서는 켜야 합니다)
> - 생성된 Lambda 코드를 조직의 보안 코딩 표준(입력 검증, 시크릿 처리,
>   의존성 스캔)에 맞춰 검증
> - 공개 ALB + CloudFront에 AWS WAF / 스로틀링 적용
> - 생성된 AI 프롬프트를 조직의 위협 모델에 맞춰 프롬프트 인젝션 저항성 검토
>
> 앱 자체는 대화 기록과 생성 에셋을 배포 대상 AWS 계정의 S3 (S3 Files NFS) 및
> DynamoDB에 저장합니다. 워크숍 세션 중 실제 PII/기밀 데이터를 입력하지 마십시오.

**AICC Builder 자체**의 보안 이슈 신고는
[CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)을 참조하세요.

## 라이선스

이 라이브러리는 MIT-0 라이선스를 따릅니다. [LICENSE](LICENSE) 파일을 참조하세요.

## 기여

자세한 내용은 [CONTRIBUTING](CONTRIBUTING.md)을 참조하세요.

---

<a id="日本語"></a>

# 日本語

## AICC Builderとは？

AICC Builderは、お客様との約1時間の会話から、Amazon Connectのカスタマイズされた
資産バンドル（Lambda、OpenAPI、AIプロンプト、Contact Flow、CDKインフラ、FAQ）を
自動生成する**オープンソースのAgentic AIサンプルアプリケーション**です。
1つのオーケストレーターエージェントがお客様にインタビューし、会話を構造化された
`OperationSpec`に蒸留した上で、各資産を生成する専門サブエージェントを呼び出します。
フェーズ間で**決定論的なクロスアセット検証**を実行し、納品前にバンドルの
内部一貫性を保証します。

**対象**: Amazon Connect AI Agentを高速に構築し、お客様の実ビジネスに
デプロイ可能なPoCに仕上げたい方。

> 📖 **お客様の要件をエンドツーエンドでどう守るか:**
> [docs/agentic-ai.md](./docs/agentic-ai.md) で全方法論を解説しています。
> OperationSpecを契約として扱う方式、9種の決定論的検証、パッチ専用修正、
> コンテナ再起動を超えて要件を保持するNFSベースのworkspace構造などを
> 詳しく説明しています。

---

## 入力 → 出力

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────────────┐
│                  │         │                  │         │                         │
│   💬 入力         │         │  🤖 AICC Builder  │         │   📦 出力                │
│                  │  ────▶  │                  │  ────▶  │                         │
│  AI 会話         │         │  9種の専門         │         │  6種のプロダクション     │
│  (~15分)         │         │  エージェント       │         │  対応資産パッケージ       │
│                  │         │                  │         │                         │
└─────────────────┘         └──────────────────┘         └─────────────────────────┘

  • 業種・会社情報             オーケストレーター        ✅ Lambda 関数
  • 業務オペレーション           リサーチエージェント       ✅ OpenAPI スペック (MCP Gateway)
  • ルール・ポリシー             FAQ ジェネレーター        ✅ AI プロンプト
  • 会話シナリオ                Lambda ジェネレーター      ✅ Contact Flow
  • トーン・言語                 OpenAPI ジェネレーター     ✅ CDK インフラ
  • エッジケース                 プロンプトジェネレーター     ✅ FAQ / ナレッジベース
                              Contact Flow ジェネレーター
                              インフラジェネレーター
                              レビューエージェント
```

---

## なぜ必要か？

| | 従来のワークショップ | AICC Builder適用後 |
|---|---|---|
| **シナリオ** | 固定のホテル予約デモ | お客様の実ビジネス |
| **アセット** | 汎用、事前作成 | 自動生成、カスタマイズ |
| **ワークショップの成果** | 教育的な知識 | デプロイ可能なPoC |
| **ワークショップ後の価値** | 「面白かったです」 | 「来週パイロットしましょう」 |
| **PoCまでの時間** | 数週間の手作業 | 15分の対話 |

---

## 仕組み

### Step 1 — デプロイ（約10分）

```bash
git clone <repository-url> && cd aicc-builder
./deploy.sh
```

### Step 2 — 対話（約15分）

お客様はWebインターフェース上でAIエージェントと対話します:

```
🤖  御社の業種を教えてください。
👤  Eコマースです。電子製品をオンライン販売しています。

🤖  AIアシスタントが扱うべき業務は何ですか？
👤  注文追跡、返品、保証請求の3つです。

🤖  返品ポリシーはどうなっていますか？
👤  500ドル未満は自動承認、それ以上はマネージャー承認。期間は30日です。

🤖  カスタマイズされた資産を生成中です...
```

### Step 3 — 生成される資産

システムは、ワークショップですぐ使える完全な成果物セットを生成します:

| 生成資産 | 内容 | 利用ワークショップモジュール |
|---|---|---|
| **Lambda 関数** | 各業務オペレーション用のPythonハンドラー（例: `process_return`, `track_order`） | Module 2: MCP Server Setup |
| **OpenAPI スペック** | Amazon Connect MCP Gateway連携用のAPI定義 | Module 2: MCP Gateway |
| **AI プロンプト** | カスタマイズされたペルソナ、トーン、ビジネスルール、ガードレール | Module 2: AI Agent Prompt |
| **Contact Flow** | Amazon Connectフロー設定 + Mermaidビジュアル図 | Module 2: Flow Builder |
| **CDK インフラ** | 完全なAWS CDKプロジェクト（Lambda、API Gateway、DynamoDB） | Module 2: Deploy |
| **FAQ ドキュメント** | 顧客の頻出質問に対するナレッジベース記事 | Module 3: Knowledge Base |

### Step 4 — ワークショップ

お客様はワークショップを通じて生成済み資産を使用し、最終的に**自社の実ビジネスに
デプロイ可能なPoC**を手にします。

---

## 🇯🇵 日本向けデプロイガイド

### リージョンとサービス対応状況

| サービス | 東京リージョン (ap-northeast-1) | 備考 |
|---------|-------------------------------|------|
| **Amazon Bedrock (Claude)** | ✅ 利用可能 | `global.anthropic.claude-opus-4-6-v1` でクロスリージョン推論を使用 |
| **Amazon Connect** | ✅ 利用可能 | 東京リージョンでインスタンス作成可能 |
| **Amazon Polly (日本語)** | ✅ 利用可能 | Takumi (男性)、Kazuha (女性)、Tomoko (女性) |
| **ECS Fargate (Graviton)** | ✅ 利用可能 | ARM64 で高コスパ運用 |
| **S3 Files** | ✅ 利用可能 | NFS マウントによるセッション永続化 |
| **Bedrock Knowledge Base** | ✅ 利用可能 | Contact Flow RAG 用 |

### 日本語 TTS 音声の選択

生成されるContact Flowでは、Amazon Pollyの日本語音声が自動設定されます:

| 音声名 | 性別 | エンジン | 推奨用途 |
|--------|------|---------|---------|
| **Takumi** | 男性 | Generative | ビジネス向け（デフォルト） |
| **Kazuha** | 女性 | Generative | カスタマーサービス向け |
| **Tomoko** | 女性 | Neural | 標準的な案内 |

### 日本語対話の特徴

- **敬語（です/ます調）** をデフォルトで使用
- 日本の電話番号形式（090-XXXX-XXXX、03-XXXX-XXXX）を自動認識
- 日本の住所形式（都道府県→市区町村→番地）に対応
- 円（¥）表記、小数点なし
- タイムゾーン: Asia/Tokyo (JST, UTC+9)
- 業務時間の典型例: 09:00-17:30 JST

### 多言語対応

UIの言語は3つから選択可能です（フロントエンド右上のメニュー）:
- 🇯🇵 日本語（デフォルト）
- 🇺🇸 English
- 🇰🇷 한국어

生成されるアセット（プロンプト、FAQ、Contact Flow）の言語は、対話中にお客様が
使用する言語に自動適応します。

---

## アーキテクチャ

```
                        ┌──────────────────────────┐
                        │   CloudFront + S3         │
                        │   React Web アプリ         │
                        └────────────┬─────────────┘
                                     │ WebSocket (Cognito JWT)
                        ┌────────────▼─────────────┐
                        │   ALB (idle 4h, sticky)   │
                        └────────────┬─────────────┘
                        ┌────────────▼─────────────┐
                        │   ECS Fargate (ARM64)     │
                        │   FastAPI + Uvicorn       │
                        │                           │
                        │   ┌───────────────────┐   │
                        │   │  オーケストレーター   │   │
                        │   │  (Claude Sonnet)  │   │
                        │   └───────┬───────────┘   │
                        │           │ Agent-as-Tool  │
                        │   ┌───────▼───────────┐   │
                        │   │  9 サブエージェント │   │
                        │   │  (専門化)          │   │
                        │   └───────────────────┘   │
                        │           │               │
                        │   ┌───────▼───────────┐   │
                        │   │  /mnt/s3 (NFS)    │   │
                        │   │  S3 Files マウント  │   │
                        │   └───────────────────┘   │
                        └────────────┬─────────────┘
                                     │
                 ┌───────────┬───────┼───────┬──────────┐
                 │           │       │       │          │
              DynamoDB    Bedrock    S3    Cognito   CloudWatch
                                    │                  (X-Ray)
                             ┌──────┴──────┐
                             │  S3 Files   │
                             │  (NFS)      │
                             └─────────────┘
```

ランタイムのハイライト:

- ランタイム: ECS Fargate (ARM64 Graviton) で FastAPI + Uvicorn を実行
- WebSocket: Cognito JWT認証付きALB（スティッキーセッション、4時間アイドルタイムアウト）、CloudFrontで同一オリジンプロキシ
- セッションストレージ: 3階層 — インメモリ → S3 Files NFS (`/mnt/s3/`) → DynamoDB
- ファイルI/O: `/mnt/s3/` への直接NFSアクセス — エージェントはローカルファイルシステムのようにread/write/patch
- スケーリング: `ActiveWebSocketConnections` CloudWatchメトリクスに基づくオートスケーリング（1〜10タスク）

**S3 Files NFS** (`/mnt/s3/`) はS3への直接ファイルシステムアクセスを提供し、以下を実現します:
- 3階層セッションストレージ（インメモリ → NFS → DynamoDBメタデータ）
- NFSバックアップされたOperationSpec / Fragment Registry（コンテナ再起動を超えて永続）
- アセットのバージョニング（再生成時に v1/, v2/）
- システムプロンプトのホットリロード
- エージェント用ワークスペースファイルツール（直接ファイルread/write/patch）

> 詳細なアーキテクチャドキュメント: [docs/architecture.md](./docs/architecture.md)

---

## クイックスタート

### 前提条件

AWS CLI 2.x（`s3files`サポートには 2.34.27 以上）· Node.js 18+ · Python 3.11+ · Docker · AWS CDK 2.x

### デプロイ

```bash
git clone https://github.com/aws-samples/sample-aicc-builder-for-amazon-connect-ai-agent.git
cd aicc-builder

# フルデプロイ（デフォルト: 東京 ap-northeast-1）
./deploy.sh

# 別リージョンへのデプロイ
AWS_DEFAULT_REGION=us-east-1 ./deploy.sh

# 名前付きステージ（prodと並行してstaging用などに別スタック）
./deploy.sh --stage prod
AWS_DEFAULT_REGION=ap-northeast-2 ./deploy.sh --stage prod  # prodスタックをソウルに
```

**選択的デプロイ:**

```bash
./deploy.sh --backend-only    # バックエンド (ECS) のみ再デプロイ
./deploy.sh --frontend-only   # フロントエンドの再ビルド + デプロイのみ
./deploy.sh --infra-only      # CDKインフラのみ再デプロイ
./deploy.sh --force           # ハッシュチェックを無視してフルリビルド
```

> deploy.sh の完全リファレンス: [docs/development.md](./docs/development.md#deploysh-reference)

### ローカル開発

```bash
mkdir -p /tmp/s3files/sessions /tmp/s3files/prompts
export S3FILES_MOUNT_PATH=/tmp/s3files SESSION_STORE_BACKEND=s3files
cd backend/ecs && uvicorn app:app --port 8080
# wscat -c "ws://localhost:8080/ws?sessionId=test-1"
```

### 管理者ユーザーの作成

```bash
# UserPoolId は deploy.sh の出力に表示されます
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username <email> \
  --user-attributes Name=email,Value=<email> \
  --temporary-password "TempPass123!" \
  --message-action SUPPRESS \
  --region ap-northeast-1
```

---

## コスト

| | おおよそのコスト |
|---|---|
| **会話セッション 1回あたり** | ~$1.55 (Bedrockトークン) |
| **月額インフラ（アイドル時）** | ~$45 (Fargate ~$25, ALB ~$10, DynamoDB ~$5, S3+CloudFront ~$5) |

---

## 技術スタック

| レイヤー | 技術 |
|---|---|
| **AI** | Strands Agents SDK · Claude (Bedrock) · Context Engineering (CLUES形式) |
| **フロントエンド** | React 18 · TypeScript · Vite · Tailwind CSS · Zustand · Mermaid.js |
| **バックエンド** | Python 3.11 · FastAPI · Uvicorn · S3 Files NFS · DynamoDB |
| **インフラ** | AWS CDK · CloudFront · Cognito · ECS Fargate · ALB · X-Ray |

---

## プロジェクト構成

```
├── backend/
│   └── ecs/                     # ECS Fargate エントリポイント (source of truth)
│       ├── app.py               # FastAPI (WebSocket + HTTP, Cognito JWT, SIGTERM)
│       ├── Dockerfile           # ARM64 Python 3.11, uvicorn
│       ├── requirements.txt
│       ├── healthcheck.py       # ALB ヘルスチェック
│       └── src/
│           ├── agents/              # 9 種の専門サブエージェント
│           │   ├── research_agent/      # Web検索 (Brave API)
│           │   ├── faq_generator/       # ナレッジベースドキュメント
│           │   ├── lambda_generator/    # Python Lambdaハンドラー
│           │   ├── openapi_generator/   # OpenAPI 3.0 スペック (チャンク化)
│           │   ├── prompt_generator/    # AI エージェントプロンプト
│           │   ├── contact_flow_generator/  # Connect フロー + Mermaid
│           │   ├── infrastructure_generator/ # CloudFormation YAML (チャンク化)
│           │   └── reviewer_agent/      # アセット一貫性検証
│           ├── tools/                   # ユーティリティツール
│           │   ├── project_workspace.py     # NFSバックアップ状態永続化
│           │   ├── spec_manager.py          # OperationSpec CRUD + NFS同期
│           │   ├── workspace_file_tools.py  # エージェント用 NFS file read/write/patch
│           │   ├── workspace_tools_for_subagent.py  # 修正リクエスト用パッチモードツール
│           │   ├── s3_asset_storage.py      # S3 + NFS デュアルライトアセットストレージ
│           │   ├── clues_format.py          # CLUES レスポンス形式 (Context Engineering)
│           │   ├── validate_consistency.py  # クロスアセット検証 (9 チェック)
│           │   └── ...
│           ├── context/                 # セッションコンテキスト (3階層 s3files store)
│           │   ├── __init__.py
│           │   ├── s3files_store.py     # メモリ → NFS → DynamoDB
│           │   ├── shared_state.py      # エージェント間共有状態
│           │   └── structured_notes.py  # 構造化ノートテイキング
│           └── prompts/
│               ├── system_prompt.py     # オーケストレーターシステムプロンプト (~60KB)
│               └── prompt_loader.py     # NFSからのホットリロード
├── frontend/                    # React 18 チャットインターフェース
│   └── src/
│       ├── components/          # UI コンポーネント (モバイル対応)
│       ├── hooks/               # useWebSocket (ALB + CloudFront 同一オリジン)
│       ├── stores/              # authStore, builderStore, sessionStore
│       └── services/            # 認証、セッション API
├── infrastructure/              # AWS CDK
│   └── lib/
│       ├── aicc-builder-stack.ts    # メインスタック (Cognito, S3, CloudFront, DynamoDB)
│       ├── ecs-stack.ts             # ECS Fargate スタック (VPC, ALB, Fargate, オートスケーリング)
│       ├── knowledge-base-stack.ts  # Bedrock Knowledge Base (オプション; FAQ既定パスは S3 + Connect AI agents domain)
│       └── app.ts                   # CDK エントリポイント
├── docs/                        # 詳細ドキュメント
└── deploy.sh                    # フルデプロイパイプライン
```

---

## ランタイム詳細

### S3 Files NFS マウント構造

```
/mnt/s3/
  sessions/{session_id}/
    state/          # project.json, progress.json, specs/*.json, schemas/
    assets/v1/      # lambda/, openapi/, prompt/, contact_flow/, infrastructure/, faq/
    assets/v2/      # 再生成時
    context/        # conversation_history.json, shared_state.json, all_results.txt
    workspace/      # requirements/, fragments/
  prompts/          # ホットリロード可能なシステムプロンプト
  config/           # ホットリロード可能なモデル設定
```

### 主な機能

- **Graceful Shutdown**: SIGTERM時にアクティブセッションをS3 Filesにフラッシュし、WebSocketを1001でクローズ
- **オートスケーリング**: `ActiveWebSocketConnections` CloudWatchメトリクスに基づくステップスケーリング（1〜10タスク）
- **オブザーバビリティ**: Container Insights + X-Rayサイドカー
- **ワークスペースファイルツール**: エージェントがNFS上のファイルを直接 read/write/patch（ローカルファイルシステムのように扱える）
- **Patch-only 修正**: `modification_request` でアセットを再生成する際、サブエージェントは workspace ツール（`read_current_file`, `patch_file`）で最小限の編集のみ実行 — ファイル全体の再生成は拒否されます
- **Fragment Registry**: インフラ/OpenAPI ジェネレーターのフラグメントをNFSに永続化 — コンテナ再起動を超えて保持
- **Context Engineering**: CLUES レスポンス形式でサブエージェントのトークン消費を削減; 長時間実行エージェント向けに SummarizingConversationManager を使用

---

## ドキュメント

| ドキュメント | 説明 |
|---|---|
| [docs/agentic-ai.md](./docs/agentic-ai.md) | **マルチエージェントシステムが顧客要件をエンドツーエンドで保持する仕組み** — OperationSpec契約、決定論的検証、パッチ専用修正 |
| [docs/architecture.md](./docs/architecture.md) | ランタイムアーキテクチャ、WebSocketプロトコル、データフロー |
| [docs/architecture-asset-flow.md](./docs/architecture-asset-flow.md) | アセットの read/write/stream パス、NFS + S3 へのデュアルライト |
| [docs/agents.md](./docs/agents.md) | 9種のエージェント: 役割、ツール、モデル設定、生成シーケンス |
| [docs/development.md](./docs/development.md) | ローカルセットアップ、エージェント追加、deploy.sh リファレンス、デバッグ |
| [backend/README.md](./backend/README.md) | バックエンド概要とディレクトリ構造 |
| [frontend/README.md](./frontend/README.md) | フロントエンドコンポーネントと状態管理 |
| [infrastructure/README.md](./infrastructure/README.md) | CDKスタック、リソース、デプロイ |

---

## セキュリティ

> ⚠️ **免責事項 — ワークショップ/デモツールであり、本番用ではありません**
>
> AICC Builder は、Amazon Connect 上にセルフサービス AI エージェントを高速に
> （約1時間で）構築したいすべての方を対象とした **ワークショップおよびPoCジェネレーター**
> です。生成される資産（Lambdaコード、CloudFormationテンプレート、プロンプト、
> Contact Flow、FAQドキュメント）は **出発点** であって、堅牢化された本番アーティファクト
> ではありません。LLMによって生成されているため、実際の顧客データを扱う環境に
> デプロイする前に必ずレビューしてください。
>
> 具体的には、生成された出力を本番で使用する前に、最低限以下を実施してください:
> - インフラジェネレーターが生成した IAMロール、セキュリティグループ、リソースポリシー
>   を見直し、最小権限化する。
> - API Gateway APIキーをローテーションし、スコープを絞る（ワークショップテンプレートは
>   簡素化のため意図的にメソッドを `ApiKeyRequired: false` に設定しています）。
> - 顧客データを保存するS3バケットでサーバーアクセスログ、オブジェクトバージョニング、
>   ライフサイクルルールを有効化する（アプリ自体のバケットは
>   `blockPublicAccess: BLOCK_ALL` および `enforceSSL: true` で設定済みですが、
>   **S3 サーバーアクセスログはデフォルトでは無効** であり、本番では有効化が必要です）。
> - 生成されたLambdaコードを組織のセキュアコーディング標準（入力検証、シークレット
>   管理、依存関係スキャン）に照らして検証する。
> - 公開ALB + CloudFrontディストリビューションに AWS WAF / スロットリングを有効化する。
> - 生成されたAIプロンプトを、組織固有の脅威モデルに対するプロンプトインジェクション
>   耐性の観点でレビューする。
>
> アプリ自体は会話のトランスクリプトと生成された資産を、デプロイ先のAWSアカウント内の
> S3（S3 Files NFS経由）と DynamoDB に保存します。ワークショップセッション中に
> 実際のPIIや機密データを入力しないでください。

**AICC Builder自体** のセキュリティ問題の報告については、
[CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) を参照してください。

## ライセンス

このライブラリは MIT-0 ライセンスの下で提供されます。詳細は [LICENSE](LICENSE) ファイルを参照してください。

## コントリビューション

詳細は [CONTRIBUTING](CONTRIBUTING.md) を参照してください。

---

<div align="center">

**Built with [Strands Agents SDK](https://strandsagents.com/)**

</div>
