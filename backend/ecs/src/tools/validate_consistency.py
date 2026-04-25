"""
Parameter Consistency Validator

Validates that field names in generated assets (Lambda, OpenAPI, Prompt)
match the operation spec's input_fields/output_fields exactly.

Called by Orchestrator after Phase 3b (Prompt) and by Reviewer Agent.
"""

import re
import logging
from typing import List, Dict, Any, Optional

import yaml
from strands import tool

from .spec_manager import get_all_specs, get_all_tools
from .s3_asset_storage import list_session_assets, get_asset_from_s3

logger = logging.getLogger(__name__)


def _extract_lambda_fields(code: str) -> set:
    """Extract field names accessed via body.get/event.get/body[...] in Lambda code."""
    patterns = [
        r'''(?:body|event|params|data)\.get\(\s*['\"](\w+)['\"]''',
        r'''(?:body|event|params|data)\[['\"](\w+)['\"]\]''',
    ]
    fields = set()
    for p in patterns:
        fields.update(re.findall(p, code))
    return fields


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a $ref pointer like '#/components/schemas/Foo' to its schema dict."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = spec
    for p in parts:
        if isinstance(node, dict):
            node = node.get(p, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _get_schema_props(spec: dict, schema: dict) -> set:
    """Extract property names from a schema, resolving $ref if present."""
    if "$ref" in schema:
        resolved = _resolve_ref(spec, schema["$ref"])
        return set(resolved.get("properties", {}).keys())
    return set(schema.get("properties", {}).keys())


def _extract_openapi_fields(yaml_content: str) -> Dict[str, Dict[str, set]]:
    """Extract request/response property names per operationId from OpenAPI YAML.
    
    Returns: {operationId: {"input": set, "output": set}}
    """
    try:
        spec = yaml.safe_load(yaml_content)
    except Exception:
        return {}

    result: Dict[str, Dict[str, set]] = {}
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or not isinstance(details, dict):
                continue
            op_id = details.get("operationId", path)
            input_fields = set()
            output_fields = set()
            # Request body properties (resolve $ref)
            rb = details.get("requestBody", {})
            if isinstance(rb, dict):
                for ct, sw in rb.get("content", {}).items():
                    if isinstance(sw, dict) and "schema" in sw:
                        input_fields.update(_get_schema_props(spec, sw["schema"]))
            # Response properties (resolve $ref)
            for code in ("200", "201", 200, 201):
                resp = details.get("responses", {}).get(code, {})
                if isinstance(resp, dict):
                    for ct, sw in resp.get("content", {}).items():
                        if isinstance(sw, dict) and "schema" in sw:
                            output_fields.update(_get_schema_props(spec, sw["schema"]))
            result[op_id] = {"input": input_fields, "output": output_fields}
    return result


@tool
def validate_parameter_consistency(session_id: str) -> dict:
    """
    Validate that field names across Lambda, OpenAPI, and Prompt match the operation spec.

    Loads assets from S3 and compares field names against saved operation specs.
    Returns a report of mismatches that the Orchestrator can use to trigger re-generation.

    Args:
        session_id: Session ID to validate assets for

    Returns:
        {
            "success": True,
            "mismatches": [...],
            "summary": "Found N mismatches across M operations",
            "operations_checked": N
        }
    """
    specs = get_all_specs()
    if not specs:
        return {"success": True, "mismatches": [], "summary": "No operation specs found", "operations_checked": 0}

    # Build expected field sets per operation (backward-compatible)
    expected: Dict[str, Dict[str, set]] = {}
    for op_id, spec in specs.items():
        inp = {f.name for f in spec.input_fields if f.name}
        out = {f.name for f in spec.output_fields if f.name}
        expected[op_id] = {"input": inp, "output": out, "all": inp | out}

    # Build expected field sets per tool (multi-tool architecture)
    all_tools = get_all_tools()
    tool_expected: Dict[str, Dict[str, set]] = {}
    for tool in all_tools:
        t_inp = {f.name for f in tool.input_fields if f.name}
        t_out = {f.name for f in tool.output_fields if f.name}
        tool_expected[tool.tool_id] = {"input": t_inp, "output": t_out, "all": t_inp | t_out}

    # Load assets from S3
    asset_keys = list_session_assets(session_id) if session_id else []
    lambda_code: Dict[str, str] = {}  # op_id -> code
    openapi_yaml: Optional[str] = None
    infra_schema: Optional[str] = None

    for key in asset_keys:
        parts = key.split("/")
        if len(parts) < 3:
            continue
        asset_type = parts[2]
        if asset_type == "lambda" and key.endswith("handler.py"):
            op_id = parts[3] if len(parts) > 4 else "default"
            content = get_asset_from_s3(key)
            if content:
                lambda_code[op_id] = content
        elif asset_type == "openapi" and (key.endswith(".yaml") or key.endswith(".yml")):
            content = get_asset_from_s3(key)
            if content:
                openapi_yaml = content

    # Auto-load infrastructure schema from registry
    try:
        from agents.infrastructure_generator.agent import get_infrastructure_schema
        infra_schema = get_infrastructure_schema()
    except Exception:
        pass

    mismatches: List[Dict[str, Any]] = []
    openapi_fields: Dict[str, Dict[str, set]] = {}  # pre-declare for D1 checks below

    # Check Lambda vs spec (input + output)
    for op_id, spec_fields in expected.items():
        code = lambda_code.get(op_id)
        if not code:
            continue
        lambda_fields = _extract_lambda_fields(code)
        for field in spec_fields["input"]:
            if field not in lambda_fields:
                mismatches.append({
                    "operation_id": op_id, "field": field,
                    "asset_type": "lambda",
                    "issue": f"Spec input field '{field}' not found in Lambda handler",
                })
        # Check output fields appear in Lambda response building
        lambda_response_fields = set(re.findall(r'''['\"](\w+)['\"]\s*:''', code))
        for field in spec_fields["output"]:
            if field not in lambda_response_fields:
                mismatches.append({
                    "operation_id": op_id, "field": field,
                    "asset_type": "lambda",
                    "issue": f"Spec output field '{field}' not found in Lambda response",
                })

    # Check OpenAPI vs spec (input + output, with $ref resolution)
    if openapi_yaml:
        openapi_fields = _extract_openapi_fields(openapi_yaml)
        for op_id, spec_fields in expected.items():
            matched = openapi_fields.get(op_id)
            if not matched:
                kebab = "/" + op_id.replace("_", "-")
                matched = openapi_fields.get(kebab)
            if not matched:
                continue
            for field in spec_fields["input"]:
                if field not in matched["input"]:
                    mismatches.append({
                        "operation_id": op_id, "field": field,
                        "asset_type": "openapi",
                        "issue": f"Spec input field '{field}' not found in OpenAPI requestBody schema",
                    })
            for field in spec_fields["output"]:
                if field not in matched["output"]:
                    mismatches.append({
                        "operation_id": op_id, "field": field,
                        "asset_type": "openapi",
                        "issue": f"Spec output field '{field}' not found in OpenAPI response schema",
                    })

    # Check Infrastructure schema vs spec data_source keys
    infra_gsi_names: Dict[str, set] = {}  # table_name -> {gsi_name, ...}
    infra_env_vars: Dict[str, str] = {}  # env_var_name -> table_name
    if infra_schema:
        try:
            import json
            schema = json.loads(infra_schema) if isinstance(infra_schema, str) else infra_schema
            tables = schema.get("tables", schema.get("dynamodb_tables", {}))
            if isinstance(tables, list):
                tables = {t.get("table_name", t.get("tableName", "")): t for t in tables}
            for tbl_name, tbl_def in tables.items():
                if not isinstance(tbl_def, dict):
                    continue
                # Collect GSI names for cross-validation
                gsi_names = set()
                for gsi in tbl_def.get("gsi", tbl_def.get("global_secondary_indexes", tbl_def.get("gsi_indexes", []))):
                    if isinstance(gsi, dict):
                        gsi_name = gsi.get("index_name", gsi.get("indexName", gsi.get("name", "")))
                        if gsi_name:
                            gsi_names.add(gsi_name)
                infra_gsi_names[tbl_name] = gsi_names
                # Collect env var mappings
                env_name = tbl_def.get("env_var_name", tbl_def.get("envVarName", ""))
                if env_name:
                    infra_env_vars[env_name] = tbl_name

            for op_id, spec in specs.items():
                ds = spec.data_source
                if not ds:
                    continue
                table_name = getattr(ds, "table_name", None) or getattr(ds, "tableName", None)
                if not table_name or table_name not in tables:
                    continue
                table_def = tables[table_name]
                # Collect all key/attribute names from infra schema
                infra_keys = set()
                for k in table_def.get("keys", table_def.get("key_schema", [])):
                    if isinstance(k, dict):
                        infra_keys.add(k.get("attribute_name", k.get("attributeName", k.get("name", ""))))
                    elif isinstance(k, str):
                        infra_keys.add(k)
                for gsi in table_def.get("gsi", table_def.get("global_secondary_indexes", table_def.get("gsi_indexes", []))):
                    if isinstance(gsi, dict):
                        for k in gsi.get("keys", gsi.get("key_schema", [])):
                            if isinstance(k, dict):
                                infra_keys.add(k.get("attribute_name", k.get("attributeName", k.get("name", ""))))
                # Check spec fields exist in infra
                spec_all = expected[op_id]["all"]
                pk = getattr(ds, "primary_key", None) or getattr(ds, "primaryKey", None)
                if pk and pk not in infra_keys and infra_keys:
                    mismatches.append({
                        "operation_id": op_id, "field": pk,
                        "asset_type": "infrastructure",
                        "issue": f"Spec data_source primary_key '{pk}' not found in infra table '{table_name}' keys: {infra_keys}",
                    })
        except Exception as e:
            logger.warning(f"[VALIDATE] Failed to check infra schema: {e}")

    # D1-1: Lambda IndexName= vs infrastructure GSI name matching
    for op_id, code in lambda_code.items():
        index_names_in_code = set(re.findall(r"IndexName\s*[=:]\s*['\"](\w[\w-]*)['\"]", code))
        for idx_name in index_names_in_code:
            found_in_any_table = False
            for tbl_gsis in infra_gsi_names.values():
                if idx_name in tbl_gsis:
                    found_in_any_table = True
                    break
            if not found_in_any_table and infra_gsi_names:
                all_gsis = set()
                for g in infra_gsi_names.values():
                    all_gsis.update(g)
                mismatches.append({
                    "operation_id": op_id, "field": idx_name,
                    "asset_type": "lambda_gsi",
                    "issue": f"Lambda uses IndexName='{idx_name}' but not found in infra GSIs: {all_gsis}",
                })

    # D1-2: Lambda os.environ["X_TABLE_NAME"] vs infrastructure env_var_name matching
    for op_id, code in lambda_code.items():
        env_refs = set(re.findall(r'os\.environ\s*\[\s*[\'"](\w+_TABLE_NAME)[\'"]\s*\]', code))
        env_refs.update(re.findall(r'os\.environ\.get\s*\(\s*[\'"](\w+_TABLE_NAME)[\'"]', code))
        for env_name in env_refs:
            if infra_env_vars and env_name not in infra_env_vars:
                mismatches.append({
                    "operation_id": op_id, "field": env_name,
                    "asset_type": "lambda_env",
                    "issue": f"Lambda references env var '{env_name}' but not found in infra env vars: {set(infra_env_vars.keys())}",
                })

    # D1-3: Lambda response structure (data wrapper) vs OpenAPI response schema
    if openapi_yaml:
        for op_id, code in lambda_code.items():
            lambda_has_data_wrapper = bool(re.search(r'["\']data["\']\s*:', code))
            openapi_op = openapi_fields.get(op_id) if openapi_fields else None
            if not openapi_op:
                kebab = "/" + op_id.replace("_", "-")
                openapi_op = openapi_fields.get(kebab) if openapi_fields else None
            if openapi_op:
                openapi_has_data = "data" in openapi_op.get("output", set())
                if lambda_has_data_wrapper != openapi_has_data:
                    mismatches.append({
                        "operation_id": op_id, "field": "data",
                        "asset_type": "response_structure",
                        "issue": f"Response structure mismatch: Lambda {'uses' if lambda_has_data_wrapper else 'omits'} data wrapper, "
                                 f"OpenAPI {'has' if openapi_has_data else 'omits'} data property",
                    })

    # D1-4: Operation/tool count verification
    # When tools are defined, check tool-level counts; otherwise fall back to operation-level
    tool_count = len(tool_expected)
    spec_count = max(len(expected), tool_count)  # Use tool count if multi-tool
    lambda_count = len(lambda_code)
    openapi_count = len(openapi_fields) if openapi_fields else 0

    if tool_count > len(expected):
        # Multi-tool mode: check tool-level counts
        if lambda_count > 0 and lambda_count < tool_count:
            missing = set(tool_expected.keys()) - set(lambda_code.keys())
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"Lambda count ({lambda_count}) < tool count ({tool_count}). Missing: {missing}",
            })
        if openapi_count > 0 and openapi_count < tool_count:
            missing_openapi = set(tool_expected.keys()) - set(openapi_fields.keys()) if openapi_fields else set()
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"OpenAPI path count ({openapi_count}) < tool count ({tool_count}). Missing: {missing_openapi}",
            })
    else:
        # Legacy mode: operation-level counts
        if lambda_count > 0 and lambda_count < len(expected):
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"Lambda count ({lambda_count}) < spec count ({len(expected)}). Missing: {set(expected.keys()) - set(lambda_code.keys())}",
            })
        if openapi_count > 0 and openapi_count < len(expected):
            missing_openapi = set(expected.keys()) - set(openapi_fields.keys()) if openapi_fields else set()
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"OpenAPI path count ({openapi_count}) < spec count ({len(expected)}). Missing: {missing_openapi}",
            })

    # Additional: validate tool-level Lambda field consistency
    for tool_id, tool_fields in tool_expected.items():
        code = lambda_code.get(tool_id)
        if not code:
            continue
        lf = _extract_lambda_fields(code)
        for field in tool_fields["input"]:
            if field not in lf:
                mismatches.append({
                    "operation_id": tool_id, "field": field,
                    "asset_type": "lambda_tool",
                    "issue": f"ToolSpec input field '{field}' not found in Lambda handler for tool '{tool_id}'",
                })

    summary = f"Found {len(mismatches)} mismatches across {len(expected)} operations"
    if mismatches:
        summary += ". Fix by using patch_workspace_file for simple renames, or re-calling the affected generator with modification_request for structural changes."

    return {
        "success": len(mismatches) == 0,
        "mismatches": mismatches,
        "summary": summary,
        "operations_checked": len(expected),
    }
