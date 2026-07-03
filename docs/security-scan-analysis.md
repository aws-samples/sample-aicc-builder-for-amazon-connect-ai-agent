# Security Scan 분석 결과

> 분석일: 2026-03-26 (초기 스캔)
> 최종 갱신: 2026-06-27 — v2.2 기준 대부분 항목 조치 완료 (아래 상태 참조)
> 스캐너: ACAT, Bandit

---

## 요약

| # | 파일 | 룰 | 심각도 | 판정 | 조치 |
|---|---|---|---|---|---|
| 1 | ~~`frontend/src/components/MermaidDiagram.tsx`~~ | dangerouslySetInnerHTML | High | **해소됨 (v2.2)** | mermaid 렌더링 제거 — Contact Flow 다이어그램이 검증된 JSON에서 파생되는 React Flow 그래프(`FlowDiagram.tsx`)로 대체되어 컴포넌트·`mermaid` 의존성·`dangerouslySetInnerHTML`이 모두 삭제됨 |
| 2 | `backend/ecs/src/tools/lambda_generator.py` (x3) | Bandit B608 | Medium | **오탐 (False Positive)** | ✅ 조치 완료 — `# nosec B608` 주석 추가됨 (Line 429, 548, 590) |
| 3 | `infrastructure/lib/aicc-builder-stack.ts` (x2) | SecureCdkBsc17 | Low | ✅ 조치 완료 | `enforceSSL: true` 적용됨 |
| 4 | `infrastructure/lib/knowledge-base-stack.ts` | SecureCdkBsc17 | Low | ✅ 조치 완료 | `enforceSSL: true` 적용됨 |
| 5 | `infrastructure/lib/knowledge-base-stack.ts` | SecureCdkBsc43 | Low | 무시 가능 | PoC 용도로 access logging 불필요 |
| 6 | `infrastructure/dist/*.js` (x2) | SecureCdkBsc17 | Low | ✅ 해소됨 | 소스(.ts)에 `enforceSSL: true` 추가 후 재빌드되어 `dist/*.js`에도 반영됨 |

---

## 수정 필요 항목

### 1. MermaidDiagram.tsx — dangerouslySetInnerHTML (High) — ✅ 해소됨 (v2.2)

**상태:** ✅ 해소됨 — v2.2에서 mermaid 렌더링이 완전히 제거되었습니다. Contact Flow 다이어그램은 이제 검증된 Connect JSON에서 파생되는 React Flow 그래프(`frontend/src/components/FlowDiagram.tsx`, `frontend/src/lib/contactFlowGraph.ts`)로 렌더링됩니다. `MermaidDiagram.tsx` 컴포넌트, `mermaid` 의존성, `dangerouslySetInnerHTML` / `securityLevel` 사용이 모두 삭제되어 이 XSS 위험은 더 이상 존재하지 않습니다.

---

### 2. S3 버킷 enforceSSL 미설정 (Low)

**파일:** `infrastructure/lib/aicc-builder-stack.ts`, `infrastructure/lib/knowledge-base-stack.ts`

**상태:** ✅ 조치 완료 — 3개 버킷 모두 `enforceSSL: true` 설정됨.

**대상 버킷:**

| 버킷 | 파일 | 위치 |
|---|---|---|
| `AssetsBucket` | `aicc-builder-stack.ts` | Line 136 |
| `FrontendBucket` | `aicc-builder-stack.ts` | Line 1092 |
| `KnowledgeBaseDocsBucket` | `knowledge-base-stack.ts` | Line 50 |

**수정 방법:** 각 `new s3.Bucket()` 호출에 `enforceSSL: true` 추가:

```typescript
// infrastructure/lib/aicc-builder-stack.ts — AssetsBucket
const assetsBucket = new s3.Bucket(this, "AssetsBucket", {
  // ... 기존 속성 유지
  enforceSSL: true,  // 추가
});

// infrastructure/lib/aicc-builder-stack.ts — FrontendBucket
const frontendBucket = new s3.Bucket(this, "FrontendBucket", {
  // ... 기존 속성 유지
  enforceSSL: true,  // 추가
});

// infrastructure/lib/knowledge-base-stack.ts — KnowledgeBaseDocsBucket
const docsBucket = new s3.Bucket(this, "KnowledgeBaseDocsBucket", {
  // ... 기존 속성 유지
  enforceSSL: true,  // 추가
});
```

`enforceSSL: true`는 CDK가 자동으로 S3 버킷 정책에 `aws:SecureTransport` 조건을 추가하여 HTTPS가 아닌 요청을 거부한다.

---

## 오탐 (False Positive) 항목

### 3. lambda_generator.py — Bandit B608: SQL injection (Medium, Confidence: Low)

**파일:** `backend/ecs/src/tools/lambda_generator.py` (Line 429, 548, 590)

**스캐너 판단:** f-string 안에 SQL 키워드(`INSERT INTO`, `SELECT`, `UPDATE`)가 포함되어 있어 SQL injection 가능성 경고

**오탐 사유:**

이 코드는 **SQL을 실행하는 코드가 아니라, Python 소스코드 템플릿을 문자열로 생성하는 코드**이다. `lambda_generator.py`는 고객 맞춤형 Lambda 함수의 소스코드를 생성하는 에이전트 도구로, f-string의 결과물은 `.py` 파일로 저장되는 코드 텍스트이다.

또한 생성되는 코드 자체도 안전한 패턴을 사용한다:

- **DynamoDB 코드** (Line 429): `ExpressionAttributeNames`, `ExpressionAttributeValues`를 사용한 parameterized 표현식
- **RDS 코드** (Line 548, 590): `rds_data.execute_statement()`의 `parameters` 인자를 통한 parameterized query (`:id`, `:field_name` 바인딩)

Bandit B608 룰은 "f-string에 SQL 패턴이 있으면 경고"하는 단순 패턴 매칭이므로, 코드 생성(code generation) 컨텍스트를 구분하지 못한다.

**조치:** ✅ 조치 완료 — 해당 라인(429, 548, 590)에 `# nosec B608` 주석이 추가되어 suppress됨:

```python
# Line 429
    elif op_type == "update":
        return f'''  # nosec B608 - code template generation, not SQL execution
        # Update item in DynamoDB

# Line 548
        return f'''  # nosec B608 - code template generation, not SQL execution
        # Insert into RDS

# Line 590
        return f'''  # nosec B608 - code template generation, not SQL execution
        # Select from RDS
```

---

## 무시 가능 항목

### 4. knowledge-base-stack.ts — SecureCdkBsc43: S3 access logging (Low)

**파일:** `infrastructure/lib/knowledge-base-stack.ts`

**스캐너 판단:** S3 버킷에 server access logging이 설정되지 않음

**무시 사유:**

- 이 프로젝트는 SA/세일즈 팀의 워크숍 PoC 생성 도구로, 프로덕션 서비스가 아님
- access logging을 위해 별도 로그 버킷을 생성하면 인프라 비용과 복잡도가 증가
- `removalPolicy: DESTROY`로 설정된 점에서도 임시 리소스 성격이 명확
- 프로덕션 전환 시 추가 검토 가능

### 5. infrastructure/dist/*.js — SecureCdkBsc17 (Low)

**파일:** `infrastructure/dist/aicc-builder-stack.js`, `infrastructure/dist/knowledge-base-stack.js`

**상태:** ✅ 해소됨 — 소스(`.ts`)에 `enforceSSL: true` 추가 후 재빌드되어 `dist/*.js`에도 반영됨 (`enforceSSL` 포함 확인). `dist/` 파일을 직접 수정할 필요 없음.
