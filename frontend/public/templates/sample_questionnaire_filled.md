# AICC Builder 사전 질문지 / Pre-Questionnaire

이 문서를 작성하여 업로드하시면, AI가 자동으로 분석하여 컨택 센터 구축을 시작합니다.
작성되지 않은 항목은 AI가 대화를 통해 추가 질문합니다.

Complete this questionnaire and upload it. The AI will analyze it and begin building your contact center.
Any unanswered sections will be clarified through follow-up questions.

---

## 1. 기본 정보 / Basic Information

### 회사명 / Company Name
```
스타호텔
```

### 산업/도메인 / Industry/Domain
```
호텔
```

### 서비스 언어 / Service Language(s)
```
한국어, 영어
```

### 컨택 센터 주요 목적 / Primary Purpose of Contact Center
```
호텔 예약 조회, 예약 변경, 예약 취소, 객실 문의 응대
```

---

## 2. API 운영 목록 / List of API Operations

필요한 모든 작업을 나열해주세요. / List all operations your AI agent should support.

### 예약/주문 관련 / Reservation/Order Operations
- [x] 생성 (Create)
- [x] 조회 (Read/Search)
- [x] 수정 (Update)
- [x] 취소 (Cancel)
- [ ] 기타 / Other: ________________

### 고객 정보 관련 / Customer Information Operations
- [x] 회원 조회 (Member Lookup)
- [ ] 정보 수정 (Update Info)
- [ ] 비밀번호 재설정 (Password Reset)
- [ ] 기타 / Other: ________________

### 결제/환불 관련 / Payment/Refund Operations
- [ ] 결제 상태 확인 (Payment Status)
- [x] 환불 요청 (Request Refund)
- [ ] 청구서 조회 (View Invoice)
- [ ] 기타 / Other: ________________

### 문의/지원 관련 / Support Operations
- [x] FAQ 검색 (FAQ Search)
- [x] 상담원 연결 (Transfer to Agent)
- [ ] 콜백 요청 (Callback Request)
- [ ] 기타 / Other: ________________

---

## 3. 각 API 상세 정의 / Detailed API Specifications

각 API에 대해 아래 템플릿을 복사하여 작성해주세요.
Copy and fill out this template for each API operation.

### API: 예약 조회

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| reservation_id | string | Yes | 8자리 영숫자 | ABC12345 |
| phone_number | string | Yes | 한국 전화번호 형식 | 010-1234-5678 |

#### 데이터 소스 / Data Source
```
DB 유형 / DB Type: DynamoDB
테이블명 / Table Name: reservations
기본 키 / Primary Key: reservation_id
```

#### 비즈니스 규칙 / Business Rules
```
1. 예약번호와 전화번호가 일치해야 조회 가능
2. 체크인 날짜가 지난 예약도 조회 가능
3. 취소된 예약은 별도 표시
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    "reservation_id": "ABC12345",
    "guest_name": "홍길동",
    "room_type": "디럭스 더블",
    "check_in_date": "2024-03-15",
    "check_out_date": "2024-03-17",
    "total_amount": 350000,
    "status": "confirmed"
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| 예약번호 불일치 | 예약 정보를 찾을 수 없습니다 | 404 |
| 전화번호 불일치 | 본인 확인에 실패했습니다 | 401 |

#### 부가 작업 / Side Effects
```
이메일 발송 / Send Email: No
SMS 발송 / Send SMS: No
알림 / Notification: No
```

### API: 예약 취소

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| reservation_id | string | Yes | 8자리 영숫자 | ABC12345 |
| phone_number | string | Yes | 한국 전화번호 형식 | 010-1234-5678 |
| cancel_reason | string | No | 최대 500자 | 일정 변경 |

#### 데이터 소스 / Data Source
```
DB 유형 / DB Type: DynamoDB
테이블명 / Table Name: reservations
기본 키 / Primary Key: reservation_id
```

#### 비즈니스 규칙 / Business Rules
```
1. 체크인 3일 전까지 무료 취소
2. 체크인 1-2일 전 취소 시 50% 위약금
3. 체크인 당일 취소 불가
4. 이미 취소된 예약은 재취소 불가
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    "reservation_id": "ABC12345",
    "cancellation_fee": 0,
    "refund_amount": 350000,
    "cancelled_at": "2024-03-10T14:30:00Z"
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| 당일 취소 | 체크인 당일은 취소가 불가능합니다 | 400 |
| 이미 취소됨 | 이미 취소된 예약입니다 | 400 |

#### 부가 작업 / Side Effects
```
이메일 발송 / Send Email: Yes - 취소 확인 이메일
SMS 발송 / Send SMS: Yes - 취소 확인 문자
알림 / Notification: No
```

---

## 4. 데이터베이스 연결 정보 / Database Connection

### DynamoDB
```
리전 / Region: ap-northeast-2
테이블명 / Table Name: reservations
```

### RDS (MySQL/PostgreSQL)
```
호스트 / Host:
포트 / Port:
데이터베이스명 / Database Name:
사용자명 / Username:
비밀번호 / Password: [업로드 후 별도 입력 / Enter separately after upload]
```

---

## 5. AI 에이전트 성격 / AI Agent Personality

### 에이전트 이름 / Agent Name
```
스타도우미
```

### 말투/톤 / Tone of Voice
```
친근하고 정중한 존댓말
```

### 절대 하지 말아야 할 것 / Never Do/Say
```
1. 다른 호텔 추천하지 않기
2. 가격 할인 약속하지 않기
3. 개인정보 요청 시 전화번호 외 다른 정보 요청하지 않기
```

### 상담원 연결 조건 / Escalation Triggers
```
1. 고객이 3회 이상 같은 문제를 반복 언급할 때
2. 고객이 명시적으로 상담원 연결을 요청할 때
3. 환불 금액 분쟁이 발생했을 때
```

---

## 6. 보안 및 규정 / Security & Compliance

### 민감 정보 필드 / PII Fields
```
전화번호, 이메일, 신용카드 번호
```

### 권한 관리 / Authorization Requirements
```
예약 조회/수정/취소 시 전화번호 인증 필수
```

---

## 7. 추가 요청 사항 / Additional Requirements

```
VIP 고객(연 10회 이상 투숙)에게는 특별 할인 안내 가능
```

---

## 작성 완료 체크리스트 / Completion Checklist

- [x] 기본 정보 작성 완료 / Basic info completed
- [x] 필요한 API 목록 선택 / Required APIs selected
- [x] 각 API 상세 정의 작성 / API details defined
- [x] 데이터베이스 정보 입력 / Database info entered
- [x] AI 에이전트 성격 정의 / AI personality defined
- [x] 보안 요구사항 확인 / Security requirements reviewed

---

*이 문서를 작성 후 AICC Builder에 업로드하세요. AI가 분석하여 자동으로 빌딩을 시작합니다.*
*Upload this document to AICC Builder after completion. The AI will analyze and begin building automatically.*
