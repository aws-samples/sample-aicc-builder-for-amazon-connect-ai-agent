"""D9-9 — the sentences the requirements quote are in the generated flows verbatim.

Live (SELC e2e, 2026-09-26): the escalation plan carried the document's hand-off
line word for word, the interview gate passed, and the bundle's EscalationFlow
said the builder's reworded line. These tests pin the check and its agreement
with what the system flow builder now emits.
"""

from __future__ import annotations

import copy
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import tools.requirement_items as ri  # noqa: E402
import tools.validate_consistency as vc  # noqa: E402
from tools.acxd_system_flows import build_escalation_flow  # noqa: E402

HANDOFF = "상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다."
QUEUE = "현재 상담량이 많아 지연되고 있습니다. 잠시 후 다시 시도해 주세요."

LEDGER = {
    "items": [{"id": "R34", "text": "이관 안내"}],
    "mappings": {"Q1": {"target": "excluded", "note": "the customer's Contact Flow announces the queue"}},
    "signals": {"quotes": [
        {"id": "Q1", "item_id": "R34", "text": QUEUE},
        {"id": "Q2", "item_id": "R34", "text": HANDOFF},
    ]},
}

SPEC = {
    "business_profile": {"company_name": "삼성전자로지텍", "language": "ko-KR"},
    "application": {"locales": ["ko-KR"]},
    "flows": [{"flow_id": "EscalationFlow", "role": "escalation", "purpose": "handoff",
               "steps": [{"step": 1, "node_type": "escalate", "description": "이관 안내", "template": HANDOFF}]}],
}


def _check(monkeypatch, bundle, *, contact_flows=False, ledger=LEDGER):
    monkeypatch.setattr(ri, "load_ledger", lambda session_id=None: copy.deepcopy(ledger))
    return vc._d9_mandated_wording_checks(bundle, "s1", include_contact_flows=contact_flows)


def test_the_generated_escalation_flow_says_the_approved_line(monkeypatch):
    """The builder splits the line over two nodes; the caller still hears one sentence pair."""
    flow = build_escalation_flow(SPEC)
    assert len([n for n in flow["nodes"].values() if n.get("messages")]) == 2
    assert _check(monkeypatch, {"flows": [flow]}) == []


def test_a_reworded_line_is_reported_with_its_quote_id(monkeypatch):
    flow = build_escalation_flow({**SPEC, "flows": []})  # the language pack's "이전에 전달해 주신"
    issues = _check(monkeypatch, {"flows": [flow]})
    assert [i["id"] for i in issues] == ["D9-9"]
    assert issues[0]["quote_id"] == "Q2" and "이전 전달주신" in issues[0]["message"]
    assert issues[0]["severity"] == "error"


def test_an_excluded_quote_is_not_required(monkeypatch):
    flow = build_escalation_flow(SPEC)
    assert all(i["quote_id"] != "Q1" for i in _check(monkeypatch, {"flows": [flow]}))


def test_contact_flow_text_counts_only_when_the_bundle_ships_a_contact_flow(monkeypatch):
    contact_flow = {"Actions": [{"Type": "MessageParticipant", "Parameters": {"Text": HANDOFF}}]}
    bundle = {"flows": [], "contact_flows": [contact_flow]}
    assert _check(monkeypatch, bundle, contact_flows=True) == []
    assert [i["quote_id"] for i in _check(monkeypatch, bundle, contact_flows=False)] == ["Q2"]


def test_no_ledger_means_nothing_to_check(monkeypatch):
    assert _check(monkeypatch, {"flows": []}, ledger=None) == []


def test_run_d9_checks_includes_d9_9(monkeypatch):
    """Wired into the D9 run the review and the packager both read."""
    import tools.acxd_flow_spec as afs

    class _Spec:
        def model_dump(self):
            return copy.deepcopy(SPEC)

    monkeypatch.setattr(vc, "is_acxd_target", lambda session_id: True)
    monkeypatch.setattr(vc, "is_acxd_only_target", lambda session_id: True)
    monkeypatch.setattr(vc, "get_acxd_flow_spec", lambda session_id=None: _Spec())
    monkeypatch.setattr(vc, "load_acxd_bundle", lambda session_id: {"flows": [build_escalation_flow({**SPEC, "flows": []})]})
    monkeypatch.setattr(afs, "is_acxd_only_target", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(ri, "load_ledger", lambda session_id=None: copy.deepcopy(LEDGER))
    issues = vc.run_d9_checks("s1", classic_mismatches=[])
    assert any(i["id"] == "D9-9" and i.get("quote_id") == "Q2" for i in issues)
