"""
Context Store - Session State Management

Provides persistent storage for session context and conversation history.
Backend is selected via SESSION_STORE_BACKEND environment variable:
- "s3files" → S3 Files NFS store (ECS mode)
- "redis" or unset → Redis store with in-memory fallback (AgentCore mode)

Usage:
    from context import get_context_store, SessionContext

    # Get or create session
    store = get_context_store()
    context = store.get_session(session_id) or SessionContext(session_id=session_id)

    # Update session
    context = store.update_session(session_id, phase="generating", turn_count=5)

    # Append conversation message
    store.append_message(session_id, "user", "Create a hotel booking system")
"""

import os

from .models import SessionContext

_STORE_BACKEND = os.environ.get("SESSION_STORE_BACKEND", "redis")

if _STORE_BACKEND == "s3files":
    from .s3files_store import (
        S3FilesContextStore as ContextStore,
        get_context_store,
        get_session_context,
        save_session_context,
        update_session_context,
        get_session_history,
        append_session_message,
        clear_session,
        format_history_for_injection,
    )
else:
    from .redis_store import (
        RedisContextStore as ContextStore,
        get_context_store,
        get_session_context,
        save_session_context,
        update_session_context,
        get_session_history,
        append_session_message,
        clear_session,
        format_history_for_injection,
    )

__all__ = [
    # Models
    "SessionContext",
    # Store class
    "ContextStore",
    # Singleton access
    "get_context_store",
    # Convenience functions
    "get_session_context",
    "save_session_context",
    "update_session_context",
    "get_session_history",
    "append_session_message",
    "clear_session",
    # Session restoration
    "format_history_for_injection",
]
