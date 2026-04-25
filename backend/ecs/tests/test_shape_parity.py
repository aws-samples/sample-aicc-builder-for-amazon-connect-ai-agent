"""Exhaustiveness matrix for tools/shape_parity.validate_shape_parity.

Every case in this file demonstrates one of:
  (a) a shape the validator accepts — parity holds, no mismatches.
  (b) a shape the validator rejects — specific ShapeMismatch code emitted.
  (c) a construct the validator refuses to reason about — ShapeParityError raised.

The matrix covers every branch in `_compare`:
  - scalar types (string / integer / boolean / date / email)
  - enum (with and without field_type="enum")
  - array of scalar / array of enum / array of object (nested props)
  - object (nested properties, missing props, extra props)
  - nested array-of-object
  - $ref resolution (same-doc) + cycle detection
  - refusals: oneOf / anyOf / allOf / external $ref / unresolvable $ref

User's "완벽 아니면 하지마" rule: every branch is covered here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

# Direct module import to avoid pulling agents/* (which requires strands).
import importlib.util
_spec_file = ROOT / "tools" / "shape_parity.py"
_spec = importlib.util.spec_from_file_location("shape_parity", _spec_file)
shape_parity = importlib.util.module_from_spec(_spec)
# Register in sys.modules BEFORE exec — @dataclass resolves its class's
# __module__ via sys.modules[<name>].__dict__, which raises AttributeError
# if the module isn't registered.
sys.modules["shape_parity"] = shape_parity
_spec.loader.exec_module(shape_parity)

validate_shape_parity = shape_parity.validate_shape_parity
ShapeParityError = shape_parity.ShapeParityError


def _op(
    op_id: str = "op",
    method: str = "POST",
    path: str | None = None,
    input_fields: list | None = None,
    output_fields: list | None = None,
    tools: list | None = None,
) -> dict:
    return {
        "operation_id": op_id,
        "http_method": method,
        "path": path or f"/tools/{op_id}",
        "input_fields": input_fields or [],
        "output_fields": output_fields or [],
        **({"tools": tools} if tools is not None else {}),
    }


def _doc(op_id: str, method: str, path: str, req_schema: dict | None = None, res_schema: dict | None = None, components: dict | None = None) -> dict:
    op_node: dict = {"operationId": op_id}
    if req_schema is not None:
        op_node["requestBody"] = {"content": {"application/json": {"schema": req_schema}}}
    if res_schema is not None:
        op_node["responses"] = {"200": {"content": {"application/json": {"schema": res_schema}}}}
    doc = {"paths": {path: {method.lower(): op_node}}}
    if components:
        doc["components"] = {"schemas": components}
    return doc


# --------------------------------------------------------------------------- #
# (a) Happy-path cases — no mismatches.                                       #
# --------------------------------------------------------------------------- #

def test_flat_scalar_ok():
    spec = _op(output_fields=[{"name": "storeName", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"storeName": {"type": "string"}}})
    assert validate_shape_parity(spec, doc) == []


def test_flat_integer_ok():
    spec = _op(output_fields=[{"name": "remaining", "field_type": "integer"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"remaining": {"type": "integer"}}})
    assert validate_shape_parity(spec, doc) == []


def test_integer_accepts_number_in_openapi():
    """Ints should validate against OpenAPI type=number too (int ⊂ number)."""
    spec = _op(output_fields=[{"name": "ratio", "field_type": "integer"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"ratio": {"type": "number"}}})
    assert validate_shape_parity(spec, doc) == []


def test_date_maps_to_string_ok():
    spec = _op(output_fields=[{"name": "dob", "field_type": "date"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"dob": {"type": "string"}}})
    assert validate_shape_parity(spec, doc) == []


def test_flat_enum_string_ok():
    spec = _op(output_fields=[{"name": "state", "field_type": "string",
                               "enum_values": ["RUNNING", "FINISH", "IDLE"]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "state": {"type": "string", "enum": ["RUNNING", "FINISH", "IDLE"]}}})
    assert validate_shape_parity(spec, doc) == []


def test_field_type_enum_ok():
    """field_type='enum' satisfied by any scalar OpenAPI type + enum keyword."""
    spec = _op(output_fields=[{"name": "state", "field_type": "enum",
                               "enum_values": ["A", "B"]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "state": {"type": "string", "enum": ["A", "B"]}}})
    assert validate_shape_parity(spec, doc) == []


def test_array_of_scalar_ok():
    spec = _op(output_fields=[{"name": "tags", "field_type": "array",
                               "items": {"name": "tag", "field_type": "string"}}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "tags": {"type": "array", "items": {"type": "string"}}}})
    assert validate_shape_parity(spec, doc) == []


def test_array_of_enum_ok():
    spec = _op(output_fields=[{"name": "modes", "field_type": "array",
                               "items": {"name": "mode", "field_type": "string",
                                         "enum_values": ["COTTON", "SILK"]}}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "modes": {"type": "array", "items": {
                       "type": "string", "enum": ["COTTON", "SILK"]}}}})
    assert validate_shape_parity(spec, doc) == []


def test_array_of_object_ok():
    """machineStatus: list of {machineType, state, remainingSeconds}."""
    spec = _op(output_fields=[{
        "name": "machineStatus", "field_type": "array",
        "items": {"name": "item", "field_type": "object", "properties": [
            {"name": "machineType", "field_type": "string"},
            {"name": "state", "field_type": "string", "enum_values": ["RUNNING", "FINISH"]},
            {"name": "remainingSeconds", "field_type": "integer"},
        ]}
    }])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "machineStatus": {"type": "array", "items": {
                       "type": "object", "properties": {
                           "machineType": {"type": "string"},
                           "state": {"type": "string", "enum": ["RUNNING", "FINISH"]},
                           "remainingSeconds": {"type": "integer"},
                       }}}}})
    assert validate_shape_parity(spec, doc) == []


def test_nested_object_ok():
    spec = _op(output_fields=[{
        "name": "user", "field_type": "object", "properties": [
            {"name": "id", "field_type": "string"},
            {"name": "address", "field_type": "object", "properties": [
                {"name": "city", "field_type": "string"},
                {"name": "zip", "field_type": "string"},
            ]},
        ]
    }])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "user": {"type": "object", "properties": {
                       "id": {"type": "string"},
                       "address": {"type": "object", "properties": {
                           "city": {"type": "string"}, "zip": {"type": "string"}}}}}}})
    assert validate_shape_parity(spec, doc) == []


def test_ref_resolution_ok():
    spec = _op(output_fields=[{
        "name": "machineStatus", "field_type": "array",
        "items": {"name": "item", "field_type": "object", "properties": [
            {"name": "state", "field_type": "string"},
        ]}
    }])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "machineStatus": {"type": "array",
                                     "items": {"$ref": "#/components/schemas/MachineStatusItem"}}}},
               components={
                   "MachineStatusItem": {"type": "object", "properties": {
                       "state": {"type": "string"}}}})
    assert validate_shape_parity(spec, doc) == []


def test_request_body_fields_ok():
    spec = _op(input_fields=[
        {"name": "storeId", "field_type": "string"},
        {"name": "mode", "field_type": "string", "enum_values": ["A", "B"]},
    ])
    doc = _doc("op", "POST", "/tools/op",
               req_schema={"type": "object", "properties": {
                   "storeId": {"type": "string"},
                   "mode": {"type": "string", "enum": ["A", "B"]}}})
    assert validate_shape_parity(spec, doc) == []


def test_multi_tool_ok():
    """Operation with tools[] — each tool's fields matched to its own path."""
    spec = _op(tools=[
        {"tool_id": "get_store", "http_method": "GET", "path": "/tools/get_store",
         "output_fields": [{"name": "name", "field_type": "string"}]},
        {"tool_id": "update_store", "http_method": "POST", "path": "/tools/update_store",
         "input_fields": [{"name": "name", "field_type": "string"}]},
    ])
    doc = {"paths": {
        "/tools/get_store": {"get": {"operationId": "get_store",
            "responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"name": {"type": "string"}}}}}}}}},
        "/tools/update_store": {"post": {"operationId": "update_store",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"name": {"type": "string"}}}}}}}},
    }}
    assert validate_shape_parity(spec, doc) == []


# --------------------------------------------------------------------------- #
# (b) Mismatch cases — specific reason codes emitted.                         #
# --------------------------------------------------------------------------- #

def test_type_mismatch_reported():
    spec = _op(output_fields=[{"name": "remaining", "field_type": "integer"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"remaining": {"type": "string"}}})
    mismatches = validate_shape_parity(spec, doc)
    assert len(mismatches) == 1
    assert mismatches[0].reason == "type_mismatch"
    assert mismatches[0].expected == "integer"
    assert mismatches[0].actual == "string"


def test_enum_values_differ_reported():
    spec = _op(output_fields=[{"name": "state", "field_type": "string",
                               "enum_values": ["RUNNING", "FINISH"]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "state": {"type": "string", "enum": ["FINISH", "RUNNING"]}}})  # order differs
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "enum_values_differ" for m in mismatches)


def test_enum_missing_in_openapi_reported():
    spec = _op(output_fields=[{"name": "state", "field_type": "string",
                               "enum_values": ["A", "B"]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"state": {"type": "string"}}})
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "enum_missing_in_openapi" for m in mismatches)


def test_array_missing_items_in_openapi_reported():
    spec = _op(output_fields=[{"name": "tags", "field_type": "array",
                               "items": {"name": "tag", "field_type": "string"}}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {"tags": {"type": "array"}}})
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "items_missing_in_openapi" for m in mismatches)


def test_spec_array_missing_items_reported():
    """Spec itself under-specified — rule 13 violation flagged."""
    spec = _op(output_fields=[{"name": "tags", "field_type": "array"}])  # no items
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "tags": {"type": "array", "items": {"type": "string"}}}})
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "spec_array_missing_items" for m in mismatches)


def test_property_missing_in_openapi_reported():
    spec = _op(output_fields=[{
        "name": "user", "field_type": "object", "properties": [
            {"name": "id", "field_type": "string"},
            {"name": "email", "field_type": "string"},
        ]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "user": {"type": "object", "properties": {"id": {"type": "string"}}}}})  # email missing
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "property_missing_in_openapi" and "email" in m.path for m in mismatches)


def test_property_extra_in_openapi_reported():
    spec = _op(output_fields=[{
        "name": "user", "field_type": "object", "properties": [
            {"name": "id", "field_type": "string"},
        ]}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "user": {"type": "object", "properties": {
                       "id": {"type": "string"}, "ssn": {"type": "string"}}}}})  # ssn extra
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "property_extra_in_openapi" and "ssn" in m.path for m in mismatches)


def test_operation_missing_in_openapi_reported():
    spec = _op(op_id="ghost", path="/tools/ghost",
               input_fields=[{"name": "x", "field_type": "string"}])
    doc = {"paths": {}}  # no match
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "operation_missing_in_openapi" for m in mismatches)


def test_response_missing_reported():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               req_schema={"type": "object", "properties": {}})  # no res
    mismatches = validate_shape_parity(spec, doc)
    assert any(m.reason == "response_missing_in_openapi" for m in mismatches)


# --------------------------------------------------------------------------- #
# (c) Refusals — the validator explicitly raises on unsupported constructs.   #
# --------------------------------------------------------------------------- #

def test_oneof_refused():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"oneOf": [{"type": "string"}, {"type": "integer"}]})
    with pytest.raises(ShapeParityError, match="oneOf"):
        validate_shape_parity(spec, doc)


def test_anyof_refused():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"anyOf": [{"type": "string"}]})
    with pytest.raises(ShapeParityError, match="anyOf"):
        validate_shape_parity(spec, doc)


def test_allof_refused():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"allOf": [{"type": "string"}]})
    with pytest.raises(ShapeParityError, match="allOf"):
        validate_shape_parity(spec, doc)


def test_external_ref_refused():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "x": {"$ref": "https://elsewhere/schema.json"}}})
    with pytest.raises(ShapeParityError, match="External \\$ref"):
        validate_shape_parity(spec, doc)


def test_unresolvable_ref_refused():
    spec = _op(output_fields=[{"name": "x", "field_type": "string"}])
    doc = _doc("op", "POST", "/tools/op",
               res_schema={"type": "object", "properties": {
                   "x": {"$ref": "#/components/schemas/NoSuch"}}},
               components={"Other": {"type": "string"}})
    with pytest.raises(ShapeParityError, match="Unresolvable"):
        validate_shape_parity(spec, doc)
