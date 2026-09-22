"""e2e (2026-09-22): the interview saved an OperationSpec for FAQ; such a spec
must count for nothing — no tool id, no Data Request, no stale request in the
bundle."""
import os
import sys

for _path in (os.path.join(os.path.dirname(__file__), "..", "src"), os.path.join(os.path.dirname(__file__), "..")):
    _path = os.path.abspath(_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.spec_manager import OperationSpec, is_kb_native_spec


def _spec(**kw):
    base = {"operation_id": "answer_faq", "summary": "FAQ", "input_fields": [], "output_fields": []}
    base.update(kw)
    return OperationSpec(**base)


def test_kb_native_by_data_source_or_flags():
    assert is_kb_native_spec(_spec(data_source={"db_type": "knowledge_base"}))
    assert is_kb_native_spec(_spec(tools=[{"tool_id": "answer_faq", "generate_lambda": False,
                                            "generate_openapi": False}]))
    assert not is_kb_native_spec(_spec(data_source={"db_type": "dynamodb", "table_name": "orders"}))
    assert not is_kb_native_spec(_spec(tools=[{"tool_id": "get_order", "generate_lambda": True}]))
    assert not is_kb_native_spec(_spec())          # a plain spec with no tools is a tool


def test_bundle_drops_kb_native_and_empty_data_requests(monkeypatch):
    import tools.acxd_bundle as bundle_mod
    docs = {"acxd_data_request": [{"dataRequestId": "answerFaq", "type": "object", "webhook": {"url": "x"}},
                                  {}, {"dataRequestId": "getOrder", "type": "object", "webhook": {"url": "x"}}]}
    monkeypatch.setattr(bundle_mod, "_read_json_docs", lambda sid, t: list(docs.get(t, [])))
    monkeypatch.setattr("tools.spec_manager.operation_specs_bucket",
                        lambda sid: {"answer_faq": _spec(data_source={"db_type": "knowledge_base"})})
    out = bundle_mod.load_acxd_bundle("session-test")
    assert [d["dataRequestId"] for d in out["data_requests"]] == ["getOrder"]
    assert out["dropped_data_requests"] == ["answerFaq", "<no dataRequestId>"]


def test_get_all_tools_skips_kb_native_specs(monkeypatch):
    import tools.spec_manager as sm
    specs = {"answer_faq": _spec(data_source={"db_type": "knowledge_base"}),
             "get_order": _spec(operation_id="get_order",
                                data_source={"db_type": "dynamodb", "table_name": "orders"})}
    monkeypatch.setattr(sm, "get_all_specs", lambda: specs)
    monkeypatch.setattr(sm, "_get_flow_cfg", lambda: None)
    assert [t.tool_id for t in sm.get_all_tools()] == ["get_order"]


def test_validator_count_ignores_kb_native_specs(monkeypatch):
    import tools.validate_consistency as vc
    specs = {"answer_faq": _spec(data_source={"db_type": "knowledge_base"}),
             "get_order": _spec(operation_id="get_order",
                                input_fields=[{"name": "orderId", "type": "string"}],
                                output_fields=[{"name": "status", "type": "string"}],
                                data_source={"db_type": "dynamodb", "table_name": "orders"})}
    monkeypatch.setattr(vc, "get_all_specs", lambda: specs)
    monkeypatch.setattr("tools.spec_manager.get_all_specs", lambda: specs)
    monkeypatch.setattr("tools.spec_manager._get_flow_cfg", lambda: None)
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: ["assets/s/lambda/get_order/index.py"])
    monkeypatch.setattr(vc, "get_asset_from_s3",
                        lambda key: "def handler(event, context):\n    orderId = event['orderId']\n    return {'status': 'ok'}\n")
    out = vc._validate_parameter_consistency_impl("session-test")
    counts = [m for m in out.get("mismatches", []) if m.get("asset_type") == "count"]
    assert counts == [], counts
