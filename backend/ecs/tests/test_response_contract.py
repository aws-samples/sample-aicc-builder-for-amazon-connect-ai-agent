"""response_contract — the OpenAPI request/response shapes are the spec's projection."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.response_contract import (  # noqa: E402
    ENVELOPE_FIELD_NAMES,
    enforce_operation_shapes,
    field_to_schema,
    fields_to_object_schema,
    operation_bundles,
    with_envelope,
)
from tools.shape_parity import validate_shape_parity  # noqa: E402

SPEC = {
    "operation_id": "create_reservation", "http_method": "POST", "path": "/tools/create_reservation",
    "success_status_code": 200,
    "input_fields": [
        {"name": "customerName", "field_type": "string", "required": True, "min_length": 2},
        {"name": "quantity", "field_type": "integer", "required": True, "min_value": 1, "max_value": 50},
    ],
    "output_fields": [
        {"name": "reservationId", "field_type": "string", "pattern": "^R-\\d{6}$"},
        {"name": "status", "field_type": "string", "enum_values": ["confirmed", "pending"]},
        {"name": "items", "field_type": "array", "items": {"name": "item", "field_type": "object", "properties": [
            {"name": "sku", "field_type": "string"}, {"name": "price", "field_type": "number", "required": False}]}},
    ],
}


def test_projection_is_recursive_and_carries_constraints():
    schema = fields_to_object_schema(SPEC["output_fields"])
    assert schema["required"] == ["reservationId", "status", "items"]
    assert schema["properties"]["reservationId"] == {"type": "string", "pattern": "^R-\\d{6}$"}
    assert schema["properties"]["status"]["enum"] == ["confirmed", "pending"]
    items = schema["properties"]["items"]["items"]
    assert items["type"] == "object" and items["required"] == ["sku"]
    assert items["properties"]["price"] == {"type": "number"}
    assert field_to_schema({"name": "when", "field_type": "date"}) == {"type": "string", "format": "date"}


def test_envelope_is_prepended_once():
    fields = with_envelope(SPEC["output_fields"] + [{"name": "success", "field_type": "boolean"}])
    names = [f["name"] for f in fields]
    assert names[:3] == ["success", "errorCode", "message"]
    assert names.count("success") == 1
    assert ENVELOPE_FIELD_NAMES == {"success", "errorCode", "message"}


def test_tools_drive_the_bundles_like_the_parity_gate():
    spec = dict(SPEC, tools=[{"tool_id": "lookup", "path": "/tools/lookup", "http_method": "GET",
                              "input_fields": [{"name": "id", "field_type": "string"}],
                              "output_fields": [{"name": "found", "field_type": "boolean"}]}])
    bundles = operation_bundles(spec)
    assert [(b["id"], b["method"], b["path"]) for b in bundles] == [("lookup", "GET", "/tools/lookup")]
    assert operation_bundles(SPEC)[0]["id"] == "create_reservation"


def _doc():
    return {
        "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
        "paths": {"/tools/create_reservation": {"post": {
            "operationId": "create_reservation",
            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateReq"}}}},
            "responses": {
                200: {"description": "business outcome", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Outcome"}}}},
                201: {"description": "created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateRes"}}}},
                500: {"description": "error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Outcome"}}}},
            }}}},
        "components": {"schemas": {
            "CreateReq": {"type": "object", "properties": {"customer_name": {"type": "string", "description": "Who"},
                                                            "quantity": {"type": "string", "description": "How many"}}},
            "Outcome": {"type": "object", "properties": {"success": {"type": "boolean"}, "errorCode": {"type": "string"},
                                                          "message": {"type": "string"}, "missingFields": {"type": "array"}}},
            "CreateRes": {"type": "object", "properties": {"reservationId": {"type": "string", "description": "Reservation id"}}},
        }},
    }


def test_enforcement_makes_parity_hold_and_keeps_descriptions():
    doc = _doc()
    assert validate_shape_parity(SPEC, doc)  # drift before
    doc, changes = enforce_operation_shapes(doc, {"create_reservation": SPEC})
    assert validate_shape_parity(SPEC, doc) == []
    op = doc["paths"]["/tools/create_reservation"]["post"]
    # spec status wins; the second success code is gone; the shared component is untouched
    assert "201" not in op["responses"] and 201 not in op["responses"]
    assert set(doc["components"]["schemas"]["Outcome"]["properties"]) == {"success", "errorCode", "message", "missingFields"}
    response = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" not in response  # inlined because Outcome is shared with the 500 response
    assert list(response["properties"])[:3] == ["success", "errorCode", "message"]
    assert response["properties"]["items"]["items"]["required"] == ["sku"]
    # request: model's description kept where the property survived, wrong type fixed
    request = doc["components"]["schemas"]["CreateReq"]
    assert request["properties"]["quantity"] == {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many"}
    assert "customer_name" not in request["properties"] and "customerName" in request["properties"]
    assert any("extra success response 201 removed" in c for c in changes)


def test_unknown_operation_is_reported_not_invented():
    doc = _doc()
    _, changes = enforce_operation_shapes(doc, {"ghost": dict(SPEC, operation_id="ghost", path="/tools/ghost")})
    assert changes == ["ghost: operation not found in the OpenAPI document (left for review)"]
    assert "/tools/ghost" not in doc["paths"]


def test_tool_field_names_resolve_to_the_operations_fields():
    """Live (SELC, 2026-09-14): a tool listed its fields as NAMES of the
    operation's top-level FieldSpecs; enforce_openapi_contract projected them to
    nothing and rewrote the request schema to properties: {} (13 fields gone),
    and shape parity saw every field as 'string'."""
    from tools.response_contract import operation_bundles, resolve_tool_fields
    spec = {"operation_id": "create_reservation", "http_method": "POST",
            "input_fields": [{"name": "consent", "field_type": "boolean"}, {"name": "quantity", "field_type": "integer"}],
            "output_fields": [{"name": "reservationId", "field_type": "string"}],
            "tools": [{"tool_id": "create_reservation", "input_fields": ["consent", "quantity", "unknownOne"],
                       "output_fields": ["reservationId"]}]}
    bundles = operation_bundles(spec)
    assert [f["name"] for f in bundles[0]["input_fields"]] == ["consent", "quantity", "unknownOne"]
    assert bundles[0]["input_fields"][0]["field_type"] == "boolean"
    assert bundles[0]["input_fields"][2] == {"name": "unknownOne", "field_type": "string"}
    assert resolve_tool_fields(spec, [], "output_fields") == spec["output_fields"]   # empty list → the operation's own
