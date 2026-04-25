# AICC Builder

<div align="center">

**Turn a 15-minute AI conversation into a fully customized Amazon Connect PoC**

[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-2.x-orange?style=flat&logo=amazonaws)](https://aws.amazon.com/cdk/)
[![React](https://img.shields.io/badge/React-18.3-blue?style=flat&logo=react)](https://reactjs.org/)
[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat&logo=python)](https://python.org/)
[![Strands Agents](https://img.shields.io/badge/Strands-Agents-purple?style=flat)](https://strandsagents.com/)

[English](#english) · [한국어](#한국어)

</div>

---

<a id="english"></a>

## What is AICC Builder?

AICC Builder is an **open-source Agentic AI sample pplication** that generates a
customized Amazon Connect asset bundle (Lambda, OpenAPI, AI prompt,
Contact Flow, CDK infrastructure, FAQ) from a ~15-minute conversation
with a customer. A single Orchestrator agent interviews the user,
distills the conversation into a structured `OperationSpec`, and calls
specialized sub-agents to produce each asset — with deterministic
cross-asset validation between phases so the bundle is internally
consistent before it is delivered.

**Who is it for?** SA/Sales teams running
[Amazon Connect AI Workshops](https://catalog.workshops.aws/) who want
the workshop to end with a deployable PoC for the customer's actual
business, not a generic hotel demo.

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
| **Time to PoC** | Weeks of SA effort | 15-minute conversation |

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
git clone <repository-url>
cd aicc-builder

# Full deployment (default: Seoul ap-northeast-2)
./deploy.sh

# Deploy to a different region
AWS_DEFAULT_REGION=us-east-1 ./deploy.sh

# Named stage (separate stacks, e.g. for staging alongside prod)
./deploy.sh --stage prod
AWS_DEFAULT_REGION=ap-northeast-1 ./deploy.sh --stage prod
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
  --region ap-northeast-2
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
> AICC Builder is intended as a **workshop and proof-of-concept generator** for
> SA/Sales engagements. The assets it produces (Lambda code, CloudFormation
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

AICC Builder는 **오픈소스 Agentic AI 애플리케이션**으로, 고객과의 약 15분
대화만으로 Amazon Connect 맞춤형 자산 번들(Lambda, OpenAPI, AI 프롬프트,
Contact Flow, CDK 인프라, FAQ)을 자동 생성합니다. 단일 오케스트레이터
에이전트가 고객을 인터뷰하고, 대화를 구조화된 `OperationSpec`으로 증류한 뒤,
각 자산을 담당하는 전문 서브 에이전트를 호출합니다. 단계 사이마다 **결정론적
교차 자산 검증**이 실행되어, 전달 전 번들의 내부 일관성이 보장됩니다.

**대상**: [Amazon Connect AI Workshop](https://catalog.workshops.aws/)을
진행하는 SA/세일즈 팀. 워크숍의 결과물을 고정된 호텔 데모가 아닌 **고객
실제 비즈니스에 배포 가능한 PoC**로 만들고 싶은 경우.

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
| **PoC까지 소요 시간** | SA 수 주 작업 | 15분 대화 |

---

## 빠른 시작

### 사전 요구사항

AWS CLI 2.x (>= 2.34.27, `s3files` 지원) · Node.js 18+ · Python 3.11+ · Docker · AWS CDK 2.x

### 배포

```bash
git clone <repository-url>
cd aicc-builder

# 전체 배포 (기본: 서울 ap-northeast-2)
./deploy.sh

# 다른 리전에 배포 (예: 도쿄)
AWS_DEFAULT_REGION=ap-northeast-1 ./deploy.sh

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

**리전 선택:** 모든 스택이 동일 리전에 배포됩니다. 기본 리전은 서울(`ap-northeast-2`)이며, `AWS_DEFAULT_REGION`으로 변경 가능합니다.

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
> AICC Builder는 SA/세일즈 참여를 위한 **워크숍 및 PoC 생성기**입니다. 생성되는
> 에셋(Lambda 코드, CloudFormation 템플릿, 프롬프트, Contact Flow, FAQ)은
> **시작점**일 뿐 프로덕션 수준의 아티팩트가 아니며, LLM으로 생성되므로
> 실제 고객 데이터가 있는 환경에 배포하기 전에 반드시 검토해야 합니다.
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

<div align="center">

**Built with [Strands Agents SDK](https://strandsagents.com/)**

</div>
