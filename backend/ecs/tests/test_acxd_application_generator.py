from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools import acxd_application_generator as generator


class _Context:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self) -> dict:
        return self.payload


def _invoke():
    return getattr(generator.generate_acxd_application, "_raw", generator.generate_acxd_application)


def test_application_generation_refuses_unconfirmed_flow_spec(monkeypatch):
    monkeypatch.setattr(generator, "acxd_flow_spec_ready", lambda: (False, ["flow plan not approved"]))
    result = _invoke()()
    assert result["status"] == "error"
    assert result["problems"] == ["flow plan not approved"]


def test_application_generation_is_patch_only_for_modification_requests(monkeypatch):
    monkeypatch.setattr(generator, "acxd_flow_spec_ready", lambda: (True, []))
    result = _invoke()(modification_request="Change the fallback wording")
    assert result["status"] == "patch_required"
    assert "patch_acxd_asset" in result["problems"][0]


def test_application_generation_builds_all_deterministic_asset_families(monkeypatch):
    spec = {
        "business_profile": {"company_name": "Acme", "language": "en-US"},
        "flows": [
            {"flow_id": "WelcomeFlow", "role": "welcome", "purpose": "welcome"},
            {"flow_id": "FallbackFlow", "role": "fallback", "purpose": "fallback"},
            {"flow_id": "EscalationFlow", "role": "escalation", "purpose": "handoff"},
        ],
        "slot_types": [{"slotTypeId": "OrderStatus", "values": [{"value": "pending"}]}],
        "data_integrations": [{
            "data_request_id": "lookupOrder",
            "operation_ref": "lookup_order",
            "mode": "external",
            "http_method": "POST",
            "request_fields": [{"name": "orderId", "type": "string", "required": True}],
            "response_fields": [{"name": "status", "type": "string"}],
            "purpose": "lookup",
        }],
        "guardrails": [{"name": "PIIFilter", "policy": "mask PII", "action": "mask"}],
        "knowledge_base": {
            "name": "Acme FAQ",
            "articles": [{"question": "Where is my order?", "answer": "Use tracking."}],
        },
        "application": {
            "name": "Acme Assistant",
            "locales": ["en-US"],
            "primary_locale": "en-US",
            "context_variables": [{"name": "customerId", "type": "string"}],
            "environment": "qa",
        },
        "deployment": {"environment": "qa"},
    }
    saved: list[str] = []
    bundle = {
        "flows": [{"flowId": "WelcomeFlow"}],
        "slot_types": [{"slotTypeId": "OrderStatus"}],
        "data_requests": [{"dataRequestId": "lookupOrder"}],
        "guardrails": [{"name": "PIIFilter"}],
        "knowledge_bases": [{"name": "Acme FAQ"}],
        "secrets": [],
        "application": {"name": "Acme Assistant"},
        "context_variables": [{"name": "customerId"}],
        "contact_flows": [],
        "infrastructure": None,
        "lambdas": [],
        "openapi": None,
    }

    monkeypatch.setattr(generator, "acxd_flow_spec_ready", lambda: (True, []))
    monkeypatch.setattr(generator, "get_acxd_spec", lambda: _Context(spec))
    monkeypatch.setattr(generator, "generate_acxd_flows", lambda: {"status": "success", "failed": []})
    monkeypatch.setattr(generator, "_save_asset", lambda _sid, asset_type, _name, _doc, **_kwargs: saved.append(asset_type))
    monkeypatch.setattr(generator, "load_acxd_bundle", lambda _sid: bundle)
    monkeypatch.setattr(generator, "validate_acxd_consistency", lambda _bundle, spec: [])

    result = _invoke()()
    assert result["status"] == "success"
    assert {"acxd_slot_type", "acxd_data_request", "acxd_guardrail", "acxd_knowledge_base", "acxd_application", "acxd_context_variable"} <= set(saved)
    assert result["faq_to_knowledge_base"].startswith("FAQ assets are consumed")
