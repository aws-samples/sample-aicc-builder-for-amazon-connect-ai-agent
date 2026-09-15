"""Task 3 tests: deterministic cross-asset validator, good/bad fixture pairs.

Each rule gets (at least) one coherent-bundle case that passes and one
broken-bundle case that emits the expected violation code.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.validate_acxd_consistency import (  # noqa: E402
    Violation,
    format_report,
    validate_acxd_consistency,
)

S, U, C, D, K, G, B, E = (f"c0000000-0000-4000-8000-00000000000{i}" for i in range(8))


def coherent_bundle() -> dict:
    """A fully cross-consistent bundle: 1 app, 2 flows, slots, DR, guardrail, KB."""
    refund_flow = {
        "flowId": "RefundFlow",
        "aiDescription": "Handles refund requests",
        "slotTypes": [
            {"name": "orderNumber", "type": "text"},
            {"name": "productCategory", "type": "ProductCategory"},
        ],
        "contextVariables": [{"name": "customerTier", "type": "text"}],
        "nodes": {
            S: {"nodeId": S, "type": "start", "childNodes": [{"nodeId": U}]},
            U: {"nodeId": U, "type": "user_input",
                "messages": [{"type": "text", "body": "Order number?"}],
                "childNodes": [{"nodeId": D}]},
            D: {"nodeId": D, "type": "data_request",
                "dataRequests": ["getOrderStatus"],
                "childNodes": [{"nodeId": C}]},
            C: {"nodeId": C, "type": "choice",
                # Conditions are what make a choice route. The platform accepts
                # conditionless choices (verified live) and builds them, so the
                # fixture must model a REAL flow, not an accepted-but-broken one.
                "childNodes": [
                    {"nodeId": G, "name": "eligible",
                     "conditions": [{"left": {"type": "slot", "name": "amount"},
                                     "operator": "lt",
                                     "right": {"type": "constant", "value": "500"}}]},
                    {"nodeId": B, "name": "not_eligible",
                     "conditions": [{"left": {"type": "slot", "name": "amount"},
                                     "operator": "gte",
                                     "right": {"type": "constant", "value": "500"}}]}]},
            G: {"nodeId": G, "type": "generative_text",
                "metadata": {"generativeText": {"prompt": "Explain the refund"}},
                "childNodes": [{"nodeId": E}]},
            B: {"nodeId": B, "type": "escalate"},
            E: {"nodeId": E, "type": "end"},
        },
    }
    kb_flow = {
        "flowId": "FaqFlow",
        "nodes": {
            S: {"nodeId": S, "type": "start", "childNodes": [{"nodeId": K}]},
            K: {"nodeId": K, "type": "knowledge_base",
                "metadata": {"knowledgeBase": {"knowledgeBaseId": "{KB:Product FAQ}", "name": "Product FAQ"}},
                "childNodes": [{"nodeId": E}]},
            E: {"nodeId": E, "type": "end"},
        },
    }
    return {
        "flows": [refund_flow, kb_flow],
        "slot_types": [{
            "slotTypeId": "ProductCategory",
            "values": [{"value": "Electronics"}],
        }],
        "data_requests": [{
            "dataRequestId": "getOrderStatus",
            "type": "object",
            "webhook": {"implementation": "external",
                        "url": "{WEBHOOK_URL}/tools/get_order_status"},
        }],
        "guardrails": [{
            "name": "Abuse Filter",
            "trigger": "input",
            "rules": [{
                "name": "Abusive language",
                "detection": {"method": "keyword", "keywords": ["abuse"]},
                "enforcement": {"action": "route",
                                "behavior": {"flowId": "RefundFlow"}},
            }],
            "fallbackBehavior": {"type": "routeToFlow", "flowId": "FaqFlow"},
        }],
        "knowledge_bases": [{
            "name": "Product FAQ",
            "type": "articles",
            "articles": [{"question": {"text": "Q?"},
                          "responses": [{"type": "text", "body": "A."}]}],
        }],
        "application": {
            "name": "RefundBot",
            "settings": {
                "defaultFlows": {
                    # all four system events must be routed
                    "welcome": {"flowId": "FaqFlow"},
                    "fallback": {"flowId": "FaqFlow"},
                    "escalation": {"flowId": "RefundFlow"},
                    "unknown": {"flowId": "FaqFlow",
                                "knowledgeBaseId": "{KB:Product FAQ}"},
                },
                "lifecycleHooks": {"escalation": "RefundFlow"},
                "guardrails": [{"guardrailId": "{GUARDRAIL:Abuse Filter}"}],
            },
            "flows": [{"flowId": "RefundFlow"}, {"flowId": "FaqFlow"}],
        },
        "contact_flows": [{
            "name": "InboundMain",
            "acxdBinding": {
                "applicationName": "RefundBot",
                "contextVariables": [{"key": "customerTier", "value": "$.Attributes.tier"}],
                "branches": {"Default": "disconnect", "Error": "queue",
                             "Escalation": "queue", "IdleTimeout": "disconnect"},
            },
        }],
    }


def matching_spec() -> dict:
    """ACXDSpec dump whose confirmed decisions match coherent_bundle()."""
    return {
        "flows": [{
            "flow_id": "RefundFlow",
            "purpose": "refunds",
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
        }, {
            "flow_id": "FaqFlow",
            "purpose": "faq",
            "steps": [
                {"step": 1, "description": "answer from KB", "node_type": "knowledge_base",
                 "determinism": "generative", "user_confirmed": True},
            ],
        }],
    }


def codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


# ---------------------------------------------------------------------------
# good fixtures
# ---------------------------------------------------------------------------

def test_coherent_bundle_passes():
    assert validate_acxd_consistency(coherent_bundle()) == []


def test_coherent_bundle_with_spec_passes():
    assert validate_acxd_consistency(coherent_bundle(), spec=matching_spec()) == []


def test_empty_bundle_passes():
    assert validate_acxd_consistency({}) == []


def test_raw_external_ids_are_trusted():
    b = coherent_bundle()
    # Raw workspace IDs (not placeholders) pass through untouched.
    b["flows"][1]["nodes"][K]["metadata"]["knowledgeBase"]["knowledgeBaseId"] = "kb-preexisting"
    b["application"]["settings"]["guardrails"] = [{"guardrailId": "g-preexisting"}]
    b["application"]["settings"]["defaultFlows"]["unknown"]["knowledgeBaseId"] = "kb-preexisting"
    assert validate_acxd_consistency(b) == []


# ---------------------------------------------------------------------------
# bad fixtures — one per rule
# ---------------------------------------------------------------------------

def _mutated(mutate):
    b = coherent_bundle()
    mutate(b)
    return b


@pytest.mark.parametrize("mutate,expected_code", [
    # schema delegation
    (lambda b: b["flows"][0].pop("flowId"), "SCHEMA"),
    (lambda b: b["data_requests"][0]["webhook"].pop("url"), "SCHEMA"),
    # graph integrity
    (lambda b: b["flows"][0]["nodes"][S].update(type="basic"), "FLOW_NO_START"),
    (lambda b: b["flows"][0]["nodes"][U].update(type="start"), "FLOW_MULTI_START"),
    (lambda b: (b["flows"][1]["nodes"][E].update(type="basic"),
                b["flows"][1]["nodes"][E].update(childNodes=[{"nodeId": K}])),
     "FLOW_NO_TERMINAL"),
    (lambda b: b["flows"][0]["nodes"][U]["childNodes"].append({"nodeId": "ghost"}),
     "FLOW_DANGLING_CHILD"),
    (lambda b: b["flows"][0]["nodes"].update(
        orphan={"nodeId": "orphan", "type": "basic",
                "messages": [{"type": "text", "body": "hi"}]}),
     "FLOW_UNREACHABLE_NODE"),
    (lambda b: b["flows"][0]["nodes"][U].update(nodeId="wrong-id"),
     "FLOW_NODEID_MISMATCH"),
    # uniqueness
    (lambda b: b["flows"].append(copy.deepcopy(b["flows"][0])), "DUP_FLOW_ID"),
    (lambda b: b["slot_types"].append(copy.deepcopy(b["slot_types"][0])),
     "DUP_SLOT_TYPE_ID"),
    (lambda b: b["data_requests"].append(copy.deepcopy(b["data_requests"][0])),
     "DUP_DATA_REQUEST_ID"),
    (lambda b: b["guardrails"].append(copy.deepcopy(b["guardrails"][0])),
     "DUP_GUARDRAIL_NAME"),
    (lambda b: b["knowledge_bases"].append(copy.deepcopy(b["knowledge_bases"][0])),
     "DUP_KB_NAME"),
    # cross references
    (lambda b: b["flows"][0]["slotTypes"][1].update(type="UnknownType"),
     "SLOT_REF_UNDEFINED"),
    (lambda b: b["flows"][0]["nodes"][D].update(dataRequests=["nosuchDR"]),
     "DATA_REQUEST_REF_UNDEFINED"),
    (lambda b: b["flows"][1]["nodes"][K]["metadata"]["knowledgeBase"].update(
        knowledgeBaseId="{KB:No Such KB}"), "KB_REF_UNDEFINED"),
    (lambda b: b["guardrails"][0]["rules"][0]["enforcement"]["behavior"].update(
        flowId="GhostFlow"), "GUARDRAIL_FLOW_REF_UNDEFINED"),
    (lambda b: b["guardrails"][0]["fallbackBehavior"].update(flowId="GhostFlow"),
     "GUARDRAIL_FLOW_REF_UNDEFINED"),
    (lambda b: b["application"]["flows"].append({"flowId": "GhostFlow"}),
     "APP_FLOW_REF_UNDEFINED"),
    (lambda b: b["application"]["settings"]["defaultFlows"].update(
        escalation={"flowId": "GhostFlow"}), "APP_FLOW_REF_UNDEFINED"),
    (lambda b: b["application"]["settings"]["lifecycleHooks"].update(
        conversationEnd="GhostFlow"), "APP_FLOW_REF_UNDEFINED"),
    (lambda b: b["application"]["settings"]["defaultFlows"]["unknown"].update(
        knowledgeBaseId="{KB:No Such KB}"), "KB_REF_UNDEFINED"),
    (lambda b: b["application"]["settings"]["guardrails"].append(
        {"guardrailId": "{GUARDRAIL:No Such}"}), "APP_GUARDRAIL_REF_UNDEFINED"),
    # contact flow binding
    (lambda b: b["contact_flows"][0].pop("acxdBinding"), "CONTACT_FLOW_NO_BINDING"),
    (lambda b: b["contact_flows"][0]["acxdBinding"].update(
        applicationName="OtherBot"), "CONTACT_FLOW_APP_MISMATCH"),
    (lambda b: b["contact_flows"][0]["acxdBinding"].update(
        contextVariables=[{"key": f"k{i}"} for i in range(11)]),
     "CONTACT_FLOW_CTXVARS_EXCEEDED"),
    (lambda b: b["contact_flows"][0]["acxdBinding"]["branches"].pop("Escalation"),
     "CONTACT_FLOW_BRANCH_MISSING"),
])
def test_broken_bundles_emit_expected_code(mutate, expected_code):
    violations = validate_acxd_consistency(_mutated(mutate))
    assert expected_code in codes(violations), format_report(violations)


# ---------------------------------------------------------------------------
# determinism contract
# ---------------------------------------------------------------------------

def test_determinism_unconfirmed_step():
    spec = matching_spec()
    spec["flows"][0]["steps"][3]["user_confirmed"] = False
    violations = validate_acxd_consistency(coherent_bundle(), spec=spec)
    assert "DETERMINISM_UNCONFIRMED" in codes(violations)
    # ... and the generative node is now also unauthorized
    assert "DETERMINISM_UNAUTHORIZED_GENERATIVE" in codes(violations)


def test_determinism_missing_confirmed_node():
    spec = matching_spec()
    spec["flows"][0]["steps"].append({
        "step": 5, "description": "final menu", "node_type": "user_choice",
        "determinism": "deterministic", "user_confirmed": True,
    })
    violations = validate_acxd_consistency(coherent_bundle(), spec=spec)
    assert "DETERMINISM_MISSING_NODE" in codes(violations)


def test_determinism_accepts_a_templated_basic_for_a_generative_text_step():
    """M2 (live): a generative_text node sends nothing, so the runtime contract
    realises a confirmed 'generative_text' result message as a deterministic
    templated basic. A node MORE deterministic than planned is not a missing
    node — while a generative node nobody confirmed is still unauthorized."""
    bundle = coherent_bundle()
    bundle["flows"][0]["nodes"][G]["type"] = "basic"
    bundle["flows"][0]["nodes"][G]["messages"] = [{"body": "Refund: {refund.amount:NLX.Variable}"}]
    bundle["flows"][0]["nodes"][G].pop("metadata", None)
    violations = validate_acxd_consistency(bundle, spec=matching_spec())
    assert "DETERMINISM_MISSING_NODE" not in codes(violations)
    assert "DETERMINISM_UNAUTHORIZED_GENERATIVE" not in codes(violations)


def test_determinism_does_not_hold_builder_owned_system_flows_to_their_plan_steps():
    """System flows are built deterministically from the routing contract; the
    interview's step list for them (an old plan even said 'generative_journey'
    for intent routing) describes behaviour to the user and is not a design the
    generator follows, so it must not fail the flow the builder produced."""
    from tools.acxd_system_flows import build_system_flow

    spec = matching_spec()
    spec["flows"].append({
        "flow_id": "WelcomeFlow", "role": "welcome", "purpose": "greet",
        "steps": [
            {"step": 1, "description": "greet", "node_type": "basic",
             "determinism": "deterministic", "user_confirmed": True},
            {"step": 2, "description": "route", "node_type": "generative_journey",
             "determinism": "generative", "user_confirmed": True},
            {"step": 3, "description": "later", "node_type": "generative_text",
             "determinism": "generative", "user_confirmed": False},
        ],
    })
    bundle = coherent_bundle()
    bundle["flows"].append(build_system_flow("welcome", {
        "application": {"locales": ["en-US"]}, "flows": spec["flows"]}))
    found = {v.code for v in validate_acxd_consistency(bundle, spec=spec)
             if "WelcomeFlow" in v.path}
    assert not found & {"DETERMINISM_MISSING_NODE", "DETERMINISM_UNCONFIRMED",
                        "DETERMINISM_UNAUTHORIZED_GENERATIVE"}


def test_determinism_unauthorized_generative_node():
    bundle = coherent_bundle()
    spec = matching_spec()
    # The interview only confirmed generative_text; sneak in generative_task.
    nodes = bundle["flows"][0]["nodes"]
    nodes[G]["type"] = "generative_task"
    violations = validate_acxd_consistency(bundle, spec=spec)
    assert "DETERMINISM_UNAUTHORIZED_GENERATIVE" in codes(violations)


def test_spec_plan_without_generated_flow_is_not_an_error():
    spec = matching_spec()
    spec["flows"].append({"flow_id": "NotYetGenerated", "purpose": "later",
                          "steps": [{"step": 1, "description": "x",
                                     "node_type": "basic",
                                     "determinism": "deterministic",
                                     "user_confirmed": False}]})
    assert validate_acxd_consistency(coherent_bundle(), spec=spec) == []


# ---------------------------------------------------------------------------
# report formatting
# ---------------------------------------------------------------------------

def test_format_report_ok_and_violations():
    assert "OK" in format_report([])
    report = format_report(validate_acxd_consistency(
        _mutated(lambda b: b["flows"][0]["nodes"][U]["childNodes"].append(
            {"nodeId": "ghost"}))))
    assert "FLOW_DANGLING_CHILD" in report
    assert "ghost" in report


def test_platform_accepted_but_broken_flows_are_caught():
    """The platform builds semantically broken flows with zero issues.

    Verified live 2026-09-08 against a real workspace: a probe application whose
    flows had dangling edges, no start node, conditionless choices, promptless
    generative nodes and empty message bodies reached status=BUILT with
    issues=null. Our validator is therefore the only gate — the user's symptom
    ("연결이 안 되어 있고 테스트도 안돼요", canvas showing "Generate {} Unnamed")
    came from exactly these shapes deploying successfully.
    """
    N = "e0000000-0000-4000-8000-00000000000%d"
    flow = {"flowId": "brokenShapes", "nodes": {
        N % 1: {"nodeId": N % 1, "type": "start", "childNodes": [{"nodeId": N % 2}]},
        # promptless generative node
        N % 2: {"nodeId": N % 2, "type": "generative_text",
                "metadata": {"generativeText": {"name": "x"}},
                "childNodes": [{"nodeId": N % 3}]},
        # conditionless choice
        N % 3: {"nodeId": N % 3, "type": "choice",
                "childNodes": [{"nodeId": N % 4, "name": "a"},
                               {"nodeId": N % 5, "name": "b"}]},
        # empty message
        N % 4: {"nodeId": N % 4, "type": "basic", "messages": [{"type": "text", "body": "  "}],
                "childNodes": [{"nodeId": N % 5}]},
        N % 5: {"nodeId": N % 5, "type": "end"},
    }}
    codes = {v.code for v in validate_acxd_consistency({"flows": [flow]}, spec={})}
    assert "FLOW_GENERATIVE_NO_PROMPT" in codes
    assert "FLOW_CHOICE_NO_CONDITIONS" in codes
    assert "FLOW_EMPTY_MESSAGE" in codes


def test_user_selected_choice_needs_no_conditions():
    """A choice with a configured source routes by the caller's selection."""
    N = "e1000000-0000-4000-8000-00000000000%d"
    flow = {"flowId": "pickList", "nodes": {
        N % 1: {"nodeId": N % 1, "type": "start", "childNodes": [{"nodeId": N % 2}]},
        N % 2: {"nodeId": N % 2, "type": "choice",
                "metadata": {"choice": {"source": "slotType", "showChoices": True}},
                "childNodes": [{"nodeId": N % 3, "name": "wall"},
                               {"nodeId": N % 3, "name": "stand"}]},
        N % 3: {"nodeId": N % 3, "type": "end"},
    }}
    codes = {v.code for v in validate_acxd_consistency({"flows": [flow]}, spec={})}
    assert "FLOW_CHOICE_NO_CONDITIONS" not in codes


def test_a_planned_redirect_target_must_be_wired():
    """Live (GAON, 2026-09-14): six regenerations sent the 'order not found →
    search by customer info' step to EscalationFlow; only the plan knows the
    intended target, so the interview records it and the gate checks it."""
    import copy
    bundle = coherent_bundle()
    spec = matching_spec()
    spec["flows"][0]["steps"].append({"step": 5, "description": "not found → customer-info search",
                                      "node_type": "redirect", "determinism": "deterministic",
                                      "user_confirmed": True, "redirect_flow_id": "SearchByCustomer"})
    flow = next(f for f in bundle["flows"] if f["flowId"] == "RefundFlow")
    redirects = {((n.get("metadata") or {}).get("redirect") or {}).get("flowId")
                 for n in flow["nodes"].values() if n.get("type") == "redirect"}
    assert "SearchByCustomer" not in redirects
    problems = [str(v) for v in validate_acxd_consistency(bundle, spec=spec)]
    assert any("DETERMINISM_REDIRECT_TARGET" in p and "SearchByCustomer" in p for p in problems), problems

    wired = copy.deepcopy(bundle)
    wired_flow = next(f for f in wired["flows"] if f["flowId"] == "RefundFlow")
    end_id = next(nid for nid, n in wired_flow["nodes"].items() if n.get("type") == "end")
    wired_flow["nodes"]["b0000000-0000-4000-8000-000000000099"] = {
        "nodeId": "b0000000-0000-4000-8000-000000000099", "type": "redirect",
        "metadata": {"redirect": {"type": "flow", "flowId": "SearchByCustomer"}},
        "childNodes": [{"nodeId": end_id, "name": "next"}]}
    start = next(n for n in wired_flow["nodes"].values() if n.get("type") == "start")
    start["childNodes"].append({"nodeId": "b0000000-0000-4000-8000-000000000099", "name": "alt"})
    problems = [str(v) for v in validate_acxd_consistency(wired, spec=spec)]
    assert not any("DETERMINISM_REDIRECT_TARGET" in p for p in problems)
