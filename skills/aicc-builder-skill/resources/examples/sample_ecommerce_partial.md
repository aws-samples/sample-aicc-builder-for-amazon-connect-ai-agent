# AICC Builder 사전 질문지 / Pre-Questionnaire

---

## 1. 기본 정보 / Basic Information

### 회사명 / Company Name
```
테크스토어 / TechStore
```

### 산업/도메인 / Industry/Domain
```
이커머스 / E-commerce (전자제품)
```

### 서비스 언어 / Service Language(s)
```
한국어
```

### 컨택 센터 주요 목적 / Primary Purpose of Contact Center
```
주문 조회, 배송 추적, 반품/교환 접수
```

---

## 2. API 운영 목록 / List of API Operations

### 예약/주문 관련 / Reservation/Order Operations
- [ ] 생성 (Create)
- [x] 조회 (Read/Search)
- [ ] 수정 (Update)
- [x] 취소 (Cancel)
- [ ] 기타 / Other: ________________

### 고객 정보 관련 / Customer Information Operations
- [x] 회원 조회 (Member Lookup)
- [ ] 정보 수정 (Update Info)
- [ ] 비밀번호 재설정 (Password Reset)
- [ ] 기타 / Other: ________________

### 결제/환불 관련 / Payment/Refund Operations
- [x] 결제 상태 확인 (Payment Status)
- [x] 환불 요청 (Request Refund)
- [ ] 청구서 조회 (View Invoice)
- [ ] 기타 / Other: ________________

### 문의/지원 관련 / Support Operations
- [x] FAQ 검색 (FAQ Search)
- [x] 상담원 연결 (Transfer to Agent)
- [x] 콜백 요청 (Callback Request)
- [x] 배송 추적 (Shipping Tracking)

---

## 3. 각 API 상세 정의 / Detailed API Specifications

### API: getOrderStatus

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| orderId | string | No | TS-XXXXXXXX 형식 | TS-20240315 |
| customerPhone | string | No | 010-XXXX-XXXX | 010-9876-5432 |

#### 데이터 소스 / Data Source
```
- DB 유형 / DB Type: MySQL
- 테이블명 / Table Name: orders
- 기본 키 / Primary Key: order_id
```

#### 비즈니스 규칙 / Business Rules
```
1. 주문번호 또는 전화번호 중 하나 필수
2. 전화번호 조회시 최근 10개 주문 반환
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    "orderId": "TS-20240315",
    "status": "배송중",
    "trackingNumber": "1234567890",
    "estimatedDelivery": "2024-03-17"
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| 주문 없음 | 주문을 찾을 수 없습니다 | 404 |

#### 부가 작업 / Side Effects
```
- 이메일 발송 / Send Email: No
- SMS 발송 / Send SMS: No
- 알림 / Notification: No
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
- 호스트 / Host: techstore-db.cluster-xxxxx.ap-northeast-2.rds.amazonaws.com
- 포트 / Port: 3306
- 데이터베이스명 / Database Name: techstore_prod
- 사용자명 / Username: api_user
- 비밀번호 / Password: [업로드 후 별도 입력]
```

---

## 5. AI 에이전트 성격 / AI Agent Personality

### 에이전트 이름 / Agent Name
```
테키 / Techy
```

### 말투/톤 / Tone of Voice
```
친근하고 캐주얼, 하지만 정확한 정보 전달
```

### 절대 하지 말아야 할 것 / Never Do/Say
```
1. 경쟁사 제품 언급 금지
2. 가격 협상 불가
```

### 상담원 연결 조건 / Escalation Triggers
```
1. 불량품 접수
2. 고객 불만
```

---

## 6. 보안 및 규정 / Security & Compliance

### 민감 정보 필드 / PII Fields
```
전화번호, 주소, 결제정보
```

### 권한 관리 / Authorization Requirements
```
[고객이 작성하지 않음 - AI가 질문 필요]
```

---

## 7. 추가 요청 사항 / Additional Requirements

```
[작성되지 않음]
```

---

## 작성 완료 체크리스트 / Completion Checklist

- [x] 기본 정보 작성 완료 / Basic info completed
- [x] 필요한 API 목록 선택 / Required APIs selected
- [ ] 각 API 상세 정의 작성 / API details defined (일부만 작성)
- [x] 데이터베이스 정보 입력 / Database info entered
- [x] AI 에이전트 성격 정의 / AI personality defined
- [ ] 보안 요구사항 확인 / Security requirements reviewed (부분 작성)
