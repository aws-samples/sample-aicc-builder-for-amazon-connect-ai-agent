"""Replaying a session's assets never puts a package's bytes in the chat.

dev, 2026-09-25: the knowledge-base ZIP was replayed as an asset preview and the
chat showed "PK\\u0003\\u0004…" in a "regenerated" card.
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

    return app


def test_rehydrate_skips_zip_and_binary_assets_but_keeps_text(app_mod, monkeypatch):
    import tools.s3_asset_storage as storage

    sid = "session-rehydrate"
    folder = next(iter(app_mod._REHYDRATE_FOLDER_TO_TYPE))            # any renderable folder
    keys = {
        f"assets/{sid}/{folder}/kb/selc_knowledge_base.zip": "PK\x03\x04\x14\x00binary",
        f"assets/{sid}/{folder}/kb/notes.txt": "sneaky \x00 bytes",
        f"assets/{sid}/{folder}/kb/FaqFlow.json": '{"flowId": "FaqFlow"}',
    }
    monkeypatch.setattr(storage, "list_session_assets", lambda _sid: list(keys))
    monkeypatch.setattr(storage, "get_asset_from_s3", lambda key, **_k: keys[key])
    monkeypatch.setattr(storage, "get_asset_mtime_ms", lambda key: None)
    sent: list[dict] = []

    async def fake_send(_ws, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(app_mod, "safe_send_json", fake_send)
    asyncio.run(app_mod._rehydrate_assets_for_display(object(), sid))
    names = [p["assetPreview"]["fileName"] for p in sent if p.get("type") == "asset_preview"]
    assert names == ["FaqFlow.json"]
