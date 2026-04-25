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
