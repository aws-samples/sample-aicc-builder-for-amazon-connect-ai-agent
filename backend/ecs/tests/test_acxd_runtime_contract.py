"""acxd_runtime_contract — the live-proven runtime contract, rule by rule.

Every assertion here is anchored on a real failure. ``tests/fixtures/acxd_live/
gaon_generator_output.json`` is the UNMODIFIED ACXD flow-generator output from a
sandbox validation (Amazon Connect Customer / Agentic CX Designer, a sandbox
account in ap-northeast-2, 2026-09-13): those flows passed every schema check, built with
zero issues, deployed — and then the bot said nothing, skipped its own
questions, or reported Success where Connect should have seen Escalation. The
same file carries the slot type documents and data requests the working
application ended up with, so the normalizer is exercised against the real
cross-asset context rather than a hand-built stub.

The tests are written as "the broken document goes in, the shape that actually
held a conversation comes out", plus the two properties the module promises:
determinism/idempotency, and ``runtime_contract_violations`` reporting exactly
the residue ``apply_runtime_contract`` refused to guess at.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_runtime_contract import (  # noqa: E402
    CROSS_SCOPE_RULES,
    FLOW_SCOPE_RULES,
    NLX_BUILTIN_SLOT_TYPES,
    NORMALIZER_SCOPE_RULES,
    YES_NO_SLOT_TYPE,
    apply_runtime_contract,
    runtime_contract_violations,
)
from tools.validate_acxd_flow import validate_acxd_asset  # noqa: E402

FIXTURE = os.path.join(_HERE, "fixtures", "acxd_live", "gaon_generator_output.json")

with open(FIXTURE, encoding="utf-8") as handle:
    LIVE = json.load(handle)

#: Roles as the live application wired them (R2): the four system events are
#: default-behaviour flows, everything else is an operation flow.
ROLES = {
    "WelcomeFlow": "welcome",
    "FallbackFlow": "fallback",
    "EscalationFlow": "escalation",
    "FollowUpFlow": "follow_up",
}
OPERATION_FLOWS = (
    "DeliveryStatusByOrderNumber", "GetCleaningPrice",
    "SearchOrderByCustomerInfo", "CreateCleaningReservation",
)


def context(flow_id: str, **overrides) -> dict:
    """The cross-asset context the bundle gives the normalizer."""
    kwargs = {
        "role": ROLES.get(flow_id, "operation"),
        "slot_type_ids": set(LIVE["slot_types"]),
        "slot_type_docs": LIVE["slot_types"],
        "data_requests": LIVE["data_requests"],
        "flow_ids": set(LIVE["flow_ids"]),
        "context_variables": set(LIVE["context_variables"]),
    }
    kwargs.update(overrides)
    return kwargs


def broken(flow_id: str) -> dict:
    return copy.deepcopy(LIVE["flows_broken"][flow_id])


def normalized(flow_id: str, **overrides) -> tuple[dict, list[str]]:
    return apply_runtime_contract(broken(flow_id), **context(flow_id, **overrides))


def nodes_of(flow: dict, node_type: str) -> list[dict]:
    return [n for n in flow["nodes"].values() if n.get("type") == node_type]


def slot_of(flow: dict, name: str) -> dict:
    return next(s for s in flow["slotTypes"] if s["name"] == name)


def edge_named(node: dict, name: str) -> dict:
    return next(e for e in node["childNodes"] if e.get("name") == name)


def past_agent_gate(flow: dict, node_id: str) -> str:
    """E2 puts an agent-request gate in front of every 'not captured' target;
    return the node the gate falls through to (or ``node_id`` when ungated)."""
    node = flow["nodes"][node_id]
    edges = node.get("childNodes") or []
    if node.get("type") == "choice" and edges and str(edges[0].get("name") or "").startswith("agentRequest:"):
        return edges[-1]["nodeId"]
    return node_id


def redirect_to(flow: dict, flow_id: str) -> dict:
    return next(n for n in nodes_of(flow, "redirect")
                if (n.get("metadata") or {}).get("redirect", {}).get("flowId") == flow_id)


def clears(node: dict) -> list[str]:
    return [m["name"] for m in (node.get("metadata") or {}).get("stateModifications") or []
            if m.get("type") == "slot" and m.get("modification") == "clear"]


def bodies(flow: dict) -> list[str]:
    return [m["body"] for n in flow["nodes"].values()
            for m in n.get("messages") or [] if isinstance(m, dict)]


# ---------------------------------------------------------------------------
# S1 — attached slot vocabulary
# ---------------------------------------------------------------------------

def test_s1_maps_generator_aliases_to_nlx_builtins():
    """`text` / `number` disabled recognition for the WHOLE application."""
    flow, notes = normalized("SearchOrderByCustomerInfo")
    assert slot_of(flow, "customerName")["type"] == "NLX.Text"
    assert slot_of(flow, "address")["type"] == "NLX.Text"
    assert any("type 'text' → 'NLX.Text'" in note for note in notes)
    for slot in flow["slotTypes"]:
        assert slot["type"] in NLX_BUILTIN_SLOT_TYPES or slot["type"] in LIVE["slot_types"]


def test_s1_number_alias_and_yes_no_slot_type():
    flow, _ = normalized("CreateCleaningReservation")
    assert slot_of(flow, "quantity")["type"] == "NLX.Number"
    # privacyConsent is compared only against the yesNo values, and there is no
    # boolean built-in (S4), so it must attach the custom yesNo slot type.
    assert slot_of(flow, "privacyConsent")["type"] == YES_NO_SLOT_TYPE


def test_s1_declared_boolean_becomes_the_yes_no_slot_type():
    flow = broken("GetCleaningPrice")
    flow["slotTypes"].append({"name": "consent", "type": "boolean"})
    out, notes = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    assert slot_of(out, "consent")["type"] == YES_NO_SLOT_TYPE
    assert any("yesNo" in note for note in notes)


def test_s1_boolean_without_a_bundled_yes_no_slot_type_is_a_violation():
    flow = broken("GetCleaningPrice")
    flow["slotTypes"].append({"name": "consent", "type": "boolean"})
    problems = runtime_contract_violations(
        flow, **context("GetCleaningPrice", slot_type_ids={"productType", "serviceType"},
                        slot_type_docs={}))
    assert any("S1" in p and YES_NO_SLOT_TYPE in p for p in problems)


def test_s1_unknown_nlx_namespace_is_a_flow_scope_violation():
    """The NLX namespace is a closed set — a plausible-looking name is not one."""
    flow = broken("GetCleaningPrice")
    flow["slotTypes"][0]["type"] = "NLX.Boolean"
    problems = runtime_contract_violations(flow, scope="flow")
    assert any("S1" in p and "NLX.Boolean" in p for p in problems)


def test_s1_unbundled_custom_slot_type_is_a_cross_scope_violation():
    flow = broken("GetCleaningPrice")
    flow["slotTypes"][0]["type"] = "productTypeV2"
    problems = runtime_contract_violations(
        flow, **context("GetCleaningPrice"), scope="cross")
    assert any("S1" in p and "productTypeV2" in p for p in problems)


def test_s1_unbundled_open_value_type_is_resolved_from_the_slot_plan():
    """Replay of the ORIGINAL GAON output through the fixed generator: the old
    generator had emitted a one-item custom slot type ``orderNumber`` and the
    model attached it; the new slot-type builder no longer emits it, so the
    attached type names nothing. The interview's slot plan knows the value's
    shape (a 10-digit regex) — that resolves it to the built-in the live bundle
    needed instead of failing the flow."""
    flow = broken("DeliveryStatusByOrderNumber")
    slot_of(flow, "orderNumber")["type"] = "orderNumber"
    slot_of(flow, "orderNumber").pop("regex", None)
    ids = set(LIVE["slot_types"]) - {"orderNumber"}
    docs = {k: v for k, v in LIVE["slot_types"].items() if k != "orderNumber"}
    plans = {"orderNumber": {"name": "orderNumber", "type": "text", "regex": "^[0-9]{10}$"}}
    fixed, notes = apply_runtime_contract(
        flow, **context("DeliveryStatusByOrderNumber", slot_type_ids=ids,
                        slot_type_docs=docs, slot_plans=plans))
    slot = slot_of(fixed, "orderNumber")
    assert slot["type"] == "NLX.AlphaNumeric"
    assert slot["regex"] == "^[0-9]{10}$"
    assert any("orderNumber" in n and "S1" in n for n in notes)
    assert not [p for p in runtime_contract_violations(
        fixed, **context("DeliveryStatusByOrderNumber", slot_type_ids=ids,
                         slot_type_docs=docs, slot_plans=plans), scope="cross")
        if "S1" in p]
    # Without the plan the same document stays a violation — never a guess.
    problems = runtime_contract_violations(
        flow, **context("DeliveryStatusByOrderNumber", slot_type_ids=ids,
                        slot_type_docs=docs), scope="cross")
    assert any("S1" in p and "orderNumber" in p for p in problems)


def test_s1_text_alias_with_a_plan_regex_becomes_alphanumeric_with_the_regex():
    """`text` + the plan's digit regex is an identifier, not free text: the
    live shape is NLX.AlphaNumeric + regex. A plan regex also wins over a bare
    `number` (NLX.Number would drop leading zeros)."""
    flow = broken("DeliveryStatusByOrderNumber")
    slot_of(flow, "orderNumber")["type"] = "text"
    slot_of(flow, "orderNumber").pop("regex", None)
    plans = {"orderNumber": {"name": "orderNumber", "type": "text", "regex": "^[0-9]{10}$"}}
    fixed, _ = apply_runtime_contract(
        flow, **context("DeliveryStatusByOrderNumber", slot_plans=plans))
    assert slot_of(fixed, "orderNumber")["type"] == "NLX.AlphaNumeric"
    assert slot_of(fixed, "orderNumber")["regex"] == "^[0-9]{10}$"

    flow = broken("DeliveryStatusByOrderNumber")
    slot_of(flow, "orderNumber")["type"] = "number"
    slot_of(flow, "orderNumber").pop("regex", None)
    fixed, _ = apply_runtime_contract(
        flow, **context("DeliveryStatusByOrderNumber", slot_plans=plans))
    assert slot_of(fixed, "orderNumber")["type"] == "NLX.AlphaNumeric"

    # A quantity with no format stays a number.
    flow = broken("DeliveryStatusByOrderNumber")
    slot_of(flow, "orderNumber")["type"] = "number"
    slot_of(flow, "orderNumber").pop("regex", None)
    fixed, _ = apply_runtime_contract(
        flow, **context("DeliveryStatusByOrderNumber",
                        slot_plans={"orderNumber": {"name": "orderNumber", "type": "number"}}))
    assert slot_of(fixed, "orderNumber")["type"] == "NLX.Number"


# ---------------------------------------------------------------------------
# S5 — a one-item custom slot type auto-selects without asking
# ---------------------------------------------------------------------------

def test_s5_open_value_slot_type_becomes_alphanumeric_with_its_regex():
    flow, notes = normalized("DeliveryStatusByOrderNumber")
    slot = slot_of(flow, "orderNumber")
    assert slot["type"] == "NLX.AlphaNumeric"
    assert slot["regex"] == "^[0-9]{10}$"
    assert slot["name"] == "orderNumber", "the slot NAME is part of the contract"
    assert any("S5" in note for note in notes)


def test_s5_phone_like_open_value_becomes_the_phone_number_builtin():
    flow, _ = normalized("SearchOrderByCustomerInfo")
    slot = slot_of(flow, "phoneNumber")
    assert slot["type"] == "NLX.PhoneNumber"
    assert "regex" not in slot, "the built-in owns the format"


def test_s5_leaves_a_real_enumerated_menu_alone():
    """productType has four values and no constraints: a genuine menu."""
    flow, _ = normalized("GetCleaningPrice")
    assert slot_of(flow, "productType")["type"] == "productType"
    assert slot_of(flow, "serviceType")["type"] == "serviceType"


def test_s5_builds_a_digit_regex_from_length_constraints_alone():
    flow = broken("DeliveryStatusByOrderNumber")
    docs = copy.deepcopy(LIVE["slot_types"])
    docs["orderNumber"]["metadata"]["constraints"] = {"min_length": 8, "max_length": 12}
    out, _ = apply_runtime_contract(
        flow, **context("DeliveryStatusByOrderNumber", slot_type_docs=docs))
    slot = slot_of(out, "orderNumber")
    assert slot["type"] == "NLX.AlphaNumeric"
    assert slot["regex"] == "^[0-9]{8,12}$"


# ---------------------------------------------------------------------------
# S2 — metadata.choice.slotTypeId is stored verbatim as the slot id
# ---------------------------------------------------------------------------

def test_s2_choice_slot_type_id_becomes_the_attached_slot_name():
    flow, notes = normalized("SearchOrderByCustomerInfo")
    referenced = [(n["metadata"]["choice"]["slotTypeId"], n["metadata"]["choice"]["source"])
                  for n in nodes_of(flow, "user_choice")]
    assert referenced == [("customerName", "slotType"), ("phoneNumber", "slotType"),
                          ("address", "slotType")], "two `text` slots resolve in node order"
    assert any("S2" in note for note in notes)


def test_s2_resolves_every_capture_node_in_a_six_slot_flow():
    flow, _ = normalized("CreateCleaningReservation")
    names = {s["name"] for s in flow["slotTypes"]}
    for node in nodes_of(flow, "user_choice"):
        assert node["metadata"]["choice"]["slotTypeId"] in names


def test_s2_unresolvable_reference_is_a_violation():
    flow = broken("CreateCleaningReservation")
    for node in flow["nodes"].values():
        if node.get("type") == "user_choice":
            node["metadata"]["choice"]["slotTypeId"] = "somethingElse"
    problems = runtime_contract_violations(flow, **context("CreateCleaningReservation"))
    assert any("S2" in p and "somethingElse" in p for p in problems)


# ---------------------------------------------------------------------------
# S3 — capture edges
# ---------------------------------------------------------------------------

def test_s3_user_choice_edges_test_the_slot_not_the_captured_flow():
    flow, _ = normalized("DeliveryStatusByOrderNumber")
    ask = next(n for n in nodes_of(flow, "user_choice"))
    assert edge_named(ask, "captured")["conditions"] == [
        {"left": {"type": "slot", "name": "orderNumber"}, "operator": "exists"}]
    assert edge_named(ask, "notCaptured")["conditions"] == [
        {"left": {"type": "slot", "name": "orderNumber"}, "operator": "not_exists"}]


def test_s3_user_input_edges_keep_captured_flow():
    """user_input captures an INTENT; only user_choice captures a value."""
    flow = broken("GetCleaningPrice")
    listen = {
        "nodeId": "b0000000-0000-4000-8000-000000000001", "type": "user_input",
        "childNodes": [
            {"nodeId": "b0000000-0000-4000-8000-000000000001", "name": "captured",
             "conditions": [{"left": {"type": "slot", "name": "productType"},
                             "operator": "exists"}]}],
    }
    # Wire it in behind the start node — an unreachable node is pruned.
    start = next(n for n in flow["nodes"].values() if n["type"] == "start")
    first = start["childNodes"][0]["nodeId"]
    listen["childNodes"].append({"nodeId": first, "name": "next"})
    start["childNodes"][0]["nodeId"] = listen["nodeId"]
    flow["nodes"][listen["nodeId"]] = listen
    out, _ = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    condition = out["nodes"][listen["nodeId"]]["childNodes"][0]["conditions"][0]
    assert condition == {"left": {"type": "captured_flow"}, "operator": "exists"}


# ---------------------------------------------------------------------------
# S6 — slot values persist for the session; clear on the way OUT
# ---------------------------------------------------------------------------

def test_s6_every_leaving_node_clears_the_flow_slots():
    flow, _ = normalized("CreateCleaningReservation")
    slots = [s["name"] for s in flow["slotTypes"]]
    for node in nodes_of(flow, "escalate") + nodes_of(flow, "redirect"):
        assert clears(node) == slots, node["nodeId"]


def test_s6_keeps_the_context_modification_it_found_there():
    flow, _ = normalized("DeliveryStatusByOrderNumber")
    escalate = nodes_of(flow, "escalate")[0]
    modifications = escalate["metadata"]["stateModifications"]
    assert modifications[0] == {"type": "context", "name": "failReason",
                                "modification": "set",
                                "value": {"type": "constant",
                                          "value": "delivery_lookup_system_error"}}
    assert clears(escalate) == ["orderNumber"]


def test_s6_never_clears_at_an_operation_flow_start():
    """Recognition fills slots from the routing utterance (zero-turn)."""
    flow = broken("DeliveryStatusByOrderNumber")
    start = next(n for n in flow["nodes"].values() if n["type"] == "start")
    first = start["childNodes"][0]["nodeId"]
    clear_id = "c1ea0000-0000-4000-8000-000000000001"
    flow["nodes"][clear_id] = {
        "nodeId": clear_id, "type": "basic",
        "metadata": {"stateModifications": [
            {"type": "slot", "name": "orderNumber", "modification": "clear"}]},
        "childNodes": [{"nodeId": first, "name": "next"}]}
    start["childNodes"][0]["nodeId"] = clear_id
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    assert clear_id not in out["nodes"]
    assert out["nodes"][next(n["nodeId"] for n in out["nodes"].values()
                             if n["type"] == "start")]["childNodes"][0]["nodeId"] == first
    assert any("slot-clear node" in note for note in notes)


def test_s6_leaves_a_follow_up_flow_free_to_clear_at_start():
    flow = broken("DeliveryStatusByOrderNumber")
    flow["flowId"] = "FollowUpFlow"
    start = next(n for n in flow["nodes"].values() if n["type"] == "start")
    first = start["childNodes"][0]["nodeId"]
    clear_id = "c1ea0000-0000-4000-8000-000000000002"
    flow["nodes"][clear_id] = {
        "nodeId": clear_id, "type": "basic",
        "metadata": {"stateModifications": [
            {"type": "slot", "name": "orderNumber", "modification": "clear"}]},
        "childNodes": [{"nodeId": first, "name": "next"}]}
    start["childNodes"][0]["nodeId"] = clear_id
    out, _ = apply_runtime_contract(flow, **context("FollowUpFlow"))
    assert clear_id in out["nodes"]


# ---------------------------------------------------------------------------
# R6 — retry
# ---------------------------------------------------------------------------

def test_r6_self_loop_becomes_a_recovery_basic_that_clears_the_slot():
    """A user_choice revisited in the same turn never re-asks; it drops to Fallback."""
    flow, notes = normalized("GetCleaningPrice")
    asks = nodes_of(flow, "user_choice")
    assert len(asks) == 2, "the retry must NOT be a second capture node"
    for ask in asks:
        slot = ask["metadata"]["choice"]["slotTypeId"]
        recovery = flow["nodes"][past_agent_gate(flow, edge_named(ask, "notCaptured")["nodeId"])]
        assert recovery["type"] == "basic"
        assert clears(recovery) == [slot]
        assert recovery["messages"][0]["body"] == (
            "죄송합니다, 확인하지 못했습니다. 다시 한 번 말씀해 주세요.")
        assert [e["nodeId"] for e in recovery["childNodes"]] == [ask["nodeId"]]
    assert sum("R6" in note for note in notes) == 2


def test_r6_reuses_the_flows_own_retry_wording_and_node():
    flow, _ = normalized("DeliveryStatusByOrderNumber")
    ask = nodes_of(flow, "user_choice")[0]
    recovery = flow["nodes"][past_agent_gate(flow, edge_named(ask, "notCaptured")["nodeId"])]
    assert recovery["type"] == "basic"
    assert "10자리 숫자로 된 주문번호를 다시 말씀해 주세요" in recovery["messages"][0]["body"]
    assert clears(recovery) == ["orderNumber"]


def test_r6_english_flow_gets_english_recovery_wording():
    flow = broken("GetCleaningPrice")
    for node in flow["nodes"].values():
        for message in node.get("messages") or []:
            message["body"] = "Which product type should I price?"
    flow["mainLanguageCode"] = "en-US"
    out, _ = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    ask = nodes_of(out, "user_choice")[0]
    recovery = out["nodes"][past_agent_gate(out, edge_named(ask, "notCaptured")["nodeId"])]
    assert recovery["messages"][0]["body"] == (
        "Sorry, I could not catch that. Please say it again.")


def test_r6_folds_a_second_capture_node_for_the_same_slot():
    """The cycle-7 shape: a retry as a second user_choice fires slot_no_match."""
    flow = broken("DeliveryStatusByOrderNumber")
    ask = next(n for n in flow["nodes"].values() if n["type"] == "user_choice")
    retry_id = "4e7a0000-0000-4000-8000-000000000009"
    flow["nodes"][retry_id] = {
        "nodeId": retry_id, "type": "user_choice",
        "messages": [{"type": "text", "body": "주문번호를 다시 말씀해 주세요."}],
        "metadata": {"choice": {"source": "slotType", "slotTypeId": "orderNumber"}},
        "childNodes": [
            {"nodeId": edge_named(ask, "captured")["nodeId"], "name": "captured"},
            {"nodeId": edge_named(ask, "notCaptured")["nodeId"], "name": "notCaptured"}],
    }
    edge_named(ask, "notCaptured")["nodeId"] = retry_id
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    folded = out["nodes"][retry_id]
    assert folded["type"] == "basic"
    assert folded["messages"][0]["body"] == "주문번호를 다시 말씀해 주세요."
    assert clears(folded) == ["orderNumber"]
    assert [e["nodeId"] for e in folded["childNodes"]] == [ask["nodeId"]]
    assert any("second capture node" in note for note in notes)


# ---------------------------------------------------------------------------
# R7 — escalate is terminal
# ---------------------------------------------------------------------------

def test_r7_escalate_loses_its_children_and_the_orphaned_end_goes():
    """escalate → end made Connect report Success instead of Escalation."""
    flow, notes = normalized("EscalationFlow", role="escalation")
    escalate = nodes_of(flow, "escalate")[0]
    assert not escalate.get("childNodes")
    assert nodes_of(flow, "end") == [], "the end node is unreachable now"
    assert any("R7" in note for note in notes)


def test_r7_keeps_an_end_node_another_branch_still_reaches():
    flow, _ = normalized("DeliveryStatusByOrderNumber")
    assert not nodes_of(flow, "escalate")[0].get("childNodes")
    assert len(nodes_of(flow, "end")) == 1


# ---------------------------------------------------------------------------
# R3 — an operation flow hands back, it never ends the session
# ---------------------------------------------------------------------------

def test_r3_success_paths_go_through_one_follow_up_redirect():
    flow, notes = normalized("GetCleaningPrice")
    handback = redirect_to(flow, "FollowUpFlow")
    assert handback["metadata"]["redirect"]["type"] == "flow"
    assert clears(handback) == ["productType", "serviceType"]
    end_id = nodes_of(flow, "end")[0]["nodeId"]
    assert [e["nodeId"] for e in handback["childNodes"]] == [end_id]
    into_end = [n for n in flow["nodes"].values()
                if any(e["nodeId"] == end_id for e in n.get("childNodes") or [])]
    assert handback["nodeId"] in [n["nodeId"] for n in into_end]
    assert all(n["type"] == "redirect" for n in into_end), "only a redirect out of the flow may end it"
    assert any("R3" in note for note in notes)


def test_r3_leaves_a_system_flow_free_to_end_the_session():
    flow, _ = normalized("FallbackFlow", role="fallback")
    assert not [n for n in nodes_of(flow, "redirect")
                if n["metadata"]["redirect"].get("flowId") == "FollowUpFlow"]


# ---------------------------------------------------------------------------
# D3 / D4 — data requests
# ---------------------------------------------------------------------------

def test_d3_payload_maps_every_request_field_to_a_slot():
    flow, notes = normalized("CreateCleaningReservation")
    payloads = {entry["dataRequestId"]: entry["payload"]
                for node in nodes_of(flow, "data_request")
                for entry in node["dataRequests"]}
    assert payloads["getCleaningPrice"] == {
        "productType": "{productType:NLX.Slot}",
        "serviceType": "{serviceType:NLX.Slot}"}
    reservation = payloads["createCleaningReservation"]
    assert reservation == {name: f"{{{name}:NLX.Slot}}" for name in (
        "privacyConsent", "installLocationType", "productType", "quantity",
        "serviceType", "requestedDate")}
    assert any("D3" in note for note in notes)


def test_d3_reports_the_real_gap_in_the_live_reservation_flow():
    """Two findings the live run got away with only because the backend was a mock.

    * createCleaningReservation's requestSchema *requires* customerName,
      phoneNumber and address; the reservation flow asks for none of them, so
      the call posted a reservation with no customer on it.
    * the quote message reads ``{getCleaningPrice.totalAmount:NLX.Variable}``,
      but getCleaningPrice returns unitPrice — totalAmount is a define/context
      variable of the flow's own, so the placeholder rendered as nothing
      ("예상 총액은 원입니다").

    Neither is repairable without inventing a question or guessing an
    arithmetic, so both are reported rather than patched.
    """
    flow, _ = normalized("CreateCleaningReservation")
    problems = runtime_contract_violations(flow, **context("CreateCleaningReservation"))
    missing = {p.split("requires field ")[1].split(" that ")[0]
               for p in problems if "requires field" in p}
    assert missing == {"customerName", "phoneNumber", "address"}
    assert sum("totalAmount" in p and p.startswith("M1:") for p in problems) == 2
    assert len(problems) == 5, problems


def test_d3_maps_a_context_variable_when_no_slot_has_the_name():
    flow = broken("GetCleaningPrice")
    requests = copy.deepcopy(LIVE["data_requests"])
    schema = requests["getCleaningPrice"]["requestSchema"]
    schema["properties"]["customerPhone"] = {"type": "string"}
    out, _ = apply_runtime_contract(
        flow, **context("GetCleaningPrice", data_requests=requests))
    payload = nodes_of(out, "data_request")[0]["dataRequests"][0]["payload"]
    assert payload["customerPhone"] == "{customerPhone:NLX.Context}"


def test_d3_required_field_the_flow_never_collects_is_a_violation():
    flow = broken("GetCleaningPrice")
    requests = copy.deepcopy(LIVE["data_requests"])
    schema = requests["getCleaningPrice"]["requestSchema"]
    schema["properties"]["storeCode"] = {"type": "string"}
    schema["required"].append("storeCode")
    problems = runtime_contract_violations(
        flow, **context("GetCleaningPrice", data_requests=requests), scope="cross")
    assert any("requires field storeCode that flow GetCleaningPrice never collects" in p
               for p in problems)


def test_d3_optional_unmapped_field_is_only_a_note():
    flow = broken("GetCleaningPrice")
    requests = copy.deepcopy(LIVE["data_requests"])
    requests["getCleaningPrice"]["requestSchema"]["properties"]["couponCode"] = {
        "type": "string"}
    out, notes = apply_runtime_contract(
        flow, **context("GetCleaningPrice", data_requests=requests))
    assert any("couponCode" in note for note in notes)
    assert runtime_contract_violations(
        out, **context("GetCleaningPrice", data_requests=requests)) == []


def test_d4_error_status_becomes_failure():
    """`error` is not a node_status: the edge matched nothing → Fallback."""
    flow, notes = normalized("DeliveryStatusByOrderNumber")
    request_node = nodes_of(flow, "data_request")[0]
    statuses = {edge["conditions"][0]["right"]["value"]
                for edge in request_node["childNodes"]}
    assert statuses == {"success", "failure"}
    assert any("D4" in note for note in notes)


def test_d4_missing_failure_edge_is_wired_to_the_escalation_node():
    flow = broken("DeliveryStatusByOrderNumber")
    request_node = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    request_node["childNodes"] = [e for e in request_node["childNodes"]
                                  if e["name"] != "error"]
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    fixed = nodes_of(out, "data_request")[0]
    failure = edge_named(fixed, "failure")
    assert out["nodes"][failure["nodeId"]]["type"] == "escalate"
    assert failure["conditions"] == [{"left": {"type": "node_status"},
                                      "operator": "eq",
                                      "right": {"type": "constant",
                                                "value": "failure"}}]
    assert any("D4" in note for note in notes)


def test_d4_missing_failure_edge_with_nowhere_to_go_is_a_violation():
    flow = broken("GetCleaningPrice")
    request_node = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    request_node["childNodes"] = [e for e in request_node["childNodes"]
                                  if e["name"] != "error"]
    problems = runtime_contract_violations(flow, scope="flow")
    assert any("D4" in p and "failure" in p for p in problems)


def test_d4_ignores_a_node_that_does_no_status_routing_at_all():
    """Statuses missing entirely is an earlier defect, repaired elsewhere."""
    flow = broken("GetCleaningPrice")
    request_node = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    for edge in request_node["childNodes"]:
        edge.pop("conditions", None)
    assert not [p for p in runtime_contract_violations(flow, scope="flow") if "D4" in p]


# ---------------------------------------------------------------------------
# M1 / M2 — messages
# ---------------------------------------------------------------------------

def test_m1_rewrites_a_close_response_schema_field():
    """The live miss: the message said `price`, the schema says `unitPrice`."""
    flow, notes = normalized("GetCleaningPrice")
    assert any("{getCleaningPrice.unitPrice:NLX.Variable}" in body for body in bodies(flow))
    assert not any("getCleaningPrice.price:" in body for body in bodies(flow))
    assert any("M1" in note for note in notes)


def test_m1_unknown_response_field_is_a_violation():
    flow = broken("GetCleaningPrice")
    for node in flow["nodes"].values():
        for message in node.get("messages") or []:
            message["body"] = message["body"].replace(
                "{getCleaningPrice.price:NLX.Variable}",
                "{getCleaningPrice.discountRate:NLX.Variable}")
    problems = runtime_contract_violations(
        flow, **context("GetCleaningPrice"), scope="cross")
    assert any("M1" in p and "discountRate" in p for p in problems)


def test_m1_slot_placeholder_must_name_an_attached_slot():
    flow = broken("GetCleaningPrice")
    node = next(n for n in flow["nodes"].values() if n.get("messages"))
    node["messages"][0]["body"] += " {orderNumber:NLX.Slot}"
    problems = runtime_contract_violations(
        flow, **context("GetCleaningPrice"), scope="cross")
    assert any("M1" in p and "orderNumber" in p for p in problems)


def m2_fallback_body(out, generative_id):
    """Live (2026-09-17): the confirmed generative_text is KEPT and a `failure`
    edge (Error IntegrationNotFound → node_status failure, measured) leads to
    the templated basic that then speaks. Returns that basic's message."""
    node = out["nodes"][generative_id]
    assert node["type"] == "generative_text"
    failure = next(e for e in node["childNodes"] if any(
        (c.get("left") or {}).get("type") == "node_status" and (c.get("right") or {}).get("value") == "failure"
        for c in e.get("conditions") or []))
    return out["nodes"][failure["nodeId"]]["messages"][0]["body"]


def test_m2_generative_text_is_kept_with_a_templated_failure_fallback():
    """A workspace without a default model answers IntegrationNotFound; the
    templated sentence rides on the node's failure edge, the generative wording
    the user confirmed stays for a workspace that has one."""
    flow, notes = normalized("DeliveryStatusByOrderNumber")
    generative = nodes_of(flow, "generative_text")
    assert len(generative) == 1
    answer = m2_fallback_body(flow, generative[0]["nodeId"])
    # a prompt that already names its placeholders is left as the user approved it
    assert "다음 값만 사용해" not in flow["nodes"][generative[0]["nodeId"]]["metadata"]["generativeText"]["prompt"]
    assert answer == (
        "조회 결과: 주문번호 {orderNumber:NLX.Slot}, "
        "배송 상태 {getDeliveryStatusByOrderNumber.deliveryStatus:NLX.Variable}, "
        "예상 배송일 "
        "{getDeliveryStatusByOrderNumber.expectedDeliveryDate:NLX.Variable}입니다.")
    assert any("M2" in note for note in notes)


def test_m2_leaves_a_generative_text_a_message_already_follows():
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values()
                      if n["type"] == "generative_text")
    speaker = "b0000000-0000-4000-8000-000000000001"
    flow["nodes"][speaker] = {
        "nodeId": speaker, "type": "basic",
        "messages": [{"type": "text", "body": "안내를 마쳤습니다."}],
        "childNodes": [{"nodeId": generative["childNodes"][0]["nodeId"], "name": "next"}]}
    generative["childNodes"] = [{"nodeId": speaker, "name": "next"}]
    out, _ = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    assert out["nodes"][generative["nodeId"]]["type"] == "generative_text"


def test_m2_without_placeholders_announces_the_data_request_result_fields():
    """Live (GAON v3, 2026-09-14): '성공 시 배송상태와 예정일을 자연스럽게 안내한다' had
    no placeholders and the generative node said nothing — the caller heard
    "anything else?" right after the order number. The nearest upstream data
    request's result fields make the announcement, labelled from the interview."""
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values()
                      if n["type"] == "generative_text")
    generative["metadata"]["generativeText"]["prompt"] = "친절하게 안내하세요."
    kwargs = context("DeliveryStatusByOrderNumber")
    request_id = "getDeliveryStatusByOrderNumber"
    assert request_id in kwargs["data_requests"]
    kwargs["field_labels"] = {request_id: {"deliveryStatus": "배송 상태"}}
    out, notes = apply_runtime_contract(flow, **kwargs)
    body = m2_fallback_body(out, generative["nodeId"])
    assert body.startswith("조회 결과: ") and f"{{{request_id}.deliveryStatus:NLX.Variable}}" in body
    assert "배송 상태 {" in body            # the interview's label, not the field name
    assert "success" not in body           # envelope fields are not announced
    assert any("M2)" in n for n in notes)
    assert not [p for p in runtime_contract_violations(flow, **kwargs, scope="flow") if "M2" in p]


def test_m2_without_any_result_fields_is_reported_to_the_generator_only():
    """No placeholders and no upstream data request: still a normalizer-scope violation."""
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values()
                      if n["type"] == "generative_text")
    generative["metadata"]["generativeText"]["prompt"] = "친절하게 안내하세요."
    kwargs = context("DeliveryStatusByOrderNumber")
    kwargs["data_requests"] = {}
    assert any("M2" in p for p in runtime_contract_violations(flow, **kwargs, scope="normalizer"))


# ---------------------------------------------------------------------------
# RX / J / A2
# ---------------------------------------------------------------------------

def test_rx_near_miss_redirect_target_is_corrected():
    flow, notes = normalized("DeliveryStatusByOrderNumber")
    assert redirect_to(flow, "SearchOrderByCustomerInfo")
    assert any("RX" in note for note in notes)


def test_rx_unknown_redirect_target_is_a_violation():
    flow = broken("DeliveryStatusByOrderNumber")
    for node in flow["nodes"].values():
        redirect = (node.get("metadata") or {}).get("redirect")
        if redirect:
            redirect["flowId"] = "TotallyDifferentFlow"
    problems = runtime_contract_violations(
        flow, **context("DeliveryStatusByOrderNumber"), scope="cross")
    assert any("RX" in p and "TotallyDifferentFlow" in p for p in problems)


def test_rx_accepts_the_system_flow_placeholder():
    flow = broken("DeliveryStatusByOrderNumber")
    for node in flow["nodes"].values():
        redirect = (node.get("metadata") or {}).get("redirect")
        if redirect:
            redirect["flowId"] = "{System.capturedFlow:NLX.System}"
    assert not [p for p in runtime_contract_violations(
        flow, **context("DeliveryStatusByOrderNumber")) if "RX" in p]


def test_j_unconditioned_journey_edge_gets_an_exit_condition():
    flow, notes = normalized("WelcomeFlow", role="welcome")
    journey = nodes_of(flow, "generative_journey")[0]
    assert edge_named(journey, "done")["conditions"] == [{
        "left": {"type": "system", "name": "System.gjConditionIndex"},
        "operator": "eq", "right": {"type": "constant", "value": 0}}]
    assert any("J" in note for note in notes)


def test_j_missing_timeout_and_failure_branches_are_added_or_reported():
    flow, _ = normalized("WelcomeFlow", role="welcome")
    problems = runtime_contract_violations(
        broken("WelcomeFlow"), **context("WelcomeFlow"), scope="flow")
    assert sum("J" in p for p in problems) == 2, "no escalation node to point at"

    with_escalation = broken("WelcomeFlow")
    escalate_id = "e5ca0000-0000-4000-8000-000000000001"
    with_escalation["nodes"][escalate_id] = {"nodeId": escalate_id, "type": "escalate"}
    journey = next(n for n in with_escalation["nodes"].values()
                   if n["type"] == "generative_journey")
    journey["childNodes"].append({"nodeId": escalate_id, "name": "toAgent"})
    out, _ = apply_runtime_contract(with_escalation, **context("WelcomeFlow"))
    fixed = nodes_of(out, "generative_journey")[0]
    assert edge_named(fixed, "timeout")["nodeId"] == escalate_id
    assert edge_named(fixed, "failure")["nodeId"] == escalate_id


def test_a2_non_ascii_routing_metadata_is_a_violation_not_a_rewrite():
    flow = broken("DeliveryStatusByOrderNumber")
    flow["aiDescription"] = "고객이 배송 상태를 물어볼 때 사용하세요."
    problems = runtime_contract_violations(flow, scope="flow")
    assert any("A2" in p and "aiDescription" in p for p in problems)
    out, _ = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    assert out["aiDescription"] == flow["aiDescription"], "never translate"


def test_a2_covers_attached_slot_descriptions():
    flow = broken("GetCleaningPrice")
    flow["slotTypes"][0]["aiDescription"] = "세척할 제품 유형"
    assert any("A2" in p and "productType" in p
               for p in runtime_contract_violations(flow, scope="flow"))


# ---------------------------------------------------------------------------
# properties the module promises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_normalization_is_idempotent(flow_id):
    once, _ = normalized(flow_id)
    twice, notes = apply_runtime_contract(
        copy.deepcopy(once), **context(flow_id))
    assert twice == once
    assert notes == []


@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_normalization_is_deterministic(flow_id):
    first, first_notes = normalized(flow_id)
    second, second_notes = normalized(flow_id)
    assert first == second
    assert first_notes == second_notes


@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_the_input_document_is_never_mutated(flow_id):
    original = broken(flow_id)
    snapshot = copy.deepcopy(original)
    apply_runtime_contract(original, **context(flow_id))
    runtime_contract_violations(original, **context(flow_id))
    assert original == snapshot


@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_normalized_flows_still_satisfy_the_contract_schema(flow_id):
    flow, _ = normalized(flow_id)
    assert validate_acxd_asset("flow", flow, runtime_contract=False) == []


@pytest.mark.parametrize("flow_id", [f for f in OPERATION_FLOWS
                                     if f != "CreateCleaningReservation"])
def test_the_gate_reports_only_what_the_normalizer_refused_to_fix(flow_id):
    flow, _ = normalized(flow_id)
    assert runtime_contract_violations(flow, **context(flow_id)) == []


@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_normalized_flows_have_no_dangling_or_unreachable_nodes(flow_id):
    flow, _ = normalized(flow_id)
    nodes = flow["nodes"]
    reachable, stack = set(), [next(n["nodeId"] for n in nodes.values()
                                   if n["type"] == "start")]
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        for edge in nodes[node_id].get("childNodes") or []:
            assert edge["nodeId"] in nodes, f"{node_id} → {edge['nodeId']}"
            stack.append(edge["nodeId"])
    assert set(nodes) == reachable


def test_scope_split_matches_the_two_gates():
    """The flow gate must not need bundle context, and the two must not overlap."""
    assert FLOW_SCOPE_RULES & CROSS_SCOPE_RULES == {"S1"}, (
        "only S1 is split: vocabulary is flow-local, membership needs the bundle")
    for flow_id in LIVE["flows_broken"]:
        flow = broken(flow_id)
        flow_scope = set(runtime_contract_violations(flow, scope="flow"))
        cross_scope = set(runtime_contract_violations(
            flow, **context(flow_id), scope="cross"))
        assert not flow_scope & cross_scope


@pytest.mark.parametrize("flow_id", sorted(LIVE["flows_broken"]))
def test_every_violation_is_tagged_with_its_rules_scope(flow_id):
    """Guards against a new rule being emitted under the wrong gate."""
    scopes = {"flow": FLOW_SCOPE_RULES, "cross": CROSS_SCOPE_RULES,
              "normalizer": NORMALIZER_SCOPE_RULES}
    seen = set()
    for scope, allowed in scopes.items():
        for message in runtime_contract_violations(
                broken(flow_id), **context(flow_id), scope=scope):
            rule = message.split(":", 1)[0]
            assert rule in allowed, f"{rule} reported under scope {scope!r}"
            seen.add(message)
    assert seen == set(runtime_contract_violations(
        broken(flow_id), **context(flow_id))), "scope='all' must be the union"


def test_flow_scope_gate_is_wired_into_validate_acxd_asset():
    flow = broken("GetCleaningPrice")
    flow["slotTypes"][0]["type"] = "NLX.Boolean"
    errors = validate_acxd_asset("flow", flow)
    assert any("runtime contract" in e and "NLX.Boolean" in e for e in errors)
    assert validate_acxd_asset("flow", flow, runtime_contract=False) == []


def test_cross_scope_gate_is_wired_into_validate_acxd_consistency():
    from tools.validate_acxd_consistency import validate_acxd_consistency

    flow = broken("GetCleaningPrice")
    # A response field no close match can rescue: only the bundle's data
    # requests reveal that it does not exist, so only this gate can catch it.
    for node in flow["nodes"].values():
        for message in node.get("messages") or []:
            message["body"] = message["body"].replace(
                "{getCleaningPrice.price:NLX.Variable}",
                "{getCleaningPrice.discountRate:NLX.Variable}")
    bundle = {
        "flows": [flow],
        "slot_types": list(LIVE["slot_types"].values()),
        "data_requests": list(LIVE["data_requests"].values()),
    }
    runtime = [v for v in validate_acxd_consistency(bundle)
               if v.code == "RUNTIME_CONTRACT"]
    assert runtime, "the cross-asset half must run where the bundle is known"
    assert "discountRate" in runtime[0].message
    assert runtime[0].path == "flows[0]"

    fixed, _ = apply_runtime_contract(broken("GetCleaningPrice"),
                                      **context("GetCleaningPrice"))
    bundle["flows"] = [fixed]
    assert not [v for v in validate_acxd_consistency(bundle)
                if v.code == "RUNTIME_CONTRACT"]


def test_nodes_the_model_left_dangling_are_pruned_not_reported():
    """Live (GreenCart RequestReturn, five attempts): the model emitted two
    nodes nothing pointed at; the flow gate refused the flow every time for
    FLOW_UNREACHABLE_NODE and the attempts were spent on a repair the
    normalizer can make safely — an unreachable node never executes. Edges that
    point at a wrong id are a different defect and stay for the gate."""
    flow = broken("GetCleaningPrice")
    flow["nodes"]["dead0001-0000-4000-8000-000000000001"] = {
        "nodeId": "dead0001-0000-4000-8000-000000000001", "type": "basic",
        "messages": [{"body": "never shown"}],
        "childNodes": [{"nodeId": "dead0002-0000-4000-8000-000000000002", "name": "next"}]}
    flow["nodes"]["dead0002-0000-4000-8000-000000000002"] = {
        "nodeId": "dead0002-0000-4000-8000-000000000002", "type": "end"}
    fixed, notes = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    assert "dead0001-0000-4000-8000-000000000001" not in fixed["nodes"]
    assert "dead0002-0000-4000-8000-000000000002" not in fixed["nodes"]
    assert any("dangling" in n for n in notes)
    assert validate_acxd_asset("flow", fixed) == []


def test_nothing_is_pruned_without_a_start_node():
    flow = broken("GetCleaningPrice")
    start_id = next(i for i, n in flow["nodes"].items() if n["type"] == "start")
    del flow["nodes"][start_id]
    fixed, _ = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    assert set(flow["nodes"]) <= set(fixed["nodes"])


def test_s8_yes_no_constants_become_the_slot_types_values():
    """Live (GAON, 2026-09-13): `privacyConsent eq "yes"` against the yesNo type
    whose values are 예/아니요 — the customer's "네" was treated as a refusal."""
    flow = broken("CreateCleaningReservation")
    flow["slotTypes"] = [s for s in flow["slotTypes"] if s["name"] != "privacyConsent"] + [
        {"name": "privacyConsent", "type": YES_NO_SLOT_TYPE}]
    flow["nodes"]["gate"] = {"nodeId": "gate", "type": "choice", "childNodes": [
        {"nodeId": "go", "name": "agreed", "conditions": [
            {"left": {"type": "slot", "name": "privacyConsent"}, "operator": "eq",
             "right": {"type": "constant", "value": "yes"}}]},
        {"nodeId": "stop", "name": "declined", "conditions": [
            {"left": {"type": "slot", "name": "privacyConsent"}, "operator": "neq",
             "right": {"type": "constant", "value": "yes"}}]},
        {"nodeId": "odd", "name": "odd", "conditions": [
            {"left": {"type": "slot", "name": "privacyConsent"}, "operator": "eq",
             "right": {"type": "constant", "value": "maybe"}}]},
    ]}
    flow["nodes"]["go"] = {"nodeId": "go", "type": "end"}
    flow["nodes"]["stop"] = {"nodeId": "stop", "type": "end"}
    flow["nodes"]["odd"] = {"nodeId": "odd", "type": "end"}
    start = next(n for n in flow["nodes"].values() if n.get("type") == "start")
    start["childNodes"] = [{"nodeId": "gate", "name": "next"}]
    ctx = context("CreateCleaningReservation")
    yes_no = ctx["slot_type_docs"][YES_NO_SLOT_TYPE]
    yes_value = yes_no["values"][0]["value"]
    out, notes = apply_runtime_contract(flow, **ctx)
    edges = {e["name"]: e for e in out["nodes"]["gate"]["childNodes"]}
    assert edges["agreed"]["conditions"][0]["right"]["value"] == yes_value
    assert edges["declined"]["conditions"][0]["right"]["value"] == yes_value
    assert edges["odd"]["conditions"][0]["right"]["value"] == "maybe"  # unknown: reported, not guessed
    assert any("(S8)" in n for n in notes)
    from tools.acxd_runtime_contract import runtime_contract_violations
    assert any(v.rule == "S8" if hasattr(v, "rule") else "S8" in str(v)
               for v in runtime_contract_violations(out, **ctx))


def test_m3_a_message_node_is_only_a_message():
    """Live (GAON, 2026-09-13): the price announcement 'basic' carried
    metadata.redirect and cleared the slots its own message rendered; the
    runtime showed the fallback re-guide instead of the price."""
    flow = broken("GetCleaningPrice")
    start = next(n for n in flow["nodes"].values() if n.get("type") == "start")
    flow["nodes"]["say"] = {"nodeId": "say", "type": "basic",
                            "messages": [{"type": "text", "body": "{productType:NLX.Slot} 단가는 100원입니다."}],
                            "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"},
                                         "stateModifications": [
                                             {"type": "slot", "name": "productType", "modification": "clear"},
                                             {"type": "slot", "name": "serviceType", "modification": "clear"}]},
                            "childNodes": [{"nodeId": "go", "name": "next"}]}
    flow["nodes"]["go"] = {"nodeId": "go", "type": "redirect",
                           "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"}},
                           "childNodes": [{"nodeId": "fin", "name": "next"}]}
    flow["nodes"]["fin"] = {"nodeId": "fin", "type": "end"}
    start["childNodes"] = [{"nodeId": "say", "name": "next"}]
    out, notes = apply_runtime_contract(flow, **context("GetCleaningPrice"))
    say = out["nodes"]["say"]
    assert "redirect" not in (say.get("metadata") or {})
    mods = (say.get("metadata") or {}).get("stateModifications") or []
    assert [m["name"] for m in mods] == ["serviceType"]  # the rendered slot keeps its value
    assert sum("(M3)" in n for n in notes) == 2


def test_d5_a_condition_on_a_data_request_result_names_a_returned_field():
    """Live (GreenCart, 2026-09-14): the choice branched on requestReturn.accepted
    while the data request returns 'success'; every accepted return escalated."""
    flow = broken("GetCleaningPrice")
    start = next(n for n in flow["nodes"].values() if n.get("type") == "start")
    flow["nodes"]["gate"] = {"nodeId": "gate", "type": "choice", "childNodes": [
        {"nodeId": "ok", "name": "accepted", "conditions": [
            {"left": {"type": "variable", "name": "getCleaningPrice.accepted"}, "operator": "eq",
             "right": {"type": "constant", "value": True}}]},
        {"nodeId": "no", "name": "rejected", "conditions": [
            {"left": {"type": "variable", "name": "getCleaningPrice.accepted"}, "operator": "neq",
             "right": {"type": "constant", "value": True}}]},
        {"nodeId": "odd", "name": "odd", "conditions": [
            {"left": {"type": "variable", "name": "getCleaningPrice.zzzUnknown"}, "operator": "eq",
             "right": {"type": "constant", "value": "x"}}]},
    ]}
    for nid in ("ok", "no", "odd"):
        flow["nodes"][nid] = {"nodeId": nid, "type": "end"}
    start["childNodes"] = [{"nodeId": "gate", "name": "next"}]
    ctx = context("GetCleaningPrice")
    props = (ctx["data_requests"]["getCleaningPrice"].get("responseSchema") or {}).get("properties") or {}
    assert "success" in props and "accepted" not in props
    out, notes = apply_runtime_contract(flow, **ctx)
    edges = {e["name"]: e for e in out["nodes"]["gate"]["childNodes"]}
    assert edges["accepted"]["conditions"][0]["left"]["name"] == "getCleaningPrice.success"
    assert edges["rejected"]["conditions"][0]["left"]["name"] == "getCleaningPrice.success"
    assert edges["odd"]["conditions"][0]["left"]["name"] == "getCleaningPrice.zzzUnknown"
    assert sum("(D5)" in n for n in notes) == 2
    from tools.acxd_runtime_contract import runtime_contract_violations
    assert any("D5" in str(v) for v in runtime_contract_violations(out, **ctx))


def test_d5_an_impossible_enum_constant_moves_a_two_way_branch_onto_the_success_flag():
    """Live (Hanbit, 2026-09-14): 'cancelAppointment.status neq "not_cancelable"'
    while the API's status enum is 예/취소/완료 — every appointment was cancelable.
    Only the two-way eq/neq pair is unambiguous; a three-way choice on impossible
    constants is reported (mapping all three onto the flag collapsed them, live)."""
    import copy

    def flow_with(edges):
        flow = broken("GetCleaningPrice")
        start = next(n for n in flow["nodes"].values() if n.get("type") == "start")
        flow["nodes"]["gate"] = {"nodeId": "gate", "type": "choice", "childNodes": edges}
        for e in edges:
            flow["nodes"][e["nodeId"]] = {"nodeId": e["nodeId"], "type": "end"}
        start["childNodes"] = [{"nodeId": "gate", "name": "next"}]
        return flow

    def cond(op, value):
        return [{"left": {"type": "variable", "name": "getCleaningPrice.state"}, "operator": op,
                 "right": {"type": "constant", "value": value}}]

    ctx = context("GetCleaningPrice")
    ctx["data_requests"] = copy.deepcopy(ctx["data_requests"])
    props = ctx["data_requests"]["getCleaningPrice"]["responseSchema"]["properties"]
    props["state"] = {"type": "string", "enum": ["예약", "취소", "완료"]}

    two_way = flow_with([{"nodeId": "ok", "name": "cancelable", "conditions": cond("neq", "not_cancelable")},
                         {"nodeId": "no", "name": "notCancelable", "conditions": cond("eq", "not_cancelable")}])
    out, notes = apply_runtime_contract(two_way, **ctx)
    edges = {e["name"]: e["conditions"][0] for e in out["nodes"]["gate"]["childNodes"]}
    assert edges["cancelable"] == {"left": {"type": "variable", "name": "getCleaningPrice.success"},
                                   "operator": "eq", "right": {"type": "constant", "value": True}}
    assert edges["notCancelable"]["operator"] == "neq"
    assert sum("(D5)" in n for n in notes) == 2

    three_way = flow_with([{"nodeId": "a", "name": "lookup", "conditions": cond("eq", "lookup")},
                           {"nodeId": "b", "name": "cancelled", "conditions": cond("eq", "cancelled")},
                           {"nodeId": "c", "name": "blocked", "conditions": cond("eq", "not_cancelable")}])
    out, notes = apply_runtime_contract(three_way, **ctx)
    conds = [e["conditions"][0]["right"]["value"] for e in out["nodes"]["gate"]["childNodes"]]
    assert conds == ["lookup", "cancelled", "not_cancelable"]          # left for the generator to fix
    from tools.acxd_runtime_contract import runtime_contract_violations
    d5 = [v for v in runtime_contract_violations(three_way, **ctx) if "D5" in str(v)]
    assert len(d5) == 3 and all("errorCode" in str(v) for v in d5)

    fine = flow_with([{"nodeId": "a", "name": "done", "conditions": cond("eq", "취소")},
                      {"nodeId": "b", "name": "other", "conditions": cond("neq", "취소")}])
    out, notes = apply_runtime_contract(fine, **ctx)
    assert not [n for n in notes if "(D5)" in n]                       # real enum members are left alone




def test_m2_prompt_labels_do_not_leak_fragments_of_earlier_placeholders():
    """Live (GreenCart, 2026-09-14): '상태 {a.status:NLX.Variable}, 택배사 {a.carrier:NLX.Variable}'
    produced '…, Variable} 택배사 …' because the sentence split cut through the
    first placeholder's ':' / '.'."""
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values() if n["type"] == "generative_text")
    rid = "getDeliveryStatusByOrderNumber"
    generative["metadata"]["generativeText"]["prompt"] = (
        f"상태 {{{rid}.deliveryStatus:NLX.Variable}}, 배송 예정일 {{{rid}.expectedDeliveryDate:NLX.Variable}}를 안내한다")
    out, _ = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    body = m2_fallback_body(out, generative["nodeId"])
    assert "Variable}" not in body.replace(":NLX.Variable}", "")
    assert "배송 예정일 {" in body and "상태 {" in body


def test_m2_result_labels_stay_in_the_callers_language():
    """Live (GAON v4): 'Air conditioner product type {…}' was read to a Korean
    caller because the interview described the field in English."""
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values() if n["type"] == "generative_text")
    generative["metadata"]["generativeText"]["prompt"] = "친절하게 안내하세요."
    kwargs = context("DeliveryStatusByOrderNumber")
    kwargs["field_labels"] = {"getDeliveryStatusByOrderNumber": {"deliveryStatus": "Delivery status in English",
                                                                 "expectedDeliveryDate": "예상 배송일"}}
    out, _ = apply_runtime_contract(flow, **kwargs)
    body = m2_fallback_body(out, generative["nodeId"])
    assert "Delivery status in English" not in body
    assert "배송 상태 {getDeliveryStatusByOrderNumber.deliveryStatus:NLX.Variable}" in body   # dictionary word
    assert "예상 배송일 {getDeliveryStatusByOrderNumber.expectedDeliveryDate:NLX.Variable}" in body  # Korean description kept


def test_d4_failure_edge_to_a_silent_followup_is_sent_to_the_escalation():
    """Live (GreenCart, 2026-09-14): the failure edge went straight to the
    FollowUp redirect, so a failed lookup produced '더 도와드릴 일이 있을까요?'
    and nothing else."""
    flow = broken("DeliveryStatusByOrderNumber")
    request_node = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    followup_id = "f0110000-0000-4000-8000-000000000001"
    flow["nodes"][followup_id] = {"nodeId": followup_id, "type": "redirect",
                                  "metadata": {"redirect": {"type": "flow", "flowId": "FollowUpFlow"}},
                                  "childNodes": []}
    request_node["childNodes"] = [e for e in request_node["childNodes"] if e["name"] != "error"]
    request_node["childNodes"].append({"nodeId": followup_id, "conditions": [
        {"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]})
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    fixed = nodes_of(out, "data_request")[0]
    failure = next(e for e in fixed["childNodes"]
                   if any(c.get("right", {}).get("value") == "failure" for c in e.get("conditions") or []))
    assert out["nodes"][failure["nodeId"]]["type"] == "escalate"
    assert any("answered nothing on failure" in note for note in notes)


def test_m2_after_the_answer_was_already_given_is_a_silent_pass_through():
    """Live (GAON, 2026-09-14): the flow's own message said '예약이 접수되었습니다.
    예약번호는 …' and a trailing generative node then produced a second
    '조회 결과: …' from raw field descriptions."""
    flow = broken("DeliveryStatusByOrderNumber")
    generative = next(n for n in flow["nodes"].values() if n["type"] == "generative_text")
    generative["metadata"]["generativeText"]["prompt"] = "친절하게 안내하세요."
    # a message node between the data request and the generative node already answers
    request = next(n for n in flow["nodes"].values() if n["type"] == "data_request")
    said_id = "5a1d0000-0000-4000-8000-000000000001"
    for edge in request["childNodes"]:
        if edge["nodeId"] == generative["nodeId"]:
            edge["nodeId"] = said_id
    flow["nodes"][said_id] = {"nodeId": said_id, "type": "basic",
                              "messages": [{"type": "text", "body": "배송 상태는 {getDeliveryStatusByOrderNumber.deliveryStatus:NLX.Variable}입니다."}],
                              "childNodes": [{"nodeId": generative["nodeId"], "name": "next"}]}
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    node = out["nodes"][generative["nodeId"]]
    assert node["type"] == "basic" and not node.get("messages")
    assert any("silent pass-through" in n for n in notes)


def test_result_labels_are_short_spoken_labels_not_whole_descriptions():
    from tools.acxd_runtime_contract import _RuntimeContract
    short = _RuntimeContract._short_label
    assert short("총 금액 (unitPrice × quantity)") == "총 금액"
    assert short("예약 상태. PoC에서는 PENDING으로 접수 후 확정 연락") == "예약 상태"
    assert short("발급된 예약번호") == "발급된 예약번호"
    assert short("이 필드는 고객이 예약을 접수할 때 시스템이 자동으로 발급하는 번호입니다") == ""


def test_s9_date_and_time_shaped_regex_slots_become_date_and_time_slots():
    """Live (Hanbit, 2026-09-14): appointmentDate was NLX.AlphaNumeric with regex
    ^\\d{4}-\\d{2}-\\d{2}$; built-in values arrive without separators, so
    '2026-09-18' never matched, the retry edge led to the fallback and the third
    miss escalated. NLX.Date recognised the same input in another project."""
    flow = broken("DeliveryStatusByOrderNumber")
    flow["slotTypes"] = list(flow.get("slotTypes") or []) + [
        {"name": "appointmentDate", "type": "NLX.AlphaNumeric", "sensitive": False,
         "regex": "^\\d{4}-\\d{2}-\\d{2}$", "aiDescription": "Appointment date"},
        {"name": "timeSlot", "type": "NLX.AlphaNumeric", "sensitive": False,
         "regex": "^\\d{2}:\\d{2}$", "aiDescription": "Appointment time"},
        {"name": "orderNo", "type": "NLX.AlphaNumeric", "sensitive": False,
         "regex": "^\\d{10}$", "aiDescription": "Order number"},
    ]
    out, notes = apply_runtime_contract(flow, **context("DeliveryStatusByOrderNumber"))
    slots = {s["name"]: s for s in out["slotTypes"]}
    assert slots["appointmentDate"]["type"] == "NLX.Date" and "regex" not in slots["appointmentDate"]
    # NLX.Time delivers a timezone-shifted instant ("10:00" → "2026-09-14T14:00:00.000Z", live),
    # so a time keeps the text type with the compact-digits regex the typed value arrives in
    assert slots["timeSlot"]["type"] == "NLX.AlphaNumeric" and slots["timeSlot"]["regex"] == "^[0-9]{3,4}$"
    assert slots["orderNo"] == {"name": "orderNo", "type": "NLX.AlphaNumeric", "sensitive": False,
                                "regex": "^\\d{10}$", "aiDescription": "Order number"}
    assert sum("S9)" in n for n in notes) == 2


def test_d5_reads_enum_values_from_the_spec_when_the_reply_schema_has_none():
    """The reply schema no longer carries enums (a not-found reply's status ""
    failed the whole reply, live); the interview's allowed values reach D5 as
    field_enums."""
    import copy
    flow = broken("GetCleaningPrice")
    start = next(n for n in flow["nodes"].values() if n.get("type") == "start")
    edges = [{"nodeId": "ok", "name": "cancelable", "conditions": [
                  {"left": {"type": "variable", "name": "getCleaningPrice.state"}, "operator": "neq",
                   "right": {"type": "constant", "value": "not_cancelable"}}]},
             {"nodeId": "no", "name": "notCancelable", "conditions": [
                  {"left": {"type": "variable", "name": "getCleaningPrice.state"}, "operator": "eq",
                   "right": {"type": "constant", "value": "not_cancelable"}}]}]
    flow["nodes"]["gate"] = {"nodeId": "gate", "type": "choice", "childNodes": edges}
    for e in edges:
        flow["nodes"][e["nodeId"]] = {"nodeId": e["nodeId"], "type": "end"}
    start["childNodes"] = [{"nodeId": "gate", "name": "next"}]
    ctx = context("GetCleaningPrice")
    ctx["data_requests"] = copy.deepcopy(ctx["data_requests"])
    ctx["data_requests"]["getCleaningPrice"]["responseSchema"]["properties"]["state"] = {"type": "string"}  # no enum
    ctx["field_enums"] = {"getCleaningPrice": {"state": ["예약", "취소", "완료"]}}
    out, notes = apply_runtime_contract(flow, **ctx)
    edges_out = {e["name"]: e["conditions"][0] for e in out["nodes"]["gate"]["childNodes"]}
    assert edges_out["cancelable"]["left"] == {"type": "variable", "name": "getCleaningPrice.success"}
    assert sum("(D5)" in n for n in notes) == 2


def test_p1_a_request_reached_by_alternative_captures_sends_only_the_slots_each_path_filled():
    """Live (GreenCart, 2026-09-14): a return lookup by return number OR order
    number sent both slot placeholders; on either path one slot was empty, the
    webhook was never invoked and the caller was escalated."""
    flow = {"flowId": "ReturnStatus", "nodes": {
        "s": {"nodeId": "s", "type": "start", "childNodes": [{"nodeId": "askR", "name": "ask"}]},
        "askR": {"nodeId": "askR", "type": "user_choice", "messages": [{"type": "text", "body": "반품번호 또는 주문번호?"}],
                 "metadata": {"choice": {"source": "slotType", "slotTypeId": "returnId"}},
                 "childNodes": [{"nodeId": "dr", "name": "returnId captured",
                                 "conditions": [{"left": {"type": "slot", "name": "returnId"}, "operator": "exists"}]},
                                {"nodeId": "askO", "name": "try order",
                                 "conditions": [{"left": {"type": "slot", "name": "returnId"}, "operator": "not_exists"}]}]},
        "askO": {"nodeId": "askO", "type": "user_choice", "messages": [{"type": "text", "body": "주문번호?"}],
                 "metadata": {"choice": {"source": "slotType", "slotTypeId": "orderNumber"}},
                 "childNodes": [{"nodeId": "dr", "name": "orderNumber captured",
                                 "conditions": [{"left": {"type": "slot", "name": "orderNumber"}, "operator": "exists"}]},
                                {"nodeId": "esc", "name": "order not captured",
                                 "conditions": [{"left": {"type": "slot", "name": "orderNumber"}, "operator": "not_exists"}]}]},
        "dr": {"nodeId": "dr", "type": "data_request",
               "dataRequests": [{"dataRequestId": "getReturnStatus",
                                 "payload": {"returnId": "{returnId:NLX.Slot}", "orderNumber": "{orderNumber:NLX.Slot}"}}],
               "childNodes": [{"nodeId": "say", "name": "success",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                              {"nodeId": "esc", "name": "failure",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        "say": {"nodeId": "say", "type": "basic", "messages": [{"type": "text", "body": "상태 {getReturnStatus.status:NLX.Variable}"}],
                "childNodes": [{"nodeId": "end", "name": "done"}]},
        "esc": {"nodeId": "esc", "type": "escalate"},
        "end": {"nodeId": "end", "type": "end"},
    }, "slotTypes": [{"name": "returnId", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^RT-[0-9]{6}$"},
                     {"name": "orderNumber", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^GC-[0-9]{8}$"}]}
    dr_doc = {"dataRequestId": "getReturnStatus", "webhook": {"url": "{WEBHOOK_URL}/tools/get_return_status"},
              "requestSchema": {"type": "object", "properties": {"returnId": {"type": "string"}, "orderNumber": {"type": "string"}}},
              "responseSchema": {"type": "object", "properties": {"success": {"type": "boolean"}, "status": {"type": "string"}}}}
    out, notes = apply_runtime_contract(flow, role="operation", data_requests={"getReturnStatus": dr_doc},
                                        flow_ids=["ReturnStatus"], escalation_flow_id="Escalation")
    requests = {nid: n["dataRequests"][0]["payload"] for nid, n in out["nodes"].items() if n["type"] == "data_request"}
    payloads = sorted(tuple(sorted(p)) for p in requests.values())
    assert payloads == [("orderNumber",), ("returnId",)]          # one request per capture path, one slot each

    def _request_after_capture(node_id, slot):
        # the capture edge (slot exists) → F1 format guard → its 'formatValid' basic → the data request
        edge = next(e for e in out["nodes"][node_id]["childNodes"]
                    if any(c.get("operator") == "exists" and c["left"].get("name") == slot for c in e.get("conditions") or []))
        nid = edge["nodeId"]
        for _ in range(4):
            if out["nodes"][nid]["type"] == "data_request":
                return nid
            nid = out["nodes"][nid]["childNodes"][0]["nodeId"]
        raise AssertionError(f"no data request after {node_id}")

    assert requests[_request_after_capture("askR", "returnId")] == {"returnId": "{returnId:NLX.Slot}"}
    assert requests[_request_after_capture("askO", "orderNumber")] == {"orderNumber": "{orderNumber:NLX.Slot}"}
    assert sum("P1)" in n for n in notes) == 2


def test_p1_a_single_path_request_drops_slots_the_path_never_captures():
    """Live (GreenCart, 2026-09-14): the flow asked for the return number only
    but the payload also sent the never-asked order number."""
    flow = {"flowId": "ReturnStatus", "nodes": {
        "s": {"nodeId": "s", "type": "start", "childNodes": [{"nodeId": "askR", "name": "ask"}]},
        "askR": {"nodeId": "askR", "type": "user_choice", "messages": [{"type": "text", "body": "반품번호?"}],
                 "metadata": {"choice": {"source": "slotType", "slotTypeId": "returnId"}},
                 "childNodes": [{"nodeId": "dr", "name": "captured",
                                 "conditions": [{"left": {"type": "slot", "name": "returnId"}, "operator": "exists"}]},
                                {"nodeId": "esc", "name": "not captured",
                                 "conditions": [{"left": {"type": "slot", "name": "returnId"}, "operator": "not_exists"}]}]},
        "dr": {"nodeId": "dr", "type": "data_request",
               "dataRequests": [{"dataRequestId": "getReturnStatus",
                                 "payload": {"returnId": "{returnId:NLX.Slot}", "orderNumber": "{orderNumber:NLX.Slot}"}}],
               "childNodes": [{"nodeId": "say", "name": "success",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                              {"nodeId": "esc", "name": "failure",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        "say": {"nodeId": "say", "type": "basic", "messages": [{"type": "text", "body": "상태 {getReturnStatus.status:NLX.Variable}"}],
                "childNodes": [{"nodeId": "end", "name": "done"}]},
        "esc": {"nodeId": "esc", "type": "escalate"},
        "end": {"nodeId": "end", "type": "end"},
    }, "slotTypes": [{"name": "returnId", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^RT-[0-9]{6}$"},
                     {"name": "orderNumber", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^GC-[0-9]{8}$"}]}
    dr_doc = {"dataRequestId": "getReturnStatus", "webhook": {"url": "{WEBHOOK_URL}/tools/get_return_status"},
              "requestSchema": {"type": "object", "properties": {"returnId": {"type": "string"}, "orderNumber": {"type": "string"}}},
              "responseSchema": {"type": "object", "properties": {"success": {"type": "boolean"}, "status": {"type": "string"}}}}
    out, notes = apply_runtime_contract(flow, role="operation", data_requests={"getReturnStatus": dr_doc},
                                        flow_ids=["ReturnStatus"], escalation_flow_id="Escalation")
    assert out["nodes"]["dr"]["dataRequests"][0]["payload"] == {"returnId": "{returnId:NLX.Slot}"}
    assert any("payload dropped ['orderNumber']" in n for n in notes)


def test_review_gate_sees_impossible_enum_constants_through_the_spec():
    """Live (2026-09-15, review): a RequestReturn choice branched on
    ``requestReturn.status == "rejected"`` while the API's status enum is
    자동승인/승인대기. The reply schema carries types only, and the D9 gate ran the
    runtime contract without the spec's enum values, so the dead branch was
    invisible at review and download time (the reviewer LLM saw it as advisory).
    The gate now derives the enums from the spec's data integrations."""
    from tools.validate_acxd_consistency import validate_acxd_consistency
    from tools.acxd_runtime_contract import field_enums_from_integrations

    def nid(n):
        return f"a0000000-0000-4000-8000-{n:012d}"

    flow = {
        "flowId": "RequestReturn", "name": "RequestReturn", "type": "flow",
        "description": "Return request", "aiDescription": "Customer wants to return an order",
        "startNodeId": nid(1),
        "nodes": {
            nid(1): {"nodeId": nid(1), "type": "start", "childNodes": [{"nodeId": nid(2), "name": "next"}]},
            nid(2): {"nodeId": nid(2), "type": "data_request",
                     "dataRequests": [{"dataRequestId": "requestReturn", "payload": {}}],
                     "childNodes": [
                         {"nodeId": nid(3), "name": "success", "conditions": [
                             {"left": {"type": "node_status"}, "operator": "eq",
                              "right": {"type": "constant", "value": "success"}}]},
                         {"nodeId": nid(5), "name": "failure", "conditions": [
                             {"left": {"type": "node_status"}, "operator": "eq",
                              "right": {"type": "constant", "value": "failure"}}]}]},
            nid(3): {"nodeId": nid(3), "type": "choice", "childNodes": [
                {"nodeId": nid(5), "name": "rejected", "conditions": [
                    {"left": {"type": "variable", "name": "requestReturn.status"}, "operator": "eq",
                     "right": {"type": "constant", "value": "rejected"}}]},
                {"nodeId": nid(4), "name": "accepted"}]},
            nid(4): {"nodeId": nid(4), "type": "basic",
                     "messages": [{"body": "Return {requestReturn.returnId:NLX.Variable} accepted"}],
                     "childNodes": [{"nodeId": nid(6), "name": "next"}]},
            nid(5): {"nodeId": nid(5), "type": "redirect",
                     "metadata": {"redirect": {"type": "flow", "flowId": "Escalation"}}, "childNodes": []},
            nid(6): {"nodeId": nid(6), "type": "end"},
        },
    }
    data_request = {"dataRequestId": "requestReturn", "name": "requestReturn",
                    "responseSchema": {"type": "object", "properties": {
                        "success": {"type": "boolean"}, "errorCode": {"type": "string"},
                        "returnId": {"type": "string"}, "status": {"type": "string"}}}}
    spec = {"data_integrations": [{"data_request_id": "requestReturn", "response_fields": [
        {"name": "status", "type": "enum", "enum_values": ["자동승인", "승인대기"]},
        {"name": "returnId", "type": "string"}]}]}
    assert field_enums_from_integrations(spec["data_integrations"]) == {
        "requestReturn": {"status": ["자동승인", "승인대기"]}}

    bundle = {"flows": [flow], "slot_types": [], "data_requests": [data_request]}
    with_spec = [v.message for v in validate_acxd_consistency(bundle, spec) if v.code == "RUNTIME_CONTRACT"]
    assert any("D5" in m and "'rejected'" in m and "never returns" in m for m in with_spec), with_spec
    without_spec = [v.message for v in validate_acxd_consistency(bundle) if v.code == "RUNTIME_CONTRACT"]
    assert not any("'rejected'" in m for m in without_spec)


def _capture_flow():
    """Return-status lookup: return number, else order number, else escalate."""
    def uc(nid, slot, prompt, captured_to, missing_to):
        return {"nodeId": nid, "type": "user_choice", "messages": [{"type": "text", "body": prompt}],
                "metadata": {"choice": {"source": "slotType", "slotTypeId": slot}},
                "childNodes": [{"nodeId": captured_to, "name": "captured",
                                "conditions": [{"left": {"type": "slot", "name": slot}, "operator": "exists"}]},
                               {"nodeId": missing_to, "name": "missing",
                                "conditions": [{"left": {"type": "slot", "name": slot}, "operator": "not_exists"}]}]}
    return {"flowId": "ReturnStatus", "mainLanguageCode": "ko-KR", "nodes": {
        "s": {"nodeId": "s", "type": "start", "childNodes": [{"nodeId": "askR", "name": "ask"}]},
        "askR": uc("askR", "returnId", "반품번호를 알려주세요.", "dr", "askO"),
        "askO": uc("askO", "orderNumber", "주문번호를 알려주세요.", "dr", "esc"),
        "dr": {"nodeId": "dr", "type": "data_request",
               "dataRequests": [{"dataRequestId": "getReturnStatus", "payload": {"returnId": "{returnId:NLX.Slot}"}}],
               "childNodes": [{"nodeId": "say", "name": "success",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                              {"nodeId": "esc", "name": "failure",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        "say": {"nodeId": "say", "type": "basic", "messages": [{"type": "text", "body": "상태 {getReturnStatus.status:NLX.Variable}"}],
                "childNodes": [{"nodeId": "end", "name": "done"}]},
        "esc": {"nodeId": "esc", "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "Escalation"}},
                "childNodes": [{"nodeId": "end", "name": "next"}]},
        "end": {"nodeId": "end", "type": "end"},
    }, "slotTypes": [{"name": "returnId", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^RT-[0-9]{6}$"},
                     {"name": "orderNumber", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^GC-[0-9]{8}$"}]}


_DR_DOC = {"dataRequestId": "getReturnStatus", "webhook": {"url": "{WEBHOOK_URL}/tools/get_return_status"},
           "requestSchema": {"type": "object", "properties": {"returnId": {"type": "string"}, "orderNumber": {"type": "string"}}},
           "responseSchema": {"type": "object", "properties": {"success": {"type": "boolean"}, "status": {"type": "string"}}}}


def test_runtime_regex_makes_separators_optional_and_letters_case_insensitive():
    import re
    from tools.acxd_runtime_contract import runtime_regex
    assert runtime_regex(r"^GC-\d{8}$") == "^[Gg][Cc][-. /:]?[0-9]{8}$"
    assert runtime_regex(r"^010-\d{4}-\d{4}$") == "^010[-. /:]?[0-9]{4}[-. /:]?[0-9]{4}$"
    for delivered in ("GC20260902", "GC-20260902", "gc 20260902"):
        assert re.fullmatch(runtime_regex(r"^GC-\d{8}$"), delivered)
    assert not re.fullmatch(runtime_regex(r"^RT-\d{6}$"), "GC20260902")
    assert runtime_regex(r"^[0-9]{8}$") == r"^[0-9]{8}$"        # already compact: used as written
    assert runtime_regex("(") is None and runtime_regex(None) is None


def test_f1_guards_a_pattern_slot_with_a_matches_regex_check_and_a_bounded_retry():
    """Live (2026-09-15): the attached regex did not gate capture — an order number
    typed at the return-number prompt was stored in returnId and the lookup
    escalated. The captured value is now checked before use."""
    out, notes = apply_runtime_contract(_capture_flow(), role="operation", data_requests={"getReturnStatus": _DR_DOC},
                                        flow_ids=["ReturnStatus", "Fallback", "Escalation"], escalation_flow_id="Escalation",
                                        slot_type_ids={"yesNo"})
    nodes = out["nodes"]
    ask = nodes["askR"]
    capture = next(e for e in ask["childNodes"] if any(c.get("operator") == "exists" and c["left"]["name"] == "returnId"
                                                       for c in e.get("conditions") or []))
    guard = nodes[capture["nodeId"]]
    assert guard["type"] == "choice"
    valid, invalid = guard["childNodes"]
    assert valid["conditions"] == [{"left": {"type": "slot", "name": "returnId"}, "operator": "matches_regex",
                                    "right": {"type": "constant", "value": "^[Rr][Tt][-. /:]?[0-9]{6}$"}}]
    ok = nodes[valid["nodeId"]]
    assert ok["type"] == "basic" and ok["metadata"]["stateModifications"][0]["name"] == "formatRetries"
    assert nodes[ok["childNodes"][0]["nodeId"]]["type"] == "data_request"
    retry = nodes[invalid["nodeId"]]
    assert retry["messages"][0]["body"] == "말씀하신 값이 형식에 맞지 않습니다."
    assert {"type": "slot", "name": "returnId", "modification": "clear"} in retry["metadata"]["stateModifications"]
    check = nodes[retry["childNodes"][0]["nodeId"]]
    give_up, again = check["childNodes"]
    assert give_up["conditions"][0]["left"] == {"type": "context", "name": "formatRetries"}
    assert give_up["conditions"][0]["right"]["value"] == 1       # another question exists → move on at once
    assert give_up["nodeId"] == "askO"           # the node's own 'not captured' path: ask for the order number
    assert again["nodeId"] == "askR"
    assert {"name": "formatRetries", "type": "number"} in out["contextVariables"]
    # the order-number node has no further question to fall back to → two misses, then the fallback flow
    order_guard = nodes[next(e for e in nodes["askO"]["childNodes"]
                             if any(c.get("operator") == "exists" and c["left"]["name"] == "orderNumber"
                                    for c in e.get("conditions") or []))["nodeId"]]
    order_check = nodes[nodes[order_guard["childNodes"][1]["nodeId"]]["childNodes"][0]["nodeId"]]
    assert order_check["childNodes"][0]["conditions"][0]["right"]["value"] == 2
    give_up_target = nodes[order_check["childNodes"][0]["nodeId"]]
    assert give_up_target["type"] == "redirect" and give_up_target["metadata"]["redirect"]["flowId"] in ("Escalation", "Fallback")
    assert sum("(F1)" in n for n in notes) == 2
    # idempotent: a second pass adds nothing
    again_out, again_notes = apply_runtime_contract(out, role="operation", data_requests={"getReturnStatus": _DR_DOC},
                                                    flow_ids=["ReturnStatus", "Fallback", "Escalation"],
                                                    escalation_flow_id="Escalation", slot_type_ids={"yesNo"})
    assert not any("(F1)" in n for n in again_notes) and len(again_out["nodes"]) == len(nodes)
    import re
    v4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    added = [nid for nid in nodes if nid not in _capture_flow()["nodes"]]
    assert added and all(v4.match(nid) for nid in added), added      # every node the rule adds is v4-shaped


def _intake_flow():
    """Fresh generation (2026-09-15): every 'not captured' edge went on to the NEXT
    question, so a missed order number reached the intake request unasked."""
    def uc(nid, slot, prompt, captured_to, missing_to):
        return {"nodeId": nid, "type": "user_choice", "messages": [{"type": "text", "body": prompt}],
                "metadata": {"choice": {"source": "slotType", "slotTypeId": slot}},
                "childNodes": [{"nodeId": captured_to, "name": f"{slot} captured",
                                "conditions": [{"left": {"type": "slot", "name": slot}, "operator": "exists"}]},
                               {"nodeId": missing_to, "name": f"{slot} not captured",
                                "conditions": [{"left": {"type": "slot", "name": slot}, "operator": "not_exists"}]}]}
    return {"flowId": "RequestReturn", "mainLanguageCode": "ko-KR", "nodes": {
        "s": {"nodeId": "s", "type": "start", "childNodes": [{"nodeId": "askO", "name": "ask"}]},
        "askO": uc("askO", "orderNumber", "반품하실 주문번호를 알려주세요.", "askP", "askP"),
        "askP": uc("askP", "contactPhone", "연락처를 알려주세요.", "dr", "esc"),
        "dr": {"nodeId": "dr", "type": "data_request",
               "dataRequests": [{"dataRequestId": "requestReturn",
                                 "payload": {"orderNumber": "{orderNumber:NLX.Slot}", "contactPhone": "{contactPhone:NLX.Slot}"}}],
               "childNodes": [{"nodeId": "say", "name": "success",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                              {"nodeId": "esc", "name": "failure",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        "say": {"nodeId": "say", "type": "basic", "messages": [{"type": "text", "body": "접수 {requestReturn.returnId:NLX.Variable}"}],
                "childNodes": [{"nodeId": "end", "name": "done"}]},
        "esc": {"nodeId": "esc", "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "Escalation"}},
                "childNodes": [{"nodeId": "end", "name": "next"}]},
        "end": {"nodeId": "end", "type": "end"},
    }, "slotTypes": [{"name": "orderNumber", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^GC-[0-9]{8}$"},
                     {"name": "contactPhone", "type": "NLX.PhoneNumber", "sensitive": False}]}


_INTAKE_DOC = {"dataRequestId": "requestReturn", "webhook": {"url": "{WEBHOOK_URL}/tools/request_return"},
               "requestSchema": {"type": "object", "required": ["orderNumber", "contactPhone"],
                                 "properties": {"orderNumber": {"type": "string"}, "contactPhone": {"type": "string"}}},
               "responseSchema": {"type": "object", "properties": {"success": {"type": "boolean"}, "returnId": {"type": "string"}}}}


def test_r8_a_missed_value_the_request_needs_is_re_asked_not_skipped():
    out, notes = apply_runtime_contract(_intake_flow(), role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                                        flow_ids=["RequestReturn", "Fallback", "Escalation"], escalation_flow_id="Escalation",
                                        slot_type_ids={"yesNo"})
    nodes = out["nodes"]
    missing = next(e for e in nodes["askO"]["childNodes"]
                   if any(c.get("operator") == "not_exists" for c in e.get("conditions") or []))
    recovery = nodes[past_agent_gate(out, missing["nodeId"])]
    assert recovery["type"] == "basic" and recovery["childNodes"] == [{"nodeId": "askO", "name": "retry"}]
    assert {"type": "slot", "name": "orderNumber", "modification": "clear"} in recovery["metadata"]["stateModifications"]
    assert recovery["messages"][0]["body"]
    assert any("(R8)" in n and "orderNumber" in n for n in notes), notes
    # the request keeps its full payload: no path reaches it without the order number any more
    request = next(n for n in nodes.values() if n.get("type") == "data_request")
    assert set(request["dataRequests"][0]["payload"]) == {"orderNumber", "contactPhone"}
    assert sum(n.get("type") == "data_request" for n in nodes.values()) == 1
    assert not any("(P1)" in n for n in notes)
    # a missed phone number goes to the escalation — that path sends nothing, so it is left alone
    phone_missing = next(e for e in nodes["askP"]["childNodes"]
                         if any(c.get("operator") == "not_exists" for c in e.get("conditions") or []))
    assert past_agent_gate(out, phone_missing["nodeId"]) == "esc"
    # F1's give-up for the order number is the fallback flow, not the next question
    guard = nodes[next(e for e in nodes["askO"]["childNodes"]
                       if any(c.get("operator") == "exists" for c in e.get("conditions") or []))["nodeId"]]
    check = nodes[nodes[guard["childNodes"][1]["nodeId"]]["childNodes"][0]["nodeId"]]
    give_up = nodes[check["childNodes"][0]["nodeId"]]
    assert give_up["type"] == "redirect" and give_up["metadata"]["redirect"]["flowId"] == "Fallback"
    assert check["childNodes"][0]["conditions"][0]["right"]["value"] == 2


def test_r8_leaves_an_alternative_identifier_path_alone():
    """Return number OR order number: the request reached without the return
    number does not send it, so 'not captured → ask the order number' stands."""
    out, notes = apply_runtime_contract(_capture_flow(), role="operation", data_requests={"getReturnStatus": _DR_DOC},
                                        flow_ids=["ReturnStatus", "Fallback", "Escalation"], escalation_flow_id="Escalation",
                                        slot_type_ids={"yesNo"})
    missing = next(e for e in out["nodes"]["askR"]["childNodes"]
                   if any(c.get("operator") == "not_exists" for c in e.get("conditions") or []))
    assert past_agent_gate(out, missing["nodeId"]) == "askO"
    assert not any("(R8)" in n for n in notes)


def _crossed_success_flow(crossed=True):
    """After the intake request: 'accepted' announces the result, 'rejected'
    escalates — but the LLM put ``success neq true`` on 'accepted' and
    ``success eq true`` on 'rejected' (live 2026-09-16: an accepted return was
    reported as refused)."""
    pos = {"left": {"type": "variable", "name": "requestReturn.success"}, "operator": "eq", "right": {"type": "constant", "value": True}}
    neg = {"left": {"type": "variable", "name": "requestReturn.success"}, "operator": "neq", "right": {"type": "constant", "value": True}}
    return {"flowId": "RequestReturn", "mainLanguageCode": "ko-KR", "nodes": {
        "s": {"nodeId": "s", "type": "start", "childNodes": [{"nodeId": "askO", "name": "ask"}]},
        "askO": {"nodeId": "askO", "type": "user_choice", "messages": [{"type": "text", "body": "주문번호를 알려주세요."}],
                 "metadata": {"choice": {"source": "slotType", "slotTypeId": "orderNumber"}},
                 "childNodes": [{"nodeId": "dr", "name": "captured", "conditions": [{"left": {"type": "slot", "name": "orderNumber"}, "operator": "exists"}]},
                                {"nodeId": "esc", "name": "missing", "conditions": [{"left": {"type": "slot", "name": "orderNumber"}, "operator": "not_exists"}]}]},
        "dr": {"nodeId": "dr", "type": "data_request",
               "dataRequests": [{"dataRequestId": "requestReturn", "payload": {"orderNumber": "{orderNumber:NLX.Slot}"}}],
               "childNodes": [{"nodeId": "branch", "name": "success",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "success"}}]},
                              {"nodeId": "esc", "name": "failure",
                               "conditions": [{"left": {"type": "node_status"}, "operator": "eq", "right": {"type": "constant", "value": "failure"}}]}]},
        "branch": {"nodeId": "branch", "type": "choice", "childNodes": [
            {"nodeId": "ok", "name": "accepted", "conditions": [neg if crossed else pos]},
            {"nodeId": "no", "name": "rejected", "conditions": [pos if crossed else neg]}]},
        "ok": {"nodeId": "ok", "type": "basic", "messages": [{"type": "text", "body": "반품번호 {requestReturn.returnId:NLX.Variable}, 상태 {requestReturn.status:NLX.Variable}"}],
               "childNodes": [{"nodeId": "end", "name": "done"}]},
        "no": {"nodeId": "no", "type": "basic", "messages": [{"type": "text", "body": "죄송해요. 이 주문은 반품 접수가 어려워요."}],
               "childNodes": [{"nodeId": "esc", "name": "next"}]},
        "esc": {"nodeId": "esc", "type": "redirect", "metadata": {"redirect": {"type": "flow", "flowId": "Escalation"}},
                "childNodes": [{"nodeId": "end", "name": "next"}]},
        "end": {"nodeId": "end", "type": "end"},
    }, "slotTypes": [{"name": "orderNumber", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^GC-[0-9]{8}$"}]}


def test_d6_crossed_success_conditions_are_swapped_back():
    out, notes = apply_runtime_contract(_crossed_success_flow(True), role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                                        flow_ids=["RequestReturn", "Fallback", "Escalation"], escalation_flow_id="Escalation",
                                        slot_type_ids={"yesNo"})
    branch = out["nodes"]["branch"]
    by_name = {e["name"]: e for e in branch["childNodes"]}
    assert by_name["accepted"]["conditions"][0]["operator"] == "eq" and by_name["accepted"]["nodeId"] == "ok"
    assert by_name["rejected"]["conditions"][0]["operator"] == "neq" and by_name["rejected"]["nodeId"] == "no"
    assert any("(D6)" in n and "crossed" in n for n in notes), notes


def test_d6_leaves_a_correct_success_branch_alone():
    out, notes = apply_runtime_contract(_crossed_success_flow(False), role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                                        flow_ids=["RequestReturn", "Fallback", "Escalation"], escalation_flow_id="Escalation",
                                        slot_type_ids={"yesNo"})
    by_name = {e["name"]: e for e in out["nodes"]["branch"]["childNodes"]}
    assert by_name["accepted"]["conditions"][0]["operator"] == "eq"
    assert not any("(D6)" in n for n in notes)


def test_e2_a_not_captured_answer_is_first_checked_for_an_agent_request():
    """Live (2026-09-16): '상담원 연결해 주세요' at the order-number prompt reached
    the escalation in one turn only when the raw utterance was tested with
    {"type": "system", "name": "System.utterance"} contains <word>; the other
    operand spellings were stored but never matched."""
    out, notes = apply_runtime_contract(_intake_flow(), role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                                        flow_ids=["RequestReturn", "RequestAgentFlow", "Fallback", "Escalation"],
                                        escalation_flow_id="Escalation", slot_type_ids={"yesNo"})
    nodes = out["nodes"]
    missing = next(e for e in nodes["askO"]["childNodes"]
                   if any(c.get("operator") == "not_exists" for c in e.get("conditions") or []))
    gate = nodes[missing["nodeId"]]
    assert gate["type"] == "choice"
    word_edges, rest = gate["childNodes"][:-1], gate["childNodes"][-1]
    assert word_edges and all(e["name"].startswith("agentRequest:") for e in word_edges)
    assert all(e["conditions"] == [{"left": {"type": "system", "name": "System.utterance"}, "operator": "contains",
                                    "right": {"type": "constant", "value": e["name"].split(":", 1)[1]}}] for e in word_edges)
    assert "상담원" in {e["name"].split(":", 1)[1] for e in word_edges}          # ko-KR flow → Korean words
    redirect = nodes[word_edges[0]["nodeId"]]
    assert redirect["type"] == "redirect" and redirect["metadata"]["redirect"]["flowId"] == "RequestAgentFlow"
    assert len({e["nodeId"] for e in word_edges}) == 1                         # one redirect per flow
    # anything else still goes to the node's own recovery (R8 made it re-ask)
    recovery = nodes[rest["nodeId"]]
    assert rest["name"] == "notAgentRequest" and recovery["type"] == "basic"
    assert recovery["childNodes"] == [{"nodeId": "askO", "name": "retry"}]
    assert sum("(E2)" in n for n in notes) == 2                                # both capture nodes gated
    # not applied to system flows, and the gate is not duplicated on a second pass
    again, notes2 = apply_runtime_contract(out, role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                                           flow_ids=["RequestReturn", "RequestAgentFlow", "Fallback", "Escalation"],
                                           escalation_flow_id="Escalation", slot_type_ids={"yesNo"})
    assert not any("(E2)" in n for n in notes2)
    _, sys_notes = apply_runtime_contract(_intake_flow(), role="followup", data_requests={"requestReturn": _INTAKE_DOC},
                                          flow_ids=["RequestReturn", "RequestAgentFlow", "Fallback", "Escalation"],
                                          escalation_flow_id="Escalation", slot_type_ids={"yesNo"})
    assert not any("(E2)" in n for n in sys_notes)


# ---------------------------------------------------------------------------
# J2-J5 — a generative_journey that collects values (live, 2026-09-17)
# ---------------------------------------------------------------------------

def _journey_flow(journey_edges=None, journey_cfg=None):
    """RequestReturn with the reason collected by a journey: the model wrote the
    node and (maybe) its exits; the contract must give it dataCapture, the
    captured edge, the agent exit, the KB tool and bounds."""
    cfg = {"prompt": "고객의 반품 사유를 자연스럽게 확인합니다."}
    if journey_cfg:
        cfg.update(journey_cfg)
    edges = journey_edges if journey_edges is not None else [
        {"nodeId": "askP", "name": "done"},                          # what the model meant as "captured"
    ]
    flow = _intake_flow()
    nodes = flow["nodes"]
    nodes["askO"]["childNodes"][0]["nodeId"] = "gj"                  # order captured → journey
    nodes["gj"] = {"nodeId": "gj", "type": "generative_journey",
                   "metadata": {"generativeJourney": cfg}, "childNodes": edges}
    nodes["dr"]["dataRequests"][0]["payload"]["reason"] = "{reason:NLX.Slot}"
    flow["slotTypes"].append({"name": "reason", "type": "reason", "sensitive": False})
    return flow


_REASON_TYPE = {"slotTypeId": "reason", "values": [{"value": v} for v in ("단순변심", "상품불량", "오배송", "파손")]}
_JOURNEY_STEPS = [{"captures": ["reason"], "journey_tools": ["knowledge_base"], "description": "사유 확인"}]


def _apply_journey(flow, **overrides):
    kwargs = dict(role="operation", data_requests={"requestReturn": _INTAKE_DOC},
                  flow_ids=["RequestReturn", "RequestAgentFlow", "Fallback", "Escalation"],
                  escalation_flow_id="Escalation", slot_type_ids={"yesNo", "reason"},
                  slot_type_docs={"reason": _REASON_TYPE}, journey_steps=_JOURNEY_STEPS,
                  kb_name="greencart-faq-kb")
    kwargs.update(overrides)
    return apply_runtime_contract(flow, **kwargs)


def test_j2_journey_gets_data_capture_from_the_plan_with_the_slots_enum_schema():
    out, notes = _apply_journey(_journey_flow())
    cfg = out["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert cfg["dataCapture"]["exitEnabled"] is True
    assert cfg["dataCapture"]["data"] == [{
        "name": "reason", "type": "slot", "required": True,
        "schema": {"type": "string", "enum": ["단순변심", "상품불량", "오배송", "파손"]}}]
    assert any("(J2)" in n for n in notes)


def test_j3_captured_edge_tests_the_slot_and_comes_first():
    """Live: once every required value is captured the journey ends and neither
    System.gjConditionIndex nor node_status success is set — without a slot
    test edge the runtime logged Error NoMessages and fell to Fallback."""
    out, notes = _apply_journey(_journey_flow())
    edges = out["nodes"]["gj"]["childNodes"]
    assert edges[0]["name"] == "captured" and edges[0]["nodeId"] == "askP"
    assert edges[0]["conditions"] == [{"left": {"type": "slot", "name": "reason"}, "operator": "exists"}]
    assert any("(J3)" in n for n in notes)
    # a journey the model already wired with a slot test is left alone
    wired = _journey_flow(journey_edges=[{"nodeId": "askP", "name": "captured",
                                          "conditions": [{"left": {"type": "slot", "name": "reason"}, "operator": "exists"}]}])
    _, notes2 = _apply_journey(wired)
    assert not any("(J3)" in n for n in notes2)


def test_j3_multiple_captures_are_one_edge_with_one_condition_per_slot():
    flow = _journey_flow()
    flow["slotTypes"].append({"name": "preferredDate", "type": "NLX.Date", "sensitive": False})
    steps = [{"captures": ["reason", "preferredDate"], "journey_tools": []}]
    out, _ = _apply_journey(flow, journey_steps=steps)
    captured = out["nodes"]["gj"]["childNodes"][0]
    assert [c["left"]["name"] for c in captured["conditions"]] == ["reason", "preferredDate"]
    schemas = {d["name"]: d["schema"] for d in out["nodes"]["gj"]["metadata"]["generativeJourney"]["dataCapture"]["data"]}
    assert schemas["preferredDate"] == {"type": "string"}


def test_j4_agent_request_exit_condition_is_appended_and_routed():
    """Live (chat 5): '상담원이랑 이야기할게요' inside the journey → gjConditionIndex 1
    → RequestAgentFlow → Escalation → queue, in 1.2 s."""
    out, notes = _apply_journey(_journey_flow(journey_cfg={"exitConditions": [{"name": "reasonCaptured", "prompt": "사유 확정"}]}))
    cfg = out["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert cfg["exitConditions"][-1]["name"] == "agentRequested" and "상담원" in cfg["exitConditions"][-1]["prompt"]
    index = len(cfg["exitConditions"]) - 1
    agent_edge = next(e for e in out["nodes"]["gj"]["childNodes"] if e["name"] == "agentRequested")
    assert agent_edge["conditions"] == [{"left": {"type": "system", "name": "System.gjConditionIndex"},
                                        "operator": "eq", "right": {"type": "constant", "value": index}}]
    assert out["nodes"][agent_edge["nodeId"]]["metadata"]["redirect"]["flowId"] == "RequestAgentFlow"
    assert any("(J4)" in n for n in notes)
    # J (existing) still adds the timeout / failure branches to the escalation redirect
    statuses = {e["name"] for e in out["nodes"]["gj"]["childNodes"]}
    assert {"timeout", "failure"} <= statuses


def test_j5_tools_kb_added_unsupported_dropped_bounds_and_model_defaulted():
    flow = _journey_flow(journey_cfg={"tools": [{"type": "dataRequest", "dataRequest": {"dataRequestId": "requestReturn"}}]})
    out, notes = _apply_journey(flow)
    cfg = out["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert cfg["tools"] == [{"type": "knowledgeBase", "knowledgeBaseId": "{KB:greencart-faq-kb}", "scopeTags": []}]
    assert cfg["maxSteps"] == 8 and cfg["modelType"] == "anthropic.claude-haiku-4-5"
    violations = runtime_contract_violations(_journey_flow(journey_cfg={"tools": [{"type": "mcpFlow", "flowId": "toolX"}]}),
                                             role="operation", flow_ids=["RequestReturn", "Escalation"],
                                             escalation_flow_id="Escalation", slot_type_ids={"yesNo", "reason"},
                                             slot_type_docs={"reason": _REASON_TYPE}, journey_steps=_JOURNEY_STEPS)
    assert any("mcpFlow" in v and "J5" in v for v in violations)


def test_j2_j5_are_idempotent_and_a_journey_without_a_plan_step_is_left_to_the_model():
    out, _ = _apply_journey(_journey_flow())
    again, notes = _apply_journey(out)
    assert again == out and not any("(J" in n for n in notes)
    # no plan step: no dataCapture is invented, the agent exit and bounds still apply
    out2, _ = _apply_journey(_journey_flow(), journey_steps=[])
    cfg = out2["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert "dataCapture" not in cfg and cfg["exitConditions"][-1]["name"] == "agentRequested"


def test_j3_reports_a_journey_that_captures_but_cannot_continue():
    flow = _journey_flow(journey_edges=[])
    violations = runtime_contract_violations(flow, role="operation", flow_ids=["RequestReturn", "Escalation"],
                                             escalation_flow_id="Escalation", slot_type_ids={"yesNo", "reason"},
                                             slot_type_docs={"reason": _REASON_TYPE}, journey_steps=_JOURNEY_STEPS)
    assert any("J3" in v and "no edge to continue" in v for v in violations)


def test_runtime_regex_loosens_separators_of_a_variable_width_pattern():
    """Live (2026-09-17): the phone slot's "^01[0-9]-[0-9]{3,4}-[0-9]{4}$" has a
    variable-width run the skeleton parser does not model; used as written, the
    F1 format check refused "010-2345-6789" because NLX.PhoneNumber delivered
    the value without dashes. Every literal separator becomes optional."""
    import re as _re
    from tools.acxd_runtime_contract import runtime_regex
    loosened = runtime_regex(r"^01[0-9]-[0-9]{3,4}-[0-9]{4}$")
    assert loosened == r"^01[0-9][-. /:]?[0-9]{3,4}[-. /:]?[0-9]{4}$"
    for value in ("010-2345-6789", "01023456789", "010 234 5678"):
        assert _re.match(loosened, value), value
    assert not _re.match(loosened, "02-345-6789")
    # a class keeps its own hyphen; a wildcard dot is not a separator
    assert runtime_regex(r"^[A-Z-]{2}[0-9]+$") == r"^[A-Z-]{2}[0-9]+$"
    assert runtime_regex(r"^[0-9]{10}$") == r"^[0-9]{10}$"
