"""
Interview Completion Tool — Signals the end of the interview phase.

When the Interview Agent calls this tool, it:
1. Writes a handoff marker to NFS (context/interview_complete.json)
2. The next user message triggers context boundary: history is archived and cleared
3. The Generation Orchestrator starts fresh, loading specs from workspace
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from strands import tool

logger = logging.getLogger(__name__)


def _handoff_path(session_id: str) -> Path:
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    safe_id = session_id.replace("..", "_").replace("/", "_")
    return Path(mount_path) / "sessions" / safe_id / "context" / "interview_complete.json"


def write_interview_handoff(session_id: str, summary: str) -> bool:
    path = _handoff_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "completed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.rename(path)
        logger.info(f"[interview_completion] handoff marker written for {session_id}")
        return True
    except Exception as e:
        logger.error(f"[interview_completion] failed to write handoff: {e}")
        return False


def check_interview_handoff(session_id: str) -> dict | None:
    path = _handoff_path(session_id)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[interview_completion] failed to read handoff: {e}")
    return None


@tool
def complete_interview(session_id: str, summary: str = "") -> dict:
    """Signal that the interview phase is complete and the Generation Orchestrator can begin.

    Call this AFTER the user has confirmed the analysis document.
    This triggers a context boundary — the generation phase will start with fresh context
    and will read all specifications from the workspace files you have saved.

    Args:
        session_id: The current session ID (from context prefix)
        summary: Brief summary of what was collected (company, industry, operation count, db_type)

    Returns:
        dict with success status and message
    """
    from tools.acxd_flow_spec import acxd_flow_spec_ready, is_acxd_target

    if is_acxd_target(session_id):
        ready, problems = acxd_flow_spec_ready()
        if not ready:
            return {
                "success": False,
                "message": "ACXD flow design is incomplete. Confirm every flow step before generation.",
                "problems": problems,
            }

    # What generation would have to guess must be in the spec first: response
    # fields with types, enum values, slots that name a real input field,
    # escalation conditions and the agent's context payload. Each gap here was
    # filled differently by each generator on live sessions.
    try:
        from tools.spec_completeness import spec_completeness_problems
        gaps = spec_completeness_problems(session_id)
    except Exception:
        gaps = []
    if gaps:
        return {
            "success": False,
            "message": ("The spec still leaves things for generation to guess. Resolve each item "
                        "(ask the customer, then save_operation_spec / upsert_acxd_flow_plan / "
                        "upsert_acxd_application) and call complete_interview again."),
            "problems": gaps[:40],
            "problem_count": len(gaps),
        }

    success = write_interview_handoff(session_id, summary)

    if success:
        return {
            "success": True,
            "message": "Interview complete. Generation phase will begin on next user message.",
            "handoff_summary": summary,
        }
    else:
        return {
            "success": False,
            "message": "Failed to write handoff marker. Please try again.",
        }
