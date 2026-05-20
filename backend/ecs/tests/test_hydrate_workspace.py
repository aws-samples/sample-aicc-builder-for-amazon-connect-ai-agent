"""Tests for hydrate_session_workspace copy-on-miss behavior.

S3 Files (managed NFS over S3) only auto-imports files ≤10MB on directory
first access, and evicts everything after 30 days. After an ECS restart, an
old session opens with `assets/` empty even though the durable PutObject
copies still exist in S3. hydrate_session_workspace bridges the gap by
copying the durable S3 objects back into the NFS view.

These tests stub the S3 client so they run with no AWS dependency.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture
def nfs_mount(tmp_path, monkeypatch):
    """Point the module at a temp NFS mount and reset its cached client."""
    mount = tmp_path / "s3files"
    mount.mkdir()
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ASSETS_BUCKET_NAME", "test-bucket")

    # Reload the module so it picks up the patched env vars.
    if "tools.s3_asset_storage" in sys.modules:
        del sys.modules["tools.s3_asset_storage"]
    import tools.s3_asset_storage as mod  # noqa: E402

    return mount, mod


def _fake_s3_client(objects: dict[str, bytes]) -> MagicMock:
    """Build a MagicMock S3 client that serves *objects* via get/list."""
    client = MagicMock()

    def get_object(Bucket, Key):
        if Key not in objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(objects[Key])}

    client.get_object.side_effect = get_object

    def paginate(Bucket, Prefix):
        contents = [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]
        return [{"Contents": contents}] if contents else [{}]

    paginator = MagicMock()
    paginator.paginate.side_effect = paginate
    client.get_paginator.return_value = paginator

    def list_objects_v2(Bucket, Prefix, ContinuationToken=None):
        contents = [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}

    client.list_objects_v2.side_effect = list_objects_v2
    return client


def test_missing_asset_is_restored_from_s3(nfs_mount):
    mount, mod = nfs_mount
    sid = "session-cold-abc"
    objects = {
        f"assets/{sid}/lambda/create_reservation/handler.py": b"def handler(): pass\n",
        f"assets/{sid}/openapi/openapi.yaml": b"openapi: 3.0.0\n",
        f"assets/{sid}/state/project.json": b'{"name": "demo"}\n',
        f"assets/{sid}/specs/op_create.json": b'{"op_id": "op_create"}\n',
    }
    client = _fake_s3_client(objects)

    with patch.object(mod, "get_s3_client", return_value=client):
        result = mod.hydrate_session_workspace(sid)

    # Every key listed; every file restored (none were on NFS); none skipped.
    assert result["listed"] == 4
    assert result["restored"] == 4
    assert result["files"] == 0
    assert result["skipped"] == 0

    base = mount / "sessions" / sid
    assert (base / "assets" / "lambda" / "create_reservation" / "handler.py").read_bytes() == \
        b"def handler(): pass\n"
    assert (base / "assets" / "openapi" / "openapi.yaml").read_bytes() == b"openapi: 3.0.0\n"
    assert (base / "state" / "project.json").read_bytes() == b'{"name": "demo"}\n'
    assert (base / "assets" / "specs" / "op_create.json").read_bytes() == b'{"op_id": "op_create"}\n'


def test_present_files_are_left_untouched(nfs_mount):
    mount, mod = nfs_mount
    sid = "session-warm-xyz"
    nfs_path = mount / "sessions" / sid / "assets" / "lambda" / "op" / "handler.py"
    nfs_path.parent.mkdir(parents=True)
    nfs_path.write_bytes(b"local-newer-content\n")

    objects = {
        f"assets/{sid}/lambda/op/handler.py": b"s3-stale-content\n",
    }
    client = _fake_s3_client(objects)

    with patch.object(mod, "get_s3_client", return_value=client):
        result = mod.hydrate_session_workspace(sid)

    assert result["files"] == 1  # already present
    assert result["restored"] == 0  # not overwritten
    assert nfs_path.read_bytes() == b"local-newer-content\n"


def test_missing_s3_object_is_skipped(nfs_mount):
    mount, mod = nfs_mount
    sid = "session-empty"
    # list_session_assets returns this key, but get_object will 404.
    listed = {f"assets/{sid}/lambda/op/handler.py": b""}
    client = _fake_s3_client(listed)
    # Simulate the listing reporting a key that no longer exists at GET time.
    from botocore.exceptions import ClientError
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )

    with patch.object(mod, "get_s3_client", return_value=client):
        result = mod.hydrate_session_workspace(sid)

    assert result["restored"] == 0
    assert result["skipped"] == 1
    assert not (mount / "sessions" / sid / "assets" / "lambda" / "op" / "handler.py").exists()


def test_no_nfs_mount_returns_zeros(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", "")
    if "tools.s3_asset_storage" in sys.modules:
        del sys.modules["tools.s3_asset_storage"]
    import tools.s3_asset_storage as mod  # noqa: E402

    result = mod.hydrate_session_workspace("anything")
    assert result == {"listed": 0, "files": 0, "restored": 0, "skipped": 0}
