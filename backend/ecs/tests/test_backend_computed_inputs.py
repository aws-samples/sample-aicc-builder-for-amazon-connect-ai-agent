"""Live (2026-09-15): an interview built from a requirements document that listed
`refundAmount` as an input saved it as a slot, so the return flow asked the caller
for the refund amount. save_operation_spec now returns an advisory for such fields."""
from tools.spec_manager import FieldSpec, backend_computed_input_warnings


def _f(name, description=None):
    return FieldSpec(name=name, field_type="string", description=description)


def test_backend_computed_inputs_are_flagged():
    warnings = backend_computed_input_warnings([
        _f("orderNumber", "GC- + 8 digits"),
        _f("refundAmount", "KRW, at most the order total"),
        _f("unitPrice"),
        _f("approvalStatus"),
        _f("status"),
        _f("amount", "환불 금액(총액 이하)"),
    ])
    flagged = [w.split("'")[1] for w in warnings]
    assert flagged == ["refundAmount", "unitPrice", "approvalStatus", "status", "amount"]
    assert "output_fields" in warnings[0]


def test_values_the_caller_knows_are_quiet():
    assert backend_computed_input_warnings([
        _f("orderNumber"), _f("returnId"), _f("contactPhone", "010-XXXX-XXXX"),
        _f("reason", "단순변심 | 상품불량"), _f("feeAgreed", "배송비 3,000원 동의 여부"),
        _f("reservationDate"), _f("amount", "송금할 금액"), _f("quantity"),
    ]) == []
    assert backend_computed_input_warnings([{"name": "returnNumber"}]) == []
    assert backend_computed_input_warnings(None) == []


def test_tool_fields_follow_an_operation_field_update():
    """Live (2026-09-15): `refundAmount` was moved from the operation's inputs to its
    outputs, but the primary tool kept it as a required input, so the generated
    Data Request demanded a slot no flow collected (D3)."""
    from tools.spec_manager import ToolSpec, reconcile_tool_fields

    old_in = [_f("orderNumber"), _f("reason"), _f("refundAmount"), _f("contactPhone")]
    new_in = [_f("orderNumber"), _f("reason"), _f("contactPhone")]
    old_out = [_f("returnId"), _f("status")]
    new_out = [_f("returnId"), _f("status"), _f("refundAmount")]
    primary = ToolSpec(tool_id="request_return", role="primary",
                       input_fields=[_f("order_number"), _f("reason"), _f("refund_amount"), _f("contact_phone")],
                       output_fields=[_f("returnId"), _f("status")])
    helper = ToolSpec(tool_id="lookup_order", role="helper",
                      input_fields=[_f("orderNumber"), _f("refundAmount")], output_fields=[_f("totalAmount")])

    _, notes = reconcile_tool_fields([primary, helper], old_in, new_in, old_out, new_out)

    # the mirroring primary tool follows the operation on both sides (snake spellings compare equal)
    assert [f.name for f in primary.input_fields] == ["orderNumber", "reason", "contactPhone"]
    assert [f.name for f in primary.output_fields] == ["returnId", "status", "refundAmount"]
    # the helper only loses the field the operation no longer has; its own outputs are untouched
    assert [f.name for f in helper.input_fields] == ["orderNumber"]
    assert [f.name for f in helper.output_fields] == ["totalAmount"]
    assert len(notes) == 3
    # an update that names only output_fields leaves the inputs alone
    _, notes2 = reconcile_tool_fields([helper], old_in, None, old_out, new_out)
    assert [f.name for f in helper.input_fields] == ["orderNumber"] and notes2 == []
