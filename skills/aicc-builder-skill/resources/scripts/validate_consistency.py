#!/usr/bin/env python3
"""Cross-asset consistency validator for AICC Builder skill output.

Rewritten from backend/ecs/src/tools/validate_consistency.py to operate on a
LOCAL output directory instead of S3, and to load specs from JSON files on
disk instead of the in-memory spec_manager bucket.

Directory layout expected (matches what the skill instructs Claude to write):

    <output_dir>/
      state/
        specs/
          <operation_id>.json    # one file per OperationSpec
        infrastructure_schema.json
        session_flow_config.json
      assets/
        lambda/<operation_id>/handler.py
        openapi/openapi.yaml
        infrastructure/template.yaml

Usage:
    python validate_consistency.py <output_dir>

Exits 0 if consistent, 1 if mismatches found. Prints a human-readable report.
Requires: PyYAML (pip install pyyaml).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Spec loading
# --------------------------------------------------------------------------

def _field_name(f: Any) -> Optional[str]:
    if isinstance(f, dict):
        return f.get("name")
    return None


def _field_set(fields: Any) -> Set[str]:
    if not isinstance(fields, list):
        return set()
    return {n for n in (_field_name(f) for f in fields) if n}


def load_specs(state_dir: Path) -> Dict[str, dict]:
    """Load all operation specs from state/specs/*.json."""
    specs_dir = state_dir / "specs"
    if not specs_dir.is_dir():
        return {}
    out = {}
    for p in specs_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(f"WARN: failed to parse {p}: {e}", file=sys.stderr)
            continue
        op_id = data.get("operation_id") or p.stem
        out[op_id] = data
    return out


def load_infra_schema(state_dir: Path) -> Optional[dict]:
    p = state_dir / "infrastructure_schema.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"WARN: infra schema parse failed: {e}", file=sys.stderr)
        return None


def collect_tools(specs: Dict[str, dict]) -> Dict[str, dict]:
    """Flatten ToolSpec entries across operations keyed by tool_id."""
    out = {}
    for op_id, spec in specs.items():
        tools = spec.get("tools") or []
        for t in tools:
            if isinstance(t, dict) and t.get("tool_id"):
                out[t["tool_id"]] = t
    return out


# --------------------------------------------------------------------------
# Asset extraction
# --------------------------------------------------------------------------

def _extract_lambda_fields(code: str) -> Set[str]:
    patterns = [
        r"""(?:body|event|params|data)\.get\(\s*['\"](\w+)['\"]""",
        r"""(?:body|event|params|data)\[['\"](\w+)['\"]\]""",
    ]
    fields: Set[str] = set()
    for p in patterns:
        fields.update(re.findall(p, code))
    return fields


def load_lambda_code(assets_dir: Path) -> Dict[str, str]:
    """op_id / tool_id -> handler.py contents."""
    lam_dir = assets_dir / "lambda"
    if not lam_dir.is_dir():
        return {}
    out = {}
    for handler in lam_dir.rglob("handler.py"):
        rel = handler.relative_to(lam_dir)
        op_id = rel.parts[0] if rel.parts else handler.stem
        out[op_id] = handler.read_text()
    return out


def _resolve_ref(spec: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _schema_props(spec: dict, schema: dict) -> Set[str]:
    if "$ref" in schema:
        return set(_resolve_ref(spec, schema["$ref"]).get("properties", {}).keys())
    return set(schema.get("properties", {}).keys())


def load_openapi(assets_dir: Path) -> Dict[str, Dict[str, Set[str]]]:
    """operationId -> {"input": set, "output": set}."""
    cand = list((assets_dir / "openapi").glob("*.y*ml")) if (assets_dir / "openapi").is_dir() else []
    if not cand:
        return {}
    try:
        spec = yaml.safe_load(cand[0].read_text())
    except Exception as e:
        print(f"WARN: openapi parse failed: {e}", file=sys.stderr)
        return {}
    out: Dict[str, Dict[str, Set[str]]] = {}
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or not isinstance(details, dict):
                continue
            op_id = details.get("operationId") or path
            in_f: Set[str] = set()
            out_f: Set[str] = set()
            rb = details.get("requestBody") or {}
            for _, sw in (rb.get("content") or {}).items():
                if isinstance(sw, dict) and "schema" in sw:
                    in_f |= _schema_props(spec, sw["schema"])
            for code in ("200", "201", 200, 201):
                resp = (details.get("responses") or {}).get(code) or {}
                for _, sw in (resp.get("content") or {}).items():
                    if isinstance(sw, dict) and "schema" in sw:
                        out_f |= _schema_props(spec, sw["schema"])
            out[op_id] = {"input": in_f, "output": out_f}
    return out


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def validate(output_dir: Path) -> List[dict]:
    state = output_dir / "state"
    assets = output_dir / "assets"
    specs = load_specs(state)
    infra = load_infra_schema(state)
    lam = load_lambda_code(assets)
    oapi = load_openapi(assets)
    tools = collect_tools(specs)

    if not specs:
        print("WARN: no specs found under state/specs/", file=sys.stderr)

    mismatches: List[dict] = []
    expected = {
        op_id: {
            "input": _field_set(s.get("input_fields", [])),
            "output": _field_set(s.get("output_fields", [])),
        }
        for op_id, s in specs.items()
    }

    # 1) Lambda vs spec
    for op_id, fields in expected.items():
        code = lam.get(op_id)
        if not code:
            continue
        lf = _extract_lambda_fields(code)
        resp_keys = set(re.findall(r"""['\"](\w+)['\"]\s*:""", code))
        for name in fields["input"]:
            if name not in lf:
                mismatches.append({
                    "check": "lambda_input", "operation_id": op_id, "field": name,
                    "issue": f"Spec input '{name}' not read in Lambda handler",
                })
        for name in fields["output"]:
            if name not in resp_keys:
                mismatches.append({
                    "check": "lambda_output", "operation_id": op_id, "field": name,
                    "issue": f"Spec output '{name}' not present in Lambda response body",
                })

    # 2) OpenAPI vs spec (with kebab-case fallback)
    for op_id, fields in expected.items():
        matched = oapi.get(op_id) or oapi.get("/" + op_id.replace("_", "-"))
        if not matched:
            continue
        for name in fields["input"]:
            if name not in matched["input"]:
                mismatches.append({
                    "check": "openapi_input", "operation_id": op_id, "field": name,
                    "issue": f"Spec input '{name}' missing from OpenAPI requestBody",
                })
        for name in fields["output"]:
            if name not in matched["output"]:
                mismatches.append({
                    "check": "openapi_output", "operation_id": op_id, "field": name,
                    "issue": f"Spec output '{name}' missing from OpenAPI response schema",
                })

    # 3) Infrastructure keys vs spec.data_source
    if infra:
        tables = infra.get("tables") or infra.get("dynamodb_tables") or {}
        if isinstance(tables, list):
            tables = {t.get("table_name") or t.get("tableName"): t for t in tables}
        for op_id, spec in specs.items():
            ds = spec.get("data_source") or {}
            tname = ds.get("table_name") or ds.get("tableName")
            if not tname or tname not in tables:
                continue
            tdef = tables[tname]
            infra_keys: Set[str] = set()
            for k in (tdef.get("keys") or tdef.get("key_schema") or []):
                if isinstance(k, dict):
                    infra_keys.add(k.get("attribute_name") or k.get("attributeName") or k.get("name", ""))
                elif isinstance(k, str):
                    infra_keys.add(k)
            for gsi in (tdef.get("gsi") or tdef.get("global_secondary_indexes") or tdef.get("gsi_indexes") or []):
                for k in (gsi.get("keys") or gsi.get("key_schema") or []):
                    if isinstance(k, dict):
                        infra_keys.add(k.get("attribute_name") or k.get("attributeName") or k.get("name", ""))
            pk = ds.get("primary_key") or ds.get("primaryKey") or ds.get("partition_key")
            if pk and infra_keys and pk not in infra_keys:
                mismatches.append({
                    "check": "infra_pk", "operation_id": op_id, "field": pk,
                    "issue": f"Spec primary_key '{pk}' not in infra keys for table '{tname}': {sorted(infra_keys)}",
                })

    # 4) Count parity
    if lam and len(lam) < len(expected):
        mismatches.append({
            "check": "count_lambda", "operation_id": "__all__", "field": "",
            "issue": f"Lambda count ({len(lam)}) < spec count ({len(expected)}). Missing: {set(expected) - set(lam)}",
        })
    if oapi and len(oapi) < len(expected):
        mismatches.append({
            "check": "count_openapi", "operation_id": "__all__", "field": "",
            "issue": f"OpenAPI path count ({len(oapi)}) < spec count ({len(expected)}). Missing: {set(expected) - set(oapi)}",
        })

    return mismatches


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output_dir>", file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1]).resolve()
    if not out_dir.is_dir():
        print(f"ERROR: not a directory: {out_dir}", file=sys.stderr)
        return 2
    mismatches = validate(out_dir)
    if not mismatches:
        print(f"OK — no consistency issues found in {out_dir}")
        return 0
    print(f"FAIL — {len(mismatches)} consistency issue(s) found in {out_dir}:\n")
    for m in mismatches:
        print(f"  [{m['check']:<16}] {m['operation_id']}.{m['field']}: {m['issue']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
