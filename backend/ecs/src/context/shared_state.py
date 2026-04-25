"""
Shared State — Context Engineering Deep Insight Pattern

Provides a shared state object that sub-agents can read/write for
cross-agent context sharing. Persisted to NFS at:
    /mnt/s3/sessions/{id}/context/shared_state.json

State structure:
    {
        "messages": [{"agent": "...", "content": "...", "timestamp": "..."}],
        "clues": [{"agent": "...", "clue": "...", "priority": 1-5}],
        "full_plan": "...",
        "history": [{"phase": "...", "summary": "..."}],
    }
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class SharedState:
    """Cross-agent shared state persisted to NFS."""

    def __init__(self, session_id: str, mount_path: str = ""):
        self.session_id = session_id
        self._mount_path = mount_path or os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
        self._state: Dict[str, Any] = {
            "messages": [],
            "clues": [],
            "full_plan": "",
            "history": [],
        }
        self._load()

    def _file_path(self) -> Path:
        safe_id = self.session_id.replace("..", "_").replace("/", "_")
        return Path(self._mount_path) / "sessions" / safe_id / "context" / "shared_state.json"

    def _load(self) -> None:
        path = self._file_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load shared state: {e}")

    def _save(self) -> None:
        path = self._file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, default=str)
            tmp.rename(path)
        except Exception as e:
            logger.error(f"Failed to save shared state: {e}")

    # ── Messages ─────────────────────────────────────────────────

    def add_message(self, agent_name: str, content: str) -> None:
        self._state["messages"].append({
            "agent": agent_name,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        })
        self._save()

    def get_messages(self, agent_filter: Optional[str] = None) -> List[Dict]:
        msgs = self._state.get("messages", [])
        if agent_filter:
            return [m for m in msgs if m.get("agent") == agent_filter]
        return msgs

    # ── Clues (compressed insights) ──────────────────────────────

    def add_clue(self, agent_name: str, clue: str, priority: int = 3) -> None:
        self._state["clues"].append({
            "agent": agent_name,
            "clue": clue,
            "priority": max(1, min(5, priority)),
            "timestamp": datetime.utcnow().isoformat(),
        })
        self._save()

    def get_clues(self, min_priority: int = 1) -> List[Dict]:
        return [
            c for c in self._state.get("clues", [])
            if c.get("priority", 3) >= min_priority
        ]

    # ── Plan ─────────────────────────────────────────────────────

    def set_plan(self, plan: str) -> None:
        self._state["full_plan"] = plan
        self._save()

    def get_plan(self) -> str:
        return self._state.get("full_plan", "")

    # ── History ──────────────────────────────────────────────────

    def add_history(self, phase: str, summary: str) -> None:
        self._state["history"].append({
            "phase": phase,
            "summary": summary,
            "timestamp": datetime.utcnow().isoformat(),
        })
        self._save()

    def get_history(self) -> List[Dict]:
        return self._state.get("history", [])

    # ── Full state ───────────────────────────────────────────────

    def get_state(self) -> Dict[str, Any]:
        return self._state.copy()


# Module-level singleton
_shared_states: Dict[str, SharedState] = {}


def get_shared_state(session_id: str) -> SharedState:
    """Get or create shared state for a session."""
    if session_id not in _shared_states:
        _shared_states[session_id] = SharedState(session_id)
    return _shared_states[session_id]
