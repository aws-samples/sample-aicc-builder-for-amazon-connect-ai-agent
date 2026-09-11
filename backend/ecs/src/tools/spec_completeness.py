"""Spec completeness — what generation must not be asked to invent.

Every gap the interview leaves is filled independently by each generator (an
LLM), and the fills disagree: SELC's spec had no `success`, no error envelope
and no escalation payload, so the OpenAPI, Lambda and Data Request generators
each chose their own. The envelope is now a contract constant
(tools/response_contract.py); the rest must be in the spec before the
interview can be marked complete. This gate says exactly what is missing so
the orchestrator asks the customer one more question instead of letting a
generator guess.
"""
from __future__ import annotations

from typing import Any, Optional

_GENERIC_TYPES = {"enum", "enumeration", "list", "choice", "choices", "option", "options",
                  "select", "selection", "category", "custom", "code", "value", "values"}
_KNOWN_TYPES = {"string", "str", "text", "integer", "int", "number", "float", "decimal", "money", "currency",
                "boolean", "bool", "date", "datetime", "time", "email", "phone", "uuid", "array", "list",
                "object", "dict", "map", "enum"}


def _dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value) if isinstance(value, dict) else {}


def _fields(items: Any) -> list[dict]:
    return [_dump(f) for f in (items or []) if _dump(f).get("name")]


def _name_variants(name: str) -> set[str]:
    import re
    raw = str(name or "")
    snake = re.sub(r"(?<!^)([A-Z])", r"_\1", raw).lower()
    camel = re.sub(r"_+([a-zA-Z0-9])", lambda m: m.group(1).upper(), raw)
    return {raw, raw.lower(), snake, camel, camel[:1].lower() + camel[1:] if camel else camel}


def operation_spec_problems(op_id: str, spec: Any) -> list[str]:
    """Gaps in one OperationSpec that a generator would otherwise fill by guessing."""
    s = _dump(spec)
    problems: list[str] = []
    inputs, outputs = _fields(s.get("input_fields")), _fields(s.get("output_fields"))
    tools = s.get("tools") or []
    if not outputs and not any(_fields(_dump(t).get("output_fields")) for t in tools):
        problems.append(f"{op_id}: no output_fields — every generator would invent the response shape; "
                        "state what the caller gets back (names and types)")
    for scope, fields in (("input", inputs), ("output", outputs)):
        for f in fields:
            ftype = str(f.get("field_type") or f.get("type") or "").lower()
            if not ftype:
                problems.append(f"{op_id}: {scope} field {f['name']!r} has no field_type")
            elif ftype not in _KNOWN_TYPES and ftype not in _GENERIC_TYPES:
                problems.append(f"{op_id}: {scope} field {f['name']!r} has unknown field_type {ftype!r}")
            if ftype in _GENERIC_TYPES and not (f.get("enum_values") or f.get("enum")):
                problems.append(f"{op_id}: {scope} field {f['name']!r} is a {ftype} without enum_values — "
                                "list the allowed values")
            if ftype == "array" and not f.get("items"):
                problems.append(f"{op_id}: {scope} field {f['name']!r} is an array without `items` (element type)")
            if ftype in ("object", "dict", "map") and not f.get("properties"):
                problems.append(f"{op_id}: {scope} field {f['name']!r} is an object without `properties`")
    return problems


def acxd_plan_problems(flow_spec: Any, specs: dict[str, Any]) -> list[str]:
    """ACXD-only gaps: slots without a spec field, escalation without a condition or payload."""
    fs = _dump(flow_spec)
    problems: list[str] = []
    app = fs.get("application") or {}
    context_names = {str(_dump(c).get("name")) for c in (app.get("context_variables") or []) if _dump(c).get("name")}
    for flow in fs.get("flows") or []:
        f = _dump(flow)
        flow_id = f.get("flow_id") or "?"
        if f.get("role", "operation") != "operation":
            continue
        op = specs.get(f.get("operation_id"))
        op_inputs = _fields(_dump(op).get("input_fields")) if op is not None else []
        known = set()
        for field in op_inputs:
            known |= _name_variants(field["name"])
        for slot in f.get("slots") or []:
            sd = _dump(slot)
            field_name = sd.get("field_name") or sd.get("name")
            if op is None:
                problems.append(f"{flow_id}: operation {f.get('operation_id')!r} has no OperationSpec")
                break
            if not (_name_variants(field_name) & known):
                problems.append(f"{flow_id}: slot {sd.get('name')!r} maps to no input field of "
                                f"{f.get('operation_id')} — add the field to the spec or rename the slot")
        has_escalate = any(_dump(step).get("node_type") == "escalate" for step in f.get("steps") or [])
        if has_escalate:
            if not (f.get("escalation_conditions") or "").strip():
                problems.append(f"{flow_id}: has an escalate step but no escalation_conditions — "
                                "when exactly does the caller reach a human?")
            if not context_names:
                problems.append(f"{flow_id}: escalates to an agent but the application declares no "
                                "context_variables — decide what the agent sees (e.g. customerPhone, failReason)")
    return problems


def spec_completeness_problems(session_id: Optional[str] = None) -> list[str]:
    """Everything generation would have to guess. Empty = the interview is complete."""
    problems: list[str] = []
    try:
        from tools.spec_manager import get_all_specs
        specs = get_all_specs() or {}
    except Exception as exc:  # pragma: no cover
        return [f"OperationSpecs unavailable: {exc}"]
    if not specs:
        return ["no OperationSpec saved yet"]
    for op_id, spec in specs.items():
        problems.extend(operation_spec_problems(op_id, spec))
    try:
        from tools.acxd_flow_spec import get_acxd_flow_spec, is_acxd_target
        if session_id and is_acxd_target(session_id):
            flow_spec = get_acxd_flow_spec()
            if flow_spec is not None:
                problems.extend(acxd_plan_problems(flow_spec, specs))
    except Exception:  # ACXD readiness has its own gate; never block on an import here
        pass
    return problems
