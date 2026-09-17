"""The ACXD contract must stay tied to the SDK, not to anyone's memory.

The first implementation hand-transcribed these values from live probing. It
claimed a node type the API rejects (`application_handoff`), missed eight real
ones, and supported exactly one condition operator out of fourteen — which
quietly rewrote "refund over $500" as "refund equals $500". These tests exist
so that class of drift fails locally instead of at deploy time.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
CONTRACT_JSON = SRC / "schemas" / "acxd" / "contract.json"
FLOW_SCHEMA = SRC / "schemas" / "acxd" / "flow.schema.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load("acxd_contract", SRC / "tools" / "acxd_contract.py")


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(CONTRACT_JSON.read_text(encoding="utf-8"))


def test_contract_records_its_provenance(raw):
    """A generated file that cannot say where it came from is a liability."""
    assert raw["sdkPackage"] == "amazon-connect-acxd-sdk"
    assert raw["sdkVersion"], "SDK version must be recorded"
    assert "extract_acxd_contract.py" in raw["$comment"]


def test_flow_node_types_match_the_sdk(raw):
    assert len(raw["enums"]["FlowNodeType"]) == 21
    assert "application_handoff" not in raw["enums"]["FlowNodeType"]
    for expected in ("basic", "choice", "data_request", "end", "escalate",
                     "generative_text", "intent_capture", "knowledge_base",
                     "loop", "redirect", "start", "user_input", "wait"):
        assert expected in contract.NODE_TYPES


def test_all_fourteen_operators_survive(raw):
    """Every operator the service supports must be reachable."""
    assert len(contract.CONDITION_OPERATORS) == 14
    for op in ("eq", "neq", "gt", "gte", "lt", "lte", "contains",
               "not_contains", "exists", "not_exists", "matches_regex",
               "prefix", "suffix", "similar"):
        assert op in contract.CONDITION_OPERATORS
        assert contract.canonical_operator(op, True) == op


@pytest.mark.parametrize("raw_op,expected", [
    ("equals", "eq"), ("greater_than", "gt"), (">", "gt"), (">=", "gte"),
    ("not_equals", "neq"), ("regex", "matches_regex"), ("starts_with", "prefix"),
    ("includes", "contains"), ("is_set", "exists"),
])
def test_model_shorthands_map_onto_real_operators(raw_op, expected):
    assert contract.canonical_operator(raw_op, True) == expected


def test_unknown_operator_degrades_without_inventing_meaning():
    # a comparison with a value falls back to equality; without one, existence
    assert contract.canonical_operator("teleports_to", True) == "eq"
    assert contract.canonical_operator(None, False) == "exists"


def test_unary_operators_are_known():
    assert contract.UNARY_OPERATORS == frozenset({"exists", "not_exists"})


def test_service_observed_extras_are_kept(raw):
    """The service emits captured_flow, which SDK 0.1.0 does not model."""
    assert "captured_flow" in contract.OPERAND_TYPES
    assert "captured_flow" in raw["serviceObservedExtras"]["OperandType"]
    assert "captured_flow" in contract.TYPED_EDGE_LEFT_TYPES
    assert "node_status" in contract.TYPED_EDGE_LEFT_TYPES


def test_implicit_edges_cover_the_nodes_that_have_them():
    assert set(contract.IMPLICIT_EDGES) == {"user_input", "data_request"}
    for node_type, edges in contract.IMPLICIT_EDGES.items():
        assert len(edges) == 2, f"{node_type} needs both halves"
        for edge in edges:
            assert edge["conditions"], "an implicit edge needs its condition"


@pytest.mark.parametrize("bare,expected", [
    ("ko", "ko-KR"), ("en", "en-US"), ("ja", "ja-JP"), ("pt", "pt-BR"),
    ("zh", "zh-CN"), ("ko-KR", "ko-KR"), ("ko_kr", "ko-KR"), ("en-GB", "en-GB"),
])
def test_locales_resolve_against_the_sdk_list(bare, expected):
    """'ko' is rejected live; resolution uses the SDK's 87 LanguageCodes."""
    assert contract.canonical_language(bare) == expected
    assert contract.canonical_language(bare) in contract.LANGUAGE_CODES


def test_language_codes_are_full_locales():
    assert len(contract.LANGUAGE_CODES) > 50
    assert all("-" in code for code in contract.LANGUAGE_CODES)


def test_flow_schema_enum_tracks_the_contract(raw):
    """Schema and contract are generated together; they must not diverge."""
    schema = json.loads(FLOW_SCHEMA.read_text(encoding="utf-8"))
    node_enum = schema["$defs"]["node"]["properties"]["type"]["enum"]
    assert sorted(node_enum) == raw["enums"]["FlowNodeType"]
    condition = schema["$defs"]["condition"]
    assert condition["required"] == ["left", "operator"]
    assert sorted(condition["properties"]["operator"]["enum"]) == \
        raw["enums"]["ConditionOperator"]


def test_generative_condition_is_representable():
    """The SDK models an LLM-evaluated branch; the schema must allow it."""
    schema = json.loads(FLOW_SCHEMA.read_text(encoding="utf-8"))
    assert "generativeCondition" in schema["$defs"]["childNode"]["properties"]
