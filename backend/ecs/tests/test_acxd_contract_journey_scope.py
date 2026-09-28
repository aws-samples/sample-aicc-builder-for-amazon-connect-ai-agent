"""A carrying journey answers general questions and hands other tasks on (J5/J8).

Live (sandbox, 2026-09-27): inside the Hanbit lookup journey, "그리고 주차는
어떻게 하나요?" got "주차 안내는 제가 도와드릴 수 있는 업무가 아닙니다 …
연결해드릴까요?" although the FAQ answers parking. The journey had no
knowledge tool, and its [tool use] line said "handle follow-up questions here:
with a tool, otherwise explain the rules and offer an agent", which beat the
anotherRequest exit.
"""

from __future__ import annotations

import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_runtime_contract import _RuntimeContract  # noqa: E402
from test_acxd_runtime_contract import _apply_journey, _journey_flow  # noqa: E402

_STEPS = [{"captures": ["reason"], "journey_tools": [], "description": "사유 확인"}]
_OLD_KO = _RuntimeContract._OLD_CONTINUE_RULE["ko"]


def _carrying_flow(prompt: str | None = None) -> dict:
    cfg = {"tools": [{"type": "dataRequest", "dataRequest": {"dataRequestId": "requestReturn"}}]}
    if prompt is not None:
        cfg["prompt"] = prompt
    return _journey_flow(journey_edges=[], journey_cfg=cfg)


def _cfg(flow: dict) -> dict:
    return flow["nodes"]["gj"]["metadata"]["generativeJourney"]


def test_a_carrying_journey_gets_the_knowledge_base_and_scoped_rules():
    out, notes = _apply_journey(_carrying_flow(), journey_steps=_STEPS)
    cfg = _cfg(out)
    # the plan named no knowledge tool: a carrying operation journey gets it anyway
    assert {"type": "knowledgeBase", "knowledgeBaseId": "{KB:greencart-faq-kb}", "scopeTags": []} in cfg["tools"]
    assert any("knowledgeBase tool" in n and "(J5)" in n for n in notes)
    prompt = cfg["prompt"]
    assert _RuntimeContract._CONTINUE_RULE["ko"] in prompt
    assert _RuntimeContract._KB_TOOL_RULE["ko"] in prompt
    assert _RuntimeContract._ANOTHER_REQUEST_RULE["ko"] in prompt
    assert _OLD_KO not in prompt and "상담원 연결을 제안합니다. 고객이 분명히" not in prompt
    # idempotent
    again, notes_again = _apply_journey(copy.deepcopy(out), journey_steps=_STEPS)
    assert again == out
    assert not any("scoped to this task" in n or "knowledgeBase tool" in n for n in notes_again)


def test_without_a_knowledge_base_only_the_hand_on_rule_is_added():
    out, _ = _apply_journey(_carrying_flow(), journey_steps=_STEPS, kb_name=None)
    cfg = _cfg(out)
    assert not any(t.get("type") == "knowledgeBase" for t in cfg["tools"])
    assert _RuntimeContract._KB_TOOL_RULE["ko"] not in cfg["prompt"]
    assert _RuntimeContract._ANOTHER_REQUEST_RULE["ko"] in cfg["prompt"]


def test_an_old_tool_use_block_is_upgraded_in_place():
    """A bundle packaged before the fix already carries [tool use]; the packaging
    pass must rewrite the line instead of leaving it (the block is appended only
    when absent)."""
    old_prompt = ("고객의 반품 사유를 자연스럽게 확인합니다.\n\n[tool use]\n"
                  "- 조회(읽기)는 필요한 값이 모이면 바로 requestReturn 도구를 호출합니다.\n"
                  f"{_OLD_KO}\n"
                  "- 안내 문구의 [필드] 자리에는 도구 결과에서 같은 이름의 값을 넣어 말합니다.")
    out, notes = _apply_journey(_carrying_flow(old_prompt), journey_steps=_STEPS)
    prompt = _cfg(out)["prompt"]
    assert _OLD_KO not in prompt
    lines = prompt.split("\n")
    at = lines.index(_RuntimeContract._CONTINUE_RULE["ko"])
    assert lines[at + 1] == _RuntimeContract._KB_TOOL_RULE["ko"]
    assert lines[at + 2] == _RuntimeContract._ANOTHER_REQUEST_RULE["ko"]
    # the lines around it are kept
    assert lines[at + 3].startswith("- 안내 문구의 [필드]")
    assert prompt.count("[tool use]") == 1
    assert any("scoped to this task" in n for n in notes)
    again, _ = _apply_journey(copy.deepcopy(out), journey_steps=_STEPS)
    assert _cfg(again)["prompt"] == prompt


def test_the_knowledge_base_name_is_the_deployed_one():
    """The runner resolves {KB:<name>} against the deployed (sanitised) name; a
    Korean name would stop the deploy at upsert-flows."""
    out, _ = _apply_journey(_carrying_flow(), journey_steps=_STEPS, kb_name="한빛병원 FAQ")
    kb = next(t for t in _cfg(out)["tools"] if t.get("type") == "knowledgeBase")
    assert kb["knowledgeBaseId"] == "{KB:FAQ}"


def test_english_journeys_get_the_same_rules():
    flow = _carrying_flow()
    flow["mainLanguageCode"] = "en-US"
    flow["languageCodes"] = ["en-US"]
    _cfg(flow)["prompt"] = "Collect the return reason."
    out, _ = _apply_journey(flow, journey_steps=_STEPS)
    prompt = _cfg(out)["prompt"]
    assert _RuntimeContract._CONTINUE_RULE["en"] in prompt
    assert _RuntimeContract._KB_TOOL_RULE["en"] in prompt
    assert _RuntimeContract._ANOTHER_REQUEST_RULE["en"] in prompt


_FLOW_IDS = ["RequestReturn", "BookAppointment", "RequestAgentFlow", "Fallback", "Escalation", "FollowUpFlow"]


def test_a_request_for_another_operation_goes_straight_to_its_flow():
    """Live (sandbox, 2026-09-27): "새로 진료 예약도 하고 싶어요" inside the lookup
    journey took anotherRequest and FollowUpFlow asked "더 도와드릴 일이 있을까요?" —
    the request was lost. Each other operation gets its own exit."""
    siblings = {"BookAppointment": "진료 예약", "RequestReturn": "반품 신청"}
    out, notes = _apply_journey(_carrying_flow(), journey_steps=_STEPS, flow_ids=_FLOW_IDS,
                                sibling_flows=siblings)
    cfg = _cfg(out)
    names = [c["name"] for c in cfg["exitConditions"]]
    assert "switchToBookAppointment" in names
    assert "switchToRequestReturn" not in names           # never an exit to itself
    index = names.index("switchToBookAppointment")
    assert names.index("anotherRequest") < index            # appended: existing indices keep their edges
    assert cfg["exitConditions"][index]["prompt"] == "고객이 이 대화의 업무가 아니라 다음 업무를 원한다: 진료 예약"
    edge = next(e for e in out["nodes"]["gj"]["childNodes"] if e["name"] == "switchToBookAppointment")
    assert edge["conditions"] == [{"left": {"type": "system", "name": "System.gjConditionIndex"},
                                   "operator": "eq", "right": {"type": "constant", "value": index}}]
    target = out["nodes"][edge["nodeId"]]
    assert target["type"] == "redirect"
    assert target["metadata"]["redirect"] == {"type": "flow", "flowId": "BookAppointment"}
    assert {"type": "slot", "name": "reason", "modification": "clear"} in target["metadata"]["stateModifications"]
    assert any("'switchToBookAppointment' → BookAppointment (J8)" in n for n in notes)
    # the flow it hands to must start its task at once (live: "담당 흐름으로 연결해 드릴게요")
    assert _RuntimeContract._HANDOVER_START_RULE["ko"] in cfg["prompt"]
    assert "흐름" not in cfg["prompt"]
    again, notes_again = _apply_journey(copy.deepcopy(out), journey_steps=_STEPS, flow_ids=_FLOW_IDS,
                                        sibling_flows=siblings)
    assert again == out and not any("switchTo" in n for n in notes_again)


def test_a_superseded_hand_on_line_is_replaced_not_duplicated():
    superseded = _RuntimeContract._SUPERSEDED_SCOPE_LINES["ko"][0]
    prompt = ("고객의 반품 사유를 자연스럽게 확인합니다.\n\n[tool use]\n"
              f"{_RuntimeContract._CONTINUE_RULE['ko']}\n{_RuntimeContract._KB_TOOL_RULE['ko']}\n{superseded}\n"
              "- 안내 문구의 [필드] 자리에는 도구 결과에서 같은 이름의 값을 넣어 말합니다.")
    out, _ = _apply_journey(_carrying_flow(prompt), journey_steps=_STEPS, flow_ids=_FLOW_IDS,
                            sibling_flows={"BookAppointment": "진료 예약"})
    lines = _cfg(out)["prompt"].split("\n")
    assert superseded not in lines
    at = lines.index(_RuntimeContract._CONTINUE_RULE["ko"])
    assert lines[at + 1:at + 4] == [_RuntimeContract._KB_TOOL_RULE["ko"],
                                    _RuntimeContract._ANOTHER_REQUEST_RULE["ko"],
                                    _RuntimeContract._HANDOVER_START_RULE["ko"]]
    assert lines[at + 4].startswith("- 안내 문구의 [필드]")


def test_the_packaging_pass_names_the_operations_that_call_the_backend():
    from tools.validate_acxd_consistency import _sibling_operation_flows

    def flow(flow_id, nodes, ai="Use this flow when the customer wants it. More text."):
        return {"flowId": flow_id, "aiDescription": ai, "nodes": nodes}

    journey = {"type": "generative_journey", "metadata": {"generativeJourney": {
        "tools": [{"type": "dataRequest", "dataRequest": {"dataRequestId": "x"}}]}}}
    flows = [
        flow("BookAppointment", {"a": journey}),
        flow("ManageAppointment", {"a": {"type": "data_request"}}),
        flow("DepartmentInfo", {"a": {"type": "knowledge_base"}}),      # FAQ: answered by the KB tool
        flow("FollowUpFlow", {"a": {"type": "user_input"}}),
    ]
    spec = {"flows": [{"flow_id": "BookAppointment", "display_name": "진료 예약"}]}
    assert _sibling_operation_flows(flows, spec) == {
        "BookAppointment": "진료 예약",
        "ManageAppointment": "Use this flow when the customer wants it.",
    }
