"""spec_completeness — what generation would have to guess is reported before the handoff."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.spec_completeness import acxd_plan_problems, operation_spec_problems  # noqa: E402


def test_operation_gaps_name_the_field_and_what_to_do():
    spec = {
        "operation_id": "get_price",
        "input_fields": [
            {"name": "productType", "field_type": "enum"},               # enum without values
            {"name": "quantity"},                                        # no type
            {"name": "items", "field_type": "array"},                    # array without items
            {"name": "address", "field_type": "object"},                 # object without properties
            {"name": "when", "field_type": "moment"},                    # unknown type
        ],
        "output_fields": [],
    }
    problems = operation_spec_problems("get_price", spec)
    joined = "\n".join(problems)
    assert "no output_fields" in joined
    assert "'productType' is a enum without enum_values" in joined
    assert "'quantity' has no field_type" in joined
    assert "'items' is an array without `items`" in joined
    assert "'address' is an object without `properties`" in joined
    assert "unknown field_type 'moment'" in joined


def test_complete_operation_has_no_gaps():
    spec = {
        "operation_id": "get_price",
        "input_fields": [{"name": "productType", "field_type": "enum", "enum_values": ["A", "B"]}],
        "output_fields": [{"name": "unitPrice", "field_type": "number"}],
    }
    assert operation_spec_problems("get_price", spec) == []


def test_acxd_plan_gaps_slot_without_field_and_escalation_without_payload():
    specs = {"get_price": {"input_fields": [{"name": "product_type", "field_type": "string"}]}}
    flow_spec = {
        "application": {"context_variables": []},
        "flows": [
            {"flow_id": "PriceInquiry", "role": "operation", "operation_id": "get_price",
             "slots": [{"name": "productType"}, {"name": "quantity", "field_name": "quantity"}],
             "steps": [{"node_type": "user_choice"}, {"node_type": "escalate"}],
             "escalation_conditions": ""},
            {"flow_id": "Welcome", "role": "welcome", "slots": [{"name": "anything"}]},
        ],
    }
    problems = acxd_plan_problems(flow_spec, specs)
    joined = "\n".join(problems)
    # camelCase ↔ snake_case spellings are the same field: productType is fine, quantity is not
    assert "slot 'productType'" not in joined
    assert "slot 'quantity' maps to no input field" in joined
    assert "no escalation_conditions" in joined
    assert "declares no context_variables" in joined
    # system flows (welcome/fallback/escalation) are not operation flows
    assert "Welcome" not in joined
