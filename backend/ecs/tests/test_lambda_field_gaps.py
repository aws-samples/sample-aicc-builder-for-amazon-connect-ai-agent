"""Save-time spec↔handler check for one Lambda (D1 per asset, at generation time)."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class _Field:
    def __init__(self, name):
        self.name = name


class _Spec:
    def __init__(self, inputs, outputs):
        self.input_fields = [_Field(n) for n in inputs]
        self.output_fields = [_Field(n) for n in outputs]


def test_reports_unread_inputs_and_unwritten_outputs_including_envelope(monkeypatch):
    import tools.validate_consistency as vc
    import tools.spec_manager as sm
    monkeypatch.setattr(sm, "get_all_specs", lambda: {"get_order": _Spec(["orderId", "phone"], ["status", "eta"])})
    monkeypatch.setattr(sm, "get_all_tools", lambda: [])
    code = '''
def handler(event, context):
    body = json.loads(event["body"])
    order_id = body.get("orderId")
    return {"statusCode": 200, "body": json.dumps({"success": True, "status": "shipped", "errorCode": None, "message": ""})}
'''
    gaps = vc.lambda_field_gaps("get_order", code)
    assert gaps["checked"] is True
    assert gaps["missing_inputs"] == ["phone"]
    assert gaps["missing_outputs"] == ["eta"]


def test_unknown_operation_is_not_checked(monkeypatch):
    import tools.validate_consistency as vc
    import tools.spec_manager as sm
    monkeypatch.setattr(sm, "get_all_specs", lambda: {})
    monkeypatch.setattr(sm, "get_all_tools", lambda: [])
    assert vc.lambda_field_gaps("ghost", "x = 1")["checked"] is False
