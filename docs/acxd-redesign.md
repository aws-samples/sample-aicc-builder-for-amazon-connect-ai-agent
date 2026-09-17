# ACXD 재설계 — Classic Full 파이프라인의 런타임 타깃으로 통합

상태: 설계안 (v3.0 ACXD 모드를 대체). 작성일 2026-09-09.

## 1. 결론 먼저

ACXD는 별도 제품이 아니라 **Classic Full 빌드의 런타임 타깃**이다. 전화/채팅은
여전히 Connect Contact Flow로 인입되고, ACXD 애플리케이션이 Lex + AI Prompt +
AgentCore Gateway 자리를 대신하며, escalate / end 시 Contact Flow로 돌아온다.
따라서 CFN 인프라, Lambda, HTTP 계약(OpenAPI → API Gateway), Contact Flow, FAQ
콘텐츠는 형태만 바뀌어 **그대로 필요**하다.

```
인입 → Connect Contact Flow → [Agentic CX 블록]
                                 ├─ Data Request ──→ API Gateway → Lambda → DB   (= Classic 백엔드)
                                 ├─ Knowledge Base 노드                          (= Classic FAQ)
                                 └─ Guardrail
        ← Default / Escalation / Error / Idle chat timeout 분기로 복귀
```

v3.0의 "두 경로는 스토리지 위로 아무것도 공유하지 않는다"(decision D1)는
폐기한다. 회귀 방지는 테스트로 하고, 파이프라인 분기로 하지 않는다.

### 유지하는 것 / 버리는 것

| 유지 (ACXD 고유 구조) | 버림 (격리를 위한 중복) |
|---|---|
| `schemas/acxd/contract.json` + `scripts/extract_acxd_contract.py` + `tools/acxd_contract.py` (SDK 추출 계약) | `prompts/acxd_system_prompt.py` (별도 오케스트레이터 프롬프트) |
| `schemas/acxd/*.schema.json`, `tools/validate_acxd_flow.py` (스키마·그래프 검증) | `tools/acxd_spec_manager.py`의 ACXDSpec (business/operations 중복) |
| `agents/acxd_flow_generator/` — LLM flow 서브에이전트 + validate-repair 루프 | 6단계 인터뷰, `set_acxd_phase`, `state/acxd_spec.json` |
| `tools/acxd_{data_request,resource}_builders.py` — 결정론적 빌더 (입력을 OperationSpec으로 교체) | `tools/acxd_orchestrator_tools.py` (별도 툴 레지스트리·phase 스위칭) |
| `tools/acxd_manifest_builder.py`, `templates/acxd_runner/` + node:test | `tools/acxd_asset_packager.py` (→ `asset_packager.py`에 흡수) |
| `tools/acxd_asset_patcher.py`의 "패치 → 번들 재검증 → 롤백" 동작 | `tools/session_mode.py` (→ InfraSpec `runtime_target`) |
| step별 deterministic/generative 사용자 확인 개념 | `frontend/AcxdPanel.tsx`, 6단계 체크리스트, `acxd_phase` 이벤트 |

## 2. 계약: OperationSpec + ACXDFlowSpec

`OperationSpec`, `DataSourceSpec`, `InfraSpec`, `ContactFlowSpec`은 그대로.
추가되는 것은 둘뿐이다.

```
InfraSpec.runtime_target: "classic" | "acxd"          # 세션 시작 시 고정
state/acxd_flow_spec.json  (ACXDFlowSpec)             # ContactFlowSpec과 같은 부속 spec
```

```jsonc
// ACXDFlowSpec — 인터뷰 ③단계에서 채움. Classic ContactFlowSpec과 형제.
{
  "flows": [{
    "flow_id": "processReturn",              // letters only (SDK 제약)
    "operation_id": "process_return",        // OperationSpec 참조 — 1 operation = 1 flow
    "steps": [{
      "step_id": "...", "description": "...",
      "node_type": "user_input|data_request|choice|message|knowledge_base|generative_journey|escalation|end|...",
      "determinism": "deterministic|generative",
      "rationale": "...",                    // AI 추천 근거 (일상어)
      "user_confirmed": true                 // false인 step이 있으면 생성 거부
    }]
  }],
  "system_flows": {"welcome": {...}, "fallback": {...}, "escalation": {...}},
  "guardrails": [{"name": "...", "detection": "regex|keyword|llmJudge", "action": "mask|modify|route|flag", ...}],
  "application": {"channels": [...], "locales": ["ko-KR"], "speech_engine": "agentic_voice|transcribe|s2s",
                  "idle_chat_timeout_sec": 300, "context_variables": [...], "environment": "development"}
}
```

돈·환불·권한·자격·컴플라이언스 판단 step은 인터뷰가 `deterministic`으로
고정하고 사용자에게 변경을 허용하지 않는다(현행 규칙 유지). "사용자가
고집하면 예외" 문구는 삭제한다.

## 3. 인터뷰 — Classic 4단계 + 삽입

Classic `interview_agent_prompt.py`의 단계를 그대로 쓰고, `runtime_target=acxd`
일 때 다음을 **삽입**한다. 별도 인터뷰 프롬프트는 없다.

| 단계 | Classic | acxd 추가 질문 |
|---|---|---|
| ① Discovery | 업종·회사·톤·언어 | 채널(voice/chat/둘 다), 음성 엔진(agentic voice 권장 / Transcribe / S2S) |
| ② Operation Deep Dive | DB 유형 필수 → operation별 입력·출력·규칙 | 변경 없음 — 각 operation은 Data Request 1개 + flow 1개가 된다 |
| ②.5 | DTMF·인증·외부연동(real vs mock)·콜 방향 | DTMF → `user_input` 노드 + slot type. 외부연동 real/mock → Data Request `external`/`mock` |
| ③ Contact Flow | 큐·영업시간·에스컬레이션·콜백 | **ACXD flow 설계**: operation별 step 목록 → node type + determinism 추천·근거 → 사용자 확인 (ACXDFlowSpec). 시스템 flow, guardrail, app 설정. 라이브 React Flow 프리뷰 |
| ④ 저장·요약·분석문서 | 확인 후 `complete_interview` | 분석 문서에 ACXD flow 설계 절 추가 |

## 4. 생성 — 동일 순서, 동일 승인 모델

`ONE PHASE PER TURN — HARD STOP`, 매 단계 결과 표시 후 사용자 승인. 4번만 타깃에
따라 바뀐다.

| # | Classic | acxd | 생성 주체 |
|---|---|---|---|
| 1 | Infrastructure (CFN: DB + Lambda + API Gateway `/tools/<op>`) | 동일. AgentCore Gateway 관련 리소스 생략 | LLM fragment + 코드 merge + cfn-lint |
| 2 | Lambda | 동일 | LLM + lint |
| 3 | OpenAPI (MCP Gateway 용) | 동일 생성기. 용도는 API Gateway 계약 + **Data Request의 request/response 스키마 원천** | LLM + lint |
| 4 | AI Prompt YAML | **ACXD Application 번들**: flows(LLM 서브에이전트, operation당 1 + 시스템 flow), data requests(OpenAPI에서 코드 유도), slot types(FieldSpec에서 코드 유도), guardrails·application.json(ACXDFlowSpec에서 코드) | LLM 1종 + 코드 |
| 5 | Contact Flow (Lex `GetParticipantInput`) | 동일 생성기. Lex 블록 대신 **Agentic CX 블록**; 분기 Default→종료, Escalation→큐, Error→폴백, Idle chat timeout(chat)→종료; 후속 블록은 `$.AgenticCX.ContextVariables.<name>` 참조; 컨텍스트 변수 ≤10 | LLM + RAG + linter |
| 6 | FAQ `.txt` (Q in Connect) | 동일 생성기. 출력을 KB articles(`knowledge_base.json`)로도 렌더 | LLM + 코드 |
| 7 | Review | **동일하게 필수**: `reviewer_agent`(+ACXD 차원) + `validate_parameter_consistency` D1–D8 + **D9** | LLM + 코드 |
| 8 | Package | 단일 `asset_packager`; `deploy.sh --target acxd` | 코드 |

AI Prompt의 내용은 이렇게 분산된다: 페르소나·톤 → application 설정 + flow
메시지 + `generative_journey` 프롬프트 / 비즈니스 규칙 → deterministic 노드와
구조화 조건 / 금칙·PII → guardrail / 에스컬레이션 정책 → escalation 노드.

Classic 하드 규칙은 acxd에서 그대로 적용되며, 네이티브 기능 규칙은 다음으로
매핑된다: FAQ 검색 → `knowledge_base` 노드(Lambda 금지), 상담원 전환·종료 →
`escalation`/`end` 노드 + Contact Flow 분기(Lambda·API 금지). real-vs-mock 확인,
NEVER AUTO-FIX, patch-only 수정(패치 후 번들 재검증·롤백)도 동일.

## 5. 검증 — D9 계열 추가

`validate_consistency.py`에 `runtime_target=acxd`일 때만 실행되는 D9 계열을
추가한다. 현행 `validate_acxd_consistency.py`의 내용을 이 안으로 옮긴다.

| ID | 검증 |
|---|---|
| D9-1 | flow JSON 스키마·그래프 정합성(단일 start, terminal, dangling edge, unreachable) — `validate_acxd_flow` |
| D9-2 | **determinism 계약**: 생성 flow의 노드 ↔ ACXDFlowSpec의 확인된 step, 미확인 step·무단 generative 노드 거부 |
| D9-3 | Data Request ↔ OpenAPI: url `/tools/<op>`이 path에 존재, request/response 필드가 스키마와 일치 |
| D9-4 | slot type ↔ FieldSpec(enum·regex·length) |
| D9-5 | KB articles ↔ FAQ 문서 |
| D9-6 | Contact Flow: Agentic CX 블록의 4분기 연결, 컨텍스트 변수 ≤10, `$.AgenticCX.ContextVariables.*` 참조가 application context variable에 존재 |
| D9-7 | ID·메타데이터: flowId letters-only, node UUID, description/aiDescription ASCII, full locale |
| D9-8 | 외부 Data Request가 있으면 D2(IAM)/D3(RDS)/D4(SQL)/D5(Connect 권한) 결과를 번들 위반으로 합성 |

D9 위반은 현행처럼 패키징을 차단한다(ACXD는 배포 시점에야 터지므로). D1–D8은
현행 fail-soft 유지.

## 6. UI — Classic과 동일한 표면

- 시작 카드: Full Build 카드 안에 **Runtime target: Classic(Lex + AI agent) / ACXD** 선택. 별도 "ACXD Build" 카드 제거.
- 우측 패널: `ProgressSidebar` + `AssetWorkspace` 그대로. `AssetWorkspace`에 **ACXD 탭 그룹**(Flows — React Flow, Data Requests, Guardrails, Knowledge Base, Application) 추가. Contact Flow 탭의 다이어그램 방식과 동일.
- 진행 목록: 기존 12항목 유지, 4번 라벨만 "AI Prompt" ↔ "ACXD Application"으로 타깃에 따라 변경.
- 인터뷰 ③단계 라이브 프리뷰: `asset_preview` 이벤트로 ACXDFlowSpec의 step 그래프를 Flows 탭에 그린다. `acxd_flow_preview`/`acxd_phase`/`mode_set` 이벤트 제거.
- Single Segment / Improve Existing: v1은 Classic만. ACXD flow JSON import는 후속.

## 7. 배포 — `deploy.sh --target acxd`

공통 스크립트 한 벌. ACXD는 phase 8–12를 runner 호출로 대체한다.

| Phase | Classic | acxd |
|---|---|---|
| 1–3 | CFN, Lambda, OpenAPI 업로드 | 동일 (API Gateway 배포 포함) |
| 4 | FAQ → S3 | 생략 (KB는 runner) |
| 5 | Connect 인스턴스 생성/선택 | 동일. **Connect Customer 인스턴스**여야 함(블록 제약) — 아니면 중단 |
| 6 | Q in Connect Assistant/KB | 생략 |
| 7 | Lambda env | 동일 |
| 8–10 | AgentCore Gateway, MCP 연결, Lex | **runner**: secrets → slot types → context vars → data requests(WEBHOOK_URL 주입) → KB publish → guardrails → flows → application compose → build → deploy |
| 11 | Contact Flow import | 동일. 블록 action type이 확정되면 자동 배선, 아니면 placeholder + WIRING-GUIDE |
| 12 | AI Prompt/Agent/Security profile | 생략 |
| 13 | 전화번호 claim | 동일 |

`status`/`cleanup`은 양쪽 상태 파일을 함께 읽는다. runner는 `--dry-run` 유지.

## 8. 언어

모든 프롬프트는 영어로 작성하고 `prompts/base.py`의 세션 언어 헬퍼로 ko/en/ja
출력을 제어한다. 한국어 미러링 같은 로케일 하드코딩은 금지. 고객 노출 텍스트
(`messages[].body`, KB 본문)는 세션 언어, 메타데이터(`description`,
`aiDescription`)는 ASCII — 후자는 코드가 ASCII 요약을 생성한다.

## 9. 선결 과제 — Agentic CX 블록의 Flow Language 정의

관리자 가이드는 블록의 파라미터(Workspace/Application/Alias ID, 음성 엔진,
오디오 필러, idle chat timeout, 컨텍스트 변수 ≤10)와 분기(Default / Error /
Idle chat timeout / Escalation), 출력 속성(`$.AgenticCX.ContextVariables.*`)을
명시하지만, **Flow Language action `Type`과 `Parameters` 형태는 API 레퍼런스에
없다**. Classic의 block allowlist를 만들 때와 같은 방법으로 확정한다:

1. Connect Customer 인스턴스에서 블록이 포함된 flow를 콘솔 export
2. `Type`/`Parameters`/`Transitions.Conditions` 형태를 `asset_linters.py`의 allowlist와 정규화 규칙에 추가
3. Contact Flow 생성기 프롬프트·RAG 문서에 예시 추가, `CreateContactFlow` probe로 검증

확정 전까지 5번 단계는 placeholder를 유지한다.

## 10. 마이그레이션 순서

1. **계약**: `InfraSpec.runtime_target` 추가, `ACXDFlowSpec` 모델·CRUD 툴(`spec_manager.py` 부속), `session_mode.py` 제거.
2. **인터뷰**: `interview_agent_prompt.py`에 acxd 삽입 블록, ③단계 step 확인 툴(`upsert_acxd_flow_plan` 재사용), 라이브 프리뷰를 `asset_preview`로.
3. **생성기**: 4단계 툴 `generate_acxd_application`(flows 서브에이전트 + 빌더 호출, 입력 = OperationSpec/OpenAPI/FieldSpec/ACXDFlowSpec), Contact Flow 생성기 타깃 분기, FAQ → KB 렌더.
4. **검증**: D9 계열을 `validate_consistency.py`로 이관, reviewer 프롬프트에 ACXD 차원 추가.
5. **오케스트레이터**: `system_prompt.py`에 `runtime_target` 조건 절(네이티브 기능 매핑, ASCII/flowId 규칙, 4단계 설명). `acxd_system_prompt.py`·`acxd_orchestrator_tools.py` 삭제.
6. **패키징·배포**: `asset_packager.py`에 ACXD 자산·manifest·runner 포함, `deploy_workshop.sh --target acxd`.
7. **UI**: 시작 카드 토글, `AssetWorkspace` ACXD 탭, `AcxdPanel` 제거, 진행 라벨.
8. **테스트**: Classic 테스트 무변경 통과, `test_acxd_e2e.py`를 Classic Full E2E와 같은 경로(target=acxd)로 재작성, runner node:test 유지.

## 11. 이유 있는 차이로 남는 것

- step별 deterministic/generative 사용자 확인 (Lex에는 노드 개념이 없음)
- SDK 추출 계약 기반 D9 fail-hard (배포 전 로컬에서 잡을 유일한 기회)
- Node runner (ACXD SDK에 CLI 없음)
- ASCII 메타데이터, letters-only flowId, UUID node id (서비스 제약)
