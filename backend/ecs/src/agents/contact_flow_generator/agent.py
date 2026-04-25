"""
Contact Flow Generator Sub-Agent

Generates Amazon Connect Contact Flow JSON with Q in Connect integration.
Uses text output with code block parsing (no tool calls) for efficiency.

Optional web search capability for looking up Amazon Connect documentation.
"""

import json
import os
import re
import time
import html
import logging
from typing import AsyncIterator, Optional
import requests
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

from .system_prompt import CONTACT_FLOW_GENERATOR_SYSTEM_PROMPT, RAG_SEARCH_INSTRUCTION
from tools.workspace_tools_for_subagent import detect_spec_escalation
from .retrieve_tool import retrieve_contact_flow_knowledge

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


def _send_progress(status: str, flow_name: str = "", message: str = ""):
    """Send progress event via WebSocket callback handler."""
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": "contact_flow_generator",
                 "status": status, "flow_name": flow_name}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


def _setup_streaming_for_subagent():
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] contact_flow_generator: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
            logger.info(f"[SUBAGENT_SETUP] contact_flow_generator: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] contact_flow_generator: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] contact_flow_generator: handler not available or missing stream_asset_preview")


def _parse_mermaid_block(text: str) -> tuple[str | None, str]:
    """Parse Mermaid code block from LLM output."""
    pattern = r'```mermaid\s*\n(.*?)```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_mermaid"
    return None, "no_match"


def _parse_json_block(text: str) -> tuple[str | None, str]:
    """
    Parse JSON code block from LLM output with multiple fallback patterns.

    Returns:
        tuple: (json_content, parse_method)
    """
    # Pattern 1: Standard json code block
    pattern1 = r'```json\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_json"

    # Pattern 2: Generic code block containing JSON
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        # Verify it looks like JSON (Contact Flow format)
        if content.startswith('{') and ('"Version"' in content or '"Actions"' in content):
            return content, "markdown_generic"

    # Pattern 3: Raw JSON detection (starts with { and looks like Contact Flow)
    lines = text.strip().split('\n')
    if lines:
        first_line = lines[0].strip()
        if first_line.startswith('{'):
            # Try to find the complete JSON object
            brace_count = 0
            json_lines = []
            for line in lines:
                json_lines.append(line)
                brace_count += line.count('{') - line.count('}')
                if brace_count == 0 and json_lines:
                    break
            if json_lines:
                candidate = '\n'.join(json_lines).strip()
                if '"Version"' in candidate or '"Actions"' in candidate:
                    return candidate, "raw_json_detected"

    return None, "no_match"


def _stream_asset(asset_type: str, file_name: str, content: str, operation_id: str = None):
    try:
        from tools.streaming_callback import stream_asset, get_streaming_callback
        logger.info(f"[STREAM_ASSET] contact_flow_generator calling stream_asset: {asset_type}/{file_name}, callback={get_streaming_callback() is not None}")
        stream_asset(asset_type, file_name, content, operation_id=operation_id, is_complete=True)
    except ImportError as e:
        logger.warning("streaming_callback not available")


# ============================================
# Web Search Tools for Contact Flow Generator
# ============================================

@tool
def search_amazon_connect_docs(
    query: str,
    count: int = 5
) -> dict:
    """
    Search for Amazon Connect documentation and best practices.

    Use this when you need to verify:
    - Contact Flow block parameters and syntax
    - Complex flow patterns (Loop, callback, external transfer)
    - Q in Connect integration details

    Args:
        query: Search query (will be combined with "Amazon Connect")
        count: Number of results (default 5, max 10)

    Returns:
        Search results with titles, URLs, and descriptions
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")

    if not api_key:
        return {
            "success": False,
            "error": "BRAVE_API_KEY not configured. Cannot perform web search.",
            "results": []
        }

    try:
        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json"
        }

        # Prefix with "Amazon Connect" and prefer AWS docs
        full_query = f"site:docs.aws.amazon.com Amazon Connect {query}"

        params = {
            "q": full_query,
            "count": min(count, 10),
        }

        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()
        results = []

        if "web" in data and "results" in data["web"]:
            for item in data["web"]["results"]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                })

        logger.info(f"[search_amazon_connect_docs] Query: {query}, Results: {len(results)}")

        return {
            "success": True,
            "query": full_query,
            "results": results,
            "count": len(results)
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Search request timed out",
            "results": []
        }
    except requests.exceptions.HTTPError as e:
        return {
            "success": False,
            "error": f"HTTP error: {e.response.status_code}",
            "results": []
        }
    except Exception as e:
        logger.error(f"[search_amazon_connect_docs] Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "results": []
        }


@tool
def fetch_documentation_page(
    url: str,
    max_length: int = 8000
) -> dict:
    """
    Fetch content from an AWS documentation page.

    Use this after search_amazon_connect_docs to get detailed information
    from a specific documentation page.

    Preferred documentation sources:
    - https://docs.aws.amazon.com/connect/latest/adminguide/
    - https://docs.aws.amazon.com/connect/latest/adminguide/contact-blocks.html
    - https://docs.aws.amazon.com/connect/latest/adminguide/amazon-q-connect.html

    Args:
        url: URL to fetch (preferably AWS docs)
        max_length: Maximum content length to return (default 8000 chars)

    Returns:
        Extracted text content from the page
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AWSDocReader/1.0)"
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        content = response.text

        # Simple HTML to text conversion
        # Remove script and style elements
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<nav[^>]*>.*?</nav>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<header[^>]*>.*?</header>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<footer[^>]*>.*?</footer>', '', content, flags=re.DOTALL | re.IGNORECASE)

        # Strip HTML tags
        content = re.sub(r'<[^>]+>', ' ', content)

        # Unescape HTML entities
        content = html.unescape(content)

        # Clean up whitespace
        content = re.sub(r'\s+', ' ', content).strip()

        # Truncate if needed
        if len(content) > max_length:
            content = content[:max_length] + f"... [truncated, {len(content) - max_length} chars remaining]"

        logger.info(f"[fetch_documentation_page] Fetched {url}, content length: {len(content)}")

        return {
            "success": True,
            "url": url,
            "content": content,
            "content_length": len(content)
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "url": url,
            "error": "Request timed out"
        }
    except requests.exceptions.HTTPError as e:
        return {
            "success": False,
            "url": url,
            "error": f"HTTP error: {e.response.status_code}"
        }
    except Exception as e:
        logger.error(f"[fetch_documentation_page] Error fetching {url}: {e}")
        return {
            "success": False,
            "url": url,
            "error": str(e)
        }


# Web search instruction to add to system prompt when enabled
WEB_SEARCH_INSTRUCTION = """
## WEB SEARCH (Enabled)

You have access to web search tools. Use them when you need clarification on:
- Block type parameters or syntax you're unsure about
- Complex patterns (Loop, callback, external transfer)
- Recent Amazon Connect API changes

### Primary Reference
Always prefer the official AWS documentation:
- **Amazon Connect Admin Guide**: https://docs.aws.amazon.com/connect/latest/adminguide/
- **Contact Flow blocks**: https://docs.aws.amazon.com/connect/latest/adminguide/contact-blocks.html
- **Q in Connect**: https://docs.aws.amazon.com/connect/latest/adminguide/amazon-q-connect.html

### Search Strategy
1. First try fetching the official AWS doc page directly if you know the topic
2. If unsure, use search_amazon_connect_docs with specific query
3. Verify block parameters against official documentation before generating

### When to Search
- GOOD: "GetParticipantInput block Lex integration" (specific block question)
- GOOD: "Loop block maximum iterations" (specific parameter question)
- BAD: "Contact flow help" (too vague)
- BAD: General questions you already know the answer to

Only search when genuinely uncertain. Your built-in knowledge covers most cases.
"""


@tool
async def contact_flow_generator_agent(
    flow_name: str,
    company_name: str,
    operations: str = "",
    language: str = "",
    modification_request: str = "",
    contact_flow_requirements: str = "",
    enable_web_search: bool = False,
    enable_rag: bool = True
) -> AsyncIterator:
    """
    Generate Amazon Connect Contact Flow with Q in Connect integration.

    Args:
        flow_name: Name for the contact flow
        company_name: Company name for greeting
        operations: JSON string of available MCP operations. Optional - auto-loads from saved specs if empty.
        language: Language code (ko-KR, en-US, ja-JP)
        modification_request: User's modification request for regeneration (optional)
        contact_flow_requirements: JSON string of contact flow requirements from interviewer (optional).
            When provided, the agent dynamically adjusts the flow structure.
            When empty, generates the standard Q in Connect self-service pattern.
        enable_web_search: Enable searching Amazon Connect docs for block syntax (default False)
        enable_rag: Enable RAG retrieval from Knowledge Base (default True)
    """
    _setup_streaming_for_subagent()

    # Auto-load operations if not provided
    if not operations:
        try:
            from tools.spec_manager import get_all_specs
            all_specs = get_all_specs()
            if all_specs:
                operations = json.dumps([s.model_dump() for s in all_specs.values()], ensure_ascii=False)
                logger.info(f"[CONTACT_FLOW] Auto-loaded {len(all_specs)} operation specs")
        except Exception as e:
            logger.warning(f"[CONTACT_FLOW] Failed to auto-load specs: {e}")

    # Auto-load session flow config for customer_info_variables and call_direction
    session_flow_json = ""
    try:
        from tools.spec_manager import get_session_flow_config
        session_flow = get_session_flow_config()
        if session_flow:
            session_flow_json = json.dumps(session_flow.model_dump(), ensure_ascii=False)
            logger.info(f"[CONTACT_FLOW] Auto-loaded session flow config")
    except Exception as e:
        logger.warning(f"[CONTACT_FLOW] Failed to auto-load session flow config: {e}")

    _send_progress("started", flow_name)
    yield {
        "type": "progress",
        "agent": "contact_flow_generator",
        "status": "started",
        "flow_name": flow_name
    }

    # Build modification section if needed
    modification_section = ""
    existing_flow = ""
    ws_path = ""
    modification_tools = []
    if modification_request:
        # Try workspace tools for direct file modification
        try:
            from tools.streaming_callback import get_session_id
            session_id = get_session_id()
            if session_id:
                from tools.workspace_tools_for_subagent import (
                    create_modification_tools,
                    WORKSPACE_TOOLS_MODIFICATION_PROMPT,
                )
                modification_tools, ws_path = create_modification_tools(
                    session_id, "contact_flow", "contact_flow.json",
                    operation_id=flow_name,
                )
        except Exception as e:
            logger.warning(f"[CONTACT_FLOW] Failed to create modification tools: {e}")

        if modification_tools:
            modification_section = WORKSPACE_TOOLS_MODIFICATION_PROMPT.format(
                modification_request=modification_request,
            )
            logger.info(f"[CONTACT_FLOW] Using workspace tools for modification ({len(modification_tools)} tools)")
        else:
            # Fallback: legacy mode
            try:
                from tools.streaming_callback import get_session_id
                from tools.workspace_file_tools import read_workspace_file, get_asset_workspace_path
                session_id = get_session_id()
                if session_id:
                    ws_path = get_asset_workspace_path(session_id, "contact_flow", "contact_flow.json", operation_id=flow_name)
                    ws_result = read_workspace_file(session_id=session_id, path=ws_path)
                    existing_flow = ws_result["content"] if ws_result.get("success") else ""
            except Exception as e:
                logger.warning(f"[CONTACT_FLOW] Failed to load existing asset from workspace: {e}")
            if not existing_flow:
                try:
                    from tools.asset_loader import load_existing_asset
                    existing_flow = load_existing_asset("contact_flow", file_name="contact_flow.json") or ""
                except Exception as e:
                    logger.warning(f"[CONTACT_FLOW] Fallback S3 load also failed: {e}")

            modification_section = f"""

⚠️ MODIFICATION REQUEST:
{modification_request}

Apply this modification to the existing flow below. Do NOT rewrite from scratch.
"""
            if existing_flow:
                modification_section += f"""
## EXISTING FLOW (MODIFY THIS)
Modify this Contact Flow JSON to fulfill the modification request. Preserve the overall structure,
action IDs, transitions, and all working blocks. Only change what the modification request asks for.

```json
{existing_flow}
```
"""

    # Build requirements section if provided
    requirements_section = ""
    if contact_flow_requirements:
        requirements_section = f"""

Contact Flow Requirements (from customer interview):
{contact_flow_requirements}

Use these requirements to customize the flow structure. For example:
- If hours_of_operation is specified, add CheckHoursOfOperation block
- If callback_enabled is true, add callback pattern
- If specific queue names are given, use them in TransferContactToQueue
- Adapt welcome_message, transfer_message, after_hours_message as specified
"""

    # Add RAG/web search instructions based on what's enabled
    search_section = ""
    kb_id = os.environ.get("CONTACT_FLOW_KB_ID", "")
    if enable_rag and kb_id:
        search_section += f"\n\n{RAG_SEARCH_INSTRUCTION}"
    if enable_web_search:
        search_section += f"\n\n{WEB_SEARCH_INSTRUCTION}"

    # Build session flow section
    session_flow_section = ""
    if session_flow_json:
        session_flow_section = f"""

Session Flow Config (customer_info_variables → Contact Flow session attributes):
{session_flow_json}
"""

    prompt = f"""Generate Contact Flow for:

Flow Name: {flow_name}
Company: {company_name}
Language: {language}

Operations:
{operations}
{session_flow_section}
{requirements_section}
{modification_section}
{search_section}"""

    try:
        model = BedrockModel(
            model_id=os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0,
            max_tokens=128000,
            # cache_prompt removed - using cachePoint in system_prompt instead
            cache_tools="default",   # Cache tool definitions (web search tools)
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        # Conditionally add RAG and web search tools
        tools = []
        if enable_rag and kb_id:
            tools.append(retrieve_contact_flow_knowledge)
            logger.info(f"[contact_flow_generator] RAG enabled for {flow_name}, KB: {kb_id}")
        if enable_web_search:
            tools.extend([search_amazon_connect_docs, fetch_documentation_page])
            logger.info(f"[contact_flow_generator] Web search enabled for {flow_name}")
        # Merge modification tools with existing tools
        if modification_tools:
            tools.extend(modification_tools)

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": CONTACT_FLOW_GENERATOR_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=tools,
        )

        yield {
            "type": "progress",
            "agent": "contact_flow_generator",
            "status": "running",
            "flow_name": flow_name
        }

        # Import heartbeat utilities
        import asyncio
        from tools.heartbeat_utils import create_heartbeat_manager

        from tools.incremental_streamer import IncrementalCodeStreamer

        full_response = ""
        last_heartbeat = time.time()
        tools_were_used = False
        json_streamer = IncrementalCodeStreamer(
            "contact_flow", "contact_flow.json", flow_name,
            code_markers=["json"], flush_interval=500
        )
        mermaid_streamer = IncrementalCodeStreamer(
            "mermaid", "contact_flow_diagram.md", flow_name,
            code_markers=["mermaid"], flush_interval=500
        )

        # Create heartbeat manager for background heartbeats
        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="contact_flow_generator",
            project_name=flow_name
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
                            json_streamer.feed(chunk)  # Progressive JSON streaming
                            mermaid_streamer.feed(chunk)  # Progressive Mermaid streaming
                        heartbeat.update_progress(len(full_response))
                        # Yield text chunks for real-time streaming
                        yield {
                            "type": "text",
                            "agent": "contact_flow_generator",
                            "content": chunk
                        }

                    # Detect tool usage (workspace tools specifically)
                    if "current_tool_use" in event and event["current_tool_use"].get("name"):
                        tool_name = event["current_tool_use"]["name"]
                        if tool_name in ("read_current_file", "patch_file", "write_file", "search_workspace_files"):
                            tools_were_used = True

                    # Also yield progress periodically
                    current_time = time.time()
                    if current_time - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = current_time
                        yield {
                            "type": "progress",
                            "agent": "contact_flow_generator",
                            "status": "running",
                            "flow_name": flow_name,
                            "message": f"Generating... ({len(full_response)} chars)"
                        }
            finally:
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

        if not modification_tools:
            json_streamer.finalize()
            mermaid_streamer.finalize()

        # === Result processing ===
        json_content = None
        json_method = None
        mermaid_content = None
        mermaid_method = None

        if modification_tools and tools_were_used:
            logger.info(f"[CONTACT_FLOW] Modification completed via workspace tools for {flow_name}")
            json_content = True  # sentinel
            json_method = "workspace_tools"
        elif modification_request and modification_tools and not tools_were_used:
            escalation = detect_spec_escalation(full_response or "")
            if escalation is not None:
                logger.info(f"[CONTACT_FLOW] Sub-agent escalated spec_level for {flow_name}: {escalation.get('reason', '')[:200]}")
                yield {
                    "type": "progress",
                    "agent": "contact_flow_generator",
                    "status": "escalated",
                }
                yield {
                    "success": False,
                    "flow_name": flow_name,
                    "escalation": "spec_level",
                    "reason": escalation.get("reason", ""),
                    "suggested_spec_updates": escalation.get("suggested_spec_updates", []),
                    "raw_response": (full_response[:2000] if full_response else ""),
                    "_completion_marker": "SUBAGENT_COMPLETE",
                }
                return
            # Strict patch-only mode: modification must use workspace tools, never regenerate
            logger.error(f"[CONTACT_FLOW] Modification mode did not use workspace tools for {flow_name}. Refusing to regenerate from scratch.")
            yield {
                "type": "progress",
                "agent": "contact_flow_generator",
                "status": "error",
            }
            yield {
                "success": False,
                "flow_name": flow_name,
                "error": "modification_did_not_patch",
                "summary": "Modification request did not result in workspace patches. File was not regenerated.",
                "raw_response": (full_response[:2000] if full_response else ""),
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
            return
        else:
            json_content = json_streamer.get_result()
            json_method = "incremental_stream" if json_content else None
            if not json_content:
                json_content, json_method = _parse_json_block(full_response)

            # Mermaid diagram: always use standard parsing (edits don't apply)
            mermaid_content = mermaid_streamer.get_result()
            mermaid_method = "incremental_stream" if mermaid_content else None
            if not mermaid_content:
                mermaid_content, mermaid_method = _parse_mermaid_block(full_response)

        if json_content:
            json_file_name = "contact_flow.json"

            if json_method == "workspace_tools":
                # Tools already handled file modification + streaming
                logger.info(f"[CONTACT_FLOW] Workspace tools completed for {flow_name}")
            else:
                logger.info(f"JSON parsed successfully for {flow_name} using method: {json_method}")

                # Stream Mermaid diagram as a separate file if available
                if mermaid_content:
                    diagram_file_name = "contact_flow_diagram.md"
                    diagram_content = f"# {flow_name} Contact Flow Diagram\n\n```mermaid\n{mermaid_content}\n```\n"
                    # Only stream final asset if incremental streamer didn't already handle it
                    if not mermaid_streamer.found_code_block:
                        _stream_asset("mermaid", diagram_file_name, diagram_content, flow_name)

                # Stream JSON as the main contact flow file
                # Modification mode (legacy fallback): write to workspace + emit diff
                if modification_request and existing_flow and ws_path:
                    try:
                        from tools.streaming_callback import get_session_id
                        from tools.workspace_file_tools import write_with_diff
                        _sid = get_session_id()
                        if _sid:
                            write_with_diff(session_id=_sid, path=ws_path, new_content=json_content)
                            logger.info(f"[CONTACT_FLOW] Wrote modified flow to workspace with diff: {ws_path}")
                    except Exception as e:
                        logger.warning(f"[CONTACT_FLOW] write_with_diff failed, falling back to stream_asset: {e}")
                        if not json_streamer.found_code_block:
                            _stream_asset("contact_flow", json_file_name, json_content, flow_name)
                elif not json_streamer.found_code_block:
                    _stream_asset("contact_flow", json_file_name, json_content, flow_name)

            _send_progress("completed", flow_name)
            yield {
                "type": "progress",
                "agent": "contact_flow_generator",
                "status": "completed",
                "flow_name": flow_name
            }

            files_generated = [json_file_name]
            if mermaid_content:
                files_generated.append("contact_flow_diagram.md")

            yield {
                "success": True,
                "flow_name": flow_name,
                "files_generated": files_generated,
                "has_mermaid": mermaid_content is not None,
                "parse_method": {"json": json_method, "mermaid": mermaid_method},
                "summary": f"Generated Contact Flow for {flow_name}" + (f" with diagram" if mermaid_content else ""),
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
        else:
            logger.error(f"Failed to parse JSON for {flow_name}. Response length: {len(full_response)}")
            logger.debug(f"Full response: {full_response[:1000]}")

            _send_progress("error", flow_name)
            yield {
                "type": "progress",
                "agent": "contact_flow_generator",
                "status": "error",
                "flow_name": flow_name
            }

            truncated_response = full_response[:4000] if len(full_response) > 4000 else full_response

            yield {
                "success": False,
                "flow_name": flow_name,
                "error": "Failed to parse JSON block",
                "parse_method": json_method,
                "raw_response": truncated_response,
                "raw_response_length": len(full_response),
                "summary": f"Contact Flow generation parsing failed for {flow_name}. Raw response included for fallback.",
                "_completion_marker": "SUBAGENT_COMPLETE"
            }

    except Exception as e:
        import traceback
        error_str = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Contact Flow generation failed for {flow_name}: {error_str}")
        logger.debug(f"Traceback: {error_traceback}")

        _send_progress("error", flow_name)
        yield {
            "type": "progress",
            "agent": "contact_flow_generator",
            "status": "error",
            "flow_name": flow_name
        }

        yield {
            "success": False,
            "flow_name": flow_name,
            "error": error_str,
            "error_type": type(e).__name__,
            "summary": f"Failed: {error_str[:100]}",
            "_completion_marker": "SUBAGENT_COMPLETE"
        }
