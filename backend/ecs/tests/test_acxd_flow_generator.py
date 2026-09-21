"""ACXD Flow Generator deterministic-core tests.

The LLM is faked via the injectable ``invoke`` — tests prove the
self-correction loop feeds exact violations back and stops on success.
"""

from __future__ import annotations

import copy
import re
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agents.acxd_flow_generator.agent import (  # noqa: E402
    build_generation_prompt,
    extract_flow_json,
    normalize_generated_flow,
    repair_generated_flow,
    run_flow_generation,
    stub_bundle_for_validation,
    validate_generated_flow,
)
from test_acxd_consistency import coherent_bundle  # noqa: E402


REFUND_FLOW = coherent_bundle()["flows"][0]

PLAN = {
    "flow_id": "RefundFlow",
    "purpose": "Handle refund requests",
    "role": "operation",
    "steps": [
        {"step": 1, "description": "ask order number", "node_type": "user_input",
         "determinism": "deterministic", "user_confirmed": True},
        {"step": 2, "description": "look up order", "node_type": "data_request",
         "determinism": "deterministic", "user_confirmed": True},
        {"step": 3, "description": "eligibility branch", "node_type": "choice",
         "determinism": "deterministic", "user_confirmed": True},
        {"step": 4, "description": "explain refund", "node_type": "generative_text",
         "determinism": "generative", "user_confirmed": True},
    ],
    "escalation_conditions": "amount over 500",
}

SPEC = {
    "business_profile": {"industry": "e-commerce", "company_name": "Acme",
                         "tone": "friendly"},
    "flows": [PLAN],
    "slot_types": [{"slotTypeId": "ProductCategory",
                    "values": [{"value": "Electronics"}]}],
    "data_integrations": [{"data_request_id": "getOrderStatus",
                           "purpose": "order lookup", "mode": "mock"}],
    "knowledge_base": {"name": "Product FAQ"},
}


# ---------------------------------------------------------------------------
# prompt building + JSON extraction
# ---------------------------------------------------------------------------

def test_generation_prompt_contains_plan_and_context():
    prompt = build_generation_prompt(PLAN, SPEC)
    assert "RefundFlow" in prompt
    assert "getOrderStatus" in prompt            # available DR IDs
    assert "ProductCategory" in prompt           # available slot types
    assert "{KB:Product FAQ}" in prompt          # KB placeholder
    assert "determinism" in prompt
    assert "e-commerce" in prompt


def test_correction_prompt_contains_only_feedback():
    prompt = build_generation_prompt(PLAN, SPEC, feedback=["[X] bad thing"])
    assert "bad thing" in prompt
    assert "fix only these problems" in prompt.lower()


@pytest.mark.parametrize("payload", [
    '```json\n{"flowId": "A"}\n```',
    '{"flowId": "A"}',
    'Here it is:\n```json\n{"flowId": "A"}\n```',
])
def test_extract_flow_json_variants(payload):
    assert extract_flow_json(payload) == {"flowId": "A"}


def test_extract_flow_json_rejects_garbage():
    assert extract_flow_json("no json here") is None
    assert extract_flow_json("[1,2,3]") is None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_valid_flow_passes_full_validation():
    assert validate_generated_flow(REFUND_FLOW, PLAN, SPEC) == []


def test_flow_id_must_match_plan():
    flow = copy.deepcopy(REFUND_FLOW)
    flow["flowId"] = "WrongFlow"
    problems = validate_generated_flow(flow, {**PLAN}, SPEC)
    assert any("flow_id" in p for p in problems)


def test_unknown_data_request_is_caught_via_stub_bundle():
    flow = copy.deepcopy(REFUND_FLOW)
    for node in flow["nodes"].values():
        if node["type"] == "data_request":
            node["dataRequests"] = ["notDeclared"]
    problems = validate_generated_flow(flow, PLAN, SPEC)
    assert any("DATA_REQUEST_REF_UNDEFINED" in p for p in problems)


def test_unauthorized_generative_node_is_caught():
    flow = copy.deepcopy(REFUND_FLOW)
    for node in flow["nodes"].values():
        if node["type"] == "generative_text":
            node["type"] = "generative_task"
            node.pop("metadata", None)
    problems = validate_generated_flow(flow, PLAN, SPEC)
    assert any("DETERMINISM_UNAUTHORIZED_GENERATIVE" in p for p in problems)


def test_stub_bundle_shape():
    bundle = stub_bundle_for_validation(SPEC, REFUND_FLOW)
    assert bundle["slot_types"][0]["slotTypeId"] == "ProductCategory"
    assert bundle["data_requests"][0]["dataRequestId"] == "getOrderStatus"
    assert bundle["knowledge_bases"][0]["name"] == "Product FAQ"


# ---------------------------------------------------------------------------
# self-correction loop (fake LLM)
# ---------------------------------------------------------------------------

def test_loop_succeeds_first_try():
    invoke_log = []

    def invoke(prompt):
        invoke_log.append(prompt)
        return json.dumps(REFUND_FLOW)

    flow, problems, attempts = run_flow_generation(PLAN, SPEC, invoke)
    assert problems == []
    assert flow["flowId"] == "RefundFlow"
    assert len(attempts) == 1


def test_loop_feeds_violations_back_and_recovers():
    # The defect must be one the deterministic repair layer CANNOT heal —
    # missing terminals and dangling children are now fixed in place, so the
    # loop would never run. An unknown node type is genuinely unrepairable:
    # only the model can choose the right one.
    bad = copy.deepcopy(REFUND_FLOW)
    bad_node = next(k for k, v in bad["nodes"].items()
                    if v["type"] not in ("start", "end"))
    bad["nodes"][bad_node]["type"] = "send_carrier_pigeon"

    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return json.dumps(bad if len(prompts) == 1 else REFUND_FLOW)

    flow, problems, attempts = run_flow_generation(PLAN, SPEC, invoke)
    assert problems == []
    assert len(attempts) == 2
    assert attempts[0]["problems"]                      # first failed
    # the correction prompt carried the exact violation codes
    assert "send_carrier_pigeon" in prompts[1]   # exact violation fed back


def test_loop_gives_up_after_max_attempts():
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        return "not json at all"

    flow, problems, attempts = run_flow_generation(PLAN, SPEC, invoke, max_attempts=3)
    assert flow is None
    assert len(attempts) == 3
    assert problems == ["response did not contain valid JSON"]


def test_agent_pool_registration():
    from agents.agent_pool import AGENT_CONFIGS
    assert "acxd_flow_generator" in AGENT_CONFIGS
    cfg = AGENT_CONFIGS["acxd_flow_generator"]
    assert cfg["system_prompt_var"] == "ACXD_FLOW_GENERATOR_SYSTEM_PROMPT"


# ---------------------------------------------------------------------------
# Live-session regression (2026-09-04): generation deadlocked 3/3 attempts
# because a plan referenced a custom slot type that was never saved to the
# spec, so every attempt failed SLOT_REF_UNDEFINED.
# ---------------------------------------------------------------------------

def test_custom_slot_type_from_plan_is_stubbed_not_rejected():
    plan = {
        "flow_id": "returnRequest", "purpose": "반품 접수", "role": "operation",
        "slots": [{"name": "returnReason", "type": "ReturnReason",
                   "examples": ["단순변심", "불량", "오배송"]}],
        "steps": [{"step": 1, "description": "사유 선택", "node_type": "user_choice",
                   "determinism": "deterministic", "user_confirmed": True}],
    }
    spec = {"flows": [plan], "slot_types": [], "data_integrations": [],
            "knowledge_base": {}}
    flow = {
        "flowId": "returnRequest",
        "slotTypes": [{"name": "returnReason", "type": "ReturnReason",
                       "examples": ["단순변심"]}],
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {"nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start", "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000004"}]},
            "a0000000-0000-4000-8000-000000000004": {"nodeId": "a0000000-0000-4000-8000-000000000004", "type": "user_choice",
                  "messages": [{"type": "text", "body": "사유를 선택해 주세요"}],
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000006"}]},
            "a0000000-0000-4000-8000-000000000006": {"nodeId": "a0000000-0000-4000-8000-000000000006", "type": "end"},
        },
    }
    bundle = stub_bundle_for_validation(spec, flow)
    assert any(st["slotTypeId"] == "ReturnReason" for st in bundle["slot_types"]), \
        "custom slot type from the plan must be stubbed"
    assert validate_generated_flow(flow, plan, spec) == []


def test_missing_escalate_node_is_still_rejected():
    """The other live failure cause must stay a hard error (not auto-fixed)."""
    plan = {
        "flow_id": "orderTracking", "purpose": "조회", "role": "operation",
        "steps": [
            {"step": 1, "description": "조회", "node_type": "data_request",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 2, "description": "상담원 연결", "node_type": "escalate",
             "determinism": "deterministic", "user_confirmed": True},
        ],
    }
    spec = {"flows": [plan],
            "data_integrations": [{"data_request_id": "orderLookup", "mode": "mock",
                                   "purpose": "조회"}]}
    flow = {
        "flowId": "orderTracking",
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {"nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start", "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
            "a0000000-0000-4000-8000-000000000003": {"nodeId": "a0000000-0000-4000-8000-000000000003", "type": "data_request",
                  "dataRequests": ["orderLookup"], "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000006"}]},
            "a0000000-0000-4000-8000-000000000006": {"nodeId": "a0000000-0000-4000-8000-000000000006", "type": "end"},
        },
    }
    problems = validate_generated_flow(flow, plan, spec)
    assert any("escalate" in p for p in problems)


def test_failed_generation_logs_actual_violations(caplog):
    """A 3-strike failure must log the violations (was: only a count)."""
    import logging
    caplog.set_level(logging.WARNING)
    plan = {"flow_id": "BadFlow", "purpose": "x", "role": "operation",
            "steps": [{"step": 1, "description": "x", "node_type": "basic",
                       "determinism": "deterministic", "user_confirmed": True}]}
    # The defect must survive the repair layer, which now fills a missing start
    # node, missing confirmed-step nodes and dangling edges. An unknown node type
    # is genuinely unrepairable — only the model can pick the right one.
    bad = ('{"flowId": "WrongId", "nodes": {"a0000000-0000-4000-8000-000000000001":'
           ' {"nodeId": "a0000000-0000-4000-8000-000000000001",'
           ' "type": "teleporter"}}}')
    flow, problems, attempts = run_flow_generation(
        plan, {"flows": [plan]}, lambda _p: bad, max_attempts=2)
    assert flow is None and len(attempts) == 2
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "BadFlow" in logged
    import re as _re
    assert _re.search(r"\[[A-Z_]{4,}\]", logged), logged   # a real violation code


def test_prompt_documents_the_two_failure_rules():
    from agents.acxd_flow_generator.system_prompt import (
        ACXD_FLOW_GENERATOR_SYSTEM_PROMPT as P)
    assert "an `end` node is NOT a substitute" in P
    assert "Slot types must exist" in P


# ---------------------------------------------------------------------------
# Live-session regressions round 2 (2026-09-04, ECS logs after the
# diagnosability fix): four independent defects blocked generation.
# ---------------------------------------------------------------------------

def test_node_slot_field_is_canonicalized_to_user_choice():
    """#1 The model emits `slot` on user_input nodes. FlowNode (SDK) has no such
    field — the serializer dropped it and the deployed node captured nothing.
    The canonicalizer turns it into a user_choice with metadata.choice; the
    strict schema rejects the raw form so nothing hollow can slip through."""
    from tools.acxd_flow_canonicalizer import canonicalize_flow
    from tools.validate_acxd_flow import validate_acxd_asset
    flow = copy.deepcopy(REFUND_FLOW)
    uid = [k for k, v in flow["nodes"].items() if v["type"] == "user_input"][0]
    flow["nodes"][uid]["slot"] = {"name": "orderNumber", "type": "text"}
    assert any("slot" in p for p in validate_acxd_asset("flow", flow))
    fixed = canonicalize_flow(flow).flow
    node = fixed["nodes"][uid]
    assert node["type"] == "user_choice"
    assert node["metadata"]["choice"] == {"source": "slotType", "slotTypeId": "text"}
    assert {"name": "orderNumber", "type": "text"} in fixed["slotTypes"]
    assert validate_acxd_asset("flow", fixed) == []


def test_non_ascii_kb_name_is_sanitized_and_placeholder_resolves():
    """#2 A Korean KB name broke BOTH the KB schema and every {KB:...} ref."""
    from tools.acxd_resource_builders import build_knowledge_base, sanitize_kb_name
    from tools.validate_acxd_flow import validate_acxd_asset

    assert sanitize_kb_name("QA일렉트로닉스 FAQ") == "QA FAQ"
    assert sanitize_kb_name("가온물류") == "AICC FAQ"   # nothing keepable → fallback

    spec = {"business_profile": {"company_name": "QA Electronics"},
            "data_integrations": [{"data_request_id": "getOrderStatus",
                                   "purpose": "order lookup", "mode": "mock"}],
            "knowledge_base": {"name": "QA일렉트로닉스 FAQ",
                               "articles": [{"question": "Q", "answer": "A"}]}}
    kb = build_knowledge_base(spec)
    assert validate_acxd_asset("knowledge_base", kb) == [], kb["name"]

    # A flow referencing the SANITIZED name validates against the stub bundle
    flow = copy.deepcopy(REFUND_FLOW)
    gid = [k for k, v in flow["nodes"].items() if v["type"] == "generative_text"][0]
    flow["nodes"][gid] = {"nodeId": gid, "type": "knowledge_base",
                          "metadata": {"knowledgeBase": {"knowledgeBaseId": f"{{KB:{kb['name']}}}", "name": kb['name']}},
                          "childNodes": flow["nodes"][gid]["childNodes"]}
    plan = copy.deepcopy(PLAN)
    plan["steps"][3]["node_type"] = "knowledge_base"
    full_spec = {**spec, "flows": [plan]}
    assert validate_generated_flow(flow, plan, full_spec) == []

    # A flow echoing the RAW (Korean) name is normalized, not rejected.
    raw_flow = copy.deepcopy(flow)
    raw_flow["nodes"][gid]["metadata"]["knowledgeBase"]["knowledgeBaseId"] = "{KB:QA일렉트로닉스 FAQ}"
    fixed = normalize_generated_flow(raw_flow, full_spec)
    assert fixed["nodes"][gid]["metadata"]["knowledgeBase"]["knowledgeBaseId"] == "{KB:QA FAQ}"
    assert validate_generated_flow(fixed, plan, full_spec) == []


def test_prompt_pins_flowid_dr_ids_and_kb_placeholder():
    """#3 The model invented a flowId / DR id / KB name — prompt must pin them."""
    prompt = build_generation_prompt(PLAN, SPEC)
    assert "flowId MUST be exactly: 'RefundFlow'" in prompt
    assert "use these EXACT ids" in prompt
    assert "copy VERBATIM" in prompt
    assert "ONLY these top-level keys" in prompt

    no_kb = {**SPEC, "knowledge_base": {}}
    assert "do NOT emit knowledge_base nodes" in build_generation_prompt(PLAN, no_kb)


def test_extract_flow_json_unwraps_and_strips_stray_assets():
    """#4 Responses bolted sibling assets onto the flow document."""
    doc = {"flowId": "RefundFlow", "nodes": {},
           "knowledge_bases": [{"name": "x"}], "application": {"name": "y"}}
    out = extract_flow_json(json.dumps(doc))
    assert "knowledge_bases" not in out and "application" not in out
    assert out["flowId"] == "RefundFlow"

    wrapped = {"flow": {"flowId": "RefundFlow", "nodes": {}}}
    assert extract_flow_json(json.dumps(wrapped))["flowId"] == "RefundFlow"


# ---------------------------------------------------------------------------
# Deterministic repair (live QA: generate_acxd_flows burned 20 calls on
# mechanical mistakes while the flow shape itself was fine).
# ---------------------------------------------------------------------------

def test_repair_fixes_all_mechanical_mistakes_at_once():
    from agents.acxd_flow_generator.agent import repair_generated_flow

    # Plan confirms only deterministic steps (no generative) + escalate.
    plan = {
        "flow_id": "RefundFlow", "purpose": "환불", "role": "operation",
        "steps": [
            {"step": 1, "description": "주문번호 요청", "node_type": "user_input",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 2, "description": "조회", "node_type": "data_request",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 3, "description": "분기", "node_type": "choice",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 4, "description": "상담원 연결", "node_type": "escalate",
             "determinism": "deterministic", "user_confirmed": True},
        ],
    }
    # An LLM output carrying EVERY recurring defect:
    bad = {
        "flowId": "wrongFlowId",                                      # 1
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {"nodeId": "MISMATCH", "type": "start", "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},   # 2
            "a0000000-0000-4000-8000-000000000002": {"nodeId": "a0000000-0000-4000-8000-000000000002", "type": "user_input",
                  "messages": [{"type": "text", "body": "주문번호?"}],
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
            "a0000000-0000-4000-8000-000000000003": {"nodeId": "a0000000-0000-4000-8000-000000000003", "type": "data_request",
                  "dataRequests": ["getOrderStatusXYZ"],                                     # 3
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000004"}]},
            "a0000000-0000-4000-8000-000000000004": {"nodeId": "a0000000-0000-4000-8000-000000000004", "type": "choice",
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000005", "name": "ok"},
                                 {"nodeId": "ghost-not-a-uuid", "name": "dangling"}]},                  # 4
            "a0000000-0000-4000-8000-000000000005": {"nodeId": "a0000000-0000-4000-8000-000000000005", "type": "generative_task",                                  # 5
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000006"}]},
            "a0000000-0000-4000-8000-000000000006": {"nodeId": "a0000000-0000-4000-8000-000000000006", "type": "end"},
        },
        # 6 no escalate node despite a confirmed escalate step
    }
    fixed = repair_generated_flow(bad, plan, SPEC)

    assert fixed["flowId"] == "RefundFlow"
    assert fixed["nodes"]["a0000000-0000-4000-8000-000000000001"]["nodeId"] == "a0000000-0000-4000-8000-000000000001"
    assert fixed["nodes"]["a0000000-0000-4000-8000-000000000003"]["dataRequests"] == ["getOrderStatus"]
    assert all(c["nodeId"] in fixed["nodes"] for c in fixed["nodes"]["a0000000-0000-4000-8000-000000000004"]["childNodes"])
    assert fixed["nodes"]["a0000000-0000-4000-8000-000000000005"]["type"] == "basic"
    assert any(n["type"] == "escalate" for n in fixed["nodes"].values())
    assert validate_generated_flow(fixed, plan, {**SPEC, "flows": [plan]}) == []


def test_repair_points_the_failure_branch_at_the_planned_operation_hand_off():
    """Live (SELC, 2026-09-21): the confirmed plan said 'order not found → hand
    off to SearchOrderByCustomerInfo'; the model sent that branch to Fallback
    twice in a row and the determinism gate refused the whole application."""
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "DeliveryStatusByOrder", "role": "operation", "steps": [
        {"step": 1, "node_type": "user_choice", "slot": "orderNumber", "user_confirmed": True},
        {"step": 2, "node_type": "data_request", "data_request_id": "getDeliveryStatus", "user_confirmed": True},
        {"step": 3, "node_type": "generative_text", "user_confirmed": True},
        {"step": 4, "node_type": "redirect", "redirect_flow_id": "SearchOrderByCustomerInfo", "user_confirmed": True},
        {"step": 5, "node_type": "redirect", "redirect_flow_id": "followup", "user_confirmed": True},
    ]}
    spec = {"flows": [plan, {"flow_id": "SearchOrderByCustomerInfo", "role": "operation", "steps": []},
                      {"flow_id": "FollowUpFlow", "role": "followup", "steps": []},
                      {"flow_id": "FallbackFlow", "role": "fallback", "steps": []}],
            "data_integrations": [{"data_request_id": "getDeliveryStatus"}]}
    n = lambda i: f"b0000000-0000-4000-8000-00000000000{i}"
    flow = {"flowId": "DeliveryStatusByOrder", "nodes": {
        n(1): {"nodeId": n(1), "type": "start", "childNodes": [{"nodeId": n(2)}]},
        n(2): {"nodeId": n(2), "type": "user_choice", "childNodes": [{"nodeId": n(3), "name": "captured"}]},
        n(3): {"nodeId": n(3), "type": "data_request", "dataRequests": ["getDeliveryStatus"],
               "childNodes": [{"nodeId": n(4), "name": "done"}]},
        n(4): {"nodeId": n(4), "type": "choice", "childNodes": [
            {"nodeId": n(5), "name": "found", "conditions": [{"left": {"type": "variable", "name": "getDeliveryStatus.found"},
                                                              "operator": "eq", "right": {"type": "constant", "value": True}}]},
            {"nodeId": n(7), "name": "notFound"}]},
        n(5): {"nodeId": n(5), "type": "generative_text", "childNodes": [{"nodeId": n(6), "name": "next"}]},
        n(6): {"nodeId": n(6), "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"}}},
        n(7): {"nodeId": n(7), "type": "basic", "messages": [{"type": "text", "body": "주문번호를 모르시거나 조회가 되지 않는 경우, 고객님의 정보로 조회를 도와드리겠습니다."}],
               "childNodes": [{"nodeId": n(8), "name": "next"}]},
        n(8): {"nodeId": n(8), "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "FallbackFlow"}}},
    }}
    fixed = repair_generated_flow(flow, plan, spec)
    targets = {nid: (node.get("metadata") or {}).get("redirect", {}).get("flowId")
               for nid, node in fixed["nodes"].items() if node.get("type") == "redirect"}
    assert targets[n(8)] == "SearchOrderByCustomerInfo"     # the failure branch now hands off as planned
    assert targets[n(6)] == "FollowUpFlow"                  # the success branch is untouched
    assert fixed["nodes"][n(7)]["messages"][0]["body"].startswith("주문번호를 모르시거나")
    # idempotent, and a plan without an operation hand-off changes nothing
    again = repair_generated_flow(fixed, plan, spec)
    assert again["nodes"][n(8)]["metadata"]["redirect"]["flowId"] == "SearchOrderByCustomerInfo"


def test_repair_degrades_data_request_when_no_integrations_exist():
    from agents.acxd_flow_generator.agent import repair_generated_flow
    spec = {"flows": [PLAN], "data_integrations": []}
    flow = {
        "flowId": "RefundFlow",
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {"nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start", "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
            "a0000000-0000-4000-8000-000000000003": {"nodeId": "a0000000-0000-4000-8000-000000000003", "type": "data_request", "dataRequests": ["nope"],
                  "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000006"}]},
            "a0000000-0000-4000-8000-000000000006": {"nodeId": "a0000000-0000-4000-8000-000000000006", "type": "end"},
        },
    }
    fixed = repair_generated_flow(flow, {"flow_id": "RefundFlow", "steps": []}, spec)
    assert fixed["nodes"]["a0000000-0000-4000-8000-000000000003"]["type"] == "basic"
    assert "dataRequests" not in fixed["nodes"]["a0000000-0000-4000-8000-000000000003"]


# ---------------------------------------------------------------------------
# LIVE-API constraints, discovered 2026-09-05 by deploying a generated bundle
# into a real ACXD workspace (us-west-2). The public docs are laxer than the
# API, and every one of these produced a hard ValidationException:
#   flowId 'probe123abc'  -> "flowId is not in the expected format"       (digits)
#   nodes  {n1: ...}      -> "nodes is not in the expected format"        (non-UUID ids)
#   description '배송...'  -> "description is not in the expected format"  (non-ASCII)
#   aiDescription '주문...' -> "aiDescription is not in the expected format"
# Korean is fine in customer-facing messages[].body (verified OK).
# ---------------------------------------------------------------------------

def test_repair_enforces_live_api_constraints():
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {"flow_id": "orderDeliveryStatus", "purpose": "배송 조회",
            "role": "operation", "steps": []}
    llm_output = {
        "flowId": "order123Status",                    # digits → rejected live
        "description": "고객의 주문 배송 상태를 조회",       # non-ASCII → rejected live
        "aiDescription": "주문 배송 상태 안내 플로우",       # non-ASCII → rejected live
        "nodes": {                                      # non-UUID ids → rejected live
            "a2000000-0000-4000-8000-000000000001": {"nodeId": "a2000000-0000-4000-8000-000000000001", "type": "start", "childNodes": [{"nodeId": "a2000000-0000-4000-8000-000000000002"}]},
            "a2000000-0000-4000-8000-000000000002": {"nodeId": "a2000000-0000-4000-8000-000000000002", "type": "basic",
                   "messages": [{"type": "text", "body": "주문번호를 말씀해 주세요."}],
                   "childNodes": [{"nodeId": "a2000000-0000-4000-8000-000000000003"}]},
            "a2000000-0000-4000-8000-000000000003": {"nodeId": "a2000000-0000-4000-8000-000000000003", "type": "end"},
        },
    }
    fixed = repair_generated_flow(llm_output, plan, {"flows": [plan]})

    assert fixed["flowId"] == "orderDeliveryStatus"          # letters only
    # Pure Korean cannot survive transliteration, but the field must NOT be
    # dropped: ACXD has no training utterances, so the AI Description is the
    # flow's only routing descriptor. A deterministic ASCII label is emitted.
    assert fixed["description"] == "Handles the order delivery status conversation"
    assert fixed["aiDescription"] == "Handles the order delivery status conversation"
    assert fixed["description"].isascii() and fixed["aiDescription"].isascii()
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    assert all(uuid_re.match(k) for k in fixed["nodes"]), list(fixed["nodes"])
    # references were remapped consistently
    ids = set(fixed["nodes"])
    for node in fixed["nodes"].values():
        assert node["nodeId"] in ids
        for child in node.get("childNodes") or []:
            assert child["nodeId"] in ids
    # customer-facing Korean survives (only metadata is ASCII-only)
    bodies = [m["body"] for n in fixed["nodes"].values() for m in (n.get("messages") or [])]
    assert "주문번호를 말씀해 주세요." in bodies
    assert validate_acxd_asset("flow", fixed) == []


def test_repair_transliterates_mixed_description():
    from agents.acxd_flow_generator.agent import repair_generated_flow
    plan = {"flow_id": "welcome", "steps": []}
    out = repair_generated_flow(
        {"flowId": "welcome", "description": "QA Mart 배송 조회 v1",
         "nodes": {"a0000000-0000-4000-8000-000000000001":
                   {"nodeId": "a0000000-0000-4000-8000-000000000001", "type": "end"}}},
        plan, {"flows": [plan]})
    assert out["description"] == "QA Mart v1"     # ASCII kept, Korean stripped


# ---------------------------------------------------------------------------
# Disconnected edges. The console warns: "This node contains a disconnected
# edge. Conversations that reach this edge will route to the application's
# Fallback flow." The API accepts these documents, so validation never caught
# it — only a live deploy surfaced it. Verified live: supplying the canonical
# typed-edge shapes WITH targets clears every warning.
# ---------------------------------------------------------------------------

def test_typed_edges_are_materialized_with_targets():
    """user_input / data_request expose failure edges whether we ask or not."""
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "lookup", "steps": []}
    flow = repair_generated_flow({
        "flowId": "lookup",
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {
                "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
            # happy path only — the not_exists edge is missing entirely
            "a0000000-0000-4000-8000-000000000002": {
                "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "user_input",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
            "a0000000-0000-4000-8000-000000000003": {
                "nodeId": "a0000000-0000-4000-8000-000000000003", "type": "data_request",
                "dataRequestId": "lookupX",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000004"}]},
            "a0000000-0000-4000-8000-000000000004": {
                "nodeId": "a0000000-0000-4000-8000-000000000004", "type": "end"},
        },
    }, plan, {"flows": [plan],
              "data_integrations": [{"data_request_id": "lookupX", "mode": "mock",
                                     "purpose": "x"}]})

    ui = fixedq(flow, "user_input")
    dr = fixedq(flow, "data_request")
    # both edges present, both targeted
    assert len(ui["childNodes"]) == 2, ui["childNodes"]
    assert len(dr["childNodes"]) == 2, dr["childNodes"]
    assert all(c.get("nodeId") for c in ui["childNodes"] + dr["childNodes"])
    # canonical shapes the service actually recognises
    assert ui["childNodes"][0]["conditions"][0]["left"]["type"] == "captured_flow"
    assert ui["childNodes"][1]["conditions"][0]["operator"] == "not_exists"
    assert dr["childNodes"][1]["conditions"][0]["right"]["value"] == "error"
    # the happy path still points where the model put it
    assert ui["childNodes"][0]["nodeId"] == "a0000000-0000-4000-8000-000000000003"


def fixedq(flow: dict, node_type: str) -> dict:
    return next(n for n in flow["nodes"].values() if n.get("type") == node_type)


def test_no_node_is_left_without_an_outgoing_edge():
    """The escalate node the repair layer adds had no edge of its own."""
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "esc", "steps": [
        {"step": 1, "node_type": "escalate", "determinism": "deterministic",
         "user_confirmed": True, "description": "hand off"}]}
    flow = repair_generated_flow({
        "flowId": "esc",
        "nodes": {"a0000000-0000-4000-8000-000000000001": {
            "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
            "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
            "a0000000-0000-4000-8000-000000000002": {
                "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "basic",
                "messages": [{"type": "text", "body": "잠시만 기다려 주세요."}]}},
    }, plan, {"flows": [plan]})

    for node in flow["nodes"].values():
        if node["type"] == "end":
            continue
        assert node.get("childNodes"), f"{node['type']} has no outgoing edge"
        assert all(c.get("nodeId") for c in node["childNodes"])
    # a terminal exists to absorb the ends
    assert any(n["type"] == "end" for n in flow["nodes"].values())


def test_generation_never_shares_a_pooled_agent_instance():
    """Concurrent sessions must not share an Agent.

    `get_agent()` returns a process-wide singleton and one ECS task serves many
    sessions. Two sessions generating at once shared the instance, clobbered
    each other's `messages`, and strands raised ConcurrencyException — the
    session then could not recover by any user reply. Reproduced by running
    five live conversations simultaneously, so the call sites are asserted here
    rather than trusted.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    for rel in ("agents/acxd_flow_generator/agent.py", "tools/acxd_resource_builders.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert 'get_agent("acxd_flow_generator")' not in src, (
            f"{rel} uses the shared pool singleton; use get_agent_with_tools() "
            "so each invocation gets its own instance")
        if "acxd_flow_generator" in src and "agent_pool" in src:
            assert "get_agent_with_tools" in src, rel


def test_repair_fills_a_missing_dataRequests_field():
    """`dataRequests` is required on a data_request node and the model omits it.

    A live run stalled on exactly this: "data_request 노드(step 4)에
    dataRequests 필드가 누락됨". The repair pass previously only touched nodes
    that already had the field, so a missing one went straight to validation and
    bounced the whole flow back to the LLM.
    """
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {"flow_id": "lookup", "steps": [
        {"step": 1, "node_type": "data_request", "determinism": "deterministic",
         "user_confirmed": True, "description": "look it up",
         "data_request_id": "orderLookup"},
    ]}
    spec = {"flows": [plan], "data_integrations": [
        {"data_request_id": "orderLookup", "mode": "mock", "purpose": "x"}]}
    flow = repair_generated_flow({
        "flowId": "lookup",
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {
                "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
            "a0000000-0000-4000-8000-000000000002": {   # no dataRequests at all
                "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "data_request",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
            "a0000000-0000-4000-8000-000000000003": {
                "nodeId": "a0000000-0000-4000-8000-000000000003", "type": "end"},
        },
    }, plan, spec)

    dr = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    assert dr["dataRequests"] == ["orderLookup"], dr
    assert validate_acxd_asset("flow", flow) == []


def test_repair_degrades_a_data_request_node_when_nothing_is_declared():
    """With no integrations at all, keep the graph valid instead of failing."""
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "lookup", "steps": []}
    flow = repair_generated_flow({
        "flowId": "lookup",
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {
                "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "data_request",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
            "a0000000-0000-4000-8000-000000000002": {
                "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "end"},
        },
    }, plan, {"flows": [plan], "data_integrations": []})
    node = flow["nodes"]["a0000000-0000-4000-8000-000000000001"]
    assert node["type"] == "basic"
    assert node.get("messages")


def test_repair_accepts_the_singular_dataRequest_key():
    """The model serialises `dataRequest` about as often as `dataRequests`.

    Live run: "노드 속성을 dataRequest(단수)로 직렬화 → 플로우 스키마가 요구하는
    dataRequests(복수 필수 배열) 위반", after three self-correction attempts.
    """
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {"flow_id": "lookup", "steps": []}
    spec = {"flows": [plan], "data_integrations": [
        {"data_request_id": "orderLookup", "mode": "mock", "purpose": "x"}]}
    for key, value in (("dataRequest", "orderLookup"),
                       ("dataRequestId", "orderLookup"),
                       ("dataRequest", ["orderLookup"])):
        flow = repair_generated_flow({
            "flowId": "lookup",
            "nodes": {
                "a0000000-0000-4000-8000-000000000001": {
                    "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
                    "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
                "a0000000-0000-4000-8000-000000000002": {
                    "nodeId": "a0000000-0000-4000-8000-000000000002",
                    "type": "data_request", key: value,
                    "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000003"}]},
                "a0000000-0000-4000-8000-000000000003": {
                    "nodeId": "a0000000-0000-4000-8000-000000000003", "type": "end"},
            },
        }, plan, spec)
        dr = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
        assert dr["dataRequests"] == ["orderLookup"], (key, dr)
        assert key not in dr or key == "dataRequests"
        assert validate_acxd_asset("flow", flow) == [], (key, flow)


def test_generator_agent_is_built_without_an_empty_tool_list():
    """`tools=[]` broke the Converse call; the model uses cache_tools='default'.

    Symptom in the live runs was misleading: the generator appeared to return
    "no valid JSON" on every attempt, because the ValidationException from
    Bedrock never reached the operator. Assert the call shape directly.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    empty_tools = re.compile(r"get_agent_with_tools\([^)]*tools\s*=\s*\[\s*\]")
    for rel in ("agents/acxd_flow_generator/agent.py", "tools/acxd_resource_builders.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert not empty_tools.search(src), f"{rel} passes an empty tool list"
        assert "tools=None" in src, rel


def test_repair_normalizes_context_variables():
    """The SDK models exactly {name, type}; the model invents extras.

    Live runs lost whole flows to
      $['contextVariables'][0]: 'type' is a required property
      $['contextVariables'][0]: Additional properties are not allowed
                                ('defaultValue' was unexpected)
    and then, once told to fix it, returned no JSON at all on all four retries —
    so one mechanical slip on attempt 1 consumed every attempt.
    """
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {"flow_id": "fallback", "steps": []}
    flow = repair_generated_flow({
        "flowId": "fallback",
        "contextVariables": [
            {"name": "retryCount", "defaultValue": 0},        # type inferred: number
            {"name": "isVip", "type": "BOOLEAN", "defaultValue": True},
            {"name": "lastIntent"},                            # no hint at all: text
            {"defaultValue": "orphan"},                        # unusable: dropped
        ],
        "nodes": {"a0000000-0000-4000-8000-000000000001": {
            "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "end"}},
    }, plan, {"flows": [plan]})

    assert flow["contextVariables"] == [
        {"name": "retryCount", "type": "number"},
        {"name": "isVip", "type": "boolean"},
        {"name": "lastIntent", "type": "text"},
    ], flow["contextVariables"]
    assert validate_acxd_asset("flow", flow) == []


def test_repair_drops_an_entirely_unusable_context_variable_list():
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "f", "steps": []}
    flow = repair_generated_flow({
        "flowId": "flow",
        "contextVariables": [{"defaultValue": 1}, "nonsense"],
        "nodes": {"a0000000-0000-4000-8000-000000000001": {
            "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "end"}},
    }, plan, {"flows": [plan]})
    assert "contextVariables" not in flow


def test_unsupported_properties_are_pruned_not_retried():
    """Extra keys the model invents cost attempts and mean nothing.

    Measured over a live window, "Additional properties are not allowed" was the
    second most common generation failure (55 occurrences). Nearly every flow
    exhausted all five attempts, and at ~5s per attempt that is what made the
    tool look hung.
    """
    from tools.validate_acxd_flow import prune_to_schema, validate_acxd_asset

    a, b = ('a0000000-0000-4000-8000-000000000001',
            'a0000000-0000-4000-8000-000000000002')
    doc = {
        'flowId': 'welcome', 'bogusTop': 1,
        'nodes': {
            a: {'nodeId': a, 'type': 'basic', 'retryPolicy': {'max': 2},
                'messages': [{'type': 'text', 'body': 'hi', 'tone': 'warm'}],
                'childNodes': [{'nodeId': b, 'name': 'next', 'weight': 3}]},
            b: {'nodeId': b, 'type': 'end'},
        },
    }
    assert validate_acxd_asset('flow', doc)          # invalid as authored
    pruned = prune_to_schema(doc, 'flow')
    assert validate_acxd_asset('flow', pruned) == []  # valid after pruning

    node = pruned['nodes'][a]
    assert 'retryPolicy' not in node
    assert node['messages'][0] == {'type': 'text', 'body': 'hi'}
    assert node['childNodes'][0] == {'nodeId': b, 'name': 'next'}


def test_pruning_keeps_legitimate_nested_config():
    """generative_journey config must survive — it lives under metadata."""
    from tools.validate_acxd_flow import prune_to_schema

    a, b = ('a0000000-0000-4000-8000-000000000001',
            'a0000000-0000-4000-8000-000000000002')
    journey = {'prompt': 'help the customer', 'modelType': 'amazon-nova-micro',
               'maxSteps': 5}
    pruned = prune_to_schema({
        'flowId': 'journey',
        'nodes': {
            a: {'nodeId': a, 'type': 'generative_journey',
                'childNodes': [{'nodeId': b}],
                'metadata': {'generativeJourney': journey}},
            b: {'nodeId': b, 'type': 'end'},
        },
    }, 'flow')
    assert pruned['nodes'][a]['metadata']['generativeJourney'] == journey


def test_generation_reports_progress_for_every_attempt():
    """Silence for minutes is indistinguishable from a hang."""
    import json as _json
    from agents.acxd_flow_generator import agent as mod
    from tools.session_context import current_callback_handler

    events = []

    class Handler:
        def add_ws_event(self, event):
            events.append(event)

    plan = {"flow_id": "welcome", "purpose": "greet", "role": "welcome",
            "steps": [{"step": 1, "description": "greet", "node_type": "basic",
                       "determinism": "deterministic", "user_confirmed": True}]}
    spec = {"flows": [plan], "business_profile": {"company_name": "X"}}

    calls = {"n": 0}

    def invoke(_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return "sorry, no json here"          # forces a second attempt
        a, b, c = ('a0000000-0000-4000-8000-000000000001',
                   'a0000000-0000-4000-8000-000000000002',
                   'a0000000-0000-4000-8000-000000000003')
        return "```json\n" + _json.dumps({
            "flowId": "welcome",
            "nodes": {
                a: {"nodeId": a, "type": "start", "childNodes": [{"nodeId": b}]},
                b: {"nodeId": b, "type": "basic",
                    "messages": [{"type": "text", "body": "안녕하세요"}],
                    "childNodes": [{"nodeId": c}]},
                c: {"nodeId": c, "type": "end"},
            },
        }) + "\n```"

    token = current_callback_handler.set(Handler())
    try:
        flow, problems, attempts = mod.run_flow_generation(plan, spec, invoke)
    finally:
        current_callback_handler.reset(token)

    assert problems == [], problems
    assert len(attempts) == 2
    running = [e for e in events if e["status"] == "running"]
    done = [e for e in events if e["status"] == "completed"]
    assert len(running) == 2, events            # one per attempt
    assert len(done) == 1
    assert all(e["subagent"] == "acxd_flow_generator" for e in events)
    assert "welcome" in running[0]["message"]
    assert "2/5" in running[1]["message"]       # attempt number is visible


def test_repair_adds_nodes_for_confirmed_steps():
    """DETERMINISM_MISSING_NODE was burning the retry budget.

    The contract is checked by node TYPE presence, so a plan that confirmed a
    user_input step fails when the model folds that prompt into a neighbour. A
    live run reported missing nodes for confirmed steps 3, 5, 8 and 9 of one flow
    and spent every attempt there. Only `escalate` used to be repaired.
    """
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_consistency import validate_acxd_consistency
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {
        "flow_id": "lookup", "purpose": "x", "role": "operation",
        "steps": [
            {"step": 1, "description": "인사", "node_type": "basic",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 2, "description": "주문번호를 받습니다", "node_type": "user_input",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 3, "description": "조회", "node_type": "data_request",
             "determinism": "deterministic", "user_confirmed": True,
             "data_request_id": "orderLookup"},
            {"step": 4, "description": "사람에게 연결", "node_type": "escalate",
             "determinism": "deterministic", "user_confirmed": True},
        ],
    }
    spec = {"flows": [plan], "data_integrations": [
        {"data_request_id": "orderLookup", "mode": "mock", "purpose": "x"}]}

    a, b = ("a0000000-0000-4000-8000-000000000001",
            "a0000000-0000-4000-8000-000000000002")
    flow = repair_generated_flow({
        "flowId": "lookup",
        "nodes": {                       # only basic + end: three types missing
            a: {"nodeId": a, "type": "basic",
                "messages": [{"type": "text", "body": "안녕하세요"}],
                "childNodes": [{"nodeId": b}]},
            b: {"nodeId": b, "type": "end"},
        },
    }, plan, spec)

    types = {n["type"] for n in flow["nodes"].values()}
    for expected in ("basic", "user_input", "data_request", "escalate", "start"):
        assert expected in types, (expected, sorted(types))
    assert validate_acxd_asset("flow", flow) == []
    violations = validate_acxd_consistency({"flows": [flow]}, spec)
    missing = [v for v in violations if "DETERMINISM_MISSING_NODE" in str(v)]
    assert missing == [], missing


def test_repair_adds_a_missing_start_node_at_the_real_entry_point():
    """FLOW_NO_START failed the flow though the entry point is unambiguous."""
    from agents.acxd_flow_generator.agent import repair_generated_flow
    from tools.validate_acxd_flow import validate_acxd_asset

    plan = {"flow_id": "lookup", "steps": []}
    a, b = ("a0000000-0000-4000-8000-000000000001",
            "a0000000-0000-4000-8000-000000000002")
    flow = repair_generated_flow({
        "flowId": "lookup",
        "nodes": {
            a: {"nodeId": a, "type": "basic",
                "messages": [{"type": "text", "body": "안녕하세요"}],
                "childNodes": [{"nodeId": b}]},
            b: {"nodeId": b, "type": "end"},
        },
    }, plan, {"flows": [plan]})

    start = next(n for n in flow["nodes"].values() if n["type"] == "start")
    # It points at the node nothing else referenced, i.e. the real entry.
    assert start["childNodes"][0]["nodeId"] == a
    assert validate_acxd_asset("flow", flow) == []


def test_repair_reports_rather_than_inventing_an_unconstructible_node():
    """A knowledge_base step with no knowledge base cannot be synthesised."""
    from agents.acxd_flow_generator.agent import repair_generated_flow

    plan = {"flow_id": "faq", "steps": [
        {"step": 1, "description": "KB 답변", "node_type": "knowledge_base",
         "determinism": "generative", "user_confirmed": True}]}
    a = "a0000000-0000-4000-8000-000000000001"
    flow = repair_generated_flow({
        "flowId": "faq",
        "nodes": {a: {"nodeId": a, "type": "end"}},
    }, plan, {"flows": [plan]})          # no knowledge_base in the spec
    assert not any(n["type"] == "knowledge_base" for n in flow["nodes"].values())


def test_no_json_failure_is_diagnosable():
    """The most common generation failure was a black box.

    "response did not contain valid JSON" was 91 of the failures in one measured
    window, and nothing recorded the response, so there was no way to tell an
    empty reply from prose, a truncated document, or a swallowed API error.
    """
    import logging
    from agents.acxd_flow_generator.agent import _describe_response

    assert "EMPTY" in _describe_response("")
    assert "ERROR-ECHO" in _describe_response(
        "An error occurred (ValidationException) when calling ConverseStream")
    assert "TRUNCATED-JSON" in _describe_response('```json\n{"flowId": "x", "nodes": {')
    assert "PROSE" in _describe_response("이 플로우는 다음과 같이 구성됩니다.")
    assert "UNPARSEABLE" in _describe_response('{"flowId": "x",, }')
    assert "FENCED-NON-JSON" in _describe_response("```\njust text\n```")


def test_no_json_failure_logs_the_response_shape(caplog):
    import logging
    caplog.set_level(logging.WARNING)
    plan = {"flow_id": "Silent", "purpose": "x", "role": "operation", "steps": []}
    run_flow_generation(plan, {"flows": [plan]}, lambda _p: "   ", max_attempts=1)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Silent" in logged
    assert "EMPTY" in logged
    assert "char response" in logged      # length is recorded
    assert "prompt" in logged             # so is the prompt size


def test_the_log_never_dumps_a_whole_response(caplog):
    """Responses carry customer content; only the shape is logged."""
    import logging
    caplog.set_level(logging.WARNING)
    secret = "고객 전화번호 010-1234-5678 " * 200          # ~5KB of PII-ish text
    plan = {"flow_id": "Big", "purpose": "x", "role": "operation", "steps": []}
    run_flow_generation(plan, {"flows": [plan]}, lambda _p: secret, max_attempts=1)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert len(logged) < 600, len(logged)          # shape, not the body
    assert logged.count("010-1234-5678") <= 3      # only the short head excerpt
    assert "4400-char response" in logged          # the size IS recorded


def test_retry_prompt_keeps_the_whole_context():
    """The correction prompt used to be just the violation list.

    Every invocation gets a fresh Agent for session isolation, so there is no
    conversation history to fall back on: a 167-character prompt left the model
    with no plan, no spec and no output format, and it answered in prose. The
    diagnostics classified that as PROSE — the dominant generation failure, which
    consumed four of the five attempts on every flow it hit.
    """
    plan = {"flow_id": "lookup", "purpose": "주문 배송 조회", "role": "operation",
            "steps": [{"step": 1, "description": "인사", "node_type": "basic",
                       "determinism": "deterministic", "user_confirmed": True}]}
    spec = {"business_profile": {"company_name": "QA마트", "industry": "물류"},
            "flows": [plan],
            "data_integrations": [{"data_request_id": "orderLookup",
                                   "mode": "mock", "purpose": "조회"}]}

    first = build_generation_prompt(plan, spec)
    retry = build_generation_prompt(plan, spec, ["[SCHEMA] nodes: bad thing"])

    # The retry is the full prompt plus the violations, never a fragment.
    assert len(retry) > len(first)
    assert len(retry) > 1000, len(retry)
    for anchor in ("lookup", "주문 배송 조회", "orderLookup", "QA마트"):
        assert anchor in retry, anchor
    assert "[SCHEMA] nodes: bad thing" in retry
    assert "json" in retry.lower()          # the output format survives
    assert "Fix ONLY these problems" in retry


def test_message_type_is_defaulted_and_required():
    """The live API requires messages[].type; the SDK schema said optional.

    Live deploy (gaon-voice-agent): upsert-flows failed with
    'nodes.<uuid>.messages[0].type is required' on an escalate node whose
    message was {"body": "..."} — generated, validated, packaged, and shipped
    without the field, then rejected at the customer's machine.
    """
    from agents.acxd_flow_generator import agent as mod

    flow = {
        "flowId": "escalation",
        "nodes": {"a": {"type": "escalate", "messages": [{"body": "잠시만요"}]}},
    }
    repaired = mod.repair_generated_flow(flow, plan={}, spec={})
    # repair also re-keys node ids to UUIDs (live constraint) — check values.
    node = next(iter(repaired["nodes"].values()))
    assert node["messages"][0]["type"] == "text"

    import json as _json
    from pathlib import Path
    schema = _json.loads((Path(__file__).resolve().parents[1] / "src" / "schemas"
                          / "acxd" / "flow.schema.json").read_text())
    assert "type" in schema["$defs"]["message"]["required"], \
        "the schema must be as strict as the live API, not the SDK export"


def test_journey_tools_and_intent_capture_are_repaired():
    """Live-verified service behaviour our generator must accommodate.

    1) intent_capture is not a real node: the canvas palette has none, the
       server drops metadata.intentCapture, and a deployed flow using
       intent_capture + choice failed on the FIRST customer utterance with
       "We encountered an issue."
    2) A dataRequest tool sent only as dataRequest.dataRequestId reads back as
       dataRequest:{}. The fix is NOT to smuggle the id through
       provider/action (the fields persist but the runtime does not resolve
       that shape as a callable tool) — it is to wrap the data request in a
       helper flow and attach it with type="flow". `mcpFlow` looks like the
       documented "agent stays in control" option but fails on invocation
       with {"error": "Unknown tool type"} (measured in the Canvas debugger).
    """
    from agents.acxd_flow_generator import agent as mod

    flow = {"flowId": "journeyFlow", "nodes": {
        "a2000000-0000-4000-8000-000000000001": {"nodeId": "a2000000-0000-4000-8000-000000000001", "type": "start", "childNodes": [{"nodeId": "a2000000-0000-4000-8000-000000000002"}]},
        "a2000000-0000-4000-8000-000000000002": {"nodeId": "a2000000-0000-4000-8000-000000000002", "type": "intent_capture",
               "metadata": {"intentCapture": {"slotName": "intentName"}},
               "messages": [{"type": "text", "body": "무엇을 도와드릴까요?"}],
               "childNodes": [{"nodeId": "a2000000-0000-4000-8000-000000000003"}]},
        "a2000000-0000-4000-8000-000000000003": {"nodeId": "a2000000-0000-4000-8000-000000000003", "type": "generative_journey",
               "metadata": {"generativeJourney": {
                   "prompt": "도와주세요",
                   "tools": [{"type": "dataRequest",
                              "dataRequest": {"dataRequestId": "getPrice"}}]}},
               "childNodes": [{"nodeId": "a2000000-0000-4000-8000-000000000004"}]},
        "a2000000-0000-4000-8000-000000000004": {"nodeId": "a2000000-0000-4000-8000-000000000004", "type": "end"},
    }}
    # The journey must be user-confirmed, else rule 5 correctly downgrades any
    # unconfirmed generative node to a plain message.
    plan = {"steps": [{"step": 1, "node_type": "generative_journey",
                       "determinism": "generative", "user_confirmed": True}]}
    out = mod.repair_generated_flow(flow, plan=plan, spec={})
    types = [n["type"] for n in out["nodes"].values()]
    assert "intent_capture" not in types, types
    assert "user_input" in types, types
    journey = next(n for n in out["nodes"].values() if n["type"] == "generative_journey")
    tool = journey["metadata"]["generativeJourney"]["tools"][0]
    # A journey cannot call a data request directly. Smuggling the id through
    # provider/action does keep the FIELDS on round-trip, but that shape is not
    # what the runtime resolves as a callable tool, and the build succeeds
    # either way — so a build is no evidence. The configuration that is
    # actually DEPLOYED and passed a live multi-turn test binds each data
    # request as an mcpFlow pointing at its generated helper flow.
    assert tool == {"type": "flow", "flowId": "toolGetPrice"}, tool


def test_knowledge_base_node_gets_required_name_and_only_sdk_keys():
    """Second real deployment (2026-09-10): the service requires
    metadata.knowledgeBase.name and has no scopeTags field."""
    flow = copy.deepcopy(REFUND_FLOW)
    gid = [k for k, v in flow["nodes"].items() if v["type"] == "generative_text"][0]
    flow["nodes"][gid] = {"nodeId": gid, "type": "knowledge_base",
                          "metadata": {"knowledgeBase": {"knowledgeBaseId": "{KB:Product FAQ}",
                                                         "scopeTags": []}, "maxRetries": 2},
                          "childNodes": flow["nodes"][gid]["childNodes"]}
    plan = copy.deepcopy(PLAN)
    plan["steps"][3]["node_type"] = "knowledge_base"
    spec = {**SPEC, "flows": [plan]}
    assert any("name" in p for p in validate_generated_flow(flow, plan, spec))
    fixed = repair_generated_flow(normalize_generated_flow(flow, spec), plan, spec)
    kb = fixed["nodes"][gid]["metadata"]["knowledgeBase"]
    assert kb == {"knowledgeBaseId": "{KB:Product FAQ}", "name": "Product FAQ"}
    # `maxRetries` is not a FlowNodeMetadata member: the canonicalizer drops it
    # (and reports it) before the strict schema sees the document.
    from tools.acxd_flow_canonicalizer import canonicalize_flow
    canonical = canonicalize_flow(fixed)
    assert "maxRetries" not in canonical.flow["nodes"][gid].get("metadata", {})
    assert any("maxRetries" in p for p in canonical.problems)
    assert validate_generated_flow(canonical.flow, plan, spec) == []


def test_repair_renames_digit_slot_type_ids_and_fills_generative_prompts():
    """Live (Harbor Bank): 5 attempts lost to `cardLast4` as a slot type name
    and a generative_text node the model left without a prompt."""
    flow = copy.deepcopy(REFUND_FLOW)
    flow["slotTypes"] = [{"name": "cardLast4", "type": "cardLast4"}]
    uid = [k for k, v in flow["nodes"].items() if v["type"] == "user_input"][0]
    flow["nodes"][uid]["slot"] = {"name": "cardLast4", "type": "cardLast4"}
    gid = [k for k, v in flow["nodes"].items() if v["type"] == "generative_text"][0]
    flow["nodes"][gid]["metadata"] = {"generativeText": {"prompt": ""}}
    plan = copy.deepcopy(PLAN)
    fixed = repair_generated_flow(flow, plan, SPEC)
    assert fixed["slotTypes"][0] == {"name": "cardLast", "type": "cardLast"}
    assert fixed["nodes"][uid]["slot"] == {"name": "cardLast", "type": "cardLast"}
    assert fixed["nodes"][gid]["metadata"]["generativeText"]["prompt"].strip()


def test_model_marked_metadata_untrained_becomes_the_contract_field():
    """Live (GAON, 2026-09-14): the model wrote metadata.untrained=true for the
    call-logging flow; the service reads only the top-level field, so the flow
    stayed routable and the re-guide menu offered it."""
    import copy
    marked = copy.deepcopy(REFUND_FLOW)
    marked.setdefault("metadata", {})["untrained"] = True

    flow, problems, _ = run_flow_generation(PLAN, SPEC, lambda prompt: json.dumps(marked))
    assert problems == []
    assert flow["untrained"] is True
    assert "untrained" not in (flow.get("metadata") or {})

    plain, _, _ = run_flow_generation(PLAN, SPEC, lambda prompt: json.dumps(REFUND_FLOW))
    assert plain.get("untrained") is not True          # a routable flow stays routable


def test_a_routing_descriptor_that_says_do_not_route_here_makes_the_flow_untrained():
    """Live (GAON, 2026-09-14): the plan still said customer_initiated=True, the
    model did not set metadata.untrained this time, but its aiDescription read
    'System utility flow … not a routing target' — and the menu offered it."""
    import copy
    described = copy.deepcopy(REFUND_FLOW)
    described["aiDescription"] = ("System utility flow that logs call results to the backend. This is not a "
                                  "routing target and should not be matched against customer utterances.")
    flow, problems, _ = run_flow_generation(PLAN, SPEC, lambda prompt: json.dumps(described))
    assert problems == []
    assert flow["untrained"] is True


def test_repair_fixes_the_mechanical_mistakes_of_the_2026_09_20_runs():
    """Live (TableNow / AnyClinic, 2026-09-20): three defects each burned a
    generation attempt on the SCHEMA or RX gate although the design was right:
    the journey's exit edge wrote the system variable as the operand TYPE, an
    attached slot spelled an optional key as null, and a redirect named the
    ROLE ('followup') instead of the bundled flow id."""
    from agents.acxd_flow_generator.agent import repair_generated_flow
    plan = {
        "flow_id": "RefundFlow", "purpose": "환불", "role": "operation",
        "steps": [
            {"step": 1, "description": "사유 청취", "node_type": "generative_journey",
             "determinism": "generative", "user_confirmed": True},
            {"step": 2, "description": "안내 후 마무리", "node_type": "redirect",
             "determinism": "deterministic", "user_confirmed": True, "redirect_flow_id": "followup"},
        ],
    }
    flow = {
        "flowId": "RefundFlow",
        "slotTypes": [{"name": "reason", "type": "NLX.Text", "regex": None, "aiDescription": None}],
        "nodes": {
            "a0000000-0000-4000-8000-000000000001": {
                "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
                "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
            "a0000000-0000-4000-8000-000000000002": {
                "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "generative_journey",
                "metadata": {"generativeJourney": {"prompt": "사유를 들어 주세요", "maxSteps": 8,
                                                   "exitConditions": [{"name": "agentRequested",
                                                                       "prompt": "상담원을 원한다"}]}},
                "childNodes": [
                    {"nodeId": "a0000000-0000-4000-8000-000000000003", "name": "captured",
                     "conditions": [{"left": {"type": "slot", "name": "reason"}, "operator": "exists"}]},
                    {"nodeId": "a0000000-0000-4000-8000-000000000004", "name": "agentRequested",
                     "conditions": [{"left": {"type": "System.gjConditionIndex"},
                                     "operator": "eq", "right": {"type": "constant", "value": 0}}]},
                ]},
            "a0000000-0000-4000-8000-000000000003": {
                "nodeId": "a0000000-0000-4000-8000-000000000003", "type": "redirect",
                "metadata": {"redirect": {"type": "flow", "flowId": "followup"}}, "childNodes": []},
            "a0000000-0000-4000-8000-000000000004": {
                "nodeId": "a0000000-0000-4000-8000-000000000004", "type": "redirect",
                "metadata": {"redirect": {"type": "flow", "flowId": "{System.capturedFlow:NLX.System}"}},
                "childNodes": []},
        },
    }
    fixed = repair_generated_flow(flow, plan, {**SPEC, "flows": [plan]})

    assert fixed["slotTypes"][0] == {"name": "reason", "type": "NLX.Text"}
    journey = fixed["nodes"]["a0000000-0000-4000-8000-000000000002"]
    exit_edge = next(c for c in journey["childNodes"] if c.get("name") == "agentRequested")
    assert exit_edge["conditions"][0]["left"] == {"type": "system", "name": "System.gjConditionIndex"}
    redirects = {nid: n["metadata"]["redirect"]["flowId"]
                 for nid, n in fixed["nodes"].items() if n.get("type") == "redirect"}
    assert redirects["a0000000-0000-4000-8000-000000000003"] == "FollowUpFlow"
    # a runtime placeholder is not an alias and passes through untouched
    assert redirects["a0000000-0000-4000-8000-000000000004"] == "{System.capturedFlow:NLX.System}"
