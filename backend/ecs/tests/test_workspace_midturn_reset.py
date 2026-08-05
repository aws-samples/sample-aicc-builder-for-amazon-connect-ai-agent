"""Regression test: a mid-turn session reset must not lose the workspace.

Found on prod 2026-08-05 (long-input e2e). The agent's own summary said

    분석 문서 저장 시 세션 관련 이슈가 있었지만, 모든 스펙은 이미 디스크에 정상 저장되었습니다

and the persisted history holds the real tool result::

    {"success": false, "error": "Workspace not initialised (no session)"}

Sequence observed in the logs, all inside one second:

  1. the kickoff message starts the background agent task,
  2. the WebSocket flaps (close 1005) 0.5s later,
  3. the frontend reconnects and — finding no history yet — sends
     ``createNewSession`` (useWebSocket.ts does this on reconnect-with-no-history),
  4. ``handle_create_new_session_ws`` runs ``clear_all_specs()`` +
     ``cleanup_session()``, dropping the ProjectWorkspace out of
     ``session_context._workspaces`` while the agent is mid-stream.

The agent kept running for another 4 minutes on a session whose workspace was
gone. Nothing looked broken, because ``save_operation_spec`` writes NFS on its
own fast-path and only the S3 backup goes through the workspace — so every spec
still landed on disk. The first call that depends *solely* on the workspace,
``save_requirement_document``, failed. Silently: the failure path returns a dict
and logs nothing at all, which is why CloudWatch had zero matching lines. The S3
copies of specs/ and state/ were also skipped for that whole session (verified:
the bucket prefix has only operation_spec/*.md, while a healthy session has
specs/*.json + state/*.json + requirement/analysis.txt).

Two layers are fixed and pinned here:

  1. app.py refuses to reset a session that has a live background agent task.
  2. Workspace readers go through ``ensure_workspace()``, which rebuilds the
     object if it went missing. A ProjectWorkspace holds only the session_id
     plus read-through caches, so rebuilding is always safe — losing a write is
     not.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools import session_context  # noqa: E402
from tools.project_workspace import (  # noqa: E402
    ProjectWorkspace,
    ensure_workspace,
    get_workspace,
    set_workspace_session_id,
)

SID = "session-11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clean_session():
    session_context.cleanup_session(SID)
    tok = session_context.current_session_id.set(SID)
    yield
    session_context.current_session_id.reset(tok)
    session_context.cleanup_session(SID)


# ---------------------------------------------------------------------------
# Layer 2 — ensure_workspace() survives the purge
# ---------------------------------------------------------------------------

def test_the_original_bug_is_reproducible_with_get_workspace():
    """Pin WHY the fix exists: after a cleanup, get_workspace() returns None —
    which is exactly the ``Workspace not initialised (no session)`` branch."""
    set_workspace_session_id(SID)
    assert get_workspace() is not None

    session_context.cleanup_session(SID)  # what createNewSession did mid-turn

    assert get_workspace() is None


def test_ensure_workspace_rebuilds_after_a_midturn_purge():
    """The same sequence must now yield a usable workspace."""
    set_workspace_session_id(SID)
    session_context.cleanup_session(SID)

    ws = ensure_workspace()

    assert isinstance(ws, ProjectWorkspace)
    assert ws.session_id == SID
    # And it is registered, so the next reader gets the same object.
    assert get_workspace() is ws


def test_ensure_workspace_is_idempotent_and_does_not_replace_a_live_workspace():
    """It must not swap out the workspace an in-flight turn already holds —
    that would drop the spec/schema/progress read-through caches."""
    original = set_workspace_session_id(SID)
    assert ensure_workspace() is original
    assert ensure_workspace() is original


def test_ensure_workspace_still_returns_none_with_no_bound_session():
    """No session id means we genuinely cannot know where to write. Guessing a
    path would be worse than failing, so this case must keep failing."""
    session_context.current_session_id.set(None)
    assert ensure_workspace() is None


def test_a_rebuilt_workspace_targets_the_same_session_paths():
    """A rebuild is only safe if it resolves to the SAME storage location —
    otherwise the document would be written where nothing looks for it."""
    before = set_workspace_session_id(SID)
    key_before = before._s3_key("progress.json")
    specs_before = before._specs_s3_key("check_reservation.json")

    session_context.cleanup_session(SID)
    after = ensure_workspace()

    assert after is not before  # genuinely a new object
    assert after._s3_key("progress.json") == key_before
    assert after._specs_s3_key("check_reservation.json") == specs_before
    assert SID in key_before  # sanity: the session id really is in the path


def test_save_requirement_document_no_longer_hits_the_no_session_branch():
    """The exact prod failure, end to end.

    ``save_requirement_document`` is a Strands @tool, so call the undecorated
    function. With no S3 bucket and no NFS mount configured in the test env the
    write itself cannot succeed — but the point is that it gets PAST the
    workspace guard and reports a real storage error instead of the misleading
    "no session".
    """
    from tools import project_workspace as pw

    set_workspace_session_id(SID)
    session_context.cleanup_session(SID)  # the purge

    fn = getattr(pw.save_requirement_document, "_tool_func", None) or \
        getattr(pw.save_requirement_document, "__wrapped__", None) or \
        pw.save_requirement_document.original_function  # type: ignore[attr-defined]
    result = fn(doc_type="analysis", content="분석 문서 본문")

    assert "no session" not in str(result.get("error", "")), (
        f"still failing on the workspace guard: {result}"
    )


# ---------------------------------------------------------------------------
# Layer 1 — app.py must not reset a session with a running agent
# ---------------------------------------------------------------------------

def _read_app_py() -> str:
    with open(os.path.join(_HERE, "..", "app.py"), encoding="utf-8") as f:
        return f.read()


def test_create_new_session_guards_on_a_running_background_task():
    """Guard the source: the reset handler must bail out while an agent runs.

    Asserted structurally (the handler is an async WebSocket coroutine that
    needs a live socket, session store and task registry to invoke) — the check
    is that the guard sits BEFORE the destructive calls, since a guard placed
    after them would read as present while changing nothing.
    """
    src = _read_app_py()
    start = src.index("async def handle_create_new_session_ws")
    end = src.index("async def handle_import_asset_ws")
    handler = src[start:end]
    # Skip the docstring — it names the destructive calls to explain the bug, and
    # matching those mentions would compare positions in prose, not in code.
    body_at = handler.index('"""', handler.index('"""') + 3) + 3
    handler = handler[body_at:]

    assert "_background_tasks.get(session_id)" in handler, (
        "handle_create_new_session_ws no longer checks for a running agent task"
    )

    guard_at = handler.index("_background_tasks.get(session_id)")
    for destructive in ("clear_all_specs()", "cleanup_session("):
        assert destructive in handler
        assert guard_at < handler.index(destructive), (
            f"the running-task guard must come BEFORE {destructive}"
        )

    # It must return early rather than fall through into the reset.
    assert re.search(r"_bg_running[\s\S]{0,900}?\n        return\n", handler), (
        "the guard does not return early — the reset would still run"
    )


def test_the_early_return_still_answers_the_client():
    """The frontend blocks on ``session_created`` before it lets the user type
    (useWebSocket.ts: "DO NOT set isSessionReady here"). Bailing out silently
    would hang the composer, so the guard must still reply."""
    src = _read_app_py()
    start = src.index("async def handle_create_new_session_ws")
    handler = src[start:src.index("async def handle_import_asset_ws")]
    guarded = handler[handler.index("_bg_running"):]
    reply_at = guarded.index('"type": "session_created"')
    return_at = guarded.index("\n        return\n")

    assert reply_at < return_at, "the guard returns without sending session_created"


def test_no_workspace_reader_bypasses_ensure_workspace():
    """Every ``get_workspace()`` caller was a latent instance of this bug: it
    returns None after a purge and each call site then silently degrades. Only
    the accessor definitions themselves may mention it.
    """
    offenders = []
    for root, _dirs, files in os.walk(_SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for lineno, line in enumerate(text.splitlines(), 1):
                if "get_workspace()" not in line:
                    continue
                if "def get_workspace()" in line or "def ensure_workspace()" in line:
                    continue
                # project_workspace.py defines/uses both accessors legitimately.
                if os.path.basename(path) == "project_workspace.py":
                    continue
                offenders.append(f"{os.path.relpath(path, _SRC)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "these readers bypass ensure_workspace() and will silently no-op after a "
        "mid-turn purge:\n" + "\n".join(offenders)
    )
