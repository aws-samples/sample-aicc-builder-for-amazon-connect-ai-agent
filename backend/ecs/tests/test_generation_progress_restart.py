"""A task that starts after a redeploy must not drop a mid-generation session
back to the interview phase (live: the model then narrated tool calls it did
not have)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from context import generation_progress as gp  # noqa: E402

SID = "session-restart-test"


def test_phase_inferred_from_stored_assets_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(gp, "_infer_assets_from_storage",
                        lambda sid: {"lambda": {"status": "completed"}, "cdk": {"status": "completed"}})
    monkeypatch.setattr("tools.interview_completion.check_interview_handoff", lambda sid: False)
    assert gp.detect_phase(SID) == "generation"


def test_phase_stays_interview_without_any_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(gp, "_infer_assets_from_storage", lambda sid: {})
    monkeypatch.setattr("tools.interview_completion.check_interview_handoff", lambda sid: False)
    assert gp.detect_phase(SID) == "interview"


def test_infer_assets_maps_asset_types():
    keys = [f"assets/{SID}/lambda/op/index.py", f"assets/{SID}/acxd_flow/A.json",
            f"assets/{SID}/operation_spec/summary.md"]
    import tools.s3_asset_storage as s3s
    orig = s3s.list_session_assets
    s3s.list_session_assets = lambda sid, **kw: keys
    try:
        inferred = gp._infer_assets_from_storage(SID)
    finally:
        s3s.list_session_assets = orig
    assert set(inferred) == {"lambda", "acxd_application"}
