"""Deterministic cross-asset validator for ACXD bundles (Task 3).

Validates an ACXD asset *bundle* — the set of documents the sub-agents
generate for one session — for internal consistency before packaging:

  1. per-asset schema validity (delegates to ``validate_acxd_flow``)
  2. flow graph integrity (single start, terminal exists, no dangling or
     unreachable nodes, map-key/nodeId agreement)
  3. bundle-level ID uniqueness
  4. cross-references (flow ↔ slot type ↔ data request ↔ guardrail ↔
     application ↔ knowledge base ↔ contact flow binding)
  5. determinism contract: generated flows must honor the interview's
     confirmed per-step decisions recorded in the ACXDSpec (D6)

Everything here is deterministic code — no LLM involvement (D2). This is
the local half of the double safety net; ``CreateApplicationBuild`` is the
server-side half.

Bundle placeholder conventions (resolved by the deploy runner):
  - ``{KB:<name>}``         → knowledgeBaseId of the bundle KB with that name
  - ``{GUARDRAIL:<name>}``  → guardrailId of the bundle guardrail with that name
  - ``{WEBHOOK_URL}``       → backend webhook base URL (CFN output)
Raw (non-placeholder) IDs are accepted untouched: they are assumed to
reference pre-existing workspace resources.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

from tools.validate_acxd_flow import (
    GENERATIVE_NODE_TYPES,
    validate_acxd_asset,
)
from tools.acxd_runtime_contract import runtime_contract_violations
from tools.acxd_bundle import ASCII_ONLY_METADATA_FIELDS, load_acxd_bundle
from tools.acxd_flow_spec import get_acxd_flow_spec

#: Primitive slot value types that need no slotTypes definition.
BUILTIN_SLOT_PRIMITIVES = frozenset({"text", "number", "boolean"})

#: GA documents built-in slot types under the ``NLX.`` namespace
#: (NLX.PhoneNumber, NLX.Email, NLX.Date, ...); CreateSlotType is only for
#: custom enumerated sets. Treating these as unknown would either reject a
#: valid flow or push it into an unnecessary custom SlotType.
BUILTIN_SLOT_NAMESPACE = "NLX."



def _capture_family(node_type):
    """Plan vocabulary vs SDK contract: a plan step 'user_input' (collect a slot)
    is implemented by a `user_choice` node (the SDK's value-capture node) and the
    canonicalizer converts slot-capturing user_input nodes accordingly, so the
    determinism gate compares the capture family, not the spelling."""
    return "user_capture" if node_type in ("user_input", "user_choice") else node_type


def _requirement_family(node_type):
    """The node family that SATISFIES a confirmed plan step.

    The capture family (user_input / user_choice) is one family. A confirmed
    ``generative_text`` is satisfied only by a ``generative_text`` node: the user
    approved generative wording there, and the runtime contract now keeps the
    node and hangs the templated sentence on its ``failure`` edge for a
    workspace without a default model (live, 2026-09-17) — so a ``basic``
    stand-in is a silent downgrade of what was confirmed (the review finding
    of 2026-09-16). The reverse — a generative node the user never confirmed —
    is caught by DETERMINISM_UNAUTHORIZED_GENERATIVE, which compares raw types."""
    return _capture_family(node_type)


def _is_builder_owned_plan(plan: dict) -> bool:
    """System flows (welcome / fallback / follow-up / agent request /
    escalation …) are built deterministically from the live-verified routing
    contract; the interview's step list for them describes the behaviour to the
    user and is not a design the generator follows, so the step-level
    determinism comparison does not apply."""
    role = str(plan.get("role") or "operation").strip().lower()
    try:
        from tools.acxd_system_flows import is_system_flow_role
        return bool(is_system_flow_role(role))
    except Exception:  # pragma: no cover - module optional in isolated tests
        return role != "operation"

def _is_builtin_slot_type(slot_type: str) -> bool:
    return (slot_type in BUILTIN_SLOT_PRIMITIVES
            or str(slot_type).startswith(BUILTIN_SLOT_NAMESPACE))

KB_PLACEHOLDER = re.compile(r"^\{KB:(?P<name>.+)\}$")
GUARDRAIL_PLACEHOLDER = re.compile(r"^\{GUARDRAIL:(?P<name>.+)\}$")

#: Agentic CX block limit (adminguide agentic-cx-block).
MAX_CONTACT_FLOW_CONTEXT_VARS = 10

#: System events an ACXD application must route. A missing one leaves the
#: runtime with nowhere to go for that event.
REQUIRED_DEFAULT_FLOW_ROLES = frozenset({
    "welcome", "fallback", "unknown", "escalation",
})


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    message: str

    def __str__(self) -> str:  # human-readable report line
        return f"[{self.code}] {self.path}: {self.message}"


def _v(out: list, code: str, path: str, message: str) -> None:
    out.append(Violation(code, path, message))


# ---------------------------------------------------------------------------
# 1+2. per-flow checks
# ---------------------------------------------------------------------------

def _check_flow_graph(flow: dict, path: str, out: list) -> None:
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return  # schema check already reported this

    starts = [nid for nid, n in nodes.items()
              if isinstance(n, dict) and n.get("type") == "start"]
    if not starts:
        _v(out, "FLOW_NO_START", path, "flow has no 'start' node")
    elif len(starts) > 1:
        _v(out, "FLOW_MULTI_START", path,
           f"flow has {len(starts)} 'start' nodes: {sorted(starts)}")

    terminals = [nid for nid, n in nodes.items()
                 if isinstance(n, dict) and n.get("type") in ("end", "escalate")]
    if not terminals:
        _v(out, "FLOW_NO_TERMINAL", path,
           "flow has no terminal node ('end' or 'escalate')")

    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("nodeId") != nid:
            _v(out, "FLOW_NODEID_MISMATCH", f"{path}.nodes[{nid!r}]",
               f"map key {nid!r} != nodeId {node.get('nodeId')!r}")
        for i, child in enumerate(node.get("childNodes") or []):
            target = child.get("nodeId") if isinstance(child, dict) else None
            if target not in nodes:
                _v(out, "FLOW_DANGLING_CHILD",
                   f"{path}.nodes[{nid!r}].childNodes[{i}]",
                   f"references nonexistent node {target!r}")

        # The platform accepts (and BUILDS, with zero issues) flows that are
        # semantically broken — proven live 2026-09-08 with a probe application:
        # dangling edges, missing start, conditionless choices, promptless
        # generative nodes and empty messages all deployed successfully and then
        # misbehaved at runtime (the canvas showed "Generate {} Unnamed" and
        # unconnected nodes). Our validator is therefore the ONLY gate.
        ntype = node.get("type")
        meta = node.get("metadata") or {}

        # A generative node with no prompt produces nothing at runtime.
        if ntype in ("generative_text", "generative_task", "generative_journey"):
            cfg_key = {"generative_text": "generativeText",
                       "generative_task": "agenticTask",
                       "generative_journey": "generativeJourney"}[ntype]
            cfg = meta.get(cfg_key) or {}
            if not str(cfg.get("prompt") or "").strip():
                _v(out, "FLOW_GENERATIVE_NO_PROMPT", f"{path}.nodes[{nid!r}]",
                   f"{ntype} node has no metadata.{cfg_key}.prompt — it will "
                   "generate nothing at runtime")

        # A choice whose branches carry no conditions cannot route.
        if ntype == "choice":
            children = node.get("childNodes") or []
            # A choice with a configured source (metadata.choice) routes by the
            # caller's selection, so per-branch conditions are optional there.
            configured = bool(meta.get("choice"))
            if children and not configured and not any(
                    (c.get("conditions") or c.get("generativeCondition"))
                    for c in children if isinstance(c, dict)):
                _v(out, "FLOW_CHOICE_NO_CONDITIONS", f"{path}.nodes[{nid!r}]",
                   "choice node has branches but none carry conditions — "
                   "routing is undefined")

        # An empty message body is a silent dead air / blank bubble.
        for mi, msg in enumerate(node.get("messages") or []):
            if isinstance(msg, dict) and not str(msg.get("body") or "").strip():
                _v(out, "FLOW_EMPTY_MESSAGE",
                   f"{path}.nodes[{nid!r}].messages[{mi}]",
                   "message body is empty — the caller hears nothing")

    if len(starts) == 1:
        reachable = set()
        stack = [starts[0]]
        while stack:
            nid = stack.pop()
            if nid in reachable or nid not in nodes:
                continue
            reachable.add(nid)
            node = nodes[nid]
            for child in (node.get("childNodes") or []) if isinstance(node, dict) else []:
                if isinstance(child, dict):
                    stack.append(child.get("nodeId"))
        for nid in nodes:
            if nid not in reachable:
                _v(out, "FLOW_UNREACHABLE_NODE", f"{path}.nodes[{nid!r}]",
                   "not reachable from the start node")


def _iter_flow_data_request_refs(flow: dict):
    """Yield (path, dataRequestId) referenced by data_request nodes."""
    for nid, node in (flow.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        for i, ref in enumerate(node.get("dataRequests") or []):
            if isinstance(ref, str):
                yield f"nodes[{nid!r}].dataRequests[{i}]", ref
            elif isinstance(ref, dict) and "dataRequestId" in ref:
                yield f"nodes[{nid!r}].dataRequests[{i}]", ref["dataRequestId"]


def _iter_flow_kb_refs(flow: dict):
    for nid, node in (flow.get("nodes") or {}).items():
        if isinstance(node, dict) and node.get("type") == "knowledge_base":
            kb_meta = (node.get("metadata") or {}).get("knowledgeBase") or {}
            kb_id = kb_meta.get("knowledgeBaseId")
            if kb_id:
                yield f"nodes[{nid!r}].metadata.knowledgeBase", kb_id


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def _check_backend_for_live_data_requests(
    bundle: dict, spec: Optional[dict], out: list
) -> None:
    """A data request that calls out needs the backend it calls.

    `sample` and `external` integrations resolve to
    ``{WEBHOOK_URL}/tools/<operation>``, which only exists if the Classic
    pipeline generated the Lambda handlers, the OpenAPI spec and the
    CloudFormation that fronts them. When that step is skipped the bundle still
    validates, deploys, and then answers nothing — a live GAON run produced an
    agent whose lookups pointed at an API Gateway that was never created.
    """
    if not spec:
        return
    needing = [
        (d.get("data_request_id"), d.get("mode"))
        for d in spec.get("data_integrations") or []
        if isinstance(d, dict) and d.get("mode") in ("sample", "external")
    ]
    if not needing:
        return
    backend = bundle.get("backend") or {}
    has_infra = bool(backend.get("infrastructure") or bundle.get("infrastructure"))
    has_lambdas = bool(backend.get("lambdas") or bundle.get("lambdas"))
    if has_infra and has_lambdas:
        return
    missing = []
    if not has_infra:
        missing.append("CloudFormation infrastructure")
    if not has_lambdas:
        missing.append("Lambda handlers")
    for dr_id, mode in needing:
        _v(out, "DATA_REQUEST_BACKEND_MISSING", f"data_requests[{dr_id}]",
           f"mode {mode!r} calls {{WEBHOOK_URL}}/tools/... but the bundle has no "
           f"{' and no '.join(missing)} — run the Classic backend generators "
           f"(lambda → openapi → infrastructure) or change the mode to 'mock'")


def validate_acxd_consistency(
    bundle: dict,
    spec: Optional[dict] = None,
    *,
    strict_subset: bool = True,
) -> list[Violation]:
    """Validate a generated ACXD bundle for internal consistency.

    Args:
        bundle: {
            "flows": [flow docs],
            "slot_types": [...], "data_requests": [...],
            "guardrails": [...], "knowledge_bases": [...],
            "application": {...} | None,
            "contact_flows": [{..., "acxdBinding": {...}}],
        }  (all keys optional)
        spec: ACXDSpec dump (optional) — enables the determinism checks
        strict_subset: enforce the supported node subset (D9)
    """
    out: list[Violation] = []

    flows = bundle.get("flows") or []
    slot_types = bundle.get("slot_types") or []
    data_requests = bundle.get("data_requests") or []
    guardrails = bundle.get("guardrails") or []
    kbs = bundle.get("knowledge_bases") or []
    application = bundle.get("application")
    contact_flows = bundle.get("contact_flows") or []

    # -- 1. schema validity ------------------------------------------------
    def _schema(kind: str, doc: Any, path: str):
        for err in validate_acxd_asset(kind, doc, strict_subset=strict_subset):
            _v(out, "SCHEMA", path, err)

    for i, f in enumerate(flows):
        _schema("flow", f, f"flows[{i}]")
    for i, s in enumerate(slot_types):
        _schema("slot_type", s, f"slot_types[{i}]")
    for i, d in enumerate(data_requests):
        _schema("data_request", d, f"data_requests[{i}]")
    for i, g in enumerate(guardrails):
        _schema("guardrail", g, f"guardrails[{i}]")
    for i, k in enumerate(kbs):
        _schema("knowledge_base", k, f"knowledge_bases[{i}]")
    if application is not None:
        _schema("application", application, "application")

    # -- 2. graph integrity --------------------------------------------------
    for i, f in enumerate(flows):
        if isinstance(f, dict):
            _check_flow_graph(f, f"flows[{i}]", out)

    # -- 3. bundle-level uniqueness ------------------------------------------
    def _dups(items, key, code, label):
        seen: dict[str, int] = {}
        for idx, item in enumerate(items):
            value = item.get(key) if isinstance(item, dict) else None
            if value is None:
                continue
            if value in seen:
                _v(out, code, f"{label}[{idx}]",
                   f"duplicate {key} {value!r} (first at {label}[{seen[value]}])")
            else:
                seen[value] = idx
        return set(seen)

    flow_ids = _dups(flows, "flowId", "DUP_FLOW_ID", "flows")
    slot_type_ids = _dups(slot_types, "slotTypeId", "DUP_SLOT_TYPE_ID", "slot_types")
    data_request_ids = _dups(data_requests, "dataRequestId",
                             "DUP_DATA_REQUEST_ID", "data_requests")
    guardrail_names = _dups(guardrails, "name", "DUP_GUARDRAIL_NAME", "guardrails")
    kb_names = _dups(kbs, "name", "DUP_KB_NAME", "knowledge_bases")

    # -- 4. cross references ---------------------------------------------------
    valid_slot_refs = BUILTIN_SLOT_PRIMITIVES | slot_type_ids

    def _check_flow_target(flow_id, code, path):
        if flow_id and flow_id not in flow_ids:
            _v(out, code, path, f"references unknown flow {flow_id!r}")

    def _check_kb_ref(kb_id, path):
        m = KB_PLACEHOLDER.match(kb_id or "")
        if m and m.group("name") not in kb_names:
            _v(out, "KB_REF_UNDEFINED", path,
               f"KB placeholder {kb_id!r} matches no bundled knowledge base")

    for i, f in enumerate(flows):
        if not isinstance(f, dict):
            continue
        for j, slot in enumerate(f.get("slotTypes") or []):
            st = slot.get("type") if isinstance(slot, dict) else None
            if st and not _is_builtin_slot_type(st) and st not in slot_type_ids:
                _v(out, "SLOT_REF_UNDEFINED", f"flows[{i}].slotTypes[{j}]",
                   f"slot type {st!r} is neither a builtin "
                   f"({sorted(BUILTIN_SLOT_PRIMITIVES)} or "
                   f"{BUILTIN_SLOT_NAMESPACE}*) nor a bundled slotTypeId")
        for p, dr in _iter_flow_data_request_refs(f):
            if dr not in data_request_ids:
                _v(out, "DATA_REQUEST_REF_UNDEFINED", f"flows[{i}].{p}",
                   f"references unknown data request {dr!r}")
        for p, kb_id in _iter_flow_kb_refs(f):
            _check_kb_ref(kb_id, f"flows[{i}].{p}")

    for i, g in enumerate(guardrails):
        if not isinstance(g, dict):
            continue
        for j, rule in enumerate(g.get("rules") or []):
            behavior = ((rule.get("enforcement") or {}).get("behavior") or {})
            _check_flow_target(behavior.get("flowId"), "GUARDRAIL_FLOW_REF_UNDEFINED",
                               f"guardrails[{i}].rules[{j}].enforcement.behavior")
        fb = g.get("fallbackBehavior") or {}
        _check_flow_target(fb.get("flowId"), "GUARDRAIL_FLOW_REF_UNDEFINED",
                           f"guardrails[{i}].fallbackBehavior")

    if isinstance(application, dict):
        for j, ref in enumerate(application.get("flows") or []):
            _check_flow_target((ref or {}).get("flowId"), "APP_FLOW_REF_UNDEFINED",
                               f"application.flows[{j}]")
        settings = application.get("settings") or {}
        for event, ref in (settings.get("defaultFlows") or {}).items():
            if isinstance(ref, dict):
                _check_flow_target(ref.get("flowId"), "APP_FLOW_REF_UNDEFINED",
                                   f"application.settings.defaultFlows.{event}")
                if ref.get("knowledgeBaseId"):
                    _check_kb_ref(ref["knowledgeBaseId"],
                                  f"application.settings.defaultFlows.{event}")
        for event, flow_id in (settings.get("lifecycleHooks") or {}).items():
            _check_flow_target(flow_id, "APP_FLOW_REF_UNDEFINED",
                               f"application.settings.lifecycleHooks.{event}")
        # Every application needs all four system entry points wired. A missing
        # one means the runtime has nowhere to go for that event: no greeting,
        # no fallback, no escalation path.
        configured_defaults = set(settings.get("defaultFlows") or {})
        # Only meaningful once the application actually attaches flows: a
        # bundle with no flows has a different, already-reported problem.
        missing_defaults = (REQUIRED_DEFAULT_FLOW_ROLES - configured_defaults
                            if application.get("flows") else set())
        for role in sorted(missing_defaults):
            _v(out, "APP_DEFAULT_FLOW_MISSING",
               "application.settings.defaultFlows",
               f"no flow is assigned to the '{role}' system event; give one "
               f"interview flow role='{role}' or set it explicitly")

        # Settings the service accepts and then silently discards.
        if "childDirected" in settings:
            _v(out, "SETTING_SILENTLY_DROPPED",
               "application.settings.childDirected",
               "is silently dropped by the service; remove it so the bundle "
               "matches what is actually stored")
        for j, lang in enumerate(settings.get("languageSettings") or []):
            for field in ("voice", "useNativeLanguage"):
                if isinstance(lang, dict) and field in lang:
                    _v(out, "SETTING_SILENTLY_DROPPED",
                       f"application.settings.languageSettings[{j}].{field}",
                       "is silently dropped by the service; ASR/audio filler "
                       "belong on the Agentic CX block and TTS on the Connect "
                       "'Set voice' block")

        for j, ref in enumerate(settings.get("guardrails") or []):
            gid = (ref or {}).get("guardrailId") or ""
            m = GUARDRAIL_PLACEHOLDER.match(gid)
            if m and m.group("name") not in guardrail_names:
                _v(out, "APP_GUARDRAIL_REF_UNDEFINED",
                   f"application.settings.guardrails[{j}]",
                   f"guardrail placeholder {gid!r} matches no bundled guardrail")

    # -- contact flow binding (Task 8 contract) --------------------------------
    app_name = (application or {}).get("name") if isinstance(application, dict) else None
    for i, cf in enumerate(contact_flows):
        if not isinstance(cf, dict):
            continue
        binding = cf.get("acxdBinding")
        if not isinstance(binding, dict):
            _v(out, "CONTACT_FLOW_NO_BINDING", f"contact_flows[{i}]",
               "contact flow is missing its 'acxdBinding' (Agentic CX block wiring)")
            continue
        bound_app = binding.get("applicationName")
        if app_name and bound_app != app_name:
            _v(out, "CONTACT_FLOW_APP_MISMATCH", f"contact_flows[{i}].acxdBinding",
               f"binds application {bound_app!r} but bundle application is {app_name!r}")
        ctx_vars = binding.get("contextVariables") or []
        if len(ctx_vars) > MAX_CONTACT_FLOW_CONTEXT_VARS:
            _v(out, "CONTACT_FLOW_CTXVARS_EXCEEDED", f"contact_flows[{i}].acxdBinding",
               f"{len(ctx_vars)} context variables exceed the Agentic CX block "
               f"limit of {MAX_CONTACT_FLOW_CONTEXT_VARS}")
        for branch in ("Default", "Error", "Escalation"):
            if branch not in (binding.get("branches") or {}):
                _v(out, "CONTACT_FLOW_BRANCH_MISSING", f"contact_flows[{i}].acxdBinding",
                   f"required Agentic CX branch {branch!r} is not wired")

    # -- 5. determinism contract (spec-driven) ---------------------------------
    if spec:
        flows_by_id = {f.get("flowId"): f for f in flows if isinstance(f, dict)}
        for plan in spec.get("flows") or []:
            flow_id = plan.get("flow_id")
            generated = flows_by_id.get(flow_id)
            if generated is None:
                continue  # not generated yet — coverage is Task 10's concern
            if _is_builder_owned_plan(plan):
                continue  # deterministic system flow: the builder is the contract
            unconfirmed = [s.get("step") for s in plan.get("steps") or []
                           if not s.get("user_confirmed")]
            if unconfirmed:
                _v(out, "DETERMINISM_UNCONFIRMED", f"flows[{flow_id}]",
                   f"flow was generated but interview steps {unconfirmed} "
                   f"were never confirmed by the user")
            node_types = {n.get("type") for n in (generated.get("nodes") or {}).values()
                          if isinstance(n, dict)}
            node_families = {_requirement_family(t) for t in node_types}
            confirmed_generative = {
                s.get("node_type") for s in plan.get("steps") or []
                if s.get("user_confirmed") and s.get("determinism") == "generative"
            }
            for s in plan.get("steps") or []:
                if s.get("user_confirmed") and _requirement_family(s.get("node_type")) not in node_families:
                    _v(out, "DETERMINISM_MISSING_NODE", f"flows[{flow_id}]",
                       f"confirmed step {s.get('step')} requires a "
                       f"{s.get('node_type')!r} node but none exists in the flow")
            for nt in sorted(node_types & GENERATIVE_NODE_TYPES):
                if nt not in confirmed_generative:
                    _v(out, "DETERMINISM_UNAUTHORIZED_GENERATIVE", f"flows[{flow_id}]",
                       f"flow contains generative node type {nt!r} that the user "
                       f"never confirmed in the interview")
            # A planned hand-off names its target flow. Live (GAON): six
            # regenerations in a row sent the "order not found → search by
            # customer info" step to EscalationFlow while the message promised a
            # customer-info search; only the plan knows the intended target.
            planned_targets = {
                str(s.get("redirect_flow_id")).strip() for s in plan.get("steps") or []
                if s.get("user_confirmed") and s.get("redirect_flow_id")}
            if planned_targets:
                actual_targets = {
                    str(((n.get("metadata") or {}).get("redirect") or {}).get("flowId") or "")
                    for n in (generated.get("nodes") or {}).values()
                    if isinstance(n, dict) and n.get("type") == "redirect"}
                for target in sorted(planned_targets - actual_targets):
                    _v(out, "DETERMINISM_REDIRECT_TARGET", f"flows[{flow_id}]",
                       f"the confirmed plan hands off to flow {target!r} but no redirect node "
                       f"targets it (redirects found: {sorted(t for t in actual_targets if t)})")

    _check_backend_for_live_data_requests(bundle, spec, out)
    _check_runtime_contract(bundle, out, spec)

    return out


#: Flow roles the application's system events imply (R2/R3). Anything else is
#: an operation flow, and operation flows are the ones that must hand back to
#: the follow-up flow instead of ending the session.
_SYSTEM_FLOW_EVENTS = ("welcome", "fallback", "unknown", "escalation", "followUp",
                       "follow_up", "followup")


def _flow_roles(bundle: dict, follow_up_flow_id: str) -> dict[str, str]:
    """flowId -> role, derived from the application's system-event wiring."""
    roles: dict[str, str] = {}
    application = bundle.get("application")
    settings = (application or {}).get("settings") or {} if isinstance(application, dict) else {}
    for event, ref in (settings.get("defaultFlows") or {}).items():
        flow_id = ref.get("flowId") if isinstance(ref, dict) else ref
        if isinstance(flow_id, str):
            roles[flow_id] = str(event)
    for event, flow_id in (settings.get("lifecycleHooks") or {}).items():
        if isinstance(flow_id, str) and flow_id not in roles:
            roles[flow_id] = str(event)
    roles.setdefault(follow_up_flow_id, "follow_up")
    return roles


def _check_runtime_contract(bundle: dict, out: list, spec: Optional[dict] = None) -> None:
    """The cross-asset half of the live runtime contract (S5, D3, D5, M1, RX).

    The flow-scope half (S1 vocabulary, S2/S3, R6/R7, D4, A2) already ran per
    flow inside ``validate_acxd_asset``; these rules need the slot type
    documents, the data requests, the bundle's flow ids and the workspace
    context variables, which only exist here. The allowed values of enum result
    fields come from the spec's data integrations (the deployed reply schema
    carries types only), so D5 can report a branch on a value the API never
    returns at review time and not only inside the generator.
    """
    flows = [f for f in (bundle.get("flows") or []) if isinstance(f, dict)]
    if not flows:
        return
    from tools.acxd_runtime_contract import field_enums_from_integrations
    field_enums = field_enums_from_integrations(
        (spec or {}).get("data_integrations") if isinstance(spec, dict) else None)
    slot_type_docs = {
        str(doc["slotTypeId"]): doc
        for doc in (bundle.get("slot_types") or [])
        if isinstance(doc, dict) and doc.get("slotTypeId")
    }
    data_requests = {
        str(doc["dataRequestId"]): doc
        for doc in (bundle.get("data_requests") or [])
        if isinstance(doc, dict) and doc.get("dataRequestId")
    }
    context_variables = {
        str(doc["name"]) for doc in (bundle.get("context_variables") or [])
        if isinstance(doc, dict) and doc.get("name")
    }
    flow_ids = {str(f["flowId"]) for f in flows if f.get("flowId")}
    roles = _flow_roles(bundle, "FollowUpFlow")
    # A flow may legitimately redirect to a system flow the bundle does not
    # carry: Welcome / Fallback / Escalation are workspace default-behaviour
    # flows, wired by the application rather than generated here. A default
    # flow that is missing altogether is already reported as
    # APP_DEFAULT_FLOW_MISSING, so counting them as known targets keeps RX
    # pointed at what it was built for — a near-miss on an operation flow id.
    known_targets = flow_ids | set(roles)
    follow_up_flow_id = next(
        (fid for fid in sorted(known_targets) if fid.lower().startswith("followup")),
        "FollowUpFlow")
    escalation_flow_id = next(
        (fid for fid in sorted(known_targets) if fid.lower().startswith("escalation")),
        "EscalationFlow")

    for index, flow in enumerate(flows):
        flow_id = str(flow.get("flowId") or "")
        for problem in runtime_contract_violations(
            flow,
            role=roles.get(flow_id, "operation"),
            slot_type_ids=set(slot_type_docs),
            slot_type_docs=slot_type_docs,
            data_requests=data_requests,
            flow_ids=known_targets,
            context_variables=context_variables,
            follow_up_flow_id=follow_up_flow_id,
            escalation_flow_id=escalation_flow_id,
            scope="cross",
            field_enums=field_enums,
        ):
            _v(out, "RUNTIME_CONTRACT", f"flows[{index}]", problem)


def format_report(violations: list[Violation]) -> str:
    """Human-readable validation report."""
    if not violations:
        return "ACXD bundle consistency: OK (0 violations)"
    lines = [f"ACXD bundle consistency: {len(violations)} violation(s)"]
    lines += [f"  - {v}" for v in violations]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session-aware ACXDFlowSpec adapter (Classic Full runtime-target integration)
# ---------------------------------------------------------------------------
# Keep the original bundle-first implementation above for generator repair
# loops and legacy fixtures. This public adapter is the runtime path: it reads
# the current ACXDFlowSpec and canonical asset bundle itself when given a
# session id, then applies the stricter confirmed-step/count and metadata
# checks that packaging needs.
_validate_acxd_consistency_bundle = validate_acxd_consistency


def _acxd_spec_dict(spec: Any) -> Optional[dict]:
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec
    dump = getattr(spec, "model_dump", None)
    if callable(dump):
        value = dump()
        return value if isinstance(value, dict) else None
    return None


def _load_acxd_validation_inputs(
    bundle: Optional[dict | str], spec: Any, session_id: Optional[str],
) -> tuple[dict, Optional[dict]]:
    """Resolve legacy fixture calls and the session-backed production API."""
    resolved_session_id = session_id
    if isinstance(bundle, str):
        resolved_session_id = bundle
        bundle = None
    if bundle is None:
        bundle = load_acxd_bundle(resolved_session_id) if resolved_session_id else {}
    if not isinstance(bundle, dict):
        bundle = {}
    if spec is None and resolved_session_id:
        spec = get_acxd_flow_spec(resolved_session_id)
    return bundle, _acxd_spec_dict(spec)


def _acxd_ascii_metadata_violations(document: Any, path: str) -> list[Violation]:
    """Validate API metadata only; customer message bodies may be localized."""
    violations: list[Violation] = []

    def visit(value: Any, current_path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{current_path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            child_path = f"{current_path}.{key}" if current_path else key
            if key in ASCII_ONLY_METADATA_FIELDS and isinstance(child, str):
                if any(ord(character) > 0x7F for character in child):
                    violations.append(Violation(
                        "ASCII_METADATA", child_path,
                        f"{key} contains non-ASCII characters; ACXD metadata must be ASCII",
                    ))
            visit(child, child_path)

    visit(document, path)
    return violations


def _acxd_determinism_count_violations(bundle: dict, spec: Optional[dict]) -> list[Violation]:
    """Compare generated node multiplicity to confirmed ACXDFlowSpec steps."""
    if not spec:
        return []
    from collections import Counter

    flows_by_id = {
        flow.get("flowId"): flow
        for flow in (bundle.get("flows") or [])
        if isinstance(flow, dict) and flow.get("flowId")
    }
    violations: list[Violation] = []
    for plan in spec.get("flows") or []:
        if not isinstance(plan, dict):
            continue
        flow_id = plan.get("flow_id") or plan.get("flowId")
        generated = flows_by_id.get(flow_id)
        if generated is None:
            continue
        if _is_builder_owned_plan(plan):
            continue  # deterministic system flow: the builder is the contract
        steps = [step for step in (plan.get("steps") or []) if isinstance(step, dict)]
        confirmed = [step for step in steps if step.get("user_confirmed")]
        raw_counts = Counter(
            node.get("type")
            for node in (generated.get("nodes") or {}).values()
            if isinstance(node, dict) and node.get("type")
        )
        node_counts = Counter(
            _requirement_family(node.get("type"))
            for node in (generated.get("nodes") or {}).values()
            if isinstance(node, dict) and node.get("type")
        )
        required_counts = Counter(
            _requirement_family(step.get("node_type")) for step in confirmed if step.get("node_type")
        )
        for node_type, required_count in required_counts.items():
            actual_count = node_counts.get(node_type, 0)
            if actual_count < required_count:
                violations.append(Violation(
                    "DETERMINISM_MISSING_NODE", f"flows[{flow_id}]",
                    f"confirmed steps require {required_count} {node_type!r} node(s), "
                    f"but generated flow has {actual_count}",
                ))
        confirmed_generative = Counter(
            step.get("node_type") for step in confirmed
            if step.get("determinism") == "generative" and step.get("node_type")
        )
        for node_type in sorted(set(raw_counts) & GENERATIVE_NODE_TYPES):
            actual_count = raw_counts[node_type]
            allowed_count = confirmed_generative.get(node_type, 0)
            if actual_count > allowed_count:
                violations.append(Violation(
                    "DETERMINISM_UNAUTHORIZED_GENERATIVE", f"flows[{flow_id}]",
                    f"flow contains {actual_count} {node_type!r} node(s), but only "
                    f"{allowed_count} were confirmed as generative",
                ))
    return violations


def _is_modern_binding_without_name(bundle: dict) -> bool:
    """The current Agentic CX binding uses applicationId, not applicationName."""
    for flow in bundle.get("contact_flows") or []:
        if not isinstance(flow, dict):
            continue
        content = flow.get("content") if isinstance(flow.get("content"), dict) else flow
        metadata = content.get("Metadata") if isinstance(content, dict) else None
        binding = (metadata or {}).get("acxdBinding") if isinstance(metadata, dict) else None
        if not isinstance(binding, dict):
            binding = flow.get("acxdBinding")
        if isinstance(binding, dict) and binding.get("applicationId") and not binding.get("applicationName"):
            return True
    return False


def _dedupe_violations(violations: list[Violation]) -> list[Violation]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Violation] = []
    for violation in violations:
        key = (violation.code, violation.path, violation.message)
        if key not in seen:
            seen.add(key)
            result.append(violation)
    return result


def validate_acxd_consistency(
    bundle: Optional[dict | str] = None,
    spec: Any = None,
    *,
    session_id: Optional[str] = None,
    strict_subset: bool = True,
) -> list[Violation]:
    """Validate an ACXD bundle against its saved ACXDFlowSpec.

    Preferred production usage is ``validate_acxd_consistency(session_id)`` or
    ``validate_acxd_consistency(session_id=...)``. Bundle-first calls remain
    supported for generation-time repair and isolated tests.
    """
    try:
        resolved_bundle, resolved_spec = _load_acxd_validation_inputs(
            bundle, spec, session_id,
        )
    except Exception:  # package gating must fail closed, not crash
        logger.exception("[ACXDConsistency] could not load the bundle for validation")
        return [Violation("BUNDLE_LOAD_FAILED", "bundle",
                          "the ACXD bundle could not be loaded for validation (see the server log)")]

    violations = _validate_acxd_consistency_bundle(
        resolved_bundle, spec=resolved_spec, strict_subset=strict_subset,
    )
    # The legacy placeholder predates the applicationId-only binding. Do not
    # reject a valid current binding merely because it intentionally omits the
    # retired display-name field.
    if _is_modern_binding_without_name(resolved_bundle):
        violations = [
            violation for violation in violations
            if violation.code not in {"CONTACT_FLOW_APP_MISMATCH", "CONTACT_FLOW_NO_BINDING"}
        ]
    violations.extend(_acxd_determinism_count_violations(resolved_bundle, resolved_spec))

    for key in ("flows", "slot_types", "data_requests", "guardrails", "knowledge_bases"):
        violations.extend(_acxd_ascii_metadata_violations(resolved_bundle.get(key) or [], key))
    if resolved_bundle.get("application") is not None:
        violations.extend(_acxd_ascii_metadata_violations(
            resolved_bundle["application"], "application",
        ))
    return _dedupe_violations(violations)
