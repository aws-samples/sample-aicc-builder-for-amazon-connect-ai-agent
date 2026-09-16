"""ACXDFlowSpec contract tests — determinism confirmation rules, readiness
validation, persistence, and the runtime-target precedence chain."""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools import acxd_flow_spec as afs  # noqa: E402
from tools.session_context import current_session_id  # noqa: E402

SID = "test-acxd-flow-spec"


@pytest.fixture(autouse=True)
def _session(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    (tmp_path / "sessions" / SID / "state").mkdir(parents=True)
    afs.clear_runtime_target(SID)
    tok = current_session_id.set(SID)
    yield tmp_path
    current_session_id.reset(tok)
    afs.clear_runtime_target(SID)


def test_runtime_target_survives_without_nfs_mount(tmp_path, monkeypatch):
    """The ECS task may run without the S3 Files mount (seen live on dev)."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path / "missing-mount"))
    assert afs.set_runtime_target(SID, "acxd")
    assert afs.get_runtime_target(SID) == "acxd"


def _steps():
    return [
        {"step": 1, "description": "Ask for the order id", "node_type": "user_input",
         "determinism": "deterministic", "decision_category": "general"},
        {"step": 2, "description": "Look up the order", "node_type": "data_request",
         "determinism": "deterministic"},
        {"step": 3, "description": "Decide whether the refund is auto-approved",
         "node_type": "generative_text", "determinism": "generative",
         "decision_category": "refund"},
        {"step": 4, "description": "Explain the outcome", "node_type": "generative_text",
         "determinism": "deterministic"},
    ]


def _upsert(**over):
    kwargs = dict(flow_id="ProcessReturn", purpose="Refunds", role="operation",
                  operation_id="process_return", steps=_steps())
    kwargs.update(over)
    return afs.upsert_acxd_flow_plan(**kwargs)


# --- upsert / determinism policy -----------------------------------------

def test_upsert_never_records_confirmation_and_coerces_money_steps():
    res = _upsert(steps=[dict(s, user_confirmed=True) for s in _steps()])
    assert res["success"]
    assert res["awaiting_confirmation"] == [1, 2, 3, 4]
    plan = afs.get_acxd_flow_spec().flow("ProcessReturn")
    s3 = plan.steps[2]
    assert s3.determinism == "deterministic" and s3.node_type == "choice"
    assert any("refund" in n for n in res["coerced"])
    # generative node type forces the generative label
    assert plan.steps[3].determinism == "generative"
    # data_request step defaults to the operation's data request
    assert plan.steps[1].data_request_id == "process_return"


def test_upsert_rejects_bad_flow_id_and_missing_operation():
    assert not _upsert(flow_id="process-return")["success"]
    assert not _upsert(flow_id="Ab")["success"]
    assert not _upsert(operation_id=None)["success"]
    assert not _upsert(role="nope")["success"]


def test_confirm_then_reupsert_keeps_unchanged_and_resets_changed():
    _upsert()
    res = afs.confirm_acxd_flow_steps("ProcessReturn")
    assert res["awaiting_confirmation"] == [] and res["flow_approved"]

    changed = _steps()
    changed[0]["determinism"] = "generative"         # deterministic → generative: a real change
    changed[0]["node_type"] = "generative_text"
    res = _upsert(steps=changed)
    assert res["awaiting_confirmation"] == [1]
    plan = afs.get_acxd_flow_spec().flow("ProcessReturn")
    assert plan.steps[0].confirmation_pending_reason
    assert plan.steps[1].user_confirmed
    assert plan.confirmed is False                    # a changed decision re-opens the plan


def test_node_type_name_correction_keeps_confirmation():
    steps = _steps()
    steps[3]["node_type"] = "generative_message"      # invented name, same generative class
    res = _upsert(steps=steps)
    assert any("normalized to 'generative_text'" in n for n in res["coerced"])
    afs.confirm_acxd_flow_steps("ProcessReturn")
    fixed = _steps()                                  # now the real name
    res = _upsert(steps=fixed)
    assert res["awaiting_confirmation"] == []
    assert res["flow_approved"] is True


def test_unknown_node_type_is_rejected_with_catalogue():
    steps = _steps()
    steps[0]["node_type"] = "teleport"
    res = _upsert(steps=steps)
    assert not res["success"] and "teleport" in res["error"] and "user_input" in res["error"]


@pytest.mark.parametrize("raw,expected", [
    ("message", "basic"), ("generative_message", "generative_text"),
    ("escalation(native)", "escalate"), ("end_call", "end"), ("branch", "choice"),
    ("Data Request", "data_request"), ("kb", "knowledge_base"), ("user_input", "user_input"),
    # intent_capture is not a deployable node, and intent routing is user_input +
    # a redirect to {System.capturedFlow:NLX.System} — NOT a generative journey
    # (live 2026-09-12: an LLM-classifier welcome flow routed nothing).
    ("intent_capture", "user_input"), ("nonsense", None),
])
def test_canonical_node_type(raw, expected):
    assert afs.canonical_node_type(raw) == expected


def test_partial_confirmation_does_not_approve_flow():
    _upsert()
    res = afs.confirm_acxd_flow_steps("ProcessReturn", step_numbers=[1, 2])
    assert res["awaiting_confirmation"] == [3, 4]
    assert res["flow_approved"] is False


def test_confirm_unknown_flow_fails():
    assert not afs.confirm_acxd_flow_steps("Nope")["success"]


# --- readiness validation -------------------------------------------------

def _full_spec():
    _upsert()
    afs.confirm_acxd_flow_steps("ProcessReturn")
    for role, fid in (("welcome", "WelcomeFlow"), ("fallback", "FallbackFlow"),
                      ("escalation", "EscalationFlow")):
        afs.upsert_acxd_flow_plan(flow_id=fid, purpose=role, role=role,
                                  steps=[{"step": 1, "description": role, "node_type": "basic"}])
        afs.confirm_acxd_flow_steps(fid)
    return afs.get_acxd_flow_spec()


def test_validate_ready_spec_has_no_problems():
    spec = _full_spec()
    assert afs.validate_acxd_flow_spec(spec, {"process_return"}) == []


def test_validate_reports_unconfirmed_missing_system_flows_and_unknown_operation():
    _upsert()
    spec = afs.get_acxd_flow_spec()
    problems = afs.validate_acxd_flow_spec(spec, {"other_op"})
    joined = "\n".join(problems)
    assert "not confirmed by the user" in joined
    assert "not approved by the user" in joined
    assert "missing system flow with role 'welcome'" in joined
    assert "has no OperationSpec" in joined


def test_validate_application_and_guardrail_constraints():
    spec = _full_spec()
    afs.save_acxd_policies(guardrails=[{"name": "Abuse", "policy": "route abuse",
                                        "action": "route", "route_flow_id": "Missing"}])
    afs.save_acxd_application_settings(
        channels=["voice", "fax"], locales=["ko"], speech_engine="agentic_voice",
        context_variables=[{"name": f"v{i}"} for i in range(11)])
    spec = afs.get_acxd_flow_spec()
    assert spec.application.locales == ["ko-KR"]       # canonicalized
    problems = "\n".join(afs.validate_acxd_flow_spec(spec, {"process_return"}))
    assert "route_flow_id" in problems
    assert "unknown channel 'fax'" in problems
    assert "at most 10 context variables" in problems


def test_acxd_flow_spec_ready_wrapper():
    ok, problems = afs.acxd_flow_spec_ready()
    assert not ok and problems
    _full_spec()
    ok, problems = afs.acxd_flow_spec_ready()
    # OperationSpec 'process_return' is not saved in this test session
    assert not ok and any("has no OperationSpec" in p for p in problems)


# --- persistence ----------------------------------------------------------

def test_persists_to_state_dir_and_restores(tmp_path):
    _upsert()
    path = tmp_path / "sessions" / SID / "state" / "acxd_flow_spec.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["flows"][0]["flow_id"] == "ProcessReturn"
    assert afs.get_acxd_flow_spec().flow("ProcessReturn").purpose == "Refunds"


def test_get_spec_without_session_is_none():
    tok = current_session_id.set(None)
    try:
        assert afs.get_acxd_flow_spec() is None
    finally:
        current_session_id.reset(tok)


# --- runtime target -------------------------------------------------------

def test_runtime_target_defaults_to_classic():
    assert afs.get_runtime_target() == "classic"
    assert afs.is_acxd_target() is False


def test_runtime_target_seed_file(tmp_path):
    assert afs.set_runtime_target(SID, "acxd")
    assert (tmp_path / "sessions" / SID / "state" / "runtime_target.json").is_file()
    assert afs.get_runtime_target() == "acxd"
    assert afs.is_acxd_target()
    assert not afs.set_runtime_target(SID, "lex")


def test_infrastructure_spec_inherits_runtime_target():
    from tools import spec_manager as sm
    afs.set_runtime_target(SID, "acxd")
    res = sm.save_infrastructure_spec(project_name="acme", db_type="dynamodb")
    assert res.get("success", True), res
    infra = sm.get_infrastructure_spec()
    assert infra is not None and infra.runtime_target == "acxd"
    # the saved spec now wins even if the seed disappears
    afs.set_runtime_target(SID, "classic")
    assert afs.get_runtime_target() == "acxd"


def test_parallel_mutations_do_not_lose_updates():
    """Strands runs a turn's tool calls in parallel; no upsert may be lost."""
    import concurrent.futures
    from tools.session_context import current_session_id as _csid

    def work(i):
        tok = _csid.set(SID)
        try:
            return afs.upsert_acxd_flow_plan(
                flow_id=f"Flow{chr(65 + i)}", purpose=str(i), role="welcome" if i == 0 else "operation",
                operation_id=None if i == 0 else f"op{i}",
                steps=[{"step": 1, "description": "x", "node_type": "basic"}])
        finally:
            _csid.reset(tok)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(8)))
    assert all(r["success"] for r in results), results
    assert len(afs.get_acxd_flow_spec().flows) == 8


def test_list_parameters_accept_json_strings():
    import json as _json
    res = _upsert(steps=_json.dumps(_steps()))
    assert res["success"], res
    assert afs.confirm_acxd_flow_steps("ProcessReturn", step_numbers="[1, 2]")["awaiting_confirmation"] == [3, 4]
    res = afs.save_acxd_policies(guardrails='[{"name":"PII","policy":"mask cards","action":"mask"}]',
                                 kb_topics='["shipping","returns"]')
    assert res["success"] and res["guardrail_count"] == 1 and res["kb_topics"] == 2
    res = afs.save_acxd_application_settings(channels='["voice","chat"]', locales='["ko","en-US"]')
    assert res["application"]["locales"] == ["ko-KR", "en-US"]
    bad = _upsert(steps="not json")
    assert not bad["success"] and "JSON" in bad["error"]


def test_every_operation_needs_a_flow_plan():
    spec = _full_spec()
    problems = afs.validate_acxd_flow_spec(spec, {"process_return", "track_order"})
    assert any("operation 'track_order' has no ACXD flow plan" in p for p in problems)


# --- one flow per system role (live: WelcomeFlow + Welcome shipped 9 flows) ---

def test_second_flow_for_a_system_role_is_refused_and_remove_frees_it():
    assert _upsert(flow_id="WelcomeFlow", purpose="greet", role="welcome", operation_id=None)["success"]
    dup = _upsert(flow_id="Welcome", purpose="greet", role="welcome", operation_id=None)
    assert not dup["success"]
    assert "WelcomeFlow" in dup["error"] and "remove_acxd_flow_plan" in dup["error"]
    # re-upserting the SAME flow id is still allowed
    assert _upsert(flow_id="WelcomeFlow", purpose="greet again", role="welcome", operation_id=None)["success"]

    removed = afs.remove_acxd_flow_plan("WelcomeFlow")
    assert removed["success"] and removed["removed"] == "WelcomeFlow"
    assert "WelcomeFlow" not in removed["flows"]
    assert _upsert(flow_id="Welcome", purpose="greet", role="welcome", operation_id=None)["success"]
    assert not afs.remove_acxd_flow_plan("Nope")["success"]


def test_validate_flags_duplicate_system_roles_in_a_loaded_spec():
    spec = afs.ACXDFlowSpec.model_validate({"flows": [
        {"flow_id": "WelcomeFlow", "purpose": "a", "role": "welcome", "confirmed": True, "steps": []},
        {"flow_id": "Welcome", "purpose": "b", "role": "welcome", "confirmed": True, "steps": []},
    ]})
    problems = afs.validate_acxd_flow_spec(spec)
    assert any("system role 'welcome' is planned by 2 flows" in p for p in problems)


def test_other_sessions_infra_spec_never_answers_for_a_fresh_session():
    """Live on dev: the connect handler asked about a brand-new session while the
    ContextVar still named the previous (classic) session; the previous
    session's InfrastructureSpec answered, `connected` echoed classic, and the
    start screen's ACXD choice was overwritten before it was sent."""
    from tools import spec_manager as sm
    afs.set_runtime_target(SID, "classic")
    res = sm.save_infrastructure_spec(project_name="acme", db_type="dynamodb")
    assert res.get("success", True), res
    fresh = "session-fresh-0000-4000-8000-000000000001"
    assert afs.get_runtime_target_if_set(fresh) is None    # nothing persisted → no echo
    assert afs.get_runtime_target(fresh) == "classic"      # default, not a leak
    assert afs.set_runtime_target(fresh, "acxd")
    assert afs.get_runtime_target_if_set(fresh) == "acxd"
    assert afs.get_runtime_target(fresh) == "acxd"
    afs.clear_runtime_target(fresh)


# --- generative journeys: capture policy, steps-only re-upsert, style -------

_JOURNEY_SLOTS = [
    {"name": "orderNumber", "type": "NLX.AlphaNumeric", "regex": "^GC-[0-9]{8}$"},
    {"name": "contactPhone", "type": "NLX.PhoneNumber"},
    {"name": "reason", "type": "reason"},
    {"name": "note", "type": "text"},
]


def _journey_steps():
    return [
        {"step": 1, "description": "주문번호 확인", "node_type": "user_choice", "slot": "orderNumber"},
        {"step": 2, "description": "사유와 상황을 자유롭게 듣고 정리", "node_type": "generative_journey",
         "determinism": "generative", "captures": ["reason", "note", "orderNumber", "ghost"],
         "journey_tools": ["knowledge_base", "requestReturn"]},
        {"step": 3, "description": "연락처 확인", "node_type": "user_choice", "slot": "contactPhone"},
        {"step": 4, "description": "접수", "node_type": "data_request"},
        {"step": 5, "description": "후속", "node_type": "redirect"},
    ]


def test_capture_policy_keeps_journeys_to_conversational_values():
    res = _upsert(steps=_journey_steps(), slots=_JOURNEY_SLOTS)
    assert res["success"], res
    plan = afs.get_acxd_flow_spec().flow("ProcessReturn")
    journey = next(s for s in plan.steps if s.node_type == "generative_journey")
    assert journey.captures == ["reason", "note"]            # strict-format and unknown slots dropped
    assert journey.journey_tools == ["knowledge_base"]       # a data request is not a journey tool here
    notes = " | ".join(res["coerced"])
    assert "orderNumber" in notes and "strict format" in notes
    assert "ghost" in notes and "requestReturn" in notes
    # a non-journey step never carries captures
    res2 = _upsert(steps=[{"step": 1, "description": "x", "node_type": "basic", "captures": ["reason"]}],
                   slots=_JOURNEY_SLOTS)
    assert afs.get_acxd_flow_spec().flow("ProcessReturn").steps[0].captures == []
    assert any("generative_journey steps only" in n for n in res2["coerced"])


def test_steps_only_and_slots_only_reupserts_keep_the_other_half():
    """Live (2026-09-16): a slots-only re-upsert wiped the confirmed steps
    (step_count 0) and the orchestrator spent a turn restoring them."""
    _upsert(steps=_journey_steps(), slots=_JOURNEY_SLOTS)
    afs.confirm_acxd_flow_steps("ProcessReturn")
    res = _upsert(steps=None, slots=_JOURNEY_SLOTS + [{"name": "extra", "type": "text"}])
    plan = afs.get_acxd_flow_spec().flow("ProcessReturn")
    assert res["step_count"] == 5 and plan.confirmed is True
    assert any("kept the 5 planned steps" in n for n in res["coerced"])
    assert {s.name for s in plan.slots} == {"orderNumber", "contactPhone", "reason", "note", "extra"}
    res2 = _upsert(steps=_journey_steps(), slots=None)      # steps-only keeps the slots
    assert {s.name for s in afs.get_acxd_flow_spec().flow("ProcessReturn").slots} >= {"extra"}


def test_validate_requires_captures_and_deterministic_strict_slots_next_to_a_journey():
    steps = _journey_steps()
    steps[1]["captures"] = []                                # journey with nothing to collect
    del steps[2]["slot"]                                     # phone capture no longer names its slot
    _upsert(steps=steps, slots=_JOURNEY_SLOTS)
    afs.confirm_acxd_flow_steps("ProcessReturn")
    problems = afs.validate_acxd_flow_spec(afs.get_acxd_flow_spec())
    assert any("must name the slots it collects in 'captures'" in p for p in problems)
    assert any("slot 'contactPhone' has a strict format and no user_choice step declares" in p for p in problems)
    assert not any("slot 'orderNumber'" in p for p in problems)   # step 1 declares it


def test_conversation_style_defaults_to_generative_and_accepts_aliases():
    assert afs.ACXDFlowSpec().application.conversation_style == "generative"
    res = afs.save_acxd_application_settings(conversation_style="시나리오")
    assert res["success"] and res["application"]["conversation_style"] == "scripted"
    res = afs.save_acxd_application_settings(conversation_style="journey")
    assert res["application"]["conversation_style"] == "generative"
    bad = afs.save_acxd_application_settings(conversation_style="whatever")
    assert not bad["success"] and "conversation_style" in bad["error"]
