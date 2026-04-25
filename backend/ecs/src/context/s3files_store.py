"""
S3 Files NFS Context Store — 3-Tier Session Storage

Provides session state persistence using S3 Files NFS mount as Tier 2
with in-memory cache as Tier 1. Same API as redis_store.py (drop-in replacement).

3-Tier Storage:
- Tier 1: In-memory cache (hot path, fastest)
- Tier 2: NFS mount at /mnt/s3/sessions/{id}/state/ (persistent across restarts)
- Tier 3: DynamoDB metadata (managed by frontend session service, not this module)

NFS Layout:
    /mnt/s3/sessions/{session_id}/
        state/project.json
        state/progress.json
        assets/specs/{op_id}.json
        context/conversation_history.json
        context/shared_state.json
        context/all_results.txt
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

from .models import SessionContext, ConversationMessage

logger = logging.getLogger(__name__)

# Configuration
MAX_HISTORY_MESSAGES = 60  # 30 turns (user + assistant)
CONTEXT_TTL_SECONDS = 8 * 60 * 60  # 8 hours


class S3FilesContextStore:
    """3-tier session context store: memory → NFS → DynamoDB metadata."""

    def __init__(self, mount_path: str = "/mnt/s3"):
        self._mount_path = mount_path
        # Tier 1: In-memory cache
        self._memory_cache: Dict[str, SessionContext] = {}
        self._history_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._subagent_cache: Dict[str, Dict[str, Any]] = {}

    # ── Path helpers ─────────────────────────────────────────────────

    def _session_dir(self, session_id: str) -> Path:
        safe_id = session_id.replace("..", "_").replace("/", "_")
        return Path(self._mount_path) / "sessions" / safe_id

    def _state_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "state"

    def _context_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "context"

    def _ensure_dir(self, path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.error(f"Failed to create directory {path}: {e}")
            return False

    def _write_json(self, path: Path, data: Any) -> bool:
        try:
            self._ensure_dir(path.parent)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            tmp.rename(path)
            return True
        except Exception as e:
            logger.error(f"Failed to write {path}: {e}")
            return False

    def _read_json(self, path: Path) -> Optional[Any]:
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")
        return None

    # ── Session Context ──────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get session context (Tier 1 → Tier 2 fallback)."""
        # Tier 1: Memory
        if session_id in self._memory_cache:
            return self._memory_cache[session_id]

        # Tier 2: NFS
        path = self._state_path(session_id) / "project.json"
        data = self._read_json(path)
        if data:
            try:
                ctx = SessionContext.from_dict(data)
                self._memory_cache[session_id] = ctx
                return ctx
            except Exception as e:
                logger.warning(f"Failed to parse session {session_id}: {e}")

        return None

    def save_session(self, context: SessionContext) -> None:
        """Save session context to Tier 1 + Tier 2."""
        context.updated_at = datetime.utcnow().isoformat()
        # Tier 1
        self._memory_cache[context.session_id] = context
        # Tier 2
        path = self._state_path(context.session_id) / "project.json"
        self._write_json(path, context.to_dict())

    def update_session(self, session_id: str, **updates) -> SessionContext:
        """Partially update session context."""
        context = self.get_session(session_id)
        if context is None:
            context = SessionContext(session_id=session_id)

        for key, value in updates.items():
            if not hasattr(context, key):
                continue
            if key == "operations" and isinstance(value, list):
                existing = {op.get("operation_id"): op for op in context.operations}
                for op in value:
                    op_id = op.get("operation_id")
                    if op_id:
                        existing[op_id] = {**existing.get(op_id, {}), **op}
                context.operations = list(existing.values())
            elif key == "completeness" and isinstance(value, dict):
                context.completeness.update(value)
            elif key == "generated_assets" and isinstance(value, dict):
                context.generated_assets.update(value)
            else:
                setattr(context, key, value)

        self.save_session(context)
        return context

    # ── Conversation History ─────────────────────────────────────────

    def get_conversation_history(
        self,
        session_id: str,
        limit: int = MAX_HISTORY_MESSAGES,
    ) -> List[Dict[str, Any]]:
        """Get conversation history (Tier 1 → Tier 2)."""
        # Tier 1
        if session_id in self._history_cache:
            return self._history_cache[session_id][-limit:]

        # Tier 2
        path = self._context_path(session_id) / "conversation_history.json"
        data = self._read_json(path)
        if data and isinstance(data, list):
            self._history_cache[session_id] = data
            return data[-limit:]

        return []

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append message to conversation history."""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if metadata:
            message["metadata"] = metadata

        if session_id not in self._history_cache:
            self._history_cache[session_id] = self.get_conversation_history(session_id, limit=MAX_HISTORY_MESSAGES)

        self._history_cache[session_id].append(message)
        # Prune
        self._history_cache[session_id] = self._history_cache[session_id][-MAX_HISTORY_MESSAGES:]

        # Write to NFS
        path = self._context_path(session_id) / "conversation_history.json"
        self._write_json(path, self._history_cache[session_id])

    def save_conversation_history(
        self,
        session_id: str,
        history: List[Dict[str, Any]],
    ) -> None:
        """Save full conversation history to NFS."""
        pruned = history[-MAX_HISTORY_MESSAGES:]
        self._history_cache[session_id] = pruned
        path = self._context_path(session_id) / "conversation_history.json"
        self._write_json(path, pruned)

    def load_conversation_history(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """Load conversation history from NFS (replaces AgentCore Memory load)."""
        path = self._context_path(session_id) / "conversation_history.json"
        data = self._read_json(path)
        if data and isinstance(data, list):
            self._history_cache[session_id] = data
            return data
        return None

    # ── Sub-Agent State ──────────────────────────────────────────────

    def get_subagent_state(
        self,
        session_id: str,
        agent_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Get sub-agent state."""
        key = f"{session_id}:{agent_name}"
        if key in self._subagent_cache:
            return self._subagent_cache[key]
        return None

    def save_subagent_state(
        self,
        session_id: str,
        agent_name: str,
        state: Dict[str, Any],
    ) -> None:
        """Save sub-agent state (in-memory only — no NFS persistence needed)."""
        key = f"{session_id}:{agent_name}"
        self._subagent_cache[key] = state

    # ── Session Cleanup ──────────────────────────────────────────────

    def delete_session(self, session_id: str) -> None:
        """Delete all session data."""
        # Tier 1 cleanup
        self._memory_cache.pop(session_id, None)
        self._history_cache.pop(session_id, None)
        keys_to_delete = [k for k in self._subagent_cache if k.startswith(f"{session_id}:")]
        for k in keys_to_delete:
            self._subagent_cache.pop(k, None)

        # Tier 2: Don't delete NFS files (they'll expire via S3 lifecycle rules)
        logger.info(f"Session {session_id} cleared from memory cache")

    # ── Health Check ─────────────────────────────────────────────────

    def health_check(self) -> Dict[str, Any]:
        """Check store health."""
        nfs_ok = os.path.isdir(self._mount_path)
        return {
            "status": "healthy" if nfs_ok else "degraded",
            "backend": "s3files_nfs",
            "mount_path": self._mount_path,
            "nfs_available": nfs_ok,
            "sessions_cached": len(self._memory_cache),
        }


# ========================================
# Singleton & Convenience Functions
# ========================================
_context_store: Optional[S3FilesContextStore] = None


def get_context_store() -> S3FilesContextStore:
    """Get context store singleton."""
    global _context_store
    if _context_store is None:
        mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
        _context_store = S3FilesContextStore(mount_path)
    return _context_store


def get_session_context(session_id: str) -> Optional[SessionContext]:
    return get_context_store().get_session(session_id)


def save_session_context(context: SessionContext) -> None:
    get_context_store().save_session(context)


def update_session_context(session_id: str, **updates) -> SessionContext:
    return get_context_store().update_session(session_id, **updates)


def get_session_history(session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> List[Dict[str, Any]]:
    return get_context_store().get_conversation_history(session_id, limit)


def append_session_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    get_context_store().append_message(session_id, role, content, metadata)


def clear_session(session_id: str) -> None:
    get_context_store().delete_session(session_id)


def format_history_for_injection(
    history: List[Dict[str, Any]],
    max_content_length: int = 1000,
    max_messages: int = 20,
    workspace_summary: str = "",
) -> str:
    """Format conversation history for injection (same as redis_store version).

    Handles both legacy text-only format and full Strands format.
    """
    if not history and not workspace_summary:
        return ""

    formatted_parts = []

    if workspace_summary:
        formatted_parts.extend([
            "[프로젝트 상태 복원 — Project State Restored]",
            "---",
            workspace_summary,
            "---",
            "",
        ])

    if history:
        recent_history = history[-max_messages:]
        formatted_parts.extend([
            "[이전 대화 내용 - Previous Conversation Context]",
            "---",
        ])
        for msg in recent_history:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            # Handle Strands format: content is a list of blocks
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if "text" in block:
                            text_parts.append(block["text"])
                        elif "toolUse" in block:
                            tu = block["toolUse"]
                            text_parts.append(f"[Tool: {tu.get('name', 'unknown')}]")
                        elif "toolResult" in block:
                            tr = block["toolResult"]
                            status = tr.get("status", "success")
                            text_parts.append(f"[Tool Result: {status}]")
                content = " ".join(text_parts)

            if role == "user":
                role_label = "사용자 (User)"
            elif role == "assistant":
                role_label = "어시스턴트 (Assistant)"
            else:
                role_label = role
            if len(content) > max_content_length:
                content = content[:max_content_length] + "... [truncated]"
            formatted_parts.append(f"{role_label}: {content}")
        formatted_parts.extend([
            "---",
            "[이전 대화 끝 - End of Previous Context]",
        ])

    formatted_parts.extend([
        "",
        "위 내용은 이전 세션의 대화 내용입니다. 이 맥락을 참고하여 아래 새로운 메시지에 응답해주세요.",
        "(The above is conversation history from a previous session. Please use this context when responding to the new message below.)",
        "",
        "[새로운 메시지 - New Message]",
    ])

    return "\n".join(formatted_parts)
