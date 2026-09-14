"""Deterministic ACXD system flows: the exact shapes the live validation proved.

Every assertion here is transcribed from a flow document that was deployed to a
real ACXD workspace and driven over Amazon Connect chat (a sandbox Connect Customer account,
2026-09-12). The platform BUILDS the wrong shapes without complaining and then
answers nothing, so a shape change that these tests do not catch is a change no
gate catches.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_system_flows import (  # noqa: E402
    CAPTURED_FLOW_PLACEHOLDER,
    MAX_FALLBACK_ATTEMPTS,
    MORE_HELP_SLOT_NAME,
    REQUEST_AGENT_AI_DESCRIPTION,
    SYSTEM_FLOW_BUILDERS,
    YES_NO_SLOT_TYPE_ID,
    build_escalation_flow,
    build_fallback_flow,
    build_follow_up_flow,
    build_request_agent_flow,
    build_system_flow,
    build_welcome_flow,
    build_yes_no_slot_type,
    is_system_flow_role,
    operation_labels,
    resolve_system_flow_ids,
    system_flow_language,
)
from tools.validate_acxd_consistency import validate_acxd_consistency  # noqa: E402
from tools.validate_acxd_flow import validate_acxd_asset  # noqa: E402

KO_SPEC = {
    "business_profile": {"company_name": "삼성전자로지텍", "language": "ko-KR"},
    "flows": [
        {"flow_id": "DeliveryStatusByOrderNumber", "role": "operation",
         "purpose": "주문번호(10자리)를 받아 배송상태와 예상 배송일을 조회·안내한다.",
         "display_name": "배송 조회", "operation_id": "get_delivery_status"},
        {"flow_id": "GetCleaningPrice", "role": "operation",
         "purpose": "제품유형·서비스유형 조합으로 세척 단가를 안내한다.",
         "display_name": "에어컨 세척 가격 문의", "operation_id": "get_cleaning_price"},
        {"flow_id": "CreateCleaningReservation", "role": "operation",
         "purpose": "개인정보 동의 후 세척 예약을 접수한다.",
         "display_name": "세척 예약", "operation_id": "create_cleaning_reservation"},
        {"flow_id": "WelcomeFlow", "role": "welcome", "purpose": "greeting"},
        {"flow_id": "FallbackFlow", "role": "fallback", "purpose": "fallback"},
        {"flow_id": "EscalationFlow", "role": "escalation", "purpose": "handoff"},
    ],
    "application": {"locales": ["ko-KR"]},
}

EN_SPEC = {
    "business_profile": {"company_name": "Acme", "language": "en-US"},
    "flows": [{"flow_id": "RefundFlow", "role": "operation", "purpose": "refunds"}],
    "application": {"locales": ["en-US"]},
}


def _types(flow: dict) -> dict:
    return {nid: node["type"] for nid, node in flow["nodes"].items()}


def _node_of_type(flow: dict, node_type: str) -> dict:
    matches = [n for n in flow["nodes"].values() if n["type"] == node_type]
    assert matches, f"no {node_type} node in {flow['flowId']}"
    return matches[0]


def _walk(flow: dict) -> list[str]:
    """Node types in breadth-first order from `start`, for a shape assertion."""
    start = next(n for n in flow["nodes"].values() if n["type"] == "start")
    order, queue, seen = [], [start["nodeId"]], set()
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        node = flow["nodes"][nid]
        order.append(node["type"])
        for child in node.get("childNodes") or []:
            queue.append(child["nodeId"])
    return order


def _redirect_targets(flow: dict) -> list[str]:
    return [n["metadata"]["redirect"]["flowId"]
            for n in flow["nodes"].values() if n["type"] == "redirect"]


# ---------------------------------------------------------------------------
# every system flow: schema, ASCII metadata, project-language messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(SYSTEM_FLOW_BUILDERS))
def test_every_system_flow_is_schema_valid(role):
    flow = build_system_flow(role, KO_SPEC)
    assert validate_acxd_asset("flow", flow) == [], flow["flowId"]


@pytest.mark.parametrize("role", sorted(SYSTEM_FLOW_BUILDERS))
def test_descriptions_are_ascii_and_messages_are_korean(role):
    """The live API rejects non-ASCII `description` / `aiDescription` outright,
    while `messages[].body` MUST stay in the customer's language."""
    flow = build_system_flow(role, KO_SPEC)
    for field in ("description", "aiDescription"):
        assert flow[field], f"{flow['flowId']}.{field} must not be empty"
        assert all(ord(c) < 128 for c in flow[field]), flow[field]
    bodies = [m["body"] for n in flow["nodes"].values() for m in n.get("messages") or []]
    if role == "agent_request":
        # A pure routing entry: it says nothing and hands straight over, so the
        # customer hears EscalationFlow's handoff message once, not twice.
        assert bodies == []
        return
    assert bodies, f"{flow['flowId']} says nothing to the customer"
    assert any(any(ord(c) > 127 for c in body) for body in bodies), bodies


@pytest.mark.parametrize("role", sorted(SYSTEM_FLOW_BUILDERS))
def test_node_ids_are_v4_shaped_and_deterministic(role):
    """A changed node id is a NEW node to the service, so regeneration must be
    byte-identical; and only v4-shaped ids round-trip through CreateFlow."""
    import re

    first = build_system_flow(role, KO_SPEC)
    second = build_system_flow(role, KO_SPEC)
    assert first == second
    v4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    for nid, node in first["nodes"].items():
        assert v4.match(nid), nid
        assert node["nodeId"] == nid


@pytest.mark.parametrize("role,untrained", [
    ("welcome", True), ("fallback", True), ("followup", True),
    ("escalation", True), ("agent_request", False),
])
def test_untrained_flags(role, untrained):
    """R2: system flows are not routing targets, so they are untrained. The one
    exception is RequestAgentFlow — 'connect me to a human' has to be MATCHED,
    because EscalationFlow is a default behaviour rather than a routing target.
    """
    assert build_system_flow(role, KO_SPEC)["untrained"] is untrained


# ---------------------------------------------------------------------------
# WelcomeFlow — R1 (this IS intent routing)
# ---------------------------------------------------------------------------

def test_welcome_flow_shape():
    flow = build_welcome_flow(KO_SPEC)
    assert flow["flowId"] == "WelcomeFlow"
    assert _walk(flow) == ["start", "basic", "user_input", "redirect", "redirect", "end"]
    assert "generative_journey" not in set(_types(flow).values())

    greeting = _node_of_type(flow, "basic")
    # No approved greeting in the spec → company + the operations menu
    assert greeting["messages"][0]["body"] == (
        "안녕하세요, 삼성전자로지텍입니다. 배송 조회, 에어컨 세척 가격 문의, 세척 예약 등을 "
        "도와드릴 수 있어요. 무엇을 도와드릴까요?")
    # the counter is reset on entry, not in FallbackFlow
    assert greeting["metadata"]["stateModifications"] == [{
        "type": "context", "name": "fallbackAttempts", "modification": "set",
        "value": {"type": "constant", "value": 0},
    }]
    assert flow["contextVariables"] == [{"name": "fallbackAttempts", "type": "number"}]

    listen = _node_of_type(flow, "user_input")
    assert [c["conditions"] for c in listen["childNodes"]] == [
        [{"left": {"type": "captured_flow"}, "operator": "exists"}],
        [{"left": {"type": "captured_flow"}, "operator": "not_exists"}],
    ]
    assert _redirect_targets(flow) == [CAPTURED_FLOW_PLACEHOLDER, "FallbackFlow"]


def test_welcome_greeting_without_a_company_name():
    flow = build_welcome_flow({"application": {"locales": ["ko-KR"]}})
    assert _node_of_type(flow, "basic")["messages"][0]["body"] == \
        "안녕하세요. 무엇을 도와드릴까요?"


# ---------------------------------------------------------------------------
# FallbackFlow — R4
# ---------------------------------------------------------------------------

def test_fallback_flow_counts_reguides_and_escalates():
    flow = build_fallback_flow(KO_SPEC)
    assert _walk(flow) == ["start", "basic", "choice", "redirect", "basic",
                           "end", "user_input", "redirect", "redirect"]

    count = flow["nodes"][flow["nodes"][
        next(n for n, v in flow["nodes"].items() if v["type"] == "start")
    ]["childNodes"][0]["nodeId"]]
    assert count["type"] == "basic" and "messages" not in count
    assert count["metadata"]["stateModifications"] == [
        {"type": "context", "name": "fallbackAttempts", "modification": "increment"}]

    branches = _node_of_type(flow, "choice")["childNodes"]
    assert branches[0]["conditions"] == [{
        "left": {"type": "context", "name": "fallbackAttempts"},
        "operator": "gte",
        "right": {"type": "constant", "value": MAX_FALLBACK_ATTEMPTS},
    }]
    # the "otherwise" branch is an EMPTY condition list (console encoding)
    assert branches[1]["conditions"] == []

    assert sorted(_redirect_targets(flow)) == sorted(
        [CAPTURED_FLOW_PLACEHOLDER, "EscalationFlow", "FallbackFlow"])
    escalate_redirect = next(n for n in flow["nodes"].values()
                             if n["type"] == "redirect"
                             and n["metadata"]["redirect"]["flowId"] == "EscalationFlow")
    assert escalate_redirect["messages"][0]["body"].startswith("요청을 정확히")


def test_fallback_reguide_lists_the_operations_in_the_project_language():
    body = next(m["body"] for n in build_fallback_flow(KO_SPEC)["nodes"].values()
                for m in n.get("messages") or [] if "이해하지 못했습니다" in m["body"])
    assert body == ("죄송합니다, 잘 이해하지 못했습니다. 배송 조회, 에어컨 세척 가격 문의, "
                    "세척 예약 중 무엇을 도와드릴까요? 상담사 연결도 가능합니다.")


def test_fallback_reguide_without_operations_still_asks():
    body = next(m["body"] for n in build_fallback_flow(
        {"application": {"locales": ["ko-KR"]}})["nodes"].values()
        for m in n.get("messages") or [] if "이해하지 못했습니다" in m["body"])
    assert body == ("죄송합니다, 잘 이해하지 못했습니다. 무엇을 도와드릴까요? "
                    "상담사 연결도 가능합니다.")


def test_operation_labels_use_display_names_and_skip_system_flows():
    labels = operation_labels({"flows": [
        {"role": "operation", "display_name": " 배송 조회 ",
         "purpose": "배송 조회를 처리한다. 주문번호로 조회한다."},
        {"role": "welcome", "purpose": "인사"},
        {"role": "operation", "display_name": "가격 문의", "purpose": "가격을 안내한다."},
    ]})
    assert labels == ["배송 조회", "가격 문의"]


def test_operation_labels_never_cut_a_menu_out_of_purpose_sentences():
    """A label sliced out of a purpose sentence reads as a fragment ("고객명"
    for "find the order by customer name, phone and address"), and a menu that
    names some operations and drops others misleads the caller — so without a
    display_name on EVERY operation flow the fallback uses the generic
    re-guidance instead of a partial list."""
    labels = operation_labels({"flows": [
        {"role": "operation", "display_name": "배송 조회", "purpose": "배송 조회"},
        {"role": "operation", "purpose": "고객명·전화번호·주소로 주문번호를 찾아준다."},
    ]})
    assert labels == []


# ---------------------------------------------------------------------------
# FollowUpFlow — R3 + R6 (the flow that keeps the session alive)
# ---------------------------------------------------------------------------

def test_follow_up_flow_shape():
    flow = build_follow_up_flow(KO_SPEC)
    assert flow["flowId"] == "FollowUpFlow"
    assert _walk(flow) == ["start", "basic", "user_choice", "choice", "choice", "basic",
                           "basic", "redirect", "redirect", "user_input", "end"]

    # R6: the slot is cleared at the START of the flow, before it is asked again
    start = _node_of_type(flow, "start")
    clear = flow["nodes"][start["childNodes"][0]["nodeId"]]
    assert clear["metadata"]["stateModifications"] == [
        {"type": "slot", "name": MORE_HELP_SLOT_NAME, "modification": "clear"}]

    # S4: yes/no needs a real slot type; S2: slotTypeId is the attached slot NAME
    assert flow["slotTypes"] == [{
        "name": MORE_HELP_SLOT_NAME, "type": YES_NO_SLOT_TYPE_ID, "sensitive": False,
        "aiDescription": "Whether the customer wants further help: yes or no",
    }]
    ask = _node_of_type(flow, "user_choice")
    assert ask["messages"][0]["body"] == "더 도와드릴 일이 있을까요?"
    assert ask["metadata"]["choice"] == {"source": "slotType",
                                        "slotTypeId": MORE_HELP_SLOT_NAME}
    # S3: a user_choice branches on `slot <name> exists`, never captured_flow
    assert [c["conditions"] for c in ask["childNodes"]] == [
        [{"left": {"type": "slot", "name": MORE_HELP_SLOT_NAME}, "operator": "exists"}],
        [{"left": {"type": "slot", "name": MORE_HELP_SLOT_NAME},
          "operator": "not_exists"}],
    ]

    captured_target = next(c["nodeId"] for c in ask["childNodes"] if c["name"] == "captured")
    yes_branch, no_branch = flow["nodes"][captured_target]["childNodes"]
    assert yes_branch["conditions"] == [{
        "left": {"type": "slot", "name": MORE_HELP_SLOT_NAME},
        "operator": "eq", "right": {"type": "constant", "value": "예"},
    }]
    assert no_branch["conditions"] == []
    assert sorted(_redirect_targets(flow)) == sorted(
        [CAPTURED_FLOW_PLACEHOLDER, "FallbackFlow"])


def test_follow_up_yes_value_follows_the_language():
    yes = next(c["conditions"][0]["right"]["value"]
               for n in build_follow_up_flow(EN_SPEC)["nodes"].values()
               if n["type"] == "choice" for c in n["childNodes"]
               if c["conditions"] and c["conditions"][0]["left"].get("type") == "slot")
    assert yes == "yes"


def test_follow_up_routes_a_direct_request_given_instead_of_yes_no():
    """Live (SELC, 2026-09-14): '더 도와드릴 일이 있을까요?' answered with
    '세척 가격도 알려주세요' went to the fallback ('잘 이해하지 못했습니다')
    although the utterance named a flow."""
    flow = build_follow_up_flow(KO_SPEC)
    ask = _node_of_type(flow, "user_choice")
    not_captured = next(c for c in ask["childNodes"] if c["name"] == "notCaptured")
    direct = flow["nodes"][not_captured["nodeId"]]
    assert direct["type"] == "choice"
    recognized, unrecognized = direct["childNodes"]
    assert recognized["conditions"] == [{"left": {"type": "captured_flow"}, "operator": "exists"}]
    assert flow["nodes"][recognized["nodeId"]]["metadata"]["redirect"]["flowId"] == CAPTURED_FLOW_PLACEHOLDER
    assert flow["nodes"][unrecognized["nodeId"]]["metadata"]["redirect"]["flowId"] == "FallbackFlow"


# ---------------------------------------------------------------------------
# RequestAgentFlow — R2 (the one routable system flow)
# ---------------------------------------------------------------------------

def test_request_agent_flow_shape():
    flow = build_request_agent_flow(KO_SPEC)
    assert flow["flowId"] == "RequestAgentFlow"
    assert _walk(flow) == ["start", "redirect", "end"]
    assert flow["aiDescription"] == REQUEST_AGENT_AI_DESCRIPTION
    assert flow["contextVariables"] == [{"name": "failReason", "type": "text"}]

    redirect = _node_of_type(flow, "redirect")
    assert redirect["metadata"]["redirect"] == {"type": "flow",
                                               "flowId": "EscalationFlow"}
    assert redirect["metadata"]["stateModifications"] == [{
        "type": "context", "name": "failReason", "modification": "set",
        "value": {"type": "constant", "value": "customer_requested_agent"},
    }]


# ---------------------------------------------------------------------------
# EscalationFlow — R7 (escalate is terminal)
# ---------------------------------------------------------------------------

def test_escalation_flow_escalate_node_is_terminal():
    flow = build_escalation_flow(KO_SPEC)
    assert _walk(flow) == ["start", "basic", "escalate"]
    # R7: an `end` after escalate made Connect report Success instead of
    # Escalation, so the caller was never transferred.
    assert "end" not in set(_types(flow).values())
    escalate = _node_of_type(flow, "escalate")
    assert "childNodes" not in escalate
    assert escalate["messages"][0]["body"].startswith("지금 상담사에게")
    assert escalate["metadata"]["stateModifications"] == [{
        "type": "context", "name": "failReason", "modification": "set",
        "value": {"type": "variable", "name": "failReason"},
    }]


# ---------------------------------------------------------------------------
# yesNo slot type — S4
# ---------------------------------------------------------------------------

def test_yes_no_slot_type_korean():
    doc = build_yes_no_slot_type(KO_SPEC)
    assert validate_acxd_asset("slot_type", doc) == []
    assert doc["slotTypeId"] == "yesNo"
    assert doc["sensitive"] is False and doc["metadata"] == {}
    assert [v["value"] for v in doc["values"]] == ["예", "아니요"]
    assert "네" in doc["values"][0]["synonyms"]
    assert "아니오" in doc["values"][1]["synonyms"]


def test_yes_no_slot_type_follows_the_language():
    assert [v["value"] for v in build_yes_no_slot_type(EN_SPEC)["values"]] == \
        ["yes", "no"]
    # an unsupported language falls back to English, not to Korean
    unknown = build_yes_no_slot_type({"application": {"locales": ["pt-BR"]}})
    assert [v["value"] for v in unknown["values"]] == ["yes", "no"]


# ---------------------------------------------------------------------------
# ids, language resolution, dispatch
# ---------------------------------------------------------------------------

def test_resolved_ids_follow_the_interviews_naming():
    spec = {"flows": [{"flow_id": "Welcome", "role": "welcome"},
                      {"flow_id": "Handoff", "role": "escalation"}]}
    ids = resolve_system_flow_ids(spec)
    assert ids["welcome"] == "Welcome" and ids["escalation"] == "Handoff"
    # ...and every redirect points at the id the bundle really ships
    assert _redirect_targets(build_request_agent_flow(spec)) == ["Handoff"]
    assert "Handoff" in build_fallback_flow(spec)["nodes"][
        next(n for n, v in build_fallback_flow(spec)["nodes"].items()
             if v["type"] == "redirect"
             and v["metadata"]["redirect"]["flowId"] == "Handoff")
    ]["metadata"]["redirect"]["flowId"]


def test_language_precedence_and_role_predicate():
    assert system_flow_language({"application": {"primary_locale": "ja-JP"}}) == "ja-JP"
    assert system_flow_language({"business_profile": {"language": "ko"}}) == "ko-KR"
    assert system_flow_language({}) == "en-US"
    assert is_system_flow_role("followup") and is_system_flow_role("agent_request")
    assert not is_system_flow_role("operation")
    assert build_system_flow("nonsense", KO_SPEC) is None


def test_system_flows_and_yes_no_are_cross_consistent():
    """All five together, with the slot type they need — no dangling refs."""
    flows = [build_system_flow(role, KO_SPEC) for role in sorted(SYSTEM_FLOW_BUILDERS)]
    flows += [{"flowId": p["flow_id"], "nodes": {
        "a0000000-0000-4000-8000-000000000001": {
            "nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
            "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
        "a0000000-0000-4000-8000-000000000002": {
            "nodeId": "a0000000-0000-4000-8000-000000000002", "type": "end"},
    }} for p in KO_SPEC["flows"] if p["role"] == "operation"]
    bundle = {"flows": flows, "slot_types": [build_yes_no_slot_type(KO_SPEC)]}
    assert [str(v) for v in validate_acxd_consistency(bundle)] == []


# ---------------------------------------------------------------------------
# generator wiring: system flows never reach the LLM
# ---------------------------------------------------------------------------

class _FakeSpec:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        import copy

        return copy.deepcopy(self._payload)


@pytest.fixture()
def wired_generator(monkeypatch):
    """`generate_acxd_flows` with storage and the LLM replaced by recorders."""
    from agents.acxd_flow_generator import agent as generator

    stored: list[dict] = []
    invoked: list[str] = []

    monkeypatch.setattr(generator, "_store_flow",
                        lambda session_id, flow: stored.append(flow))

    def _invoke_factory():
        def invoke(prompt):
            invoked.append(prompt)
            return "not JSON at all"
        return invoke

    monkeypatch.setattr(generator, "_make_llm_invoke", _invoke_factory)
    run = getattr(generator.generate_acxd_flows, "_raw", generator.generate_acxd_flows)

    def call(spec, **kwargs):
        monkeypatch.setattr(generator, "get_acxd_spec", lambda *a, **k: _FakeSpec(spec))
        return run(**kwargs), stored, invoked

    return call


def _confirmed(plan: dict) -> dict:
    return {**plan, "steps": [{"step": 1, "description": "d", "node_type": "basic",
                               "determinism": "deterministic", "user_confirmed": True}]}


SYSTEM_ONLY_SPEC = {
    "business_profile": {"company_name": "삼성전자로지텍", "language": "ko-KR"},
    "application": {"locales": ["ko-KR"]},
    "flows": [
        _confirmed({"flow_id": "WelcomeFlow", "role": "welcome", "purpose": "greeting"}),
        _confirmed({"flow_id": "FallbackFlow", "role": "fallback", "purpose": "fallback"}),
        _confirmed({"flow_id": "EscalationFlow", "role": "escalation", "purpose": "handoff"}),
    ],
}


def test_system_roles_bypass_the_llm_entirely(wired_generator):
    result, stored, invoked = wired_generator(SYSTEM_ONLY_SPEC)
    assert result["status"] == "success", result
    assert invoked == [], "a system flow must never be sent to the model"
    assert all(item.get("source") == "deterministic" for item in result["generated"])
    assert all(item["attempts"] == 0 for item in result["generated"])


def test_follow_up_and_request_agent_are_always_generated(wired_generator):
    """The interview planned neither, and the application still needs both: R3
    (an operation's success path has nowhere to go without FollowUpFlow) and R2
    ('connect me to a human' matches nothing without RequestAgentFlow)."""
    result, stored, _ = wired_generator(SYSTEM_ONLY_SPEC)
    assert [f["flowId"] for f in stored] == [
        "WelcomeFlow", "FallbackFlow", "EscalationFlow",
        "FollowUpFlow", "RequestAgentFlow"]
    assert {item["flow_id"] for item in result["generated"]} == {
        "WelcomeFlow", "FallbackFlow", "EscalationFlow",
        "FollowUpFlow", "RequestAgentFlow"}


def test_unconfirmed_system_steps_do_not_block_generation(wired_generator):
    """A system flow's planned steps are a description shown to the user, not the
    design the builder follows, so an unconfirmed one cannot block the run —
    while an unconfirmed OPERATION step still must."""
    spec = {**SYSTEM_ONLY_SPEC, "flows": [
        {"flow_id": "WelcomeFlow", "role": "welcome", "purpose": "greeting",
         "steps": [{"step": 1, "description": "greet", "node_type": "basic",
                    "determinism": "deterministic", "user_confirmed": False}]},
    ]}
    result, stored, invoked = wired_generator(spec)
    assert result["status"] == "success", result
    assert invoked == []
    assert "WelcomeFlow" in [f["flowId"] for f in stored]


def test_unconfirmed_operation_steps_still_block_generation(wired_generator):
    spec = {**SYSTEM_ONLY_SPEC, "flows": SYSTEM_ONLY_SPEC["flows"] + [
        {"flow_id": "RefundFlow", "role": "operation", "purpose": "refunds",
         "operation_id": "refund", "steps": [
             {"step": 1, "description": "ask", "node_type": "user_choice",
              "determinism": "deterministic", "user_confirmed": False}]},
    ]}
    result, _, invoked = wired_generator(spec)
    assert result["status"] == "error"
    assert "RefundFlow" in result["message"]
    assert invoked == []


def test_flow_ids_subset_selects_a_single_system_flow(wired_generator):
    result, stored, _ = wired_generator(SYSTEM_ONLY_SPEC, flow_ids=["FallbackFlow"])
    assert [f["flowId"] for f in stored] == ["FallbackFlow"]
    assert result["status"] == "success"


def test_runtime_contract_hook_is_optional_and_used_when_present(monkeypatch):
    """The runtime-contract normalizer is a separate module: generation must work without it and
    must run it (after canonicalization, before validation) when it is there."""
    import sys as _sys
    import types

    from agents.acxd_flow_generator import agent as generator

    flow = {"flowId": "RefundFlow", "nodes": {}}
    calls: list[dict] = []

    module = types.ModuleType("tools.acxd_runtime_contract")

    def apply_runtime_contract(doc, **kwargs):
        calls.append(kwargs)
        return {**doc, "aiDescription": "contracted"}, ["R3: redirected to FollowUpFlow"]

    module.apply_runtime_contract = apply_runtime_contract
    monkeypatch.setitem(_sys.modules, "tools.acxd_runtime_contract", module)

    out, notes = generator.apply_runtime_contract_if_available(
        flow, {"flow_id": "RefundFlow", "role": "operation"},
        {"flows": [{"flow_id": "RefundFlow", "role": "operation"}],
         "slot_types": [], "data_integrations": [],
         "application": {"context_variables": [{"name": "customerPhone",
                                                "type": "string"}]}})
    assert out["aiDescription"] == "contracted"
    assert notes == ["R3: redirected to FollowUpFlow"]
    assert calls[0]["role"] == "operation"
    assert calls[0]["follow_up_flow_id"] == "FollowUpFlow"
    assert calls[0]["escalation_flow_id"] == "EscalationFlow"
    # the yesNo slot type is always available to the normalizer
    assert "yesNo" in calls[0]["slot_type_ids"]
    assert calls[0]["slot_type_docs"]["yesNo"]["values"]
    assert calls[0]["context_variables"] == ["customerPhone"]
    assert "FollowUpFlow" in calls[0]["flow_ids"]

    # A broken normalizer must not take generation down with it.
    def exploding(doc, **kwargs):
        raise TypeError("signature drift")

    module.apply_runtime_contract = exploding
    same, no_notes = generator.apply_runtime_contract_if_available(
        flow, {"flow_id": "RefundFlow"}, {"flows": []})
    assert same is flow and no_notes == []


def test_generation_prompt_never_offers_bare_text_number_boolean():
    """S1: an attached slot typed `text` / `number` / `boolean` silently disables
    flow recognition for the WHOLE application, so the prompt must not suggest
    them — it used to say "(none — use text/number/boolean)"."""
    from agents.acxd_flow_generator.agent import build_generation_prompt

    prompt = build_generation_prompt(
        {"flow_id": "RefundFlow", "role": "operation", "purpose": "refunds"},
        {"business_profile": {}, "flows": [], "slot_types": [],
         "data_integrations": []})
    assert "use text/number/boolean" not in prompt
    assert "NLX.AlphaNumeric" in prompt
    # ...and the model is told where a successful operation goes
    assert "FollowUpFlow" in prompt


def test_welcome_flow_speaks_the_approved_greeting_and_never_the_project_slug():
    """Live (SELC, 2026-09-13): 'selc-aicc' (the project slug) was greeted as the
    company because the profile had no company name; the interview had
    recorded the customer's greeting verbatim all along."""
    import copy
    spec = copy.deepcopy(KO_SPEC)
    spec["business_profile"] = {"project_name": "selc-aicc", "company_name": "selc-aicc", "language": "ko-KR",
                                "greeting": "안녕하세요, 삼성전자로지텍 고객센터입니다. 무엇을 도와드릴까요?"}
    flow = build_welcome_flow(spec)
    assert _node_of_type(flow, "basic")["messages"][0]["body"] == \
        "안녕하세요, 삼성전자로지텍 고객센터입니다. 무엇을 도와드릴까요?"
    spec["business_profile"].pop("greeting")
    flow = build_welcome_flow(spec)
    body = _node_of_type(flow, "basic")["messages"][0]["body"]
    assert body.startswith("안녕하세요. 배송 조회, ") and "selc-aicc" not in body
    spec["flows"] = []
    flow = build_welcome_flow(spec)
    assert _node_of_type(flow, "basic")["messages"][0]["body"] == "안녕하세요. 무엇을 도와드릴까요?"


def test_operation_labels_leave_out_internal_operations():
    """Live (SELC, 2026-09-13): the re-guide menu offered '통화 결과 기록'
    (call-result logging) — an operation the customer never asks for."""
    import copy
    spec = copy.deepcopy(KO_SPEC)
    spec["flows"].append({"flow_id": "LogCallResult", "role": "operation", "purpose": "통화 결과를 기록한다.",
                          "display_name": "통화 결과 기록", "operation_id": "log_call_result",
                          "customer_initiated": False})
    labels = operation_labels(spec)
    assert "통화 결과 기록" not in labels
    assert "배송 조회" in labels


def test_operation_labels_leave_out_flows_the_generator_marked_untrained():
    """Live (SELC, 2026-09-14): the plan still said customer_initiated=True for
    call-result logging, the model marked the generated flow untrained, and the
    re-guide offered '통화 결과 기록' — a flow the caller cannot reach."""
    import copy
    spec = copy.deepcopy(KO_SPEC)
    spec["flows"].append({"flow_id": "LogCallResult", "role": "operation", "purpose": "통화 결과를 기록한다.",
                          "display_name": "통화 결과 기록", "operation_id": "log_call_result"})
    assert "통화 결과 기록" in operation_labels(spec)
    spec["untrained_flow_ids"] = ["LogCallResult"]
    labels = operation_labels(spec)
    assert "통화 결과 기록" not in labels and "배송 조회" in labels
