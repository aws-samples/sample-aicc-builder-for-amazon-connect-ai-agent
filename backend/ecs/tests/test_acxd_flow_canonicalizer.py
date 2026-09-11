"""acxd_flow_canonicalizer — every encoding seen in live flows maps onto the SDK contract.

GetFlow of a deployed Harbor Bank flow (2026-09-11) returned `user_input` nodes
with `messages: []` / `metadata: {}` and a define without `name`: the SDK
serializer had dropped every key it did not know. These tests pin the mapping.
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_flow_canonicalizer import (  # noqa: E402
    SDK_METADATA_KEYS,
    SDK_NODE_KEYS,
    canonicalize_flow,
)
from tools.validate_acxd_flow import validate_acxd_asset  # noqa: E402

N = [f"a0000000-0000-4000-8000-00000000000{i}" for i in range(10)]


def _flow(nodes, **extra):
    return {"flowId": "Probe", "slotTypes": [], "contextVariables": [], "nodes": nodes, **extra}


def _sdk_only(flow):
    for node in flow["nodes"].values():
        assert set(node) <= SDK_NODE_KEYS, set(node) - SDK_NODE_KEYS
        assert set(node.get("metadata", {})) <= SDK_METADATA_KEYS


def test_slot_capture_in_metadata_userinput_becomes_user_choice_with_choice_config():
    """Harbor Bank: metadata.userInput.{slotName,messages} — dropped by the SDK."""
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "user_input",
               "metadata": {"userInput": {"slotName": "cardLast", "messages": [{"body": "Last 4 digits?"}]}},
               "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "end"},
    }, slotTypes=[{"name": "cardLast", "type": "cardLast"}])
    out = canonicalize_flow(flow)
    node = out.flow["nodes"][N[2]]
    assert node["type"] == "user_choice"
    assert node["messages"] == [{"type": "text", "body": "Last 4 digits?"}]
    assert node["metadata"] == {"choice": {"source": "slotType", "slotTypeId": "cardLast"}}
    _sdk_only(out.flow)
    assert validate_acxd_asset("flow", out.flow) == []


def test_top_level_slot_object_and_userchoice_options():
    """SELC: top-level `slot` + metadata.choices / metadata.userChoice.options."""
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "user_choice", "messages": [{"body": "Which type?", "type": "text"}],
               "slot": {"name": "productType", "slotTypeId": "productType"},
               "metadata": {"maxRetries": 2, "choices": [{"label": "A", "value": "A"}]},
               "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "user_input", "messages": [{"body": "Where?", "type": "text"}],
               "metadata": {"userChoice": {"slotName": "installLocationType", "options": [{"value": "home"}]}},
               "childNodes": [{"nodeId": N[4]}]},
        N[4]: {"nodeId": N[4], "type": "end"},
    })
    out = canonicalize_flow(flow)
    n2, n3 = out.flow["nodes"][N[2]], out.flow["nodes"][N[3]]
    assert n2["metadata"]["choice"] == {"source": "slotType", "slotTypeId": "productType", "showChoices": True}
    assert "slot" not in n2 and "choices" not in n2["metadata"]
    assert n3["type"] == "user_choice"
    assert n3["metadata"]["choice"]["slotTypeId"] == "installLocationType"
    # both slots are now attached to the flow
    assert {s["name"] for s in out.flow["slotTypes"]} == {"productType", "installLocationType"}
    # the dropped retry limit is reported, not silently lost
    assert any("maxRetries" in p for p in out.problems)
    _sdk_only(out.flow)


def test_define_variants_map_to_name_value_and_extra_assignments_chain():
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "define",
               "metadata": {"define": {"variableName": "attemptCount", "value": {"type": "constant", "value": 1}}},
               "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "define",
               "metadata": {"define": {"assignments": [
                   {"variable": "escalationResult", "value": "requested"},
                   {"variable": "failReason", "value": {"type": "context", "name": "failReason"}},
                   {"variable": "customerName", "value": {"type": "context", "name": "customerName"}}]}},
               "childNodes": [{"nodeId": N[4]}]},
        N[4]: {"nodeId": N[4], "type": "end"},
    })
    out = canonicalize_flow(flow)
    nodes = out.flow["nodes"]
    assert nodes[N[2]]["metadata"]["define"] == {"name": "attemptCount", "value": {"type": "constant", "value": 1}}
    assert nodes[N[3]]["metadata"]["define"] == {"name": "escalationResult", "value": {"type": "constant", "value": "requested"}}
    # two extra assignments → two chained define nodes ending at the original child
    chain = [nodes[N[3]]["childNodes"][0]["nodeId"]]
    chain.append(nodes[chain[0]]["childNodes"][0]["nodeId"])
    assert [nodes[c]["metadata"]["define"]["name"] for c in chain] == ["failReason", "customerName"]
    assert nodes[chain[1]]["childNodes"] == [{"nodeId": N[4]}]
    v4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    assert all(v4.match(c) for c in chain)
    assert validate_acxd_asset("flow", out.flow) == []


def test_messages_under_metadata_and_escalate_context_updates():
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "basic", "metadata": {"messages": [{"body": "Hello"}]},
               "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "escalate", "messages": [{"body": "Transferring", "type": "text"}],
               "metadata": {"contextUpdates": [{"name": "failReason", "value": "price"}], "escalate": {"queue": "x"}}},
    })
    out = canonicalize_flow(flow)
    assert out.flow["nodes"][N[2]]["messages"] == [{"type": "text", "body": "Hello"}]
    assert "metadata" not in out.flow["nodes"][N[2]]
    assert out.flow["nodes"][N[3]]["metadata"] == {"stateModifications": [
        {"type": "context", "name": "failReason", "modification": "set", "value": {"type": "constant", "value": "price"}}]}
    assert validate_acxd_asset("flow", out.flow) == []


def test_data_request_id_moves_from_metadata_to_dataRequests():
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "data_request", "dataRequests": ["getPrice"],
               "metadata": {"dataRequest": {"dataRequestId": "getPrice"}}, "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "end"},
    })
    out = canonicalize_flow(flow)
    assert out.flow["nodes"][N[2]]["dataRequests"] == [{"dataRequestId": "getPrice"}]
    assert "metadata" not in out.flow["nodes"][N[2]]


def test_placeholders_become_nlx_syntax_and_unknown_names_are_reported():
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "data_request", "dataRequests": [{"dataRequestId": "getPrice"}],
               "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "basic", "messages": [{"type": "text",
               "body": "{{productType}} costs {unitPrice}원, total {getPrice.total}. {{ghost}} {System.utterance} {KB:FAQ}"}],
               "childNodes": [{"nodeId": N[4]}]},
        N[4]: {"nodeId": N[4], "type": "end"},
    }, slotTypes=[{"name": "productType", "type": "productType"}],
       contextVariables=[{"name": "unitPrice", "type": "text"}])
    out = canonicalize_flow(flow)
    body = out.flow["nodes"][N[3]]["messages"][0]["body"]
    assert body == ("{productType:NLX.Slot} costs {unitPrice:NLX.Variable}원, total "
                    "{getPrice.total:NLX.Variable}. {{ghost}} {System.utterance} {KB:FAQ}")
    assert any("ghost" in p for p in out.problems)


def test_text_typed_boolean_variables_and_string_constants_become_booleans():
    """SELC 2-1: `found` typed text, compared with "true" — a branch that can never be taken."""
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "choice", "childNodes": [
            {"nodeId": N[3], "name": "found", "conditions": [{"left": {"type": "variable", "name": "found"},
                                                              "operator": "eq", "right": {"type": "constant", "value": "true"}}]},
            {"nodeId": N[4], "name": "not found", "conditions": [{"left": {"type": "variable", "name": "found"},
                                                                  "operator": "eq", "right": {"type": "constant", "value": "false"}}]}]},
        N[3]: {"nodeId": N[3], "type": "end"},
        N[4]: {"nodeId": N[4], "type": "end"},
    }, contextVariables=[{"name": "found", "type": "text"}])
    out = canonicalize_flow(flow)
    assert out.flow["contextVariables"] == [{"name": "found", "type": "boolean"}]
    rights = [c["conditions"][0]["right"]["value"] for c in out.flow["nodes"][N[2]]["childNodes"]]
    assert rights == [True, False]


def test_canonical_input_is_left_alone():
    flow = _flow({
        N[1]: {"nodeId": N[1], "type": "start", "childNodes": [{"nodeId": N[2]}]},
        N[2]: {"nodeId": N[2], "type": "user_choice", "messages": [{"type": "text", "body": "Order number?"}],
               "metadata": {"choice": {"source": "slotType", "slotTypeId": "text"}}, "childNodes": [{"nodeId": N[3]}]},
        N[3]: {"nodeId": N[3], "type": "end"},
    }, slotTypes=[{"name": "orderNumber", "type": "text"}])
    out = canonicalize_flow(flow)
    assert out.changes == [] and out.problems == []
    assert out.flow == flow
