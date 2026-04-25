"""
Redis-based Context Store

Provides persistent session state storage using Redis (ElastiCache).
Falls back to in-memory storage when Redis is unavailable.

Features:
- Session context persistence across MicroVM restarts
- Conversation history with automatic pruning
- Sub-agent state tracking
- TTL-based automatic cleanup
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

from .models import SessionContext, ConversationMessage

logger = logging.getLogger(__name__)

# Configuration
CONTEXT_TTL_SECONDS = 8 * 60 * 60  # 8 hours (matches AgentCore session limit)
MAX_HISTORY_MESSAGES = 60  # 30 turns (user + assistant)
REDIS_KEY_PREFIX = "aicc"

# Redis client singleton
_redis_client = None
_redis_available: Optional[bool] = None

# Fallback in-memory storage (when Redis unavailable)
_memory_sessions: Dict[str, str] = {}  # session_id -> JSON
_memory_history: Dict[str, List[str]] = {}  # session_id -> [message JSONs]
_memory_subagent: Dict[str, str] = {}  # session_id:agent_name -> JSON


def _get_redis():
    """
    Get Redis client singleton.

    Returns None if Redis is not available (falls back to in-memory).
    """
    global _redis_client, _redis_available

    # Skip if we already know Redis is unavailable
    if _redis_available is False:
        return None

    if _redis_client is None:
        redis_url = os.environ.get("REDIS_URL", "")

        if not redis_url:
            logger.info("REDIS_URL not set, using in-memory storage")
            _redis_available = False
            return None

        try:
            import redis
            _redis_client = redis.from_url(redis_url, decode_responses=True)
            # Test connection
            _redis_client.ping()
            _redis_available = True
            logger.info(f"Redis connected: {redis_url.split('@')[-1] if '@' in redis_url else redis_url}")
        except ImportError:
            logger.warning("redis package not installed, using in-memory storage")
            _redis_available = False
            return None
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}, using in-memory storage")
            _redis_available = False
            return None

    return _redis_client


class RedisContextStore:
    """Redis-based session context store with in-memory fallback."""

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}:session:{session_id}:context"

    @staticmethod
    def _history_key(session_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}:session:{session_id}:history"

    @staticmethod
    def _subagent_key(session_id: str, agent_name: str) -> str:
        return f"{REDIS_KEY_PREFIX}:session:{session_id}:subagent:{agent_name}"

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get session context."""
        redis = _get_redis()

        if redis:
            try:
                data = redis.get(self._session_key(session_id))
                if data:
                    return SessionContext.from_json(data)
            except Exception as e:
                logger.error(f"Redis get_session error: {e}")
        else:
            # Fallback to memory
            data = _memory_sessions.get(session_id)
            if data:
                return SessionContext.from_json(data)

        return None

    def save_session(self, context: SessionContext) -> None:
        """Save session context."""
        context.updated_at = datetime.utcnow().isoformat()
        json_data = context.to_json()
        redis = _get_redis()

        if redis:
            try:
                redis.setex(
                    self._session_key(context.session_id),
                    CONTEXT_TTL_SECONDS,
                    json_data
                )
            except Exception as e:
                logger.error(f"Redis save_session error: {e}")
                # Fallback to memory
                _memory_sessions[context.session_id] = json_data
        else:
            _memory_sessions[context.session_id] = json_data

    def update_session(self, session_id: str, **updates) -> SessionContext:
        """
        Partially update session context.

        Handles special merge logic for nested fields like operations and completeness.
        """
        context = self.get_session(session_id)
        if context is None:
            context = SessionContext(session_id=session_id)

        for key, value in updates.items():
            if not hasattr(context, key):
                continue

            if key == "operations" and isinstance(value, list):
                # Merge operations by operation_id
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

    def get_conversation_history(
        self,
        session_id: str,
        limit: int = MAX_HISTORY_MESSAGES
    ) -> List[Dict[str, str]]:
        """
        Get conversation history (most recent messages).

        Returns list of {"role": "...", "content": "..."} dicts.
        """
        redis = _get_redis()

        if redis:
            try:
                key = self._history_key(session_id)
                messages = redis.lrange(key, -limit, -1)
                return [json.loads(m) for m in messages]
            except Exception as e:
                logger.error(f"Redis get_history error: {e}")
                return []
        else:
            messages = _memory_history.get(session_id, [])
            return [json.loads(m) for m in messages[-limit:]]

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Append message to conversation history."""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if metadata:
            message["metadata"] = metadata

        json_data = json.dumps(message, ensure_ascii=False)
        redis = _get_redis()

        if redis:
            try:
                key = self._history_key(session_id)
                redis.rpush(key, json_data)
                redis.ltrim(key, -MAX_HISTORY_MESSAGES, -1)
                redis.expire(key, CONTEXT_TTL_SECONDS)
            except Exception as e:
                logger.error(f"Redis append_message error: {e}")
                # Fallback to memory
                if session_id not in _memory_history:
                    _memory_history[session_id] = []
                _memory_history[session_id].append(json_data)
                _memory_history[session_id] = _memory_history[session_id][-MAX_HISTORY_MESSAGES:]
        else:
            if session_id not in _memory_history:
                _memory_history[session_id] = []
            _memory_history[session_id].append(json_data)
            _memory_history[session_id] = _memory_history[session_id][-MAX_HISTORY_MESSAGES:]

    def get_subagent_state(
        self,
        session_id: str,
        agent_name: str
    ) -> Optional[Dict[str, Any]]:
        """Get sub-agent state."""
        redis = _get_redis()
        key = self._subagent_key(session_id, agent_name)

        if redis:
            try:
                data = redis.get(key)
                return json.loads(data) if data else None
            except Exception as e:
                logger.error(f"Redis get_subagent_state error: {e}")
                return None
        else:
            data = _memory_subagent.get(f"{session_id}:{agent_name}")
            return json.loads(data) if data else None

    def save_subagent_state(
        self,
        session_id: str,
        agent_name: str,
        state: Dict[str, Any]
    ) -> None:
        """Save sub-agent state."""
        json_data = json.dumps(state, ensure_ascii=False)
        redis = _get_redis()
        key = self._subagent_key(session_id, agent_name)

        if redis:
            try:
                redis.setex(key, CONTEXT_TTL_SECONDS, json_data)
            except Exception as e:
                logger.error(f"Redis save_subagent_state error: {e}")
                _memory_subagent[f"{session_id}:{agent_name}"] = json_data
        else:
            _memory_subagent[f"{session_id}:{agent_name}"] = json_data

    def delete_session(self, session_id: str) -> None:
        """Delete all session data."""
        redis = _get_redis()

        if redis:
            try:
                keys = redis.keys(f"{REDIS_KEY_PREFIX}:session:{session_id}:*")
                if keys:
                    redis.delete(*keys)
            except Exception as e:
                logger.error(f"Redis delete_session error: {e}")
        else:
            # Clean up memory storage
            _memory_sessions.pop(session_id, None)
            _memory_history.pop(session_id, None)
            # Clean up subagent states
            keys_to_delete = [k for k in _memory_subagent if k.startswith(f"{session_id}:")]
            for k in keys_to_delete:
                _memory_subagent.pop(k, None)

    def health_check(self) -> Dict[str, Any]:
        """Check store health and return status."""
        redis = _get_redis()

        if redis:
            try:
                redis.ping()
                info = redis.info("memory")
                return {
                    "status": "healthy",
                    "backend": "redis",
                    "memory_used": info.get("used_memory_human", "unknown"),
                }
            except Exception as e:
                return {
                    "status": "degraded",
                    "backend": "memory_fallback",
                    "error": str(e),
                }
        else:
            return {
                "status": "healthy",
                "backend": "memory",
                "sessions_count": len(_memory_sessions),
            }


# Singleton instance
_context_store: Optional[RedisContextStore] = None


def get_context_store() -> RedisContextStore:
    """Get context store singleton."""
    global _context_store
    if _context_store is None:
        _context_store = RedisContextStore()
    return _context_store


# Convenience functions for common operations
def get_session_context(session_id: str) -> Optional[SessionContext]:
    """Get session context by ID."""
    return get_context_store().get_session(session_id)


def save_session_context(context: SessionContext) -> None:
    """Save session context."""
    get_context_store().save_session(context)


def update_session_context(session_id: str, **updates) -> SessionContext:
    """Update session context with partial updates."""
    return get_context_store().update_session(session_id, **updates)


def get_session_history(session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> List[Dict[str, str]]:
    """Get conversation history for session."""
    return get_context_store().get_conversation_history(session_id, limit)


def append_session_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    """Append message to session history."""
    get_context_store().append_message(session_id, role, content, metadata)


def clear_session(session_id: str) -> None:
    """Clear all session data."""
    get_context_store().delete_session(session_id)


def format_history_for_injection(
    history: List[Dict[str, str]],
    max_content_length: int = 1000,
    max_messages: int = 20,
    workspace_summary: str = "",
) -> str:
    """
    Format conversation history for injection into new AgentCore session.

    When a user returns to a previously ended session, the AgentCore Runtime
    creates a new session ID. This function formats the previous conversation
    history so it can be prepended to the first message, allowing the agent
    to continue as if it were the same session.

    Args:
        history: List of {"role": "user"|"assistant", "content": "..."} dicts
        max_content_length: Maximum length per message content (truncates if longer)
        max_messages: Maximum number of messages to include
        workspace_summary: Optional structured project state restored from S3

    Returns:
        Formatted string ready to prepend to user message, or empty string if no history
    """
    if not history and not workspace_summary:
        return ""

    formatted_parts = []

    # Structured project state from S3 workspace (A5)
    if workspace_summary:
        formatted_parts.extend([
            "[프로젝트 상태 복원 — Project State Restored]",
            "---",
            workspace_summary,
            "---",
            "",
        ])

    if history:
        # Take most recent messages up to limit
        recent_history = history[-max_messages:]

        formatted_parts.extend([
            "[이전 대화 내용 - Previous Conversation Context]",
            "---"
        ])

        for msg in recent_history:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            # Role label
            if role == "user":
                role_label = "사용자 (User)"
            elif role == "assistant":
                role_label = "어시스턴트 (Assistant)"
            else:
                role_label = role

            # Truncate long content
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
        "[새로운 메시지 - New Message]"
    ])

    return "\n".join(formatted_parts)
