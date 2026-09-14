"""Live (Hanbit, 2026-09-14): ACXD delivered 010-1111-2222 as 01011112222,
2026-09-16 as 20260916 and 10:00 as 1000; the Lambda validated the spec format
and rejected every booking."""
import json
import types

import pytest

from tools.acxd_lambda_adapter import MARKER, inject, restore, restorable_patterns, skeleton


@pytest.mark.parametrize("value,pattern,expected", [
    ("01011112222", r"^010-\d{4}-\d{4}$", "010-1111-2222"),
    ("20260916", r"^\d{4}-\d{2}-\d{2}$", "2026-09-16"),
    ("1000", r"^\d{2}:\d{2}$", "10:00"),
    ("GC20260901", r"^GC-\d{8}$", "GC-20260901"),
    ("gc20260901", r"^GC-\d{8}$", "GC-20260901"),
    ("010-1111-2222", r"^010-\d{4}-\d{4}$", None),      # already in shape
    ("A20260916", r"^A\d{8}$", None),                   # nothing to restore
    ("1234567890", r"^[0-9]{10}$", None),
    ("0101111222", r"^010-\d{4}-\d{4}$", None),         # too short: never guess
    ("abc", r"^(a|b)+$", None),                         # unsupported construct
])
def test_restore(value, pattern, expected):
    assert restore(value, pattern) == expected


def test_skeleton_and_restorable_patterns():
    assert skeleton(r"^010-\d{4}-\d{4}$") == [("lit", "010"), ("sep", "-"), ("run", "dddd"), ("sep", "-"), ("run", "dddd")]
    assert skeleton(r"^\d{10}$") is None
    assert restorable_patterns({"phone": r"^010-\d{4}-\d{4}$", "id": r"^A\d{8}$", "x": None}) == {"phone": r"^010-\d{4}-\d{4}$"}


def test_injected_wrapper_restores_the_payload_and_is_idempotent():
    handler = ("import json\n"
               "def lambda_handler(event, context):\n"
               "    return {'statusCode': 200, 'body': event['body']}\n")
    code, fields = inject(handler, {"phoneNumber": r"^010-\d{4}-\d{4}$", "date": r"^\d{4}-\d{2}-\d{2}$",
                                    "appointmentId": r"^A\d{8}$", "note": None})
    assert fields == ["date", "phoneNumber"]
    module = types.ModuleType("h")
    exec(compile(code, "h.py", "exec"), module.__dict__)
    out = module.lambda_handler({"body": json.dumps({"phoneNumber": "01011112222", "date": "20260916",
                                                     "appointmentId": "A20260916", "name": "김하늘"})}, None)
    assert json.loads(out["body"]) == {"phoneNumber": "010-1111-2222", "date": "2026-09-16",
                                       "appointmentId": "A20260916", "name": "김하늘"}
    again, fields2 = inject(code, {"phoneNumber": r"^010-\d{4}-\d{4}$"})
    assert again == code and fields2 == []
    assert inject(handler, {"appointmentId": r"^A\d{8}$"}) == (handler, [])       # nothing restorable
    assert inject("def other(e, c): pass\n", {"phoneNumber": r"^010-\d{4}-\d{4}$"})[1] == []
    assert code.count(MARKER) == 1
