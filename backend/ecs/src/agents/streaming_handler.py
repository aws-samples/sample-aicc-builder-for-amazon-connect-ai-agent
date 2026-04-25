"""
Tool Input Streaming Handler

DEPRECATED: This module is no longer used as of the AsyncGenerator streaming migration.
The new pattern uses `@tool async def ... -> AsyncIterator` with `yield` for real-time
streaming events directly in Strands SDK v1.22.0+.

See lambda_generator/agent.py for the new AsyncGenerator pattern example.

---

This module provided a callback handler for Sub-Agents that streams
tool input content (especially code) in real-time as it's being generated.

KEY INSIGHT:
When LLM generates code, it passes the code as a tool argument (e.g., code_content).
Strands SDK progressively fills in the `input` dict in `current_tool_use` events.
We intercept these partial inputs and stream the code content to the frontend.

Architecture:
1. LLM starts generating tool call with code_content argument
2. Strands emits current_tool_use events with progressively longer input.code_content
3. This handler detects the growing code_content and streams deltas to frontend
4. When tool actually executes, the code is already displayed in frontend

Usage in Sub-Agent (DEPRECATED):
    from ..streaming_handler import ToolInputStreamingHandler

    streaming_handler = ToolInputStreamingHandler(
        parent_handler=get_callback_handler(),
        operation_id="my_operation",
        asset_type="lambda",
        content_field="code_content"
    )

    agent = Agent(..., callback_handler=streaming_handler)
"""

import os
import re
import json
import time
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


class ToolInputStreamingHandler:
    """
    Callback handler for Sub-Agent that streams tool input content in real-time.

    KEY INSIGHT:
    When LLM calls tools like save_generated_code(code_content="..."), the code
    is passed as a tool argument. Strands SDK progressively fills in the input
    dict via repeated current_tool_use events as tokens are generated.

    This handler intercepts these partial inputs and streams the code_content
    (or other specified field) to the frontend in real-time.

    Flow:
    1. LLM starts generating tool call
    2. Strands emits current_tool_use with partial input (code_content grows)
    3. We detect the delta and stream it to frontend
    4. When complete, we send is_complete=True
    """

    # Minimum characters to accumulate before sending an update
    STREAM_THRESHOLD = 50

    def __init__(
        self,
        parent_handler,
        operation_id: str,
        asset_type: str = "lambda",
        content_fields: Optional[List[str]] = None,
        tool_names: Optional[List[str]] = None
    ):
        """
        Initialize the streaming handler.

        Args:
            parent_handler: The StrandsCallbackHandler from Orchestrator
            operation_id: The operation ID for this generation
            asset_type: Type of asset being generated (lambda, openapi, prompt, contact_flow)
            content_fields: List of field names in tool input that contain streamable content
                           Default: ["code_content", "content", "yaml_content", "json_content"]
            tool_names: List of tool names to intercept for streaming
                       Default: ["save_generated_code", "save_generated_code_tracked"]
        """
        self.parent_handler = parent_handler
        self.operation_id = operation_id
        self.asset_type = asset_type

        # Fields to stream from tool input
        self.content_fields = content_fields or [
            "code_content",
            "content",
            "yaml_content",
            "json_content",
            "prompt_content",
            "flow_json"
        ]

        # Tools to intercept - include all variations used by Sub-Agents
        self.tool_names = tool_names or [
            # Lambda generator
            "save_generated_code",
            "save_generated_code_tracked",
            # OpenAPI generator
            "save_openapi_spec",
            "generate_openapi_spec",
            # Prompt generator
            "save_ai_prompt",
            "generate_ai_prompt",
            # Contact flow generator
            "save_contact_flow",
            "generate_contact_flow",
        ]

        # Tracking state per tool use ID
        self.tool_streams: Dict[str, Dict[str, Any]] = {}
        # Format: {tool_use_id: {"field": field_name, "last_length": int, "file_name": str}}

        # Accumulated full response (from data events)
        self.full_response = ""

        # Completed files for summary
        self.completed_files: List[str] = []

    def _get_file_name(self, tool_input) -> str:
        """Extract or generate file name from tool input (handles both str and dict)."""
        import re

        # Case 1: Dict format (after completion)
        if isinstance(tool_input, dict):
            for field in ["file_name", "fileName", "filename"]:
                if field in tool_input and tool_input[field]:
                    return tool_input[field]

        # Case 2: String format (during streaming)
        elif isinstance(tool_input, str):
            for field in ["file_name", "fileName", "filename"]:
                # Pattern: "file_name": "handler.py"
                pattern = f'"{field}"\\s*:\\s*"([^"]+)"'
                match = re.search(pattern, tool_input)
                if match:
                    return match.group(1)

        # Default based on asset type
        defaults = {
            "lambda": "handler.py",
            "openapi": "openapi.yaml",
            "prompt": "ai_prompt.yaml",
            "contact_flow": "contact_flow.json",
        }
        return defaults.get(self.asset_type, "generated_file.txt")

    def _extract_content_from_input(self, tool_input) -> tuple:
        """
        Extract content from tool input, handling both string (streaming) and dict (complete) formats.

        During streaming: input is a JSON string fragment like '{"code_content": "import json\\n...'
        After complete: input is a parsed dict like {"code_content": "import json\n..."}

        Returns:
            tuple: (content_field, content) or (None, None) if not found
        """
        # Case 1: Already parsed dict (after completion)
        if isinstance(tool_input, dict):
            for field in self.content_fields:
                if field in tool_input and tool_input[field]:
                    return field, tool_input[field]
            return None, None

        # Case 2: JSON string fragment (during streaming)
        if isinstance(tool_input, str):
            # Try to extract content from partial JSON string
            # Example: '{"file_name": "handler.py", "code_content": "import json\nimport boto3...'
            for field in self.content_fields:
                # Look for the field in the string
                pattern = f'"{field}"\\s*:\\s*"'
                import re
                match = re.search(pattern, tool_input)
                if match:
                    # Extract content after the field key
                    start_idx = match.end()
                    # Find the content - it's everything after the opening quote
                    # Handle escaped characters in the JSON string
                    content = tool_input[start_idx:]

                    # Try to unescape the content (handle \n, \", etc.)
                    # But don't require a closing quote since it's still streaming
                    try:
                        # Add closing quote and try to parse just this field
                        # This handles escape sequences properly
                        test_json = '{"v": "' + content
                        # Find a safe truncation point (avoid breaking escape sequences)
                        # Just use the raw content with basic unescaping
                        unescaped = content.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
                        return field, unescaped
                    except:
                        return field, content

            return None, None

        return None, None

    def _stream_tool_content(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input,  # Can be dict or str during streaming
        is_complete: bool = False
    ):
        """
        Stream content from tool input to frontend.

        Args:
            tool_use_id: Unique ID for this tool invocation
            tool_name: Name of the tool being called
            tool_input: Current state of tool input (str during streaming, dict when complete)
            is_complete: Whether tool call is complete
        """
        if tool_name not in self.tool_names:
            return

        # Extract content, handling both string and dict formats
        content_field, content = self._extract_content_from_input(tool_input)

        if not content:
            return

        # Get or create tracking state for this tool use
        if tool_use_id not in self.tool_streams:
            self.tool_streams[tool_use_id] = {
                "field": content_field,
                "last_length": 0,
                "file_name": self._get_file_name(tool_input),
                "streamed": False
            }

        state = self.tool_streams[tool_use_id]
        file_name = state["file_name"]

        # Update file name if it changed
        new_file_name = self._get_file_name(tool_input)
        if new_file_name != file_name:
            state["file_name"] = new_file_name
            file_name = new_file_name

        # Calculate delta
        current_length = len(content)
        last_length = state["last_length"]
        delta = current_length - last_length

        # Stream if we have enough new content or if complete
        if delta >= self.STREAM_THRESHOLD or is_complete:
            if self.parent_handler and hasattr(self.parent_handler, 'stream_asset_preview'):
                self.parent_handler.stream_asset_preview(
                    asset_type=self.asset_type,
                    content=content,
                    operation_id=self.operation_id,
                    file_name=file_name,
                    is_complete=is_complete
                )
                state["last_length"] = current_length
                state["streamed"] = True

                logger.info(
                    f"[TOOL_STREAM] Streamed {current_length} chars (+{delta}), "
                    f"file={file_name}, complete={is_complete}"
                )

        # Track completed files
        if is_complete and file_name not in self.completed_files:
            self.completed_files.append(file_name)

    def __call__(self, **kwargs):
        """
        Handle callback events from Sub-Agent's internal LLM.

        Intercepts current_tool_use events to stream tool input content.
        """
        # Log callback invocation for debugging
        keys = list(kwargs.keys())
        if "current_tool_use" in keys or "tool_result" in keys:
            logger.info(f"[TOOL_STREAM_DEBUG] Callback with keys: {keys}")

        # Handle streaming text data (for non-tool responses)
        if "data" in kwargs:
            chunk = kwargs["data"]
            self.full_response += chunk

        # Handle progressive tool input streaming
        if "current_tool_use" in kwargs:
            tool_info = kwargs["current_tool_use"]
            tool_name = tool_info.get("name", "")
            tool_use_id = tool_info.get("toolUseId", "unknown")
            tool_input = tool_info.get("input", "")  # Can be str during streaming

            if tool_name in self.tool_names and tool_input:
                # Log input type and length for debugging
                input_type = type(tool_input).__name__
                input_len = len(tool_input) if isinstance(tool_input, str) else len(str(tool_input))
                logger.debug(
                    f"[TOOL_STREAM] Progressive input for {tool_name}, "
                    f"input_type={input_type}, input_len={input_len}"
                )
                self._stream_tool_content(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    is_complete=False
                )

        # Handle tool completion - ensure final content is sent
        if "tool_result" in kwargs:
            tool_result = kwargs["tool_result"]
            tool_use_id = tool_result.get("toolUseId", "unknown")

            # Mark as complete if we were tracking this tool
            if tool_use_id in self.tool_streams:
                state = self.tool_streams[tool_use_id]
                # Send completion event if we streamed content
                if state["streamed"] and self.parent_handler:
                    # The content was already sent, just need to ensure is_complete=True
                    # was sent. If not, send a final update.
                    logger.info(f"[TOOL_STREAM] Tool {tool_use_id} completed, file={state['file_name']}")

        # Forward all events to parent handler
        if self.parent_handler:
            try:
                self.parent_handler(**kwargs)
            except Exception as e:
                logger.warning(f"[TOOL_STREAM] Parent handler error: {e}")

    def get_full_response(self) -> str:
        """Get the accumulated full response."""
        return self.full_response

    def get_completed_files(self) -> List[str]:
        """Get list of completed file names."""
        return self.completed_files


# Alias for backwards compatibility
CodeBlockStreamingHandler = ToolInputStreamingHandler


def _safe_serialize(obj: Any, max_length: int = 1000) -> Any:
    """Safely serialize an object for logging, truncating if needed."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        result = obj
    elif isinstance(obj, (list, tuple)):
        result = [_safe_serialize(item, max_length // 2) for item in obj[:10]]
    elif isinstance(obj, dict):
        result = {k: _safe_serialize(v, max_length // 2) for k, v in list(obj.items())[:20]}
    else:
        result = str(obj)
    if isinstance(result, str) and len(result) > max_length:
        return result[:max_length] + f"... [truncated, total {len(str(obj))} chars]"
    return result


class _NullLogger:
    """Minimal logger stand-in when no AgentLogger is provided."""

    def info(self, event, **kwargs):
        logger.info(f"[StrandsCallback] {event} {kwargs}" if kwargs else f"[StrandsCallback] {event}")

    def debug(self, event, **kwargs):
        logger.debug(f"[StrandsCallback] {event} {kwargs}" if kwargs else f"[StrandsCallback] {event}")

    def warning(self, event, **kwargs):
        logger.warning(f"[StrandsCallback] {event} {kwargs}" if kwargs else f"[StrandsCallback] {event}")

    def error(self, event, **kwargs):
        logger.error(f"[StrandsCallback] {event} {kwargs}" if kwargs else f"[StrandsCallback] {event}")

    def log_stream_chunk(self, chunk_len, total_len):
        pass

    def log_tool_call_start(self, tool_name, tool_input):
        logger.info(f"[StrandsCallback] tool_start: {tool_name}")
        return 0

    def log_tool_call_end(self, index, result=None, error=None, status="completed"):
        logger.info(f"[StrandsCallback] tool_end: status={status}")

    def log_agent_thinking(self, content):
        pass


class StrandsCallbackHandler:
    """
    Callback handler for Strands Agent that sends events to WebSocket.

    Ported from agentcore/logging_utils.py for use in the ECS backend.
    Accepts an optional agent_logger (falls back to standard logging if None).

    WebSocket events sent:
    - {"type": "tool_start", "tool": "...", "input": {...}}
    - {"type": "tool_end", "tool": "...", "result": {...}, "status": "completed|error"}
    - {"type": "thinking", "content": "..."}
    - {"type": "asset_preview", "assetPreview": {...}}
    - {"type": "download_ready", "downloadUrl": "...", "expiresAt": "..."}
    """

    TOOL_TO_ASSET_TYPE = {
        "generate_lambda_function": "lambda",
        "generate_ai_prompt": "prompt",
        "generate_openapi_spec": "openapi",
        "generate_contact_flow": "contact_flow",
        "generate_cdk_infrastructure": "cdk",
        "save_operation_spec": "operations",
        "package_and_upload_assets": "download",
    }

    EXT_TO_LANGUAGE = {
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".txt": "text",
    }

    STREAM_CHUNK_ENABLED = False
    MAX_CONTENT_SIZE = 28000

    def __init__(self, agent_logger, websocket=None):
        self.agent_logger = agent_logger or _NullLogger()
        self.websocket = websocket
        self.full_response = ""
        self.current_tool_index: Optional[int] = None
        self.current_tool_name: Optional[str] = None
        self.current_tool_use_id: Optional[str] = None
        self.current_tool_input: Dict[str, Any] = {}
        self.pending_tools: Dict[str, Dict[str, Any]] = {}  # toolUseId → {name, input, index}
        self.stream_buffer = ""
        self.pending_ws_events: List[Dict[str, Any]] = []
        self.current_asset_previews: Dict[str, Dict[str, Any]] = {}
        self._event_loop = None
        self._is_disconnected = False
        # When True, tool_start/tool_end/thinking events are NOT sent via WebSocket.
        # The main stream_async loop handles these directly for immediate delivery.
        # Internal tracking (pending_tools, asset_preview) is always active.
        self.suppress_tool_ws_events = False

    def set_event_loop(self, loop):
        """Set the asyncio event loop reference for cross-thread WebSocket sending."""
        self._event_loop = loop

    def _mark_disconnected(self):
        if not self._is_disconnected:
            self._is_disconnected = True
            self.agent_logger.warning("ws_marked_disconnected")

    def is_disconnected(self) -> bool:
        return self._is_disconnected

    def reset_connection_state(self):
        self._is_disconnected = False

    def add_ws_event(self, event: Dict[str, Any]):
        """Add a WebSocket event and attempt immediate delivery.

        Events are always appended to pending_ws_events first.
        Immediate delivery is attempted via the event loop; on success the
        event is removed from pending so heartbeat_loop won't re-send it.
        On failure the event stays in pending for heartbeat_loop to pick up.
        """
        if self._is_disconnected:
            return
        self.pending_ws_events.append(event)
        if self._event_loop and self.websocket:
            try:
                import asyncio
                future = asyncio.run_coroutine_threadsafe(
                    self._send_event_immediately(event),
                    self._event_loop,
                )
                # Register a callback to remove from pending only after successful send
                def _on_sent(fut, evt=event):
                    try:
                        if fut.result() and evt in self.pending_ws_events:
                            self.pending_ws_events.remove(evt)
                    except Exception:
                        pass
                future.add_done_callback(_on_sent)
            except Exception:
                pass

    async def _send_event_immediately(self, event: Dict[str, Any]) -> bool:
        if self._is_disconnected or not self.websocket:
            self._mark_disconnected()
            return False
        try:
            if hasattr(self.websocket, "client_state"):
                state_name = getattr(self.websocket.client_state, "name", None)
                if state_name and state_name != "CONNECTED":
                    self._mark_disconnected()
                    return False
            await self.websocket.send_json(event)
            return True
        except Exception:
            self._mark_disconnected()
            return False

    def setup_streaming_callback(self):
        """Set up the streaming callback integration for tools."""
        try:
            import sys

            modules_to_try = [
                "src.tools.streaming_callback",
                "tools.streaming_callback",
                "streaming_callback",
            ]
            callback_set_count = 0
            for module_name in modules_to_try:
                try:
                    module = __import__(module_name, fromlist=["set_streaming_callback"])
                    if hasattr(module, "set_streaming_callback"):
                        module.set_streaming_callback(self.stream_asset_preview)
                        callback_set_count += 1
                except ImportError:
                    pass

            for key in list(sys.modules.keys()):
                if "streaming_callback" in key and key not in modules_to_try:
                    module = sys.modules[key]
                    if hasattr(module, "set_streaming_callback"):
                        module.set_streaming_callback(self.stream_asset_preview)
                        callback_set_count += 1

            self.agent_logger.info("streaming_callback_setup", status="success", modules_set=callback_set_count)
        except Exception as e:
            self.agent_logger.error("streaming_callback_setup_failed", error=str(e))

    def cleanup_streaming_callback(self):
        """Clean up the streaming callback after agent execution.

        When suppress_tool_ws_events is True, the main loop handles tool_end.
        Only log internally; don't emit tool_end WebSocket events.
        """
        # Close all pending tools that never received a tool_result
        for tool_use_id, tool_info in list(self.pending_tools.items()):
            self.agent_logger.log_tool_call_end(tool_info["index"], status="completed")
            if not self.suppress_tool_ws_events:
                self.add_ws_event({
                    "type": "tool_end",
                    "tool": tool_info["name"],
                    "toolUseId": tool_use_id,
                    "input": _safe_serialize(tool_info.get("input", {}), max_length=2000),
                    "result": "(completed - cleanup)",
                    "status": "completed",
                })
        self.pending_tools.clear()

        # Fallback: close current_tool if still tracked (shouldn't happen with pending_tools)
        if self.current_tool_name and self.current_tool_index is not None:
            self.agent_logger.log_tool_call_end(self.current_tool_index, status="completed")
            if not self.suppress_tool_ws_events:
                self.add_ws_event({
                    "type": "tool_end",
                    "tool": self.current_tool_name,
                    "toolUseId": self.current_tool_use_id,
                    "input": _safe_serialize(self.current_tool_input, max_length=2000),
                    "result": "(completed - cleanup)",
                    "status": "completed",
                })
            self.current_tool_index = None
            self.current_tool_name = None
            self.current_tool_use_id = None
            self.current_tool_input = {}

        try:
            import sys
            for module_name in ["src.tools.streaming_callback", "tools.streaming_callback", "streaming_callback"]:
                try:
                    module = __import__(module_name, fromlist=["clear_streaming_callback"])
                    if hasattr(module, "clear_streaming_callback"):
                        module.clear_streaming_callback()
                except ImportError:
                    pass
            for key in list(sys.modules.keys()):
                if "streaming_callback" in key:
                    module = sys.modules[key]
                    if hasattr(module, "clear_streaming_callback"):
                        module.clear_streaming_callback()
        except Exception:
            pass

    def stream_asset_preview(
        self,
        asset_type: str,
        content: str,
        operation_id: str = None,
        file_name: str = None,
        is_complete: bool = False,
        s3_key: str = None,
        message_index: int = None,
        download_data: str = None,
    ):
        """Stream an asset preview to the frontend in real-time."""
        ext = os.path.splitext(file_name or "")[1].lower() if file_name else ""
        language = self.EXT_TO_LANGUAGE.get(ext, "text")
        if language == "text":
            type_to_lang = {
                "lambda": "python", "openapi": "yaml", "prompt": "markdown",
                "contact_flow": "json", "cdk": "typescript", "operations": "json",
                "validation": "json", "company": "markdown",
            }
            language = type_to_lang.get(asset_type, "text")

        if file_name and operation_id:
            key = f"{asset_type}-{operation_id}-{file_name}"
        elif file_name:
            key = f"{asset_type}-{file_name}"
        elif operation_id:
            key = f"{asset_type}-{operation_id}"
        else:
            key = asset_type

        previous_content_length = 0
        if key in self.current_asset_previews:
            previous_content_length = len(self.current_asset_previews[key].get("content", ""))

        is_delta = previous_content_length > 0
        content_to_send = content[previous_content_length:] if is_delta else content
        total_length = len(content) if content else 0

        if is_delta and not content_to_send and not is_complete:
            return

        if content_to_send and len(content_to_send) > self.MAX_CONTENT_SIZE:
            sent = 0
            while sent < len(content_to_send):
                chunk_end = min(sent + self.MAX_CONTENT_SIZE, len(content_to_send))
                is_last = chunk_end >= len(content_to_send)
                self.add_ws_event({
                    "type": "asset_preview",
                    "assetPreview": {
                        "assetType": asset_type, "operationId": operation_id,
                        "fileName": file_name, "content": content_to_send[sent:chunk_end],
                        "isComplete": is_complete if is_last else False,
                        "language": language, "s3Key": s3_key if is_last else None,
                        "createdAt": int(time.time() * 1000),
                        "messageIndex": message_index, "isDelta": True,
                        "totalLength": total_length,
                    },
                })
                sent = chunk_end
            self.current_asset_previews[key] = {
                "assetType": asset_type, "operationId": operation_id,
                "fileName": file_name, "content": content,
                "isComplete": is_complete, "language": language,
            }
            return

        asset_preview = {
            "assetType": asset_type, "operationId": operation_id,
            "fileName": file_name, "content": content_to_send,
            "isComplete": is_complete, "language": language,
            "s3Key": s3_key,
            "createdAt": int(time.time() * 1000),
            "messageIndex": message_index,
            "isDelta": is_delta, "totalLength": total_length,
        }
        self.add_ws_event({"type": "asset_preview", "assetPreview": asset_preview})
        self.current_asset_previews[key] = {**asset_preview, "content": content}

    def complete_asset_preview(self, asset_type: str, operation_id: str = None):
        """Mark all asset previews of this type/operation as complete."""
        matching_keys = []
        for key in self.current_asset_previews:
            prefix = f"{asset_type}-{operation_id}" if operation_id else asset_type
            if key == prefix or key.startswith(f"{prefix}-"):
                matching_keys.append(key)
        for key in matching_keys:
            preview = self.current_asset_previews[key]
            if not preview.get("isComplete"):
                preview["isComplete"] = True
                self.pending_ws_events.append({"type": "asset_preview", "assetPreview": preview})

    def __call__(self, **kwargs):
        """Handle callback events from Strands Agent."""
        if "data" in kwargs:
            chunk = kwargs["data"]
            self.full_response += chunk
            self.stream_buffer += chunk

        if "current_tool_use" in kwargs:
            tool_info = kwargs["current_tool_use"]
            tool_name = tool_info.get("name")
            tool_use_id = tool_info.get("toolUseId")
            tool_input = tool_info.get("input", {})
            if isinstance(tool_input, dict) and tool_input:
                self.current_tool_input = tool_input

            # Detect new tool: by toolUseId if available, fallback to name change
            is_new_tool = False
            if tool_use_id and tool_use_id != self.current_tool_use_id:
                is_new_tool = True
            elif not tool_use_id and tool_name and tool_name != self.current_tool_name:
                is_new_tool = True

            if tool_name and is_new_tool:
                # Do NOT force-close the previous tool — let tool_result handle it.
                # Just register the previous tool in pending_tools so tool_result can find it.
                if self.current_tool_name and self.current_tool_index is not None:
                    prev_id = self.current_tool_use_id or f"_fallback_{self.current_tool_name}_{self.current_tool_index}"
                    self.pending_tools[prev_id] = {
                        "name": self.current_tool_name,
                        "input": self.current_tool_input,
                        "index": self.current_tool_index,
                    }

                self.current_tool_name = tool_name
                self.current_tool_use_id = tool_use_id
                self.current_tool_input = tool_input if isinstance(tool_input, dict) else {}
                self.current_tool_index = self.agent_logger.log_tool_call_start(tool_name, tool_input)
                if not self.suppress_tool_ws_events:
                    self.add_ws_event({
                        "type": "tool_start",
                        "tool": tool_name,
                        "toolUseId": tool_use_id,
                        "input": _safe_serialize(tool_input, max_length=500),
                    })

        if "tool_result" in kwargs:
            tool_result = kwargs.get("tool_result", {})
            tool_use_id = tool_result.get("toolUseId")
            tool_name = tool_result.get("name")
            result_content = tool_result.get("content", tool_result.get("result"))
            # Strands SDK returns content as list of blocks — extract text
            if isinstance(result_content, list):
                texts = [b.get("text", "") for b in result_content if isinstance(b, dict) and "text" in b]
                result_content = "\n".join(texts) if texts else str(result_content)
            status = tool_result.get("status", "completed")

            # Match tool_result to the correct tool via toolUseId → pending_tools
            matched_tool = None
            matched_index = None
            matched_input = {}

            if tool_use_id and tool_use_id in self.pending_tools:
                # Found in pending_tools (previous tool that was superseded)
                matched_tool_info = self.pending_tools.pop(tool_use_id)
                matched_tool = matched_tool_info["name"]
                matched_index = matched_tool_info["index"]
                matched_input = matched_tool_info.get("input", {})
            elif tool_use_id and tool_use_id == self.current_tool_use_id:
                # It's the current tool
                matched_tool = tool_name or self.current_tool_name
                matched_index = self.current_tool_index
                matched_input = self.current_tool_input
            elif not tool_use_id and self.current_tool_index is not None:
                # Fallback: no toolUseId, use current tool (legacy behavior)
                matched_tool = tool_name or self.current_tool_name
                matched_index = self.current_tool_index
                matched_input = self.current_tool_input
            else:
                # Last resort: try matching by name in pending_tools
                for pid, pinfo in list(self.pending_tools.items()):
                    if pinfo["name"] == tool_name:
                        matched_tool_info = self.pending_tools.pop(pid)
                        matched_tool = matched_tool_info["name"]
                        matched_index = matched_tool_info["index"]
                        matched_input = matched_tool_info.get("input", {})
                        tool_use_id = pid
                        break

            if matched_index is not None:
                self.agent_logger.log_tool_call_end(matched_index, result=result_content, status=status)
                if not self.suppress_tool_ws_events:
                    self.add_ws_event({
                        "type": "tool_end",
                        "tool": matched_tool,
                        "toolUseId": tool_use_id,
                        "input": _safe_serialize(matched_input, max_length=2000),
                        "result": _safe_serialize(result_content, max_length=1000),
                        "status": status,
                    })
                # Always emit asset preview regardless of suppress flag
                if status == "completed" and matched_tool:
                    self._emit_asset_preview_from_result(matched_tool, result_content)

                # Clear current tracking if this was the current tool
                if matched_index == self.current_tool_index:
                    self.current_tool_index = None
                    self.current_tool_name = None
                    self.current_tool_use_id = None
                    self.current_tool_input = {}

        if "reasoningText" in kwargs:
            thinking_content = kwargs["reasoningText"]
            if thinking_content and not self.suppress_tool_ws_events:
                self.pending_ws_events.append({"type": "thinking", "content": thinking_content})

        if "error" in kwargs:
            self.pending_ws_events.append({"type": "error", "content": str(kwargs["error"])})

    def _emit_asset_preview_from_result(self, tool_name: str, result: Any):
        """Parse tool result and emit asset preview events."""
        asset_type = self.TOOL_TO_ASSET_TYPE.get(tool_name)
        if not asset_type:
            return
        if asset_type == "download" and isinstance(result, dict):
            if result.get("success") and result.get("download_url"):
                self.pending_ws_events.append({
                    "type": "download_ready",
                    "downloadUrl": result.get("download_url"),
                    "expiresAt": result.get("expires_at"),
                })
            return
        if not isinstance(result, dict) or not result.get("success", True):
            return

        files = {}
        if "files" in result and isinstance(result["files"], dict):
            files = result["files"]
        if "openapi_yaml" in result:
            files["openapi.yaml"] = result["openapi_yaml"]
        if "openapi_json" in result:
            files["openapi.json"] = result["openapi_json"]
        if "prompt_yaml" in result and isinstance(result["prompt_yaml"], str):
            files["ai_prompt.yaml"] = result["prompt_yaml"]
        elif "prompt" in result and isinstance(result["prompt"], str):
            files["ai_prompt.md"] = result["prompt"]
        if "contact_flow_json" in result:
            files["contact_flow.json"] = result["contact_flow_json"]
        if "mermaid_diagram" in result:
            files["flow_diagram.md"] = result["mermaid_diagram"]
        if "spec" in result and isinstance(result["spec"], dict):
            files["operation_spec.json"] = json.dumps(result["spec"], indent=2, ensure_ascii=False)

        operation_id = result.get("operation_id")
        for filename, content in files.items():
            if not content or not isinstance(content, str):
                continue
            if filename and operation_id:
                asset_key = f"{asset_type}-{operation_id}-{filename}"
            elif filename:
                asset_key = f"{asset_type}-{filename}"
            elif operation_id:
                asset_key = f"{asset_type}-{operation_id}"
            else:
                asset_key = asset_type
            existing = self.current_asset_previews.get(asset_key)
            if existing and existing.get("content") == content:
                if not existing.get("isComplete"):
                    existing["isComplete"] = True
                    self.pending_ws_events.append({"type": "asset_preview", "assetPreview": existing})
                continue
            ext = os.path.splitext(filename)[1].lower()
            language = self.EXT_TO_LANGUAGE.get(ext, "text")
            asset_preview = {
                "assetType": asset_type, "operationId": operation_id,
                "fileName": filename, "content": content,
                "isComplete": True, "language": language,
                "createdAt": int(time.time() * 1000), "messageIndex": None,
            }
            self.pending_ws_events.append({"type": "asset_preview", "assetPreview": asset_preview})
            self.current_asset_previews[asset_key] = asset_preview

    def get_pending_ws_events(self) -> List[Dict[str, Any]]:
        """Get and clear pending WebSocket events."""
        events = self.pending_ws_events.copy()
        self.pending_ws_events = []
        return events

    def get_full_response(self) -> str:
        """Get the accumulated response."""
        return self.full_response

    def get_buffer(self) -> str:
        """Get and clear the stream buffer."""
        buffer = self.stream_buffer
        self.stream_buffer = ""
        return buffer
