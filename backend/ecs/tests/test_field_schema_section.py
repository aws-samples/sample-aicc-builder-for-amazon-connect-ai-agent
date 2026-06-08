"""Regression tests for build_field_schema_section robustness.

This helper is shared by the lambda_generator and openapi_generator. A complex
spec whose input_fields/output_fields contain a bare string element, or whose
nested items/properties arrive as double-encoded JSON strings, used to crash
every generation with "'str' object has no attribute 'get'". These tests lock
in the fault-tolerant behavior.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agents._consistency_rules import (  # noqa: E402
    build_field_schema_section,
    _render_field_tree,
    _field_has_nested_structure,
)


def test_bare_string_field_does_not_crash():
    assert _field_has_nested_structure("cardId") is False
    assert _render_field_tree("cardId") == ["- `cardId`: string"]


def test_double_encoded_field_recovered():
    encoded = json.dumps({"name": "status", "enum_values": ["A", "B"]})
    lines = _render_field_tree(encoded)
    assert any("status" in ln and "enum" in ln for ln in lines)


def test_json_string_items_and_properties():
    op = {
        "operation_id": "check_suspicious_transactions",
        "input_fields": [
            "cardId",  # bare string element
            {"name": "filters", "field_type": "object",
             "properties": json.dumps([{"name": "minAmount", "field_type": "number"}])},
        ],
        "output_fields": [
            {"name": "transactions", "field_type": "array",
             "items": json.dumps({"name": "tx", "field_type": "object",
                                  "properties": [{"name": "status", "enum_values": ["SUSPICIOUS", "CANCELLED"]}]})},
        ],
    }
    out = build_field_schema_section([op])
    # Renders without raising, and recovers the nested enum from the encoded items
    assert "check_suspicious_transactions" in out
    assert "minAmount" in out
    assert "SUSPICIOUS" in out


def test_all_string_fields_yields_no_section():
    # purely scalar / string fields → no nested schema section (no noise, no crash)
    op = {"operation_id": "x", "input_fields": ["a", "b"], "output_fields": ["c"]}
    assert build_field_schema_section([op]) == ""


def test_non_dict_op_skipped():
    assert build_field_schema_section(["not-a-dict", 42, None]) == ""
