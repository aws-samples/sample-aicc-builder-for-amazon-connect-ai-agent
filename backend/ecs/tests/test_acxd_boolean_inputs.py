"""ACXD has no boolean built-in: a yes/no the caller says is a yesNo slot and the
data request posts the word. Live (SELC, 2026-09-21): privacyConsent was boolean
in the spec, the OpenAPI said boolean, the flow captured "예"/"아니오" — two
blocking parity findings, and a Lambda that would have rejected the value."""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture()
def bound(monkeypatch, tmp_path):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.spec_manager as sm
    from tools.session_context import current_session_id

    token = current_session_id.set("session-bool-norm-test")
    monkeypatch.setattr(sm, "_nfs_persist_spec", lambda *a, **k: None)
    yield sm
    sm._specs_bucket().clear()
    current_session_id.reset(token)


def test_boolean_inputs_become_spoken_yes_no_enums(bound):
    sm = bound
    from tools.spec_manager import OperationSpec, FieldSpec
    sm._specs_bucket()["create_cleaning_reservation"] = OperationSpec(
        operation_id="create_cleaning_reservation",
        input_fields=[FieldSpec(name="privacyConsent", field_type="boolean", description="개인정보 동의"),
                      FieldSpec(name="quantity", field_type="integer"),
                      FieldSpec(name="productType", field_type="string", enum_values=["벽걸이실내기", "스탠드"])],
        output_fields=[FieldSpec(name="reservationId", field_type="string"),
                       FieldSpec(name="confirmed", field_type="boolean")],
    )
    changed = sm.normalize_boolean_inputs_for_acxd("ko-KR")
    assert changed == ["create_cleaning_reservation.privacyConsent"]
    spec = sm._specs_bucket()["create_cleaning_reservation"]
    consent = next(f for f in spec.input_fields if f.name == "privacyConsent")
    assert consent.field_type == "string" and consent.enum_values == ["예", "아니오"]
    assert "예 = true" in consent.description and consent.description.startswith("개인정보 동의")
    # other inputs and OUTPUT booleans are untouched (the API returns real booleans)
    assert next(f for f in spec.input_fields if f.name == "quantity").field_type == "integer"
    assert next(f for f in spec.output_fields if f.name == "confirmed").field_type == "boolean"
    # idempotent, and English callers get English words
    assert sm.normalize_boolean_inputs_for_acxd("ko-KR") == []
    sm._specs_bucket()["op2"] = OperationSpec(operation_id="op2",
                                              input_fields=[FieldSpec(name="agree", field_type="bool")],
                                              output_fields=[FieldSpec(name="ok", field_type="string")])
    assert sm.normalize_boolean_inputs_for_acxd("en-US") == ["op2.agree"]
    assert next(f for f in sm._specs_bucket()["op2"].input_fields).enum_values == ["yes", "no"]
