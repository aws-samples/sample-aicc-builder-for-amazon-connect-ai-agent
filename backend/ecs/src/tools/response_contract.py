"""The response contract every generated asset must share.

Why (live finding, SELC 2026-09-11): the OpenAPI generator (an LLM) declared
`success` / `errorCode` / `message` / `missingFields` on every response, the
Data Requests were built from the spec's `output_fields`, and the parity gate
then reported 11 mismatches that nobody could fix from either side — the
contract was being decided three times. Here it is decided once:

* the **envelope** — fields every operation response carries at the root in
  addition to the spec's `output_fields` (business outcome in a 200 body,
  because the AI agent reads nothing from a non-2xx);
* a deterministic **FieldSpec → JSON Schema** projection (nested objects and
  arrays included), so schemas are computed from the spec rather than written;
* `enforce_operation_shapes` — rewrites an OpenAPI document's request/response
  schemas to exactly that projection, keeping the generator's descriptions.
  The parity gate then holds by construction.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

#: Root-level fields of every operation response, on top of `output_fields`.
RESPONSE_ENVELOPE: list[dict] = [
    {"name": "success", "field_type": "boolean", "required": True,
     "description": "true when the business outcome succeeded; false with errorCode/message otherwise"},
    {"name": "errorCode", "field_type": "string", "required": False,
     "description": "Machine-readable outcome code when success is false (e.g. NOT_FOUND, VALIDATION_ERROR)"},
    {"name": "message", "field_type": "string", "required": False,
     "description": "Human-readable outcome message when success is false"},
]
ENVELOPE_FIELD_NAMES: frozenset[str] = frozenset(f["name"] for f in RESPONSE_ENVELOPE)

_TYPE_MAP = {
    "string": "string", "str": "string", "text": "string", "date": "string", "datetime": "string",
    "email": "string", "phone": "string", "uuid": "string", "enum": "string",
    "integer": "integer", "int": "integer",
    "number": "number", "float": "number", "decimal": "number", "money": "number", "currency": "number",
    "boolean": "boolean", "bool": "boolean",
    "array": "array", "list": "array",
    "object": "object", "dict": "object", "map": "object",
}
_FORMAT_MAP = {"date": "date", "datetime": "date-time", "email": "email", "uuid": "uuid"}


def _dump(field: Any) -> dict:
    if hasattr(field, "model_dump"):
        return field.model_dump()
    return dict(field) if isinstance(field, dict) else {}


def field_to_schema(field: Any) -> dict:
    """JSON Schema for one FieldSpec (recursive)."""
    f = _dump(field)
    raw_type = str(f.get("field_type") or f.get("type") or "string").lower()
    json_type = _TYPE_MAP.get(raw_type, "string")
    schema: dict = {"type": json_type}
    if raw_type in _FORMAT_MAP and not f.get("date_format"):
        schema["format"] = _FORMAT_MAP[raw_type]
    if f.get("description"):
        schema["description"] = f["description"]
    enum_values = f.get("enum_values") or f.get("enum")
    if enum_values:
        schema["enum"] = list(enum_values)
    pattern = f.get("pattern") or f.get("regex")
    if pattern and json_type == "string":
        schema["pattern"] = pattern
    for src, dst in (("min_length", "minLength"), ("max_length", "maxLength"),
                     ("min_value", "minimum"), ("max_value", "maximum")):
        if f.get(src) is not None:
            schema[dst] = f[src]
    example = f.get("example_value", f.get("example"))
    if example is not None:
        schema["example"] = example
    if json_type == "array":
        items = f.get("items")
        schema["items"] = field_to_schema(items) if items else {"type": "string"}
    if json_type == "object":
        schema.update(fields_to_object_schema(f.get("properties") or []))
    return schema


def fields_to_object_schema(fields: list) -> dict:
    """JSON Schema `object` whose properties are the given FieldSpecs."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for field in fields or []:
        f = _dump(field)
        name = f.get("name")
        if not name:
            continue
        properties[name] = field_to_schema(f)
        if f.get("required", True):
            required.append(name)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def with_envelope(output_fields: list) -> list[dict]:
    """Envelope + the given output fields (envelope wins on a name clash)."""
    out = [dict(f) for f in RESPONSE_ENVELOPE]
    for field in output_fields or []:
        f = _dump(field)
        if f.get("name") and f["name"] not in ENVELOPE_FIELD_NAMES:
            out.append(f)
    return out


def response_fields(spec: Any) -> list[dict]:
    """Envelope + the spec's operation-level output_fields."""
    return with_envelope(_dump(spec).get("output_fields") or [])


def response_schema(spec: Any) -> dict:
    return fields_to_object_schema(response_fields(spec))


def request_schema(spec: Any) -> dict:
    return fields_to_object_schema(_dump(spec).get("input_fields") or [])


def operation_bundles(spec: Any) -> list[dict]:
    """The API operations an OperationSpec declares, one per `tools[]` entry.

    Mirrors the parity gate: when the spec lists tools, each tool is an
    operation with its own path / method / fields; otherwise the operation
    itself is the single bundle.
    """
    s = _dump(spec)
    op_id = s.get("operation_id") or s.get("tool_id") or ""
    method = str(s.get("http_method") or "POST")
    status = str(s.get("success_status_code") or 200)
    tools = s.get("tools") or []
    if not tools:
        return [{"id": op_id, "method": method, "path": s.get("path") or f"/tools/{op_id}",
                 "input_fields": s.get("input_fields") or [], "output_fields": s.get("output_fields") or [],
                 "status": status}]
    bundles = []
    for tool in tools:
        t = _dump(tool)
        t_id = t.get("tool_id") or op_id
        bundles.append({"id": t_id, "method": str(t.get("http_method") or method),
                        "path": t.get("path") or f"/tools/{t_id}",
                        "input_fields": tool_contract_fields(s, t, "input_fields"),
                        "output_fields": tool_contract_fields(s, t, "output_fields"),
                        "status": str(t.get("success_status_code") or status)})
    return bundles


def _field_key(name: Any) -> str:
    return re.sub(r"[\s_\-]+", "", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or ""))).lower()


def tool_contract_fields(spec: dict, tool: dict, kind: str) -> list:
    """The field list a tool's API contract is generated from.

    The PRIMARY tool is the operation's own handler: its Lambda is generated
    from the operation's ``input_fields`` / ``output_fields``, so its OpenAPI
    operation and Data Request must carry the same fields. Interviews write
    the tool's lists by hand and they drift — a subset that drops ``orderDate``
    or ``errorCode`` (live: gate D9-3 and the parity gate fired on both), or a
    stale field the operation no longer has (live: ``refundAmount`` moved to the
    outputs, the Data Request still demanded it). So a primary tool whose names
    are all operation fields takes the operation's list verbatim; a primary tool
    with a field of its own, and every helper tool, keeps its own list
    (resolved through :func:`resolve_tool_fields`).
    """
    resolved = resolve_tool_fields(spec, tool.get(kind), kind)
    top = [_dump(f) for f in (spec.get(kind) or []) if f is not None]
    if str(tool.get("role") or "primary").lower() != "primary" or not top:
        return resolved
    top_keys = {_field_key(f.get("name")) for f in top if isinstance(f, dict)}
    own = [f for f in resolved if isinstance(f, dict) and _field_key(f.get("name")) not in top_keys]
    return resolved if own else top


def resolve_tool_fields(spec: dict, tool_fields: Any, kind: str) -> list:
    """A tool's field list as FieldSpec dicts.

    Interviews store a tool's ``input_fields`` / ``output_fields`` either as
    FieldSpecs or as the NAMES of the operation's top-level fields. Live (SELC):
    the names were taken as-is, projected to nothing, and the OpenAPI request
    schema was rewritten to ``properties: {}`` — thirteen fields gone. A name
    resolves to the top-level field of that name; an unknown name is kept as a
    string-typed field rather than dropped. An empty tool list falls back to the
    operation's own fields.
    """
    top = [_dump(f) for f in (spec.get(kind) or []) if f is not None]
    by_name = {str(f.get("name")): f for f in top if isinstance(f, dict) and f.get("name")}
    if not tool_fields:
        return top
    out = []
    for item in tool_fields:
        if isinstance(item, str):
            out.append(by_name.get(item) or {"name": item, "field_type": "string"})
        else:
            d = _dump(item)
            if isinstance(d, dict):
                out.append(d)
    return out


# --------------------------------------------------------------------------
# OpenAPI enforcement
# --------------------------------------------------------------------------

def _resolve(doc: dict, schema: Optional[dict]) -> Optional[dict]:
    """Follow a local $ref to the component it names (the component is rewritten in place)."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 8:
        ref = str(schema["$ref"])
        if not ref.startswith("#/"):
            return None
        node: Any = doc
        for part in ref[2:].split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        schema = node
        seen += 1
    return schema if isinstance(schema, dict) else None


def _keep_descriptions(target: dict, current: Optional[dict]) -> dict:
    """Spec schema, keeping the generator's descriptions/examples where names match."""
    if not isinstance(current, dict):
        return target
    cur_props = current.get("properties") or {}
    for name, prop in (target.get("properties") or {}).items():
        old = cur_props.get(name)
        if isinstance(old, dict):
            for key in ("description", "example"):
                if key not in prop and old.get(key) is not None:
                    prop[key] = old[key]
            if prop.get("type") == "object" and isinstance(old.get("properties"), dict):
                _keep_descriptions(prop, old)
            if prop.get("type") == "array" and isinstance(old.get("items"), dict) \
                    and isinstance(prop.get("items"), dict) and prop["items"].get("type") == "object":
                _keep_descriptions(prop["items"], old["items"])
    for key in ("description", "title"):
        if key not in target and current.get(key) is not None:
            target[key] = current[key]
    return target


def _find_operation(doc: dict, op_id: str, method: str, op_path: str) -> Optional[dict]:
    paths = doc.get("paths") or {}
    node = None
    if op_path and isinstance(paths.get(op_path), dict):
        node = paths[op_path].get(method.lower())
    if node is None:
        for _path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for _m, candidate in methods.items():
                if isinstance(candidate, dict) and candidate.get("operationId") == op_id:
                    return candidate
    return node if isinstance(node, dict) else None


def _ref_count(doc: dict, ref: str) -> int:
    import json
    return json.dumps(doc, ensure_ascii=False).count(f'"{ref}"')


def _rewrite(doc: dict, container: dict, target: dict, label: str, changes: list[str]) -> None:
    """Replace the schema under `container['schema']` (or its $ref target) with `target`.

    A component referenced by more than one operation is shared (e.g. a
    generic BusinessOutcomeResponse); it is left alone and the operation gets
    its own inline schema instead.
    """
    current = container.get("schema")
    resolved = _resolve(doc, current)
    merged = _keep_descriptions(copy.deepcopy(target), resolved)
    if isinstance(current, dict) and "$ref" in current and resolved is not None:
        name = str(current["$ref"]).split("/")[-1]
        if resolved == merged:
            return
        if _ref_count(doc, str(current["$ref"])) > 1:
            container["schema"] = merged
            changes.append(f"{label}: shared component {name} left alone; operation schema inlined from the spec")
        else:
            resolved.clear()
            resolved.update(merged)
            changes.append(f"{label}: component {name} rewritten from the spec")
    elif current != merged:
        container["schema"] = merged
        changes.append(f"{label}: schema rewritten from the spec")


def enforce_operation_shapes(openapi_doc: dict, specs: dict[str, Any]) -> tuple[dict, list[str]]:
    """Make every operation's request/response schema the spec's projection.

    `specs` maps operation_id → OperationSpec (model or dict). Operations the
    spec does not know are left alone (the reviewer reports them); operations
    the document lacks are reported, not invented.
    """
    doc = openapi_doc
    changes: list[str] = []
    bundles = [b for spec in (specs or {}).values() for b in operation_bundles(spec)]
    for bundle in bundles:
        op_id, method, op_path = bundle["id"], bundle["method"], bundle["path"]
        operation = _find_operation(doc, op_id, method, op_path)
        if operation is None:
            changes.append(f"{op_id}: operation not found in the OpenAPI document (left for review)")
            continue
        # request body
        if bundle["input_fields"]:
            body = operation.setdefault("requestBody", {"required": True, "content": {}})
            content = body.setdefault("content", {})
            entry = content.get("application/json")
            if not isinstance(entry, dict):
                entry = next((v for v in content.values() if isinstance(v, dict)), None)
                if entry is None:
                    entry = content.setdefault("application/json", {})
            _rewrite(doc, entry, fields_to_object_schema(bundle["input_fields"]), f"{op_id}.requestBody", changes)
        # success response
        status = bundle["status"]
        responses = operation.setdefault("responses", {})
        entry = responses.get(status)
        if not isinstance(entry, dict):
            # the generator keyed success under another 2xx: move it
            other = next((c for c in ("200", "201", 200, 201) if isinstance(responses.get(c), dict)), None)
            entry = responses.pop(other) if other is not None else {"description": "Success"}
            responses[status] = entry
            if other is not None and str(other) != status:
                changes.append(f"{op_id}: success response moved from {other} to {status} (spec success_status_code)")
        content = entry.setdefault("content", {})
        c_entry = content.get("application/json")
        if not isinstance(c_entry, dict):
            c_entry = next((v for v in content.values() if isinstance(v, dict)), None)
            if c_entry is None:
                c_entry = content.setdefault("application/json", {})
        _rewrite(doc, c_entry, fields_to_object_schema(with_envelope(bundle["output_fields"])),
                 f"{op_id}.responses.{status}", changes)
        # One success code. Another 2xx carrying a schema is the model's second
        # opinion on the same outcome (live: 200 = "business outcome", 201 =
        # "created"); the gateway and the agent expect exactly the spec's code.
        for code in [c for c in list(responses) if str(c).startswith("2") and str(c) != status]:
            entry = responses.pop(code)
            if isinstance(entry, dict) and entry.get("content"):
                changes.append(f"{op_id}: extra success response {code} removed (spec success_status_code is {status})")
    return doc, changes


def enforce_openapi_yaml(yaml_text: str, specs: Optional[dict[str, Any]] = None) -> tuple[str, list[str]]:
    """`enforce_operation_shapes` for a YAML document; returns (yaml, changes).

    `specs` defaults to the current session's OperationSpecs. Anything that
    prevents enforcement (no specs, unparsable YAML) leaves the text untouched
    and is reported as a single note — the gate downstream still runs.
    """
    import yaml

    if specs is None:
        try:
            from tools.spec_manager import get_all_specs
            specs = get_all_specs() or {}
        except Exception as exc:  # pragma: no cover - defensive
            return yaml_text, [f"response contract not enforced: specs unavailable ({exc})"]
    if not specs:
        return yaml_text, ["response contract not enforced: no OperationSpecs in this session"]
    try:
        doc = yaml.safe_load(yaml_text)
    except Exception as exc:
        return yaml_text, [f"response contract not enforced: YAML did not parse ({exc})"]
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        return yaml_text, ["response contract not enforced: no paths in the document"]
    doc, changes = enforce_operation_shapes(doc, specs)
    if not any("rewritten" in c or "moved" in c for c in changes):
        return yaml_text, changes
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), changes
