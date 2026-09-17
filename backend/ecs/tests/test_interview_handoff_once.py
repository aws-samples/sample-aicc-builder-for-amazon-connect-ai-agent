"""The interview → generation context boundary must run exactly once per session.

Seen on dev (session in review, 2026-09-11): every redeploy dropped the
in-memory `_handoff_processed` flag, so the next turn re-ran the boundary —
cleared the conversation, injected the 'Begin generation' bootstrap and forced
the phase from review back to generation.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import context.generation_progress as gp  # noqa: E402


def _use_tmp_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(gp, "_progress_path", lambda sid: tmp_path / sid / "context" / "generation_progress.json")
    monkeypatch.setattr(gp, "_infer_assets_from_storage", lambda _sid: {})


def test_boundary_runs_once_and_the_marker_survives_a_restart(monkeypatch, tmp_path):
    _use_tmp_mount(monkeypatch, tmp_path)
    sid = "session-a"
    # Fresh handoff: phase detected as generation, nothing generated yet.
    assert gp.interview_handoff_pending(sid, "generation") is True
    gp.mark_handoff_processed(sid)
    # Same turn or any later process (the marker lives on NFS, not in memory).
    assert gp.interview_handoff_pending(sid, "generation") is False
    assert gp._read_state(sid)["handoff_processed"] is True


def test_boundary_never_runs_outside_the_generation_phase(monkeypatch, tmp_path):
    _use_tmp_mount(monkeypatch, tmp_path)
    sid = "session-b"  # no marker at all
    for phase in ("interview", "review", "post_generation"):
        assert gp.interview_handoff_pending(sid, phase) is False


def test_legacy_session_with_generated_assets_counts_as_handed_off(monkeypatch, tmp_path):
    """Sessions created before the marker existed: recorded progress proves the
    boundary was passed, so a redeploy must not re-run it."""
    _use_tmp_mount(monkeypatch, tmp_path)
    sid = "session-c"
    gp._write_state(sid, {"phase": "generation", "assets": {"lambda": {"status": "completed"}}})
    assert gp.is_handoff_processed(sid) is True
    assert gp.interview_handoff_pending(sid, "generation") is False


def test_unreadable_progress_state_falls_back_to_durable_traces(monkeypatch, tmp_path):
    """NFS lag after a task swap can leave the progress file empty for a moment;
    the archived interview history (written by the boundary itself) still proves it ran."""
    _use_tmp_mount(monkeypatch, tmp_path)
    monkeypatch.setattr(gp, "_infer_assets_from_storage", lambda _sid: {})
    sid = "session-d"
    assert gp.is_handoff_processed(sid) is False
    archive = gp._progress_path(sid).parent / "interview_history.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("[]", encoding="utf-8")
    assert gp.is_handoff_processed(sid) is True
    # …and so do assets already stored in S3 when even the archive is missing.
    archive.unlink()
    monkeypatch.setattr(gp, "_infer_assets_from_storage", lambda _sid: {"lambda": {"status": "completed"}})
    assert gp.is_handoff_processed(sid) is True
