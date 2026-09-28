"""K2 — a knowledge-base placeholder in text fails the turn.

Live (sandbox, 2026-09-28): a regenerated booking journey listed its knowledge
tool as "- {KB:FAQ}: …" in its prompt, and every booking ended at once in the
agent hand-off ("Slot=KB is unresolved", AgentFailure). The runner resolves
{KB:<name>} in knowledgeBaseId only.
"""

from __future__ import annotations

import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_runtime_contract import apply_runtime_contract, runtime_contract_violations  # noqa: E402
from test_acxd_runtime_contract import _apply_journey, _journey_flow, _journey_kwargs  # noqa: E402


def _journey(flow: dict) -> dict:
    return flow["nodes"]["gj"]["metadata"]["generativeJourney"]


def test_a_kb_placeholder_in_a_journey_prompt_becomes_plain_words():
    flow = _journey_flow(journey_cfg={
        "prompt": "[도구]\n- requestReturn: 반품을 접수합니다.\n- {KB:greencart-faq-kb}: 정책 질문에 답합니다.",
        "tools": [{"type": "knowledgeBase", "knowledgeBaseId": "{KB:greencart-faq-kb}", "scopeTags": [],
                   "prompt": "{KB:greencart-faq-kb}로 답하세요"}]})
    out, notes = _apply_journey(flow)
    cfg = _journey(out)
    assert "{KB:" not in cfg["prompt"] and "- knowledgeBase 도구(greencart-faq-kb): 정책 질문에 답합니다." in cfg["prompt"]
    kb_tool = next(t for t in cfg["tools"] if t.get("type") == "knowledgeBase")
    assert kb_tool["knowledgeBaseId"] == "{KB:greencart-faq-kb}"          # the id field is the runner's
    assert "{KB:" not in kb_tool["prompt"]
    assert any("(K2)" in n for n in notes)
    again, notes_again = _apply_journey(copy.deepcopy(out))
    assert again == out and not any("(K2)" in n for n in notes_again)


def test_a_kb_placeholder_in_a_message_is_rewritten_and_a_foreign_token_reported():
    flow = _journey_flow()
    flow["nodes"]["askO"]["messages"] = [{"type": "text", "body": "궁금한 점은 {KB:greencart-faq-kb}에서 찾아 드려요."}]
    out, _ = apply_runtime_contract(copy.deepcopy(flow), **_journey_kwargs())
    body = out["nodes"]["askO"]["messages"][0]["body"]
    assert body == "궁금한 점은 knowledgeBase 도구(greencart-faq-kb)에서 찾아 드려요."
    bad = _journey_flow(journey_cfg={"prompt": "주문 {order:Slot}를 확인합니다. {orderNumber:NLX.Slot}"})
    problems = runtime_contract_violations(bad, **_journey_kwargs(), scope="flow")
    assert any("K2" in p and "{order:Slot}" in p for p in problems)
    assert not any("orderNumber:NLX.Slot" in p for p in problems if "K2" in p)
