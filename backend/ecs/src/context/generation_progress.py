"""
Generation Progress — Persistent progress tracking for context engineering.

Scans Strands-format messages for sub-agent tool completions and maintains
an authoritative log of what has been generated / reviewed / fixed.

The orchestrator injects this into its system context so it survives
conversation history pruning and never re-generates completed assets.

NFS path: /mnt/s3/sessions/{session_id}/context/generation_progress.json
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Core assets whose completion signals transition interview → generation → review
GENERATION_ASSETS = {"cdk", "lambda", "openapi", "prompt", "contact_flow"}

# Sub-agent tool name → progress asset ID (mirrors app.py mapping)
_TOOL_TO_ASSET: Dict[str, str] = {
    "infrastructure_generator_agent": "cdk",
    "merge_infrastructure_fragments": "cdk",
    "merge_openapi_fragments": "openapi",
    "lambda_generator_agent": "lambda",
    "openapi_generator_agent": "openapi",
    "prompt_generator_agent": "prompt",
    "contact_flow_generator_agent": "contact_flow",
    "faq_generator_agent": "knowledge_base",
    "reviewer_agent": "review",
}

# Human-readable labels for the summary
_ASSET_LABELS: Dict[str, str] = {
    "lambda": "Lambda Functions",
    "openapi": "OpenAPI Spec",
    "prompt": "Prompt Templates",
    "contact_flow": "Contact Flow",
    "cdk": "CDK Infrastructure",
    "knowledge_base": "Knowledge Base / FAQ",
    "review": "Review",
}


def _progress_path(session_id: str) -> Path:
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    safe_id = session_id.replace("..", "_").replace("/", "_")
    return Path(mount_path) / "sessions" / safe_id / "context" / "generation_progress.json"


def _read_state(session_id: str) -> Dict[str, Any]:
    path = _progress_path(session_id)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[generation_progress] failed to read state: {e}")
    return {"assets": {}, "events": []}


def _write_state(session_id: str, state: Dict[str, Any]) -> None:
    path = _progress_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        tmp.rename(path)
    except Exception as e:
        logger.warning(f"[generation_progress] failed to write state: {e}")


def update_from_new_messages(session_id: str, messages: List[Dict[str, Any]]) -> None:
    """Scan Strands-format messages for sub-agent completions and update progress.

    Messages follow the Strands format:
        [{"role": "assistant", "content": [{"toolUse": {...}}, ...]},
         {"role": "user",      "content": [{"toolResult": {...}}, ...]}]
    """
    if not messages:
        logger.debug("[generation_progress] no messages to scan")
        return

    # Build toolUseId → tool_name mapping from assistant messages
    tool_names: Dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for block in (msg.get("content") or []):
            if isinstance(block, dict) and "toolUse" in block:
                tu = block["toolUse"]
                tool_use_id = tu.get("toolUseId", "")
                tool_name = tu.get("name", "")
                if tool_use_id and tool_name:
                    tool_names[tool_use_id] = tool_name

    logger.info(
        f"[generation_progress] scanned {len(messages)} messages, "
        f"found {len(tool_names)} toolUse entries: {list(tool_names.values())}"
    )

    # Scan tool results for known sub-agent completions
    completions: List[Dict[str, str]] = []
    unmatched_tools: List[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        for block in (msg.get("content") or []):
            if isinstance(block, dict) and "toolResult" in block:
                tr = block["toolResult"]
                tool_use_id = tr.get("toolUseId", "")
                tool_name = tool_names.get(tool_use_id, "")
                asset_id = _TOOL_TO_ASSET.get(tool_name)
                if not asset_id:
                    if tool_name:
                        unmatched_tools.append(tool_name)
                    continue
                status = tr.get("status", "success")
                completions.append({
                    "asset_id": asset_id,
                    "tool_name": tool_name,
                    "status": "completed" if status == "success" else status,
                })

    if unmatched_tools:
        logger.info(
            f"[generation_progress] tools not in _TOOL_TO_ASSET (ignored): {unmatched_tools}"
        )

    if not completions:
        logger.info(
            f"[generation_progress] no matching completions found for session {session_id}"
        )
        return

    # Merge into persistent state
    state = _read_state(session_id)
    assets = state.setdefault("assets", {})
    events = state.setdefault("events", [])
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for c in completions:
        aid = c["asset_id"]
        prev = assets.get(aid, {})
        prev_status = prev.get("status")

        # Determine new status with review-aware transitions
        if aid == "review":
            new_status = "reviewed"
        elif prev_status in ("reviewed", "fix_in_progress") and c["status"] == "completed":
            # Regenerated after review → fixed
            new_status = "fixed"
        else:
            new_status = c["status"]

        assets[aid] = {
            "status": new_status,
            "tool": c["tool_name"],
            "updated_at": now,
        }
        events.append({
            "asset_id": aid,
            "tool": c["tool_name"],
            "status": new_status,
            "timestamp": now,
        })

    _write_state(session_id, state)
    logger.info(
        f"[generation_progress] updated {session_id}: "
        + ", ".join(f"{c['asset_id']}={c['status']}" for c in completions)
    )


def record_tool_completion(
    session_id: str,
    tool_name: str,
    status: str = "completed",
) -> bool:
    """Record a single tool completion directly (called from streaming event loop).

    This is the primary recording path — called inline when a toolResult event
    is received during stream_async, so it does not depend on post-hoc message
    scanning which can miss events due to message sanitization or reference issues.

    Returns True if the tool was recorded, False if it was not a tracked tool.
    """
    asset_id = _TOOL_TO_ASSET.get(tool_name)
    if not asset_id:
        return False

    state = _read_state(session_id)
    assets = state.setdefault("assets", {})
    events = state.setdefault("events", [])
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    prev = assets.get(asset_id, {})
    prev_status = prev.get("status")

    # Determine new status with review-aware transitions
    if asset_id == "review":
        new_status = "reviewed"
    elif prev_status in ("reviewed", "fix_in_progress") and status == "completed":
        new_status = "fixed"
    else:
        new_status = status

    assets[asset_id] = {
        "status": new_status,
        "tool": tool_name,
        "updated_at": now,
    }
    events.append({
        "asset_id": asset_id,
        "tool": tool_name,
        "status": new_status,
        "timestamp": now,
    })

    _write_state(session_id, state)
    logger.info(f"[generation_progress] recorded {tool_name} -> {asset_id}={new_status} for {session_id}")
    return True


def read_progress(session_id: str) -> Optional[str]:
    """Read generation progress as a human-readable string for context injection.

    Returns None if no progress has been recorded yet.
    """
    state = _read_state(session_id)
    assets = state.get("assets", {})
    if not assets:
        return None

    # Status → emoji mapping
    status_icons = {
        "completed": "\u2705",
        "reviewed": "\ud83d\udcdd",
        "fixed": "\ud83d\udd27\u2192\u2705",
        "error": "\u274c",
    }

    lines = []
    for asset_id, info in assets.items():
        label = _ASSET_LABELS.get(asset_id, asset_id)
        icon = status_icons.get(info.get("status", ""), "\u23f3")
        ts = info.get("updated_at", "")
        lines.append(f"{icon} {label}: {info.get('status', 'unknown')} ({ts})")

    # Append recent event log (last 10)
    events = state.get("events", [])
    if events:
        lines.append("")
        lines.append("Recent events:")
        for ev in events[-10:]:
            label = _ASSET_LABELS.get(ev["asset_id"], ev["asset_id"])
            lines.append(f"  [{ev['timestamp']}] {label} \u2192 {ev['status']}")

    return "\n".join(lines)


# =========================================================================
# Phase state management
# =========================================================================

def read_phase(session_id: str) -> str:
    """Read current phase from NFS state. Default is 'interview'."""
    state = _read_state(session_id)
    return state.get("phase", "interview")


def update_phase(session_id: str, new_phase: str, trigger: str = "") -> tuple:
    """Transition to a new phase. Returns (previous_phase, changed: bool).

    If the phase is already the target, no write occurs.
    """
    state = _read_state(session_id)
    old_phase = state.get("phase", "interview")
    if old_phase == new_phase:
        return old_phase, False

    state["phase"] = new_phase
    history = state.setdefault("phase_history", [])
    history.append({
        "from": old_phase,
        "to": new_phase,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger,
    })
    _write_state(session_id, state)
    logger.info(f"[phase] {session_id}: {old_phase} → {new_phase} (trigger={trigger})")
    return old_phase, True


def detect_phase(session_id: str) -> str:
    """Deterministically detect the current phase from asset states.

    Always inspects asset completion states to detect transitions —
    the explicit 'phase' field is used only as fallback when no assets exist.

    Detection logic:
    1. No assets + no interview handoff → explicit phase or 'interview'
    1b. No assets + interview handoff exists → 'generation' (ready to start)
    2. Review completed → 'post_generation' (regeneration mode)
    3. Core assets (lambda, openapi, prompt, contact_flow) all completed → 'review'
    4. Some assets exist → 'generation'
    5. Default → 'interview'
    """
    state = _read_state(session_id)
    assets = state.get("assets", {})

    if not assets:
        # Check if interview was completed (handoff marker exists)
        from tools.interview_completion import check_interview_handoff
        if check_interview_handoff(session_id):
            return "generation"
        return state.get("phase", "interview")

    # Check which core assets are completed
    core_completed = set()
    has_review = False
    has_post_review_fix = False

    for asset_id, info in assets.items():
        status = info.get("status", "")
        if asset_id in GENERATION_ASSETS and status in ("completed", "fixed", "reviewed"):
            core_completed.add(asset_id)
        if asset_id == "review" and status == "reviewed":
            has_review = True
        if status == "fixed":
            has_post_review_fix = True

    # 2. Review completed → post_generation (user can request targeted fixes)
    #    Whether or not fixes have been applied, once review is done
    #    the next turn should use the regeneration prompt.
    if has_review:
        return "post_generation"

    # 3. Core assets (lambda, openapi, prompt, contact_flow) all completed → review
    required_core = {"lambda", "openapi", "prompt", "contact_flow"}
    if required_core.issubset(core_completed):
        return "review"

    # 4. Some assets exist → generation
    if core_completed:
        return "generation"

    # 5. Default
    return state.get("phase", "interview")


def get_frontend_progress_state(session_id: str) -> Dict[str, Any]:
    """Return NFS asset statuses mapped to frontend ProgressItem format.

    Returns a dict like {"lambda": {"status": "completed", "progress": 100}, ...}
    where keys match frontend progress item IDs and status/progress values are
    compatible with the frontend's updateProgress(itemId, status, progress) API.
    """
    state = _read_state(session_id)
    assets = state.get("assets", {})
    result: Dict[str, Any] = {}
    for asset_id, info in assets.items():
        nfs_status = info.get("status", "")
        if nfs_status in ("completed", "reviewed", "fixed"):
            result[asset_id] = {"status": "completed", "progress": 100}
        elif nfs_status in ("in_progress", "fix_in_progress"):
            result[asset_id] = {"status": "in_progress", "progress": 50}
        elif nfs_status == "error":
            result[asset_id] = {"status": "pending", "progress": 0}
        else:
            result[asset_id] = {"status": "in_progress", "progress": 10}
    return result
