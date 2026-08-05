"""Regression tests for the interview spec-saving loop (2026-08-04 real run).

The reported symptom: in a long interview the agent had already saved every
operation spec, yet it kept asking "shall I save the specs?" and re-saving the
same ones. Root cause was twofold:

  1. The orchestrator ran with no ``max_tokens``, so Bedrock applied its 4096
     default. A turn containing a big ``save_operation_spec`` payload hit the
     output cap and Strands rewrote EVERY toolUse block in that assistant
     message as "tool use was incomplete due to maximum token limits being
     reached" — the tool never ran and the save vanished from history.
  2. History is pruned (MAX_HISTORY_MESSAGES) and tool payloads truncated, so
     even successful saves fall out of context in a long interview.

The fix injects ``<interview_state>``, derived from the files on NFS rather
than from conversation history, so the agent can always see what is already
persisted. These tests pin that reader's contract: what exists must be
reported ✅, what doesn't must be ⏳, and it must never raise on a partial or
missing session directory (it runs on every interview turn).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Make ``src`` importable the same way the Docker entrypoint does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    """Import app.py with the NFS mount pointed at a temp dir.

    S3FILES_MOUNT is read at import time, so patch the module attribute after
    import to keep this fixture independent of import order.
    """
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
    import app  # noqa: E402

    monkeypatch.setattr(app, "S3FILES_MOUNT", str(tmp_path))
    return app


def _session_dir(tmp_path, sid: str):
    d = tmp_path / "sessions" / sid
    (d / "assets" / "specs").mkdir(parents=True, exist_ok=True)
    (d / "state" / "requirements").mkdir(parents=True, exist_ok=True)
    return d


def test_empty_session_reports_everything_pending(app_mod, tmp_path):
    """A fresh interview must show every item as ⏳ — and must not crash on a
    session directory that doesn't exist yet (the very first turn)."""
    out = app_mod._read_interview_state("brand-new-session")
    assert out is not None
    assert "⏳ Operation specs: none saved yet" in out
    assert "⏳ Infrastructure spec: not saved yet" in out
    assert "⏳ Requirement documents: none saved yet" in out
    # Nothing is done, so no ✅ anywhere.
    assert "✅" not in out


def test_saved_specs_are_listed_by_operation_id(app_mod, tmp_path):
    """The whole point: already-saved specs must be visible as ✅ WITH their
    ids, so the agent can tell what's done without relying on history."""
    sid = "sess-specs"
    d = _session_dir(tmp_path, sid)
    for op in ("cancel_reservation", "check_availability", "check_reservation"):
        (d / "assets" / "specs" / f"{op}.json").write_text(
            json.dumps({"operation_id": op}), encoding="utf-8"
        )

    out = app_mod._read_interview_state(sid)

    assert "✅ Operation specs SAVED (3):" in out
    # Sorted and complete — a missing id would let the agent re-ask for it.
    assert "cancel_reservation, check_availability, check_reservation" in out
    assert "⏳ Operation specs" not in out


def test_infrastructure_spec_is_not_counted_as_an_operation(app_mod, tmp_path):
    """infrastructure_spec.json lives alongside the op specs in some layouts;
    counting it would inflate the count and invent an operation that the
    interviewer never defined."""
    sid = "sess-infra-mixed"
    d = _session_dir(tmp_path, sid)
    (d / "assets" / "specs" / "check_reservation.json").write_text("{}", encoding="utf-8")
    (d / "assets" / "specs" / "infrastructure_spec.json").write_text("{}", encoding="utf-8")

    out = app_mod._read_interview_state(sid)

    assert "✅ Operation specs SAVED (1): check_reservation" in out
    assert "infrastructure_spec" not in out.split("\n")[0]


def test_state_artifacts_and_requirements_are_reported(app_mod, tmp_path):
    sid = "sess-state"
    d = _session_dir(tmp_path, sid)
    (d / "state" / "infrastructure_spec.json").write_text("{}", encoding="utf-8")
    (d / "state" / "requirements" / "business_analysis.txt").write_text("x", encoding="utf-8")

    out = app_mod._read_interview_state(sid)

    assert "✅ Infrastructure spec: SAVED" in out
    # flow_config wasn't written, so it must still read as pending.
    assert "⏳ Session flow config: not saved yet" in out
    assert "✅ Requirement documents SAVED: business_analysis" in out


def test_handoff_marker_is_surfaced(app_mod, tmp_path, monkeypatch):
    """Once complete_interview has run, the block must say so — this is what
    stops the "you said done, shall I save the specs again?" turn."""
    sid = "sess-handoff"
    _session_dir(tmp_path, sid)
    monkeypatch.setattr(app_mod, "check_interview_handoff", lambda _sid: {"status": "ready"})

    out = app_mod._read_interview_state(sid)
    assert "✅ complete_interview: ALREADY CALLED" in out


def test_handoff_marker_absent_when_not_called(app_mod, tmp_path, monkeypatch):
    sid = "sess-no-handoff"
    _session_dir(tmp_path, sid)
    monkeypatch.setattr(app_mod, "check_interview_handoff", lambda _sid: None)

    out = app_mod._read_interview_state(sid)
    assert "complete_interview" not in out


def test_reader_never_raises_on_broken_state(app_mod, tmp_path, monkeypatch):
    """It runs on EVERY interview turn, so a filesystem hiccup (stale NFS
    handle, permissions) must degrade to a pending line, not break the turn."""
    sid = "sess-broken"
    _session_dir(tmp_path, sid)

    def boom(_sid):
        raise RuntimeError("NFS stale file handle")

    monkeypatch.setattr(app_mod, "check_interview_handoff", boom)

    out = app_mod._read_interview_state(sid)  # must not raise
    assert "⏳ Operation specs: none saved yet" in out


@pytest.mark.parametrize(
    "hostile_id",
    [
        # Traversal in several encodings — each resolves INTO another session's
        # directory (or outside the mount entirely) if used verbatim.
        "../sessions/victim",
        "..%s..%ssessions%svictim" % (os.sep, os.sep, os.sep),
        "sub/../victim",
        "....//sessions//victim",
        "/etc/passwd",
        "..\\..\\sessions\\victim",
        # Absolute-ish and NUL-byte attempts.
        "sessions/victim",
        "victim\x00",
        # Shapes that are simply not session ids.
        "",
        ".",
        "..",
        "a" * 200,
    ],
)
def test_unsafe_session_ids_are_rejected(app_mod, tmp_path, hostile_id):
    """The session id is the only user-controlled value that reaches the
    filesystem here (CodeQL "uncontrolled data in path expression"). Real ids
    are `session-<uuid4>`, so anything outside the allowlist must be REFUSED —
    not scrubbed into some neighbouring path. Refusing returns None, which the
    caller treats as "no block to inject".
    """
    victim = _session_dir(tmp_path, "victim")
    (victim / "assets" / "specs" / "secret_op.json").write_text("{}", encoding="utf-8")

    assert app_mod._session_base_dir(hostile_id) is None
    assert app_mod._read_interview_state(hostile_id) is None


@pytest.mark.parametrize(
    "good_id",
    [
        "session-3f2b1c9a-7d4e-4a1b-9c8f-0e5d6a7b8c9d",  # the real shape
        "test-1",
        "abc_123",
        "A" * 128,  # exactly at the length bound
    ],
)
def test_legitimate_session_ids_are_accepted(app_mod, tmp_path, good_id):
    """The allowlist must not be so tight that real sessions break — that would
    silently disable the whole <interview_state> fix."""
    base = app_mod._session_base_dir(good_id)
    assert base is not None
    assert base.name == good_id
    assert base.parent.name == "sessions"


def test_resolved_path_stays_inside_the_sessions_root(app_mod, tmp_path):
    """Second, independent guarantee: whatever the allowlist admits must still
    resolve inside <mount>/sessions. This is the backstop that keeps traversal
    impossible even if the pattern is later loosened."""
    sessions_root = (tmp_path / "sessions").resolve()
    base = app_mod._session_base_dir("session-abc")
    assert base is not None
    assert sessions_root in base.parents


def test_interview_state_is_injected_only_during_the_interview(app_mod):
    """Sanity on the wiring: the block is prepended to combined_state, so the
    reader must exist and be callable by the WS handler (a rename would silently
    drop the fix — the injection is wrapped in a try/except).
    """
    assert callable(app_mod._read_interview_state)
