#!/usr/bin/env python3
"""Deterministic spec ↔ OpenAPI shape parity validator (Phase 7 HARD GATE).

Ported verbatim from backend/ecs/src/tools/shape_parity.py, with a CLI wrapper
that reads the skill's on-disk layout instead of the in-memory spec bucket:

    <output_dir>/state/specs/*.json   ×   <output_dir>/assets/v1/openapi/openapi.yaml

Given an OperationSpec (or its model_dump()) and a parsed OpenAPI document,
walks every input_field / output_field (including nested items / properties and
multi-tool inputs/outputs) and compares to the corresponding OpenAPI schema
nodes. Reports every mismatch as a structured record.

## Why this is exhaustive

Both sides are **declarative** data structures:
  - spec side: FieldSpec trees — finite, named, typed, with declared
    `items` / `properties` / `enum_values`.
  - OpenAPI side: schema objects — finite, with `type` / `items` /
    `properties` / `enum` / `$ref`.

There is no runtime execution to analyze. Every path in the spec has a
corresponding path in the OpenAPI doc (or the absence is itself a reportable
mismatch). The comparison function walks both trees in lockstep with:
  - explicit type-mapping table (spec.field_type → openapi.type),
  - $ref resolution bounded to the same document,
  - explicit refusal of constructs the validator does not understand
    (oneOf / anyOf / allOf / external $ref) — mismatches rather than silent
    passes,
  - cycle detection via visited set.

This module intentionally does NOT validate Lambda code. AST analysis of Python
return statements cannot enumerate every code path (conditional returns, dynamic
dicts, external API passthrough). Lambda shape fidelity is covered by golden
rule 16 (LAMBDA_NESTED_RESPONSE_RULE) in resources/sub-agents/_shared_rules.md,
and the field-level checks in validate_consistency.py.

Usage:
    python shape_parity.py <output_dir>
    python shape_parity.py <output_dir> --json

Exit 0 = parity holds. Exit 1 = mismatches. Exit 2 = usage/parse error, or the
OpenAPI document uses a construct the validator refuses to reason about.
Requires: PyYAML (pip install pyyaml) — only for the CLI, not the comparison.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


# Map spec `field_type` values → the OpenAPI `type` keyword(s) that satisfy them.
# `enum` is special: satisfied by any OpenAPI node that has an `enum:` keyword
# regardless of the underlying type (typically `string`).
_SPEC_TO_OPENAPI_TYPE: dict[str, set[str]] = {
    "string":   {"string"},
    "number":   {"number"},
    "integer":  {"integer", "number"},  # accept either — ints are numbers
    "boolean":  {"boolean"},
    "date":     {"string"},
    "datetime": {"string"},
    "email":    {"string"},
    "phone":    {"string"},
    "enum":     {"string", "integer", "number", "boolean"},  # any scalar + enum keyword
    "array":    {"array"},
    "object":   {"object"},
}


@dataclass(frozen=True)
class ShapeMismatch:
    """One mismatch between spec and OpenAPI."""
    path: str          # dotted path from the operation root, e.g. "get_machine_info.output_fields.machineStatus.items.properties.state"
    reason: str        # short code, e.g. "type_mismatch", "missing_in_openapi", "enum_values_differ"
    expected: Any      # what the spec declared
    actual: Any        # what the OpenAPI schema had (or None if missing)
    detail: str = ""   # human-readable explanation

    def to_dict(self) -> dict:
        return asdict(self)


class ShapeParityError(Exception):
    """Raised for constructs the validator refuses to reason about.

    The validator must not silently pass over constructs it does not fully
    understand (oneOf / anyOf / allOf / external $ref). Raise so the caller
    knows the generated OpenAPI uses shapes this validator cannot compare
    exhaustively.
    """


def _pick_field(d: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present value among aliases."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _get_enum(field: dict) -> Optional[list]:
    return _pick_field(field, "enum_values", "enum", "allowed_values", "options")


def _get_items(field: dict) -> Optional[dict]:
    return _pick_field(field, "items", "item", "element", "elementType")


def _get_properties(field: dict) -> Optional[list]:
    """FieldSpec `properties` is a LIST of sub-FieldSpec (not a dict)."""
    return _pick_field(field, "properties", "sub_fields", "subFields", "fields")


def _get_field_type(field: dict) -> str:
    t = _pick_field(field, "field_type", "type")
    return (t or "string").lower()


def _resolve_ref(doc: dict, ref: str, seen: set) -> dict:
    """Resolve `#/components/schemas/X` within the same document."""
    if not ref.startswith("#/"):
        raise ShapeParityError(
            f"External $ref not supported by shape validator: {ref!r}. "
            "Extract the referenced schema into components/schemas of the same document."
        )
    if ref in seen:
        # Cycle — treat as satisfied at the cycle boundary (the first occurrence did the work).
        return {}
    seen = seen | {ref}
    parts = ref.lstrip("#/").split("/")
    node: Any = doc
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            raise ShapeParityError(f"Unresolvable $ref {ref!r}: segment {p!r} missing.")
        node = node[p]
    if not isinstance(node, dict):
        raise ShapeParityError(f"$ref {ref!r} does not resolve to a schema object.")
    return node


def _follow_ref(schema: dict, doc: dict, seen: set) -> dict:
    """If `schema` is a $ref, resolve it once; otherwise return as-is."""
    if not isinstance(schema, dict):
        raise ShapeParityError(f"Expected OpenAPI schema object, got {type(schema).__name__}: {schema!r}")
    ref = schema.get("$ref")
    if ref:
        return _resolve_ref(doc, ref, seen)
    return schema


def _reject_composition(schema: dict, path: str) -> None:
    """Refuse oneOf / anyOf / allOf — validator cannot exhaustively compare these."""
    for kw in ("oneOf", "anyOf", "allOf"):
        if kw in schema:
            raise ShapeParityError(
                f"OpenAPI schema at {path} uses `{kw}` — the shape validator cannot "
                "compare composition keywords exhaustively. Generate a single concrete "
                "schema instead, or extend the validator."
            )


def _compare(field: dict, schema: dict, path: str, doc: dict, seen: set, out: list) -> None:
    """Recursively compare one FieldSpec dict to one OpenAPI schema."""
    schema = _follow_ref(schema, doc, seen)
    _reject_composition(schema, path)

    spec_type = _get_field_type(field)
    openapi_type = schema.get("type")

    # Special case: spec says "enum" → any scalar OpenAPI type with an `enum` keyword is OK.
    if spec_type == "enum":
        if "enum" not in schema:
            out.append(ShapeMismatch(
                path=path, reason="missing_enum_in_openapi",
                expected={"enum_values": _get_enum(field)}, actual=schema,
                detail="Spec declared field_type='enum' but OpenAPI schema has no `enum:` keyword.",
            ))
    else:
        expected_openapi_types = _SPEC_TO_OPENAPI_TYPE.get(spec_type, {"string"})
        if openapi_type is None:
            # OpenAPI allows omitting `type` in some contexts; only tolerate if it's clearly a ref we already followed.
            out.append(ShapeMismatch(
                path=path, reason="missing_type_in_openapi",
                expected=spec_type, actual=None,
                detail=f"OpenAPI schema has no `type`. Spec field_type={spec_type!r}.",
            ))
        elif openapi_type not in expected_openapi_types:
            out.append(ShapeMismatch(
                path=path, reason="type_mismatch",
                expected=spec_type, actual=openapi_type,
                detail=f"Spec field_type={spec_type!r} does not map to OpenAPI type={openapi_type!r} "
                       f"(allowed: {sorted(expected_openapi_types)}).",
            ))

    # Enum values — compare verbatim including order.
    spec_enum = _get_enum(field)
    openapi_enum = schema.get("enum")
    if spec_enum is not None:
        if openapi_enum is None:
            out.append(ShapeMismatch(
                path=path, reason="enum_missing_in_openapi",
                expected=spec_enum, actual=None,
                detail="Spec has enum_values but OpenAPI schema has no `enum:`.",
            ))
        elif list(openapi_enum) != list(spec_enum):
            out.append(ShapeMismatch(
                path=path, reason="enum_values_differ",
                expected=spec_enum, actual=list(openapi_enum),
                detail="OpenAPI `enum` differs from spec enum_values (order / casing / set).",
            ))

    # Array: descend into items.
    spec_items = _get_items(field)
    if spec_type == "array":
        if spec_items is None:
            # Spec itself is under-specified — rule 13 forbids bare type=array without items.
            out.append(ShapeMismatch(
                path=path, reason="spec_array_missing_items",
                expected="items FieldSpec", actual=None,
                detail="Spec declares field_type='array' but has no `items`. Populate during interview.",
            ))
        else:
            openapi_items = schema.get("items")
            if openapi_items is None:
                out.append(ShapeMismatch(
                    path=path + ".items", reason="items_missing_in_openapi",
                    expected=spec_items, actual=None,
                    detail="Spec has `items` but OpenAPI schema has no `items:` block.",
                ))
            else:
                # Give the items a synthetic name if FieldSpec didn't supply one
                items_field = dict(spec_items)
                items_field.setdefault("name", "items")
                _compare(items_field, openapi_items, path + ".items", doc, seen, out)

    # Object: descend into properties.
    spec_props = _get_properties(field)
    if spec_type == "object":
        if spec_props is None:
            out.append(ShapeMismatch(
                path=path, reason="spec_object_missing_properties",
                expected="properties FieldSpec list", actual=None,
                detail="Spec declares field_type='object' but has no `properties`. Populate during interview.",
            ))
        else:
            openapi_props = schema.get("properties") or {}
            spec_prop_names = {p.get("name") for p in spec_props if p.get("name")}
            openapi_prop_names = set(openapi_props.keys())

            missing = spec_prop_names - openapi_prop_names
            for name in sorted(missing):
                out.append(ShapeMismatch(
                    path=path + f".properties.{name}", reason="property_missing_in_openapi",
                    expected=name, actual=None,
                    detail=f"Spec declares property {name!r}; OpenAPI `properties` does not contain it.",
                ))

            extra = openapi_prop_names - spec_prop_names
            for name in sorted(extra):
                out.append(ShapeMismatch(
                    path=path + f".properties.{name}", reason="property_extra_in_openapi",
                    expected=None, actual=name,
                    detail=f"OpenAPI has property {name!r} not declared by the spec.",
                ))

            for p in spec_props:
                pname = p.get("name")
                if pname and pname in openapi_props:
                    _compare(p, openapi_props[pname], path + f".properties.{pname}", doc, seen, out)


def _find_operation_schemas(openapi_doc: dict, op_id: str, method: str, op_path: str) -> tuple[Optional[dict], Optional[dict]]:
    """Locate (request schema, 200 response schema) for a given operation.

    Lookup is by path+method first, falling back to operationId match (more
    forgiving for generators that store op_id without the /tools/ prefix).
    """
    paths = openapi_doc.get("paths") or {}
    method_lower = (method or "").lower()

    op_node = None
    # Try exact path + method
    if op_path and op_path in paths:
        op_node = (paths[op_path] or {}).get(method_lower)
    # Fall back to operationId
    if op_node is None:
        for _path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for _m, node in methods.items():
                if isinstance(node, dict) and node.get("operationId") == op_id:
                    op_node = node
                    break
            if op_node:
                break

    if not op_node:
        return None, None

    # Request body schema
    req_schema = None
    rb = op_node.get("requestBody")
    if isinstance(rb, dict):
        content = rb.get("content") or {}
        for _ct, entry in content.items():
            if isinstance(entry, dict) and "schema" in entry:
                req_schema = entry["schema"]
                break

    # 200 response schema
    res_schema = None
    responses = op_node.get("responses") or {}
    for code in ("200", "201", 200, 201):
        entry = responses.get(code)
        if isinstance(entry, dict):
            content = entry.get("content") or {}
            for _ct, c_entry in content.items():
                if isinstance(c_entry, dict) and "schema" in c_entry:
                    res_schema = c_entry["schema"]
                    break
            if res_schema:
                break

    return req_schema, res_schema


def _fields_to_synthetic_object(fields: list, object_name: str) -> dict:
    """Wrap a flat list of FieldSpec dicts into a synthetic object FieldSpec.

    This lets `_compare` treat a request body (input_fields) / response body
    (output_fields) as if it were an object FieldSpec — matching how OpenAPI
    declares request/response schemas as objects whose `properties` are the
    top-level fields.
    """
    return {
        "name": object_name,
        "field_type": "object",
        "properties": fields or [],
    }


def validate_shape_parity(spec: dict, openapi_doc: dict) -> list[ShapeMismatch]:
    """Compare one OperationSpec dict to an OpenAPI document.

    Args:
        spec: OperationSpec dict (state/specs/<operation_id>.json contents).
              Must include operation_id, http_method, path, input_fields,
              output_fields, and optionally `tools[]`.
        openapi_doc: Parsed OpenAPI document (yaml.safe_load'd or json.loads'd).

    Returns:
        List of ShapeMismatch records. Empty = parity holds.

    Raises:
        ShapeParityError: when the OpenAPI document uses constructs the
            validator refuses to silently reason about (oneOf / anyOf /
            allOf / external $ref / unresolvable refs).
    """
    mismatches: list[ShapeMismatch] = []

    op_id = spec.get("operation_id") or spec.get("tool_id") or ""
    method = spec.get("http_method") or "POST"
    op_path = spec.get("path") or (f"/tools/{op_id}" if op_id else "")

    def _compare_bundle(owner_label: str, in_fields: list, out_fields: list, _op_id: str, _method: str, _path: str) -> None:
        req_schema, res_schema = _find_operation_schemas(openapi_doc, _op_id, _method, _path)
        if req_schema is None and (in_fields or []):
            mismatches.append(ShapeMismatch(
                path=f"{owner_label}.requestBody", reason="operation_missing_in_openapi",
                expected={"operation_id": _op_id, "method": _method, "path": _path},
                actual=None,
                detail="Spec declares operation but OpenAPI has no matching path+method or operationId.",
            ))
        elif in_fields:
            synthetic = _fields_to_synthetic_object(in_fields, f"{owner_label}.requestBody")
            _compare(synthetic, req_schema, f"{owner_label}.requestBody", openapi_doc, set(), mismatches)
        if res_schema is None and (out_fields or []):
            mismatches.append(ShapeMismatch(
                path=f"{owner_label}.response", reason="response_missing_in_openapi",
                expected="200 response schema", actual=None,
                detail="Spec has output_fields but OpenAPI has no 200/201 response schema.",
            ))
        elif out_fields:
            synthetic = _fields_to_synthetic_object(out_fields, f"{owner_label}.response")
            _compare(synthetic, res_schema, f"{owner_label}.response", openapi_doc, set(), mismatches)

    # Compare operation-level input/output fields (when no tools[] drives them).
    tools = spec.get("tools") or []
    if not tools:
        _compare_bundle(
            owner_label=op_id or "<anonymous>",
            in_fields=spec.get("input_fields") or [],
            out_fields=spec.get("output_fields") or [],
            _op_id=op_id, _method=method, _path=op_path,
        )
    else:
        for t in tools:
            t_id = t.get("tool_id") or ""
            t_method = t.get("http_method") or method
            t_path = t.get("path") or (f"/tools/{t_id}" if t_id else op_path)
            _compare_bundle(
                owner_label=t_id or op_id,
                in_fields=t.get("input_fields") or [],
                out_fields=t.get("output_fields") or [],
                _op_id=t_id, _method=t_method, _path=t_path,
            )

    return mismatches


def format_mismatches(mismatches: list[ShapeMismatch]) -> str:
    """Human-readable summary for injection into reviewer output."""
    if not mismatches:
        return "No shape parity violations."
    lines = [f"Found {len(mismatches)} shape parity violation(s):"]
    for m in mismatches:
        lines.append(f"  - [{m.reason}] {m.path}")
        if m.detail:
            lines.append(f"      {m.detail}")
        if m.expected is not None:
            lines.append(f"      expected: {m.expected!r}")
        if m.actual is not None:
            lines.append(f"      actual:   {m.actual!r}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI — reads the skill's on-disk layout
# --------------------------------------------------------------------------

def _assets_root(output_dir: Path) -> Path:
    """Newest ``assets/vN`` directory, falling back to a flat ``assets/``."""
    base = output_dir / "assets"
    versioned = sorted(
        (d for d in base.glob("v*") if d.is_dir() and re.fullmatch(r"v\d+", d.name)),
        key=lambda d: int(d.name[1:]),
    )
    return versioned[-1] if versioned else base


def _load_openapi_doc(assets_dir: Path) -> Optional[dict]:
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
        return None
    oapi_dir = assets_dir / "openapi"
    if not oapi_dir.is_dir():
        return None
    for p in sorted(oapi_dir.glob("*.y*ml")) + sorted(oapi_dir.glob("*.json")):
        try:
            return yaml.safe_load(p.read_text())   # safe_load also parses JSON
        except Exception as e:
            print(f"WARN: failed to parse {p}: {e}", file=sys.stderr)
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv[1:]
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <output_dir> [--json]", file=sys.stderr)
        return 2
    out_dir = Path(args[0]).resolve()
    if not out_dir.is_dir():
        print(f"ERROR: not a directory: {out_dir}", file=sys.stderr)
        return 2

    specs_dir = out_dir / "state" / "specs"
    if not specs_dir.is_dir():
        print(f"ERROR: no specs at {specs_dir} — nothing to compare.", file=sys.stderr)
        return 2

    doc = _load_openapi_doc(_assets_root(out_dir))
    if not isinstance(doc, dict):
        print(f"ERROR: no parsable OpenAPI document under "
              f"{_assets_root(out_dir) / 'openapi'}", file=sys.stderr)
        return 2

    all_mismatches: list[dict] = []
    for p in sorted(specs_dir.glob("*.json")):
        try:
            spec = json.loads(p.read_text())
        except Exception as e:
            print(f"WARN: failed to parse {p}: {e}", file=sys.stderr)
            continue
        try:
            found = validate_shape_parity(spec, doc)
        except ShapeParityError as e:
            print(f"REFUSED ({p.name}): {e}", file=sys.stderr)
            return 2
        all_mismatches.extend(m.to_dict() for m in found)

    if as_json:
        print(json.dumps({"success": not all_mismatches,
                          "mismatches": all_mismatches}, indent=2))
        return 1 if all_mismatches else 0

    if not all_mismatches:
        print(f"OK — spec ↔ OpenAPI shape parity holds for {out_dir}")
        return 0
    print(format_mismatches([ShapeMismatch(**m) for m in all_mismatches]))
    print("\nHARD GATE: fix every violation before the review gate passes. Patch the "
          "OpenAPI schema (or the spec, if the spec is what is wrong) — do not "
          "regenerate from the chat log.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
