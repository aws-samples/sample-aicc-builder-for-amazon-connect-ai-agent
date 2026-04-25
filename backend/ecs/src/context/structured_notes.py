"""
Structured Notes — Append-only note-taking for sub-agents

Sub-agents append compressed results to a shared all_results.txt file.
The orchestrator can read accumulated notes to build context.

NFS path: /mnt/s3/sessions/{id}/context/all_results.txt
"""

import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _notes_path(session_id: str) -> Path:
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    safe_id = session_id.replace("..", "_").replace("/", "_")
    return Path(mount_path) / "sessions" / safe_id / "context" / "all_results.txt"


def append_note(session_id: str, agent_name: str, content: str) -> bool:
    """Append a structured note from a sub-agent."""
    path = _notes_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n--- [{timestamp}] {agent_name} ---\n{content}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        return True
    except Exception as e:
        logger.error(f"Failed to append note for {session_id}: {e}")
        return False


def read_notes(session_id: str) -> str:
    """Read accumulated notes for a session."""
    path = _notes_path(session_id)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Failed to read notes for {session_id}: {e}")
    return ""


def clear_notes(session_id: str) -> bool:
    """Clear all notes for a session."""
    path = _notes_path(session_id)
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        logger.error(f"Failed to clear notes for {session_id}: {e}")
        return False
