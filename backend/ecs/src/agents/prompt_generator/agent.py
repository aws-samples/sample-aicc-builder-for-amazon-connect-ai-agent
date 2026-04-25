"""
Prompt Generator Sub-Agent

Generates Amazon Connect AI Agent prompts with Q in Connect integration.
Uses text output with YAML parsing (no tool calls) for efficiency.
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

from .system_prompt import PROMPT_GENERATOR_SYSTEM_PROMPT
from tools.workspace_tools_for_subagent import detect_spec_escalation

logger = logging.getLogger(__name__)

# Heartbeat interval to keep WebSocket connection alive
HEARTBEAT_INTERVAL_SECONDS = 10

# Callback handler lives in a ContextVar so concurrent async users don't
# overwrite each other's handler.
from tools.session_context import current_callback_handler


def set_callback_handler(handler):
    current_callback_handler.set(handler)


def get_callback_handler():
    return current_callback_handler.get()


def _send_progress(status: str, agent_name: str = "", message: str = ""):
    """Send progress event via WebSocket callback handler."""
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": "prompt_generator",
                 "status": status, "agent_name": agent_name}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


def _setup_streaming_for_subagent():
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] prompt_generator: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback, get_session_id, set_session_id
            set_streaming_callback(handler.stream_asset_preview)
            # Re-set session_id for S3 storage (may be lost in sub-agent context)
            session_id = get_session_id()
            if session_id:
                set_session_id(session_id)
                logger.info(f"[SUBAGENT_SETUP] prompt_generator: session_id={session_id}")
            logger.info(f"[SUBAGENT_SETUP] prompt_generator: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] prompt_generator: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] prompt_generator: handler not available or missing stream_asset_preview")


def _parse_yaml_block(text: str) -> tuple[str | None, str]:
    """
    Parse YAML code block from LLM output with multiple fallback patterns.

    Returns:
        tuple: (yaml_content, parse_method)
    """
    # Pattern 1: Standard yaml/yml code block
    pattern1 = r'```(?:yaml|yml)\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_yaml"

    # Pattern 2: Generic code block
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        # Verify it looks like YAML prompt config
        if ('agent_name:' in content or 'persona:' in content or
            'system_prompt:' in content or 'instructions:' in content):
            return content, "markdown_generic"

    # Pattern 3: Raw YAML detection (starts with typical prompt YAML keys)
    lines = text.strip().split('\n')
    if lines:
        first_line = lines[0].strip()
        if (first_line.startswith('agent_name:') or first_line.startswith('persona:') or
            first_line.startswith('system_prompt:') or first_line.startswith('---')):
            yaml_lines = []
            for line in lines:
                if line.strip().startswith('This ') or line.strip().startswith('The above'):
                    break
                yaml_lines.append(line)
            if yaml_lines:
                return '\n'.join(yaml_lines).strip(), "raw_yaml_detected"

    return None, "no_match"


def _stream_asset(asset_type: str, file_name: str, content: str, operation_id: str):
    try:
        from tools.streaming_callback import stream_asset, get_streaming_callback
        logger.info(f"[STREAM_ASSET] prompt_generator calling stream_asset: {asset_type}/{file_name}, callback={get_streaming_callback() is not None}")
        stream_asset(asset_type, file_name, content, operation_id=operation_id, is_complete=True)
    except ImportError as e:
        logger.warning(f"streaming_callback not available: {e}")


@tool
async def prompt_generator_agent(
    agent_name: str,
    company_name: str,
    industry: str,
    operations: str = "",
    infrastructure_schema: str = "",
    language: str = "",
    modification_request: str = ""
) -> AsyncIterator:
    """
    Generate Amazon Connect AI Agent prompt.

    Args:
        agent_name: Name of the AI agent (e.g., "Alex", "Sunny")
        company_name: Company name (e.g., "AnyCompany Hotels")
        industry: Industry/domain (e.g., "hospitality", "healthcare")
        operations: JSON string of available MCP operations. Optional - auto-loads from saved specs if empty.
        infrastructure_schema: JSON string containing infrastructure schema. Optional - auto-loads from infrastructure registry if empty.
        language: Primary language code (e.g., "ko-KR", "en-US")
        modification_request: User's modification request for regeneration (optional)
    """
    _setup_streaming_for_subagent()

    # Auto-load operations if not provided
    if not operations:
        try:
            from tools.spec_manager import get_all_specs
            all_specs = get_all_specs()
            if all_specs:
                operations = json.dumps([s.model_dump() for s in all_specs.values()], ensure_ascii=False)
                logger.info(f"[PROMPT] Auto-loaded {len(all_specs)} operation specs")
        except Exception as e:
            logger.warning(f"[PROMPT] Failed to auto-load specs: {e}")

    # Auto-load infrastructure_schema if not provided
    if not infrastructure_schema:
        try:
            from agents.infrastructure_generator.agent import get_infrastructure_schema
            schema = get_infrastructure_schema()
            if schema:
                infrastructure_schema = schema
                logger.info(f"[PROMPT] Auto-loaded infrastructure schema")
        except Exception as e:
            logger.warning(f"[PROMPT] Failed to auto-load infra schema: {e}")

    # Auto-load all tools (operation tools + session tools)
    all_tools_json = ""
    try:
        from tools.spec_manager import get_all_tools
        all_tools = get_all_tools()
        if all_tools:
            all_tools_json = json.dumps([t.model_dump() for t in all_tools], ensure_ascii=False)
            logger.info(f"[PROMPT] Auto-loaded {len(all_tools)} tools (operation + session)")
    except Exception as e:
        logger.warning(f"[PROMPT] Failed to auto-load tools: {e}")

    # Auto-load session flow config
    session_flow_json = ""
    try:
        from tools.spec_manager import get_session_flow_config
        session_flow = get_session_flow_config()
        if session_flow:
            session_flow_json = json.dumps(session_flow.model_dump(), ensure_ascii=False)
            logger.info(f"[PROMPT] Auto-loaded session flow config")
    except Exception as e:
        logger.warning(f"[PROMPT] Failed to auto-load session flow config: {e}")

    _send_progress("started", agent_name)
    yield {
        "type": "progress",
        "agent": "prompt_generator",
        "status": "started",
        "agent_name": agent_name
    }

    # Build modification section if needed
    modification_section = ""
    existing_prompt = ""
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
                    session_id, "prompt", "ai_agent_prompt.yaml",
                    operation_id=agent_name,
                )
        except Exception as e:
            logger.warning(f"[PROMPT] Failed to create modification tools: {e}")

        if modification_tools:
            modification_section = WORKSPACE_TOOLS_MODIFICATION_PROMPT.format(
                modification_request=modification_request,
            )
            logger.info(f"[PROMPT] Using workspace tools for modification ({len(modification_tools)} tools)")
        else:
            # Fallback: legacy mode
            try:
                from tools.streaming_callback import get_session_id
                from tools.workspace_file_tools import read_workspace_file, get_asset_workspace_path
                session_id = get_session_id()
                if session_id:
                    ws_path = get_asset_workspace_path(session_id, "prompt", "ai_agent_prompt.yaml", operation_id=agent_name)
                    ws_result = read_workspace_file(session_id=session_id, path=ws_path)
                    existing_prompt = ws_result["content"] if ws_result.get("success") else ""
            except Exception as e:
                logger.warning(f"[PROMPT] Failed to load existing asset from workspace: {e}")
            if not existing_prompt:
                try:
                    from tools.asset_loader import load_existing_asset
                    existing_prompt = load_existing_asset("prompt", file_name="ai_agent_prompt.yaml") or ""
                except Exception as e:
                    logger.warning(f"[PROMPT] Fallback S3 load also failed: {e}")

            modification_section = f"""

⚠️ MODIFICATION REQUEST:
{modification_request}

Apply this modification to the existing prompt below. Do NOT rewrite from scratch.
"""
            if existing_prompt:
                modification_section += f"""
## EXISTING PROMPT (MODIFY THIS)
Modify this prompt to fulfill the modification request. Preserve the overall structure,
persona, tool_instructions, security rules, and response examples.
Only change what the modification request asks for.

```yaml
{existing_prompt}
```
"""

    # Build infrastructure schema section if provided
    schema_section = ""
    if infrastructure_schema:
        schema_section = f"""

## INFRASTRUCTURE SCHEMA (CRITICAL - USE EXACT FIELD NAMES IN TOOL GUIDANCE)
The following schema defines the EXACT field names used by the backend APIs.
When guiding the AI on how to use tools, reference these EXACT field names:

{infrastructure_schema}

IMPORTANT for tool_instructions:
- When describing how to call APIs, use exact field names from the schema
- Example: If schema says partition_key.name = "phoneNumber", guide AI to extract "phoneNumber" from customer
- Ensure AI knows the expected data formats from data_conventions
"""

    # Build tools section if available
    tools_section = ""
    if all_tools_json:
        tools_section = f"""

## ALL TOOLS (operation tools + session tools — generate tool_instructions for ALL of these)
{all_tools_json}
"""

    # Build session flow config section if available
    session_flow_section = ""
    if session_flow_json:
        session_flow_section = f"""

## SESSION FLOW CONFIG (common settings — use for persona, greeting, closing, customer_info)
{session_flow_json}
"""

    prompt = f"""Generate AI Agent prompt for:

Agent Name: {agent_name}
Company: {company_name}
Industry: {industry}
Language: {language}

Operations:
{operations}
{tools_section}
{session_flow_section}
{schema_section}
{modification_section}"""

    try:
        model = BedrockModel(
            model_id=os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0,
            max_tokens=128000,
            # cache_prompt removed - using cachePoint in system_prompt instead
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": PROMPT_GENERATOR_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=modification_tools if modification_tools else [],
        )

        _send_progress("running", agent_name)
        yield {
            "type": "progress",
            "agent": "prompt_generator",
            "status": "running",
            "agent_name": agent_name
        }

        # Import heartbeat utilities
        import asyncio
        from tools.heartbeat_utils import create_heartbeat_manager

        from tools.incremental_streamer import IncrementalCodeStreamer

        full_response = ""
        last_heartbeat = time.time()
        tools_were_used = False
        file_name = "ai_agent_prompt.yaml"
        streamer = IncrementalCodeStreamer(
            "prompt", file_name, agent_name,
            code_markers=["yaml", "yml"], flush_interval=500
        )

        # Create heartbeat manager for background heartbeats
        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="prompt_generator",
            project_name=agent_name
        )

        async with heartbeat:
            # CRITICAL: Use explicit generator cleanup to prevent OpenTelemetry context errors
            generator = agent.stream_async(prompt)
            try:
                async for event in generator:
                    if "data" in event:
                        chunk = event["data"]
                        full_response += chunk
                        if not modification_tools:
                            streamer.feed(chunk)  # Progressive streaming
                        heartbeat.update_progress(len(full_response))
                        # Yield text chunks for real-time streaming
                        yield {
                            "type": "text",
                            "agent": "prompt_generator",
                            "content": chunk
                        }

                    # Detect tool usage
                    if "current_tool_use" in event and event["current_tool_use"].get("name"):
                        tools_were_used = True

                    # Also yield progress periodically
                    current_time = time.time()
                    if current_time - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = current_time
                        yield {
                            "type": "progress",
                            "agent": "prompt_generator",
                            "status": "running",
                            "agent_name": agent_name,
                            "message": f"Generating... ({len(full_response)} chars)"
                        }
            finally:
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

        if not modification_tools:
            streamer.finalize()

        # === Result processing ===
        if modification_tools and tools_were_used:
            logger.info(f"[PROMPT] Modification completed via workspace tools for {agent_name}")
            yaml_content = True  # sentinel
            parse_method = "workspace_tools"
        elif modification_request and modification_tools and not tools_were_used:
            escalation = detect_spec_escalation(full_response or "")
            if escalation is not None:
                logger.info(f"[PROMPT] Sub-agent escalated spec_level for {agent_name}: {escalation.get('reason', '')[:200]}")
                yield {
                    "type": "progress",
                    "agent": "prompt_generator",
                    "status": "escalated",
                }
                yield {
                    "success": False,
                    "agent_name": agent_name,
                    "escalation": "spec_level",
                    "reason": escalation.get("reason", ""),
                    "suggested_spec_updates": escalation.get("suggested_spec_updates", []),
                    "raw_response": (full_response[:2000] if full_response else ""),
                    "_completion_marker": "SUBAGENT_COMPLETE",
                }
                return
            # Strict patch-only mode: modification must use workspace tools, never regenerate
            logger.error(f"[PROMPT] Modification mode did not use workspace tools for {agent_name}. Refusing to regenerate from scratch.")
            yield {
                "type": "progress",
                "agent": "prompt_generator",
                "status": "error",
            }
            yield {
                "success": False,
                "agent_name": agent_name,
                "error": "modification_did_not_patch",
                "summary": "Modification request did not result in workspace patches. File was not regenerated.",
                "raw_response": (full_response[:2000] if full_response else ""),
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
            return
        else:
            yaml_content = None
            parse_method = None

            yaml_content = streamer.get_result()
            parse_method = "incremental_stream" if yaml_content else None
            if not yaml_content:
                yaml_content, parse_method = _parse_yaml_block(full_response)

        if yaml_content:
            if parse_method != "workspace_tools":
                logger.info(f"YAML parsed successfully for {agent_name} using method: {parse_method}")

                # Modification mode (legacy fallback): write to workspace + emit diff
                if modification_request and existing_prompt and ws_path:
                    try:
                        from tools.streaming_callback import get_session_id
                        from tools.workspace_file_tools import write_with_diff
                        session_id = get_session_id()
                        if session_id:
                            write_with_diff(session_id=session_id, path=ws_path, new_content=yaml_content)
                            logger.info(f"[PROMPT] Wrote modified prompt to workspace with diff: {ws_path}")
                    except Exception as e:
                        logger.warning(f"[PROMPT] write_with_diff failed, falling back to stream_asset: {e}")
                        if not streamer.found_code_block:
                            _stream_asset("prompt", file_name, yaml_content, agent_name)
                # Normal generation: stream asset as before
                elif not streamer.found_code_block:
                    _stream_asset("prompt", file_name, yaml_content, agent_name)

            _send_progress("completed", agent_name)
            yield {
                "type": "progress",
                "agent": "prompt_generator",
                "status": "completed",
                "agent_name": agent_name
            }

            yield {
                "success": True,
                "agent_name": agent_name,
                "file_name": file_name,
                "parse_method": parse_method,
                "summary": f"Generated AI prompt for {agent_name}",
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
        else:
            logger.error(f"Failed to parse YAML for {agent_name}. Response length: {len(full_response)}")
            logger.debug(f"Full response: {full_response[:1000]}")

            _send_progress("error", agent_name)
            yield {
                "type": "progress",
                "agent": "prompt_generator",
                "status": "error",
                "agent_name": agent_name
            }

            truncated_response = full_response[:4000] if len(full_response) > 4000 else full_response

            yield {
                "success": False,
                "agent_name": agent_name,
                "error": "Failed to parse YAML block",
                "parse_method": parse_method,
                "raw_response": truncated_response,
                "raw_response_length": len(full_response),
                "summary": f"Prompt generation parsing failed for {agent_name}. Raw response included for fallback.",
                "_completion_marker": "SUBAGENT_COMPLETE"
            }

    except Exception as e:
        import traceback
        error_str = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Prompt generation failed for {agent_name}: {error_str}")
        logger.debug(f"Traceback: {error_traceback}")

        _send_progress("error", agent_name)
        yield {
            "type": "progress",
            "agent": "prompt_generator",
            "status": "error",
            "agent_name": agent_name
        }

        yield {
            "success": False,
            "agent_name": agent_name,
            "error": error_str,
            "error_type": type(e).__name__,
            "summary": f"Failed: {error_str[:100]}",
            "_completion_marker": "SUBAGENT_COMPLETE"
        }
