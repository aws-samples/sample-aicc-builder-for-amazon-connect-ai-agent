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


def test_a_step_naming_a_request_by_another_operation_s_snake_case_id_is_mapped(monkeypatch):
    """Live (AnyClinic, 2026-09-20): ManageAppointment's step called the
    reschedule request by its operation id `reschedule_appointment`; the request
    itself is `rescheduleAppointment`. The raw id was no key of the mapping (the
    keys are the operations' own ids as the OperationSpec spells them), so the
    step reached the generator unmapped and five attempts failed
    DATA_REQUEST_REF_UNDEFINED. The normalized form is resolved as well."""
    operations = {
        "RescheduleAppointment": _Model({"operation_id": "RescheduleAppointment", "http_method": "POST",
                                         "summary": "Reschedule", "input_fields": [], "output_fields": []}),
        "get_appointment": _Model({"operation_id": "get_appointment", "http_method": "POST",
                                   "summary": "Get", "input_fields": [], "output_fields": []}),
    }
    flow_spec = _Model({
        "flows": [{
            "flow_id": "ManageAppointment", "operation_id": "get_appointment", "role": "operation",
            "purpose": "Look up and change an appointment",
            "steps": [
                {"step": 1, "node_type": "data_request", "determinism": "deterministic",
                 "user_confirmed": True},
                {"step": 2, "node_type": "data_request", "determinism": "deterministic",
                 "user_confirmed": True, "data_request_id": "reschedule_appointment"},
            ],
            "slots": [],
        }],
        "guardrails": [], "knowledge_base": {}, "application": {"name": "Clinic", "locales": ["ko-KR"]},
    })
    monkeypatch.setattr(context, "get_all_specs", lambda: operations)
    monkeypatch.setattr(context, "get_infrastructure_spec", lambda: _Model({"project_name": "clinic"}))
    monkeypatch.setattr(context, "get_acxd_flow_spec", lambda _sid=None: flow_spec)
    monkeypatch.setattr(context, "ensure_workspace", lambda: None)
    monkeypatch.setattr(context, "_load_openapi_document", lambda _sid: {})
    monkeypatch.setattr(context, "_load_faq_articles", lambda _sid: [])

    payload = context.build_generation_context("session-2").model_dump()
    request_ids = {d["data_request_id"] for d in payload["data_integrations"]}
    assert request_ids == {"rescheduleAppointment", "getAppointment"}
    steps = payload["flows"][0]["steps"]
    assert steps[0]["data_request_id"] == "getAppointment"          # the flow's own operation
    assert steps[1]["data_request_id"] == "rescheduleAppointment"   # named by snake_case → resolved


def test_non_ascii_field_descriptions_are_rewritten_not_stripped():
    """Live review (2026-09-20): stripping Korean from ``"총액 = unitPrice × quantity (서버 산정)"``
    left ``"= unitPrice quantity ( )"`` for the model to read. A non-ASCII
    description is replaced by one synthesised from the field's machine facts;
    an ASCII description passes through; a field without one stays bare."""
    from tools.acxd_data_request_builder import fields_to_json_schema
    props = fields_to_json_schema([
        {"name": "totalAmount", "type": "number", "description": "총액 = unitPrice × quantity (서버 산정)"},
        {"name": "reservationId", "type": "string", "description": "예약번호 (형식 CL-YYYYMMDD-NNNN)",
         "regex": "^CL-\\d{8}-\\d{4}$"},
        {"name": "status", "type": "string", "description": "예약 상태", "enum_values": ["예약", "취소"]},
        {"name": "note", "type": "string", "description": "Free text from the customer"},
        {"name": "bare", "type": "string"},
    ])["properties"]
    assert props["totalAmount"]["description"] == "totalAmount (number)"
    assert props["reservationId"]["description"] == "reservationId (string); format ^CL-\\d{8}-\\d{4}$"
    assert props["status"]["description"] == "status (string)"          # Korean enum values are not repeated
    assert props["note"]["description"] == "Free text from the customer"
    assert "description" not in props["bare"]
    for prop in props.values():
        assert all(ord(ch) < 0x7F for ch in prop.get("description", ""))


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


def test_response_schema_carries_no_value_constraints():
    """Live (Hanbit, 2026-09-14): a not-found reply's status "" against enum
    [예약, 취소, 완료] failed the whole reply and the caller was escalated."""
    from tools.acxd_data_request_builder import response_json_schema, fields_to_json_schema
    fields = [{"name": "success", "type": "boolean"},
              {"name": "status", "type": "string", "enum_values": ["예약", "취소", "완료"], "max_length": 10},
              {"name": "appointmentId", "type": "string", "regex": "^A\\d{8}$"}]
    request_side = fields_to_json_schema(fields)["properties"]
    assert request_side["status"]["enum"] == ["예약", "취소", "완료"] and request_side["appointmentId"]["pattern"]
    reply = response_json_schema(fields)["properties"]
    assert reply["status"] == {"type": "string"} and reply["appointmentId"] == {"type": "string"}
