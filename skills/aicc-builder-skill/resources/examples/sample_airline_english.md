# AICC Builder Pre-Questionnaire

---

## 1. Basic Information

### Company Name
```
SkyWings Airlines
```

### Industry/Domain
```
Airline / Aviation
```

### Service Language(s)
```
English, Korean, Japanese
```

### Primary Purpose of Contact Center
```
Flight booking management, check-in assistance, baggage inquiries, flight status updates
```

---

## 2. List of API Operations

### Reservation/Order Operations
- [x] Create (Book Flight)
- [x] Read/Search (Find Booking)
- [x] Update (Change Flight)
- [x] Cancel (Cancel Booking)
- [x] Other: Check-in

### Customer Information Operations
- [x] Member Lookup (Frequent Flyer)
- [x] Update Info (Contact Details)
- [ ] Password Reset
- [x] Other: Mileage Balance

### Payment/Refund Operations
- [x] Payment Status
- [x] Request Refund
- [x] View Invoice
- [ ] Other: ________________

### Support Operations
- [x] FAQ Search
- [x] Transfer to Agent
- [x] Callback Request
- [x] Other: Flight Status

---

## 3. Detailed API Specifications

### API: getFlightStatus

#### Input Fields
| Field Name | Type | Required | Validation | Example |
|------------|------|----------|------------|---------|
| flightNumber | string | Yes | SW + 3-4 digits | SW1234 |
| departureDate | date | Yes | YYYY-MM-DD | 2024-03-15 |

#### Data Source
```
- DB Type: DynamoDB
- Table Name: skywings-flights
- Primary Key: flightId
```

#### Business Rules
```
1. Flight status available 24 hours before departure
2. Show real-time gate information if within 3 hours of departure
3. Include weather delay information when applicable
```

#### Success Response
```json
{
  "status": "success",
  "data": {
    "flightNumber": "SW1234",
    "departure": {
      "airport": "ICN",
      "scheduledTime": "2024-03-15T10:00:00Z",
      "actualTime": "2024-03-15T10:15:00Z",
      "gate": "A12",
      "terminal": "T2"
    },
    "arrival": {
      "airport": "NRT",
      "scheduledTime": "2024-03-15T12:30:00Z",
      "estimatedTime": "2024-03-15T12:45:00Z"
    },
    "status": "DELAYED",
    "delayReason": "Weather conditions"
  }
}
```

#### Error Cases
| Scenario | Error Message | HTTP Code |
|----------|---------------|-----------|
| Flight not found | Flight SW1234 not found for the specified date | 404 |
| Invalid flight number | Please enter a valid flight number (e.g., SW1234) | 400 |
| Date too far | Flight information is only available within 7 days | 400 |

#### Side Effects
```
- Send Email: No
- Send SMS: No
- Notification: No
```

---

### API: findBooking

#### Input Fields
| Field Name | Type | Required | Validation | Example |
|------------|------|----------|------------|---------|
| bookingReference | string | No | 6 alphanumeric chars | ABC123 |
| lastName | string | Yes (if using reference) | 2-50 chars | Smith |
| ticketNumber | string | No | 13 digits | 1234567890123 |
| frequentFlyerNumber | string | No | SW + 9 digits | SW123456789 |

#### Data Source
```
- DB Type: DynamoDB
- Table Name: skywings-bookings
- Primary Key: bookingReference
```

#### Business Rules
```
1. Booking reference + last name OR ticket number OR frequent flyer number required
2. Show all segments for multi-city bookings
3. Include seat assignments and meal preferences
4. Display upgrade eligibility for frequent flyer members
```

#### Success Response
```json
{
  "status": "success",
  "data": {
    "bookingReference": "ABC123",
    "passengers": [
      {
        "name": "SMITH/JOHN",
        "ticketNumber": "1234567890123",
        "frequentFlyer": "SW123456789",
        "status": "Gold"
      }
    ],
    "flights": [
      {
        "flightNumber": "SW1234",
        "departure": "ICN",
        "arrival": "NRT",
        "date": "2024-03-15",
        "class": "Business",
        "seat": "2A",
        "meal": "Asian Vegetarian"
      }
    ],
    "totalPrice": {
      "amount": 850000,
      "currency": "KRW"
    }
  }
}
```

#### Error Cases
| Scenario | Error Message | HTTP Code |
|----------|---------------|-----------|
| Booking not found | We couldn't find a booking with these details | 404 |
| Last name mismatch | The last name doesn't match our records | 400 |
| Booking expired | This booking has already been used or expired | 410 |

#### Side Effects
```
- Send Email: No
- Send SMS: No
- Notification: No
```

---

## 4. Database Connection

### DynamoDB
```
- Region: ap-northeast-2
- Table Name: skywings-bookings, skywings-flights
```

### RDS (MySQL/PostgreSQL)
```
- Host:
- Port:
- Database Name:
- Username:
- Password:
```

---

## 5. AI Agent Personality

### Agent Name
```
Skylar
```

### Tone of Voice
```
Professional, helpful, and reassuring. Use airline industry terminology appropriately. Be empathetic during delays or cancellations.
```

### Never Do/Say
```
1. Never guarantee specific compensation amounts
2. Never blame weather or other airlines directly
3. Never share other passengers' information
4. Never make promises about upgrades without verification
```

### Escalation Triggers
```
1. Medical emergencies
2. Lost/damaged baggage claims over $500
3. Customer mentions legal action
4. Complaints about crew members
5. Requests for compensation over $200
```

---

## 6. Security & Compliance

### PII Fields
```
Passport number, date of birth, phone number, email, credit card details, frequent flyer number
```

### Authorization Requirements
```
Booking modifications require: Booking reference + Last name
Sensitive changes (name correction): Booking reference + Last name + Date of birth
Refunds: Booking reference + Last name + Last 4 digits of payment card
```

---

## 7. Additional Requirements

```
- Integration with flight status API for real-time updates
- Special handling for unaccompanied minors (UM service)
- Wheelchair and special assistance requests
- Pet travel inquiries
- COVID-19 travel requirements by destination
```

---

## Completion Checklist

- [x] Basic info completed
- [x] Required APIs selected
- [x] API details defined (partial - 2 of 5+ APIs)
- [x] Database info entered
- [x] AI personality defined
- [x] Security requirements reviewed
