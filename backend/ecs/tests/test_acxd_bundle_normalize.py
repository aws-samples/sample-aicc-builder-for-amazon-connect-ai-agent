"""Bundle-load normalization: flows generated before a live contract fix stay deployable."""
from tools.acxd_bundle import normalize_flow_for_service


def test_kb_node_gets_name_from_placeholder_and_loses_unknown_keys():
    flow = {"flowId": "Fallback", "nodes": {
        "n1": {"type": "knowledge_base", "metadata": {
            "knowledgeBase": {"knowledgeBaseId": "{KB:SunnyHotelFAQ}", "scopeTags": []}, "maxRetries": 2}},
        "n2": {"type": "end", "metadata": {}},
    }}
    out = normalize_flow_for_service(flow)
    assert out["nodes"]["n1"]["metadata"]["knowledgeBase"] == {
        "knowledgeBaseId": "{KB:SunnyHotelFAQ}", "name": "SunnyHotelFAQ"}
    # non-SDK metadata (`maxRetries`) is dropped on load — the SDK would drop it anyway
    assert "maxRetries" not in out["nodes"]["n1"]["metadata"]
    assert out["nodes"]["n2"] == {"type": "end"}


def test_explicit_name_is_kept_and_non_dicts_pass_through():
    flow = {"nodes": {"a": {"type": "knowledge_base", "metadata": {
        "knowledgeBase": {"knowledgeBaseId": "kb-1", "name": "Given"}}}}}
    assert normalize_flow_for_service(flow)["nodes"]["a"]["metadata"]["knowledgeBase"]["name"] == "Given"
    assert normalize_flow_for_service("not-a-flow") == "not-a-flow"


def test_rebind_never_touches_an_nlx_builtin_slot():
    """Live (SELC, 2026-09-13): the normalizer had turned the order-number slot
    into NLX.AlphaNumeric + regex, but a stale one-item custom slot type named
    'orderNumber' was still in the bundle and the loader rebound the slot back
    to it — re-creating the auto-selecting one-item menu at packaging time."""
    from tools.acxd_bundle import _rebind_slot_types

    flow = {"flowId": "F", "slotTypes": [
        {"name": "orderNumber", "type": "NLX.AlphaNumeric", "regex": "^[0-9]{10}$"},
        {"name": "serviceType", "type": "enum"},
    ], "nodes": {}}
    slot_types = [{"slotTypeId": "orderNumber", "values": [{"value": "1234567890"}]},
                  {"slotTypeId": "serviceType", "values": [{"value": "a"}, {"value": "b"}]}]
    out = _rebind_slot_types(flow, slot_types)
    assert out["slotTypes"][0]["type"] == "NLX.AlphaNumeric"
    assert out["slotTypes"][0]["regex"] == "^[0-9]{10}$"
    assert out["slotTypes"][1]["type"] == "serviceType"  # the legitimate rebind still happens


def test_load_acxd_bundle_leaves_out_slot_types_no_flow_attaches(monkeypatch):
    """Live (SELC): one-item 'orderNumber'/'phoneNumber' slot types from an
    earlier generation stayed on disk after the contract moved the slots to NLX
    built-ins, and would have been deployed as stray resources."""
    import tools.acxd_bundle as bundle_mod

    docs = {
        "acxd_flow": [{"flowId": "F", "slotTypes": [
            {"name": "orderNumber", "type": "NLX.AlphaNumeric", "regex": "^[0-9]{10}$"},
            {"name": "serviceType", "type": "serviceType"}], "nodes": {}}],
        "acxd_slot_type": [
            {"slotTypeId": "orderNumber", "values": [{"value": "1234567890"}]},
            {"slotTypeId": "serviceType", "values": [{"value": "a"}, {"value": "b"}]},
        ],
    }
    monkeypatch.setattr(bundle_mod, "_read_json_docs", lambda _sid, asset_type: list(docs.get(asset_type, [])))
    monkeypatch.setattr(bundle_mod, "_backend_inventory",
                        lambda _sid: {"infrastructure": None, "lambdas": [], "openapi": None})
    monkeypatch.setattr(bundle_mod, "_collapse_identical_docs", lambda d, _t: d)
    loaded = bundle_mod.load_acxd_bundle("session-x")
    assert [st["slotTypeId"] for st in loaded["slot_types"]] == ["serviceType"]
    assert loaded["dropped_slot_types"] == ["orderNumber"]


def test_load_acxd_bundle_refreshes_the_contact_flow_and_prunes_unreferenced_resources(monkeypatch):
    """Live (GreenCart, 2026-09-14): the stored Contact Flow predated the language
    block and still carried the rejected chat analytics; an unprefixed
    'BackendApiKey' secret and 'guardrail1' from earlier generations shipped next
    to the project-scoped ones."""
    import tools.acxd_bundle as bundle_mod

    flow = {"Version": "2019-10-30", "StartAction": "acx", "Actions": [
        {"Identifier": "acx", "Type": "ConnectParticipantWithAgenticCX",
         "Parameters": {"AgentConfiguration": {"WorkspaceId": "{ACXD_WORKSPACE_ID}", "ApplicationId": "{ACXD_APPLICATION_ID}",
                                               "Alias": "{ACXD_ALIAS_ID}", "ContextVariables": {}}},
         "Transitions": {"NextAction": "end", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "end"}]}},
        {"Identifier": "end", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
    ]}
    docs = {
        "contact_flow": [flow],
        "acxd_application": [{"name": "app", "settings": {"guardrails": [{"guardrailId": "{GUARDRAIL:gc-pii}"}]}}],
        "acxd_guardrail": [{"name": "gc-pii", "rules": []}, {"name": "guardrail1", "rules": []}],
        "acxd_secret": [{"name": "gcBackendApiKey"}, {"name": "BackendApiKey"}],
        "acxd_data_request": [{"dataRequestId": "getOrder", "webhook": {"headers": [
            {"key": "x-api-key", "value": "{gcBackendApiKey:NLX.Secret}"}]}}],
    }
    monkeypatch.setattr(bundle_mod, "_read_json_docs", lambda _sid, asset_type: list(docs.get(asset_type, [])))
    monkeypatch.setattr(bundle_mod, "_backend_inventory",
                        lambda _sid: {"infrastructure": None, "lambdas": [], "openapi": None})
    monkeypatch.setattr(bundle_mod, "_collapse_identical_docs", lambda d, _t: d)
    monkeypatch.setattr(bundle_mod, "_dedupe_contact_flows", lambda d: d)
    import tools.acxd_flow_spec as afs
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda sid=None: None)
    loaded = bundle_mod.load_acxd_bundle("session-x")
    types = [a["Type"] for a in loaded["contact_flows"][0]["Actions"]]
    assert "UpdateContactData" in types                       # language block added on load
    assert [g["name"] for g in loaded["guardrails"]] == ["gc-pii"]
    assert loaded["dropped_guardrails"] == ["guardrail1"]
    assert [s["name"] for s in loaded["secrets"]] == ["gcBackendApiKey"]
    assert loaded["dropped_secrets"] == ["BackendApiKey"]
