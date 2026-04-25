"""
Streaming Callback Registry for Real-time Asset Preview

This module provides a global callback mechanism that allows tool functions
to stream generated content to the frontend in real-time.

When is_complete=True, the asset is also saved to S3 for persistent storage
and later download.

Usage in tools:
    from .streaming_callback import stream_asset, complete_asset

    def generate_lambda_function(...):
        # Stream content as it's generated
        stream_asset("lambda", "handler.py", partial_code, operation_id="createReservation")

        # ... generate more code ...
        # Mark as complete - this also saves to S3
        stream_asset("lambda", "handler.py", full_code, operation_id="createReservation", is_complete=True)

Usage in agent.py:
    from src.tools.streaming_callback import set_streaming_callback, clear_streaming_callback, set_session_id

    # Set session ID for S3 storage
    set_session_id(session_id)

    # Before invoking agent
    set_streaming_callback(callback_handler.stream_asset_preview)

    # After agent completes
    clear_streaming_callback()
"""

from typing import Callable, Optional
import logging

from .session_context import (
    current_streaming_callback,
    current_session_id,
    current_message_index,
)

logger = logging.getLogger(__name__)


# NOTE — session isolation:
# Callback, session_id, and message_index previously lived in module-level
# globals, which leaked across concurrent async users sharing a single
# uvicorn worker. They now live in ContextVars (see ``session_context``),
# so each async task/request sees only its own values. Public API
# signatures are preserved; bodies are thin wrappers over ContextVar I/O.


def set_streaming_callback(callback: Callable[[str, str, Optional[str], Optional[str], bool], None]) -> None:
    """Set the streaming callback for the current request."""
    current_streaming_callback.set(callback)
    import sys
    caller_info = ""
    if hasattr(sys, '_getframe'):
        try:
            frame = sys._getframe(1)
            caller_info = f" (called from {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name})"
        except Exception:
            pass
    logger.info(f"[CALLBACK] Streaming callback SET: {callback}{caller_info}")


def clear_streaming_callback() -> None:
    """Clear the streaming callback for the current request."""
    prev_callback = current_streaming_callback.get()
    current_streaming_callback.set(None)
    import sys
    caller_info = ""
    if hasattr(sys, '_getframe'):
        try:
            frame = sys._getframe(1)
            caller_info = f" (called from {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name})"
        except Exception:
            pass
    logger.info(f"[CALLBACK] Streaming callback CLEARED (was: {prev_callback}){caller_info}")


def get_streaming_callback() -> Optional[Callable]:
    """Get the current streaming callback."""
    return current_streaming_callback.get()


def set_session_id(session_id: str) -> None:
    """Set the current session ID for S3 storage."""
    current_session_id.set(session_id)
    logger.debug(f"Session ID set: {session_id}")


def get_session_id() -> Optional[str]:
    """Get the current session ID."""
    return current_session_id.get()


def clear_session_id() -> None:
    """Clear the current session ID."""
    current_session_id.set(None)
    logger.debug("Session ID cleared")


def set_message_index(index: int) -> None:
    """Set the current message index for asset placement tracking."""
    current_message_index.set(index)
    logger.debug(f"Message index set: {index}")


def get_message_index() -> int:
    """Get the current message index."""
    return current_message_index.get()


def stream_asset(
    asset_type: str,
    file_name: str,
    content: str,
    operation_id: Optional[str] = None,
    is_complete: bool = False,
    download_data: Optional[str] = None,
    force_full: bool = False,
    s3_key: Optional[str] = None
) -> Optional[str]:
    """
    Stream asset content to the frontend and optionally save to S3.

    This function is called by generator tools to send real-time updates
    as content is being generated. When is_complete=True, the asset is
    also saved to S3 for persistent storage.

    Args:
        asset_type: Type of asset ("lambda", "openapi", "prompt", "contact_flow", "cdk", "operations")
        file_name: Name of the file being generated (e.g., "handler.py")
        content: Current content (can be partial during streaming)
        operation_id: Optional operation identifier for grouping
        is_complete: Set to True when generation is finished
        download_data: Optional base64-encoded binary data (e.g., ZIP file for packages)
        force_full: If True, clear cache before streaming to force full content send
                   (use when content structure has changed, e.g., after merging chunks)
        s3_key: Optional pre-existing S3 key (skips internal S3 save, e.g., for binary assets already saved)

    Returns:
        S3 key if saved to S3 (when is_complete=True), None otherwise
    """
    # Clear cache if force_full is requested (e.g., after merging chunked CFN phases)
    if force_full:
        clear_asset_preview_cache(asset_type, file_name, operation_id)

    # Skip internal S3 save if caller already saved and provided the key
    _sid = current_session_id.get()
    if s3_key:
        pass
    elif is_complete and content and _sid:
        try:
            from .s3_asset_storage import save_asset_to_s3
            s3_key = save_asset_to_s3(
                session_id=_sid,
                asset_type=asset_type,
                file_name=file_name,
                content=content,
                operation_id=operation_id
            )
            if s3_key:
                logger.info(f"[STREAM_ASSET] Saved to S3: {s3_key}")
        except ImportError:
            logger.warning("[STREAM_ASSET] s3_asset_storage not available, skipping S3 save")
        except Exception as e:
            logger.error(f"[STREAM_ASSET] Failed to save to S3: {e}")

    # Emit workspace_update so FileExplorer auto-refreshes after asset save
    if is_complete and s3_key and asset_type != "workspace_update":
        try:
            callback = get_streaming_callback()
            if callback:
                import json as _json
                callback(
                    asset_type="workspace_update",
                    content=_json.dumps({"action": "asset_saved", "path": s3_key, "size": len(content) if content else 0}),
                    operation_id=None,
                    file_name=file_name or "",
                    is_complete=True,
                    s3_key=None,
                    message_index=None,
                    download_data=None,
                )
        except Exception as e:
            logger.debug(f"[STREAM_ASSET] workspace_update emit failed (non-critical): {e}")

    # Get current message index for asset placement in session restore
    message_index = get_message_index()

    # Stream to frontend via callback
    callback = get_streaming_callback()
    if callback:
        try:
            callback(
                asset_type=asset_type,
                content=content,
                operation_id=operation_id,
                file_name=file_name,
                is_complete=is_complete,
                s3_key=s3_key,  # Include S3 key for frontend
                message_index=message_index,  # Include message index for ordering
                download_data=download_data  # Include base64 data for packages
            )
            # Always log when streaming asset (for debugging)
            logger.info(f"[STREAM_ASSET] Streamed: {asset_type}/{file_name}, op={operation_id}, complete={is_complete}, len={len(content) if content else 0}, s3_key={s3_key}, msg_idx={message_index}, has_download_data={download_data is not None}")
        except TypeError:
            # Callback might not support new parameters yet (backwards compatibility)
            try:
                callback(
                    asset_type=asset_type,
                    content=content,
                    operation_id=operation_id,
                    file_name=file_name,
                    is_complete=is_complete,
                    s3_key=s3_key,
                    message_index=message_index
                )
                logger.info(f"[STREAM_ASSET] Streamed (no download_data): {asset_type}/{file_name}")
            except TypeError:
                # Fallback for oldest callback signature
                try:
                    callback(
                        asset_type=asset_type,
                        content=content,
                        operation_id=operation_id,
                        file_name=file_name,
                        is_complete=is_complete
                    )
                    logger.info(f"[STREAM_ASSET] Streamed (minimal): {asset_type}/{file_name}")
                except Exception as e:
                    logger.warning(f"Streaming callback error: {e}")
        except Exception as e:
            # Don't let callback errors break tool execution
            logger.warning(f"Streaming callback error: {e}")
    else:
        # Enhanced logging when callback is missing (for debugging regeneration issue)
        import sys
        caller_info = ""
        if hasattr(sys, '_getframe'):
            try:
                frame = sys._getframe(1)
                caller_info = f" | caller: {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
            except Exception:
                pass
        module_info = f" | module: {__name__} | session_id: {current_session_id.get()}"
        logger.warning(f"[STREAM_ASSET] NO CALLBACK! Skipping: {asset_type}/{file_name}{module_info}{caller_info}")

    return s3_key


def clear_asset_preview_cache(asset_type: str, file_name: str, operation_id: Optional[str] = None) -> None:
    """
    Clear cached asset preview to force full content send on next stream.

    This is necessary when the content structure changes (e.g., after merging
    chunked CloudFormation phases) and the cached previous_content_length
    would cause incorrect delta calculations.

    Args:
        asset_type: Type of asset ("lambda", "openapi", "cloudformation", etc.)
        file_name: Name of the file (e.g., "infrastructure.yaml")
        operation_id: Optional operation identifier
    """
    callback = get_streaming_callback()
    if callback and hasattr(callback, '__self__'):
        handler = callback.__self__
        if hasattr(handler, 'current_asset_previews'):
            # Build the key in the same format as logging_utils.py
            if file_name and operation_id:
                key = f"{asset_type}-{operation_id}-{file_name}"
            elif file_name:
                key = f"{asset_type}-{file_name}"
            elif operation_id:
                key = f"{asset_type}-{operation_id}"
            else:
                key = asset_type

            if key in handler.current_asset_previews:
                del handler.current_asset_previews[key]
                logger.info(f"[CLEAR_CACHE] Cleared asset preview cache for key: {key}")
            else:
                logger.debug(f"[CLEAR_CACHE] Key not found in cache: {key}")
        else:
            logger.debug("[CLEAR_CACHE] Handler has no current_asset_previews")
    else:
        logger.debug("[CLEAR_CACHE] No callback or callback has no __self__")


def complete_asset(asset_type: str, operation_id: Optional[str] = None) -> None:
    """
    Mark an asset as complete.

    This signals to the frontend that no more updates are coming for this asset.

    Args:
        asset_type: Type of asset
        operation_id: Optional operation identifier
    """
    callback = get_streaming_callback()
    if callback:
        try:
            # Send a completion signal
            callback(
                asset_type=asset_type,
                content="",  # Empty content signals completion check
                operation_id=operation_id,
                file_name=None,
                is_complete=True
            )
            logger.debug(f"Completed asset: {asset_type}, operation_id={operation_id}")
        except Exception as e:
            logger.warning(f"Complete asset callback error: {e}")
