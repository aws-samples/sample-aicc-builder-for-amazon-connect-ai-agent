# AICC Builder 사전 질문지 / Pre-Questionnaire

---

## 1. 기본 정보 / Basic Information

### 회사명 / Company Name
```
스카이뷰 호텔 / SkyView Hotel
```

### 산업/도메인 / Industry/Domain
```
호텔 / Hospitality
```

### 서비스 언어 / Service Language(s)
```
한국어, 영어
```

### 컨택 센터 주요 목적 / Primary Purpose of Contact Center
```
호텔 예약 생성, 조회, 수정, 취소 및 고객 문의 응대
```

---

## 2. API 운영 목록 / List of API Operations

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

### API: createReservation

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| guestName | string | Yes | 2-50자, 한글/영문만 | 홍길동 |
| guestPhone | string | Yes | 010-XXXX-XXXX 형식 | 010-1234-5678 |
| guestEmail | string | Yes | 이메일 형식 | guest@email.com |
| checkInDate | date | Yes | YYYY-MM-DD, 오늘 이후 | 2024-03-15 |
| checkOutDate | date | Yes | YYYY-MM-DD, 체크인 이후 | 2024-03-17 |
| roomType | enum | Yes | STANDARD, DELUXE, SUITE | DELUXE |
| guestCount | number | Yes | 1-4명 | 2 |
| specialRequests | string | No | 최대 500자 | 늦은 체크인 요청 |

#### 데이터 소스 / Data Source
```
- DB 유형 / DB Type: DynamoDB
- 테이블명 / Table Name: skyview-reservations
- 기본 키 / Primary Key: reservationId
```

#### 비즈니스 규칙 / Business Rules
```
1. 체크인 날짜는 오늘 이후여야 함
2. 체크아웃은 체크인 이후여야 함
3. 최대 연박 가능일 14일
4. 예약 전 객실 가용성 확인 필수
5. 동일 날짜 동일 객실 타입 중복 예약 불가
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    "reservationId": "RSV-20240315-001",
    "guestName": "홍길동",
    "roomType": "DELUXE",
    "checkInDate": "2024-03-15",
    "checkOutDate": "2024-03-17",
    "totalPrice": 440000,
    "confirmationCode": "SKV123456"
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| 객실 매진 | 선택하신 날짜에 해당 객실이 매진되었습니다 | 409 |
| 잘못된 날짜 | 체크인/체크아웃 날짜를 확인해 주세요 | 400 |
| 최대 연박 초과 | 최대 14일까지 예약 가능합니다 | 400 |

#### 부가 작업 / Side Effects
```
- 이메일 발송 / Send Email: Yes - 예약 확인 이메일 발송
- SMS 발송 / Send SMS: Yes - 예약 확인 문자 발송
- 알림 / Notification: No
```

---

### API: getReservation

#### 입력 필드 / Input Fields
| 필드명 / Field Name | 타입 / Type | 필수 / Required | 유효성 검사 / Validation | 예시 / Example |
|---------------------|-------------|-----------------|--------------------------|----------------|
| reservationId | string | No | RSV-XXXXXXXX-XXX 형식 | RSV-20240315-001 |
| guestPhone | string | No | 010-XXXX-XXXX 형식 | 010-1234-5678 |
| confirmationCode | string | No | 6자리 영숫자 | SKV123456 |

#### 데이터 소스 / Data Source
```
- DB 유형 / DB Type: DynamoDB
- 테이블명 / Table Name: skyview-reservations
- 기본 키 / Primary Key: reservationId
```

#### 비즈니스 규칙 / Business Rules
```
1. reservationId, guestPhone, confirmationCode 중 하나 이상 필수
2. 전화번호 조회시 최근 5개 예약만 반환
3. 체크아웃 완료된 예약은 90일간 조회 가능
```

#### 성공 응답 / Success Response
```json
{
  "status": "success",
  "data": {
    "reservationId": "RSV-20240315-001",
    "guestName": "홍길동",
    "roomType": "DELUXE",
    "roomNumber": "1205",
    "checkInDate": "2024-03-15",
    "checkOutDate": "2024-03-17",
    "status": "CONFIRMED",
    "totalPrice": 440000
  }
}
```

#### 에러 케이스 / Error Cases
| 상황 / Scenario | 에러 메시지 / Error Message | HTTP 코드 / HTTP Code |
|-----------------|------------------------------|----------------------|
| 예약 없음 | 해당 정보로 예약을 찾을 수 없습니다 | 404 |
| 조회 조건 없음 | 예약번호, 전화번호, 확인코드 중 하나를 입력해 주세요 | 400 |

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
- 리전 / Region: ap-northeast-2
- 테이블명 / Table Name: skyview-reservations
```

### RDS (MySQL/PostgreSQL)
```
- 호스트 / Host:
- 포트 / Port:
- 데이터베이스명 / Database Name:
- 사용자명 / Username:
- 비밀번호 / Password:
```

---

## 5. AI 에이전트 성격 / AI Agent Personality

### 에이전트 이름 / Agent Name
```
스카이 / Sky
```

### 말투/톤 / Tone of Voice
```
정중하고 따뜻한 호텔리어 스타일, 존댓말 사용
```

### 절대 하지 말아야 할 것 / Never Do/Say
```
1. 다른 호텔 추천하지 않기
2. 가격 할인 약속하지 않기
3. 객실 업그레이드 임의로 약속하지 않기
```

### 상담원 연결 조건 / Escalation Triggers
```
1. 고객이 화가 났을 때
2. 결제/환불 관련 복잡한 문의
3. 불만 접수 시
4. 3번 이상 같은 질문 반복 시
```

---

## 6. 보안 및 규정 / Security & Compliance

### 민감 정보 필드 / PII Fields
```
전화번호, 이메일, 신용카드 번호
```

### 권한 관리 / Authorization Requirements
```
예약 조회/수정/취소는 본인 인증(전화번호 + 예약번호 또는 확인코드) 후 가능
```

---

## 7. 추가 요청 사항 / Additional Requirements

```
- 체크인 전날 리마인더 SMS 발송 기능 필요
- VIP 고객(연간 10회 이상 투숙) 자동 인식 및 특별 인사
```

---

## 작성 완료 체크리스트 / Completion Checklist

- [x] 기본 정보 작성 완료 / Basic info completed
- [x] 필요한 API 목록 선택 / Required APIs selected
- [x] 각 API 상세 정의 작성 / API details defined
- [x] 데이터베이스 정보 입력 / Database info entered
- [x] AI 에이전트 성격 정의 / AI personality defined
- [x] 보안 요구사항 확인 / Security requirements reviewed
