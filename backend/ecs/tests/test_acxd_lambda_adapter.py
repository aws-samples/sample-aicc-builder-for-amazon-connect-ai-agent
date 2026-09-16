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
                                       "appointmentId": "A20260916", "name": "김하늘",
                                       "success": True}   # the envelope flag is derived when the handler omits it
    again, fields2 = inject(code, {"phoneNumber": r"^010-\d{4}-\d{4}$"})
    assert again == code and fields2 == []
    assert inject("def other(e, c): pass\n", {"phoneNumber": r"^010-\d{4}-\d{4}$"})[1] == []
    assert code.count(MARKER) == 1


def test_injected_wrapper_normalises_the_response_for_acxd():
    """Live (GAON v4): the create handler answered 201 with errorCode null; ACXD
    took the failure branch and the caller never heard the reservation number."""
    handler = ("import json\n"
               "def lambda_handler(event, context):\n"
               "    return {'statusCode': 201, 'body': json.dumps({'success': True, 'reservationId': 'RSV-1',"
               " 'errorCode': None, 'message': None})}\n")
    code, fields = inject(handler, {"appointmentId": r"^A\d{8}$"})   # nothing to restore, still wrapped
    assert fields == ["<response>"]
    module = types.ModuleType("h")
    exec(compile(code, "h.py", "exec"), module.__dict__)
    out = module.lambda_handler({"body": "{}"}, None)
    assert out["statusCode"] == 200
    assert json.loads(out["body"]) == {"success": True, "reservationId": "RSV-1", "errorCode": "", "message": ""}


def test_response_values_are_coerced_to_the_data_request_schema_types():
    """Live (GreenCart, 2026-09-14): the handler returned totalAmount "45000"
    where the Data Request's responseSchema says number; the service failed the
    whole reply and the order lookup went silent."""
    import json as _json
    from tools.acxd_lambda_adapter import inject
    code = (
        "import json\n"
        "def lambda_handler(event, context):\n"
        "    return {'statusCode': 200, 'body': json.dumps({'success': True, 'totalAmount': '45,000',"
        " 'quantity': '2', 'found': 'true', 'orderNumber': 20260901, 'note': None})}\n"
    )
    new_code, _ = inject(code, {}, response_types={
        "totalAmount": "number", "quantity": "integer", "found": "boolean", "orderNumber": "string", "note": "string"})
    ns: dict = {}
    exec(new_code, ns)  # noqa: S102 - executing the generated handler is the test
    out = ns["lambda_handler"]({}, None)
    body = _json.loads(out["body"])
    assert body["totalAmount"] == 45000 and isinstance(body["totalAmount"], int)
    assert body["quantity"] == 2 and body["found"] is True
    assert body["orderNumber"] == "20260901" and body["note"] == ""


def test_packager_reads_response_types_from_the_operations_data_request():
    from tools.asset_packager import _acxd_response_types
    bundle = {"data_requests": [
        {"dataRequestId": "getOrderStatus",
         "webhook": {"url": "{WEBHOOK_URL}/tools/get_order_status"},
         "responseSchema": {"type": "object", "properties": {"success": {"type": "boolean"}, "totalAmount": {"type": "number"}}}},
        {"dataRequestId": "requestReturn", "webhook": {"urls": {"development": "{WEBHOOK_URL}/tools/request_return"}},
         "responseSchema": {"type": "object", "properties": {"returnId": {"type": "string"}}}},
    ]}
    assert _acxd_response_types(bundle, "get_order_status") == {"success": "boolean", "totalAmount": "number"}
    assert _acxd_response_types(bundle, "request_return") == {"returnId": "string"}
    assert _acxd_response_types(bundle, "unknown_op") == {}


def test_a_time_typed_without_a_leading_zero_is_restored():
    """"9:30" reaches the slot as "930" (compact); the skeleton needs four digits."""
    from tools.acxd_lambda_adapter import restore
    assert restore("930", r"^\d{2}:\d{2}$") == "09:30"
    assert restore("1000", r"^\d{2}:\d{2}$") == "10:00"
    assert restore("123456789", r"^\d{10}$") is None      # an id one digit short is not padded


def test_a_reply_without_the_success_flag_gets_it_derived():
    """Live (GreenCart, 2026-09-14): get_return_status answered {found: true, …}
    with no `success`; the reply schema requires it, so the lookup escalated."""
    import json as _json
    from tools.acxd_lambda_adapter import inject
    code = (
        "import json\n"
        "def lambda_handler(event, context):\n"
        "    body = json.loads(event.get('body') or '{}')\n"
        "    if body.get('returnId') == 'RT-1':\n"
        "        return {'statusCode': 200, 'body': json.dumps({'found': True, 'status': '접수'})}\n"
        "    if body.get('returnId') == 'RT-0':\n"
        "        return {'statusCode': 200, 'body': json.dumps({'found': False, 'message': 'no'})}\n"
        "    return {'statusCode': 404, 'body': json.dumps({'errorCode': 'NOT_FOUND'})}\n"
    )
    new_code, _ = inject(code, {})
    ns: dict = {}
    exec(new_code, ns)  # noqa: S102
    ok = _json.loads(ns["lambda_handler"]({"body": _json.dumps({"returnId": "RT-1"})}, None)["body"])
    assert ok["success"] is True
    missing = _json.loads(ns["lambda_handler"]({"body": _json.dumps({"returnId": "RT-0"})}, None)["body"])
    assert missing["success"] is False
    err = _json.loads(ns["lambda_handler"]({"body": _json.dumps({"returnId": "x"})}, None)["body"])
    assert err["success"] is False


def test_a_value_that_fits_only_another_field_is_moved_there():
    """Live (2026-09-15): the return-status flow asks for a return number
    (^RT-\\d{6}$) and then an order number (^GC-\\d{8}$), both NLX.AlphaNumeric. The
    runtime does not enforce the attached regex, so an order number typed at the
    return-number prompt arrived as returnId="GC20260902" and the lookup escalated
    the caller. The wrapper moves it onto orderNumber (separators restored)."""
    from tools.acxd_lambda_adapter import reassign_misfiled

    patterns = {"returnId": r"^RT-\d{6}$", "orderNumber": r"^GC-\d{8}$", "contactPhone": r"^010-\d{4}-\d{4}$"}
    data = {"returnId": "GC20260902"}
    assert reassign_misfiled(data, patterns) is True
    assert data == {"orderNumber": "GC-20260902"}
    # a value that fits its own field, or fits nothing, or fits two candidates, stays put
    for payload in ({"returnId": "RT100001"}, {"returnId": "hello"},
                    {"returnId": "GC20260902", "orderNumber": "GC-20260901"}):
        before = dict(payload)
        assert reassign_misfiled(payload, patterns) is False and payload == before

    handler = ("import json\n"
               "def lambda_handler(event, context):\n"
               "    return {'statusCode': 200, 'body': event['body']}\n")
    code, _ = inject(handler, patterns)
    module = types.ModuleType("h")
    exec(compile(code, "h.py", "exec"), module.__dict__)
    out = module.lambda_handler({"body": json.dumps({"returnId": "GC20260902", "nlx_context": {}})}, None)
    assert json.loads(out["body"]) == {"orderNumber": "GC-20260902", "nlx_context": {}, "success": True}
