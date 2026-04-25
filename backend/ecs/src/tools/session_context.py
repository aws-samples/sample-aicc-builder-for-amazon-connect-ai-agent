"""
Per-session context storage using ContextVars.

Provides session isolation for concurrent WebSocket handlers that share a
single asyncio event loop. Replaces the module-level globals that previously
leaked state across users (a user's workspace/specs/streaming events would
bleed into another user's session whenever the async runtime interleaved
them at an ``await`` point).

Python 3.11+ behavior we rely on:
  * ``asyncio.create_task(...)`` captures the current context at task creation
    time, so background agent tasks see whatever ContextVars were bound at
    the call site.
  * ``asyncio.to_thread(...)`` executes via ``contextvars.copy_context()``,
    so sync ``@tool`` functions scheduled on the default executor inherit
    the calling task's ContextVars.

Anything else that spawns threads on its own (raw ``ThreadPoolExecutor``)
must call ``contextvars.copy_context().run(...)`` explicitly — none of
our first-party code does this today; Strands agent tool dispatch runs on
the asyncio event loop or via asyncio.to_thread, which are both covered.
"""

from __future__ import annotations

import contextvars
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ContextVars — scalar state that is logically "current" for one request.
# ---------------------------------------------------------------------------
current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_session_id", default=None
)

current_streaming_callback: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    "current_streaming_callback", default=None
)

current_message_index: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_message_index", default=0
)

# Consolidated callback handler — the same instance is shared across all
# sub-agents in one request (see app.py: set_research_callback, set_faq_callback,
# ... all receive the same ``callback_handler``). Nine module-level globals
# collapse to this one ContextVar.
current_callback_handler: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "current_callback_handler", default=None
)


# ---------------------------------------------------------------------------
# Per-session singletons / collections, keyed by session_id.
#
# These replace module-level ``_workspace``, ``_infrastructure_spec``,
# ``_session_flow_config``, and the global ``_operation_specs`` dict
# (which was keyed only by op_id and therefore mixed all users together).
#
# Insertion-ordered so we can LRU-evict the oldest when ``_MAX_SESSIONS``
# is exceeded.
# ---------------------------------------------------------------------------
_MAX_SESSIONS = 50

_workspaces: "OrderedDict[str, Any]" = OrderedDict()
_infrastructure_specs: "OrderedDict[str, Any]" = OrderedDict()
_session_flow_configs: "OrderedDict[str, Any]" = OrderedDict()
_operation_specs_by_session: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_fragment_registries: "OrderedDict[str, dict[str, dict]]" = OrderedDict()
_schema_registries: "OrderedDict[str, dict[str, str]]" = OrderedDict()
_openapi_fragment_registries: "OrderedDict[str, dict[str, dict]]" = OrderedDict()

_ALL_BUCKETS = (
    _workspaces,
    _infrastructure_specs,
    _session_flow_configs,
    _operation_specs_by_session,
    _fragment_registries,
    _schema_registries,
    _openapi_fragment_registries,
)


def _touch(bucket: "OrderedDict[str, Any]", sid: str) -> None:
    """Move *sid* to the MRU end of *bucket* (if present)."""
    try:
        bucket.move_to_end(sid)
    except KeyError:
        pass


def _enforce_lru() -> None:
    """Evict the oldest session if total active sessions exceeds the cap.

    Safety net against unbounded memory growth from missed cleanup calls.
    Active sessions are the union of keys across every bucket.
    """
    active = active_session_ids()
    if len(active) <= _MAX_SESSIONS:
        return
    # Oldest session = first key in the first non-empty bucket.
    oldest = None
    for bucket in _ALL_BUCKETS:
        if bucket:
            oldest = next(iter(bucket))
            break
    if oldest is None:
        return
    logger.warning(
        "[session_context] LRU eviction: sessions=%d exceeds cap=%d; evicting %s",
        len(active), _MAX_SESSIONS, oldest,
    )
    cleanup_session(oldest)


# ---------------------------------------------------------------------------
# Workspace accessors
# ---------------------------------------------------------------------------
def get_workspace_for(sid: Optional[str]) -> Any:
    if not sid:
        return None
    ws = _workspaces.get(sid)
    if ws is not None:
        _touch(_workspaces, sid)
    return ws


def set_workspace_for(sid: str, ws: Any) -> None:
    _workspaces[sid] = ws
    _touch(_workspaces, sid)
    _enforce_lru()


# ---------------------------------------------------------------------------
# Infrastructure spec accessors
# ---------------------------------------------------------------------------
def get_infrastructure_spec_for(sid: Optional[str]) -> Any:
    if not sid:
        return None
    spec = _infrastructure_specs.get(sid)
    if spec is not None:
        _touch(_infrastructure_specs, sid)
    return spec


def set_infrastructure_spec_for(sid: str, spec: Any) -> None:
    _infrastructure_specs[sid] = spec
    _touch(_infrastructure_specs, sid)
    _enforce_lru()


# ---------------------------------------------------------------------------
# Session flow config accessors
# ---------------------------------------------------------------------------
def get_session_flow_config_for(sid: Optional[str]) -> Any:
    if not sid:
        return None
    cfg = _session_flow_configs.get(sid)
    if cfg is not None:
        _touch(_session_flow_configs, sid)
    return cfg


def set_session_flow_config_for(sid: str, cfg: Any) -> None:
    _session_flow_configs[sid] = cfg
    _touch(_session_flow_configs, sid)
    _enforce_lru()


# ---------------------------------------------------------------------------
# Operation specs bucket (per-session dict of op_id → OperationSpec).
#
# The previous implementation used a single process-wide ``_operation_specs``
# dict keyed only by op_id, which meant every user's specs were stored in
# the same place — the smoking-gun leak.
# ---------------------------------------------------------------------------
def operation_specs_bucket(sid: Optional[str]) -> "dict[str, Any]":
    """Return the per-session op_id → spec dict (created on demand).

    ``sid=None`` (or unset ContextVar) falls back to a shared ``"__anon__"``
    bucket. This keeps unit tests and one-off invocations working without
    crashes; production code paths go through the WebSocket dispatch which
    always binds ``current_session_id``.
    """
    key = sid or "__anon__"
    bucket = _operation_specs_by_session.get(key)
    if bucket is None:
        bucket = {}
        _operation_specs_by_session[key] = bucket
        _enforce_lru()
    else:
        _touch(_operation_specs_by_session, key)
    return bucket


# ---------------------------------------------------------------------------
# Per-session fragment/schema registries for the generator sub-agents.
#
# The inner dict is still keyed by project_name (existing contract); the
# outer dict isolates by session_id so users with identical project names
# don't collide.
# ---------------------------------------------------------------------------
def fragment_registry_for(sid: Optional[str]) -> "dict[str, dict]":
    key = sid or "__anon__"
    reg = _fragment_registries.get(key)
    if reg is None:
        reg = {}
        _fragment_registries[key] = reg
        _enforce_lru()
    else:
        _touch(_fragment_registries, key)
    return reg


def schema_registry_for(sid: Optional[str]) -> "dict[str, str]":
    key = sid or "__anon__"
    reg = _schema_registries.get(key)
    if reg is None:
        reg = {}
        _schema_registries[key] = reg
        _enforce_lru()
    else:
        _touch(_schema_registries, key)
    return reg


def openapi_fragment_registry_for(sid: Optional[str]) -> "dict[str, dict]":
    key = sid or "__anon__"
    reg = _openapi_fragment_registries.get(key)
    if reg is None:
        reg = {}
        _openapi_fragment_registries[key] = reg
        _enforce_lru()
    else:
        _touch(_openapi_fragment_registries, key)
    return reg


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
@asynccontextmanager
async def session_scope(
    session_id: str,
    *,
    callback_handler: Any = None,
    streaming_callback: Optional[Callable] = None,
    message_index: int = 0,
):
    """Bind per-request ContextVars for the duration of the ``async with`` body.

    On exit, all tokens are reset — but dict-keyed state (workspaces, specs,
    …) is NOT automatically removed. Call :func:`cleanup_session` when the
    session has truly ended (e.g. on ``createNewSession``).
    """
    tok_sid = current_session_id.set(session_id)
    tok_cb = current_callback_handler.set(callback_handler)
    tok_sc = current_streaming_callback.set(streaming_callback)
    tok_mi = current_message_index.set(message_index)
    try:
        yield
    finally:
        current_message_index.reset(tok_mi)
        current_streaming_callback.reset(tok_sc)
        current_callback_handler.reset(tok_cb)
        current_session_id.reset(tok_sid)


def cleanup_session(session_id: str) -> None:
    """Drop every per-session entry this module holds for *session_id*."""
    if not session_id:
        return
    for bucket in _ALL_BUCKETS:
        bucket.pop(session_id, None)
    logger.debug("[session_context] cleanup_session(%s)", session_id)


def active_session_ids() -> list[str]:
    """Return the union of session_ids with any resident state."""
    active: set[str] = set()
    for bucket in _ALL_BUCKETS:
        active.update(bucket.keys())
    active.discard("__anon__")
    return sorted(active)
