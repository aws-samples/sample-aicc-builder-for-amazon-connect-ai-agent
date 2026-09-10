"""Regression tests for the dev incident of 2026-09-10: a scale-in stopped the
task hosting a live interview turn, and a freshly started task served traffic
before its S3 Files mount was visible."""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for candidate in (_SRC, _ROOT):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def test_task_protection_is_reference_counted_and_uses_the_ecs_agent_endpoint(monkeypatch):
    from context import task_protection as tp

    sent: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return _Resp(b'{"protection": {"ProtectionEnabled": true}}')

    monkeypatch.setenv("ECS_AGENT_URI", "http://169.254.170.2/api/abc")
    monkeypatch.setattr(tp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tp, "_active_turns", 0)

    assert tp.turn_started() == 1
    assert tp.turn_started() == 2          # second concurrent turn renews, count 2
    assert tp.turn_finished() == 1         # still one running → no release yet
    assert tp.turn_finished() == 0         # last one out releases

    urls = {u for u, _ in sent}
    assert urls == {"http://169.254.170.2/api/abc/task-protection/v1/state"}
    bodies = [b for _, b in sent]
    assert bodies[0] == {"ProtectionEnabled": True, "ExpiresInMinutes": tp.PROTECTION_MINUTES}
    assert bodies[1]["ProtectionEnabled"] is True
    assert bodies[-1] == {"ProtectionEnabled": False}
    assert len(bodies) == 3                # enable, renew, disable — nothing on the 2→1 step


def test_task_protection_is_a_noop_outside_ecs(monkeypatch):
    from context import task_protection as tp

    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    monkeypatch.setattr(tp, "_active_turns", 0)
    calls = []
    monkeypatch.setattr(tp.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    assert tp.turn_started() == 1
    assert tp.turn_finished() == 0
    assert calls == []


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path / "mnt"))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app
    from fastapi.testclient import TestClient

    return app, TestClient(app.app), tmp_path / "mnt"


def test_ping_is_unhealthy_until_the_mount_is_visible_and_live_is_not(app_client):
    _app, client, mount = app_client

    assert client.get("/live").status_code == 200
    assert client.get("/ping").status_code == 503        # volume not attached yet

    mount.mkdir(parents=True)
    (mount / "sessions").mkdir()
    assert client.get("/ping").status_code == 200        # ALB may route traffic now
    assert client.get("/live").status_code == 200
