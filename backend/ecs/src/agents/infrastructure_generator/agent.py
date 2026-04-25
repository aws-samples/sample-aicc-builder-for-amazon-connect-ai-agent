"""
Infrastructure Generator Sub-Agent

Generates AWS CloudFormation YAML templates for infrastructure components.
Supports three modes:
- full: Complete template in one call (legacy, backward compatible)
- base: Shared infrastructure only (DDB, S3, IAM, API GW RestApi, etc.)
- operation: Single operation fragment (Lambda + API GW Resource/Method/OPTIONS/Permission)
"""

import os
import re
import shutil
import time
import json
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator, Union
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

from .system_prompt import (
    INFRASTRUCTURE_GENERATOR_SYSTEM_PROMPT,
    BASE_MODE_PROMPT,
    OPERATION_MODE_PROMPT,
)
from tools.workspace_tools_for_subagent import detect_spec_escalation

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10
ANCHOR_COMMENT = "# --- ADDITIONAL RESOURCES ANCHOR ---"

# Per-session fragment/schema registries.  Concurrent users on the same ECS task
# must not share these dicts, so they live in session_context keyed by session_id.
from tools.session_context import (
    current_session_id,
    fragment_registry_for,
    schema_registry_for,
)


def _fragments() -> "dict[str, dict]":
    return fragment_registry_for(current_session_id.get())


def _schemas() -> "dict[str, str]":
    return schema_registry_for(current_session_id.get())


def get_infrastructure_schema(project_name: str = "") -> str:
    """Get stored infrastructure schema JSON. Returns latest if no project_name.

    Falls back to S3 ProjectWorkspace when in-memory registry is empty.
    """
    reg = _schemas()
    if project_name and project_name in reg:
        return reg[project_name]
    # Return latest stored schema from memory
    result = next(iter(reversed(reg.values())), "")
    if result:
        return result
    # S3 fallback (A4)
    try:
        from tools.project_workspace import get_workspace
        ws = get_workspace()
        if ws:
            schema_dict = ws.load_schema()
            if schema_dict:
                import json as _json
                schema_json = _json.dumps(schema_dict, ensure_ascii=False)
                key = project_name or "__default__"
                reg[key] = schema_json
                logger.info(f"[INFRA] Restored infrastructure schema from S3 workspace")
                return schema_json
    except Exception as e:
        logger.warning(f"[INFRA] S3 schema fallback failed: {e}")
    return ""


def _nfs_fragment_dir(project_name: str) -> Path | None:
    """Get NFS directory path for fragment storage. Returns None if NFS unavailable."""
    mount = os.environ.get("S3FILES_MOUNT_PATH", "")
    if not mount:
        return None
    safe_name = re.sub(r'[/\\]', '_', project_name)
    return Path(mount) / "sessions" / "_fragments" / "infrastructure" / safe_name


def _persist_fragment_to_nfs(project_name: str, key: str, content: str):
    """Persist a single fragment to NFS for crash recovery."""
    try:
        nfs_dir = _nfs_fragment_dir(project_name)
        if not nfs_dir:
            return
        os.makedirs(nfs_dir, exist_ok=True)
        target = nfs_dir / f"{key}.yaml"
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
        logger.warning(f"[INFRA] NFS persist failed for {project_name}/{key}: {e}")


def _restore_fragments_from_nfs(project_name: str) -> dict | None:
    """Restore fragments from NFS when in-memory registry is empty."""
    try:
        nfs_dir = _nfs_fragment_dir(project_name)
        if not nfs_dir or not nfs_dir.exists():
            return None
        result = {"base": None, "fragments": {}, "base_refs": ""}
        for entry in os.scandir(nfs_dir):
            if not entry.is_file() or not entry.name.endswith('.yaml'):
                continue
            key = entry.name[:-5]  # strip .yaml
            content = Path(entry.path).read_text(encoding='utf-8')
            if key == "base":
                result["base"] = content
                result["base_refs"] = _extract_base_refs(content)
            else:
                result["fragments"][key] = content
        if result["base"] is not None or result["fragments"]:
            logger.info(f"[INFRA] Restored fragments from NFS: base={result['base'] is not None}, "
                        f"fragments={list(result['fragments'].keys())}")
            return result
    except Exception as e:
        logger.warning(f"[INFRA] NFS restore failed for {project_name}: {e}")
    return None


def _clear_nfs_fragments(project_name: str):
    """Remove NFS fragment directory."""
    try:
        nfs_dir = _nfs_fragment_dir(project_name)
        if nfs_dir and nfs_dir.exists():
            shutil.rmtree(nfs_dir)
    except Exception as e:
        logger.warning(f"[INFRA] NFS clear failed for {project_name}: {e}")


def _store_fragment(project_name: str, key: str, yaml_content: str):
    """Store a YAML fragment in the registry for later merge, with NFS persistence."""
    reg = _fragments()
    if project_name not in reg:
        reg[project_name] = {"base": None, "fragments": {}, "base_refs": ""}
    if key == "base":
        reg[project_name]["base"] = yaml_content
        reg[project_name]["base_refs"] = _extract_base_refs(yaml_content)
    else:
        reg[project_name]["fragments"][key] = yaml_content
    # Persist to NFS for crash recovery
    _persist_fragment_to_nfs(project_name, key, yaml_content)


def _extract_base_refs(base_yaml: str) -> str:
    """Extract key resource references from base template for operation consistency.
    Returns a short string that operation prompts can use."""
    refs = []

    # Find DynamoDB table logical IDs
    for m in re.finditer(r'^  (\w+):\s*\n\s+Type:\s*AWS::DynamoDB::Table', base_yaml, re.MULTILINE):
        refs.append(f"DynamoDB Table: !Ref {m.group(1)}")

    # Find IAM Role logical IDs
    for m in re.finditer(r'^  (\w+):\s*\n\s+Type:\s*AWS::IAM::Role\b(?!.*Seeder|.*Retriever)', base_yaml, re.MULTILINE):
        refs.append(f"Lambda Role: !GetAtt {m.group(1)}.Arn")

    # Find RestApi logical ID
    for m in re.finditer(r'^  (\w+):\s*\n\s+Type:\s*AWS::ApiGateway::RestApi', base_yaml, re.MULTILINE):
        refs.append(f"RestApi: !Ref {m.group(1)}")
        refs.append(f"RootResourceId: !GetAtt {m.group(1)}.RootResourceId")

    # Find environment variables pattern from base (e.g., from SampleDataSeeder)
    env_vars = re.findall(r'(\w+_TABLE_NAME):\s*!Ref\s+(\w+)', base_yaml)
    for var_name, ref in env_vars:
        refs.append(f"Env var: {var_name}: !Ref {ref}")

    return "\n".join(refs) if refs else ""


def get_fragments(project_name: str) -> dict | None:
    """Get stored fragments for merge. Falls back to NFS if in-memory is empty."""
    reg = _fragments()
    result = reg.get(project_name)
    if result is not None:
        return result
    # Try NFS restore
    restored = _restore_fragments_from_nfs(project_name)
    if restored:
        reg[project_name] = restored
        return restored
    return None


def clear_fragments(project_name: str):
    """Clear stored fragments after merge (both memory and NFS)."""
    _fragments().pop(project_name, None)
    _clear_nfs_fragments(project_name)

# Callback handler lives in a ContextVar so concurrent async users don't
# overwrite each other's handler.
from tools.session_context import current_callback_handler


def set_callback_handler(handler):
    current_callback_handler.set(handler)


def get_callback_handler():
    return current_callback_handler.get()


def _setup_streaming_for_subagent():
    handler = get_callback_handler()
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
        except ImportError:
            pass


def _parse_yaml_block(text: str) -> tuple[str | None, str]:
    """Parse YAML code block from LLM output."""
    for pattern, name in [
        (r'```yaml\s*\n(.*?)```', "markdown_yaml"),
        (r'```(?:yml|YAML)\s*\n(.*?)```', "markdown_yml_variant"),
        (r'```\s*\n(.*?)```', "markdown_generic"),
    ]:
        flags = re.DOTALL | (re.IGNORECASE if "variant" in name else 0)
        match = re.search(pattern, text, flags)
        if match:
            code = match.group(1).strip()
            if name == "markdown_generic" and not any(
                k in code for k in ('AWSTemplateFormatVersion', 'AWS::', 'Resources:')
            ):
                continue
            return code, name

    # Fallback: raw YAML detection
    lines = text.strip().split('\n')
    if lines and any(
        lines[0].strip().startswith(p)
        for p in ('AWSTemplateFormatVersion', 'Description:', 'Parameters:', 'Resources:', '#')
    ):
        code_lines = []
        for line in lines:
            if line.strip().startswith(('This template', 'The above')):
                break
            code_lines.append(line)
        if code_lines:
            return '\n'.join(code_lines).strip(), "raw_yaml_detected"

    return None, "no_match"


def _stream_asset(asset_type: str, file_name: str, content: str, project_name: str = ""):
    """Save asset to S3 and stream to frontend via callback."""
    try:
        from tools.streaming_callback import stream_asset
        return stream_asset(asset_type, file_name, content, operation_id=project_name, is_complete=True)
    except Exception as e:
        logger.error(f"[INFRA_STREAM] Exception: {e}")
    return None


def _stream_asset_final(asset_type: str, file_name: str, content: str, project_name: str = ""):
    """Stream final asset in chunks, clearing cache first."""
    MAX_CHUNK = 15000
    try:
        from tools.streaming_callback import stream_asset, clear_asset_preview_cache
        clear_asset_preview_cache(asset_type, file_name, project_name)
        result = None
        for i in range(0, len(content), MAX_CHUNK):
            chunk_end = min(i + MAX_CHUNK, len(content))
            is_last = chunk_end >= len(content)
            result = stream_asset(asset_type, file_name, content[:chunk_end],
                                  operation_id=project_name, is_complete=is_last)
        return result
    except Exception as e:
        logger.error(f"[INFRA_STREAM_FINAL] Exception: {e}")
    return None


def _send_progress(agent_name: str, status: str, project_name: str = "", message: str = ""):
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": agent_name, "status": status,
                 "project_name": project_name}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


def _operation_to_logical_id(operation_id: str) -> str:
    """Convert operation_id to PascalCase. e.g., 'check_reservation' -> 'CheckReservation'"""
    return "".join(p.capitalize() for p in operation_id.replace("-", "_").split("_"))


def _build_operation_spec_table(ops_list: list) -> str:
    """Render a Markdown source-of-truth table binding operation_id → method/path/logical-id.

    Bound by HTTP_METHOD_RULE + PATH_PREFIX_RULE in _consistency_rules: the CFN
    HttpMethod, OpenAPI verb, and OperationSpec.http_method must all match, and
    the OpenAPI path is always `/tools/{operation_id}`. Injecting this table
    into the user prompt forces the generator to emit CFN FROM the spec rather
    than guess. Multi-tool operations (ToolSpecs) are enumerated row-by-row.
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
                method = (t.get("http_method") or "POST").upper()
                path = t.get("path") or f"/tools/{tool_id}"
                if not path.startswith("/tools/"):
                    path = f"/tools/{tool_id}"
                logical = _operation_to_logical_id(tool_id)
                rows.append(f"| `{tool_id}` | {method} | `{path}` | `{logical}Function` / `{logical}Method` |")
        elif op_id:
            method = (op.get("http_method") or "POST").upper()
            path = op.get("path") or f"/tools/{op_id}"
            if not path.startswith("/tools/"):
                path = f"/tools/{op_id}"
            logical = _operation_to_logical_id(op_id)
            rows.append(f"| `{op_id}` | {method} | `{path}` | `{logical}Function` / `{logical}Method` |")
    if not rows:
        return ""
    header = "| operation_id | http_method | path (OpenAPI form) | CFN logical IDs |"
    sep = "|---|---|---|---|"
    return (
        "\n## 📋 OPERATIONSPEC SOURCE OF TRUTH — GENERATE FROM THIS TABLE\n\n"
        "For EVERY row, emit a Lambda + API Gateway Resource + Method using the\n"
        "EXACT `http_method` and `path` shown. This binds OpenAPI verb, CFN\n"
        "`HttpMethod`, and `OperationSpec.http_method` (HTTP_METHOD_RULE).\n"
        "Every per-operation `AWS::ApiGateway::Resource.ParentId` must be\n"
        "`!Ref ToolsResource` (API_GATEWAY_PARENT_RULE); the `/tools/` segment\n"
        "in the path column lives under `ToolsResource`, not in the resource's\n"
        "own `PathPart`.\n\n"
        f"{header}\n{sep}\n" + "\n".join(rows) + "\n"
    )


def _create_agent(system_prompt_text: str, tools: list | None = None):
    """Create a Strands Agent with the given system prompt."""
    model_id = os.environ.get("INFRA_MODEL_ID", os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"))
    model = BedrockModel(
        model_id=model_id,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        temperature=0,
        max_tokens=128000,
        boto_client_config=BotocoreConfig(read_timeout=600),
    )
    return Agent(
        model=model,
        system_prompt=[{"text": system_prompt_text}, {"cachePoint": {"type": "default"}}],
        tools=tools if tools else [],
    )


async def _run_llm_streaming(agent, prompt: str, project_name: str, agent_label: str,
                              streamer=None, skip_streamer: bool = False) -> tuple[str, bool]:
    """Run LLM with streaming, heartbeat, and optional IncrementalCodeStreamer.
    Returns (full_response_text, tools_were_used)."""
    from tools.heartbeat_utils import create_heartbeat_manager

    heartbeat = create_heartbeat_manager(
        callback_handler=get_callback_handler(),
        agent_name="infrastructure_generator",
        project_name=project_name,
    )

    full_response = ""
    last_heartbeat = time.time()
    tools_were_used = False

    async with heartbeat:
        generator = agent.stream_async(prompt)
        try:
            async for event in generator:
                if "data" in event:
                    chunk = event["data"]
                    full_response += chunk
                    if streamer and not skip_streamer:
                        streamer.feed(chunk)
                    heartbeat.update_progress(len(full_response))

                if "current_tool_use" in event and event["current_tool_use"].get("name"):
                    tools_were_used = True

                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    last_heartbeat = time.time()
                    _send_progress("infrastructure_generator", "running", project_name,
                                   f"{agent_label}: {len(full_response)} chars")
        finally:
            try:
                await generator.aclose()
            except Exception:
                pass

    if streamer and not skip_streamer:
        streamer.finalize()

    logger.info(f"[INFRA] {agent_label} complete: {len(full_response)} chars, tools_used={tools_were_used}")
    return full_response, tools_were_used


@tool
async def infrastructure_generator_agent(
    project_name: str,
    industry: str,
    operations: Union[str, list, dict] = "",
    mode: str = "full",
    operation_id: str = "",
    db_schema: str = "",
    include_sample_data: bool = True,
    include_customer_phone_lookup: bool = False,
    modification_request: str = "",
) -> AsyncIterator:
    """
    Generate AWS CloudFormation YAML template for infrastructure components.

    Args:
        project_name: Project name for resource naming (e.g., "sunny-hotel")
        industry: Business industry type (e.g., "hospitality", "healthcare")
        operations: Operations spec - JSON string, list, or dict. Optional - if empty, auto-loads from saved operation specs.
                    For mode="operation", pass operation_id instead.
        mode: Generation mode - "full" (complete template), "base" (shared infra only),
              or "operation" (single operation fragment)
        operation_id: For mode="operation" - the operation ID to generate fragment for. The spec is auto-loaded.
        db_schema: Optional custom database schema requirements
        include_sample_data: Whether to include sample data seeding
        modification_request: User's modification request for regeneration

    Yields:
        Progress events and final completion marker.
    """
    import traceback as tb

    logger.info(f"[INFRA] ENTERED: project={project_name}, mode={mode}, industry={industry}, operation_id={operation_id}")

    # Auto-load operations from spec_manager if not provided
    if not operations:
        try:
            from tools.spec_manager import get_all_specs
            all_specs = get_all_specs()
            if mode == "operation" and operation_id:
                # Single operation mode - load just the one spec
                spec = all_specs.get(operation_id)
                if spec:
                    operations = json.dumps(spec.model_dump(), ensure_ascii=False)
                    logger.info(f"[INFRA] Auto-loaded operation spec: {operation_id}")
                else:
                    # Try to find as a tool_id within operations
                    from tools.spec_manager import get_tool_with_parent_spec
                    tool_spec, parent_spec = get_tool_with_parent_spec(operation_id)
                    if tool_spec:
                        # Convert ToolSpec to a minimal OperationSpec-like dict for the prompt
                        operations = json.dumps({
                            "operation_id": tool_spec.tool_id,
                            "summary": tool_spec.summary,
                            "http_method": tool_spec.http_method or "POST",
                            "path": tool_spec.path or f"/tools/{tool_spec.tool_id}",
                            "input_fields": [f.model_dump() for f in tool_spec.input_fields],
                            "output_fields": [f.model_dump() for f in tool_spec.output_fields],
                            "data_source": tool_spec.data_source.model_dump() if tool_spec.data_source else None,
                        }, ensure_ascii=False)
                        logger.info(f"[INFRA] Auto-loaded tool spec: {operation_id} (from parent: {parent_spec.operation_id if parent_spec else 'session'})")
                    else:
                        logger.warning(f"[INFRA] Operation/tool '{operation_id}' not found in specs, available: {list(all_specs.keys())}")
            else:
                # Base/full mode - load all specs
                operations = json.dumps([s.model_dump() for s in all_specs.values()], ensure_ascii=False)
                logger.info(f"[INFRA] Auto-loaded {len(all_specs)} operation specs from spec_manager")
        except Exception as e:
            logger.warning(f"[INFRA] Failed to auto-load specs: {e}")

    # Auto-load all tools for tool-level Lambda resource generation
    all_tools_json = ""
    try:
        from tools.spec_manager import get_all_tools
        all_tools = get_all_tools()
        if all_tools:
            all_tools_json = json.dumps([t.model_dump() for t in all_tools], ensure_ascii=False)
            logger.info(f"[INFRA] Auto-loaded {len(all_tools)} tools for Lambda resource generation")
    except Exception as e:
        logger.warning(f"[INFRA] Failed to auto-load tools: {e}")

    # Auto-load infrastructure spec (source of truth for DB type, Lambda config, API GW config)
    infra_spec_json = ""
    try:
        from tools.spec_manager import get_infrastructure_spec
        infra_spec = get_infrastructure_spec()
        if infra_spec:
            infra_spec_json = json.dumps(infra_spec.model_dump(), ensure_ascii=False)
            logger.info(f"[INFRA] Auto-loaded infrastructure spec: db_type={infra_spec.db_type}, region={infra_spec.region}")
            # Override include_sample_data and include_customer_phone_lookup from spec
            if infra_spec.dynamodb_config:
                include_sample_data = infra_spec.dynamodb_config.include_sample_data
            include_customer_phone_lookup = infra_spec.include_customer_phone_lookup
    except Exception as e:
        logger.warning(f"[INFRA] Failed to auto-load infrastructure spec: {e}")

    # Validate required params
    missing = [p for p, v in [("project_name", project_name), ("industry", industry),
                               ("operations", operations)] if not v]
    if missing:
        error_msg = f"ERROR: Missing required parameters: {', '.join(missing)}"
        logger.error(f"[INFRA] {error_msg}")
        yield {"type": "error", "agent": "infrastructure_generator", "content": error_msg}
        yield {"success": False, "error": error_msg, "_completion_marker": "SUBAGENT_COMPLETE"}
        return

    try:
        _setup_streaming_for_subagent()
    except Exception as e:
        logger.error(f"[INFRA] Setup failed: {e}")

    yield {"type": "progress", "agent": "infrastructure_generator", "status": "started",
           "project_name": project_name}

    # Normalize operations to string
    if isinstance(operations, (list, dict)):
        operations_str = json.dumps(operations, ensure_ascii=False, indent=2)
    else:
        operations_str = operations

    # Parse operations list
    try:
        ops_list = json.loads(operations_str) if isinstance(operations_str, str) else operations
        if isinstance(ops_list, dict):
            ops_list = [ops_list]
    except (json.JSONDecodeError, TypeError):
        ops_list = []

    modification_section = ""
    existing_yaml = ""
    ws_path = ""
    modification_tools = []
    if modification_request and mode == "full":
        # Try workspace tools for full mode modification only
        try:
            from tools.streaming_callback import get_session_id
            _sid = get_session_id()
            if _sid:
                from tools.workspace_tools_for_subagent import (
                    create_modification_tools,
                    WORKSPACE_TOOLS_MODIFICATION_PROMPT,
                )
                modification_tools, ws_path = create_modification_tools(
                    _sid, "cloudformation", "infrastructure.yaml",
                    operation_id=project_name,
                )
        except Exception as e:
            logger.warning(f"[INFRA] Failed to create modification tools: {e}")

    if modification_request:
        if modification_tools:
            modification_section = WORKSPACE_TOOLS_MODIFICATION_PROMPT.format(
                modification_request=modification_request,
            )
            logger.info(f"[INFRA] Using workspace tools for modification ({len(modification_tools)} tools)")
        else:
            # Fallback: legacy mode (or base/operation mode)
            try:
                from tools.streaming_callback import get_session_id
                from tools.workspace_file_tools import read_workspace_file, get_asset_workspace_path
                _sid = get_session_id()
                if _sid:
                    if mode == "operation" and operation_id:
                        ws_path = get_asset_workspace_path(_sid, "cloudformation", "infrastructure.yaml", operation_id=operation_id)
                    else:
                        ws_path = get_asset_workspace_path(_sid, "cloudformation", "infrastructure.yaml", operation_id=project_name)
                    ws_result = read_workspace_file(session_id=_sid, path=ws_path)
                    existing_yaml = ws_result["content"] if ws_result.get("success") else ""
            except Exception as e:
                logger.warning(f"[INFRA] Failed to load existing asset from workspace: {e}")
            if not existing_yaml:
                try:
                    from tools.asset_loader import load_existing_asset
                    if mode == "operation" and operation_id:
                        existing_yaml = load_existing_asset("cloudformation", operation_id=operation_id) or ""
                    if not existing_yaml:
                        existing_yaml = load_existing_asset("cloudformation", file_name="infrastructure.yaml") or ""
                except Exception as e:
                    logger.warning(f"[INFRA] Fallback S3 load also failed: {e}")

            modification_section = f"\n\n⚠️ MODIFICATION REQUEST:\n{modification_request}\n\nApply this modification to the existing template below. Do NOT rewrite from scratch.\n"
            if existing_yaml:
                modification_section += f"""
## EXISTING TEMPLATE (MODIFY THIS)
Modify this template to fulfill the modification request. Preserve the overall structure,
resource names, table definitions, GSI configurations, and all working resources.
Only change what the modification request asks for.

```yaml
{existing_yaml}
```
"""

    try:
        yield {"type": "progress", "agent": "infrastructure_generator", "status": "running",
               "project_name": project_name}

        # ===== MODE DISPATCH =====
        if mode == "base":
            code, schema_json = await _generate_base(
                project_name, industry, operations_str, ops_list,
                db_schema, include_sample_data, include_customer_phone_lookup,
                modification_section, existing_yaml=existing_yaml,
                infra_spec_json=infra_spec_json)

        elif mode == "operation":
            code = await _generate_operation(
                project_name, operations_str, ops_list,
                modification_section=modification_section, existing_yaml=existing_yaml,
                infra_spec_json=infra_spec_json)

        else:  # mode == "full" (legacy)
            code, _tools_used = await _generate_full(
                project_name, industry, operations_str,
                db_schema, include_sample_data, include_customer_phone_lookup,
                modification_section, existing_yaml=existing_yaml,
                modification_tools=modification_tools if modification_tools else None,
                infra_spec_json=infra_spec_json)
            schema_json = None

        # Spec-level escalation: surface so the orchestrator updates the spec first
        if isinstance(code, str) and code.startswith("__SPEC_ESCALATION__:"):
            import json as _json
            escalation_payload = {}
            try:
                escalation_payload = _json.loads(code[len("__SPEC_ESCALATION__:"):])
            except Exception:
                pass
            yield {"type": "progress", "agent": "infrastructure_generator",
                   "status": "escalated", "project_name": project_name}
            yield {"success": False, "project_name": project_name, "mode": mode,
                   "escalation": "spec_level",
                   "reason": escalation_payload.get("reason", ""),
                   "suggested_spec_updates": escalation_payload.get("suggested_spec_updates", []),
                   "_completion_marker": "SUBAGENT_COMPLETE"}
            return

        # Strict patch-only refusal: surface a clear error instead of the generic parse failure
        if code == "__PATCH_ONLY_REFUSED__":
            yield {"type": "progress", "agent": "infrastructure_generator",
                   "status": "error", "project_name": project_name}
            yield {"success": False, "project_name": project_name, "mode": mode,
                   "error": "modification_did_not_patch",
                   "summary": "Modification request did not result in workspace patches. Template was not regenerated.",
                   "_completion_marker": "SUBAGENT_COMPLETE"}
            return

        # ===== YIELD RESULTS =====
        if code:
            code_len = len(code) if isinstance(code, str) else 0
            logger.info(f"[INFRA] mode={mode} success: {code_len} chars")

            result = {
                "success": True,
                "project_name": project_name,
                "mode": mode,
                "_completion_marker": "SUBAGENT_COMPLETE",
            }

            if mode == "base":
                # Store in registry for merge tool to read (avoids LLM re-outputting YAML)
                _store_fragment(project_name, "base", code)
                result["summary"] = f"Generated base infrastructure template ({len(code)} chars)"
                if schema_json:
                    _schemas()[project_name] = schema_json
                    result["summary"] += " with schema"
                    # Persist schema to S3 workspace (A4)
                    try:
                        from tools.project_workspace import get_workspace
                        ws = get_workspace()
                        if ws:
                            ws.save_schema(json.loads(schema_json))
                    except Exception as _e:
                        logger.warning(f"[INFRA] Failed to persist schema to workspace: {_e}")

            elif mode == "operation":
                op_id = ops_list[0].get("operation_id", "") if ops_list else ""
                # Store in registry for merge tool to read
                _store_fragment(project_name, op_id, code)
                result["operation_id"] = op_id
                result["summary"] = f"Generated operation fragment for {op_id} ({len(code)} chars)"

            else:  # full
                # Deterministic sanitizer: strip `/tools` from ApiEndpoint if present
                try:
                    from tools.merge_infrastructure import _strip_tools_from_api_endpoint
                    code = _strip_tools_from_api_endpoint(code) if isinstance(code, str) else code
                except Exception as _e:
                    logger.warning(f"[INFRA] ApiEndpoint sanitizer skipped: {_e}")

                if _tools_used:
                    # Workspace tools already handled file modification + streaming
                    logger.info(f"[INFRA] Modification completed via workspace tools for {project_name}")
                elif modification_request and existing_yaml and ws_path:
                    # Legacy modification mode: write to workspace + emit diff
                    try:
                        from tools.streaming_callback import get_session_id as _get_sid
                        from tools.workspace_file_tools import write_with_diff
                        _sid = _get_sid()
                        if _sid:
                            write_with_diff(session_id=_sid, path=ws_path, new_content=code)
                            logger.info(f"[INFRA] Wrote modified template to workspace with diff: {ws_path}")
                    except Exception as e:
                        logger.warning(f"[INFRA] write_with_diff failed, falling back to stream_asset: {e}")
                        _stream_asset("cloudformation", "infrastructure.yaml", code, project_name)
                else:
                    # Full mode: stream directly (legacy single-call)
                    _send_progress("infrastructure_generator", "running", project_name, "Streaming to frontend...")
                    _stream_asset("cloudformation", "infrastructure.yaml", code, project_name)
                result["files_generated"] = ["infrastructure.yaml"]
                result["summary"] = f"Generated complete CloudFormation template for {project_name}"

            yield {"type": "progress", "agent": "infrastructure_generator",
                   "status": "completed", "project_name": project_name}
            yield result
        else:
            logger.error(f"[INFRA] mode={mode} YAML parsing failed")
            yield {"type": "progress", "agent": "infrastructure_generator",
                   "status": "error", "project_name": project_name}
            yield {"success": False, "project_name": project_name, "mode": mode,
                   "error": "Failed to parse YAML from response",
                   "_completion_marker": "SUBAGENT_COMPLETE"}

    except Exception as e:
        import traceback
        logger.error(f"[INFRA] Failed: {e}\n{traceback.format_exc()}")
        yield {"type": "progress", "agent": "infrastructure_generator",
               "status": "error", "project_name": project_name}
        yield {"success": False, "project_name": project_name, "mode": mode,
               "error": str(e), "error_type": type(e).__name__,
               "_completion_marker": "SUBAGENT_COMPLETE"}


# ===== MODE IMPLEMENTATIONS =====

def _phone_lookup_instruction(enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        "⚠️ include_customer_phone_lookup=true: You MUST include BOTH of these Lambda resources "
        "(see CUSTOMER PHONE LOOKUP section for exact YAML):\n"
        "1. CustomerLookupFunction (Python 3.11, placeholder) + CustomerLookupRole + CustomerLookupConnectPermission\n"
        "2. UpdateQSessionFunction (Node.js 18.x, placeholder) + UpdateQSessionRole + UpdateQSessionConnectPermission\n"
        "Do NOT skip UpdateQSessionFunction — it is REQUIRED."
    )


async def _generate_base(project_name, industry, operations_str, ops_list,
                          db_schema, include_sample_data, include_customer_phone_lookup,
                          modification_section, existing_yaml="", infra_spec_json=""):
    """Generate base template (shared infra only). Returns (yaml_code, schema_json).
    Streams to frontend as infrastructure-base.yaml for real-time preview."""
    from tools.incremental_streamer import IncrementalCodeStreamer

    # Build DependsOn list for ALL tools (1 Operation = N Tools)
    depends_on = []
    for op in ops_list:
        tools = op.get("tools", [])
        if tools:
            # Multi-tool operation: each tool gets its own API GW resources
            for tool in tools:
                logical = _operation_to_logical_id(tool.get("tool_id", ""))
                depends_on.extend([f"{logical}Method", f"{logical}Options"])
        else:
            # Legacy single-tool operation
            logical = _operation_to_logical_id(op.get("operation_id", ""))
            depends_on.extend([f"{logical}Method", f"{logical}Options"])
    depends_on_yaml = "\n".join(f"      - {dep}" for dep in depends_on)

    infra_spec_section = ""
    if infra_spec_json:
        infra_spec_section = f"""
## INFRASTRUCTURE SPEC (Source of Truth — follow this exactly)
{infra_spec_json}
"""

    spec_table = _build_operation_spec_table(ops_list)

    prompt = f"""BASE MODE - Generate shared infrastructure only.

Project Name: {project_name}
Industry: {industry}
Include Sample Data: {include_sample_data}
Include Customer Phone Lookup: {include_customer_phone_lookup}
{infra_spec_section}{spec_table}
ALL operations (for Schema Summary JSON, DependsOn, and DynamoDB design):
{operations_str}

{f"Custom Schema Requirements: {db_schema}" if db_schema else ""}{modification_section}

ApiDeployment DependsOn MUST include ALL of these:
{depends_on_yaml}

Generate the base CloudFormation YAML + Schema Summary JSON.
{_phone_lookup_instruction(include_customer_phone_lookup)}
"""

    agent = _create_agent(BASE_MODE_PROMPT)
    streamer = IncrementalCodeStreamer(
        "cloudformation", "infrastructure-base.yaml", project_name,
        code_markers=["yaml"], flush_interval=500,
        suppress_complete=True,
    )

    _send_progress("infrastructure_generator", "running", project_name,
                   "Generating base infrastructure template...")

    full_response, _ = await _run_llm_streaming(agent, prompt, project_name, "Base mode", streamer)

    # Code block parsing (apply_edits removed)
    code = streamer.get_result()
    if not code:
        code, _ = _parse_yaml_block(full_response)

    # Parse Schema Summary JSON
    schema_json = None
    json_match = re.search(r'```json\s*\n(.*?)```', full_response, re.DOTALL)
    if json_match:
        try:
            schema_json = json_match.group(1).strip()
            json.loads(schema_json)  # validate
        except json.JSONDecodeError:
            logger.warning("[INFRA_BASE] Schema JSON parse failed")
            schema_json = json_match.group(1).strip()

    return code, schema_json


async def _generate_operation(project_name, operations_str, ops_list,
                               modification_section="", existing_yaml="", infra_spec_json=""):
    """Generate a single operation YAML fragment. Returns yaml_code.
    Streams to frontend as {op_id}-fragment.yaml for real-time preview."""
    from tools.incremental_streamer import IncrementalCodeStreamer

    op = ops_list[0] if ops_list else {}
    op_id = op.get("operation_id", "unknown")

    # Get base resource references for consistency
    base_refs_section = ""
    data = _fragments().get(project_name)
    if data and data.get("base_refs"):
        base_refs_section = f"""
⚠️ CRITICAL: Use these EXACT resource references from the base template:
{data['base_refs']}

You MUST use these exact logical IDs. Do NOT invent your own names.
"""

    infra_spec_section = ""
    if infra_spec_json:
        infra_spec_section = f"""
## INFRASTRUCTURE SPEC (Source of Truth)
{infra_spec_json}
"""

    spec_table = _build_operation_spec_table(ops_list)

    prompt = f"""OPERATION MODE - Generate resources for a single operation.

Project Name: {project_name}
{infra_spec_section}{spec_table}
Operation:
{operations_str}
{base_refs_section}{modification_section}
Generate the CloudFormation YAML fragment for this operation only.
"""

    agent = _create_agent(OPERATION_MODE_PROMPT)
    streamer = IncrementalCodeStreamer(
        "cloudformation", f"{op_id}-fragment.yaml", project_name,
        code_markers=["yaml"], flush_interval=300,
        suppress_complete=True,
    )

    _send_progress("infrastructure_generator", "running", project_name,
                   f"Generating operation: {op_id}...")

    full_response, _ = await _run_llm_streaming(agent, prompt, project_name,
                                                  f"Operation {op_id}", streamer)

    # Code block parsing (apply_edits removed)
    code = streamer.get_result()
    if not code:
        code, _ = _parse_yaml_block(full_response)

    return code


async def _generate_full(project_name, industry, operations_str,
                          db_schema, include_sample_data, include_customer_phone_lookup,
                          modification_section, existing_yaml="",
                          modification_tools=None, infra_spec_json=""):
    """Legacy full mode - generate complete template in one call.
    Returns (yaml_code, tools_were_used)."""
    from tools.incremental_streamer import IncrementalCodeStreamer

    infra_spec_section = ""
    if infra_spec_json:
        infra_spec_section = f"""
## INFRASTRUCTURE SPEC (Source of Truth — follow this exactly)
{infra_spec_json}
"""

    try:
        _ops_list_full = json.loads(operations_str) if isinstance(operations_str, str) else operations_str
        if isinstance(_ops_list_full, dict):
            _ops_list_full = [_ops_list_full]
    except (json.JSONDecodeError, TypeError):
        _ops_list_full = []
    spec_table = _build_operation_spec_table(_ops_list_full)

    prompt = f"""Generate a complete CloudFormation YAML template for:

Project Name: {project_name}
Industry: {industry}
Include Sample Data: {include_sample_data}
{infra_spec_section}{spec_table}
Operations that need API endpoints:
{operations_str}

{f"Custom Schema Requirements: {db_schema}" if db_schema else ""}{modification_section}

Generate the complete infrastructure.yaml file with ALL resources.
"""

    agent = _create_agent(INFRASTRUCTURE_GENERATOR_SYSTEM_PROMPT, tools=modification_tools)
    use_tools = bool(modification_tools)
    streamer = IncrementalCodeStreamer(
        "cloudformation", "infrastructure.yaml", project_name,
        code_markers=["yaml"], flush_interval=500,
    )

    _send_progress("infrastructure_generator", "running", project_name,
                   "Generating complete infrastructure template...")

    full_response, tools_were_used = await _run_llm_streaming(
        agent, prompt, project_name, "Full mode", streamer,
        skip_streamer=use_tools,
    )

    if use_tools and tools_were_used:
        return True, True  # sentinel: tools handled everything

    if use_tools and not tools_were_used:
        escalation = detect_spec_escalation(full_response or "")
        if escalation is not None:
            logger.info(f"[INFRA] Sub-agent escalated spec_level for {project_name}: {escalation.get('reason', '')[:200]}")
            # Encode the escalation into a sentinel the caller will detect.
            import json as _json
            return f"__SPEC_ESCALATION__:{_json.dumps(escalation)}", False
        # Strict patch-only mode: modification tools were available but unused.
        # Refuse to regenerate the template from scratch.
        logger.error(f"[INFRA] Modification mode did not use workspace tools for {project_name}. Refusing to regenerate from scratch.")
        return "__PATCH_ONLY_REFUSED__", False

    # Code block parsing (apply_edits removed)
    code = streamer.get_result()
    if not code:
        code, _ = _parse_yaml_block(full_response)

    return code, False
