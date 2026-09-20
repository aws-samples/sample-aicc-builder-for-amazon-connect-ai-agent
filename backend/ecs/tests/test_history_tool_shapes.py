"""Conversation history keeps every tool call in its real shape.

Live (2026-09-21, session ad8fdd03): inputs over 1000 chars were stored as
{"_truncated": "<json prefix>... [truncated]"}. Every turn rebuilds the agent
from the stored history, so the model saw all of its earlier plan saves in
that shape, called upsert_acxd_flow_plan with the arguments wrapped in
`_truncated` (pydantic: flow_id missing), and — unable to read what it had
saved — saved Flow 2 and Flow 3 again and again."""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.abspath(os.path.join(_HERE, "..", "src")), os.path.abspath(os.path.join(_HERE, ".."))):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app
    return app


PLAN_CALL = {
    "flow_id": "SearchOrder",
    "purpose": "주문번호를 모르는 고객에게 고객명과 전화번호를 받아 주문을 역조회한다",
    "role": "operation",
    "operation_id": "search_order_number_by_customer_info",
    "steps": [{"step": n, "description": "단계 설명 " * 30, "node_type": "user_choice", "slot": f"s{n}"}
              for n in range(1, 9)],
    "slots": [{"name": f"s{n}", "type": "text", "examples": ["예시 값 " * 10]} for n in range(1, 9)],
}


def test_large_inputs_keep_their_keys_and_never_become_a_placeholder(app_mod):
    shrunk = app_mod._truncate_tool_payload(PLAN_CALL, 1500)
    assert set(shrunk) == set(PLAN_CALL)                      # every argument name survives
    assert "_truncated" not in json.dumps(shrunk)
    assert shrunk["flow_id"] == "SearchOrder" and shrunk["role"] == "operation"
    assert isinstance(shrunk["steps"], list) and shrunk["steps"][0]["step"] == 1
    assert any(isinstance(x, str) and "more item" in x for x in shrunk["steps"])   # list cut, marked in place
    assert len(json.dumps(shrunk, ensure_ascii=False)) < len(json.dumps(PLAN_CALL, ensure_ascii=False))
    # small inputs are untouched
    assert app_mod._truncate_tool_payload({"flow_id": "X"}, 1500) == {"flow_id": "X"}


def test_stored_history_carries_real_call_shapes(app_mod):
    messages = [
        {"role": "assistant", "content": [
            {"toolUse": {"name": "upsert_acxd_flow_plan", "toolUseId": "t1", "input": PLAN_CALL}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "status": "success",
                            "content": [{"json": {"success": True, "flow_id": "SearchOrder", "step_count": 8}}]}}]},
    ]
    out = app_mod._extract_new_messages(messages, 0)
    tu = out[0]["content"][0]["toolUse"]
    assert tu["name"] == "upsert_acxd_flow_plan" and tu["input"]["flow_id"] == "SearchOrder"
    assert "_truncated" not in json.dumps(out)
    assert '"flow_id": "SearchOrder"' in out[1]["content"][0]["toolResult"]["content"][0]["text"]


def test_legacy_placeholders_are_repaired_on_load(app_mod):
    prefix = json.dumps(PLAN_CALL, ensure_ascii=False)[:1000] + "... [truncated]"
    history = [{"role": "assistant", "content": [
        {"toolUse": {"name": "upsert_acxd_flow_plan", "toolUseId": "t1", "input": {"_truncated": prefix}}}]}]
    repaired = app_mod._repair_history_tool_inputs(history)
    tu = repaired[0]["content"][0]["toolUse"]["input"]
    assert "_truncated" not in tu
    assert tu["flow_id"] == "SearchOrder" and tu["operation_id"] == "search_order_number_by_customer_info"
    # a placeholder whose prefix is still valid JSON comes back whole
    small = {"_truncated": json.dumps({"flow_id": "A", "purpose": "b"})}
    assert app_mod._repair_truncated_tool_input(small) == {"flow_id": "A", "purpose": "b"}
    # ordinary inputs pass through untouched
    assert app_mod._repair_truncated_tool_input({"flow_id": "A"}) == {"flow_id": "A"}
