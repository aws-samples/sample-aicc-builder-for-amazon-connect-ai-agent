"""Deterministic contract repairs found by the 2026-09-10 ten-run campaign.

Each of these refused a bundle at the D9 gate (or would have failed the live
deploy) for a reason that is mechanical, not a real design mismatch.
"""
import json

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

def test_open_value_attaches_a_builtin_and_gets_no_custom_slot_type():
    """S5 (live 2026-09-12): a custom slot type carries a VALUE SET, so one built
    from a single sample deploys as a one-item menu the runtime auto-selects
    without ever asking — the order-number question was skipped outright. An open
    value therefore attaches an NLX built-in plus the regex, and no slot type is
    emitted. The FieldSpec constraint must still reach the slot across a name
    variant (plan `phonePin` <- FieldSpec `phone_pin`), and 'phonePin' is a PIN,
    not a phone number, so it must not become NLX.PhoneNumber. A value WITH a
    format is an identifier, not a quantity: it attaches NLX.AlphaNumeric and
    keeps the regex (the live SELC order number), never NLX.Number, which would
    drop leading zeros and ignore the exact length.
    """
    plans = [{"operation_id": "check_balance",
              "slots": [{"name": "phonePin", "type": "text"}]}]
    operations = {"check_balance": {"input_fields": [
        {"name": "phone_pin", "field_type": "string", "pattern": r"^\d{6}$", "sensitive": True}]}}
    types = _derive_slot_types(plans, operations)
    assert types == []
    slot = plans[0]["slots"][0]
    assert slot["type"] == "NLX.AlphaNumeric"
    assert slot["regex"] == r"^\d{6}$"
    assert slot["sensitive"] is True


def test_enum_field_still_gets_a_custom_slot_type_with_its_constraints():
    """The other half of the same rule: a value SET is a real custom slot type."""
    plans = [{"operation_id": "check_balance",
              "slots": [{"name": "accountType", "type": "enum"}]}]
    operations = {"check_balance": {"input_fields": [
        {"name": "account_type", "field_type": "string",
         "enum_values": ["checking", "savings"], "max_length": 8}]}}
    types = _derive_slot_types(plans, operations)
    assert [t["slotTypeId"] for t in types] == ["accountType"]
    assert [v["value"] for v in types[0]["values"]] == ["checking", "savings"]
    assert types[0]["metadata"]["constraints"] == {"max_length": 8}
    assert plans[0]["slots"][0]["type"] == "accountType"


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


# --- D1/D2: secret header shape and webhook environments (live 2026-09-13) -----

_EXTERNAL_PLAN = {
    "data_request_id": "get_cleaning_price", "mode": "external",
    "path": "/tools/get_cleaning_price",
    "request_fields": [{"name": "productType", "type": "text"}],
    "response_fields": [{"name": "unitPrice", "type": "number"}],
}


def test_secret_header_uses_the_nlx_secret_reference_not_a_template_placeholder():
    """`{{secrets.BackendApiKey}}` was sent to the backend VERBATIM → 403. The
    runtime resolves `{BackendApiKey:NLX.Secret}`, and `dynamic` means "the
    caller supplies the value per request", which for a secret sends nothing."""
    from tools.acxd_data_request_builder import build_data_request
    doc = build_data_request(dict(_EXTERNAL_PLAN))
    assert doc["webhook"]["headers"] == [
        {"key": "x-api-key", "value": "{BackendApiKey:NLX.Secret}", "sensitive": True}]
    serialized = json.dumps(doc)
    assert "{{secrets" not in serialized
    assert "dynamic" not in serialized


def test_external_webhook_carries_both_environment_blocks_with_the_same_url_and_headers():
    """D2: the secret is resolved only from webhook.environments.<env>; a
    top-level url/headers pair alone answered 403."""
    from tools.acxd_data_request_builder import build_data_request
    webhook = build_data_request(dict(_EXTERNAL_PLAN))["webhook"]
    assert set(webhook["environments"]) == {"production", "development"}
    for env in ("production", "development"):
        assert webhook["environments"][env]["url"] == webhook["url"]
        assert webhook["environments"][env]["headers"] == webhook["headers"]
    # the environments are independent copies, so mutating one cannot leak
    webhook["environments"]["production"]["headers"][0]["key"] = "x-other"
    assert webhook["headers"][0]["key"] == "x-api-key"


def test_a_customer_supplied_auth_header_becomes_its_own_secret_reference():
    from tools.acxd_data_request_builder import build_data_request
    doc = build_data_request({**_EXTERNAL_PLAN, "url": "https://crm.example.com/price",
                              "auth_header": "Authorization",
                              "auth_secret_name": "crmToken"})
    assert doc["webhook"]["headers"] == [
        {"key": "Authorization", "value": "{crmToken:NLX.Secret}", "sensitive": True}]


def test_generated_data_requests_pass_their_own_schema():
    from tools.acxd_data_request_builder import build_all_data_requests
    docs, problems = build_all_data_requests({"data_integrations": [dict(_EXTERNAL_PLAN)]})
    assert problems == []
    assert [d["dataRequestId"] for d in docs] == ["getCleaningPrice"]


def test_a_bundle_generated_before_the_contract_is_repaired_on_load():
    """A session packaged earlier still holds {{secrets.X}} and no environments;
    load-time repair keeps it deployable instead of 403-ing on every call."""
    from tools.acxd_data_request_builder import repair_data_request_contract
    stale = {"dataRequestId": "getOrder", "type": "object", "webhook": {
        "implementation": "external", "method": "POST",
        "url": "{WEBHOOK_URL}/tools/get_order",
        "headers": [{"key": "x-api-key", "value": "{{secrets.BackendApiKey}}", "dynamic": True}],
        "sendContext": True}}
    webhook = repair_data_request_contract(stale)["webhook"]
    assert webhook["headers"] == [
        {"key": "x-api-key", "value": "{BackendApiKey:NLX.Secret}", "sensitive": True}]
    assert webhook["environments"]["production"] == {
        "url": "{WEBHOOK_URL}/tools/get_order", "headers": webhook["headers"]}
    # repairs in place and is idempotent; a mock webhook is left alone
    assert repair_data_request_contract(stale) is stale
    assert stale["webhook"]["headers"] == webhook["headers"]
    mock = {"dataRequestId": "x", "webhook": {"implementation": "inline-static", "code": "{}"}}
    assert repair_data_request_contract(mock) == mock


def test_the_manifest_still_schedules_upsert_secrets_for_the_new_reference_syntax():
    from tools.acxd_manifest_builder import build_manifest
    bundle = {"data_requests": [{"dataRequestId": "getOrder", "webhook": {
        "implementation": "external", "url": "{WEBHOOK_URL}/tools/get_order",
        "headers": [{"key": "x-api-key", "value": "{BackendApiKey:NLX.Secret}"}]}}]}
    steps = [s["type"] for s in build_manifest(bundle, project_name="selc")["steps"]]
    assert "upsert-secrets" in steps


def test_the_d9_backend_auth_gate_accepts_the_live_secret_reference():
    from tools.validate_consistency import _d9_backend_auth_checks
    bundle = {"data_requests": [{"dataRequestId": "getOrder", "webhook": {
        "implementation": "external", "url": "{WEBHOOK_URL}/tools/get_order",
        "headers": [{"key": "x-api-key", "value": "{BackendApiKey:NLX.Secret}"}]}}]}
    assert _d9_backend_auth_checks(bundle, "no-such-session") == []
    naked = {"data_requests": [{"dataRequestId": "getOrder", "webhook": {
        "implementation": "external", "url": "{WEBHOOK_URL}/tools/get_order", "headers": []}}]}
    assert [i["id"] for i in _d9_backend_auth_checks(naked, "no-such-session")] == ["D9-8"]


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


# --- application name for a CJK company name comes from the project slug ------

def test_application_name_falls_back_to_the_project_name():
    from tools.acxd_resource_builders import build_application
    spec = {"business_profile": {"company_name": "서울밝은안과"},
            "infrastructure": {"project_name": "seoul-bright-eye"},
            "flows": [{"flow_id": "Welcome", "role": "welcome", "language": "ko-KR"}]}
    app = build_application(spec)
    assert app["name"] == "Seoul Bright Eye Assistant"
    bare = build_application({"business_profile": {"company_name": "서울밝은안과"}, "flows": []})
    assert bare["name"] == "AICC Assistant"


# --- slot types: one custom slot type per constrained field, never per type name ---

def test_enum_slots_of_different_fields_get_their_own_slot_types():
    """Live (SELC, 2026-09-11): productType / serviceType / installLocationType were
    all declared `type: enum`, so they collapsed into ONE slot type 'enum' holding
    every value of every field — and D9-4 rejected the bundle field by field."""
    plans = [
        {"operation_id": "get_cleaning_price",
         "slots": [{"name": "productType", "type": "enum"}, {"name": "serviceType", "type": "enum"}]},
        {"operation_id": "create_cleaning_reservation",
         "slots": [{"name": "installLocationType", "type": "enum"}]},
    ]
    operations = {
        "get_cleaning_price": {"input_fields": [
            {"name": "productType", "enum_values": ["벽걸이실내기", "스탠드"]},
            {"name": "serviceType", "enum_values": ["종합세척", "기본세척"]}]},
        "create_cleaning_reservation": {"input_fields": [
            {"name": "installLocationType", "enum_values": ["사업장", "가정"]}]},
    }
    types = {t["slotTypeId"]: [v["value"] for v in t["values"]] for t in _derive_slot_types(plans, operations)}
    assert types == {
        "productType": ["벽걸이실내기", "스탠드"],
        "serviceType": ["종합세척", "기본세척"],
        "installLocationType": ["사업장", "가정"],
    }
    # the plan now names the per-field slot type, and D9-4 resolves the same id
    from tools.acxd_generation_context import slot_type_id_for
    assert plans[0]["slots"][0]["type"] == "productType"
    assert slot_type_id_for("productType", "enum") == "productType"
    # a specific custom type name is still shared under the name it declares
    assert slot_type_id_for("fromCity", "CityName") == "CityName"


def test_data_request_fields_merge_openapi_contract_with_the_spec():
    """Live: the OpenAPI generator shipped a CREATE operation whose response
    schema carried only the envelope and whose request schema was empty, so the
    Data Request built from that contract could not carry the collected slots
    (D3) nor the reply placeholders (M1). The OperationSpec is the source of
    truth for WHICH fields exist; the OpenAPI only contributes the deployed
    spellings, so the two are merged (camel/snake-insensitive) instead of the
    contract silently winning."""
    from tools.acxd_generation_context import _merge_fields, _with_envelope

    contract_response = [{"name": "success", "type": "boolean"},
                         {"name": "errorCode", "type": "string"},
                         {"name": "message", "type": "string"}]
    spec_output = [{"name": "reservation_id", "type": "string"},
                   {"name": "totalAmount", "type": "number"},
                   {"name": "message", "type": "string"}]
    merged = _with_envelope(_merge_fields(contract_response, spec_output))
    names = [f["name"] for f in merged]
    assert names[:3] == ["success", "errorCode", "message"]
    assert "reservation_id" in names and "totalAmount" in names
    assert names.count("message") == 1

    # camelCase in the contract wins over the spec's snake_case twin
    merged = _merge_fields([{"name": "orderNumber", "type": "string"}],
                           [{"name": "order_number", "type": "string"},
                            {"name": "address", "type": "string"}])
    assert [f["name"] for f in merged] == ["orderNumber", "address"]


def test_data_request_and_slot_type_descriptions_are_ascii_by_construction():
    """ACXD metadata must be ASCII. Descriptions come from the interview in the
    project language, so the builders strip them at build time — the D9 gate
    then passes without the orchestrator rewriting the customer's OpenAPI."""
    from tools.acxd_data_request_builder import build_data_request
    from tools.acxd_resource_builders import build_slot_types

    doc = build_data_request({
        "data_request_id": "createCleaningReservation", "mode": "external",
        "path": "/tools/create_cleaning_reservation", "purpose": "세척 예약 접수",
        "request_fields": [{"name": "productType", "type": "text", "description": "제품 유형",
                            "enum_values": ["벽걸이실내기", "스탠드"]}],
        "response_fields": [{"name": "reservationId", "type": "text", "description": "예약 번호"}],
    })
    blob = json.dumps({k: v for k, v in doc.items() if k != "requestSchema"}, ensure_ascii=False)
    assert all(ord(c) < 0x80 for c in (doc.get("description") or ""))
    for schema_key in ("requestSchema", "responseSchema"):
        for prop in doc[schema_key]["properties"].values():
            assert all(ord(c) < 0x80 for c in prop.get("description", ""))
    # enum VALUES are slot values, not metadata — they must survive verbatim
    assert doc["requestSchema"]["properties"]["productType"]["enum"] == ["벽걸이실내기", "스탠드"]
    assert "예약" not in blob

    docs, problems = build_slot_types({
        "slot_types": [{"slotTypeId": "productType", "description": "제품 유형",
                        "values": [{"value": "벽걸이실내기", "synonyms": ["벽걸이"]}]}],
        "application": {"locales": ["ko-KR"]},
    })
    assert not problems
    product = next(d for d in docs if d["slotTypeId"] == "productType")
    assert all(ord(c) < 0x80 for c in product.get("description", ""))
    assert product["values"][0]["value"] == "벽걸이실내기"


def test_backend_api_key_secret_is_named_per_project():
    """Live (2026-09-14): three projects in one ACXD workspace shared the secret
    'BackendApiKey'; each deploy overwrote the others' API key and their Data
    Requests answered 403."""
    from tools.acxd_data_request_builder import (
        auth_secret_name_for, backend_api_key_secret_name, build_secret_assets)

    assert backend_api_key_secret_name("greencart") == "greencartBackendApiKey"
    assert backend_api_key_secret_name("Hanbit-Hospital") == "hanbitHospitalBackendApiKey"
    assert backend_api_key_secret_name(None) == "BackendApiKey"          # no project: legacy name
    plan = {"data_request_id": "getOrder", "url": "{WEBHOOK_URL}/tools/get_order", "project_slug": "greencart"}
    assert auth_secret_name_for(plan) == "greencartBackendApiKey"
    docs = build_secret_assets({"data_integrations": [plan]})
    assert [d["name"] for d in docs] == ["greencartBackendApiKey"]
    assert docs[0]["valueEnv"] == "ACXD_SECRET_GREENCARTBACKENDAPIKEY"


def test_the_manifest_records_when_and_by_what_it_was_generated(monkeypatch):
    """Live (2026-09-14): two orchestrators reported a regeneration that never
    ran. The manifest's provenance lets a bundle be checked against the claim."""
    from tools.acxd_manifest_builder import build_manifest, validate_deploy_manifest
    monkeypatch.setenv("AICC_BUILDER_BUILD", "abc1234")
    manifest = build_manifest({"flows": [{"flowId": "Welcome", "nodes": {}}]}, project_name="selc")
    assert manifest["generatedAt"].endswith("Z") and manifest["generatedAt"][:2] == "20"
    assert manifest["builder"] == {"name": "aicc-builder", "build": "abc1234"}
    assert validate_deploy_manifest(manifest) == []
    monkeypatch.delenv("AICC_BUILDER_BUILD")
    assert build_manifest({"flows": []}, project_name="selc")["builder"] == {"name": "aicc-builder"}


def test_d9_4_accepts_self_validating_builtins_without_a_regex():
    """Live (Hanbit, 2026-09-14): S9 turned the date/time slots into NLX.Date /
    NLX.Time (the regex with separators could never match a built-in value) and
    D9-4 then blocked the bundle for the missing regex."""
    from tools.validate_consistency import _d9_open_value_slot_issues
    bundle = {"flows": [{"flowId": "BookAppointment", "slotTypes": [
        {"name": "appointmentDate", "type": "NLX.Date", "sensitive": False},
        {"name": "timeSlot", "type": "NLX.Time", "sensitive": False},
        {"name": "orderNo", "type": "NLX.AlphaNumeric", "sensitive": False, "regex": "^\\d{8}$"},
    ]}]}
    plan = {"flow_id": "BookAppointment"}
    assert _d9_open_value_slot_issues(bundle, plan, {"name": "appointmentDate"}, "appointmentDate",
                                      "book_appointment", "^\\d{4}-\\d{2}-\\d{2}$", None, None) == []
    assert _d9_open_value_slot_issues(bundle, plan, {"name": "timeSlot"}, "timeSlot",
                                      "book_appointment", "^\\d{2}:\\d{2}$", None, None) == []
    mismatch = _d9_open_value_slot_issues(bundle, plan, {"name": "orderNo"}, "orderNo",
                                          "book_appointment", "^\\d{10}$", None, None)
    assert mismatch and "does not match" in mismatch[0]["message"]
