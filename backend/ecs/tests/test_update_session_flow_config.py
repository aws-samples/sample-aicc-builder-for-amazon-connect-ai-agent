"""update_session_flow_config — the post-interview editor for the session flow
config. Live (2026-09-20): after the interview the orchestrator had only the
read-only getter, so a session tool the customer had excluded kept the count
gate red and a corrupted Korean retry message could not be repaired."""
import pytest


@pytest.fixture()
def bound_session(monkeypatch, tmp_path):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.spec_manager as sm
    from tools.session_context import current_session_id

    token = current_session_id.set("session-update-cfg-test")
    monkeypatch.setattr(sm, "_persist_flow_cfg", lambda cfg: sm._set_flow_cfg(cfg))
    yield sm
    sm._set_flow_cfg(None)
    current_session_id.reset(token)


def test_update_merges_and_keeps_the_rest(bound_session):
    sm = bound_session
    saved = sm.save_session_flow_config(
        call_direction="inbound",
        agent_persona="삼성전자로지텍 AI 상담원",
        common_greeting="안녕하세요",
        no_response_policy={"max_retries": 2,
                            "retry_message": "고객님, 말자을 잡지 모했습니다. 다시 한 번 말씀해 주시갞에요?",
                            "final_message": "상담원을 연결해 드리겠습니다.", "final_action": "escalate"},
        session_tools=[{"tool_id": "log_call_result", "role": "session", "summary": "통화 결과 기록"},
                       {"tool_id": "get_outbound_targets", "role": "session", "summary": "아웃바운드 대상"}],
    )
    assert saved["success"] is True

    result = sm.update_session_flow_config(
        no_response_policy={"retry_message": "고객님, 말씀을 잘 알아듣지 못했습니다. 다시 한 번 말씀해 주시겠어요?"},
        remove_session_tools=["log_call_result"],
        session_tool_flags={"get_outbound_targets": {"generate_openapi": False}},
    )
    assert result["success"] is True, result
    cfg = sm.get_session_flow_config()
    # merged, not replaced
    assert cfg.agent_persona == "삼성전자로지텍 AI 상담원"
    assert cfg.common_greeting == "안녕하세요"
    assert cfg.no_response_policy.max_retries == 2
    assert cfg.no_response_policy.final_action == "escalate"
    assert cfg.no_response_policy.retry_message.startswith("고객님, 말씀을 잘 알아듣지")
    # the excluded session tool is gone; the other keeps its declaration with one flag off
    assert [t.tool_id for t in cfg.session_tools] == ["get_outbound_targets"]
    assert cfg.session_tools[0].generate_lambda is True
    assert cfg.session_tools[0].generate_openapi is False
    # …which is exactly what the count gate and get_all_tools read
    assert [t.tool_id for t in sm.get_all_tools() if getattr(t, "role", "") == "session"] == ["get_outbound_targets"]


def test_update_refuses_when_nothing_is_saved(bound_session):
    sm = bound_session
    result = sm.update_session_flow_config(common_closing="감사합니다")
    assert result["success"] is False and "No session flow config" in result["message"]
