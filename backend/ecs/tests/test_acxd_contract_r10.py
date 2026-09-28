"""R10 — the escalation flow says the hand-off line; a redirect to it says only why.

Live (Hanbit e2e, 2026-09-27): a journey's failure edge redirected to
EscalationFlow with "예약 접수가 어려워 상담원에게 연결해 드리겠습니다. 잠시만 기다려
주세요." and EscalationFlow then said "상담원에게 연결해 드리겠습니다.".
"""

from __future__ import annotations

import copy
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_runtime_contract import apply_runtime_contract  # noqa: E402

S = "a0000000-0000-4000-8000-000000000001"
B = "a0000000-0000-4000-8000-000000000002"
R = "a0000000-0000-4000-8000-000000000003"
F = "a0000000-0000-4000-8000-000000000004"
E = "a0000000-0000-4000-8000-000000000005"


def _flow(message: str, target: str = "EscalationFlow", language: str = "ko-KR") -> dict:
    return {
        "flowId": "BookAppointment", "untrained": False, "description": "booking",
        "aiDescription": "Use this flow to book an appointment.", "mainLanguageCode": language,
        "languageCodes": [language], "slotTypes": [], "contextVariables": [],
        "nodes": {
            S: {"nodeId": S, "type": "start", "childNodes": [{"nodeId": B, "name": "next"}]},
            B: {"nodeId": B, "type": "basic",
                "messages": [{"type": "text", "body": "예약을 도와드릴게요." if language.startswith("ko")
                              else "Let me help you book."}],
                "childNodes": [{"nodeId": R, "name": "fail"}, {"nodeId": F, "name": "ok"}]},
            R: {"nodeId": R, "type": "redirect", "messages": [{"type": "text", "body": message}],
                "metadata": {"redirect": {"type": "flow", "flowId": target}},
                "childNodes": [{"nodeId": E, "name": "next"}]},
            F: {"nodeId": F, "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"}},
                "childNodes": [{"nodeId": E, "name": "next"}]},
            E: {"nodeId": E, "type": "end"},
        },
    }


def _apply(flow: dict) -> dict:
    out, _ = apply_runtime_contract(
        copy.deepcopy(flow), role="operation",
        flow_ids={"BookAppointment", "EscalationFlow", "FollowUpFlow", "RequestAgentFlow"})
    return out


def test_the_live_double_hand_off_keeps_only_the_reason():
    out = _apply(_flow("예약 접수가 어려워 상담원에게 연결해 드리겠습니다. 잠시만 기다려 주세요."))
    # The whole first sentence announces the transfer; nothing else is left to say.
    assert "messages" not in out["nodes"][R]


def test_a_reason_sentence_survives():
    out = _apply(_flow("예약 시스템에 일시적인 오류가 있습니다. 상담원에게 연결해 드릴게요."))
    assert out["nodes"][R]["messages"][0]["body"] == "예약 시스템에 일시적인 오류가 있습니다."


def test_only_hand_off_targets_are_trimmed_and_english_is_covered():
    out = _apply(_flow("상담원에게 연결해 드리겠습니다.", target="FollowUpFlow"))
    assert out["nodes"][R]["messages"][0]["body"] == "상담원에게 연결해 드리겠습니다."
    out = _apply(_flow("I could not book that. I'll connect you to an agent, please hold.",
                       target="RequestAgentFlow", language="en-US"))
    assert out["nodes"][R]["messages"][0]["body"] == "I could not book that."


def test_a_basic_node_that_only_leads_to_the_hand_off_is_trimmed_too():
    """Hanbit (2026-09-27): DepartmentInfo said "…담당 직원에게 연결해 드리겠습니다."
    in a basic node right before its redirect to EscalationFlow."""
    flow = _flow("")
    nodes = flow["nodes"]
    nodes[R].pop("messages")
    nodes[B]["messages"] = [{"type": "text", "body": "죄송합니다. 문의하신 내용에 대한 안내를 찾지 못했어요. "
                                                     "담당 직원에게 연결해 드리겠습니다."}]
    nodes[B]["childNodes"] = [{"nodeId": R, "name": "handoff"}]
    out = _apply(flow)
    assert out["nodes"][B]["messages"][0]["body"] == "죄송합니다. 문의하신 내용에 대한 안내를 찾지 못했어요."
    # a basic node that also leads somewhere else keeps its words
    flow["nodes"][B]["childNodes"] = [{"nodeId": R, "name": "handoff"}, {"nodeId": F, "name": "ok"}]
    out = _apply(flow)
    assert "연결해 드리겠습니다" in out["nodes"][B]["messages"][0]["body"]
