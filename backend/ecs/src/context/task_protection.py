"""ECS task scale-in protection for in-flight agent turns.

An interview or generation turn runs 5–15 minutes on one Fargate task. When
Application Auto Scaling scaled the service in (or a rolling deploy replaced the
task) that task was stopped mid-turn: the WebSocket dropped, the turn was
cancelled, and the user saw "turns getting lost". Fargate exposes task
scale-in protection through the ECS agent endpoint; while it is on, neither
scale-in nor a deployment stops the task (they wait for it to lapse).

Reference counted: protection is armed when the first turn starts and released
when the last one ends. Everything here is best-effort — outside ECS (local
dev, tests) ``ECS_AGENT_URI`` is unset and the calls are no-ops.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

#: Upper bound on one protected stretch. Renewed by every turn start, so a
#: session of back-to-back turns stays protected; a single turn longer than
#: this simply loses protection for its tail.
PROTECTION_MINUTES = int(os.environ.get("ECS_TASK_PROTECTION_MINUTES", "60"))

_lock = threading.Lock()
_active_turns = 0


def _endpoint() -> str | None:
    base = os.environ.get("ECS_AGENT_URI")
    if not base:
        return None
    return f"{base.rstrip('/')}/task-protection/v1/state"


def _send(enabled: bool) -> bool:
    url = _endpoint()
    if url is None:
        return False
    body: dict = {"ProtectionEnabled": enabled}
    if enabled:
        body["ExpiresInMinutes"] = PROTECTION_MINUTES
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 — link-local ECS agent
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("[task_protection] %s failed: %s", "enable" if enabled else "disable", exc)
        return False
    failure = payload.get("failure")
    if failure:
        logger.warning("[task_protection] %s refused: %s", "enable" if enabled else "disable", failure)
        return False
    logger.info("[task_protection] %s (active turns=%d)", "enabled" if enabled else "disabled", _active_turns)
    return True


def turn_started() -> int:
    """Register a running turn; arms (or renews) protection. Returns the count."""
    global _active_turns
    with _lock:
        _active_turns += 1
        count = _active_turns
    if _endpoint():
        _send(True)
    return count


def turn_finished() -> int:
    """Unregister a turn; releases protection when it was the last one."""
    global _active_turns
    with _lock:
        _active_turns = max(0, _active_turns - 1)
        count = _active_turns
    if count == 0 and _endpoint():
        _send(False)
    return count


def active_turns() -> int:
    return _active_turns
