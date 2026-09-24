"""The ACXD asset tools must not collide with the strands ToolResult protocol.

strands passes a tool return carrying BOTH ``status`` and ``content`` straight
through as an already-formed ToolResult
(``strands/tools/decorator.py::_wrap_tool_result``), so the file text landed
where the toolResult's content LIST belongs. bedrock.py then iterated that
string character by character and killed the turn with
``TypeError: content_type=<{> | unsupported type`` (2026-09-22 dev incident).
The payload key is ``file_content`` for exactly that reason.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_asset_patcher import patch_acxd_asset, read_acxd_asset  # noqa: E402
from tools.session_context import current_session_id  # noqa: E402

SID = "test-acxd-asset-patcher"


@pytest.fixture(autouse=True)
def _asset_mount(tmp_path, monkeypatch):
    flow_dir = tmp_path / "sessions" / SID / "assets" / "acxd_flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "RefundFlow.json").write_text(
        json.dumps({"name": "RefundFlow", "startNodeId": "n1"}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    tok = current_session_id.set(SID)
    yield tmp_path
    current_session_id.reset(tok)


def test_read_acxd_asset_has_no_content_key():
    result = read_acxd_asset(asset_type="acxd_flow", file_name="RefundFlow.json")
    assert result["status"] == "ok"
    assert "content" not in result
    assert "RefundFlow" in result["file_content"]


def test_patch_miss_hands_back_file_content_not_content():
    result = patch_acxd_asset(asset_type="acxd_flow", file_name="RefundFlow.json",
                              old_str="문구가 없습니다", new_str="x")
    assert result["status"] == "error"
    assert "content" not in result
    assert "RefundFlow" in result["file_content"]
