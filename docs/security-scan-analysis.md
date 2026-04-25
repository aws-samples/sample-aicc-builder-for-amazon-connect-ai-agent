# Security Scan 분석 결과

> 분석일: 2026-03-26
> 스캐너: ACAT, Bandit

---

## 요약

| # | 파일 | 룰 | 심각도 | 판정 | 조치 |
|---|---|---|---|---|---|
| 1 | `frontend/src/components/MermaidDiagram.tsx` | dangerouslySetInnerHTML | High | 완화 필요 | securityLevel 변경 + DOMPurify 추가 |
| 2 | `backend/src/tools/lambda_generator.py` (x3) | Bandit B608 | Medium | **오탐 (False Positive)** | `# nosec B608` 주석 추가 |
| 3 | `infrastructure/lib/aicc-builder-stack.ts` (x2) | SecureCdkBsc17 | Low | 수정 필요 | `enforceSSL: true` 추가 |
| 4 | `infrastructure/lib/knowledge-base-stack.ts` | SecureCdkBsc17 | Low | 수정 필요 | `enforceSSL: true` 추가 |
| 5 | `infrastructure/lib/knowledge-base-stack.ts` | SecureCdkBsc43 | Low | 무시 가능 | PoC 용도로 access logging 불필요 |
| 6 | `infrastructure/dist/*.js` (x2) | SecureCdkBsc17 | Low | 무시 | 컴파일 결과물, 소스(.ts) 수정 시 자동 해결 |

---

## 수정 필요 항목

### 1. MermaidDiagram.tsx — dangerouslySetInnerHTML (High)

**파일:** `frontend/src/components/MermaidDiagram.tsx`

**현재 상태:**
- `mermaid.initialize()`에서 `securityLevel: 'loose'`로 설정되어 있어 mermaid 라벨 내 HTML/script 삽입 가능
- mermaid.render()가 반환한 SVG 문자열을 sanitize 없이 `dangerouslySetInnerHTML`로 주입

**위험:**
- `chart` prop은 LLM이 생성한 mermaid 코드에서 오는데, `securityLevel: 'loose'`일 때 mermaid가 라벨 안의 HTML 태그를 그대로 렌더링함
- 악의적인 mermaid 코드가 주입될 경우 XSS 가능성 존재

**수정 방법:**

(1) mermaid securityLevel을 `strict`으로 변경:

```tsx
// frontend/src/components/MermaidDiagram.tsx (Line 17)

// Before
securityLevel: 'loose',

// After
securityLevel: 'strict',
```

(2) DOMPurify 패키지 설치:

```bash
cd frontend
npm install dompurify
npm install -D @types/dompurify
```

(3) SVG sanitize 적용:

```tsx
// frontend/src/components/MermaidDiagram.tsx

import DOMPurify from 'dompurify';

// mermaid.render() 호출 후 (Line 143 부근)
const { svg: renderedSvg } = await mermaid.render(id, cleanChart);
setSvg(DOMPurify.sanitize(renderedSvg, { USE_PROFILES: { svg: true, svgFilters: true } }));
```

**참고:** `dangerouslySetInnerHTML` 자체는 mermaid.js가 SVG를 문자열로 반환하는 구조상 제거할 수 없으므로, scanner warning은 남을 수 있다. 위 조치로 실질적 XSS 위험은 해소된다.

---

### 2. S3 버킷 enforceSSL 미설정 (Low)

**파일:** `infrastructure/lib/aicc-builder-stack.ts`, `infrastructure/lib/knowledge-base-stack.ts`

**현재 상태:** S3 버킷 3개에 `enforceSSL` 옵션이 없어 HTTP 평문 접근이 가능

**대상 버킷:**

| 버킷 | 파일 | 위치 |
|---|---|---|
| `AssetsBucket` | `aicc-builder-stack.ts` | Line 137 |
| `FrontendBucket` | `aicc-builder-stack.ts` | Line 1091 |
| `KnowledgeBaseDocsBucket` | `knowledge-base-stack.ts` | Line 24 |

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

**파일:** `backend/src/tools/lambda_generator.py` (Line 429, 548, 590)

**스캐너 판단:** f-string 안에 SQL 키워드(`INSERT INTO`, `SELECT`, `UPDATE`)가 포함되어 있어 SQL injection 가능성 경고

**오탐 사유:**

이 코드는 **SQL을 실행하는 코드가 아니라, Python 소스코드 템플릿을 문자열로 생성하는 코드**이다. `lambda_generator.py`는 고객 맞춤형 Lambda 함수의 소스코드를 생성하는 에이전트 도구로, f-string의 결과물은 `.py` 파일로 저장되는 코드 텍스트이다.

또한 생성되는 코드 자체도 안전한 패턴을 사용한다:

- **DynamoDB 코드** (Line 429): `ExpressionAttributeNames`, `ExpressionAttributeValues`를 사용한 parameterized 표현식
- **RDS 코드** (Line 548, 590): `rds_data.execute_statement()`의 `parameters` 인자를 통한 parameterized query (`:id`, `:field_name` 바인딩)

Bandit B608 룰은 "f-string에 SQL 패턴이 있으면 경고"하는 단순 패턴 매칭이므로, 코드 생성(code generation) 컨텍스트를 구분하지 못한다.

**조치:** 해당 라인에 `# nosec B608` 주석을 추가하여 suppress:

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

**무시 사유:**

`dist/` 디렉토리는 TypeScript 소스의 컴파일 결과물이다. 소스 파일(`.ts`)에서 `enforceSSL: true`를 추가한 후 재빌드하면 자동으로 해결되므로, `dist/` 파일을 직접 수정할 필요 없음.
