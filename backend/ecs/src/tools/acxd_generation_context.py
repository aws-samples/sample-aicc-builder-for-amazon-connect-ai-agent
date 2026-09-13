"""Adapter from Classic Full specifications to the ACXD generation contract.

ACXD is a runtime target, not a second interview.  The generation layer keeps
its compact, former ``ACXDSpec`` input shape through this adapter while all
business facts remain owned by Classic ``OperationSpec`` / ``InfrastructureSpec``
and ``ACXDFlowSpec`` holds only ACXD-specific design decisions.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from tools.acxd_flow_spec import get_acxd_flow_spec
from tools.project_workspace import ensure_workspace
from tools.session_context import current_session_id
from tools.spec_manager import get_all_specs, get_infrastructure_spec

logger = logging.getLogger(__name__)

_BUILTIN_SLOT_TYPES = {
    "text", "string", "number", "integer", "int", "boolean", "bool",
    "date", "datetime", "email", "phone",
}

#: GA namespace for built-in slot types (NLX.AlphaNumeric, NLX.PhoneNumber, ...).
#: These need no CreateSlotType call — they are the runtime's own types.
_BUILTIN_SLOT_NAMESPACE = "NLX."


def _session_id(session_id: Optional[str] = None) -> str:
    return session_id or current_session_id.get() or "default"


def _model_dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value or {}) if isinstance(value, dict) else {}


def _normalise_data_request_id(raw: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(raw or ""))
    words = re.sub(r"[^A-Za-z0-9]+", " ", words).split()
    if not words:
        return "dataRequest"
    head, *rest = words
    return head.lower() + "".join(word[:1].upper() + word[1:] for word in rest)


def _field_dict(field: Any) -> dict:
    raw = _model_dump(field)
    field_type = raw.get("field_type") or raw.get("type") or "text"
    result = {
        "name": raw.get("name"),
        "type": field_type,
        "required": bool(raw.get("required", True)),
        "description": raw.get("description"),
    }
    for source, target in (
        ("enum_values", "enum_values"),
        ("enum", "enum_values"),
        ("pattern", "regex"),
        ("regex", "regex"),          # specs edited live carry the constraint under `regex`; D9-4 reads both
        ("min_length", "min_length"),
        ("max_length", "max_length"),
        ("example_value", "example"),
        ("example", "example"),
        ("is_pii", "sensitive"),
        ("sensitive", "sensitive"),
    ):
        if raw.get(source) is not None:
            result[target] = raw[source]
    return {key: value for key, value in result.items() if value is not None}


def _business_profile(infrastructure: dict) -> dict:
    """Read the existing project workspace without introducing a second profile."""
    project: dict = {}
    try:
        workspace = ensure_workspace()
        if workspace:
            project = workspace.load_project() or {}
    except Exception as exc:  # pragma: no cover - workspace outage is non-fatal
        logger.debug("[ACXDContext] project profile load skipped: %s", exc)
    nested = project.get("business_profile") or project.get("businessProfile") or {}
    profile = dict(nested) if isinstance(nested, dict) else {}
    for key in (
        "company_name", "companyName", "industry", "description", "tone",
        "language", "primary_language", "primaryLocale",
    ):
        if project.get(key) is not None:
            profile.setdefault(key, project[key])
    if infrastructure.get("project_name"):
        profile.setdefault("project_name", infrastructure["project_name"])
        profile.setdefault("company_name", infrastructure["project_name"])
    if profile.get("companyName") and not profile.get("company_name"):
        profile["company_name"] = profile["companyName"]
    if profile.get("primary_language") and not profile.get("language"):
        profile["language"] = profile["primary_language"]
    return profile


def _resolve_schema(schema: Any, components: dict) -> dict:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return _resolve_schema(components.get(ref.rsplit("/", 1)[-1]), components)
    return schema


def _schema_fields(schema: Any, components: dict) -> list[dict]:
    schema = _resolve_schema(schema, components)
    if not isinstance(schema, dict):
        return []
    required = set(schema.get("required") or [])
    fields: list[dict] = []
    for name, raw in (schema.get("properties") or {}).items():
        prop = _resolve_schema(raw, components)
        if not isinstance(prop, dict):
            prop = {}
        field = {
            "name": name,
            "type": prop.get("type", "text"),
            "required": name in required,
            "description": prop.get("description"),
        }
        if prop.get("enum") is not None:
            field["enum_values"] = prop["enum"]
        if prop.get("pattern"):
            field["regex"] = prop["pattern"]
        if prop.get("minLength") is not None:
            field["min_length"] = prop["minLength"]
        if prop.get("maxLength") is not None:
            field["max_length"] = prop["maxLength"]
        if prop.get("example") is not None:
            field["example"] = prop["example"]
        fields.append({key: value for key, value in field.items() if value is not None})
    return fields


def _operation_contracts(openapi: dict) -> dict[str, dict]:
    """Index OpenAPI operations by operationId with request/response fields."""
    components = (openapi.get("components") or {}).get("schemas") or {}
    contracts: dict[str, dict] = {}
    for path, methods in (openapi.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(operation, dict) or not operation.get("operationId"):
                continue
            request_schema = (
                ((operation.get("requestBody") or {}).get("content") or {})
                .get("application/json", {}).get("schema")
            )
            request_fields = _schema_fields(request_schema, components)
            for parameter in operation.get("parameters") or []:
                if not isinstance(parameter, dict) or not parameter.get("name"):
                    continue
                parameter_schema = _resolve_schema(parameter.get("schema"), components)
                request_fields.append({
                    "name": parameter["name"],
                    "type": parameter_schema.get("type", "text"),
                    "required": bool(parameter.get("required")),
                    "description": parameter.get("description"),
                })

            responses = operation.get("responses") or {}
            response = next(
                (value for key, value in responses.items() if str(key).startswith("2")),
                responses.get("default") or {},
            )
            response_schema = (
                ((response.get("content") or {}).get("application/json") or {})
                .get("schema")
            ) if isinstance(response, dict) else {}
            response_schema = _resolve_schema(response_schema, components)
            data_schema = _resolve_schema(
                (response_schema.get("properties") or {}).get("data"), components
            )
            response_fields = _schema_fields(
                data_schema if data_schema else response_schema, components
            )
            contracts[operation["operationId"]] = {
                "path": path,
                "http_method": method.upper(),
                "request_fields": request_fields,
                "response_fields": response_fields,
            }
    return contracts


def _load_openapi_document(session_id: str) -> dict:
    """Read the OpenAPI asset through the shared asset storage layer."""
    try:
        from tools.s3_asset_storage import get_asset_from_s3, list_session_assets

        keys = list_session_assets(session_id)
        candidates = [
            key for key in keys
            if "/openapi/" in f"/{key}" and key.lower().endswith((".yaml", ".yml", ".json"))
        ]
        candidates.sort(key=lambda key: (not key.endswith("openapi.yaml"), key))
        for key in candidates:
            content = get_asset_from_s3(key)
            if not content:
                continue
            try:
                loaded = json.loads(content) if key.lower().endswith(".json") else yaml.safe_load(content)
            except (json.JSONDecodeError, yaml.YAMLError) as exc:
                logger.warning("[ACXDContext] invalid OpenAPI asset %s: %s", key, exc)
                continue
            if isinstance(loaded, dict) and loaded.get("paths"):
                return loaded
    except Exception as exc:  # pragma: no cover - storage outage is non-fatal
        logger.debug("[ACXDContext] OpenAPI asset load skipped: %s", exc)
    return {}


def _section(content: str, headings: tuple[str, ...]) -> str:
    pattern = r"(?:^|\n)## (?:" + "|".join(re.escape(item) for item in headings) + r")\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _article_from_content(content: str) -> Optional[dict]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("question") and parsed.get("answer"):
        return {
            "question": parsed["question"],
            "answer": parsed["answer"],
            "tags": parsed.get("tags") or parsed.get("keywords") or [],
        }
    question = _section(content, ("Question", "질문 (Question)", "질문"))
    answer = _section(content, ("Answer", "답변 (Answer)", "답변"))
    keywords = _section(content, ("Keywords", "메타데이터 (Metadata)"))
    if not question or not answer:
        return None
    tags: list[str] = []
    keyword_match = re.search(r"(?:키워드|Keywords)\s*:\s*(.+)", keywords, re.IGNORECASE)
    if keyword_match:
        tags = [item.strip() for item in keyword_match.group(1).split(",") if item.strip()]
    return {"question": question, "answer": answer, "tags": tags}


def _load_faq_articles(session_id: str) -> list[dict]:
    """Parse FAQ assets produced by the unchanged FAQ generator."""
    articles: list[dict] = []
    try:
        from tools.s3_asset_storage import get_asset_from_s3, list_session_assets

        for key in sorted(list_session_assets(session_id)):
            if "/faq/" not in f"/{key}" or not key.lower().endswith((".txt", ".md", ".json")):
                continue
            content = get_asset_from_s3(key)
            if not content:
                continue
            article = _article_from_content(content)
            if article:
                articles.append(article)
    except Exception as exc:  # pragma: no cover - storage outage is non-fatal
        logger.debug("[ACXDContext] FAQ asset load skipped: %s", exc)
    return articles


def _slot_type_id(raw: str) -> str:
    candidate = re.sub(r"[^A-Za-z]", "", raw or "")
    if len(candidate) < 3:
        candidate = f"{candidate}Value" if candidate else "CustomValue"
    return candidate[:100]


# Declared slot types that only name a *kind* of value, not one field's value
# set. Deriving the slot type id from such a name shares one slot type between
# every slot that declares it — live: three 'enum' slots (productType,
# serviceType, installLocationType) collapsed into a single 'enum' slot type
# holding all eight values, which D9-4 then rejected field by field.
_GENERIC_SLOT_TYPES = {
    "enum", "enumeration", "list", "choice", "choices", "option", "options",
    "select", "selection", "category", "custom", "code", "value", "values",
}


def slot_type_id_for(slot_name: str, declared_type: str) -> str:
    """Id of the custom slot type a flow slot deploys with.

    A built-in or generic type name ('text', 'enum', 'choice', 'NLX.Number', …)
    says nothing about the field, so the id comes from the slot name — one slot
    type per field. A specific custom type name ('OrderNumber') is kept as the
    shared id it names. D9-4 uses the same rule to find the slot type it must
    validate.
    """
    kind = str(declared_type or "").strip()
    if kind.startswith(_BUILTIN_SLOT_NAMESPACE):
        return _slot_type_id(slot_name)
    kind = kind.lower()
    if not kind or kind in _BUILTIN_SLOT_TYPES or kind in _GENERIC_SLOT_TYPES:
        return _slot_type_id(slot_name)
    return _slot_type_id(declared_type)


def _name_variants(name: str) -> set[str]:
    """camelCase / snake_case / lowercase spellings of a field or slot name."""
    raw = str(name or "")
    snake = re.sub(r"(?<!^)([A-Z])", r"_\1", raw).lower()
    camel = re.sub(r"_+([a-zA-Z0-9])", lambda m: m.group(1).upper(), raw)
    return {raw, raw.lower(), snake, camel, camel[:1].lower() + camel[1:] if camel else camel}


def _field_constraint(field: dict, *keys: str):
    for key in keys:
        if field.get(key) is not None:
            return field[key]
    return None


def _merge_fields(primary: Optional[list], secondary: Optional[list]) -> list[dict]:
    """``primary`` (the OpenAPI contract) first, then every ``secondary`` field
    (the OperationSpec) whose name — in any camel/snake spelling — is not
    already present. Never drops a field either side knows about."""
    out: list[dict] = []
    seen: set[str] = set()
    for field in list(primary or []) + list(secondary or []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        variants = _name_variants(field["name"])
        if variants & seen:
            continue
        seen |= variants
        out.append(dict(field))
    return out


def _with_envelope(fields: list) -> list[dict]:
    """Envelope (success / errorCode / message) + the given response fields."""
    from tools.response_contract import RESPONSE_ENVELOPE, ENVELOPE_FIELD_NAMES
    out = [dict(f) for f in RESPONSE_ENVELOPE]
    for field in fields or []:
        f = _field_dict(field) if not isinstance(field, dict) or "type" not in field else dict(field)
        if f.get("name") and f["name"] not in ENVELOPE_FIELD_NAMES:
            out.append(f)
    return out


#: Built-in ACXD slot types an open value attaches instead of a custom slot type.
#: S5 (live-verified): a custom slot type carries a VALUE SET, so one built from
#: a single example deploys as a one-item menu that is auto-selected without ever
#: asking the customer. Order numbers, phone numbers and free text have no value
#: set — they are a format, and a format is an ``NLX.*`` built-in plus a regex.
NLX_ALPHANUMERIC = "NLX.AlphaNumeric"
NLX_NUMBER = "NLX.Number"
NLX_PHONE_NUMBER = "NLX.PhoneNumber"
NLX_TEXT = "NLX.Text"

#: A name is a phone NUMBER only when it ENDS in a phone-ish token: 'customerPhone'
#: and 'phoneNumber' are phone numbers, 'phonePin' and 'phoneModel' are not — they
#: merely mention a phone, and NLX.PhoneNumber would normalize them wrongly.
_PHONE_NAME_RE = re.compile(
    r"(?:phone|mobile|cell|cellular|tel|telephone|msisdn)(?:number|num|no)?$")
_NUMERIC_TYPES = {"number", "integer", "int", "float", "decimal"}
_DIGITS_ONLY_RE = re.compile(r"^\^?(?:\\d|\[0-9\])[^A-Za-z]*\$?$")


def builtin_slot_type_for(slot_name: str, declared_type: str, regex: Optional[str]) -> str:
    """The ``NLX.*`` built-in an open (non-enumerated) value should attach.

    Order matters: a phone number is a phone number even when its regex is all
    digits. Any value with an explicit format — an order number, a booking
    reference, a 10-digit code — attaches ``NLX.AlphaNumeric`` and carries the
    regex: that is the shape the live SELC bundle needed (``NLX.Number`` would
    parse the value as a quantity, losing leading zeros and the exact length
    the regex enforces). ``NLX.Number`` is for a quantity-like numeric field
    with no format of its own.
    """
    name = re.sub(r"[^A-Za-z0-9]", "", str(slot_name or "")).lower()
    kind = str(declared_type or "").strip().lower()
    pattern = str(regex or "")
    if _PHONE_NAME_RE.search(name) or kind in {"phone", "phonenumber", "phone_number"}:
        return NLX_PHONE_NUMBER
    if pattern:
        return NLX_ALPHANUMERIC
    if kind in _NUMERIC_TYPES:
        return NLX_NUMBER
    return NLX_TEXT


def _derive_slot_types(flow_plans: list[dict], operations: dict[str, Any]) -> list[dict]:
    """Derive custom ACXD slot types from flow slots and FieldSpec constraints.

    A field with an ``enum`` is a value set → one custom slot type per field.
    A field with only format constraints (regex / length) is an OPEN value → no
    custom slot type at all; the plan's slot is rewritten to attach an ``NLX.*``
    built-in and carry the regex (S5).
    """
    output: dict[str, dict] = {}
    for plan in flow_plans:
        operation = operations.get(plan.get("operation_id"))
        input_fields: dict[str, dict] = {}
        for item in (_model_dump(operation).get("input_fields") or []):
            field = _field_dict(item)
            if field.get("name"):
                # Live: the flow plan names a slot `phonePin` for the FieldSpec
                # `phone_pin` (or the reverse); an exact-name miss silently
                # produced no slot type and D9-4 refused the bundle.
                for variant in _name_variants(field["name"]):
                    input_fields.setdefault(variant, field)
        for slot in plan.get("slots") or []:
            if not isinstance(slot, dict) or not slot.get("name"):
                continue
            field = {}
            for candidate in (slot.get("field_name"), slot["name"]):
                for variant in _name_variants(candidate or ""):
                    if variant in input_fields:
                        field = input_fields[variant]
                        break
                if field:
                    break
            declared_type = str(slot.get("type") or field.get("type") or field.get("field_type") or "text")
            # FieldSpec spells the regex `pattern`; older specs and plans say `regex`.
            regex = _field_constraint(field, "regex", "pattern") or slot.get("regex")
            enum_values = _field_constraint(field, "enum_values", "allowed_values", "enum") or []
            min_length = _field_constraint(field, "min_length", "minLength")
            max_length = _field_constraint(field, "max_length", "maxLength")
            if not enum_values:
                # An open value: keep the constraint, drop the would-be one-item
                # menu. A declared custom type name with no values behind it is
                # not a value set either, so it goes the same way.
                if declared_type.startswith(_BUILTIN_SLOT_NAMESPACE):
                    builtin = declared_type
                else:
                    builtin = builtin_slot_type_for(slot["name"], declared_type, regex)
                slot["type"] = builtin
                if regex:
                    slot["regex"] = regex
                if slot.get("sensitive") or field.get("sensitive"):
                    # PII must be marked on the ATTACHED slot; without a custom
                    # slot type this is the only place left to carry it.
                    slot["sensitive"] = True
                continue
            slot_type_id = slot_type_id_for(slot["name"], declared_type)
            slot["type"] = slot_type_id
            if regex:
                slot["regex"] = regex          # the plan mirrors the FieldSpec, not the model's respelling
            metadata = {
                key: value for key, value in (
                    ("regex", regex), ("min_length", min_length), ("max_length", max_length))
                if value is not None
            }
            current = output.setdefault(slot_type_id, {
                "slotTypeId": slot_type_id,
                "values": [],
                "sensitive": bool(slot.get("sensitive") or field.get("sensitive")),
                "description": slot.get("description") or field.get("description") or "",
                "metadata": {"constraints": metadata} if metadata else {},
            })
            known = {str(item.get("value")) for item in current["values"]}
            for value in enum_values:
                text = str(value)
                if text and text not in known:
                    current["values"].append({"value": text[:256]})
                    known.add(text)
    return list(output.values())


@dataclass(frozen=True)
class ACXDGenerationContext:
    """Thin ``model_dump`` compatibility shim for retained ACXD generators."""

    payload: dict

    def model_dump(self) -> dict:
        return copy.deepcopy(self.payload)


def build_generation_context(session_id: Optional[str] = None) -> ACXDGenerationContext:
    """Build the former ACXDSpec-shaped dict from the new source-of-truth specs."""
    sid = _session_id(session_id)
    infrastructure = _model_dump(get_infrastructure_spec())
    operations = get_all_specs()
    flow_spec = get_acxd_flow_spec(session_id)
    flow_data = _model_dump(flow_spec)
    plans = copy.deepcopy(flow_data.get("flows") or [])
    openapi = _load_openapi_document(sid)
    contracts = _operation_contracts(openapi)

    data_integrations: list[dict] = []
    request_ids: dict[str, str] = {}
    for operation_id, operation in operations.items():
        op = _model_dump(operation)
        raw_id = str(op.get("operation_id") or operation_id)
        data_request_id = _normalise_data_request_id(raw_id)
        request_ids[raw_id] = data_request_id
        # The Classic contract exposes an operation through its TOOLS: the Lambda
        # folder, the OpenAPI operationId and the API path are /tools/<tool_id>,
        # and tool_id is often not the operation_id (live: operation
        # `check_balance` → tool `verify_and_get_balance`). A Data Request that
        # targets /tools/<operation_id> then calls a path that does not exist.
        tools = [t for t in (op.get("tools") or []) if isinstance(t, dict) and t.get("tool_id")]
        primary_tool = tools[0] if tools else {}
        tool_id = str(primary_tool.get("tool_id") or raw_id)
        contract = contracts.get(tool_id) or contracts.get(raw_id) or {}
        for key in (_normalise_data_request_id(tool_id), _normalise_data_request_id(raw_id)):
            if contract:
                break
            contract = contracts.get(key) or {}
        path = primary_tool.get("path") or contract.get("path") or f"/tools/{tool_id}"
        if not str(path).startswith("/tools/"):
            path = f"/tools/{str(path).strip('/').split('/')[-1]}"
        data_integrations.append({
            "data_request_id": data_request_id,
            "operation_ref": tool_id,
            "operation_id": raw_id,
            "path": path,
            "mode": "external",
            "http_method": contract.get("http_method") or primary_tool.get("http_method")
            or op.get("http_method") or "POST",
            # The OpenAPI contract carries the DEPLOYED spellings (camelCase),
            # the OperationSpec is the source of truth for WHICH fields exist:
            # merge them so a field the OpenAPI generator dropped (live: a CREATE
            # operation's response schema shipped with only the envelope, and its
            # request schema empty) still reaches the Data Request. The parity
            # gate reports the OpenAPI defect itself at review time.
            "request_fields": _merge_fields(
                contract.get("request_fields"),
                [_field_dict(field) for field in (op.get("input_fields") or [])]),
            # Same contract as the OpenAPI response: envelope + output_fields, so
            # D9-3 (Data Request ↔ OpenAPI) holds by construction.
            "response_fields": _with_envelope(_merge_fields(
                contract.get("response_fields"),
                [_field_dict(field) for field in (op.get("output_fields") or [])])),
            "purpose": op.get("summary") or op.get("description") or raw_id,
        })

    for plan in plans:
        operation_id = plan.get("operation_id")
        for step in plan.get("steps") or []:
            if not isinstance(step, dict) or step.get("node_type") != "data_request":
                continue
            raw_id = step.get("data_request_id") or operation_id
            if raw_id in request_ids:
                step["data_request_id"] = request_ids[raw_id]

    slot_types = _derive_slot_types(plans, operations)
    kb_plan = copy.deepcopy(flow_data.get("knowledge_base") or {})
    articles = _load_faq_articles(sid)
    if articles:
        kb_plan["articles"] = articles

    application = copy.deepcopy(flow_data.get("application") or {})
    locales = application.get("locales") or []
    if locales:
        application.setdefault("languages", locales)
    if application.get("primary_locale"):
        application.setdefault("primary_language", application["primary_locale"])

    payload = {
        "business_profile": _business_profile(infrastructure),
        "flows": plans,
        "slot_types": slot_types,
        "data_integrations": data_integrations,
        "guardrails": copy.deepcopy(flow_data.get("guardrails") or []),
        "knowledge_base": kb_plan,
        "application": application,
        "infrastructure": infrastructure,
        "deployment": {"environment": application.get("environment") or "development"},
    }
    return ACXDGenerationContext(payload)


def get_acxd_spec(session_id: Optional[str] = None) -> ACXDGenerationContext:
    """Compatibility entry point replacing the deleted ``acxd_spec_manager``."""
    return build_generation_context(session_id)
