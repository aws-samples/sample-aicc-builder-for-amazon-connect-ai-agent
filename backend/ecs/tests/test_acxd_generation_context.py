from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_data_request_builder import build_data_request
from tools import acxd_generation_context as context


class _Model:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self) -> dict:
        return self.payload.copy()


def test_generation_context_adapts_classic_specs_openapi_and_faq(monkeypatch):
    operation = _Model({
        "operation_id": "lookup_order",
        "http_method": "POST",
        "summary": "Look up an order",
        "input_fields": [{
            "name": "orderStatus",
            "field_type": "enum",
            "required": True,
            "enum_values": ["pending", "shipped"],
            "pattern": "^[a-z]+$",
            "min_length": 3,
            "max_length": 12,
        }],
        "output_fields": [{"name": "trackingNumber", "field_type": "string"}],
    })
    flow_spec = _Model({
        "flows": [{
            "flow_id": "OrderLookup",
            "operation_id": "lookup_order",
            "role": "operation",
            "purpose": "Look up an order",
            "steps": [{
                "step": 1,
                "node_type": "data_request",
                "determinism": "deterministic",
                "user_confirmed": True,
            }],
            "slots": [{"name": "orderStatus", "type": "text", "field_name": "orderStatus"}],
        }],
        "guardrails": [{"name": "PII", "policy": "mask PII"}],
        "knowledge_base": {"name": "Order FAQ", "topics": ["returns"]},
        "application": {
            "name": "Order Assistant",
            "locales": ["en-US"],
            "primary_locale": "en-US",
            "context_variables": [{"name": "customerId", "type": "string"}],
            "environment": "qa",
        },
    })
    openapi = {
        "openapi": "3.0.1",
        "paths": {
            "/tools/lookup_order": {
                "post": {
                    "operationId": "lookup_order",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LookupRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/LookupResponse"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "LookupRequest": {
                    "type": "object",
                    "required": ["orderStatus"],
                    "properties": {"orderStatus": {"type": "string", "enum": ["pending", "shipped"]}},
                },
                "LookupResponse": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object", "properties": {
                            "trackingNumber": {"type": "string"},
                        }},
                    },
                },
            }
        },
    }

    monkeypatch.setattr(context, "get_all_specs", lambda: {"lookup_order": operation})
    monkeypatch.setattr(context, "get_infrastructure_spec", lambda: _Model({"project_name": "orders"}))
    monkeypatch.setattr(context, "get_acxd_flow_spec", lambda _sid=None: flow_spec)
    monkeypatch.setattr(context, "ensure_workspace", lambda: None)
    monkeypatch.setattr(context, "_load_openapi_document", lambda _sid: openapi)
    monkeypatch.setattr(
        context,
        "_load_faq_articles",
        lambda _sid: [{"question": "Where is my order?", "answer": "Use tracking.", "tags": ["tracking"]}],
    )

    payload = context.build_generation_context("session-1").model_dump()

    assert set(("business_profile", "flows", "slot_types", "data_integrations", "guardrails", "knowledge_base", "application")) <= set(payload)
    integration = payload["data_integrations"][0]
    assert integration["mode"] == "external"
    assert integration["data_request_id"] == "lookupOrder"
    assert integration["http_method"] == "POST"
    assert integration["request_fields"][0]["name"] == "orderStatus"
    # response = the shared envelope + the contract's fields (same projection as the OpenAPI)
    assert [f["name"] for f in integration["response_fields"]] == ["success", "errorCode", "message", "trackingNumber"]
    assert integration["response_fields"][-1] == {"name": "trackingNumber", "type": "string", "required": False}
    assert payload["flows"][0]["steps"][0]["data_request_id"] == "lookupOrder"

    data_request = build_data_request(integration)
    # the generated backend requires its API key in the ACXD target; the value
    # comes from the BackendApiKey secret deploy.sh fills — never inline. The
    # runtime resolves `{Name:NLX.Secret}` (not `{{secrets.Name}}`, which is sent
    # verbatim → 403) and only from the environment blocks (live 2026-09-13).
    secret_header = {"key": "x-api-key", "value": "{ordersBackendApiKey:NLX.Secret}", "sensitive": True}  # project-scoped: secrets are workspace-level
    assert data_request["webhook"] == {
        "implementation": "external",
        "method": "POST",
        "url": "{WEBHOOK_URL}/tools/lookup_order",
        "headers": [secret_header],
        "sendContext": True,
        "environments": {
            "production": {"url": "{WEBHOOK_URL}/tools/lookup_order", "headers": [secret_header]},
            "development": {"url": "{WEBHOOK_URL}/tools/lookup_order", "headers": [secret_header]},
        },
    }
    assert data_request["requestSchema"]["properties"]["orderStatus"]["enum"] == ["pending", "shipped"]

    slot_type = payload["slot_types"][0]
    assert slot_type["values"] == [{"value": "pending"}, {"value": "shipped"}]
    assert slot_type["metadata"]["constraints"] == {
        "regex": "^[a-z]+$", "min_length": 3, "max_length": 12,
    }
    assert payload["knowledge_base"]["articles"][0]["question"] == "Where is my order?"
    assert payload["deployment"]["environment"] == "qa"


def test_context_shim_returns_a_defensive_model_dump():
    shim = context.ACXDGenerationContext({"flows": [{"flow_id": "Welcome"}]})
    first = shim.model_dump()
    first["flows"][0]["flow_id"] = "Changed"
    assert shim.model_dump()["flows"][0]["flow_id"] == "Welcome"


def test_response_schema_requires_only_the_envelope():
    """A not-found or refused outcome legitimately omits the data fields; the
    choice node tells outcomes apart with `exists`. Requiring every output
    field would fail the reply on exactly those outcomes (live contract:
    the service validates the body against responseSchema)."""
    from tools.acxd_data_request_builder import response_json_schema, fields_to_json_schema
    fields = [{"name": "success", "type": "boolean"}, {"name": "errorCode", "type": "string", "required": False},
              {"name": "orderNumber", "type": "string"}, {"name": "totalAmount", "type": "number"}]
    assert fields_to_json_schema(fields)["required"] == ["success", "orderNumber", "totalAmount"]  # request side unchanged
    assert response_json_schema(fields)["required"] == ["success"]
    assert "required" not in response_json_schema([{"name": "orderNumber", "type": "string"}])
