"""A late injectHistory must not replace the conversation of a running turn.

dev, 2026-09-26: the browser's session restore was held back 33 s by asset
lazy-loading, so its injectHistory arrived 18 s into a turn the user had already
started, and the history under the running agent was replaced with the
browser's (older) copy. With no turn running, the richer injected copy still wins.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.abspath(os.path.join(_HERE, "..", "src")), os.path.abspath(os.path.join(_HERE, "..")), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app
    import tools.s3_asset_storage as storage

    monkeypatch.setattr(storage, "hydrate_session_workspace", lambda _sid: None)
    sent: list[dict] = []

    async def fake_send(_ws, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(app, "safe_send_json", fake_send)
    monkeypatch.setattr(app._context_store, "save_conversation_history", lambda *_a, **_k: None)
    app._test_sent = sent
    return app


_INJECTED = [
    {"role": "user", "content": "배송 조회해 주세요"},
    {"role": "assistant", "content": "주문번호를 알려 주세요."},
    {"role": "user", "content": "1234567890"},
]


def _run_inject(app_mod, sid: str) -> list:
    async def go():
        app_mod._background_tasks_lock = asyncio.Lock()
        await app_mod.handle_inject_history_ws(object(), sid, {"history": _INJECTED, "originalSessionId": sid})
    asyncio.run(go())
    return app_mod.get_or_create_session(sid)["conversation_history"]


def test_inject_history_keeps_the_live_history_while_a_turn_runs(app_mod):
    sid = "session-inject-running"
    live = [{"role": "user", "content": [{"text": "새 요청"}]}]
    app_mod.get_or_create_session(sid)["conversation_history"] = list(live)

    class _Running:
        def done(self):
            return False

    app_mod._background_tasks[sid] = {"task": _Running(), "ws_holder": {"ws": None}}
    try:
        assert _run_inject(app_mod, sid) == live
    finally:
        app_mod._background_tasks.pop(sid, None)


def test_inject_history_replaces_a_shorter_history_when_no_turn_runs(app_mod):
    sid = "session-inject-idle"
    app_mod.get_or_create_session(sid)["conversation_history"] = []
    history = _run_inject(app_mod, sid)
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[-1]["content"] == [{"text": "1234567890"}]
    # the frontend logs "History injection failed" unless the reply says success
    reply = [p for p in app_mod._test_sent if p.get("type") == "history_injected"][-1]
    assert reply["success"] is True and reply["injectedCount"] == len(_INJECTED)
