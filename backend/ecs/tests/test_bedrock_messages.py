"""Bedrock message sanitizer — the two live failures from the 2026-09-10 runs."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from context.bedrock_messages import fix_messages_for_bedrock, normalize_content_blocks  # noqa: E402


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


# --- content-block coercion — the 2026-09-22 EventLoopException -------------
# A tool returning {"status": ..., "content": "<file text>"} is passed through
# by strands as an already-formed ToolResult, so the string landed where the
# content list belongs and bedrock.py raised
# "content_type=<{> | unsupported type" mid-turn.

def test_str_block_in_assistant_content_is_wrapped():
    msgs = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": ["bare string block"]},
        {"role": "user", "content": [{"text": "next"}]},
    ]
    fixed = fix_messages_for_bedrock(msgs)
    assert fixed[1]["content"] == [{"text": "bare string block"}]


def test_str_block_in_tool_result_content_is_wrapped():
    msgs = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": ['{"name": "flow"}']}}]},
    ]
    fixed = fix_messages_for_bedrock(msgs)
    assert fixed[-1]["content"][0]["toolResult"]["content"] == [{"text": '{"name": "flow"}'}]


def test_tool_result_content_that_is_a_bare_string_becomes_one_text_block():
    # The live shape: toolResult["content"] was the whole file, not a list —
    # strands iterated it character by character.
    msgs = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": '{"a": 1}'}}]},
    ]
    fixed = fix_messages_for_bedrock(msgs)
    tool_result = fixed[-1]["content"][0]["toolResult"]
    assert tool_result["content"] == [{"text": '{"a": 1}'}]
    assert tool_result["toolUseId"] == "t1"


def test_normalize_leaves_well_formed_messages_untouched():
    msgs = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"json": {"ok": True}}]}}]},
    ]
    assert normalize_content_blocks(msgs) == msgs
