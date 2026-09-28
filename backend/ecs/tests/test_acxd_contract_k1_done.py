"""Two conversation defects seen live in the sandbox (2026-09-27, Hanbit ACXD only).

* J8 done hand-off: "아니요 없어요" to the journey's "더 필요한 것이 있으신가요?" was
  answered with FollowUpFlow's "더 도와드릴 일이 있을까요?".
* K1: a knowledge_base node retrieved the answer (confidence 95) and the caller
  heard only the follow-up question — the answer is spoken only where a message
  references `{<output>.answer:NLX.Local}`.
"""

from __future__ import annotations

import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_runtime_contract import apply_runtime_contract  # noqa: E402
from test_acxd_runtime_contract import _apply_journey, _journey_flow  # noqa: E402


def test_the_done_exit_tells_follow_up_the_customer_is_done():
    # No continuation edge written by the model: done and anotherRequest both reach
    # FollowUpFlow, as in the Hanbit bundle; only done may carry the flag.
    flow = _journey_flow(journey_edges=[],
                         journey_cfg={"tools": [{"type": "dataRequest", "dataRequest": {"dataRequestId": "requestReturn"}}]})
    out, notes = _apply_journey(flow)
    gj = out["nodes"]["gj"]
    by_name = {e["name"]: e for e in gj["childNodes"]}
    done_target = out["nodes"][by_name["done"]["nodeId"]]
    another_target = out["nodes"][by_name["anotherRequest"]["nodeId"]]
    flag = {"type": "context", "name": "journeyDone", "modification": "set", "value": {"type": "constant", "value": 1}}
    assert done_target["type"] == "redirect" and done_target["metadata"]["redirect"]["flowId"] == "FollowUpFlow"
    assert flag in done_target["metadata"]["stateModifications"]
    assert by_name["done"]["nodeId"] != by_name["anotherRequest"]["nodeId"]
    assert flag not in (another_target.get("metadata") or {}).get("stateModifications", [])
    assert {"name": "journeyDone", "type": "number"} in out["contextVariables"]
    assert any("journeyDone = 1" in n for n in notes)
    again, notes_again = _apply_journey(copy.deepcopy(out))
    assert again == out and not any("journeyDone" in n for n in notes_again)


S = "b0000000-0000-4000-8000-000000000001"
K = "b0000000-0000-4000-8000-000000000002"
F = "b0000000-0000-4000-8000-000000000003"
E = "b0000000-0000-4000-8000-000000000004"
X = "b0000000-0000-4000-8000-000000000005"


def _kb_flow(name: str = "FAQ", extra_message: str | None = None) -> dict:
    nodes = {
        S: {"nodeId": S, "type": "start", "childNodes": [{"nodeId": K, "name": "next"}]},
        K: {"nodeId": K, "type": "knowledge_base",
            "metadata": {"knowledgeBase": {"knowledgeBaseId": "{KB:FAQ}", "name": name}},
            "childNodes": [
                {"nodeId": F, "name": "answered", "conditions": [
                    {"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                {"nodeId": X, "name": "failed", "conditions": [
                    {"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        F: {"nodeId": F, "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"}},
            "childNodes": [{"nodeId": E, "name": "next"}]},
        X: {"nodeId": X, "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "EscalationFlow"}},
            "childNodes": [{"nodeId": E, "name": "next"}]},
        E: {"nodeId": E, "type": "end"},
    }
    if extra_message:
        nodes[F]["messages"] = [{"type": "text", "body": extra_message}]
    return {"flowId": "DepartmentInfo", "untrained": False, "description": "faq", "aiDescription": "Department questions.",
            "mainLanguageCode": "ko-KR", "languageCodes": ["ko-KR"], "slotTypes": [], "contextVariables": [],
            "nodes": nodes}


def _apply(flow: dict) -> tuple[dict, list[str]]:
    return apply_runtime_contract(copy.deepcopy(flow), role="help",
                                  flow_ids={"DepartmentInfo", "FollowUpFlow", "EscalationFlow", "RequestAgentFlow"})


def test_the_knowledge_base_answer_is_said_on_the_success_path():
    out, notes = _apply(_kb_flow())
    success = next(e for e in out["nodes"][K]["childNodes"] if e["name"] == "answered")
    say = out["nodes"][success["nodeId"]]
    assert say["type"] == "basic" and say["messages"] == [{"type": "text", "body": "{FAQ.answer:NLX.Local}"}]
    assert say["childNodes"][0]["nodeId"] == F
    assert any("(K1)" in n for n in notes)
    again, notes_again = _apply(out)
    assert again == out and not any("(K1)" in n for n in notes_again)


def test_an_unusable_output_name_is_renamed_and_a_wrong_reference_fixed():
    out, _ = _apply(_kb_flow(name="hanbit-faq"))
    assert out["nodes"][K]["metadata"]["knowledgeBase"]["name"] == "faqAnswer"
    out, _ = _apply(_kb_flow(extra_message="{FAQ.answer:NLX.Variable}"))
    assert out["nodes"][F]["messages"][0]["body"] == "{FAQ.answer:NLX.Local}"
    success = next(e for e in out["nodes"][K]["childNodes"] if e["name"] == "answered")
    assert success["nodeId"] == F     # already said downstream: no second answer node


def test_no_match_goes_where_failure_goes():
    """Deployed runtime (2026-09-28): a success edge is taken on no_match with an
    empty answer, so the caller heard only the follow-up question."""
    out, notes = _apply(_kb_flow())
    edges = out["nodes"][K]["childNodes"]
    assert edges[0]["name"] == "noMatch" and edges[0]["conditions"][0]["right"]["value"] == "no_match"
    assert edges[0]["nodeId"] == X                                       # the failure target
    assert any("no_match" in n for n in notes)
    again, notes_again = _apply(copy.deepcopy(out))
    assert again == out and not any("no_match" in n for n in notes_again)


def test_knowledge_bases_get_the_application_languages_at_packaging():
    from tools.validate_acxd_consistency import normalize_bundle_languages
    bundle = {"application": {"settings": {"languageCode": "ko-KR", "languageCodes": ["ko-KR"]}},
              "knowledge_bases": [{"name": "FAQ", "type": "articles"}]}
    notes = normalize_bundle_languages(bundle)
    assert bundle["knowledge_bases"][0]["mainLanguageCode"] == "ko-KR"
    assert bundle["knowledge_bases"][0]["languageCodes"] == ["ko-KR"]
    assert notes and normalize_bundle_languages(bundle) == []
    from tools.acxd_resource_builders import build_knowledge_base
    doc = build_knowledge_base({"application": {"primary_locale": "ja-JP", "locales": ["ja-JP"]},
                                "knowledge_base": {"name": "faq", "articles": [{"question": "q", "answer": "a"}]}})
    assert doc["mainLanguageCode"] == "ja-JP" and doc["languageCodes"] == ["ja-JP"]
