"""review_gates — blocking findings come from deterministic gates with stable ids."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import tools.review_gates as gates  # noqa: E402


def test_blocking_findings_have_stable_ids_and_gate_totals(monkeypatch):
    monkeypatch.setattr(gates, "_consistency_findings", lambda sid: [
        {"id": "D:lambda:get_order:orderId:Spec input field", "gate": "consistency", "severity": "error", "message": "m1"},
        {"id": "D9-4:slot_type:get_price:productType", "gate": "acxd-d9", "severity": "error", "message": "m2"},
        {"id": "D9-4:slot_type:get_price:productType", "gate": "acxd-d9", "severity": "error", "message": "dup"},
    ])
    monkeypatch.setattr(gates, "_parity_findings", lambda sid: [
        {"id": "PARITY:create.response.properties.x:property_extra_in_openapi", "gate": "parity", "severity": "error", "message": "m3"},
    ])
    report = gates.collect_blocking_findings("s")
    assert report["count"] == 3  # duplicate id collapsed
    assert report["gates"] == {"consistency": 1, "acxd-d9": 1, "parity": 1}
    # the same inputs give the same ids — that is what makes them diffable
    assert [f["id"] for f in gates.collect_blocking_findings("s")["findings"]] == [f["id"] for f in report["findings"]]


def test_diff_tells_fixed_from_new():
    current = [{"id": "A"}, {"id": "C"}]
    assert gates.diff_findings(["A", "B"], current) == {"fixed": ["B"], "new": ["C"], "remaining": ["A"]}
    assert gates.diff_findings(None, current)["new"] == ["A", "C"]


def test_consistency_findings_map_d1_d8_and_d9_shapes(monkeypatch):
    import tools.validate_consistency as vc
    monkeypatch.setattr(vc, "_validate_parameter_consistency_impl", lambda sid: {"mismatches": [
        {"operation_id": "get_order", "field": "orderId", "asset_type": "lambda",
         "issue": "Spec input field 'orderId' not found in Lambda handler"},
        {"id": "D9-3", "severity": "error", "message": "Data Request fields differ", "asset_type": "data_request",
         "operation_id": "createReservation", "field": "responseSchema"},
    ]})
    found = gates._consistency_findings("s")
    assert found[0]["id"].startswith("D:lambda:get_order:orderId:")
    assert found[0]["gate"] == "consistency"
    assert found[1]["id"] == "D9-3:data_request:createReservation:responseSchema"
    assert found[1]["gate"] == "acxd-d9"


def test_summary_text_names_the_rule(monkeypatch):
    text = gates.format_blocking_summary({"count": 2, "gates": {"parity": 2}}, "ko")
    assert "차단 항목(결정론 검사) 2건" in text and "0건이어야" in text


def test_orphan_operation_gate_flags_specless_assets_but_not_supporting_lambdas(monkeypatch):
    """Live: log_call_result was hand-built during generation (Lambda + OpenAPI
    path + Data Request) with no OperationSpec, so no gate ever checked it.
    customer_lookup / update_q_session ship index.py and are supporting Lambdas."""
    import tools.review_gates as rg
    import tools.spec_manager as sm
    import tools.s3_asset_storage as s3s
    import tools.asset_loader as al

    monkeypatch.setattr(sm, "get_all_specs", lambda: {"get_cleaning_price": object()})
    monkeypatch.setattr(s3s, "list_session_assets", lambda sid: [
        "assets/s/lambda/get_cleaning_price/handler.py",
        "assets/s/lambda/log_call_result/index.py",         # supporting-style file, but…
        "assets/s/lambda/customer_lookup/index.py",
        "assets/s/lambda/update_q_session/index.js",
        "assets/s/acxd_data_request/getCleaningPrice.json",
        "assets/s/acxd_data_request/logCallResult.json",    # …its Data Request/OpenAPI need a spec
    ])
    monkeypatch.setattr(al, "load_existing_asset", lambda *a, **k: (
        "openapi: 3.0.0\npaths:\n  /tools/get_cleaning_price:\n    post:\n      operationId: get_cleaning_price\n"
        "  /tools/log_call_result:\n    post:\n      operationId: log_call_result\n"))
    ids = sorted(f["id"] for f in rg._orphan_operation_findings("s"))
    assert ids == ["SPEC:acxd_data_request:logCallResult", "SPEC:openapi:log_call_result"]


def test_d9_flags_agenticcx_reads_before_the_block():
    from tools.validate_consistency import _d9_agenticcx_refs_before_block
    actions = [
        {"Identifier": "lookup", "Type": "InvokeLambdaFunction", "Transitions": {"NextAction": "check"}},
        {"Identifier": "check", "Type": "Compare",
         "Parameters": {"ComparisonValue": "$.AgenticCX.ContextVariables.isKnownCustomer"},
         "Transitions": {"NextAction": "AgenticCXPlaceholder"}},
        {"Identifier": "AgenticCXPlaceholder", "Type": "InvokeFlowModule",
         "Parameters": {"AgentConfiguration": {}}, "Transitions": {"NextAction": "log"}},
        {"Identifier": "log", "Type": "InvokeLambdaFunction",
         "Parameters": {"LambdaInvocationAttributes": {"intent": "$.AgenticCX.ContextVariables.intent"}},
         "Transitions": {}},
    ]
    before = _d9_agenticcx_refs_before_block({"StartAction": "lookup"}, actions)
    assert before == {"check": ["$.AgenticCX.ContextVariables.isKnownCustomer"]}   # 'log' is after the block
