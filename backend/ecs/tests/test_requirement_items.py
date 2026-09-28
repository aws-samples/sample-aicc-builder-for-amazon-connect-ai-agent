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


def test_acxd_only_does_not_ask_for_a_contact_flow_phone_lookup(ledger, monkeypatch):
    """ACXD only withholds save_contact_flow_spec: the phone-lookup check could
    only be cleared by excluding the item (live on dev, 2026-09-25)."""
    ri = ledger
    import tools.spec_manager as sm
    import tools.acxd_flow_spec as afs

    ri.register_document(DOC)
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(sm, "get_contact_flow_spec", lambda: None)
    monkeypatch.setattr(afs, "is_acxd_target", lambda *a, **k: True)
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: None)

    monkeypatch.setattr(afs, "is_acxd_only_target", lambda *a, **k: False)
    assert any("include_customer_phone_lookup" in p for p in ri.requirement_coverage_problems("s1"))

    monkeypatch.setattr(afs, "is_acxd_only_target", lambda *a, **k: True)
    assert not any("include_customer_phone_lookup" in p for p in ri.requirement_coverage_problems("s1"))


QUOTE_DOC = """# 상담원 이관
- 이관 직전 안내: "상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다."
- 회사명은 "삼성전자로지텍"이고 단가는 "벽걸이 실내기 대당 10만원"입니다.
"""


def test_quoted_sentences_are_speech_only():
    import tools.requirement_items as ri
    quotes = ri.quoted_sentences(ri.split_requirement_items(QUOTE_DOC))
    assert [q["text"] for q in quotes] == ["상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다."]


def test_a_callers_example_utterance_is_not_tracked():
    """Hanbit's expected-dialogue section quotes what the CALLER says."""
    import tools.requirement_items as ri
    doc = ("## 규칙\n- 미등록 → \"초진 환자는 접수 시 등록됩니다\" 안내 후 계속 진행\n"
           "- 1. \"에어컨 세척하려고요\" → 개인정보 동의 확인\n\n"
           "## 섹션 4 · 기대 대화 (테스트 기준)\n- 인사 → 업무 안내. \"진료 예약하고 싶어요\" → 전화번호\n")
    assert [q["text"] for q in ri.quoted_sentences(ri.split_requirement_items(doc))] == ["초진 환자는 접수 시 등록됩니다"]


def test_a_quoted_sentence_the_plan_rewords_blocks_until_copied_or_excluded(ledger, monkeypatch):
    """e2e 2026-09-25: the plan said "이전에 전달해 주신" for the document's "이전 전달주신"."""
    ri = ledger
    import tools.spec_manager as sm
    import tools.acxd_flow_spec as afs

    ri.register_document(QUOTE_DOC)
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(sm, "get_contact_flow_spec", lambda: None)
    monkeypatch.setattr(sm, "get_session_flow_config", lambda: None)
    monkeypatch.setattr(afs, "is_acxd_target", lambda *a, **k: True)
    reworded = {"flows": [{"steps": [{"template": "상담사를 연결해드리겠습니다. 이전에 전달해 주신 정보는 상담사에게 자동 전달됩니다."}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: reworded)
    assert any("verbatim" in p and "이전 전달주신" in p for p in ri.requirement_coverage_problems("s1"))

    verbatim = {"flows": [{"steps": [{"template": "상담사를 연결해드리겠습니다.  이전 전달주신 정보는 상담사에게 자동 전달됩니다."}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: verbatim)
    assert not any("verbatim" in p for p in ri.requirement_coverage_problems("s1"))


def test_a_system_plan_counts_only_its_template(ledger, monkeypatch):
    """The builder speaks a welcome/fallback/escalation plan's `template` and
    nothing else — a sentence left in the step's description is never said
    (SELC e2e, 2026-09-26). An operation plan's description still counts: it
    feeds the flow generator."""
    ri = ledger
    import tools.spec_manager as sm
    import tools.acxd_flow_spec as afs

    ri.register_document(QUOTE_DOC)
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(sm, "get_contact_flow_spec", lambda: None)
    monkeypatch.setattr(sm, "get_session_flow_config", lambda: None)
    monkeypatch.setattr(afs, "is_acxd_target", lambda *a, **k: True)
    line = "상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다."
    in_description = {"flows": [{"flow_id": "EscalationFlow", "role": "escalation", "steps": [
        {"step": 1, "node_type": "escalate", "description": f"'{line}' 안내 후 이관"}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: in_description)
    problems = [p for p in ri.requirement_coverage_problems("s1") if "verbatim" in p]
    assert problems and "only the step's `template` is spoken" in problems[0]

    in_template = {"flows": [{"flow_id": "EscalationFlow", "role": "escalation", "steps": [
        {"step": 1, "node_type": "escalate", "description": "이관 안내", "template": line}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: in_template)
    assert not any("verbatim" in p for p in ri.requirement_coverage_problems("s1"))

    operation = {"flows": [{"flow_id": "Delivery", "role": "operation", "steps": [
        {"step": 1, "node_type": "generative_journey", "description": f"실패 시 '{line}'라고 안내"}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: operation)
    assert not any("verbatim" in p for p in ri.requirement_coverage_problems("s1"))


def test_one_quote_of_a_table_item_can_be_excluded_without_the_other(ledger, monkeypatch):
    """SELC's hand-off table holds the queue notice (the customer's Contact Flow
    in ACXD only) and the hand-off line the plan must say, in ONE item."""
    ri = ledger
    import tools.spec_manager as sm
    import tools.acxd_flow_spec as afs

    doc = ("## 이관\n| 요구 | 구현 |\n|---|---|\n"
           "| 대기열 초과 → \"현재 상담량이 많아 지연되고 있습니다. 잠시 후 다시 시도해 주세요.\" 안내 | 큐 |\n"
           "| 안내 멘트 \"상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다.\" | 이관 |\n")
    ri.register_document(doc)
    quotes = ri.load_ledger()["signals"]["quotes"]
    assert [q["id"] for q in quotes] == ["Q1", "Q2"] and quotes[0]["item_id"] == quotes[1]["item_id"]
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(sm, "get_contact_flow_spec", lambda: None)
    monkeypatch.setattr(sm, "get_session_flow_config", lambda: None)
    monkeypatch.setattr(afs, "is_acxd_target", lambda *a, **k: True)
    plan = {"flows": [{"steps": [{"template": "상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다."}]}]}
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda *a, **k: plan)
    problems = [p for p in ri.requirement_coverage_problems("s1") if "verbatim" in p]
    assert problems and "Q1" in problems[0] and "Q2" not in problems[0]
    result = ri.map_requirement_items([{"item_id": "Q1", "target": "excluded",
                                        "note": "ACXD only: the customer's Contact Flow announces the queue"}])
    assert not result.get("rejected"), result
    assert not any("verbatim" in p for p in ri.requirement_coverage_problems("s1"))
    # Live (Hanbit, 2026-09-26): the model sent the list JSON-encoded as a string.
    import json as _json
    again = ri.map_requirement_items(_json.dumps([{"item_id": "Q1", "target": "excluded",
                                                   "note": "said by the customer's Contact Flow"}]))
    assert again.get("success") and not again.get("rejected"), again
    assert ri.map_requirement_items("not json")["success"] is False
