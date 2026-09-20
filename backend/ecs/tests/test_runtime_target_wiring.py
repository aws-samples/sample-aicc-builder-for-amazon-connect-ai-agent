"""Runtime-target wiring tests for the Classic Full and ACXD pipelines."""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for candidate in (_SRC, _ROOT):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app

    return app


def test_get_tools_for_phase_keeps_classic_and_swaps_acxd_prompt_generator(app_mod, monkeypatch):
    def generate_acxd_application():
        pass

    def patch_acxd_asset():
        pass

    monkeypatch.setattr(app_mod, "_load_acxd_application_tool", lambda: generate_acxd_application)
    monkeypatch.setattr(app_mod, "_load_acxd_asset_patcher", lambda: patch_acxd_asset)

    classic_interview = app_mod.get_tools_for_phase("interview", runtime_target="classic")
    acxd_interview = app_mod.get_tools_for_phase("interview", runtime_target="acxd")
    classic_generation = app_mod.get_tools_for_phase("generation", runtime_target="classic")
    acxd_generation = app_mod.get_tools_for_phase("generation", runtime_target="acxd")

    assert app_mod.prompt_generator_agent in classic_generation
    assert app_mod.prompt_generator_agent not in acxd_generation
    assert generate_acxd_application in acxd_generation
    assert patch_acxd_asset in acxd_generation
    assert app_mod.ACXD_INTERVIEW_TOOLS[0] not in classic_interview
    assert set(app_mod.ACXD_INTERVIEW_TOOLS).issubset(acxd_interview)



def test_session_flow_config_editor_is_available_after_the_interview(app_mod, monkeypatch):
    """Live (2026-09-20): after the interview the orchestrator had only the
    read-only getter for the session flow config, so a session tool the customer
    had excluded from the PoC kept the count gate red and a corrupted Korean
    retry message could not be fixed. The merge editor must be there in every
    post-interview phase for both targets; the whole-config saver stays an
    interview tool."""
    monkeypatch.setattr(app_mod, "_load_acxd_application_tool", lambda: None)
    monkeypatch.setattr(app_mod, "_load_acxd_asset_patcher", lambda: None)
    for phase in ("generation", "review", "post_generation"):
        for target in ("classic", "acxd"):
            tools = app_mod.get_tools_for_phase(phase, runtime_target=target)
            assert app_mod.update_session_flow_config in tools, (phase, target)
            assert app_mod.save_session_flow_config not in tools, (phase, target)
    assert app_mod.update_session_flow_config in app_mod.get_tools_for_phase("interview", runtime_target="acxd")

def test_complete_interview_blocks_unready_acxd_flow_spec(monkeypatch):
    from tools import acxd_flow_spec
    from tools import interview_completion

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(acxd_flow_spec, "is_acxd_target", lambda session_id: True)
    monkeypatch.setattr(
        acxd_flow_spec,
        "acxd_flow_spec_ready",
        lambda: (False, ["flow 'RefundFlow' step 2: determinism decision not confirmed by the user"]),
    )
    monkeypatch.setattr(
        interview_completion,
        "write_interview_handoff",
        lambda session_id, summary: writes.append((session_id, summary)) or True,
    )

    blocked = interview_completion.complete_interview("session-acxd", "refund assistant")

    assert blocked["success"] is False
    assert blocked["problems"] == ["flow 'RefundFlow' step 2: determinism decision not confirmed by the user"]
    assert writes == []


def test_complete_interview_allows_ready_acxd_flow_spec(monkeypatch):
    from tools import acxd_flow_spec
    from tools import interview_completion

    monkeypatch.setattr(acxd_flow_spec, "is_acxd_target", lambda session_id: True)
    monkeypatch.setattr(acxd_flow_spec, "acxd_flow_spec_ready", lambda: (True, []))
    monkeypatch.setattr(interview_completion, "write_interview_handoff", lambda session_id, summary: True)

    result = interview_completion.complete_interview("session-ready", "ready")

    assert result["success"] is True


def test_acxd_prompt_blocks_are_present_only_for_acxd_target():
    from prompts.interview_agent_prompt import get_interview_agent_prompt
    from prompts.system_prompt import get_phase_system_prompt

    classic_interview = get_interview_agent_prompt("classic")[0]["text"]
    acxd_interview = get_interview_agent_prompt("acxd")[0]["text"]
    classic_generation = get_phase_system_prompt("generation", runtime_target="classic")[0]["text"]
    acxd_generation = get_phase_system_prompt("generation", runtime_target="acxd")[0]["text"]
    acxd_full_generation = get_phase_system_prompt(
        "generation",
        scope=["cdk", "lambda", "openapi", "acxd_application", "contact_flow", "knowledge_base"],
        runtime_target="acxd",
    )[0]["text"]

    assert "ACXD RUNTIME TARGET: FLOW DESIGN INSERTION" not in classic_interview
    assert "ACXD RUNTIME TARGET: FLOW DESIGN INSERTION" in acxd_interview
    assert "confirm_acxd_flow_steps" in acxd_interview
    assert "ACXD RUNTIME TARGET — THIS OVERRIDES CONFLICTING CLASSIC PHASE WORDING" not in classic_generation
    assert "ACXD RUNTIME TARGET — THIS OVERRIDES CONFLICTING CLASSIC PHASE WORDING" in acxd_generation
    assert "generate_acxd_application" in acxd_generation
    assert "SCOPED GENERATION MODE" not in acxd_full_generation


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload):  # pragma: no cover - safe_send_json may use either
        import json as _json
        self.sent.append(_json.loads(payload))

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.anyio
async def test_set_runtime_target_action_switches_an_empty_session(app_mod):
    """Live report (dev, 2026-09-10): the session is auto-created with the
    default target when opened, so picking ACXD on the start screen afterwards
    had no effect — the radio change must re-seed the still-empty session."""
    sid = "session-empty-acxd"
    app_mod.session_store[sid] = {"conversation_history": [], "runtime_target": "classic", "_runtime_target_seeded": True}
    ws = _FakeWebSocket()

    await app_mod.handle_set_runtime_target_ws(ws, sid, {"runtime_target": "acxd"})

    assert ws.sent[-1]["type"] == "runtime_target_updated"
    assert ws.sent[-1]["accepted"] is True
    assert ws.sent[-1]["runtime_target"] == "acxd"
    assert app_mod.session_store[sid]["runtime_target"] == "acxd"
    assert app_mod.get_runtime_target(sid) == "acxd"


@pytest.mark.anyio
async def test_set_runtime_target_action_is_refused_once_the_conversation_started(app_mod):
    sid = "session-started-classic"
    app_mod.set_runtime_target(sid, "classic")
    app_mod.session_store[sid] = {
        "conversation_history": [{"role": "user", "content": [{"text": "hi"}]}],
        "runtime_target": "classic",
        "_runtime_target_seeded": True,
    }
    ws = _FakeWebSocket()

    await app_mod.handle_set_runtime_target_ws(ws, sid, {"runtime_target": "acxd"})

    assert ws.sent[-1]["accepted"] is False
    assert ws.sent[-1]["runtime_target"] == "classic"
    assert app_mod.get_runtime_target(sid) == "classic"
