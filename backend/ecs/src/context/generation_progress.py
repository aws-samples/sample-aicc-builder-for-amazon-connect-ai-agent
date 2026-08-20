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

# Canonical full asset set (everything the unscoped pipeline can produce).
# A scope equal to this set (or empty) means "full build".
FULL_ASSET_SET = GENERATION_ASSETS | {"knowledge_base"}

# Scoped (single-segment) generation ids the frontend may request.
VALID_SCOPE_IDS = {"contact_flow", "prompt", "faq"}

# Maps a requested scope id to the progress asset id it produces.
_SCOPE_TO_ASSET = {
    "contact_flow": "contact_flow",
    "prompt": "prompt",
    "faq": "knowledge_base",
}

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


# Completions are recorded on TWO paths: inline per toolResult during
# stream_async (record_tool_completion — the primary path) and again by the
# end-of-turn message scan (update_from_new_messages — the fallback for events
# the inline path missed). Without dedup the scan re-recorded every completion
# the inline path already logged, doubling the events list (observed: 17
# identical entries in one second) and — worse — re-applying the review-aware
# status transition, which downgraded a 'fixed' asset back to 'completed'.
# Both paths now remember the toolUseIds they consumed and skip known ones.
_MAX_RECORDED_TOOL_USE_IDS = 500


def _already_recorded(state: Dict[str, Any], tool_use_id: Optional[str]) -> bool:
    if not tool_use_id:
        return False  # no id → can't dedupe; keep legacy behavior
    return tool_use_id in state.get("recorded_tool_use_ids", [])


def _remember_tool_use_id(state: Dict[str, Any], tool_use_id: Optional[str]) -> None:
    if not tool_use_id:
        return
    ids: List[str] = state.setdefault("recorded_tool_use_ids", [])
    if tool_use_id in ids:
        return
    ids.append(tool_use_id)
    if len(ids) > _MAX_RECORDED_TOOL_USE_IDS:
        state["recorded_tool_use_ids"] = ids[-_MAX_RECORDED_TOOL_USE_IDS:]


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
                    "tool_use_id": tool_use_id,
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

    # Drop completions the inline path (record_tool_completion) already logged
    # this turn — re-recording duplicated every event and could downgrade a
    # 'fixed' asset back to 'completed'.
    deduped = [c for c in completions if not _already_recorded(state, c.get("tool_use_id"))]
    skipped = len(completions) - len(deduped)
    if skipped:
        logger.info(
            f"[generation_progress] skipped {skipped} completion(s) already recorded inline"
        )
    if not deduped:
        return
    completions = deduped

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
        event: Dict[str, Any] = {
            "asset_id": aid,
            "tool": c["tool_name"],
            "status": new_status,
            "timestamp": now,
        }
        if c.get("tool_use_id"):
            event["toolUseId"] = c["tool_use_id"]
        events.append(event)
        _remember_tool_use_id(state, c.get("tool_use_id"))

    _write_state(session_id, state)
    logger.info(
        f"[generation_progress] updated {session_id}: "
        + ", ".join(f"{c['asset_id']}={c['status']}" for c in completions)
    )


def record_tool_completion(
    session_id: str,
    tool_name: str,
    status: str = "completed",
    tool_use_id: Optional[str] = None,
) -> bool:
    """Record a single tool completion directly (called from streaming event loop).

    This is the primary recording path — called inline when a toolResult event
    is received during stream_async, so it does not depend on post-hoc message
    scanning which can miss events due to message sanitization or reference issues.

    ``tool_use_id`` (when available) is remembered so the end-of-turn fallback
    scan (update_from_new_messages) does not record the same completion again.

    Returns True if the tool was recorded, False if it was not a tracked tool
    or was already recorded.
    """
    asset_id = _TOOL_TO_ASSET.get(tool_name)
    if not asset_id:
        return False

    state = _read_state(session_id)
    if _already_recorded(state, tool_use_id):
        logger.debug(
            f"[generation_progress] {tool_name} ({tool_use_id}) already recorded — skipping"
        )
        return False

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
    event: Dict[str, Any] = {
        "asset_id": asset_id,
        "tool": tool_name,
        "status": new_status,
        "timestamp": now,
    }
    if tool_use_id:
        event["toolUseId"] = tool_use_id
    events.append(event)
    _remember_tool_use_id(state, tool_use_id)

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


def get_selected_model(session_id: str) -> Optional[str]:
    """Read the persisted Bedrock model id for this session (None if unset)."""
    state = _read_state(session_id)
    return state.get("selected_model")


def set_selected_model(session_id: str, model_id: str) -> None:
    """Persist the selected Bedrock model id (survives reconnect/restart)."""
    state = _read_state(session_id)
    if state.get("selected_model") == model_id:
        return
    state["selected_model"] = model_id
    _write_state(session_id, state)
    logger.info(f"[selected_model] {session_id}: -> {model_id}")


def get_selected_effort(session_id: str) -> Optional[str]:
    """Read the persisted Anthropic effort level for this session (None if unset)."""
    state = _read_state(session_id)
    return state.get("selected_effort")


def set_selected_effort(session_id: str, effort: Optional[str]) -> None:
    """Persist the selected effort level; None/"" clears it (model default)."""
    state = _read_state(session_id)
    if state.get("selected_effort") == effort:
        return
    if effort:
        state["selected_effort"] = effort
    else:
        state.pop("selected_effort", None)
    _write_state(session_id, state)
    logger.info(f"[selected_effort] {session_id}: -> {effort or '(default)'}")


def get_generation_scope(session_id: str) -> List[str]:
    """Return the requested generation scope as asset ids.

    Default (no scope set, or "full") → the canonical full asset set.
    Scoped runs map the requested ids ({contact_flow, prompt, faq}) to their
    produced asset ids ({contact_flow, prompt, knowledge_base}).
    """
    state = _read_state(session_id)
    raw = state.get("generation_scope")
    if not raw:
        return sorted(FULL_ASSET_SET)
    assets: List[str] = []
    for sid in raw:
        mapped = _SCOPE_TO_ASSET.get(sid)
        if mapped and mapped not in assets:
            assets.append(mapped)
    return assets or sorted(FULL_ASSET_SET)


def set_generation_scope(session_id: str, scope: List[str]) -> None:
    """Persist the requested generation scope (list of {contact_flow, prompt, faq}).

    Invalid ids are dropped. An empty/None scope clears the override (full build).
    """
    cleaned = [s for s in (scope or []) if s in VALID_SCOPE_IDS]
    state = _read_state(session_id)
    if cleaned:
        state["generation_scope"] = cleaned
    else:
        state.pop("generation_scope", None)
    _write_state(session_id, state)
    logger.info(f"[generation_scope] {session_id}: -> {cleaned or 'full'}")


def mark_imported_session(session_id: str) -> None:
    """Flag a session as having an externally-imported asset.

    detect_phase honors this flag and returns 'post_generation' regardless of how
    many assets are 'complete' — an imported single asset would otherwise satisfy
    the scoped review gate and be routed to review instead of the edit flow.
    """
    state = _read_state(session_id)
    state["imported"] = True
    _write_state(session_id, state)
    logger.info(f"[imported] {session_id}: marked imported")


def is_imported_session(session_id: str) -> bool:
    """True if this session was seeded from an imported asset."""
    return bool(_read_state(session_id).get("imported"))


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

    # Scope-derived expectations. For an unscoped (full) run this resolves to the
    # canonical full asset set, preserving legacy behavior exactly.
    scope = set(get_generation_scope(session_id))
    required_core = {a for a in scope if a in GENERATION_ASSETS}
    # Non-core scoped assets (e.g. faq → knowledge_base) gate the terminal rule
    # below when the scope produces nothing in GENERATION_ASSETS.
    scoped_non_core = {a for a in scope if a not in GENERATION_ASSETS}

    if not assets:
        # Check if interview was completed (handoff marker exists)
        from tools.interview_completion import check_interview_handoff
        if check_interview_handoff(session_id):
            return "generation"
        return state.get("phase", "interview")

    # Check which core assets are completed
    core_completed = set()
    non_core_completed = set()
    has_review = False
    has_post_review_fix = False

    for asset_id, info in assets.items():
        status = info.get("status", "")
        if asset_id in GENERATION_ASSETS and status in ("completed", "fixed", "reviewed"):
            core_completed.add(asset_id)
        elif status in ("completed", "fixed", "reviewed"):
            non_core_completed.add(asset_id)
        if asset_id == "review" and status == "reviewed":
            has_review = True
        if status == "fixed":
            has_post_review_fix = True

    # 1c. Imported asset → always post_generation (modification mode). An imported
    #     single asset satisfies the scoped review gate below, so without this it
    #     would route to 'review' instead of the patch-only edit flow the user wants.
    if state.get("imported"):
        return "post_generation"

    # 2. Review completed → post_generation (user can request targeted fixes)
    #    Whether or not fixes have been applied, once review is done
    #    the next turn should use the regeneration prompt.
    if has_review:
        return "post_generation"

    # 3a. Scopes whose assets fall entirely outside GENERATION_ASSETS (e.g.
    #     FAQ-only → knowledge_base): once those scoped assets complete there is
    #     no vacuous "core" set to wait on — advance to post_generation so the
    #     user can iterate on the produced asset.
    if not required_core and scoped_non_core:
        if scoped_non_core.issubset(non_core_completed):
            return "post_generation"

    # 3b. Required core assets (full set, or the scoped subset) all completed → review
    if required_core and required_core.issubset(core_completed):
        return "review"

    # 4. Some assets exist → generation
    if core_completed or non_core_completed:
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
