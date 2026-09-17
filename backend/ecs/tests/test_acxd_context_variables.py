"""Context variables: the bundle loader and the D9 schema gate.

Live (2026-09-17): a project with no context variables saved the generator's
normal wrapper ``{"contextVariables": []}``; the loader's ``or`` chain turned
the empty list into ``[doc]``, the wrapper itself was packaged as a variable,
every gate passed, and the runner failed at step 5 with "key is required".
"""
import json

import pytest

from tools import acxd_bundle
from tools.validate_acxd_consistency import validate_acxd_consistency


def _load_with_docs(monkeypatch, docs):
    def fake_read(_session_id, asset_type):
        return list(docs) if asset_type == acxd_bundle.ACXD_CONTEXT_VARIABLE_TYPE else []
    monkeypatch.setattr(acxd_bundle, "_read_json_docs", fake_read)
    return acxd_bundle.load_acxd_bundle("s-test")


def test_empty_context_variable_wrapper_yields_no_variables(monkeypatch):
    bundle = _load_with_docs(monkeypatch, [{"contextVariables": []}])
    assert bundle["context_variables"] == []


def test_context_variable_wrapper_items_are_unwrapped(monkeypatch):
    bundle = _load_with_docs(
        monkeypatch, [{"contextVariables": [{"name": "customerId", "type": "string"}]}])
    assert bundle["context_variables"] == [{"name": "customerId", "type": "string"}]


def test_bare_context_variable_documents_are_kept(monkeypatch):
    bundle = _load_with_docs(monkeypatch, [{"name": "orderNumber", "type": "text"}])
    assert bundle["context_variables"] == [{"name": "orderNumber", "type": "text"}]


def test_schema_gate_rejects_a_context_variable_without_a_name():
    violations = validate_acxd_consistency({"flows": [], "context_variables": [{"contextVariables": []}]})
    assert any("context_variables[0]" in str(v) and "name" in str(v) for v in violations)


@pytest.mark.parametrize("kind", ["boolean", "number", "string", "text"])
def test_schema_gate_accepts_every_sdk_context_variable_type(kind):
    """SDK ContextVariableType is BOOLEAN | NUMBER | STRING | TEXT; the builder
    emits 'string' by default, which the schema used to reject."""
    violations = validate_acxd_consistency(
        {"flows": [], "context_variables": [{"name": "x", "type": kind}]})
    assert not [v for v in violations if "context_variables" in str(v)], json.dumps(
        [str(v) for v in violations], ensure_ascii=False)
