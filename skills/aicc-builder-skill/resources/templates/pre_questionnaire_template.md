# AICC Builder 사전 질문지 / Pre-Questionnaire

이 문서를 작성하여 업로드하시면, AI가 자동으로 분석하여 컨택 센터 구축을 시작합니다.
작성되지 않은 항목은 AI가 대화를 통해 추가 질문합니다.

Complete this questionnaire and upload it. The AI will analyze it and begin building your contact center.
Any unanswered sections will be clarified through follow-up questions.

---

## 1. 기본 정보 / Basic Information

### 회사명 / Company Name
```
[회사명을 입력하세요 / Enter your company name]
```

### 산업/도메인 / Industry/Domain
```
[예: 호텔, 항공, 의료, 금융, 이커머스 등 / e.g., Hotel, Airline, Healthcare, Finance, E-commerce]
```

### 서비스 언어 / Service Language(s)
```
[예: 한국어, 영어, 일본어 / e.g., Korean, English, Japanese]
```

### 컨택 센터 주요 목적 / Primary Purpose of Contact Center
```
[고객이 전화로 무엇을 하길 원하나요? / What should customers be able to do via phone?]
```

---

## 2. API 운영 목록 / List of API Operations

필요한 모든 작업을 나열해주세요. / List all operations your AI agent should support.

### 예약/주문 관련 / Reservation/Order Operations
- [ ] 생성 (Create)
- [ ] 조회 (Read/Search)
- [ ] 수정 (Update)
- [ ] 취소 (Cancel)
- [ ] 기타 / Other: ________________

### 고객 정보 관련 / Customer Information Operations
- [ ] 회원 조회 (Member Lookup)
- [ ] 정보 수정 (Update Info)
- [ ] 비밀번호 재설정 (Password Reset)
- [ ] 기타 / Other: ________________

### 결제/환불 관련 / Payment/Refund Operations
- [ ] 결제 상태 확인 (Payment Status)
- [ ] 환불 요청 (Request Refund)
- [ ] 청구서 조회 (View Invoice)
- [ ] 기타 / Other: ________________

### 문의/지원 관련 / Support Operations
- [ ] FAQ 검색 (FAQ Search)
- [ ] 상담원 연결 (Transfer to Agent)
- [ ] 콜백 요청 (Callback Request)
- [ ] 기타 / Other: ________________

---

## 3. 각 API 상세 정의 / Detailed API Specifications

각 API에 대해 아래 템플릿을 복사하여 작성해주세요.
Copy and fill out this template for each API operation.

### API: [작업명 / Operation Name]

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| | | Yes/No | | |
| | | Yes/No | | |
| | | Yes/No | | |

#### 데이터 소스 / Data Source
```
- DB 유형 / DB Type: [DynamoDB / MySQL / PostgreSQL / 외부 API]
- 테이블명 / Table Name:
- 기본 키 / Primary Key:
```

#### 비즈니스 규칙 / Business Rules
```
1. [규칙 1 / Rule 1]
2. [규칙 2 / Rule 2]
3. [규칙 3 / Rule 3]
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    // 응답 필드를 정의하세요 / Define response fields
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| | | |
| | | |

#### 부가 작업 / Side Effects
```
- 이메일 발송 / Send Email: [Yes/No] - 내용/Content:
- SMS 발송 / Send SMS: [Yes/No] - 내용/Content:
- 알림 / Notification: [Yes/No] - 대상/To:
```

---

## 4. 데이터베이스 연결 정보 / Database Connection

### DynamoDB
```
- 리전 / Region:
- 테이블명 / Table Name:
```

### RDS (MySQL/PostgreSQL)
```
- 호스트 / Host:
- 포트 / Port:
- 데이터베이스명 / Database Name:
- 사용자명 / Username:
- 비밀번호 / Password: [업로드 후 별도 입력 / Enter separately after upload]
```

---

## 5. AI 에이전트 성격 / AI Agent Personality

### 에이전트 이름 / Agent Name
```
[예: 도우미, Alex, 지니 / e.g., Helper, Alex, Genie]
```

### 말투/톤 / Tone of Voice
```
[예: 친근하고 캐주얼 / 정중하고 격식체 / 전문적이고 간결
 e.g., Friendly & Casual / Polite & Formal / Professional & Concise]
```

### 절대 하지 말아야 할 것 / Never Do/Say
```
1.
2.
3.
```

### 상담원 연결 조건 / Escalation Triggers
```
1. [예: 고객이 화가 났을 때 / e.g., When customer is angry]
2. [예: 3번 이상 같은 질문을 반복할 때 / e.g., Same question asked 3+ times]
3.
```

---

## 6. 보안 및 규정 / Security & Compliance

### 민감 정보 필드 / PII Fields
```
[예: 전화번호, 이메일, 신용카드 번호 등 마스킹이 필요한 필드]
[e.g., Phone, Email, Credit Card Number - fields that need masking]
```

### 권한 관리 / Authorization Requirements
```
[예: 본인 인증 후에만 예약 조회 가능 / e.g., Reservation lookup only after identity verification]
```

---

## 7. 추가 요청 사항 / Additional Requirements

```
[기타 특별한 요구사항이나 참고사항을 자유롭게 작성해주세요]
[Any other special requirements or notes]
```

---

## 작성 완료 체크리스트 / Completion Checklist

- [ ] 기본 정보 작성 완료 / Basic info completed
- [ ] 필요한 API 목록 선택 / Required APIs selected
- [ ] 각 API 상세 정의 작성 / API details defined
- [ ] 데이터베이스 정보 입력 / Database info entered
- [ ] AI 에이전트 성격 정의 / AI personality defined
- [ ] 보안 요구사항 확인 / Security requirements reviewed

---

*이 문서를 작성 후 AICC Builder에 업로드하세요. AI가 분석하여 자동으로 빌딩을 시작합니다.*
*Upload this document to AICC Builder after completion. The AI will analyze and begin building automatically.*
