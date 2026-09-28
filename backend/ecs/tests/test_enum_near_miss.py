"""Enum values the interview re-typed one syllable off the customer's document
are caught before generation, and a consent quote that wraps over several lines
of the document is still checked as one sentence."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import tools.requirement_items as ri  # noqa: E402
import tools.spec_manager as sm  # noqa: E402
from tools.requirement_items import enum_near_misses, quoted_sentences, split_requirement_items  # noqa: E402

DOC = (
    "| `productType` | string(enum) | Y | `벽걸이실내기` \\| `스탠드` \\| `시스템에어컨` \\| `천장형` (샘플, 확정 필요) |\n"
    "| `serviceType` | string(enum) | Y | `종합세척` \\| `기본세척` (샘플, 확정 필요) |\n"
    "| `status` | string(enum) | 예약 상태 | `CONFIRMED` \\| `PENDING` |\n"
    "코드 예: `TYPE1`, 설치 장소는 종합 세척 전에 확인한다.\n"
)


def spec(**fields):
    return {"input_fields": [{"name": n, "enum_values": v} for n, v in fields.items()]}


def test_a_value_one_syllable_off_is_reported_with_the_document_spelling():
    got = enum_near_misses(DOC, {"book": spec(serviceType=["종합세추", "기본세척"],
                                              productType=["뱅걸이실내기", "스탠드"])})
    assert {(e["field"], e["value"], e["document"]) for e in got} == {
        ("serviceType", "종합세추", "종합세척"), ("productType", "뱅걸이실내기", "벽걸이실내기")}
    assert {e["id"] for e in got} == {"E:book.serviceType=종합세추", "E:book.productType=뱅걸이실내기"}


def test_document_values_new_values_variants_and_digit_changes_pass():
    specs = {"book": spec(serviceType=["종합세척", "기본세척", "프리미엄세척"],
                          status=["CONFIRMED", "CANCELLED"], code=["TYPE2"], place=["가정용"])}
    assert enum_near_misses(DOC, specs) == []
    # the document writes the value with a space
    assert enum_near_misses("서비스: 종합 세척 / 기본 세척", {"b": spec(serviceType=["종합세척"])}) == []
    assert enum_near_misses("", {"b": spec(serviceType=["종합세추"])}) == []


def test_complete_interview_is_blocked_until_the_typo_is_fixed_or_excluded(monkeypatch):
    ledger = {"items": [{"id": "R1", "text": DOC, "section": "B1"}],
              "mappings": {"R1": {"target": "operation:book"}}, "signals": {}}
    monkeypatch.setattr(ri, "load_ledger", lambda: ledger)
    monkeypatch.setattr(ri, "_acxd_context", lambda sid: (False, None))
    monkeypatch.setattr(sm, "get_all_specs", lambda: {"book": spec(serviceType=["종합세추", "기본세척"])})
    problems = ri.requirement_coverage_problems()
    assert any("'종합세추'" in p and "'종합세척'" in p and "E:book.serviceType=종합세추" in p for p in problems)

    ledger["mappings"]["E:book.serviceType=종합세추"] = {"target": "excluded: the customer asked for it"}
    assert not any("종합세추" in p for p in ri.requirement_coverage_problems())

    monkeypatch.setattr(sm, "get_all_specs", lambda: {"book": spec(serviceType=["종합세척", "기본세척"])})
    ledger["mappings"].pop("E:book.serviceType=종합세추")
    assert not any("enum value" in p for p in ri.requirement_coverage_problems())


def test_a_consent_quote_that_wraps_over_three_lines_is_one_quote():
    doc = (
        "#### ▶ Operation B2 · `create_cleaning_reservation`\n"
        "- **비즈니스 규칙**\n"
        "  1. **개인정보 동의(privacyConsent)를 먼저 확인**한다. 안내 문구:\n"
        "     \"계약상담·접수·만족도조사를 위해 전화번호·성명·주소를 동의 철회 전까지 보관합니다.\n"
        "     동의는 거부하실 수 있고, 거부 시 서비스 이용이 제한될 수 있습니다. 동의하시면 '예',\n"
        "     아니면 '아니오'라고 말씀해 주세요.\"\n"
        "  2. 설치 장소(사업장/가정) 확인 → 대수 → 제품유형 → 서비스유형 → 가격 안내(B1) → 접수.\n"
    )
    quotes = quoted_sentences(split_requirement_items(doc))
    assert [q["text"] for q in quotes] == [
        "계약상담·접수·만족도조사를 위해 전화번호·성명·주소를 동의 철회 전까지 보관합니다. "
        "동의는 거부하실 수 있고, 거부 시 서비스 이용이 제한될 수 있습니다. 동의하시면 '예', "
        "아니면 '아니오'라고 말씀해 주세요."]


def test_a_shared_input_field_with_different_enums_blocks_until_aligned_or_excluded(monkeypatch):
    """Live (SELC, 2026-09-26): the typo was fixed in create_cleaning_reservation
    only; get_cleaning_price kept '종합세추' and D9 passed the bundle."""
    from tools.requirement_items import input_enum_conflicts

    specs = {"create_cleaning_reservation": spec(serviceType=["종합세척", "기본세척"]),
             "get_cleaning_price": spec(serviceType=["종합세추", "기본세추"])}
    got = input_enum_conflicts(specs)
    assert [c["id"] for c in got] == ["X:serviceType"]
    assert got[0]["variants"]["get_cleaning_price"] == ["종합세추", "기본세추"]
    # same values in another order, and a field only one operation takes, pass
    assert input_enum_conflicts({"a": spec(t=["x", "y"]), "b": spec(t=["y", "x"]), "c": spec(u=["z"])}) == []

    ledger = {"items": [{"id": "R1", "text": "서비스 유형: 종합세척, 기본세척", "section": "B1"}],
              "mappings": {"R1": {"target": "operation:get_cleaning_price"}}, "signals": {}}
    monkeypatch.setattr(ri, "load_ledger", lambda: ledger)
    monkeypatch.setattr(sm, "get_all_specs", lambda: specs)
    monkeypatch.setattr(ri, "_acxd_context", lambda sid: (False, None))
    assert not any("different enum values" in p for p in ri.requirement_coverage_problems())   # Classic
    monkeypatch.setattr(ri, "_acxd_context", lambda sid: (True, None))
    assert any("X:serviceType" in p and "different enum values" in p for p in ri.requirement_coverage_problems())
    ledger["mappings"]["X:serviceType"] = {"target": "excluded: two vocabularies on purpose"}
    assert not any("different enum values" in p for p in ri.requirement_coverage_problems())
