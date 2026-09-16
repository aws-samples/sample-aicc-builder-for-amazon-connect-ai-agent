"""Bedrock message sanitizer — the two live failures from the 2026-09-10 runs."""
from context.bedrock_messages import fix_messages_for_bedrock


def test_trailing_assistant_text_is_dropped():
    msgs = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "partial, cut by restart"}]},
    ]
    fixed = fix_messages_for_bedrock(msgs)
    assert fixed[-1]["role"] == "user"
    assert len(fixed) == 1


def test_empty_trailing_user_gets_tool_call_correction():
    msgs = [
        {"role": "user", "content": [{"text": "generate"}]},
        {"role": "assistant", "content": [{"text": "<invoke name=\"generate_acxd_application\"></invoke>"}]},
        {"role": "user", "content": []},
    ]
    fixed = fix_messages_for_bedrock(msgs)
    assert fixed[-1]["role"] == "user"
    assert "tool-use interface" in fixed[-1]["content"][0]["text"]


def test_valid_tool_cycle_untouched():
    msgs = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}}]},
    ]
    assert fix_messages_for_bedrock(msgs) == msgs
