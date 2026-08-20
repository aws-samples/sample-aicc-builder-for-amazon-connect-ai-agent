"""Regression tests for generation_progress toolUseId dedup.

The same completion used to be recorded TWICE: once inline per toolResult
(record_tool_completion, streaming loop) and again by the end-of-turn message
scan (update_from_new_messages). That doubled the events list (observed live:
17 identical entries in one second) and could downgrade a 'fixed' asset back
to 'completed' by re-applying the review-aware transition.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def gp(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import context.generation_progress as module
    importlib.reload(module)
    return module


def _state(tmp_path, session_id):
    path = Path(tmp_path) / "sessions" / session_id / "context" / "generation_progress.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _turn_messages(tool_name, tool_use_id):
    """Strands-format assistant toolUse + user toolResult pair."""
    return [
        {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": tool_use_id, "name": tool_name, "input": {}}},
        ]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": tool_use_id, "status": "success", "content": []}},
        ]},
    ]


def test_inline_then_scan_records_once(gp, tmp_path):
    sid = "sess-dedup-1"
    # Inline path records the completion first (streaming loop).
    assert gp.record_tool_completion(sid, "lambda_generator_agent", "completed", tool_use_id="tu-1") is True
    # End-of-turn fallback scan sees the same toolResult — must be a no-op.
    gp.update_from_new_messages(sid, _turn_messages("lambda_generator_agent", "tu-1"))

    state = _state(tmp_path, sid)
    assert len(state["events"]) == 1
    assert state["events"][0]["toolUseId"] == "tu-1"
    assert state["assets"]["lambda"]["status"] == "completed"


def test_inline_duplicate_is_skipped(gp, tmp_path):
    sid = "sess-dedup-2"
    assert gp.record_tool_completion(sid, "prompt_generator_agent", "completed", tool_use_id="tu-2") is True
    # Replay of the same toolResult (e.g. reconnect catch-up) is ignored.
    assert gp.record_tool_completion(sid, "prompt_generator_agent", "completed", tool_use_id="tu-2") is False
    assert len(_state(tmp_path, sid)["events"]) == 1


def test_scan_does_not_downgrade_fixed_status(gp, tmp_path):
    sid = "sess-dedup-3"
    # Generate → review → fix-regeneration, all inline.
    gp.record_tool_completion(sid, "contact_flow_generator_agent", "completed", tool_use_id="tu-gen")
    gp.record_tool_completion(sid, "reviewer_agent", "completed", tool_use_id="tu-rev")
    # detect_phase requires review state; simulate the post-review fix:
    state = _state(tmp_path, sid)
    state["assets"]["contact_flow"]["status"] = "reviewed"
    gp._write_state(sid, state)
    gp.record_tool_completion(sid, "contact_flow_generator_agent", "completed", tool_use_id="tu-fix")
    assert _state(tmp_path, sid)["assets"]["contact_flow"]["status"] == "fixed"

    # End-of-turn scan replays the SAME fix toolResult. Before the dedup this
    # re-applied the transition with prev_status='fixed' → downgraded to
    # 'completed'. Now it must remain 'fixed'.
    gp.update_from_new_messages(sid, _turn_messages("contact_flow_generator_agent", "tu-fix"))
    assert _state(tmp_path, sid)["assets"]["contact_flow"]["status"] == "fixed"


def test_scan_still_records_missed_completions(gp, tmp_path):
    sid = "sess-dedup-4"
    # Nothing recorded inline (e.g. event was missed) — the fallback scan must
    # still record it, preserving its safety-net role.
    gp.update_from_new_messages(sid, _turn_messages("openapi_generator_agent", "tu-4"))
    state = _state(tmp_path, sid)
    assert state["assets"]["openapi"]["status"] == "completed"
    assert len(state["events"]) == 1
    # And a later duplicate scan of the same turn is a no-op.
    gp.update_from_new_messages(sid, _turn_messages("openapi_generator_agent", "tu-4"))
    assert len(_state(tmp_path, sid)["events"]) == 1


def test_recorded_ids_capped(gp, tmp_path):
    sid = "sess-dedup-5"
    for i in range(gp._MAX_RECORDED_TOOL_USE_IDS + 25):
        gp.record_tool_completion(sid, "lambda_generator_agent", "completed", tool_use_id=f"tu-{i}")
    ids = _state(tmp_path, sid)["recorded_tool_use_ids"]
    assert len(ids) <= gp._MAX_RECORDED_TOOL_USE_IDS
    # Most recent ids retained
    assert f"tu-{gp._MAX_RECORDED_TOOL_USE_IDS + 24}" in ids
