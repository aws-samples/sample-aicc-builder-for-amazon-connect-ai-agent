"""GET /api/assets/{sid}/download — session binding and readable refusals.

Live (2026-09-11): the endpoint ran the D9 gate without the session ContextVar,
so every flow slot was 'unknown OperationSpec field' and the refusal went out
as a 404 — which CloudFront rewrites to index.html, leaving the browser with
"Unexpected token '<'" and the user with no reason at all.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path / "mnt"))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app
    from fastapi.testclient import TestClient

    app.app.dependency_overrides[app.verify_token] = lambda: {"sub": "test"}
    yield app, TestClient(app.app)
    app.app.dependency_overrides.pop(app.verify_token, None)


def test_refusal_is_a_409_json_with_the_problem_list_and_the_session_bound(client, monkeypatch):
    app, http = client
    import tools.asset_packager as packager
    from tools.session_context import current_session_id

    seen = {}

    def fake_package(session_id, asset_type_filter=None, include_readme=True, **_kw):
        seen["bound_sid"] = current_session_id.get()
        return {"success": False, "error": "ACXD packaging refused:\n- D9-3: x", "problems": ["D9: D9-3: x"]}

    monkeypatch.setattr(packager, "package_assets_impl", fake_package)
    monkeypatch.setattr("tools.project_workspace.set_workspace_session_id", lambda sid: None)
    monkeypatch.setattr("tools.spec_manager.restore_specs_from_workspace", lambda: None)

    res = http.get("/api/assets/session-x/download")
    assert res.status_code == 409
    body = res.json()
    assert body["success"] is False
    assert body["problems"] == ["D9: D9-3: x"]
    assert "refused" in body["error"]
    assert seen["bound_sid"] == "session-x"
    # the binding is scoped to the request
    assert current_session_id.get() != "session-x"


def test_success_returns_the_presigned_url(client, monkeypatch):
    app, http = client
    import tools.asset_packager as packager

    monkeypatch.setattr(packager, "package_assets_impl", lambda **_kw: {
        "success": True, "download_url": "https://example.test/a.zip", "expires_at": "later",
        "s3_key": "packages/a.zip", "file_count": 3})
    monkeypatch.setattr("tools.project_workspace.set_workspace_session_id", lambda sid: None)
    monkeypatch.setattr("tools.spec_manager.restore_specs_from_workspace", lambda: None)

    res = http.get("/api/assets/session-y/download?asset_type=acxd")
    assert res.status_code == 200
    assert res.json()["downloadUrl"] == "https://example.test/a.zip"
