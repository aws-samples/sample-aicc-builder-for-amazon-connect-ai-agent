"""Message log: sequence numbers restart every turn, so a client's position is (turn, seq)."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from context.message_log import MessageLog  # noqa: E402


def test_turn_id_changes_on_clear_and_survives_a_new_instance(tmp_path):
    log = MessageLog(tmp_path / "message_log")
    assert log.turn_id == ""  # nothing logged yet
    log.clear()
    first = log.turn_id
    assert first
    log.append({"type": "stream", "content": "a"})
    log.clear()
    assert log.turn_id != first
    # another process (ECS task) opening the same NFS directory sees the same turn
    assert MessageLog(tmp_path / "message_log").turn_id == log.turn_id


def test_read_after_ignores_a_pointer_from_an_older_turn(tmp_path):
    """QA 2026-09-11: after a fresh turn the client's stored seq (142) skipped every
    event of the new turn, so nothing was replayed on return."""
    log = MessageLog(tmp_path / "message_log")
    log.clear()
    old_turn = log.turn_id
    for i in range(3):
        log.append({"type": "stream", "content": str(i)})
    assert [e["seq"] for e in log.read_after(2, turn_id=old_turn)] == [3]

    log.clear()  # next turn: numbering restarts at 1
    log.append({"type": "stream", "content": "new"})
    # pointer from the old turn → whole current log
    assert [e["seq"] for e in log.read_after(142, turn_id=old_turn)] == [1]
    # pointer from the current turn → strictly newer entries only
    assert log.read_after(1, turn_id=log.turn_id) == []
    # no turn given (legacy caller) → plain seq filter
    assert [e["seq"] for e in log.read_after(0)] == [1]
