"""The requirements document, item by item, until every item is a spec.

Live (2026-09-20): a document with `include_customer_phone_lookup=true`, a
`## FAQ` section of seven topics and "3회 실패" as an escalation rule produced
a contact flow with the lookup off, no knowledge base and no FAQ asset; the
review found nothing because it compares assets with the plan, not with the
document."""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

DOC = """# 삼성전자로지텍 AI 상담 요구사항

## 업무
- A1 배송조회: 10자리 주문번호로 배송 상태와 예상 배송일 안내
  - 예외: 주문번호 미보유/조회실패 → A2 전환
- A2 고객정보로 주문 찾기: 성명·주소·휴대폰으로 주문번호 검색
- A3 에어컨 세척 예약: 성명, 주소, 에어컨 종류(벽걸이형/스탠드형/시스템), 세척 프로그램, 희망 방문일(preferredDate)

## 정책
- 운영시간: 전문상담원 연결=평일09-18/토09-13. AI자동=24/7
- 에스컬레이션 조건: 상담원 요청/3회 실패/불만/환불·클레임
- 개인정보를 안내하기 전에 본인 확인을 한다

## FAQ
- 배송조회방법, 배송지연, 설치방법, 세척서비스안내, 가격·소요시간, 예약변경/취소, 개인정보처리방침

## 추가
- 전화기반 고객조회 사용 → include_customer_phone_lookup=true

| 주문번호 | 상태 |
|---|---|
| 1000000001 | 배송중 |
"""


def test_split_keeps_sections_bullets_continuations_and_tables():
    from tools.requirement_items import split_requirement_items
    items = split_requirement_items(DOC)
    by_text = {i["text"]: i for i in items}
    a1 = next(i for i in items if i["text"].startswith("A1 배송조회"))
    assert a1["section"] == "업무" and a1["kind"] == "bullet"
    nested = next(i for i in items if i["text"].startswith("예외: 주문번호 미보유"))
    assert nested["section"] == "업무"                       # a sub-bullet is a requirement of its own
    faq = [i for i in items if i["section"] == "FAQ"]
    assert len(faq) == 1 and "배송조회방법" in faq[0]["text"]
    tables = [i for i in items if i["kind"] == "table"]
    assert len(tables) == 1 and "1000000001" in tables[0]["text"] and "---" not in tables[0]["text"]
    assert [i["id"] for i in items] == [f"R{n}" for n in range(1, len(items) + 1)]
    assert "# 삼성전자로지텍 AI 상담 요구사항" not in by_text   # headings are sections, not items


def test_signals_read_the_literal_statements():
    from tools.requirement_items import split_requirement_items, requirement_signals
    items = split_requirement_items(DOC)
    signals = requirement_signals(items)
    assert signals["phone_lookup"]["value"] is True
    assert signals["max_failures"]["value"] == 3
    assert len(signals["faq"]["item_ids"]) == 1
    assert len(signals["identity_verification"]["item_ids"]) == 1


@pytest.fixture()
def ledger(monkeypatch):
    """An in-memory workspace: the ledger round-trips without S3 or NFS."""
    import tools.requirement_items as ri

    store: dict = {}

    class _WS:
        def save_requirement_items(self, data):
            store["ledger"] = data

        def load_requirement_items(self):
            return store.get("ledger")

    monkeypatch.setattr(ri, "_workspace", lambda: _WS())
    return ri


def test_register_map_and_coverage_gate(ledger, monkeypatch):
    ri = ledger
    import tools.spec_manager as sm
    import tools.acxd_flow_spec as afs

    summary = ri.register_document(DOC)
    assert summary["item_count"] >= 8 and summary["mapped"] == 0

    # Specs that name the operations: items spelling those identifiers are auto-covered.
    class _F:
        def __init__(self, name): self.name = name

    class _Spec:
        def __init__(self, fields): self.input_fields, self.output_fields, self.tools = fields, [], []

    monkeypatch.setattr(sm, "get_all_specs", lambda: {
        "get_delivery_status": _Spec([_F("orderNumber")]),
        "create_cleaning_reservation": _Spec([_F("preferredDate"), _F("phoneNumber")]),
    })

    class _Flow:
        def __init__(self, fid, steps): self.flow_id, self.steps = fid, steps

    class _Step:
        def __init__(self, d): self.description = d

    class _KB:
        topics: list = []

    class _FlowSpec:
        flows = [_Flow("CleaningReservation", [_Step("고객명·주소를 수집")])]
        knowledge_base = _KB()

    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: _FlowSpec())
    monkeypatch.setattr(afs, "is_acxd_target", lambda *a, **k: True)

    class _CF:
        include_customer_phone_lookup = False

    monkeypatch.setattr(sm, "get_contact_flow_spec", lambda: _CF())

    rows = ri.items_with_status(ri.load_ledger())
    statuses = {r["id"]: r["status"] for r in rows}
    a3 = next(r for r in rows if r["text"].startswith("A3"))
    assert a3["status"] == "auto" and a3["target"].startswith("field:create_cleaning_reservation.preferredDate")
    assert "unmapped" in statuses.values()

    problems = ri.requirement_coverage_problems("s1")
    joined = "\n".join(problems)
    assert "reflected in no spec" in joined
    assert "include_customer_phone_lookup=true" in joined and "contact flow spec has False" in joined
    assert "FAQ section" in joined and "plans no topics" in joined
    assert "identity verification" in joined

    # Map everything; exclude identity verification with the customer's reason.
    unmapped = [r["id"] for r in rows if r["status"] == "unmapped"]
    identity_id = ri.load_ledger()["signals"]["identity_verification"]["item_ids"][0]
    faq_id = ri.load_ledger()["signals"]["faq"]["item_ids"][0]
    lookup_id = ri.load_ledger()["signals"]["phone_lookup"]["item_id"]
    result = ri.map_requirement_items([
        *[{"item_id": i, "target": "session_config"} for i in unmapped if i not in (identity_id, faq_id, lookup_id)],
        {"item_id": identity_id, "target": "excluded", "note": "PoC 범위 제외 (고객 확인)"},
        {"item_id": faq_id, "target": "kb"},
        {"item_id": lookup_id, "target": "contact_flow"},
        {"item_id": "R999", "target": "kb"},                               # unknown id → rejected
        {"item_id": unmapped[0], "target": "operation:no_such_op"},        # unknown operation → rejected
    ])
    assert result["success"] and result["remaining_unmapped"] == []
    assert any("R999" in r for r in result["rejected"]) and any("no_such_op" in r for r in result["rejected"])

    problems = ri.requirement_coverage_problems("s1")
    # mapping does not silence the literal checks: the flag and the KB are still wrong…
    assert any("include_customer_phone_lookup" in p for p in problems)
    assert any("FAQ section" in p for p in problems)
    # …but the excluded identity requirement no longer blocks
    assert not any("identity verification" in p for p in problems)

    # …and once the spec says what the document says, the gate is clean.
    _CF.include_customer_phone_lookup = True
    _KB.topics = ["배송조회방법", "배송지연"]
    assert ri.requirement_coverage_problems("s1") == []


def test_no_document_means_no_problems(ledger):
    assert ledger.requirement_coverage_problems("s1") == []
    assert ledger.list_requirement_items()["items"] == []
