"""
OpenAPI Generator Sub-Agent

Generates OpenAPI 3.0 specifications with MCP Gateway extensions.
Supports three modes:
- full: Complete spec in one call (legacy, for ≤6 operations)
- base: Shared structure only (info, servers, security, ErrorResponse, anchors)
- chunk: A batch of operations (paths + schemas for 5-6 operations)
"""

import json
import os
import re
import shutil
import time
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

from .system_prompt import (
    OPENAPI_GENERATOR_SYSTEM_PROMPT,
    BASE_MODE_PROMPT,
    CHUNK_MODE_PROMPT,
)
from agents._consistency_rules import build_field_schema_section
from tools.workspace_tools_for_subagent import detect_spec_escalation

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10

# ── Fragment registry (same pattern as infrastructure_generator) with NFS backing ──
# Per-session registry: concurrent async users on the same ECS task must not
# share this dict, so it lives in session_context keyed by session_id.
from tools.session_context import (
    current_callback_handler,
    current_session_id,
    openapi_fragment_registry_for,
)


def _fragments() -> "dict[str, dict]":
    return openapi_fragment_registry_for(current_session_id.get())


def set_callback_handler(handler):
    current_callback_handler.set(handler)


def get_callback_handler():
    return current_callback_handler.get()


def _nfs_fragment_dir(api_title: str) -> Path | None:
    """Get NFS directory path for fragment storage. Returns None if NFS unavailable."""
    mount = os.environ.get("S3FILES_MOUNT_PATH", "")
    if not mount:
        return None
    safe_name = re.sub(r'[/\\. ]', '_', api_title).lower()
    return Path(mount) / "sessions" / "_fragments" / "openapi" / safe_name


def _persist_fragment_to_nfs(api_title: str, key: str, content: str):
    """Persist a single fragment to NFS for crash recovery."""
    try:
        nfs_dir = _nfs_fragment_dir(api_title)
        if not nfs_dir:
            return
        os.makedirs(nfs_dir, exist_ok=True)
        safe_key = re.sub(r'[/\\]', '_', key)
        target = nfs_dir / f"{safe_key}.yaml"
        fd, tmp_path = tempfile.mkstemp(dir=nfs_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.rename(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning(f"[OPENAPI] NFS persist failed for {api_title}/{key}: {e}")


def _restore_fragments_from_nfs(api_title: str) -> dict | None:
    """Restore fragments from NFS when in-memory registry is empty."""
    try:
        nfs_dir = _nfs_fragment_dir(api_title)
        if not nfs_dir or not nfs_dir.exists():
            return None
        result = {"base": None, "chunks": {}}
        for entry in os.scandir(nfs_dir):
            if not entry.is_file() or not entry.name.endswith('.yaml'):
                continue
            key = entry.name[:-5]  # strip .yaml
            content = Path(entry.path).read_text(encoding='utf-8')
            if key == "base":
                result["base"] = content
            else:
                result["chunks"][key] = content
        if result["base"] is not None or result["chunks"]:
            logger.info(f"[OPENAPI] Restored fragments from NFS: base={result['base'] is not None}, "
                        f"chunks={list(result['chunks'].keys())}")
            return result
    except Exception as e:
        logger.warning(f"[OPENAPI] NFS restore failed for {api_title}: {e}")
    return None


def _clear_nfs_fragments(api_title: str):
    """Remove NFS fragment directory."""
    try:
        nfs_dir = _nfs_fragment_dir(api_title)
        if nfs_dir and nfs_dir.exists():
            shutil.rmtree(nfs_dir)
    except Exception as e:
        logger.warning(f"[OPENAPI] NFS clear failed for {api_title}: {e}")


def _store_fragment(api_title: str, key: str, content: str):
    """Store a fragment in the registry with NFS persistence."""
    reg = _fragments()
    if api_title not in reg:
        reg[api_title] = {"base": None, "chunks": {}}
    if key == "base":
        reg[api_title]["base"] = content
    else:
        reg[api_title]["chunks"][key] = content
    # Persist to NFS for crash recovery
    _persist_fragment_to_nfs(api_title, key, content)


def get_fragments(api_title: str) -> dict | None:
    """Get stored fragments. Falls back to NFS if in-memory is empty."""
    reg = _fragments()
    result = reg.get(api_title)
    if result is not None:
        return result
    # Try NFS restore
    restored = _restore_fragments_from_nfs(api_title)
    if restored:
        reg[api_title] = restored
        return restored
    return None


def clear_fragments(api_title: str):
    """Clear stored fragments (both memory and NFS)."""
    _fragments().pop(api_title, None)
    _clear_nfs_fragments(api_title)


def _setup_streaming_for_subagent():
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] openapi_generator: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback, get_session_id, set_session_id
            set_streaming_callback(handler.stream_asset_preview)
            session_id = get_session_id()
            if session_id:
                set_session_id(session_id)
                logger.info(f"[SUBAGENT_SETUP] openapi_generator: session_id={session_id}")
            logger.info(f"[SUBAGENT_SETUP] openapi_generator: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] openapi_generator: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] openapi_generator: handler not available or missing stream_asset_preview")


def _parse_yaml_block(text: str) -> tuple[str | None, str]:
    pattern1 = r'```(?:yaml|yml)\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_yaml"

    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        if 'openapi:' in content or 'swagger:' in content or 'paths:' in content:
            return content, "markdown_generic"

    lines = text.strip().split('\n')
    if lines:
        first_line = lines[0].strip()
        if first_line.startswith('openapi:') or first_line.startswith('swagger:'):
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
        logger.info(f"[STREAM_ASSET] openapi_generator: {asset_type}/{file_name}, callback={get_streaming_callback() is not None}")
        return stream_asset(asset_type, file_name, content, operation_id=operation_id, is_complete=True)
    except ImportError as e:
        logger.warning(f"streaming_callback not available: {e}")
    except Exception as e:
        logger.error(f"Failed to stream asset: {e}")
    return None


def _send_progress(status: str, api_title: str = "", message: str = ""):
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": "openapi_generator",
                 "status": status, "api_title": api_title}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


def _build_operation_spec_table(ops_list: list) -> str:
    """Render a Markdown source-of-truth table binding operation_id → method/path.

    Bound by HTTP_METHOD_RULE + PATH_PREFIX_RULE in _consistency_rules: the
    OpenAPI verb must match OperationSpec.http_method and the CFN HttpMethod,
    and every OpenAPI `paths:` key begins with `/tools/`. Injecting this table
    so the generator emits `paths:` FROM the spec rather than guessing.
    """
    rows = []
    for op in ops_list or []:
        op_id = op.get("operation_id") or op.get("tool_id") or ""
        tools = op.get("tools") or []
        if tools:
            for t in tools:
                tool_id = t.get("tool_id") or ""
                if not tool_id:
                    continue
                method = (t.get("http_method") or "POST").lower()
                path = t.get("path") or f"/tools/{tool_id}"
                if not path.startswith("/tools/"):
                    path = f"/tools/{tool_id}"
                rows.append(f"| `{tool_id}` | `{method}` | `{path}` |")
        elif op_id:
            method = (op.get("http_method") or "POST").lower()
            path = op.get("path") or f"/tools/{op_id}"
            if not path.startswith("/tools/"):
                path = f"/tools/{op_id}"
            rows.append(f"| `{op_id}` | `{method}` | `{path}` |")
    if not rows:
        return ""
    header = "| operation_id | http_method (OpenAPI verb) | path |"
    sep = "|---|---|---|"
    return (
        "\n## 📋 OPERATIONSPEC SOURCE OF TRUTH — EMIT paths: FROM THIS TABLE\n\n"
        "BASE PATH: `/tools` — every path in your output MUST begin with `/tools/`.\n"
        "For EVERY row, emit a `paths:` entry with the EXACT verb and path shown.\n"
        "The verb MUST match OperationSpec.http_method AND the CloudFormation\n"
        "`HttpMethod` for the same operation_id (HTTP_METHOD_RULE).\n\n"
        f"{header}\n{sep}\n" + "\n".join(rows) + "\n"
    )


def _build_prompt(mode, api_title, api_description, operations,
                  infrastructure_schema, modification_request, chunk_operations,
                  existing_asset="", all_tools_json=""):
    """Build the prompt string for the given mode."""
    schema_section = ""
    if infrastructure_schema:
        schema_section = f"\n## INFRASTRUCTURE SCHEMA (USE EXACT FIELD NAMES)\n{infrastructure_schema}\n"

    tools_section = ""
    if all_tools_json:
        tools_section = f"\n## ALL TOOLS (each tool = 1 API path)\n{all_tools_json}\n⚠️ Generate an API path for EACH tool. Use tool.path if provided, else /tools/{{tool_id}}.\n"

    modification_section = ""
    if modification_request:
        modification_section = f"\n⚠️ MODIFICATION REQUEST:\n{modification_request}\n\nApply this modification to the existing spec below. Do NOT rewrite from scratch.\n"
        if existing_asset:
            modification_section += f"""
## EXISTING SPEC (MODIFY THIS)
Modify this spec to fulfill the modification request. Preserve the overall structure,
paths, schemas, security definitions, and all x-amazon-connect extensions.
Only change what the modification request asks for.

```yaml
{existing_asset}
```
"""

    # Parse operations for spec-table injection (source-of-truth per-operation rows).
    try:
        _all_ops_list = json.loads(operations) if isinstance(operations, str) else operations
        if isinstance(_all_ops_list, dict):
            _all_ops_list = [_all_ops_list]
        elif not isinstance(_all_ops_list, list):
            _all_ops_list = []
    except (json.JSONDecodeError, TypeError):
        _all_ops_list = []

    if mode == "base":
        spec_table = _build_operation_spec_table(_all_ops_list)
        field_schema = build_field_schema_section(_all_ops_list)
        return f"""BASE MODE - Generate the shared OpenAPI structure only.

API Title: {api_title}
Description: {api_description}
{spec_table}{field_schema}
ALL operations (for info.description listing and ErrorResponse error codes):
{operations}
{tools_section}{schema_section}{modification_section}
Generate the base OpenAPI YAML with anchor comments for paths and schemas.
"""
    elif mode == "chunk":
        # Filter operations to only include those in this chunk
        chunk_ops_list = []
        if chunk_operations:
            try:
                chunk_ids = json.loads(chunk_operations) if isinstance(chunk_operations, str) else chunk_operations
                if isinstance(_all_ops_list, list):
                    chunk_ops_list = [op for op in _all_ops_list if op.get("operation_id") in chunk_ids]
            except (json.JSONDecodeError, TypeError):
                pass
        ops_str = json.dumps(chunk_ops_list, ensure_ascii=False, indent=2) if chunk_ops_list else operations
        spec_table = _build_operation_spec_table(chunk_ops_list or _all_ops_list)
        field_schema = build_field_schema_section(chunk_ops_list or _all_ops_list)

        return f"""CHUNK MODE - Generate paths and schemas for these operations only.

API Title: {api_title}
{spec_table}{field_schema}
Operations in this chunk:
{ops_str}
{tools_section}{schema_section}{modification_section}
Generate the YAML fragment with paths section and schemas section separated by the marker.
"""
    else:  # full
        spec_table = _build_operation_spec_table(_all_ops_list)
        field_schema = build_field_schema_section(_all_ops_list)
        return f"""Generate OpenAPI 3.0 spec for:

API Title: {api_title}
Description: {api_description}
{spec_table}{field_schema}
Operations:
{operations}
{tools_section}{schema_section}{modification_section}"""


@tool
async def openapi_generator_agent(
    api_title: str,
    api_description: str,
    operations: str = "",
    infrastructure_schema: str = "",
    modification_request: str = "",
    mode: str = "full",
    chunk_operations: str = "",
) -> AsyncIterator:
    """
    Generate OpenAPI 3.0 specification with MCP Gateway extensions.

    Args:
        api_title: Title of the API (e.g., "Hotel Reservation API")
        api_description: Description of what this API does
        operations: JSON string of operation specs. Optional - auto-loads if empty.
        infrastructure_schema: JSON string of infra schema. Optional - auto-loads if empty.
        modification_request: User's modification request for regeneration (optional)
        mode: Generation mode - "full" (complete spec), "base" (shared structure only),
              or "chunk" (batch of operations)
        chunk_operations: For mode="chunk" - JSON array of operation IDs to include in this chunk
    """
    _setup_streaming_for_subagent()

    # Auto-load operations if not provided
    if not operations:
        try:
            from tools.spec_manager import get_all_specs
            all_specs = get_all_specs()
            if all_specs:
                operations = json.dumps([s.model_dump() for s in all_specs.values()], ensure_ascii=False)
                logger.info(f"[OPENAPI] Auto-loaded {len(all_specs)} operation specs")
        except Exception as e:
            logger.warning(f"[OPENAPI] Failed to auto-load specs: {e}")

    # Auto-load all tools for multi-tool OpenAPI path generation
    all_tools_json = ""
    try:
        from tools.spec_manager import get_all_tools
        all_tools = get_all_tools()
        if all_tools:
            all_tools_json = json.dumps([t.model_dump() for t in all_tools], ensure_ascii=False)
            logger.info(f"[OPENAPI] Auto-loaded {len(all_tools)} tools (operation + session)")
    except Exception as e:
        logger.warning(f"[OPENAPI] Failed to auto-load tools: {e}")

    # Auto-load infrastructure_schema if not provided
    if not infrastructure_schema:
        try:
            from agents.infrastructure_generator.agent import get_infrastructure_schema
            schema = get_infrastructure_schema()
            if schema:
                infrastructure_schema = schema
                logger.info(f"[OPENAPI] Auto-loaded infrastructure schema")
        except Exception as e:
            logger.warning(f"[OPENAPI] Failed to auto-load infra schema: {e}")

    # Auto-load infrastructure spec for API Gateway config (base_path, stage_name, etc.)
    try:
        from tools.spec_manager import get_infrastructure_spec
        infra_spec = get_infrastructure_spec()
        if infra_spec and infra_spec.api_gateway_config:
            api_gw = infra_spec.api_gateway_config
            if api_gw.base_path and api_gw.base_path != "/tools":
                logger.info(f"[OPENAPI] Infrastructure spec base_path: {api_gw.base_path}")
    except Exception as e:
        logger.warning(f"[OPENAPI] Failed to auto-load infrastructure spec: {e}")

    _send_progress("started", api_title)
    yield {"type": "progress", "agent": "openapi_generator", "status": "started",
           "api_title": api_title}

    # Select system prompt based on mode
    if mode == "base":
        system_prompt_text = BASE_MODE_PROMPT
    elif mode == "chunk":
        system_prompt_text = CHUNK_MODE_PROMPT
    else:
        system_prompt_text = OPENAPI_GENERATOR_SYSTEM_PROMPT

    # Load existing asset for modification
    existing_asset = ""
    ws_path = ""
    modification_tools = []
    if modification_request and mode == "full":
        # Try workspace tools for full mode modification
        try:
            from tools.streaming_callback import get_session_id
            session_id = get_session_id()
            if session_id:
                from tools.workspace_tools_for_subagent import (
                    create_modification_tools,
                    WORKSPACE_TOOLS_MODIFICATION_PROMPT,
                )
                _op_id = api_title.replace(" ", "_").lower()
                modification_tools, ws_path = create_modification_tools(
                    session_id, "openapi", "openapi.yaml",
                    operation_id=_op_id,
                )
        except Exception as e:
            logger.warning(f"[OPENAPI] Failed to create modification tools: {e}")

    if modification_request and not modification_tools:
        # Fallback: legacy mode (or base/chunk mode)
        try:
            from tools.streaming_callback import get_session_id
            from tools.workspace_file_tools import read_workspace_file, get_asset_workspace_path
            session_id = get_session_id()
            if session_id:
                _fallback_op_id = api_title.replace(" ", "_").lower()
                ws_path = get_asset_workspace_path(session_id, "openapi", "openapi.yaml", operation_id=_fallback_op_id)
                ws_result = read_workspace_file(session_id=session_id, path=ws_path)
                existing_asset = ws_result["content"] if ws_result.get("success") else ""
        except Exception as e:
            logger.warning(f"[OPENAPI] Failed to load existing asset from workspace: {e}")
        if not existing_asset:
            try:
                from tools.asset_loader import load_existing_asset
                existing_asset = load_existing_asset("openapi", file_name="openapi.yaml") or ""
            except Exception as e:
                logger.warning(f"[OPENAPI] Fallback S3 load also failed: {e}")

    if modification_tools:
        from tools.workspace_tools_for_subagent import WORKSPACE_TOOLS_MODIFICATION_PROMPT
        ws_modification_section = WORKSPACE_TOOLS_MODIFICATION_PROMPT.format(
            modification_request=modification_request,
        )
        logger.info(f"[OPENAPI] Using workspace tools for modification ({len(modification_tools)} tools)")
        prompt = _build_prompt(mode, api_title, api_description, operations,
                               infrastructure_schema, "", chunk_operations,
                               existing_asset="", all_tools_json=all_tools_json)
        prompt += ws_modification_section
    else:
        prompt = _build_prompt(mode, api_title, api_description, operations,
                               infrastructure_schema, modification_request, chunk_operations,
                               existing_asset=existing_asset, all_tools_json=all_tools_json)

    # Determine file name for streamer
    op_id = api_title.replace(" ", "_").lower()
    if mode == "base":
        file_name = "openapi-base.yaml"
        suppress_complete = True
    elif mode == "chunk":
        # Use hash of chunk_operations for unique file name (parallel chunks would race on index)
        chunk_hash = abs(hash(chunk_operations)) % 1000
        file_name = f"openapi-chunk-{chunk_hash}.yaml"
        suppress_complete = True
    else:
        file_name = "openapi.yaml"
        suppress_complete = False

    try:
        model = BedrockModel(
            model_id=os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0,
            max_tokens=128000,
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        agent = Agent(
            model=model,
            system_prompt=[{"text": system_prompt_text}, {"cachePoint": {"type": "default"}}],
            tools=modification_tools if modification_tools else [],
        )

        _send_progress("running", api_title)
        yield {"type": "progress", "agent": "openapi_generator", "status": "running",
               "api_title": api_title}

        # ===== Streaming loop (same pattern as lambda_generator) =====
        from tools.incremental_streamer import IncrementalCodeStreamer
        from tools.heartbeat_utils import create_heartbeat_manager

        full_response = ""
        last_heartbeat = time.time()
        tools_were_used = False
        streamer = IncrementalCodeStreamer(
            "openapi", file_name, op_id,
            code_markers=["yaml", "yml"], flush_interval=500,
            suppress_complete=suppress_complete,
        )

        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="openapi_generator",
            project_name=api_title,
        )

        async with heartbeat:
            generator = agent.stream_async(prompt)
            try:
                async for event in generator:
                    if "data" in event:
                        chunk = event["data"]
                        full_response += chunk
                        if not modification_tools:
                            streamer.feed(chunk)
                        heartbeat.update_progress(len(full_response))
                        yield {
                            "type": "text",
                            "agent": "openapi_generator",
                            "content": chunk
                        }

                    if "current_tool_use" in event and event["current_tool_use"].get("name"):
                        tools_were_used = True

                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = time.time()
                        _send_progress("running", api_title,
                                       f"Generating... ({len(full_response)} chars)")
            finally:
                try:
                    await generator.aclose()
                except Exception:
                    pass

        if not modification_tools:
            streamer.finalize()

        # === Result processing ===
        if modification_tools and tools_were_used:
            logger.info(f"[OPENAPI] Modification completed via workspace tools for {api_title}")
            code = True  # sentinel
            parse_method = "workspace_tools"
        elif modification_request and modification_tools and not tools_were_used:
            escalation = detect_spec_escalation(full_response or "")
            if escalation is not None:
                logger.info(f"[OPENAPI] Sub-agent escalated spec_level for {api_title}: {escalation.get('reason', '')[:200]}")
                yield {
                    "type": "progress",
                    "agent": "openapi_generator",
                    "status": "escalated",
                }
                yield {
                    "success": False,
                    "api_title": api_title,
                    "mode": mode,
                    "escalation": "spec_level",
                    "reason": escalation.get("reason", ""),
                    "suggested_spec_updates": escalation.get("suggested_spec_updates", []),
                    "raw_response": (full_response[:2000] if full_response else ""),
                    "_completion_marker": "SUBAGENT_COMPLETE",
                }
                return
            # Strict patch-only mode: modification must use workspace tools, never regenerate
            logger.error(f"[OPENAPI] Modification mode did not use workspace tools for {api_title}. Refusing to regenerate from scratch.")
            yield {
                "type": "progress",
                "agent": "openapi_generator",
                "status": "error",
            }
            yield {
                "success": False,
                "api_title": api_title,
                "mode": mode,
                "error": "modification_did_not_patch",
                "summary": "Modification request did not result in workspace patches. File was not regenerated.",
                "raw_response": (full_response[:2000] if full_response else ""),
                "_completion_marker": "SUBAGENT_COMPLETE"
            }
            return
        else:
            code = None
            parse_method = None
            code = streamer.get_result()
            parse_method = "incremental_stream" if code else None
            if not code:
                code, parse_method = _parse_yaml_block(full_response)

        # ===== YIELD RESULTS =====
        if code:
            result = {"success": True, "api_title": api_title, "mode": mode,
                      "parse_method": parse_method,
                      "_completion_marker": "SUBAGENT_COMPLETE"}

            if mode == "base":
                _store_fragment(api_title, "base", code)
                result["summary"] = f"Generated base OpenAPI structure ({len(code)} chars)"

            elif mode == "chunk":
                chunk_key = chunk_operations or "unknown_chunk"
                _store_fragment(api_title, chunk_key, code)
                result["chunk_operations"] = chunk_operations
                result["summary"] = f"Generated OpenAPI chunk ({len(code)} chars)"

            else:  # full
                if parse_method == "workspace_tools":
                    pass  # Tools already handled file modification + streaming
                elif modification_request and existing_asset and ws_path:
                    # Legacy modification mode: write to workspace + emit diff
                    try:
                        from tools.streaming_callback import get_session_id
                        from tools.workspace_file_tools import write_with_diff
                        _sid = get_session_id()
                        if _sid:
                            write_with_diff(session_id=_sid, path=ws_path, new_content=code)
                            logger.info(f"[OPENAPI] Wrote modified spec to workspace with diff: {ws_path}")
                    except Exception as e:
                        logger.warning(f"[OPENAPI] write_with_diff failed, falling back to stream_asset: {e}")
                        if not streamer.found_code_block:
                            _stream_asset("openapi", "openapi.yaml", code, op_id)
                elif not streamer.found_code_block:
                    _stream_asset("openapi", "openapi.yaml", code, op_id)
                result["file_name"] = "openapi.yaml"
                result["summary"] = f"Generated OpenAPI spec for {api_title}"

            _send_progress("completed", api_title)
            yield {"type": "progress", "agent": "openapi_generator",
                   "status": "completed", "api_title": api_title}
            yield result
        else:
            logger.error(f"[OPENAPI] mode={mode} YAML parsing failed. Response length: {len(full_response)}")
            _send_progress("error", api_title)
            yield {"type": "progress", "agent": "openapi_generator",
                   "status": "error", "api_title": api_title}

            truncated = full_response[:4000] if len(full_response) > 4000 else full_response
            yield {"success": False, "api_title": api_title, "mode": mode,
                   "error": "Failed to parse YAML block",
                   "raw_response": truncated,
                   "_completion_marker": "SUBAGENT_COMPLETE"}

    except Exception as e:
        import traceback
        logger.error(f"[OPENAPI] Failed: {e}\n{traceback.format_exc()}")
        _send_progress("error", api_title)
        yield {"type": "progress", "agent": "openapi_generator",
               "status": "error", "api_title": api_title}
        yield {"success": False, "api_title": api_title, "mode": mode,
               "error": str(e), "error_type": type(e).__name__,
               "_completion_marker": "SUBAGENT_COMPLETE"}
