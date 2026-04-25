"""
Lambda Generator Sub-Agent

Generates production-ready AWS Lambda function code based on operation specifications.
Uses text output with code block parsing (no tool calls) for efficiency.
"""

import json
import os
import re
import time
import logging
from typing import AsyncIterator
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

# Heartbeat interval to keep WebSocket connection alive during long-running generation
HEARTBEAT_INTERVAL_SECONDS = 10

from .system_prompt import LAMBDA_GENERATOR_SYSTEM_PROMPT
from agents._consistency_rules import build_field_schema_section
from tools.workspace_tools_for_subagent import detect_spec_escalation

logger = logging.getLogger(__name__)

# Callback handler lives in a ContextVar so concurrent async users don't
# overwrite each other's handler.
from tools.session_context import current_callback_handler


def set_callback_handler(handler):
    """Set the callback handler from parent agent."""
    current_callback_handler.set(handler)


def get_callback_handler():
    """Get the current callback handler."""
    return current_callback_handler.get()


def _setup_streaming_for_subagent():
    """Set up streaming callback for Sub-Agent execution."""
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] lambda_generator: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
            logger.info(f"[SUBAGENT_SETUP] lambda_generator: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] lambda_generator: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] lambda_generator: handler not available or missing stream_asset_preview")


def _send_progress(agent_name: str, status: str, operation_id: str = "", message: str = ""):
    """Send progress event via callback handler."""
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": agent_name, "status": status,
                 "operation_id": operation_id}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


def _parse_code_block(text: str) -> tuple[str | None, str]:
    """
    Parse Python code block from LLM output with multiple fallback patterns.

    Returns:
        tuple: (code_content, parse_method)
        - code_content: Extracted code or None if all patterns failed
        - parse_method: Which pattern matched (for debugging)
    """
    # Pattern 1: Standard markdown code block with python tag
    pattern1 = r'```python\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_python"

    # Pattern 2: Generic markdown code block
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        code = match.group(1).strip()
        # Verify it looks like Python code
        if 'def ' in code or 'import ' in code or 'class ' in code:
            return code, "markdown_generic"

    # Pattern 3: Code block with language variations (py, Python, etc.)
    pattern3 = r'```(?:py|Python|PYTHON)\s*\n(.*?)```'
    match = re.search(pattern3, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip(), "markdown_py_variant"

    # Pattern 4: If response starts with typical Python code patterns (no code block)
    # This handles cases where LLM forgot markdown formatting
    lines = text.strip().split('\n')
    if lines:
        first_line = lines[0].strip()
        # Check if it starts like Python code
        if (first_line.startswith('"""') or
            first_line.startswith("'''") or
            first_line.startswith('import ') or
            first_line.startswith('from ') or
            first_line.startswith('def ') or
            first_line.startswith('class ') or
            first_line.startswith('# ')):
            # Find where the actual code ends (before any explanation text)
            code_lines = []
            in_code = True
            for line in lines:
                # Stop if we hit obvious non-code content
                if in_code and line.strip().startswith('This code') or line.strip().startswith('The above'):
                    break
                code_lines.append(line)
            if code_lines:
                return '\n'.join(code_lines).strip(), "raw_python_detected"

    return None, "no_match"


def _stream_asset(asset_type: str, file_name: str, content: str, operation_id: str):
    """Stream asset to frontend via callback."""
    try:
        from tools.streaming_callback import stream_asset, get_streaming_callback
        logger.info(f"[STREAM_ASSET] lambda_generator calling stream_asset: {asset_type}/{file_name}, callback={get_streaming_callback() is not None}")
        stream_asset(asset_type, file_name, content, operation_id=operation_id, is_complete=True)
    except ImportError as e:
        logger.warning(f"streaming_callback not available: {e}")


@tool
async def lambda_generator_agent(
    operation_id: str,
    operation_spec: str = "",
    infrastructure_schema: str = "",
    db_type: str = "dynamodb",
    modification_request: str = ""
) -> AsyncIterator:
    """
    Generate production-ready Lambda function code for a single operation.

    Args:
        operation_id: Unique identifier for the operation (e.g., "create_reservation")
        operation_spec: The operation specification. Optional - if empty, auto-loads from saved specs.
        infrastructure_schema: DynamoDB schema from Infrastructure generator. Optional - if empty, auto-loads from infra registry.
        db_type: Database type - "dynamodb", "rds_mysql", or "rds_postgresql"
        modification_request: User's modification request for regeneration (optional)
    """
    _setup_streaming_for_subagent()

    # Auto-load operation_spec and tool_spec
    # With multi-tool architecture, operation_id may be a tool_id (e.g., "resend_email")
    # We need to find the ToolSpec AND its parent OperationSpec for context.
    tool_spec_json = ""
    try:
        from tools.spec_manager import get_tool_with_parent_spec, get_all_specs
        tool_spec_obj, parent_spec = get_tool_with_parent_spec(operation_id)

        if tool_spec_obj:
            # operation_id is a tool_id within a multi-tool operation
            tool_spec_json = json.dumps(tool_spec_obj.model_dump(), ensure_ascii=False)
            logger.info(f"[LAMBDA] Found ToolSpec for: {operation_id} (role={tool_spec_obj.role})")

            # Use parent operation spec for context (if not already provided)
            if not operation_spec and parent_spec:
                operation_spec = json.dumps(parent_spec.model_dump(), ensure_ascii=False)
                logger.info(f"[LAMBDA] Auto-loaded parent OperationSpec for tool: {operation_id} (parent={parent_spec.operation_id})")
        elif parent_spec:
            # operation_id matches an operation directly (backward compat: no tools[])
            if not operation_spec:
                operation_spec = json.dumps(parent_spec.model_dump(), ensure_ascii=False)
                logger.info(f"[LAMBDA] Auto-loaded spec for: {operation_id} (no tools[])")
        else:
            # Fallback: try direct operation_id lookup
            if not operation_spec:
                all_specs = get_all_specs()
                spec = all_specs.get(operation_id)
                if spec:
                    operation_spec = json.dumps(spec.model_dump(), ensure_ascii=False)
                    logger.info(f"[LAMBDA] Auto-loaded spec for: {operation_id}")
    except Exception as e:
        logger.warning(f"[LAMBDA] Failed to auto-load spec/tool: {e}")

    # Final fallback for operation_spec
    if not operation_spec:
        try:
            from tools.spec_manager import get_all_specs
            spec = get_all_specs().get(operation_id)
            if spec:
                operation_spec = json.dumps(spec.model_dump(), ensure_ascii=False)
                logger.info(f"[LAMBDA] Fallback auto-loaded spec for: {operation_id}")
        except Exception as e:
            logger.warning(f"[LAMBDA] Fallback auto-load failed: {e}")

    # Auto-load infrastructure_schema if not provided
    if not infrastructure_schema:
        try:
            from agents.infrastructure_generator.agent import get_infrastructure_schema
            schema = get_infrastructure_schema()
            if schema:
                infrastructure_schema = schema
                logger.info(f"[LAMBDA] Auto-loaded infrastructure schema ({len(schema)} chars)")
        except Exception as e:
            logger.warning(f"[LAMBDA] Failed to auto-load infra schema: {e}")

    # Auto-load infrastructure spec for db_type, Lambda config, etc.
    infra_spec_section = ""
    try:
        from tools.spec_manager import get_infrastructure_spec
        infra_spec = get_infrastructure_spec()
        if infra_spec:
            infra_spec_section = f"\n## INFRASTRUCTURE SPEC (Source of Truth)\n{json.dumps(infra_spec.model_dump(), ensure_ascii=False)}\n"
            logger.info(f"[LAMBDA] Auto-loaded infrastructure spec: db_type={infra_spec.db_type}")
    except Exception as e:
        logger.warning(f"[LAMBDA] Failed to auto-load infrastructure spec: {e}")

    yield {
        "type": "progress",
        "agent": "lambda_generator",
        "status": "started",
        "operation_id": operation_id
    }

    # Build prompt
    modification_section = ""
    existing_code = ""
    ws_path = ""
    modification_tools = []
    if modification_request:
        # Try to create workspace tools for direct file modification
        try:
            from tools.streaming_callback import get_session_id
            session_id = get_session_id()
            if session_id:
                from tools.workspace_tools_for_subagent import (
                    create_modification_tools,
                    WORKSPACE_TOOLS_MODIFICATION_PROMPT,
                )
                modification_tools, ws_path = create_modification_tools(
                    session_id, "lambda", "index.py", operation_id=operation_id,
                )
        except Exception as e:
            logger.warning(f"[LAMBDA] Failed to create modification tools: {e}")

        if modification_tools:
            # Workspace tools mode: LLM uses tools to patch file directly
            modification_section = WORKSPACE_TOOLS_MODIFICATION_PROMPT.format(
                modification_request=modification_request,
            )
            logger.info(f"[LAMBDA] Using workspace tools for modification ({len(modification_tools)} tools)")
        else:
            # Fallback: legacy mode (inject code into prompt + full code block output)
            try:
                from tools.streaming_callback import get_session_id
                from tools.workspace_file_tools import read_workspace_file, get_asset_workspace_path
                session_id = get_session_id()
                if session_id:
                    ws_path = get_asset_workspace_path(session_id, "lambda", "index.py", operation_id=operation_id)
                    ws_result = read_workspace_file(session_id=session_id, path=ws_path)
                    existing_code = ws_result["content"] if ws_result.get("success") else ""
            except Exception as e:
                logger.warning(f"[LAMBDA] Failed to load existing asset from workspace: {e}")
            # Fallback to S3 if workspace read failed
            if not existing_code:
                try:
                    from tools.asset_loader import load_existing_asset
                    existing_code = load_existing_asset("lambda", operation_id=operation_id, file_name="index.py") or ""
                except Exception as e:
                    logger.warning(f"[LAMBDA] Fallback S3 load also failed: {e}")

            modification_section = f"""

⚠️ MODIFICATION REQUEST:
{modification_request}

Apply this modification to the existing code below. Do NOT rewrite from scratch.
"""
            if existing_code:
                modification_section += f"""
## EXISTING CODE (MODIFY THIS)
Modify this code to fulfill the modification request. Preserve the overall structure,
infrastructure_schema references, table/GSI names, and all working logic.
Only change what the modification request asks for.

```python
{existing_code}
```
"""

    # Build infrastructure schema section
    schema_section = ""
    if infrastructure_schema:
        schema_section = f"""

Infrastructure Schema (from Infrastructure Generator):
{infrastructure_schema}

⚠️ CRITICAL: Use this schema for GSI names and key names. Extract dynamically from schema JSON.
"""

    # Build tool spec section if available
    tool_spec_section = ""
    if tool_spec_json:
        tool_spec_section = f"""

Tool Spec (from ToolSpec — this is the specific tool within an operation):
{tool_spec_json}

⚠️ CRITICAL: This Lambda is for THIS SPECIFIC TOOL only (tool_id="{operation_id}").
Use ONLY the ToolSpec's input_fields/output_fields/data_source for this Lambda.
Do NOT generate code for other tools in the same operation — each tool gets its own Lambda.
The ToolSpec role indicates: primary=main CRUD, helper=auxiliary logic, session=session utility.
"""

    # Inject nested field-schema tree as source-of-truth (FIELD_SHAPE_FIDELITY_RULE).
    # If the lambda is scoped to a specific tool_id, prefer tool_spec; else operation_spec.
    field_schema_section = ""
    try:
        field_src = None
        if tool_spec_json:
            field_src = json.loads(tool_spec_json)
        elif operation_spec:
            field_src = json.loads(operation_spec)
        if field_src:
            field_schema_section = build_field_schema_section([field_src])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"[LAMBDA] Failed to build field-schema section: {e}")

    prompt = f"""Generate index.py for:

Tool/Operation ID: {operation_id}

Specification:
{operation_spec}
{tool_spec_section}{field_schema_section}
Database: {db_type}{schema_section}{infra_spec_section}{modification_section}
"""

    try:
        model = BedrockModel(
            model_id=os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0,  # Deterministic output for consistent formatting
            max_tokens=128000,
            # cache_prompt removed - using cachePoint in system_prompt instead
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": LAMBDA_GENERATOR_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=modification_tools if modification_tools else [],
        )

        yield {
            "type": "progress",
            "agent": "lambda_generator",
            "status": "running",
            "operation_id": operation_id
        }

        # Collect full response while streaming text chunks to frontend
        from tools.incremental_streamer import IncrementalCodeStreamer
        from tools.heartbeat_utils import create_heartbeat_manager

        full_response = ""
        last_heartbeat = time.time()
        tools_were_used = False
        streamer = IncrementalCodeStreamer(
            "lambda", "index.py", operation_id,
            code_markers=["python"], flush_interval=500
        )

        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="lambda_generator",
            project_name=operation_id,
        )

        # CRITICAL: Use explicit generator cleanup to prevent OpenTelemetry context errors
        async with heartbeat:
            generator = agent.stream_async(prompt)
            try:
                async for event in generator:
                    if "data" in event:
                        chunk = event["data"]
                        full_response += chunk
                        # Skip incremental streaming when tools handle it
                        if not modification_tools:
                            streamer.feed(chunk)  # Progressive streaming
                        heartbeat.update_progress(len(full_response))
                        # Yield text chunks for real-time streaming
                        yield {
                            "type": "text",
                            "agent": "lambda_generator",
                            "content": chunk
                        }

                    # Detect tool usage
                    if "current_tool_use" in event and event["current_tool_use"].get("name"):
                        tools_were_used = True

                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = time.time()
                        _send_progress("lambda_generator", "running", operation_id,
                                       f"Generating code... ({len(full_response)} chars)")
            finally:
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

        if not modification_tools:
            streamer.finalize()

        # === Result processing ===
        if modification_tools and tools_were_used:
            # Workspace tools handled file modification + streaming directly
            logger.info(f"[LAMBDA] Modification completed via workspace tools for {operation_id}")
            code = True  # sentinel: tools already wrote the file
            parse_method = "workspace_tools"
        elif modification_request and modification_tools and not tools_were_used:
            escalation = detect_spec_escalation(full_response or "")
            if escalation is not None:
                logger.info(f"[LAMBDA] Sub-agent escalated spec_level for {operation_id}: {escalation.get('reason', '')[:200]}")
                yield {
                    "type": "progress",
                    "agent": "lambda_generator",
                    "status": "escalated",
                    "operation_id": operation_id,
                }
                yield {
                    "success": False,
                    "operation_id": operation_id,
                    "escalation": "spec_level",
                    "reason": escalation.get("reason", ""),
                    "suggested_spec_updates": escalation.get("suggested_spec_updates", []),
                    "raw_response": (full_response[:2000] if full_response else ""),
                    "_completion_marker": "SUBAGENT_COMPLETE",
                }
                return
            # Strict patch-only mode: modification must use workspace tools, never regenerate
            logger.error(f"[LAMBDA] Modification mode did not use workspace tools for {operation_id}. Refusing to regenerate from scratch.")
            yield {
                "type": "progress",
                "agent": "lambda_generator",
                "status": "error",
                "operation_id": operation_id
            }
            yield {
                "success": False,
                "operation_id": operation_id,
                "error": "modification_did_not_patch",
                "summary": "Modification request did not result in workspace patches. File was not regenerated.",
                "raw_response": (full_response[:2000] if full_response else ""),
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
            return
        else:
            # Standard code block parsing (fresh generation only)
            code = None
            parse_method = None

            code = streamer.get_result()
            parse_method = "incremental_stream" if code else None
            if not code:
                code, parse_method = _parse_code_block(full_response)

        if code:
            if parse_method != "workspace_tools":
                logger.info(f"Code parsed successfully for {operation_id} using method: {parse_method}")

                # Modification mode (legacy fallback): write to workspace + emit diff
                if modification_request and existing_code and ws_path:
                    try:
                        from tools.streaming_callback import get_session_id
                        from tools.workspace_file_tools import write_with_diff
                        session_id = get_session_id()
                        if session_id:
                            write_with_diff(session_id=session_id, path=ws_path, new_content=code)
                            logger.info(f"[LAMBDA] Wrote modified code to workspace with diff: {ws_path}")
                    except Exception as e:
                        logger.warning(f"[LAMBDA] write_with_diff failed, falling back to stream_asset: {e}")
                        if not streamer.found_code_block:
                            _stream_asset("lambda", "index.py", code, operation_id)
                # Normal generation: stream asset as before
                elif not streamer.found_code_block:
                    _stream_asset("lambda", "index.py", code, operation_id)

            yield {
                "type": "progress",
                "agent": "lambda_generator",
                "status": "completed",
                "operation_id": operation_id
            }

            # Final result for Orchestrator - always yields this
            yield {
                "success": True,
                "operation_id": operation_id,
                "files_generated": ["index.py"],
                "parse_method": parse_method,
                "summary": f"Generated index.py for {operation_id}",
                "_completion_marker": "SUBAGENT_COMPLETE"  # Explicit completion signal
            }
        else:
            # Failed to parse code block - include raw response for Orchestrator fallback
            logger.error(f"Failed to parse code block for {operation_id}. Response length: {len(full_response)}")
            logger.debug(f"Full response: {full_response[:1000]}")

            yield {
                "type": "progress",
                "agent": "lambda_generator",
                "status": "error",
                "operation_id": operation_id
            }

            # Include raw_response for Orchestrator to potentially parse or retry
            # Truncate to prevent context overflow but keep enough for LLM to understand
            truncated_response = full_response[:4000] if len(full_response) > 4000 else full_response

            yield {
                "success": False,
                "operation_id": operation_id,
                "error": "Failed to parse code block from response",
                "parse_method": parse_method,
                "raw_response": truncated_response,  # Orchestrator can use this for fallback
                "raw_response_length": len(full_response),
                "summary": f"Code generation parsing failed for {operation_id}. Raw response included for fallback.",
                "_completion_marker": "SUBAGENT_COMPLETE"  # Still mark as complete even on failure
            }

    except Exception as e:
        import traceback
        error_str = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Lambda generation failed for {operation_id}: {error_str}")
        logger.debug(f"Traceback: {error_traceback}")

        yield {
            "type": "progress",
            "agent": "lambda_generator",
            "status": "error",
            "operation_id": operation_id
        }

        yield {
            "success": False,
            "operation_id": operation_id,
            "error": error_str,
            "error_type": type(e).__name__,
            "summary": f"Failed: {error_str[:100]}",
            "_completion_marker": "SUBAGENT_COMPLETE"  # Always mark complete, even on exception
        }
