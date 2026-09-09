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
