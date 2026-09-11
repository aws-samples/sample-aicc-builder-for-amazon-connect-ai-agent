"""ACXD Data Request builder (Task 7).

Builds ACXD DataRequest documents from the interview's
``ACXDDataIntegrationPlan``s — **fully deterministically** (no LLM): the
spec already carries every field, so generating this document with code
is strictly safer than prompting (D2 taken to its conclusion).

Modes (D3/D8):
  - ``mock``     → ``inline-static`` webhook with a static JSON response
                   (no infrastructure needed; ideal for demos)
  - ``external`` → ``external`` webhook pointing at the Classic pipeline's
                   API Gateway: ``{WEBHOOK_URL}/tools/<operation>`` — the
                   same ``/tools/`` path contract the Classic
                   openapi/lambda/infrastructure generators produce
                   (reused unchanged, D8). ``{WEBHOOK_URL}`` is resolved
                   by the deploy runner from the CFN stack output.
  - ``mcp``      → ``mcp`` webhook referencing an AgentCore MCP gateway.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from strands import tool

from tools.acxd_generation_context import get_acxd_spec
from tools.session_context import current_session_id
from tools.validate_acxd_flow import validate_acxd_asset

logger = logging.getLogger(__name__)

#: interview field types → JSON Schema types
_FIELD_TYPE_MAP = {
    "text": "string", "string": "string", "email": "string", "phone": "string",
    "date": "string", "datetime": "string", "enum": "string",
    "number": "number", "float": "number",
    "integer": "integer", "int": "integer",
    "boolean": "boolean", "bool": "boolean",
    "array": "array", "list": "array",
    "object": "object",
}

#: JSON Schema type → placeholder mock value
_MOCK_VALUES = {
    "string": "sample-value",
    "number": 42.0,
    "integer": 42,
    "boolean": True,
    "array": [],
    "object": {},
}


def fields_to_json_schema(fields: list[dict]) -> dict:
    """Convert interview field specs to a JSON Schema object."""
    properties = {}
    required = []
    for field in fields or []:
        name = field.get("name")
        if not name:
            continue
        json_type = _FIELD_TYPE_MAP.get(
            str(field.get("type") or field.get("field_type") or "text").lower(), "string")
        prop: dict = {"type": json_type}
        if field.get("description"):
            prop["description"] = field["description"]
        enum_values = field.get("enum_values") or field.get("enum")
        if enum_values:
            prop["enum"] = list(enum_values)
        if field.get("regex") or field.get("pattern"):
            prop["pattern"] = field.get("regex") or field.get("pattern")
        if field.get("min_length") is not None:
            prop["minLength"] = field["min_length"]
        if field.get("max_length") is not None:
            prop["maxLength"] = field["max_length"]
        properties[name] = prop
        if field.get("required", True):
            required.append(name)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_mock_response(response_fields: list[dict],
                        override: Optional[dict] = None) -> dict:
    """Static mock payload: spec-provided override wins, else type defaults."""
    if override:
        return override
    out = {}
    for field in response_fields or []:
        name = field.get("name")
        if not name:
            continue
        json_type = _FIELD_TYPE_MAP.get(
            str(field.get("type") or field.get("field_type") or "text").lower(), "string")
        out[name] = field.get("example", _MOCK_VALUES[json_type])
    return out


def build_data_request(plan: dict) -> dict:
    """Build one ACXD DataRequest document from an integration plan."""
    # ACXD requires an alphanumeric dataRequestId, so a Classic snake_case
    # operation_id (get_delivery_status_by_order_number) is rejected verbatim.
    # Normalize the ID but keep the webhook PATH on the original operation
    # name, so it still matches the Lambda/API Gateway the Classic generators
    # produce.
    raw_id = plan["data_request_id"]
    dr_id = normalize_data_request_id(raw_id)
    mode = plan.get("mode", "mock")
    request_fields = plan.get("request_fields") or []
    response_fields = plan.get("response_fields") or []
    operation = plan.get("operation_ref") or raw_id
    # The generation context resolves the deployed path from the OperationSpec's
    # tool (/tools/<tool_id>); fall back to the operation name for older plans.
    tool_path = str(plan.get("path") or f"/tools/{operation}")
    if not tool_path.startswith("/"):
        tool_path = "/" + tool_path

    if mode == "mock":
        webhook = {
            "implementation": "inline-static",
            "code": json.dumps(
                build_mock_response(response_fields, plan.get("mock_response")),
                ensure_ascii=False),
            "sendContext": False,
        }
    elif mode in ("external", "sample"):
        # 'sample' and 'external' produce the same webhook: both call
        # {WEBHOOK_URL}/tools/<operation>. They differ only in who builds what
        # sits behind it — for 'sample' we generate the backend ourselves, with
        # demo tables and seed rows, which is what a PoC customer means by
        # "샘플 DB로 만들어줘". Routing that to 'mock' produced an agent that
        # answered with one hard-coded string and nothing deployable.
        webhook = {
            "implementation": "external",
            "method": plan.get("http_method", "POST"),
            "url": f"{{WEBHOOK_URL}}{tool_path}",
            # A header takes a SECRET, not a variable — that is the whole point
            # of Secrets in ACXD. When the interview captured an auth header we
            # emit a {{secrets.<name>}} reference; the deploy runner creates the
            # secret from an env var so no credential ever enters the bundle.
            "headers": auth_headers_for(plan),
            "sendContext": True,
        }
    elif mode == "mcp":
        webhook = {
            "implementation": "mcp",
            "mcp": {
                "url": plan.get("mcp_url") or "{WEBHOOK_URL}/mcp",
                "tools": [{
                    "name": operation,
                    "enabled": True,
                    "requestSchema": fields_to_json_schema(request_fields),
                    "responseSchema": fields_to_json_schema(response_fields),
                }],
            },
        }
    else:
        raise ValueError(f"unknown data integration mode {mode!r} "
                         f"(expected mock/external/mcp)")

    doc = {
        "dataRequestId": dr_id,
        "type": "object",
        "webhook": webhook,
        "responseSchema": fields_to_json_schema(response_fields),
        "sensitive": bool(plan.get("sensitive")),
        "description": (plan.get("purpose") or "")[:200],
    }
    if request_fields:
        doc["requestSchema"] = fields_to_json_schema(request_fields)
    return doc


def build_all_data_requests(spec: dict) -> tuple[list[dict], list[str]]:
    """Build every data request in the spec. Returns (docs, problems)."""
    docs: list[dict] = []
    problems: list[str] = []
    for i, plan in enumerate(spec.get("data_integrations") or []):
        try:
            doc = build_data_request(plan)
        except (KeyError, ValueError) as e:
            problems.append(f"data_integrations[{i}]: {e}")
            continue
        errors = validate_acxd_asset("data_request", doc)
        if errors:
            problems.extend(f"data_integrations[{i}] ({doc['dataRequestId']}): {e}"
                            for e in errors)
        else:
            docs.append(doc)
    return docs, problems


@tool
def generate_acxd_data_requests() -> dict:
    """Generate all ACXD DataRequest documents from the interview spec.

    Deterministic (no LLM). 'mock' integrations become inline-static
    webhooks; 'external' integrations point at the Classic Lambda+API GW
    backend via the {WEBHOOK_URL}/tools/<operation> contract — generate
    that backend with the Classic lambda/openapi/infrastructure tools
    using the plan's operation_ref.
    """
    session_id = current_session_id.get() or "default"
    spec = get_acxd_spec().model_dump()
    if not spec.get("data_integrations"):
        return {"status": "success", "generated": [],
                "message": "no data integrations in the spec"}

    docs, problems = build_all_data_requests(spec)
    if problems:
        return {"status": "error", "problems": problems,
                "generated": [d["dataRequestId"] for d in docs]}

    from tools.s3_asset_storage import save_asset_to_s3
    from tools.streaming_callback import stream_asset
    for doc in docs:
        content = json.dumps(doc, indent=2, ensure_ascii=False)
        file_name = f"{doc['dataRequestId']}.json"
        s3_key = save_asset_to_s3(session_id, "acxd_data_request", file_name, content)
        stream_asset(
            "acxd_data_request",
            file_name,
            content,
            operation_id=doc["dataRequestId"],
            is_complete=True,
            s3_key=s3_key,
        )

    external = [d["dataRequestId"] for d in docs
                if d["webhook"]["implementation"] == "external"]
    # NameError: `plans` does not exist in this scope — the tool reads the spec.
    sample_ids = [p.get("data_request_id")
                  for p in (spec.get("data_integrations") or [])
                  if isinstance(p, dict) and p.get("mode") == "sample"]
    return {
        "status": "success",
        "generated": [d["dataRequestId"] for d in docs],
        "external_backends_needed": external,
        "sample_backends_needed": [i for i in sample_ids if i],
        "note": ("external data requests require the Classic backend "
                 "(lambda/openapi/infrastructure generators) for the "
                 "referenced operations" if external else None),
    }

# ---------------------------------------------------------------------------
# Journey tool helper flows
# ---------------------------------------------------------------------------
#
# A Generative Journey cannot call a data request directly: the service DROPS
# `dataRequest.dataRequestId` on save. Smuggling the id through
# provider/action does keep the FIELDS, but that shape is not what the runtime
# resolves as a callable tool — and the build succeeds either way, so a
# successful build is no evidence.
#
# The configuration that is actually DEPLOYED and passed a live multi-turn test
# (SELC Assistant, build c4d9eeaf — the only deployment on that app) binds each
# data request as an **mcpFlow** pointing at a tiny helper flow that wraps it:
#
#     start -> data_request (the id survives here) -> basic message -> end
#
# where the basic message body is EXACTLY `{<id>.toolResponse:NLX.Variable}`.
# The runtime uses the text a tool flow emits as the tool result, so the backend
# serializes its whole result into one JSON *string* field; interpolating a
# whole object produces broken nested-JSON quoting.
#
# The binding is `flow`, NOT `mcpFlow`. ACXD's architecture docs describe an
# MCP flow as the one where "the agent stays in control", which reads like the
# right choice for a tool — but the RUNTIME disagrees. Measured in the ACXD
# Canvas debugger (2026-09-09), an mcpFlow tool fails on invocation:
#     toolType: mcpFlow   duration: 0   success: false
#     result:   {"error": "Unknown tool type"}
# It saves and builds cleanly, so neither round-trip nor a green build catches
# it. `flow` is the binding the runtime actually resolves.

TOOL_RESPONSE_PROPERTY = "toolResponse"

TOOL_RESPONSE_DESCRIPTION = (
    "Serialized JSON result emitted by an ACXD helper flow as its tool "
    "response."
)

#: Journey binding for a tool flow. Single named switch so one live turn can
#: re-settle it: mcpFlow was tried and fails at runtime (see above).
JOURNEY_TOOL_FLOW_BINDING = "flow"

_FLOW_ID_STOPWORDS = ("by", "from", "with", "for", "the", "of", "to", "info",
                      "information")


def tool_response_template(data_request_id: str) -> str:
    """The EXACT basic-message body a helper flow must emit."""
    return f"{{{data_request_id}.{TOOL_RESPONSE_PROPERTY}:NLX.Variable}}"


def _id_words(identifier: str) -> list:
    spaced = re.sub(r"[^A-Za-z0-9]+", " ", str(identifier))
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    return [w for w in spaced.split() if w]


def normalize_data_request_id(raw: str) -> str:
    """Classic snake_case operation_id -> alphanumeric camelCase ACXD id.

    ACXD requires an alphanumeric dataRequestId, so
    `get_delivery_status_by_order_number` is rejected verbatim.
    """
    words = _id_words(raw)
    if not words:
        return "dataRequest"
    head, *rest = words
    return head.lower() + "".join(w.capitalize() for w in rest)


def helper_flow_id(data_request_id: str, max_length: int = 27) -> str:
    """Helper-flow id: alphabetic only, <= 27 chars, shortened at word bounds."""
    words = [w for w in _id_words(data_request_id) if w.isalpha()] or ["tool"]

    def build(ws):
        return "tool" + "".join(w.capitalize() for w in ws)

    candidate = build(words)
    if len(candidate) <= max_length:
        return candidate
    trimmed = [w for i, w in enumerate(words)
               if w.lower() not in _FLOW_ID_STOPWORDS
               or i == 0 or i == len(words) - 1]
    if len(build(trimmed)) <= max_length:
        return build(trimmed)
    while len(trimmed) > 2 and len(build(trimmed)) > max_length:
        del trimmed[len(trimmed) // 2]
    return build(trimmed)[:max_length]

#: Shape of a helper flow's data_request node reference.
#:
#: `payload` is deliberately EMPTY: the helper flow declares its inputs via
#: `mcp.input` and the data request carries `sendContext: true`, so the runtime
#: supplies the values. This mirrors the flow that is actually DEPLOYED and
#: passed a live multi-turn test (toolCleaningPrice: payload {}).
#:
#: Do NOT synthesize `{"type": "recursive", "value": {...}}` context mappings:
#: that shape came from a fix candidate that was validated locally but never
#: deployed, and a journey calling such a helper flow failed at runtime with a
#: tool error ("가격 조회 시스템에 일시적인 문제").
def helper_flow_data_request_ref(data_request_id: str) -> dict:
    return {"dataRequestId": data_request_id, "headers": {}, "payload": {}}

# ---------------------------------------------------------------------------
# Secrets (auth headers)
# ---------------------------------------------------------------------------
#
# ACXD holds API keys and tokens as Secrets and lets a data request reference
# them from its headers. Emitting `headers: []` unconditionally made that
# capability unreachable and left the runner's upsert-secrets step dead.

#: The generated backend (CloudFormation API Gateway) requires its API key in
#: the ACXD target — the merge sets ApiKeyRequired: true on every method — and
#: the Data Requests send it from this secret. deploy.sh stores the stack's
#: ApiKeyValue output in it (env ACXD_SECRET_BACKENDAPIKEY), so nothing is manual
#: and no key ever enters the bundle.
BACKEND_API_KEY_SECRET = "BackendApiKey"
BACKEND_API_KEY_HEADER = "x-api-key"


def _calls_generated_backend(plan: dict) -> bool:
    """True when the integration targets the backend this bundle deploys
    (the {WEBHOOK_URL} placeholder) rather than a customer-supplied URL."""
    if str(plan.get("mode") or "external").lower() not in ("external", "sample"):
        return False
    url = str(plan.get("url") or plan.get("webhook_url") or "")
    return not url or "{WEBHOOK_URL}" in url


def auth_secret_name_for(plan: dict) -> Optional[str]:
    """Secret name for this integration's auth header, if any."""
    if not plan.get("auth_header"):
        return BACKEND_API_KEY_SECRET if _calls_generated_backend(plan) else None
    explicit = plan.get("auth_secret_name")
    if explicit:
        return re.sub(r"[^A-Za-z0-9_]", "", str(explicit)) or None
    base = normalize_data_request_id(plan.get("data_request_id") or "api")
    return f"{base}ApiKey"


def auth_headers_for(plan: dict) -> list:
    """Header list for a webhook: a Secret reference, never a literal value."""
    header = str(plan.get("auth_header") or "").strip()
    if not header and _calls_generated_backend(plan):
        header = BACKEND_API_KEY_HEADER
    secret = auth_secret_name_for(plan)
    if not header or not secret:
        return []
    return [{"key": header, "value": f"{{{{secrets.{secret}}}}}"}]


def build_secret_assets(spec: dict) -> list:
    """Secret documents for every integration that declared an auth header.

    The document carries only the NAME, a description and the env var the
    runner should read — never the value.
    """
    out, seen = [], set()
    for plan in spec.get("data_integrations") or []:
        if not isinstance(plan, dict):
            continue
        secret = auth_secret_name_for(plan)
        if not secret or secret in seen:
            continue
        seen.add(secret)
        if secret == BACKEND_API_KEY_SECRET:
            out.append({
                "name": secret,
                "description": (
                    "API Gateway key of the generated backend, sent as x-api-key by the "
                    "Data Requests. deploy.sh fills it from the CloudFormation ApiKeyValue "
                    "output (env ACXD_SECRET_BACKENDAPIKEY)."),
                "valueEnv": "ACXD_SECRET_BACKENDAPIKEY",
            })
            continue
        out.append({
            "name": secret,
            "description": (
                f"Auth header value for the {plan.get('data_request_id')} data "
                "request. Set at deploy time from the environment."),
            "valueEnv": f"ACXD_SECRET_{secret.upper()}",
        })
    return out

