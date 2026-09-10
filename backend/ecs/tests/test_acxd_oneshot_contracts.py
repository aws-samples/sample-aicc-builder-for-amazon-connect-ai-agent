"""Deterministic contract repairs found by the 2026-09-10 ten-run campaign.

Each of these refused a bundle at the D9 gate (or would have failed the live
deploy) for a reason that is mechanical, not a real design mismatch.
"""
from tools.acxd_generation_context import _derive_slot_types
from tools.spec_manager import _enforce_exact_length_phrase
from tools.validate_consistency import (
    _d9_field_index,
    _d9_openapi_operations,
    _d9_path_aliases,
    _d9_regex_canonical,
)


# --- D9-3: /tools prefix in servers.url, kebab-case path keys -------------

def test_openapi_path_aliases_cover_servers_prefix_and_case_variants():
    doc = {"servers": [{"url": "https://x.execute-api.us-east-1.amazonaws.com/prod/tools"}]}
    aliases = _d9_path_aliases(doc, "/check-balance")
    assert "/tools/check_balance" in aliases
    assert "/check-balance" in aliases and "/tools/check-balance" in aliases


def test_openapi_operations_are_indexed_under_the_data_request_spelling():
    doc = {
        "servers": [{"url": "https://x.example.com/prod/tools"}],
        "paths": {"/check_balance": {"post": {
            "operationId": "checkBalance",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"cardLast4": {"type": "string"}}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"balance": {"type": "number"}}}}}}},
        }}},
    }
    ops = _d9_openapi_operations([doc])
    assert "/tools/check_balance" in ops
    assert ops["/tools/check_balance"]["request"] == {"cardLast4"}
    assert ops["/tools/check_balance"]["response"] == {"balance"}


# --- D9-4: field spelling and regex equivalence ----------------------------

def test_field_index_matches_camel_and_snake_spellings():
    index = _d9_field_index({"input_fields": [{"name": "phone_pin", "pattern": r"^\d{6}$"}]})
    assert index["phonePin"] is index["phone_pin"]


def test_regex_canonical_treats_digit_class_spellings_as_equal():
    assert _d9_regex_canonical(r"^\d{8}$") == _d9_regex_canonical("^[0-9]{8}$")
    assert _d9_regex_canonical(r"\d{4}") == _d9_regex_canonical("^[0-9]{4}$")
    assert _d9_regex_canonical(r"^\d{4}$") != _d9_regex_canonical(r"^\d{6}$")


# --- slot types: FieldSpec.pattern is the regex, names may differ in case ---

def test_slot_type_is_derived_from_fieldspec_pattern_with_name_variants():
    plans = [{"operation_id": "check_balance",
              "slots": [{"name": "phonePin", "type": "text"}]}]
    operations = {"check_balance": {"input_fields": [
        {"name": "phone_pin", "field_type": "string", "pattern": r"^\d{6}$", "sensitive": True}]}}
    types = _derive_slot_types(plans, operations)
    assert [t["slotTypeId"] for t in types] == ["phonePin"]
    assert types[0]["metadata"]["constraints"]["regex"] == r"^\d{6}$"
    assert types[0]["sensitive"] is True
    assert plans[0]["slots"][0]["type"] == "phonePin"
    assert plans[0]["slots"][0]["regex"] == r"^\d{6}$"


# --- Japanese exact-length phrases ------------------------------------------

def test_japanese_length_phrase_in_pattern_becomes_a_regex():
    field = _enforce_exact_length_phrase({"name": "policyNumber", "field_type": "string",
                                          "pattern": "^数字10桁$"})
    assert field["pattern"] == r"^\d{10}$"
    assert field["min_length"] == 10 and field["max_length"] == 10


def test_japanese_alphanumeric_phrase_in_description():
    field = _enforce_exact_length_phrase({"name": "claimId", "field_type": "string",
                                          "description": "請求番号（英数字8桁）"})
    assert field["pattern"] == r"^[A-Za-z0-9]{8}$"
    assert field["max_length"] == 8


# --- Data Request path follows the OperationSpec tool, not the operation id ---

def test_data_request_url_uses_the_resolved_tool_path():
    from tools.acxd_data_request_builder import build_data_request
    plan = {"data_request_id": "checkBalance", "operation_ref": "verify_and_get_balance",
            "path": "/tools/verify_and_get_balance", "mode": "external",
            "request_fields": [{"name": "cardLast4", "type": "text"}],
            "response_fields": [{"name": "balance", "type": "number"}]}
    doc = build_data_request(plan)
    assert doc["webhook"]["url"] == "{WEBHOOK_URL}/tools/verify_and_get_balance"
    legacy = build_data_request({**plan, "path": None, "operation_ref": None})
    assert legacy["webhook"]["url"] == "{WEBHOOK_URL}/tools/checkBalance"


# --- English descriptive length phrases are not format masks -----------------

def test_english_phrase_with_modifier_is_a_length_not_a_mask():
    from tools.spec_manager import _normalize_field_constraints
    field = _normalize_field_constraints({"name": "bagTagNumber", "field_type": "string",
                                          "date_format": "10 alphanumeric characters"})
    assert field["pattern"] == r"^[A-Za-z0-9]{10}$"
    assert field["min_length"] == 10 and field["max_length"] == 10
    assert field.get("date_format") is None


def test_real_masks_still_convert_and_sentences_are_left_alone():
    from tools.spec_manager import _normalize_field_constraints
    masked = _normalize_field_constraints({"name": "phone", "field_type": "string",
                                           "date_format": "010-XXXX-XXXX"})
    assert masked["pattern"] == r"^010\-\d{4}\-\d{4}$"
    sentence = _normalize_field_constraints({"name": "note", "field_type": "string",
                                             "date_format": "free text entered by the agent"})
    assert sentence.get("pattern") is None
