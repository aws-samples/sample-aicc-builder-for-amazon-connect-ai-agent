"""D9 ACXD runtime-target validation fixtures.

The valid fixture exercises the runtime-target source of truth (ACXDFlowSpec +
canonical ACXD bundle). Each mutation isolates one D9 design-document check.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


N1 = "10000000-0000-4000-8000-000000000001"
N2 = "10000000-0000-4000-8000-000000000002"
N3 = "10000000-0000-4000-8000-000000000003"
N4 = "10000000-0000-4000-8000-000000000004"


class _FlowSpecModel:
    def __init__(self, value: dict):
        self._value = value

    def model_dump(self) -> dict:
        return copy.deepcopy(self._value)


def _simple_flow(flow_id: str, node_type: str = "basic", *, data_request: str | None = None) -> dict:
    nodes = {
        N1: {"nodeId": N1, "type": "start", "childNodes": [{"nodeId": N2}]},
        N2: {
            "nodeId": N2,
            "type": node_type,
            "messages": [{"type": "text", "body": "Continue."}],
            "childNodes": [{"nodeId": N3}],
        },
        N3: {"nodeId": N3, "type": "end"},
    }
    if node_type == "data_request":
        nodes[N2].pop("messages")
        nodes[N2]["dataRequests"] = [data_request]
    elif node_type == "escalate":
        nodes[N2].pop("messages")
        nodes[N2].pop("childNodes")
        nodes.pop(N3)
    return {
        "flowId": flow_id,
        "description": f"{flow_id} flow",
        "aiDescription": f"Handles {flow_id}",
        "mainLanguageCode": "en-US",
        "languageCodes": ["en-US"],
        "nodes": nodes,
    }


def valid_flow_spec() -> dict:
    return {
        "flows": [
            {
                "flow_id": "LookupOrder",
                "operation_id": "lookup_order",
                "role": "operation",
                "purpose": "Look up an order",
                "confirmed": True,
                "steps": [
                    {"step": 1, "description": "Collect order number", "node_type": "user_input", "determinism": "deterministic", "user_confirmed": True},
                    {"step": 2, "description": "Look up order", "node_type": "data_request", "determinism": "deterministic", "user_confirmed": True, "data_request_id": "lookupOrder"},
                ],
                "slots": [
                    {"name": "orderNumber", "type": "OrderNumber", "field_name": "orderNumber", "regex": "^[A-Z0-9]{8}$"},
                    {"name": "priority", "type": "Priority", "field_name": "priority"},
                ],
            },
            {
                "flow_id": "WelcomeFlow", "role": "welcome", "purpose": "Welcome", "confirmed": True,
                "steps": [{"step": 1, "description": "Welcome", "node_type": "basic", "determinism": "deterministic", "user_confirmed": True}],
            },
            {
                "flow_id": "FallbackFlow", "role": "fallback", "purpose": "Fallback", "confirmed": True,
                "steps": [{"step": 1, "description": "Fallback", "node_type": "basic", "determinism": "deterministic", "user_confirmed": True}],
            },
            {
                "flow_id": "EscalationFlow", "role": "escalation", "purpose": "Escalate", "confirmed": True,
                "steps": [{"step": 1, "description": "Escalate", "node_type": "escalate", "determinism": "deterministic", "user_confirmed": True}],
            },
        ],
        "guardrails": [],
        "knowledge_base": {"name": "Support FAQ", "topics": ["order status"]},
        "application": {
            "name": "SupportBot",
            "locales": ["en-US"],
            "primary_locale": "en-US",
            "context_variables": [{"name": "customerId", "type": "string"}],
        },
    }


def valid_bundle() -> dict:
    lookup = {
        "flowId": "LookupOrder",
        "description": "Lookup order flow",
        "aiDescription": "Collects an order number and fetches status",
        "mainLanguageCode": "en-US",
        "languageCodes": ["en-US"],
        "slotTypes": [
            {"name": "orderNumber", "type": "OrderNumber"},
            {"name": "priority", "type": "Priority"},
        ],
        "nodes": {
            N1: {"nodeId": N1, "type": "start", "childNodes": [{"nodeId": N2}]},
            N2: {"nodeId": N2, "type": "user_input", "messages": [{"type": "text", "body": "Order number?"}], "childNodes": [{"nodeId": N3}]},
            N3: {"nodeId": N3, "type": "data_request", "dataRequests": ["lookupOrder"], "childNodes": [{"nodeId": N4}]},
            N4: {"nodeId": N4, "type": "end"},
        },
    }
    contact_flow = {
        "Version": "2019-10-30",
        "StartAction": "AgenticCXStart",
        "Metadata": {
            "acxdBinding": {
                "workspaceId": "workspace-1",
                "applicationId": "application-1",
                "aliasId": "alias-1",
                "speechEngine": "agentic_voice",
                "contextVariables": [{"name": "customerId", "value": "$.Attributes.customerId"}],
                "branches": {
                    "Default": "DefaultExit",
                    "Escalation": "EscalationExit",
                    "Error": "ErrorExit",
                    "IdleChatTimeout": "IdleExit",
                },
            },
        },
        "Actions": [
            {"Identifier": "AgenticCXStart", "Type": "MessageParticipant", "Parameters": {"Text": "$.AgenticCX.ContextVariables.customerId"}, "Transitions": {}},
            {"Identifier": "DefaultExit", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
            {"Identifier": "EscalationExit", "Type": "TransferContactToQueue", "Parameters": {}, "Transitions": {}},
            {"Identifier": "ErrorExit", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
            {"Identifier": "IdleExit", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        ],
    }
    return {
        "flows": [
            lookup,
            _simple_flow("WelcomeFlow"),
            _simple_flow("FallbackFlow"),
            _simple_flow("EscalationFlow", "escalate"),
        ],
        "slot_types": [
            {"slotTypeId": "OrderNumber", "values": [{"value": "placeholder"}], "metadata": {"regex": "^[A-Z0-9]{8}$", "minLength": 8, "maxLength": 8}},
            {"slotTypeId": "Priority", "values": [{"value": "standard"}, {"value": "express"}]},
        ],
        "data_requests": [
            {
                "dataRequestId": "lookupOrder",
                "type": "object",
                "webhook": {"implementation": "external", "method": "POST", "url": "{WEBHOOK_URL}/tools/lookup_order"},
                "requestSchema": {"type": "object", "properties": {"orderNumber": {"type": "string"}}},
                "responseSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
            },
        ],
        "guardrails": [],
        "knowledge_bases": [
            {
                "name": "Support FAQ", "type": "articles", "description": "Support knowledge base",
                "articles": [{"question": {"text": "Where is my order?"}, "responses": [{"type": "text", "body": "Use the order number to track it."}]}],
            },
        ],
        "application": {
            "name": "SupportBot", "description": "Support application",
            "flows": [{"flowId": "LookupOrder"}, {"flowId": "WelcomeFlow"}, {"flowId": "FallbackFlow"}, {"flowId": "EscalationFlow"}],
            "settings": {
                "languageCode": "en-US", "languageCodes": ["en-US"], "languageSettings": [{"languageCode": "en-US"}],
                "defaultFlows": {
                    "welcome": {"flowId": "WelcomeFlow"}, "fallback": {"flowId": "FallbackFlow"},
                    "unknown": {"flowId": "FallbackFlow"}, "escalation": {"flowId": "EscalationFlow"},
                },
            },
        },
        "context_variables": [{"name": "customerId", "type": "string"}],
        "contact_flows": [contact_flow],
    }


OPENAPI = {
    "openapi": "3.0.1",
    "info": {"title": "Support API", "version": "1"},
    "paths": {
        "/tools/lookup_order": {
            "post": {
                "operationId": "lookup_order",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"orderNumber": {"type": "string"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}}}},
            },
        },
    },
}


@pytest.fixture()
def d9_runner(monkeypatch):
    import tools.validate_consistency as validator

    def run(bundle: dict | None = None, flow_spec: dict | None = None, *, classic_mismatches: list[dict] | None = None) -> list[dict]:
        bundle = copy.deepcopy(bundle or valid_bundle())
        flow_spec = copy.deepcopy(flow_spec or valid_flow_spec())
        monkeypatch.setattr(validator, "is_acxd_target", lambda session_id: True)
        monkeypatch.setattr(validator, "get_acxd_flow_spec", lambda session_id=None: _FlowSpecModel(flow_spec))
        monkeypatch.setattr(validator, "load_acxd_bundle", lambda session_id: copy.deepcopy(bundle))
        monkeypatch.setattr(validator, "get_all_specs", lambda: {
            "lookup_order": {
                "input_fields": [
                    {"name": "orderNumber", "pattern": "^[A-Z0-9]{8}$", "min_length": 8, "max_length": 8},
                    {"name": "priority", "enum_values": ["standard", "express"]},
                ],
                "output_fields": [{"name": "status"}],
            },
        })
        monkeypatch.setattr(validator, "_load_d9_openapi_documents", lambda session_id, ignored_bundle: [copy.deepcopy(OPENAPI)])
        monkeypatch.setattr(validator, "_load_d9_faq_documents", lambda session_id, ignored_bundle: [{"question": "Where is my order?", "answer": "Use the order number to track it."}])
        return validator.run_d9_checks("acxd-session", classic_mismatches=classic_mismatches)

    return run


def test_valid_acxd_bundle_passes_all_d9_checks(d9_runner):
    assert d9_runner() == []


@pytest.mark.parametrize(
    ("name", "mutate_bundle", "mutate_spec", "classic_mismatches", "expected_id"),
    [
        ("D9-1", lambda bundle: bundle["flows"][0]["nodes"][N2]["childNodes"].append({"nodeId": "ghost"}), None, None, "D9-1"),
        ("D9-2", None, lambda spec: spec["flows"][0]["steps"][0].update(user_confirmed=False), None, "D9-2"),
        ("D9-3", lambda bundle: bundle["data_requests"][0]["webhook"].update(url="{WEBHOOK_URL}/tools/missing"), None, None, "D9-3"),
        ("D9-4", lambda bundle: bundle["slot_types"][0]["metadata"].update(maxLength=7), None, None, "D9-4"),
        ("D9-5", lambda bundle: bundle["knowledge_bases"][0].update(articles=[]), None, None, "D9-5"),
        ("D9-6", lambda bundle: bundle["contact_flows"][0]["Metadata"]["acxdBinding"]["branches"].update(Escalation="MissingAction"), None, None, "D9-6"),
        ("D9-7", lambda bundle: bundle["flows"][0].update(description="한국어 설명"), None, None, "D9-7"),
        ("D9-8", None, None, [{"operation_id": "lookup_order", "field": "", "asset_type": "iam_permissions", "issue": "Missing invoke permission"}], "D9-8"),
    ],
    ids=["d9-1", "d9-2", "d9-3", "d9-4", "d9-5", "d9-6", "d9-7", "d9-8"],
)
def test_each_d9_check_reports_its_own_id(
    d9_runner, name, mutate_bundle, mutate_spec, classic_mismatches, expected_id,
):
    bundle = valid_bundle()
    flow_spec = valid_flow_spec()
    if mutate_bundle:
        mutate_bundle(bundle)
    if mutate_spec:
        mutate_spec(flow_spec)
    issues = d9_runner(bundle, flow_spec, classic_mismatches=classic_mismatches)
    assert {issue["id"] for issue in issues} == {expected_id}, (name, issues)
    assert all(issue["severity"] == "error" and issue["message"] for issue in issues)


def test_validate_parameter_consistency_appends_d9_only_for_acxd(monkeypatch):
    import tools.validate_consistency as validator

    baseline = {
        "success": True,
        "mismatches": [],
        "summary": "Found 0 mismatches across 1 operations",
        "operations_checked": 1,
    }
    monkeypatch.setattr(validator, "_D1_D8_IMPLEMENTATION", lambda session_id: copy.deepcopy(baseline))
    monkeypatch.setattr(validator, "is_acxd_target", lambda session_id: True)
    monkeypatch.setattr(
        validator,
        "run_d9_checks",
        lambda session_id, *, classic_mismatches: [
            {"id": "D9-1", "severity": "error", "message": "Broken graph", "asset_type": "flow"}
        ],
    )

    acxd = validator._validate_parameter_consistency_impl("acxd-session")
    assert acxd["success"] is False
    assert acxd["mismatches"] == [{
        "id": "D9-1", "severity": "error", "message": "Broken graph",
        "operation_id": "__acxd__", "field": "", "asset_type": "flow",
        "issue": "Broken graph",
    }]

    monkeypatch.setattr(validator, "is_acxd_target", lambda session_id: False)
    monkeypatch.setattr(validator, "run_d9_checks", lambda *args, **kwargs: pytest.fail("D9 must not run for Classic"))
    classic = validator._validate_parameter_consistency_impl("classic-session")
    assert classic["success"] is True
    assert classic["mismatches"] == []
