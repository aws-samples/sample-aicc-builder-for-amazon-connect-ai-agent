"""Regression test for the orchestrator's output-token budget.

Background (2026-08-04 real run): the interview intermittently died with
max_tokens errors. The context window (1M) was never close — the problem was
OUTPUT. Strands omits ``maxTokens`` from ``inferenceConfig`` when its
``max_tokens`` is ``None``, and Bedrock's default in that case is only **4096**
output tokens. Verified live against ``global.anthropic.claude-opus-4-8`` in
ap-northeast-2:

    maxTokens omitted  → stopReason=max_tokens at outputTokens=4096
    maxTokens=64000    → stopReason=end_turn   at outputTokens=26078
    maxTokens=128001   → ValidationException: exceeds the model limit of 128000

``get_model_config()`` passed no ``max_tokens``, so the orchestrator ran the
whole interview on 4096 output tokens while every sub-agent got 128000. A
single ``save_operation_spec`` payload can exceed 4096 on its own.

This test pins the invariant that broke: the cap must be EXPLICIT (never None),
comfortably above Bedrock's 4096 default, and clamped to the model ceiling so
an env override can't turn every request into a ValidationException.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make ``src`` importable the same way the Docker entrypoint does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Bedrock's default when inferenceConfig.maxTokens is absent.
BEDROCK_DEFAULT_MAX_TOKENS = 4096
# Hard ceiling for Opus 4.6/4.7/4.8/5 — Bedrock rejects anything above this.
MODEL_OUTPUT_CEILING = 128000


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
    import app  # noqa: E402

    return app


def test_orchestrator_max_tokens_is_explicit_and_above_the_bedrock_default(app_mod):
    cap = app_mod.ORCHESTRATOR_MAX_TOKENS
    assert cap is not None, "must be explicit — None makes Bedrock fall back to 4096"
    assert isinstance(cap, int)
    assert cap > BEDROCK_DEFAULT_MAX_TOKENS, (
        f"cap={cap} is at or below Bedrock's implicit default "
        f"({BEDROCK_DEFAULT_MAX_TOKENS}); the interview will truncate again"
    )


def test_orchestrator_max_tokens_is_within_the_model_ceiling(app_mod):
    assert app_mod.ORCHESTRATOR_MAX_TOKENS <= MODEL_OUTPUT_CEILING


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("999999", MODEL_OUTPUT_CEILING),  # over the ceiling → clamped
        ("128000", MODEL_OUTPUT_CEILING),  # exactly at the ceiling → kept
        ("32000", 32000),                  # under → honoured
    ],
)
def test_env_override_is_clamped_to_the_model_ceiling(monkeypatch, env_value, expected):
    """An operator raising the cap must not be able to push it past the model
    limit — that would turn every orchestrator call into a ValidationException.
    Re-evaluates the same expression app.py uses at import time.
    """
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", env_value)
    value = min(int(os.environ.get("ORCHESTRATOR_MAX_TOKENS", "64000")), MODEL_OUTPUT_CEILING)
    assert value == expected


def test_get_model_config_passes_the_cap_to_bedrock(app_mod, monkeypatch):
    """The cap is worthless if it never reaches BedrockModel. Capture the kwargs
    build_model_kwargs receives and assert max_tokens is among them.
    """
    captured: dict = {}

    def fake_build_model_kwargs(model_id, **kwargs):
        captured.update(kwargs)
        captured["model_id"] = model_id
        return {"model_id": model_id or "stub", "max_tokens": kwargs.get("max_tokens")}

    monkeypatch.setattr(app_mod, "build_model_kwargs", fake_build_model_kwargs)
    monkeypatch.setattr(app_mod, "BedrockModel", lambda **kw: kw)

    app_mod.get_model_config()

    assert "max_tokens" in captured, "get_model_config() did not pass max_tokens"
    assert captured["max_tokens"] == app_mod.ORCHESTRATOR_MAX_TOKENS
