"""
Reviewer Agent Sub-Agent

Reviews all generated assets for consistency, validates dependencies,
and provides modification suggestions.

Uses internal tools:
- lookup_assets: Retrieve assets from S3
- validate_openapi_schema: Schema validation
- check_field_consistency: Cross-asset field name validation
"""

import os
import re
import time
import logging
from typing import AsyncIterator, Dict, List, Any, Optional
from strands import Agent, tool
from strands.models import BedrockModel
from strands.agent.conversation_manager import SummarizingConversationManager
from botocore.config import Config as BotocoreConfig

from .system_prompt import REVIEWER_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Heartbeat interval to keep WebSocket connection alive
HEARTBEAT_INTERVAL_SECONDS = 5

# Callback handler lives in a ContextVar so concurrent async users don't
# overwrite each other's handler.
from tools.session_context import current_callback_handler


def set_callback_handler(handler):
    current_callback_handler.set(handler)


def get_callback_handler():
    return current_callback_handler.get()


def _setup_streaming_for_subagent():
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] reviewer_agent: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
            logger.info(f"[SUBAGENT_SETUP] reviewer_agent: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] reviewer_agent: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] reviewer_agent: handler not available or missing stream_asset_preview")


# ============================================
# Internal Tools for Reviewer Agent
# ============================================

@tool
def lookup_assets(
    session_id: str,
    asset_type: Optional[str] = None
) -> dict:
    """
    List generated assets for a session (metadata only, no content).
    Use get_asset_content to load individual asset content.

    Args:
        session_id: Session identifier
        asset_type: Optional filter (lambda, openapi, prompt, contact_flow, cloudformation)

    Returns:
        dict with asset metadata list (s3_key, type, operation_id, file_name, size)
    """
    try:
        from tools.s3_asset_storage import list_session_assets, get_s3_client, get_bucket_name

        keys = list_session_assets(session_id)
        assets = []
        bucket = get_bucket_name()
        s3 = get_s3_client() if bucket else None

        for key in keys:
            parts = key.split('/')
            if len(parts) < 4:
                continue

            a_type = parts[2]
            if asset_type and a_type != asset_type:
                continue

            op_id = None if len(parts) == 4 else parts[3]
            file_name = parts[3] if len(parts) == 4 else parts[-1]

            # Get size without downloading content — NFS first, S3 fallback
            size = 0
            try:
                from tools.s3_asset_storage import _parse_s3_key_to_nfs_components, _nfs_asset_path, _nfs_available
                sid, a_t, f_n, o_id = _parse_s3_key_to_nfs_components(key)
                if sid and _nfs_available():
                    nfs_path = _nfs_asset_path(sid, a_t, f_n, o_id)
                    if nfs_path.exists():
                        size = nfs_path.stat().st_size
            except Exception:
                pass

            if size == 0 and s3 and bucket:
                try:
                    head = s3.head_object(Bucket=bucket, Key=key)
                    size = head.get('ContentLength', 0)
                except Exception:
                    pass

            assets.append({
                "s3_key": key,
                "asset_type": a_type,
                "operation_id": op_id,
                "file_name": file_name,
                "size_bytes": size,
            })

        type_counts = {}
        for a in assets:
            type_counts[a["asset_type"]] = type_counts.get(a["asset_type"], 0) + 1

        summary_parts = [f"{c} {t}" for t, c in type_counts.items()]
        return {
            "success": True,
            "assets": assets,
            "count": len(assets),
            "summary": f"Found {len(assets)} assets: " + ", ".join(summary_parts) if summary_parts else "No assets found"
        }
    except Exception as e:
        logger.error(f"lookup_assets error: {e}")
        return {"success": False, "error": str(e), "assets": [], "count": 0}


@tool
def get_asset_content(
    s3_key: str,
) -> dict:
    """
    Load the full content of a single asset by S3 key.
    Call lookup_assets first to get the list of s3_keys.

    Args:
        s3_key: S3 key from lookup_assets result

    Returns:
        dict with asset content
    """
    try:
        from tools.s3_asset_storage import get_asset_from_s3
        content = get_asset_from_s3(s3_key, allow_binary=True)
        if not content or content.startswith("[BINARY FILE:"):
            return {"success": False, "error": "Binary or empty file", "s3_key": s3_key}
        return {
            "success": True,
            "s3_key": s3_key,
            "content": content,
            "content_length": len(content),
        }
    except Exception as e:
        return {"success": False, "error": str(e), "s3_key": s3_key}


@tool
def validate_openapi_schema(yaml_content: str) -> dict:
    """
    Validate OpenAPI YAML syntax and structure.

    Args:
        yaml_content: OpenAPI YAML content as string

    Returns:
        dict with validation results and issues
    """
    try:
        import yaml
        spec = yaml.safe_load(yaml_content)

        issues = []
        warnings = []

        # Check required top-level fields
        if "openapi" not in spec:
            issues.append("Missing 'openapi' version field")
        elif not spec.get("openapi", "").startswith("3."):
            warnings.append(f"OpenAPI version {spec.get('openapi')} - expected 3.x")

        if "info" not in spec:
            issues.append("Missing 'info' section")
        else:
            if "title" not in spec.get("info", {}):
                warnings.append("Missing 'info.title'")
            if "version" not in spec.get("info", {}):
                warnings.append("Missing 'info.version'")

        if "paths" not in spec:
            issues.append("Missing 'paths' section")
        else:
            paths = spec.get("paths", {})
            if not paths:
                issues.append("'paths' section is empty")

            # Check each path/method for MCP extensions
            for path, methods in paths.items():
                if not isinstance(methods, dict):
                    continue

                for method, details in methods.items():
                    if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                        continue

                    if not isinstance(details, dict):
                        continue

                    # Check MCP extensions
                    if "x-amazon-connect-tool-name" not in details:
                        warnings.append(f"{method.upper()} {path}: Missing x-amazon-connect-tool-name")

                    if "x-amazon-connect-tool-description" not in details:
                        warnings.append(f"{method.upper()} {path}: Missing x-amazon-connect-tool-description")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
            "paths_count": len(spec.get("paths", {})),
            "has_servers": "servers" in spec,
            "summary": f"{'Valid' if len(issues) == 0 else 'Invalid'} - {len(issues)} errors, {len(warnings)} warnings"
        }

    except yaml.YAMLError as e:
        return {
            "valid": False,
            "issues": [f"YAML parse error: {str(e)}"],
            "warnings": [],
            "summary": "Invalid YAML syntax"
        }
    except Exception as e:
        return {
            "valid": False,
            "issues": [f"Validation error: {str(e)}"],
            "warnings": [],
            "summary": f"Validation failed: {str(e)}"
        }


@tool
def validate_shape_parity_report(openapi_yaml: str) -> dict:
    """
    Deterministic spec↔OpenAPI shape parity check (FIELD_SHAPE_FIDELITY_RULE,
    ENUM_FIDELITY_RULE, NESTED_OPENAPI_SCHEMA_RULE).

    Walks every saved OperationSpec (and its tools[]), finds the matching
    requestBody / response schema in the OpenAPI doc, and verifies that:
      - field_type maps to OpenAPI `type` (array↔array, object↔object, etc.)
      - `items` recursively matches (arrays of scalars/enums/objects)
      - `properties` keys match exactly (no flattening, no renaming)
      - `enum_values` match OpenAPI `enum` verbatim (order & casing preserved)

    Refuses to silently pass for `oneOf` / `anyOf` / `allOf` / external `$ref` —
    surfaces them as a parity error so the reviewer regenerates rather than
    approving a shape it can't reason about exhaustively.

    Args:
        openapi_yaml: OpenAPI YAML content as string

    Returns:
        dict with keys:
          - success (bool): True if no mismatches and no refusals
          - total_mismatches (int)
          - by_operation (dict[operation_id → list[{path, reason, expected, actual, detail}]])
          - refused (list[str]): operation_ids where validator raised ShapeParityError
          - summary (str)
    """
    try:
        import yaml
        from tools.shape_parity import validate_shape_parity, ShapeParityError
        from tools.spec_manager import get_all_specs
    except Exception as e:
        return {"success": False, "error": f"import_failed: {e}", "summary": "validator unavailable"}

    try:
        openapi_doc = yaml.safe_load(openapi_yaml)
    except yaml.YAMLError as e:
        return {"success": False, "error": f"yaml_parse_error: {e}", "summary": "invalid YAML"}

    if not isinstance(openapi_doc, dict):
        return {"success": False, "error": "openapi_not_mapping", "summary": "OpenAPI root is not a mapping"}

    specs = get_all_specs() or {}
    by_operation: Dict[str, List[dict]] = {}
    refused: List[str] = []
    total = 0

    for op_id, spec_obj in specs.items():
        try:
            spec_dict = spec_obj.model_dump() if hasattr(spec_obj, "model_dump") else dict(spec_obj)
        except Exception as e:
            refused.append(f"{op_id}: spec_serialization_failed: {e}")
            continue

        try:
            mismatches = validate_shape_parity(spec_dict, openapi_doc)
        except ShapeParityError as e:
            refused.append(f"{op_id}: {e}")
            continue
        except Exception as e:
            refused.append(f"{op_id}: validator_error: {e}")
            continue

        if mismatches:
            by_operation[op_id] = [
                {
                    "path": m.path,
                    "reason": m.reason,
                    "expected": m.expected,
                    "actual": m.actual,
                    "detail": m.detail,
                }
                for m in mismatches
            ]
            total += len(mismatches)

    success = total == 0 and not refused
    if success:
        summary = f"shape parity OK across {len(specs)} operation(s)"
    else:
        parts = []
        if total:
            parts.append(f"{total} mismatch(es) across {len(by_operation)} op(s)")
        if refused:
            parts.append(f"{len(refused)} refusal(s) — regenerate with simpler schema")
        summary = "; ".join(parts)

    return {
        "success": success,
        "total_mismatches": total,
        "by_operation": by_operation,
        "refused": refused,
        "summary": summary,
    }


@tool
def check_field_consistency(
    lambda_code: str,
    openapi_yaml: str,
    prompt_content: str = ""
) -> dict:
    """
    Check field name consistency across Lambda, OpenAPI, and Prompt.

    Args:
        lambda_code: Lambda handler.py code
        openapi_yaml: OpenAPI YAML spec
        prompt_content: AI prompt content (optional)

    Returns:
        dict with consistency check results
    """
    import yaml

    issues = []
    lambda_fields = set()
    openapi_fields = set()
    prompt_fields = set()

    # Extract field names from Lambda (look for request.get patterns)
    lambda_patterns = [
        r"\.get\(['\"](\w+)['\"]",  # .get("field")
        r"\[['\"](\w+)['\"]\]",      # ["field"]
        r"body\.get\(['\"](\w+)['\"]",  # body.get("field")
        r"event\.get\(['\"](\w+)['\"]",  # event.get("field")
        r"params\.get\(['\"](\w+)['\"]",  # params.get("field")
    ]

    for pattern in lambda_patterns:
        matches = re.findall(pattern, lambda_code)
        lambda_fields.update(matches)

    # Filter out common non-field names
    common_excludes = {
        "body", "statusCode", "headers", "Content-Type", "application/json",
        "TABLE_NAME", "GSI_NAME", "httpMethod", "resource", "path",
        "queryStringParameters", "pathParameters", "requestContext",
        "pk", "sk", "PK", "SK", "Item", "Items", "Count"
    }
    lambda_fields = lambda_fields - common_excludes

    # Extract field names from OpenAPI
    try:
        spec = yaml.safe_load(openapi_yaml)
        for path, methods in spec.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue

            for method, details in methods.items():
                if not isinstance(details, dict):
                    continue

                # From request body
                if "requestBody" in details:
                    content = details.get("requestBody", {}).get("content", {})
                    json_schema = content.get("application/json", {}).get("schema", {})

                    if "properties" in json_schema:
                        openapi_fields.update(json_schema["properties"].keys())

                    # Check $ref if present
                    if "$ref" in json_schema:
                        ref_name = json_schema["$ref"].split("/")[-1]
                        if "components" in spec and "schemas" in spec["components"]:
                            if ref_name in spec["components"]["schemas"]:
                                schema_props = spec["components"]["schemas"][ref_name].get("properties", {})
                                openapi_fields.update(schema_props.keys())

                # From parameters
                for param in details.get("parameters", []):
                    if isinstance(param, dict) and "name" in param:
                        openapi_fields.add(param["name"])

    except Exception as e:
        issues.append(f"Failed to parse OpenAPI: {str(e)}")

    # Extract field mentions from Prompt (simpler pattern matching)
    if prompt_content:
        prompt_patterns = [
            r"(?:extract|get|ask for|collect|require)\s+(?:the\s+)?(\w+)",
            r"(\w+)\s+(?:field|parameter|value)",
            r"customer's?\s+(\w+)",
        ]
        for pattern in prompt_patterns:
            matches = re.findall(pattern, prompt_content.lower())
            prompt_fields.update(m for m in matches if len(m) > 2)

    # Check consistency
    lambda_only = lambda_fields - openapi_fields
    openapi_only = openapi_fields - lambda_fields

    # Filter out unlikely mismatches
    lambda_only = {f for f in lambda_only if len(f) > 2 and not f.startswith("_")}
    openapi_only = {f for f in openapi_only if len(f) > 2}

    if lambda_only:
        issues.append(f"Fields in Lambda but not in OpenAPI: {sorted(lambda_only)}")

    if openapi_only:
        issues.append(f"Fields in OpenAPI but not in Lambda: {sorted(openapi_only)}")

    # Check for common naming convention mismatches
    naming_issues = []
    for lf in lambda_fields:
        for of in openapi_fields:
            # Check if they might be the same field with different naming
            if lf.lower().replace("_", "") == of.lower().replace("_", ""):
                if lf != of:
                    naming_issues.append(f"Naming mismatch: Lambda uses '{lf}', OpenAPI uses '{of}'")

    return {
        "consistent": len(issues) == 0 and len(naming_issues) == 0,
        "issues": issues,
        "naming_issues": naming_issues,
        "lambda_fields": sorted(lambda_fields),
        "openapi_fields": sorted(openapi_fields),
        "prompt_fields": sorted(prompt_fields) if prompt_fields else [],
        "summary": f"{'Consistent' if len(issues) == 0 else 'Inconsistent'} - {len(issues) + len(naming_issues)} issues found"
    }


def _stream_asset(asset_type: str, file_name: str, content: str, operation_id: str):
    """Stream asset to frontend via callback."""
    try:
        from tools.streaming_callback import stream_asset, get_streaming_callback
        logger.info(f"[STREAM_ASSET] reviewer_agent calling stream_asset: {asset_type}/{file_name}, callback={get_streaming_callback() is not None}")
        stream_asset(asset_type, file_name, content, operation_id=operation_id, is_complete=True)
    except ImportError as e:
        logger.warning(f"streaming_callback not available: {e}")


# ============================================
# Main Reviewer Agent Tool
# ============================================

@tool
async def reviewer_agent(
    session_id: str,
    review_scope: str = "all",
    infrastructure_schema: str = "",
    language: str = "ko-KR",
    focus_items: str = ""
) -> AsyncIterator:
    """
    Review generated assets for consistency and validate dependencies.

    This agent should be called after all assets are generated to ensure:
    1. Field names are consistent across Lambda/OpenAPI/Prompt
    2. OpenAPI spec is valid and has required MCP extensions
    3. Lambda code references correct table/GSI names
    4. No syntax errors in generated code

    Args:
        session_id: Session identifier to look up assets
        review_scope: What to review - "all", "lambda", "openapi", "prompt", "contact_flow", "cloudformation"
        infrastructure_schema: Optional schema JSON for field name validation. Auto-loads from infrastructure registry if empty.
        language: Language for the review report (default "ko-KR")
        focus_items: Optional JSON array of specific items to re-review after fixes.
            Each item should include asset_type, operation_id (if applicable), and previous_issue describing what was wrong.
            The orchestrator provides previous_issue from the 1st review results it already has in context.
            Example: '[{"asset_type":"lambda","operation_id":"requestSuspension","previous_issue":"field mismatch: used customerId instead of phoneNumber"}]'
            When provided, ONLY these items are reviewed instead of all assets.

    Yields:
        Progress events and review findings

    Final yield:
        Complete review report with issues and recommendations
    """
    _setup_streaming_for_subagent()

    # Auto-load infrastructure_schema if not provided
    if not infrastructure_schema:
        try:
            from agents.infrastructure_generator.agent import get_infrastructure_schema
            schema = get_infrastructure_schema()
            if schema:
                infrastructure_schema = schema
                logger.info(f"[REVIEWER] Auto-loaded infrastructure schema")
        except Exception as e:
            logger.warning(f"[REVIEWER] Failed to auto-load infra schema: {e}")

    # Auto-load infrastructure spec for validation (DB type, Lambda config, API Gateway config)
    infra_spec_section = ""
    try:
        from tools.spec_manager import get_infrastructure_spec
        infra_spec = get_infrastructure_spec()
        if infra_spec:
            import json as _json
            infra_spec_section = f"\n## Infrastructure Spec (Source of Truth for Architecture)\n{_json.dumps(infra_spec.model_dump(), ensure_ascii=False)}\n"
            logger.info(f"[REVIEWER] Auto-loaded infrastructure spec: db_type={infra_spec.db_type}")
    except Exception as e:
        logger.warning(f"[REVIEWER] Failed to auto-load infrastructure spec: {e}")

    yield {
        "type": "progress",
        "agent": "reviewer_agent",
        "status": "started",
        "session_id": session_id
    }

    try:
        model = BedrockModel(
            model_id=os.environ.get("MODEL_ID", "global.anthropic.claude-opus-4-6-v1"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0.2,  # Lower temperature for analytical review
            max_tokens=128000,
            # cache_prompt removed - using cachePoint in system_prompt instead
            cache_tools="default",   # Cache tool definitions (lookup, validate, check)
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        # Internal tools for the reviewer
        internal_tools = [
            lookup_assets,
            get_asset_content,
            validate_openapi_schema,
            validate_shape_parity_report,
            check_field_consistency,
        ]

        # Add workspace tools for direct NFS file access
        try:
            from tools.workspace_file_tools import (
                read_workspace_file, list_workspace_dir,
                find_workspace_files, grep_workspace,
            )
            internal_tools.extend([read_workspace_file, list_workspace_dir, find_workspace_files, grep_workspace])
        except ImportError:
            logger.warning("[REVIEWER] workspace_file_tools not available, skipping workspace tools")

        # Add operations spec tools (for completeness checking)
        try:
            from tools.spec_manager import list_operations, get_operation_spec
            internal_tools.extend([list_operations, get_operation_spec])
        except ImportError:
            logger.warning("[REVIEWER] spec_manager not available, skipping operations tools")

        # Add parameter consistency validation tool
        try:
            from tools.validate_consistency import validate_parameter_consistency
            internal_tools.append(validate_parameter_consistency)
        except ImportError:
            logger.warning("[REVIEWER] validate_consistency not available")

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": REVIEWER_AGENT_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=internal_tools,
            callback_handler=None,  # Use stream_async instead
            conversation_manager=SummarizingConversationManager(
                summary_ratio=0.5,
                preserve_recent_messages=10,
            ),
        )

        # Build review prompt
        schema_section = ""
        if infrastructure_schema:
            schema_section = f"""

## Infrastructure Schema (Source of Truth)
The following schema defines the canonical field names and structure:

{infrastructure_schema}

Use this schema to verify that generated assets match expected field names."""

        scope_filter = ""
        if review_scope != "all":
            scope_filter = f"\nFocus primarily on reviewing: {review_scope}"

        focus_filter = ""
        if focus_items:
            focus_filter = f"""

## ⚠️ TARGETED RE-REVIEW (focus_items provided)
This is a follow-up review after fixes were applied. ONLY review these specific items:

{focus_items}

Each item includes `previous_issue` describing what was wrong before the fix.
Do NOT review other assets. For each item:
1. Load the asset with get_asset_content
2. Check whether the `previous_issue` has been fixed
3. Check cross-asset consistency for the changed fields (e.g., load OpenAPI to verify field names match)
4. Report for each item: ✅ RESOLVED or ❌ STILL PRESENT (with details)
"""

        # Build review steps based on mode
        if focus_items:
            review_steps = (
                f'1. Call lookup_assets(session_id="{session_id}") to find the specific assets listed in focus_items\n'
                "2. Load ONLY those assets with get_asset_content\n"
                "3. For each asset, verify the fix and check cross-asset consistency\n"
                "4. Compile a short focused report: RESOLVED / STILL PRESENT for each item"
            )
        else:
            review_steps = (
                "1. Call list_operations to get the expected operations (source of truth)\n"
                f'2. Call lookup_assets(session_id="{session_id}") to get the asset inventory (metadata only)\n'
                "3. **Completeness check**: Verify each operation has a Lambda, is in OpenAPI, etc.\n"
                "4. Load key assets with get_asset_content (OpenAPI, a few Lambdas, Prompt) — load selectively, not all at once\n"
                "5. For OpenAPI, use validate_openapi_schema\n"
                "6. **Spec↔OpenAPI shape parity**: call validate_shape_parity_report(openapi_yaml) — any mismatches or refusals are a HARD GATE (regeneration required)\n"
                "7. For Lambda+OpenAPI, use check_field_consistency on a representative sample\n"
                "8. Compile findings into a structured review report — include a `shape_mismatches` section listing every mismatch returned by the validator (or `none` if empty)"
            )

        lang_name = "Korean (한국어)" if language.startswith("ko") else "Japanese (日本語)" if language.startswith("ja") else "English"

        prompt = f"""Review assets for session: {session_id}
{scope_filter}{focus_filter}
{schema_section}{infra_spec_section}

**Write the entire review report in {lang_name}.**

## Review Steps
{review_steps}

Begin now."""

        yield {
            "type": "progress",
            "agent": "reviewer_agent",
            "status": "running",
            "message": "Analyzing assets..."
        }

        # Import heartbeat utilities
        from tools.heartbeat_utils import create_heartbeat_manager

        full_response = ""
        last_heartbeat = time.time()
        last_stream_len = 0
        STREAM_INTERVAL = 500  # Stream every 500 chars of new content

        # Use timestamp-based operation_id so 2nd review creates a new preview
        review_op_id = f"review-{int(time.time())}"
        review_file = "review_report.md"

        # Create heartbeat manager
        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="reviewer_agent",
            project_name="review"
        )

        async with heartbeat:
            # CRITICAL: Use explicit generator cleanup to prevent OpenTelemetry context errors
            generator = agent.stream_async(prompt)
            try:
                async for event in generator:
                    if "data" in event:
                        chunk = event["data"]
                        full_response += chunk
                        heartbeat.update_progress(len(full_response))

                        # Progressive streaming of review report
                        if len(full_response) - last_stream_len >= STREAM_INTERVAL:
                            try:
                                from tools.streaming_callback import stream_asset
                                stream_asset("review", review_file, full_response,
                                             operation_id=review_op_id, is_complete=False)
                                last_stream_len = len(full_response)
                            except Exception:
                                pass

                        yield {
                            "type": "text",
                            "agent": "reviewer_agent",
                            "content": chunk
                        }

                    if "tool_result" in event:
                        tool_result = event["tool_result"]
                        result_content = tool_result.get("content")
                        # Truncate large tool results (same pattern as research agent)
                        if isinstance(result_content, str) and len(result_content) > 8000:
                            result_content = result_content[:8000] + "... [truncated]"
                        elif isinstance(result_content, list):
                            truncated = []
                            for item in result_content:
                                if isinstance(item, dict) and "text" in item:
                                    text = item["text"]
                                    if isinstance(text, str) and len(text) > 8000:
                                        truncated.append({"text": text[:8000] + "... [truncated]"})
                                    else:
                                        truncated.append(item)
                                else:
                                    truncated.append(item)
                            result_content = truncated
                        yield {
                            "type": "tool_result",
                            "agent": "reviewer_agent",
                            "tool": tool_result.get("name"),
                            "result": result_content
                        }

                    # Periodic progress updates
                    current_time = time.time()
                    if current_time - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = current_time
                        yield {
                            "type": "progress",
                            "agent": "reviewer_agent",
                            "status": "running",
                            "message": f"Reviewing... ({len(full_response)} chars)"
                        }
            finally:
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

            # Final stream INSIDE heartbeat block (prevents WS 1006)
            _stream_asset("review", review_file, full_response, review_op_id)

        yield {
            "type": "progress",
            "agent": "reviewer_agent",
            "status": "completed"
        }

        # Count issues from response
        critical_count = full_response.count("❌")
        warning_count = full_response.count("⚠️")

        yield {
            "success": True,
            "session_id": session_id,
            "review_scope": review_scope,
            "critical_issues": critical_count,
            "warnings": warning_count,
            "report_preview": full_response[:500] if len(full_response) > 500 else full_response,
            "summary": f"Review completed. Found {critical_count} critical issues, {warning_count} warnings. See review_report.md for details.",
            "_completion_marker": "SUBAGENT_COMPLETE"
        }

    except Exception as e:
        import traceback
        error_str = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Reviewer agent failed: {error_str}")
        logger.debug(f"Traceback: {error_traceback}")

        yield {
            "type": "progress",
            "agent": "reviewer_agent",
            "status": "error"
        }

        yield {
            "success": False,
            "session_id": session_id,
            "error": error_str,
            "error_type": type(e).__name__,
            "summary": f"Review failed: {error_str[:100]}",
            "_completion_marker": "SUBAGENT_COMPLETE"
        }
