"""An input mask never hides a value the flows collect.

Live (5-use-case run, 2026-09-28): none of the five requirement documents asked
for masking, yet the interview planned PII masks, and ACXD masks the caller's
words BEFORE the flow reads them. Daon's birth-date mask (``\\d{8}``) turned
"19900512" into "[REDACTED]" and identity verification asked for the birth date
again and again; SELC's phone mask (``\\d{3}-\\d{4}-\\d{4}``) was followed by an
AgentFailure; Hanul's phone mask made the claim's contact number unusable when
said with dashes. The value is already marked ``sensitive`` on its slot.
"""
from __future__ import annotations

import copy
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_resource_builders import (  # noqa: E402
    build_guardrails,
    mask_hidden_slots,
    regex_samples,
)
from tools.validate_acxd_consistency import normalize_bundle_guardrails  # noqa: E402
from tools.validate_acxd_flow import validate_acxd_asset  # noqa: E402

DAON_SLOTS = [
    {"name": "phoneNumber", "type": "NLX.PhoneNumber", "regex": r"^010-?\d{4}-?\d{4}$"},
    {"name": "birthDate", "type": "NLX.AlphaNumeric", "regex": r"^\d{8}$", "sensitive": True},
]
SELC_SLOTS = [
    {"name": "quantity", "type": "NLX.Number"},
    {"name": "customerName", "type": "NLX.Text"},
    {"name": "phoneNumber", "type": "NLX.PhoneNumber", "regex": "^010-[0-9]{4}-[0-9]{4}$", "sensitive": True},
    {"name": "productType", "type": "productType"},
]


def _spec(guardrails, slots):
    return {
        "flows": [{"flow_id": "GetPlanInfo", "role": "operation", "slots": slots},
                  {"flow_id": "EscalationFlow", "role": "escalation"}],
        "guardrails": guardrails,
        "infrastructure": {"project_name": "daon-telecom"},
    }


@pytest.mark.parametrize("pattern, expected", [
    (r"^010-?\d{4}-?\d{4}$", {"01011111111", "010-11111111", "0101111-1111", "010-1111-1111"}),
    (r"^\d{8}$", {"11111111"}),
    (r"^(?:TN|RS)-\d{3}$", {"TN-111", "RS-111"}),
    (r"^HW-[0-9]{8}$", {"HW-11111111"}),
    (r"01[016789]-?\d{3,4}-?\d{4}", {"010-111-1111", "010-1111-1111", "0101111111", "01011111111",
                                     "010111-1111", "0101111-1111", "010-1111111", "010-11111111"}),
])
def test_regex_samples_cover_optional_parts_bounded_repeats_and_alternatives(pattern, expected):
    samples = regex_samples(pattern)
    assert set(samples) == expected
    assert all(re.fullmatch(pattern, sample) for sample in samples)


def test_regex_samples_give_up_on_what_is_not_a_value_shape():
    assert regex_samples(r"(a)\1") == []
    assert regex_samples("[") == []
    assert all(re.fullmatch(r"[^@\s]+@[^@\s]+", s) for s in regex_samples(r"[^@\s]+@[^@\s]+"))


def test_a_birth_date_mask_is_kept_as_flag_when_the_flow_collects_the_birth_date():
    plan = {"name": "개인정보 마스킹(생년월일)", "trigger": "input",
            "policy": "고객이 말한 생년월일(8자리)를 트랜스크립트에서 마스킹한다.",
            "detection_method": "regex", "action": "mask", "examples": ["19900512"]}

    docs, problems = build_guardrails(_spec([plan], DAON_SLOTS))

    assert problems == []
    rule = docs[0]["rules"][0]
    assert rule["detection"] == {"method": "regex", "pattern": r"\d{8}"}
    assert rule["enforcement"] == {"action": "flag"}
    # an eight-digit run is also inside a phone number said without dashes
    assert rule["description"] == "AICC: flag, not mask: it would hide phoneNumber, birthDate from the flow"
    assert validate_acxd_asset("guardrail", docs[0]) == []


def test_a_phone_mask_is_kept_as_flag_when_the_flow_collects_the_phone():
    plan = {"name": "전화번호 마스킹", "trigger": "input", "policy": "고객이 말한 전화번호를 전사에서 마스킹한다.",
            "detection_method": "regex", "action": "mask", "examples": ["010-1234-5678", "01012345678"]}

    docs, _ = build_guardrails(_spec([plan], SELC_SLOTS))

    rule = docs[0]["rules"][0]
    assert rule["detection"]["pattern"] == r"\d{3}-\d{4}-\d{4}"
    assert rule["enforcement"] == {"action": "flag"}
    assert "phoneNumber" in rule["description"] and len(rule["description"]) <= 100


def test_a_mask_over_values_no_flow_collects_keeps_its_action():
    card = {"name": "Card numbers", "trigger": "input", "policy": "mask card numbers",
            "detection_method": "regex", "action": "mask", "examples": [r"\d{4}-\d{4}-\d{4}-\d{4}"]}
    resident = {"name": "Resident numbers", "trigger": "input", "policy": "mask resident numbers",
                "detection_method": "regex", "action": "mask", "examples": ["900512-1234567"]}

    docs, _ = build_guardrails(_spec([card, resident], DAON_SLOTS + SELC_SLOTS))
    assert [d["rules"][0]["enforcement"]["action"] for d in docs] == ["mask", "mask"]

    docs, _ = build_guardrails(_spec([card], []))
    assert docs[0]["rules"][0]["enforcement"]["action"] == "mask"


def test_a_mask_on_the_bots_own_replies_is_the_plans_decision():
    plan = {"name": "Mask replies", "trigger": "output", "policy": "mask phone numbers in replies",
            "detection_method": "regex", "action": "mask", "examples": [r"\d{3}-\d{4}-\d{4}"],
            "mask_bot_output": True}

    docs, _ = build_guardrails(_spec([plan], SELC_SLOTS))

    assert docs[0]["trigger"] == "output"
    assert docs[0]["rules"][0]["enforcement"]["action"] == "mask"


def test_a_built_in_slot_without_a_regex_counts_by_its_type():
    assert mask_hidden_slots(r"\d{8}", [{"name": "birthDate", "type": "NLX.Date"}]) == ["birthDate"]
    assert mask_hidden_slots(r"\d{3}-\d{4}-\d{4}", [{"name": "birthDate", "type": "NLX.Date"}]) == []
    assert mask_hidden_slots(r"\d{3}-\d{4}-\d{4}",
                             [{"name": "contactPhone", "type": "NLX.PhoneNumber"}]) == ["contactPhone"]
    # free text and menus have no value shape to test
    assert mask_hidden_slots(r"\d{8}", [{"name": "description", "type": "NLX.Text"},
                                        {"name": "claimType", "type": "claimType"}]) == []


def test_the_masks_own_examples_count_when_neither_side_can_be_sampled_into_the_other():
    # A look-ahead is not sampled, and the slot's sample 01111111111 does not
    # start with 010: only the mask's own example shows the overlap.
    slot = {"name": "phone", "type": "NLX.PhoneNumber", "regex": "^01[0-9]{9}$"}
    assert mask_hidden_slots(r"(?=010)\d{11}", [slot]) == []
    assert mask_hidden_slots(r"(?=010)\d{11}", [slot], examples=["01012345678"]) == ["phone"]
    # the mask's generated samples count the same way
    assert mask_hidden_slots(r"010\d{8}", [slot]) == ["phone"]


def _mask_doc(name, pattern):
    return {"name": name, "trigger": "input", "active": True, "rules": [{
        "name": name, "detection": {"method": "regex", "pattern": pattern},
        "enforcement": {"action": "mask", "behavior": {"maskText": "[REDACTED]"}}, "active": True}],
        "fallbackBehavior": {"type": "continue"}}


def test_packaging_repairs_a_bundle_generated_before_the_rule():
    route = {"name": "hanul-guardrail1", "trigger": "input", "active": True, "rules": [{
        "name": "hanul-guardrail1", "detection": {"method": "keyword", "keywords": ["응급", "숨이"]},
        "enforcement": {"action": "route", "behavior": {"flowId": "NoticeHandoffFlow"}}, "active": True}],
        "fallbackBehavior": {"type": "continue"}}
    bundle = {
        "flows": [{"flowId": "GetPlanInfo", "slotTypes": copy.deepcopy(DAON_SLOTS)},
                  {"flowId": "FollowUpFlow", "slotTypes": [{"name": "moreHelp", "type": "yesNo"}]}],
        "guardrails": [_mask_doc("daon-guardrail1", r"\d{8}"), copy.deepcopy(route),
                       _mask_doc("daon-cards", r"\d{4}-\d{4}-\d{4}-\d{4}")],
    }

    notes = normalize_bundle_guardrails(bundle)

    mask, kept_route, cards = bundle["guardrails"]
    assert mask["rules"][0]["enforcement"] == {"action": "flag"}
    assert "birthDate" in mask["rules"][0]["description"]
    assert kept_route == route
    assert cards["rules"][0]["enforcement"]["action"] == "mask"
    assert len(notes) == 1 and "daon-guardrail1" in notes[0]
    assert normalize_bundle_guardrails(bundle) == []
    for doc in bundle["guardrails"]:
        assert validate_acxd_asset("guardrail", doc) == []


def test_the_interview_is_told_not_to_mask_a_collected_value():
    from prompts.interview_agent_prompt import get_interview_agent_prompt

    prompt = get_interview_agent_prompt("acxd_only")
    text = " ".join(" ".join(block.get("text", "") for block in prompt if isinstance(block, dict)).split())
    assert "NEVER plan a mask over a value any flow collects" in text
    assert "do not add a PII mask on your own" in text
    assert "a PII `mask` runs on `input`" not in text
