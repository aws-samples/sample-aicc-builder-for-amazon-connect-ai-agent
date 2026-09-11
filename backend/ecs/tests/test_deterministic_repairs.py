"""Regressions from a live ACXD session (2026-09-11): the slot-type rebuild wrote
a second copy of every asset under <type>/<id>/, reported success, and the
bundle then failed on duplicate ids; D9-4 blocked on `\\-` vs `-`."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools.acxd_bundle import _collapse_identical_docs
from tools.validate_consistency import _d9_regex_canonical


def test_regex_canonical_treats_escaped_hyphen_outside_class_as_literal():
    assert _d9_regex_canonical(r"^010\-\d{4}\-\d{4}$") == _d9_regex_canonical("^010-[0-9]{4}-[0-9]{4}$")
    # inside a class the escape is kept (a hyphen there can be a range)
    assert _d9_regex_canonical(r"^[a\-z]+$") != _d9_regex_canonical("^[a-z]+$")
    # meaning-changing escapes are never touched
    assert _d9_regex_canonical(r"^a\.b$") != _d9_regex_canonical("^a.b$")


def test_identical_duplicate_docs_collapse_but_conflicts_stay():
    a = {"slotTypeId": "phoneNumber", "values": [{"value": "010-1234-5678"}]}
    b = {"slotTypeId": "phoneNumber", "values": [{"value": "010-0000-0000"}]}
    assert _collapse_identical_docs([a, dict(a)], "acxd_slot_type") == [a]
    assert _collapse_identical_docs([a, b], "acxd_slot_type") == [a, b]     # D9-1 must still see it


def test_rebuild_writes_once_and_refuses_placeholder_slot_types(monkeypatch):
    import tools.deterministic_repairs as dr

    saved, streamed = [], []
    monkeypatch.setattr(dr, "_session_id", lambda: "session-t")

    import tools.acxd_flow_spec as afs
    import tools.acxd_generation_context as agc
    import tools.acxd_resource_builders as arb
    import tools.asset_loader as al
    import tools.s3_asset_storage as s3s
    import tools.spec_manager as sm
    import tools.streaming_callback as sc

    monkeypatch.setattr(afs, "is_acxd_target", lambda sid: True)

    class _Spec:
        flows = []
    monkeypatch.setattr(afs, "get_acxd_flow_spec", lambda: _Spec())
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(agc, "_derive_slot_types", lambda plans, ops: [
        {"slotTypeId": "productType", "values": [{"value": "스탠드"}], "sensitive": False, "description": "", "metadata": {}},
        {"slotTypeId": "failReason", "values": [], "sensitive": False, "description": "", "metadata": {}},
    ])
    monkeypatch.setattr(arb, "build_slot_types", lambda spec: (spec["slot_types"], []))
    monkeypatch.setattr(al, "load_existing_asset", lambda asset_type, file_name=None, operation_id=None: None)
    monkeypatch.setattr(s3s, "save_asset_to_s3", lambda **kw: saved.append(kw) or f"assets/session-t/{kw['asset_type']}/{kw['file_name']}")
    monkeypatch.setattr(sc, "stream_asset", lambda *a, **kw: streamed.append(kw))
    monkeypatch.setattr(dr, "_remaining_d9", lambda sid, prefixes: [])

    class _Acxd:
        def model_dump(self):
            return {}
    monkeypatch.setattr(agc, "get_acxd_spec", lambda: _Acxd())
    import tools.acxd_data_request_builder as adr
    monkeypatch.setattr(adr, "build_all_data_requests", lambda spec: ([], []))

    result = dr.rebuild_acxd_slot_types_tool()
    # one canonical write, and the stream carries that key (no second nested save)
    assert [kw["asset_type"] for kw in saved] == ["acxd_slot_type"]
    assert streamed and streamed[0]["s3_key"] == "assets/session-t/acxd_slot_type/productType.json"
    assert result["slot_types"] == {"productType": "updated"}
    assert any("failReason" in p and "no values" in p for p in result["problems"])
    assert result["status"] == "updated"
