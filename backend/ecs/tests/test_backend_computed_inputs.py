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
