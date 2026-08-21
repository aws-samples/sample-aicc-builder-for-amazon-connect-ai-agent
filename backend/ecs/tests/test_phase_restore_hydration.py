"""Regression tests for generation_progress.json phase-restore reliability.

Live session session-e594e230: after leaving and returning to a session that
was mid-regeneration (post_generation phase), the AI's own context correctly
understood it was regenerating, but the phase-stepper UI stayed stuck on
"Interview" — step 1 of 4.

Root cause: generation_progress.json (the file detect_phase() reads) lived on
NFS only, with no durable S3 backup, and hydrate_session_workspace() (which
repopulates a lazily-imported NFS view from S3 on injectHistory) never
covered its context/ prefix. A fresh ECS task, or a reconnect the ALB routes
to a different task, sees "no file" on NFS and falls back to phase, and the
WebSocket 'connected' handler calls detect_phase() BEFORE injectHistory / any
hydration ever runs — so even a later correct injectHistory response could
lose the race against a UI that already latched onto the wrong phase.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def gp(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.delenv("ASSETS_BUCKET_NAME", raising=False)
    import context.generation_progress as module
    importlib.reload(module)
    return module


class FakeS3:
    """Minimal in-memory S3 stand-in for put_object/get_object."""
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("NoSuchKey")
        body = self.objects[(Bucket, Key)]
        class _Body:
            def read(self_inner):
                return body
        return {"Body": _Body()}


def test_write_state_backs_up_to_s3(gp, tmp_path, monkeypatch):
    fake_s3 = FakeS3()
    monkeypatch.setenv("ASSETS_BUCKET_NAME", "test-bucket")
    with mock.patch("tools.s3_asset_storage.get_s3_client", return_value=fake_s3):
        gp.record_tool_completion("sess-1", "contact_flow_generator_agent", tool_use_id="t1")

    key = ("test-bucket", "assets/sess-1/context/generation_progress.json")
    assert key in fake_s3.objects
    saved = json.loads(fake_s3.objects[key])
    assert "contact_flow" in saved["assets"]


def test_read_state_falls_back_to_s3_when_nfs_cold(gp, tmp_path, monkeypatch):
    """Simulates a lazily-imported NFS view: the file was never written to
    NFS in THIS process, but a durable S3 copy exists (written by a
    different ECS task earlier). detect_phase() must not see "no progress"."""
    fake_s3 = FakeS3()
    monkeypatch.setenv("ASSETS_BUCKET_NAME", "test-bucket")

    # Seed S3 with progress that indicates "review completed" (post_generation)
    # WITHOUT ever writing to the local NFS path.
    state = {
        "assets": {
            "lambda": {"status": "completed", "tool": "lambda_generator_agent", "updated_at": "x"},
            "openapi": {"status": "completed", "tool": "openapi_generator_agent", "updated_at": "x"},
            "prompt": {"status": "completed", "tool": "prompt_generator_agent", "updated_at": "x"},
            "contact_flow": {"status": "completed", "tool": "contact_flow_generator_agent", "updated_at": "x"},
            "cdk": {"status": "completed", "tool": "infrastructure_generator_agent", "updated_at": "x"},
            "review": {"status": "reviewed", "tool": "reviewer_agent", "updated_at": "x"},
        },
        "events": [],
    }
    fake_s3.objects[("test-bucket", "assets/sess-cold/context/generation_progress.json")] = (
        json.dumps(state).encode("utf-8")
    )

    with mock.patch("tools.s3_asset_storage.get_s3_client", return_value=fake_s3):
        phase = gp.detect_phase("sess-cold")

    assert phase == "post_generation", (
        f"expected post_generation (review completed) but got {phase!r} — "
        "this is the exact live bug: phase falls back to 'interview'/'generation' "
        "when NFS is cold even though S3 has the real progress"
    )


def test_read_state_warms_nfs_after_s3_fallback(gp, tmp_path, monkeypatch):
    """After falling back to S3, the NFS view should be warmed so subsequent
    reads in the SAME process don't need S3 again."""
    fake_s3 = FakeS3()
    monkeypatch.setenv("ASSETS_BUCKET_NAME", "test-bucket")
    state = {"assets": {"lambda": {"status": "completed"}}, "events": []}
    fake_s3.objects[("test-bucket", "assets/sess-warm/context/generation_progress.json")] = (
        json.dumps(state).encode("utf-8")
    )

    with mock.patch("tools.s3_asset_storage.get_s3_client", return_value=fake_s3):
        gp._read_state("sess-warm")

    nfs_path = gp._progress_path("sess-warm")
    assert nfs_path.exists(), "NFS view should be warmed from the S3 fallback"


def test_read_state_no_s3_backup_yet_defaults_empty(gp, tmp_path, monkeypatch):
    """A genuinely new session (nothing on NFS or S3) must still default to
    empty state, not raise."""
    fake_s3 = FakeS3()
    monkeypatch.setenv("ASSETS_BUCKET_NAME", "test-bucket")
    with mock.patch("tools.s3_asset_storage.get_s3_client", return_value=fake_s3):
        state = gp._read_state("sess-brand-new")
    assert state == {"assets": {}, "events": []}


def test_hydrate_session_workspace_covers_context_prefix(monkeypatch, tmp_path):
    """hydrate_session_workspace must restore context/generation_progress.json
    from S3 into the NFS view, not just assets/specs/state/."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import importlib
    import tools.s3_asset_storage as sas
    importlib.reload(sas)

    def fake_list_prefix(prefix):
        if prefix == "assets/sess-hydrate/context/":
            return ["assets/sess-hydrate/context/generation_progress.json"]
        return []

    copied = []

    def fake_copy(s3_key, nfs_path):
        copied.append((s3_key, str(nfs_path)))
        nfs_path.parent.mkdir(parents=True, exist_ok=True)
        nfs_path.write_text("{}")
        return True

    with mock.patch.object(sas, "_nfs_available", return_value=True), \
         mock.patch.object(sas, "list_session_assets", return_value=[]), \
         mock.patch.object(sas, "_list_s3_prefix", side_effect=fake_list_prefix), \
         mock.patch.object(sas, "_copy_s3_key_to_nfs", side_effect=fake_copy):
        result = sas.hydrate_session_workspace("sess-hydrate")

    assert result["restored"] == 1
    assert any("generation_progress.json" in c[0] for c in copied)
