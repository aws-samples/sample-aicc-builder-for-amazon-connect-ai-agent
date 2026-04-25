# Agentic AI Methodology

How AICC Builder turns a free-form customer conversation into a set of
consistent, deployable Amazon Connect assets — and how it keeps the
customer's original requirements intact through every stage of generation.

[English](#english) · [한국어](#한국어)

---

<a id="english"></a>

## TL;DR

AICC Builder is a **multi-agent system** built on the
[Strands Agents SDK](https://strandsagents.com/). One Orchestrator agent
interviews the customer, captures requirements into a structured
`OperationSpec`, and then calls specialized sub-agents to produce each
asset. The Orchestrator is responsible for **requirement fidelity**:
before, during, and after generation, it runs deterministic (non-LLM)
consistency checks that compare every generated asset back to the spec.
If anything drifts, it issues a **minimal patch** through the relevant
sub-agent rather than regenerating from scratch.

```
 customer ── interview ── OperationSpec ── sub-agents ── assets
                               ▲                            │
                               │     validate & patch       │
                               └────────────────────────────┘
```

---

## 1. Requirements are the source of truth, not the conversation

The chat log is noisy; customers change their minds, mix languages, skip
details. The orchestrator does not ask sub-agents to "read the chat."
Instead, it distills each business operation into a strict, machine-
readable `OperationSpec` and writes it to the session workspace:

```json
{
  "operationId": "processReturn",
  "description": "Customer-initiated return request with policy guard",
  "inputFields": [
    { "name": "orderId",   "type": "string", "required": true },
    { "name": "reason",    "type": "string", "required": true },
    { "name": "itemPrice", "type": "number", "required": true }
  ],
  "outputFields": [
    { "name": "status",       "type": "string" },
    { "name": "refundAmount", "type": "number" },
    { "name": "requiresApproval", "type": "boolean" }
  ],
  "businessRules": [
    "Auto-approve if itemPrice < 500 AND returnWindow <= 30 days",
    "Require manager approval otherwise"
  ],
  "dataSource": { "table": "Orders", "key": "orderId" }
}
```

Every sub-agent (Lambda, OpenAPI, Prompt, Contact Flow, Infrastructure)
generates its asset **from this spec**, not from the conversation.
Field names, types, and business rules enter the system **once** and
propagate downstream identically.

> See the contract in code:
> [`spec_manager.py`](../backend/ecs/src/tools/spec_manager.py) and the
> orchestrator system prompt's "Operation Spec" section.

---

## 2. Agents as tools, not as a chain

The Orchestrator registers each sub-agent as a **tool** (Strands'
Agent-as-a-Tool pattern). Calling a sub-agent looks like calling a
function — the Orchestrator passes the spec plus an explicit task
(`mode=full`, `mode=operation`, `modification_request="..."`) and gets
the generated asset back.

This matters for requirement fidelity:

- **No hidden state**: each sub-agent call is parameterized. A sub-agent
  cannot "remember" a conversation turn from three messages ago.
- **Deterministic phase ordering**: Infrastructure runs first (its
  schema summary is pinned). All downstream agents read that frozen
  schema, so table names and GSIs cannot drift between Lambda code,
  OpenAPI, and CloudFormation.
- **Parallel-safe**: because sub-agents have no shared memory,
  Lambda/OpenAPI/Prompt generators run in parallel without risk of
  one polluting another's context.

```
Phase 1: Infrastructure Generator
            ├── emits CloudFormation YAML
            └── emits Schema Summary JSON  ── pinned ──┐
                                                       ▼
Phase 2: Lambda / OpenAPI / Prompt / FAQ (parallel, read frozen schema)
Phase 3: Contact Flow Generator          (reads spec + KB)
Phase 4: Reviewer Agent                  (cross-asset consistency)
```

---

## 3. End-to-end field-name enforcement

Field-name drift is the #1 way an AI-generated asset bundle breaks at
deploy time. A Lambda expects `order_id`, OpenAPI declares `orderId`,
CloudFormation's DynamoDB partition key is `OrderID`. Everything passes
type checks individually; nothing works together.

AICC Builder enforces a single rule system-wide:

> **All field names across the system are `camelCase`**, from the
> interviewer's summary through the OperationSpec, generated Lambda
> code, OpenAPI schema, DynamoDB keys, and the AI agent prompt.

The rule is embedded in:

- The Orchestrator system prompt ("Field Naming Convention").
- Each sub-agent's system prompt.
- The deterministic validator
  (`tools/validate_consistency.py`), which reads the spec and every
  generated asset from the workspace and reports mismatches as
  structured findings.

---

## 4. Deterministic validation runs between LLM phases

After each generation phase, the Orchestrator invokes
`validate_parameter_consistency(session_id)`. This is **not** an LLM
call — it is a pure Python function that parses each asset and compares
it against the spec. It currently runs 9 cross-asset checks:

| # | Check |
|---|---|
| 1 | Lambda `body.get(...)` / `event.get(...)` fields ⊆ spec `inputFields` |
| 2 | Lambda response dict keys ⊇ spec `outputFields` |
| 3 | OpenAPI request schema (with `$ref` resolution) == spec `inputFields` |
| 4 | OpenAPI response schema == spec `outputFields` |
| 5 | OpenAPI `operationId` set == spec `operationId` set (no orphans) |
| 6 | CloudFormation DynamoDB keys == spec `dataSource.key` |
| 7 | Lambda `IndexName="..."` == CloudFormation GSI names |
| 8 | Lambda `os.environ["X_TABLE_NAME"]` == CloudFormation env var keys |
| 9 | Operation count: Lambda files == OpenAPI paths == spec count |

Because the checks are deterministic, their output is unambiguous
feedback the LLM can act on. If any check fails, the Orchestrator
issues a **patch request** (see next section) rather than asking the
sub-agent to regenerate.

> Source: [`backend/ecs/src/tools/validate_consistency.py`](../backend/ecs/src/tools/validate_consistency.py)

---

## 5. Patch-only modification (no silent regeneration)

When a validator finding or a customer follow-up requires a change,
the Orchestrator does **not** tell a sub-agent to produce a new version
of the whole asset. Instead it issues a `modification_request` that
describes exactly what must change, and the sub-agent is required to
use workspace tools (`read_current_file`, `patch_file`) to make the
minimum edit.

If a sub-agent tries to regenerate the whole file without calling the
patch tool, the workspace returns a `modification_did_not_patch` error
and the Orchestrator retries with a more specific request. This is
enforced — not advisory — because full regeneration is how small
requirement drift turns into large requirement loss.

> Source: [`backend/ecs/src/tools/workspace_tools_for_subagent.py`](../backend/ecs/src/tools/workspace_tools_for_subagent.py)

### 5.1 Modification triage: spec-level vs asset-level

Patch-only keeps the mechanics safe; triage keeps the **semantics**
safe. Before any patch, the Orchestrator classifies the request:

- **Spec-level** — domain rule the spec owns (data model, slot
  granularity, operating hours, retention, recording on/off, session
  greeting content, identifier scheme). These changes must update the
  spec first (`update_operation_spec` /
  `save_infrastructure_spec` / `save_session_flow_config`), because any
  later regeneration will otherwise revive the old value and silently
  undo the user's decision.
- **Asset-level** — wording or presentation in a single file.

Sub-agents enforce this from their side: if a `modification_request`
changes a domain rule, the sub-agent returns
`{"escalation": "spec_level", "reason": "..."}` instead of patching.
The Orchestrator then:

1. Updates the spec.
2. Analyzes downstream impact (which other assets reference the changed
   rule).
3. Presents the affected-assets plan to the user and waits for
   confirmation before re-dispatching sub-agents.

### 5.2 Rule-based disambiguation when the same request repeats

On every user turn, the system injects a `<modification_state>` block
into the orchestrator context. It contains the parsed **asset
keywords** from the user's message (`flow` / `플로우` →
`contact_flow.json`, `프롬프트` → `ai_agent_prompt.yaml`, and so on)
plus a per-keyword **repeat counter**.

If the same keyword has been requested ≥ 2 times and the previous
patch claimed success, the Orchestrator is instructed to **stop
patching** and ask the user to pick between candidate files. This is
deterministic — keyword match + counter — not LLM judgment, so the
signal is reliable.

> Sources:
> [`backend/ecs/src/context/modification_tracking.py`](../backend/ecs/src/context/modification_tracking.py),
> and the `HANDLING USER MODIFICATIONS` section of
> [`backend/ecs/src/prompts/system_prompt.py`](../backend/ecs/src/prompts/system_prompt.py).

### 5.3 Terminology facts block (broadcast to every agent)

A short authoritative "Amazon Connect AI agents" terminology block is
loaded into both the Orchestrator and every sub-agent prompt. Its job
is to override stale training-data assumptions in user-facing output:

- Product name is **"Amazon Connect AI agents"** (not "Q in Connect"
  in user-facing copy).
- Configuration unit is a **domain**.
- Default FAQ path is **an S3 bucket registered as a Knowledge Source
  on the domain**, with Bedrock Knowledge Base as an optional
  alternative for orchestration-type agents with on-contact retrieval.
- SDK identifiers (`amazon-q-connect`, `CreateWisdomSession`,
  `wisdom:*` IAM actions) are legacy-compatible and must not be
  renamed.

> Source: `TERMINOLOGY_FACTS` in
> [`backend/ecs/src/prompts/system_prompt.py`](../backend/ecs/src/prompts/system_prompt.py)
> and `SUBAGENT_TERMINOLOGY_AND_ESCALATION` in
> [`backend/ecs/src/agents/_consistency_rules.py`](../backend/ecs/src/agents/_consistency_rules.py).

---

## 6. Workspace as durable memory

Every structured artifact the agents produce lives on an NFS-backed
workspace at `/mnt/s3/sessions/{session_id}/`:

```
state/
  project.json              # Industry, company, mode
  progress.json             # Per-phase completion
  specs/{op_id}.json        # OperationSpec — the contract
  schemas/infrastructure.json  # Frozen CloudFormation schema summary
assets/v1/                  # Generated asset bundle
  lambda/{op_id}/*.py
  openapi/openapi.yaml
  prompt/ai_agent_prompt.yaml
  contact_flow/*.json
  infrastructure/template.yaml
  faq/*.md
context/                    # Conversation + shared state
workspace/                  # Fragments, requirements
```

This has two consequences for requirement fidelity:

1. **Sub-agents read the live workspace**, not a prompt containing a
   stale copy. If the Orchestrator patches the spec mid-generation,
   every subsequent sub-agent call sees the patched version.
2. **Sessions survive container restarts.** ECS tasks, ALB failovers,
   and Bedrock stream disconnects do not lose requirement state —
   the spec is durable on NFS (backed by S3).

---

## 7. The Reviewer Agent (final audit)

After Phase 4 (Contact Flow), the Orchestrator calls the
`reviewer_agent`. Unlike the deterministic validator, this is an LLM
agent whose job is to read every asset plus the original operation
specs and produce a semantic audit:

- Does the AI prompt actually encode the customer's business rules
  (return windows, approval thresholds, escalation policies)?
- Does the Contact Flow route edge cases the customer mentioned?
- Is the FAQ aligned with the prompt's stated tone and escalation rules?

Findings are returned as structured items the Orchestrator can turn
into further `modification_request` patches — again, never a full
regeneration.

> Source:
> [`backend/ecs/src/agents/reviewer_agent/`](../backend/ecs/src/agents/reviewer_agent/)

---

## 8. Context engineering: keeping agents cheap and coherent

Long-running multi-agent systems fail when each agent re-reads
everything. AICC Builder uses three techniques from the
[context engineering](https://strandsagents.com/) playbook:

- **CLUES response format**: sub-agents emit a compact
  `[C]ontext-[L]earned-[U]pdates-[E]vents-[S]ummary` block instead of
  verbose narrative. The Orchestrator can ingest many sub-agent
  results without blowing through its context window.
- **Frozen schema summary**: the Infrastructure Generator emits a
  Schema Summary JSON that Phase-2 agents read as a static file, not
  as a re-derived context block each call.
- **Summarizing conversation manager**: on long sessions the
  Orchestrator compacts older turns via Strands'
  `SummarizingConversationManager`, preserving the spec and
  decisions while dropping chit-chat.

The net effect: adding more sub-agents does not linearly inflate token
usage, and the Orchestrator's view of the customer's requirements
stays sharp across a long session.

---

## 9. Why this matters for a workshop PoC

A plausible-looking asset bundle that subtly diverges from the
customer's requirements is worse than no bundle at all — the customer
will find the divergence during the workshop, lose trust in the PoC,
and attribute the errors to the tooling rather than to the LLM.

The mechanisms above are designed so that, by the time the orchestrator
tells the user "your assets are ready," three things are simultaneously
true:

1. **Every asset traces back to a specific line in the spec.** There
   is no hallucinated field, method, or table that has no source.
2. **Every asset agrees with every other asset.** Deterministic
   validation guarantees cross-asset field-name and operation-count
   consistency.
3. **Every asset reflects the latest state of the requirement.**
   Patch-only modification plus the NFS workspace mean the last thing
   the customer said is the thing that's encoded in the bundle.

---

<a id="한국어"></a>

# 한국어

## 요약

AICC Builder는 [Strands Agents SDK](https://strandsagents.com/) 기반의
**멀티 에이전트 시스템**입니다. 하나의 오케스트레이터 에이전트가 고객과
대화하면서 요구사항을 구조화된 `OperationSpec`으로 수집하고, 각 자산을
담당하는 전문 서브 에이전트를 호출해 산출물을 만듭니다. 오케스트레이터의
핵심 책임은 **요구사항 충실도**를 끝까지 유지하는 것입니다. 생성 전/중/후에
결정론적(LLM이 아닌) 일관성 검증을 실행하고, 드리프트가 발견되면 전체
재생성이 아닌 **최소 패치**로 수정합니다.

```
 고객 ── 인터뷰 ── OperationSpec ── 서브 에이전트들 ── 자산
                        ▲                                │
                        │      검증 및 패치               │
                        └────────────────────────────────┘
```

---

## 1. 대화 로그가 아닌 '요구사항'이 단일 진실의 원천

대화 로그에는 노이즈가 많습니다. 고객은 마음을 바꾸고, 언어를 섞어 쓰고,
세부사항을 빠뜨립니다. 오케스트레이터는 서브 에이전트에게 "대화 로그를 읽고
판단해라"라고 맡기지 않습니다. 대신 각 업무를 엄격한 기계 판독 가능한
`OperationSpec`으로 정제해 세션 workspace에 저장합니다. 모든 서브 에이전트
(Lambda / OpenAPI / Prompt / Contact Flow / Infrastructure)는 대화가 아닌
**이 스펙**을 보고 자산을 생성합니다. 필드명, 타입, 비즈니스 룰은 시스템에
**단 한 번만** 들어오고 그대로 하위로 전파됩니다.

---

## 2. 체인이 아닌 '도구로서의 에이전트'

오케스트레이터는 각 서브 에이전트를 **도구(tool)**로 등록합니다(Strands의
Agent-as-a-Tool 패턴). 서브 에이전트 호출은 함수 호출과 동일합니다.
오케스트레이터가 스펙과 명시적인 작업(`mode=full`, `modification_request=...`)을
전달하고 결과만 받아옵니다.

- **숨겨진 상태 없음**: 모든 호출이 매개변수화되어 있어 서브 에이전트가
  3턴 전 대화를 "기억"할 수 없습니다.
- **결정론적 단계 순서**: 인프라가 먼저 실행되고 스키마 요약이 고정됩니다.
  이후의 모든 에이전트는 그 고정된 스키마를 읽으므로 테이블명/GSI가 Lambda,
  OpenAPI, CloudFormation 간에 어긋날 수 없습니다.
- **병렬 안전**: 서브 에이전트 간 공유 메모리가 없으므로 Lambda / OpenAPI /
  Prompt가 병렬로 실행돼도 서로의 컨텍스트를 오염시키지 않습니다.

---

## 3. End-to-End 필드명 강제

AI로 생성된 자산 번들이 배포 시점에 가장 흔하게 깨지는 원인은 **필드명
드리프트**입니다. Lambda는 `order_id`를 기대하는데 OpenAPI는 `orderId`를
선언하고, CloudFormation DynamoDB 파티션 키는 `OrderID`. 각각의 타입 검증은
통과하지만, 합쳐놓으면 동작하지 않습니다.

AICC Builder는 시스템 전체에 단일 규칙을 강제합니다:

> **모든 필드명은 `camelCase`**. 인터뷰어 요약 → OperationSpec → 생성된
> Lambda 코드 → OpenAPI 스키마 → DynamoDB 키 → AI 에이전트 프롬프트까지
> 동일.

이 규칙은 오케스트레이터 시스템 프롬프트, 각 서브 에이전트의 시스템 프롬프트,
그리고 결정론적 검증기(`tools/validate_consistency.py`)에 모두 인코딩되어
있습니다.

---

## 4. LLM 단계 사이의 결정론적 검증

오케스트레이터는 각 생성 단계 종료 시 `validate_parameter_consistency(session_id)`를
호출합니다. 이는 **LLM 호출이 아닌** 순수 Python 함수이며, 각 자산을 파싱해
스펙과 비교합니다. 현재 9개 교차 자산 검증을 실행합니다:

1. Lambda `body.get(...)` 필드 ⊆ 스펙 `inputFields`
2. Lambda 응답 딕셔너리 키 ⊇ 스펙 `outputFields`
3. OpenAPI 요청 스키마(`$ref` 해석) == 스펙 `inputFields`
4. OpenAPI 응답 스키마 == 스펙 `outputFields`
5. OpenAPI `operationId` 집합 == 스펙 `operationId` 집합 (고아 없음)
6. CloudFormation DynamoDB 키 == 스펙 `dataSource.key`
7. Lambda `IndexName="..."` == CloudFormation GSI 이름
8. Lambda `os.environ["X_TABLE_NAME"]` == CloudFormation 환경변수 키
9. Operation 수: Lambda 파일 수 == OpenAPI 경로 수 == 스펙 수

결정론적이기 때문에 출력이 명확하고, LLM이 다음에 무엇을 해야 할지 모호하지
않습니다. 실패 시 오케스트레이터는 재생성이 아닌 **패치 요청**을 발행합니다.

---

## 5. 패치 전용 수정 (암묵적 재생성 금지)

검증 결과 또는 고객 후속 요청으로 인해 변경이 필요한 경우, 오케스트레이터는
서브 에이전트에게 "파일 전체를 다시 만들어라"라고 지시하지 **않습니다**.
대신 `modification_request`로 "정확히 이것만 바꿔라"를 전달하고, 서브
에이전트는 workspace 도구(`read_current_file`, `patch_file`)를 사용해 최소
편집만 수행해야 합니다.

서브 에이전트가 patch 도구를 호출하지 않고 전체 파일을 다시 만들면
workspace가 `modification_did_not_patch` 오류를 반환하며, 오케스트레이터는
더 구체적인 요청으로 재시도합니다. **의도된 동작**입니다. 작은 요구사항
드리프트가 큰 요구사항 상실로 번지는 가장 흔한 경로가 전체 재생성이기 때문입니다.

### 5.1 수정 요청 트리아지 — spec-level vs asset-level

패치 전용은 메커니즘을 안전하게 만들지만, 요구사항 의미의 충실도는 **분류**가
지킵니다. 오케스트레이터는 패치하기 전에 요청을 먼저 분류합니다:

- **Spec-level** — 스펙이 소유하는 도메인 규칙 (데이터 모델, 슬롯 단위,
  운영 시간, 보존 정책, 녹음 on/off, 세션 인사 멘트, 식별 체계 등). 스펙을
  먼저 업데이트해야 합니다 (`update_operation_spec` /
  `save_infrastructure_spec` / `save_session_flow_config`). 그러지 않으면
  다음 번 재생성이 예전 값을 되살려 사용자의 결정을 **조용히 무효화**합니다.
- **Asset-level** — 특정 파일의 표현/문구만 바뀌는 변경.

서브 에이전트도 이를 강제합니다: `modification_request`가 도메인 규칙을
바꾸면 서브 에이전트는 패치하지 않고
`{"escalation": "spec_level", "reason": "..."}`을 반환합니다. 그러면
오케스트레이터는:

1. 스펙을 업데이트
2. 다운스트림 영향 분석 (어느 에셋이 변경된 규칙을 참조하는지)
3. 영향 받는 에셋 플랜을 사용자에게 제시하고 컨펌받은 뒤 서브 에이전트
   재호출

### 5.2 반복 수정 감지 — 룰 기반 disambiguation

매 사용자 턴마다 시스템은 `<modification_state>` 블록을 오케스트레이터
컨텍스트에 주입합니다. 이 블록에는 이번 턴 사용자 메시지의 **에셋 키워드**
파싱 결과 (예: `flow` / `플로우` → `contact_flow.json`, `프롬프트` →
`ai_agent_prompt.yaml`)와 키워드별 **반복 카운터**가 들어 있습니다.

같은 키워드가 ≥ 2회 반복되었고 직전 수정이 성공으로 기록되어 있다면
오케스트레이터는 패치를 **중단**하고 어느 파일을 고치려는지 사용자에게
다시 묻습니다. 키워드 매칭 + 카운터로 동작하는 **결정론적 규칙**이라 LLM
판단에 의존하지 않습니다.

### 5.3 용어 팩트 블록 (모든 에이전트에 브로드캐스트)

짧은 "Amazon Connect AI agents" 용어 블록이 오케스트레이터와 모든 서브
에이전트 프롬프트에 주입됩니다. 용도는 사용자 대면 출력에서 오래된
학습 데이터 기반 가정을 **덮어쓰는 것**입니다:

- 제품 명칭: 사용자 대면에서는 **"Amazon Connect AI agents"** (구명
  "Q in Connect"는 사용자 대면 카피에서 사용하지 않음).
- 설정 단위: **domain**.
- FAQ 기본 경로: **domain에 등록된 S3 버킷**. Bedrock KB는 orchestration
  타입 + on-contact 사용 시의 옵션.
- SDK 식별자(`amazon-q-connect`, `CreateWisdomSession`, `wisdom:*` IAM
  액션)는 하위 호환용 레거시 — 절대 이름 변경하지 않음.

관련 파일: `backend/ecs/src/prompts/system_prompt.py`의
`TERMINOLOGY_FACTS`, `backend/ecs/src/agents/_consistency_rules.py`의
`SUBAGENT_TERMINOLOGY_AND_ESCALATION`.

---

## 6. Workspace = 지속 가능한 메모리

모든 구조화된 산출물은 NFS-backed workspace
(`/mnt/s3/sessions/{session_id}/`)에 저장됩니다. 이는 요구사항 충실도에
두 가지 결과를 가져옵니다:

1. **서브 에이전트는 프롬프트가 아닌 live workspace를 읽습니다.** 오케스트레이터가
   생성 도중 스펙을 패치하면 이후의 모든 서브 에이전트 호출이 패치된 버전을
   봅니다.
2. **세션이 컨테이너 재시작을 견딥니다.** ECS 태스크 교체, ALB failover,
   Bedrock 스트림 단절이 발생해도 요구사항 상태를 잃지 않습니다 — 스펙은
   NFS(S3 백업)에 영속적으로 저장됩니다.

---

## 7. Reviewer Agent (최종 감사)

Phase 4(Contact Flow) 이후 오케스트레이터는 `reviewer_agent`를 호출합니다.
결정론적 검증기와 달리 이는 LLM 에이전트이며, 모든 자산과 원본 OperationSpec을
읽고 의미론적 감사를 수행합니다:

- AI 프롬프트가 고객의 비즈니스 룰(반품 기간, 승인 임계값, 에스컬레이션 정책)을
  실제로 인코딩했는가?
- Contact Flow가 고객이 언급한 엣지 케이스를 처리하는가?
- FAQ가 프롬프트의 톤과 에스컬레이션 규칙과 일관되는가?

발견된 이슈는 구조화된 항목으로 반환되며, 오케스트레이터는 이를 다시
`modification_request` 패치로 변환합니다. 역시 전체 재생성이 아닙니다.

---

## 8. Context Engineering — 저렴하고 일관된 에이전트

장시간 실행되는 멀티 에이전트 시스템은 각 에이전트가 모든 것을 다시 읽을 때
실패합니다. AICC Builder는 세 가지 기법을 사용합니다:

- **CLUES 응답 형식**: 서브 에이전트는 장황한 서술 대신 압축된
  `[C]ontext-[L]earned-[U]pdates-[E]vents-[S]ummary` 블록을 반환합니다.
  오케스트레이터는 컨텍스트 윈도우를 터뜨리지 않고 여러 서브 에이전트
  결과를 흡수할 수 있습니다.
- **고정 스키마 요약**: 인프라 생성기는 Schema Summary JSON을 방출하고,
  Phase 2 에이전트들은 매번 재추론이 아닌 정적 파일로 읽습니다.
- **요약 대화 관리자**: 장기 세션에서는 Strands의 `SummarizingConversationManager`가
  오래된 턴을 압축하면서 스펙과 의사결정은 보존합니다.

결과적으로 서브 에이전트를 추가해도 토큰 사용량이 선형으로 증가하지 않고,
오케스트레이터의 요구사항에 대한 이해도 장기 세션 내내 유지됩니다.

---

## 9. 워크숍 PoC에 왜 이게 중요한가?

겉보기에는 그럴듯한데 고객 요구사항에서 미묘하게 벗어난 자산 번들은 **차라리
없는 것보다 나쁩니다**. 고객은 워크숍 중에 그 차이를 발견하게 되고, PoC에
대한 신뢰를 잃으며, 그 오류를 LLM이 아닌 **툴링 탓**으로 돌립니다.

위 메커니즘은 오케스트레이터가 사용자에게 "자산이 준비되었습니다"라고 말하는
시점에 다음 세 가지가 동시에 참이 되도록 설계되었습니다:

1. **모든 자산은 스펙의 특정 라인으로 추적됩니다.** 출처 없는 hallucinated
   필드/메서드/테이블은 존재하지 않습니다.
2. **모든 자산은 서로 일치합니다.** 결정론적 검증이 교차 자산 필드명 및
   operation 개수 일관성을 보장합니다.
3. **모든 자산은 요구사항의 최신 상태를 반영합니다.** 패치 전용 수정 +
   NFS workspace로 고객이 마지막에 말한 것이 번들에 인코딩됩니다.
