"""ACXD Flow Generator sub-agent (Task 6).

Turns confirmed ``ACXDFlowPlan``s into validated ACXD flow documents.

Architecture (decision D2 — LLM declares, code validates):
  1. ``build_generation_prompt`` assembles the plan + business context.
  2. The LLM returns a flow JSON document.
  3. ``validate_generated_flow`` runs the schema validator AND the
     cross-asset consistency validator (with stub slot-types/data-requests
     derived from the spec) AND the determinism contract.
  4. On violations, ``run_flow_generation`` feeds the exact problems back
     (self-correction loop, bounded attempts).

Everything except the actual LLM call is deterministic and unit-tested;
the LLM call is injected (``invoke``) so tests use fakes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from strands import tool

from tools.acxd_generation_context import get_acxd_spec
from tools.session_context import current_session_id
from tools.validate_acxd_flow import GENERATIVE_NODE_TYPES, prune_to_schema
from tools.validate_acxd_consistency import (
    BUILTIN_SLOT_PRIMITIVES,
    format_report,
    validate_acxd_consistency,
)

from .system_prompt import ACXD_FLOW_GENERATOR_SYSTEM_PROMPT  # noqa: F401 (pool)

from tools.acxd_contract import (
    CONDITION_OPERATORS,
    enum,
    IMPLICIT_EDGES,
    NODE_TYPES,
    OPERATOR_ALIASES,
    TERMINAL_NODE_TYPES,
    TYPED_EDGE_LEFT_TYPES,
    UNARY_OPERATORS,
    canonical_language,
    canonical_operator,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# deterministic core
# ---------------------------------------------------------------------------

def build_generation_prompt(plan: dict, spec: dict,
                            feedback: Optional[list[str]] = None) -> str:
    """Assemble the generation (or correction) prompt for one flow plan.

    A correction carries the ENTIRE original prompt plus the violations. It used
    to send only the violation list, which left the model with no plan, no spec,
    no node catalogue and no output format — and since every invocation gets a
    fresh Agent (for session isolation) there is no conversation history to fall
    back on either. The model did the reasonable thing with 167 characters of
    context: it replied in prose. That is the dominant generation failure,
    classified as PROSE by the diagnostics, and it burned four of the five
    attempts on every flow it touched.
    """
    base = _build_initial_prompt(plan, spec)
    if not feedback:
        return base
    return (
        base
        + "\n\n## Your previous attempt failed deterministic validation\n\n"
        + "\n".join(f"- {p}" for p in feedback)
        + "\n\nFix ONLY these problems. Return the corrected COMPLETE flow "
          "document as a single fenced JSON block, exactly as specified above — "
          "no explanation, no partial document.\n"
    )


def _build_initial_prompt(plan: dict, spec: dict) -> str:
    """The full generation prompt: plan, spec context, rules, output format."""
    profile = spec.get("business_profile") or {}
    kb_name_raw = ((spec.get("knowledge_base") or {}).get("name")) or ""
    if kb_name_raw:
        from tools.acxd_resource_builders import sanitize_kb_name
        kb_name = sanitize_kb_name(kb_name_raw)
    else:
        kb_name = ""
    known_slot_types = [st.get("slotTypeId") for st in spec.get("slot_types") or []
                        if isinstance(st, dict) and st.get("slotTypeId")]
    known_data_requests = [di.get("data_request_id")
                           for di in spec.get("data_integrations") or []
                           if isinstance(di, dict) and di.get("data_request_id")]

    parts = [
        "Generate the ACXD flow document for this confirmed plan.",
        "",
        "## Business context",
        json.dumps({k: v for k, v in profile.items() if v}, ensure_ascii=False),
        "",
        "## Confirmed flow plan (honor every step's determinism decision)",
        json.dumps(plan, ensure_ascii=False, indent=2),
        "",
        f"## flowId MUST be exactly: {plan.get('flow_id')!r}",
        f"## Available data request IDs (use these EXACT ids, no others): "
        f"{known_data_requests or '(none — do not use data_request nodes)'}",
        f"## Available custom slot type IDs: {known_slot_types or '(none — use text/number/boolean)'}",
        f"## Knowledge base placeholder — copy VERBATIM: {{KB:{kb_name}}}"
        if kb_name_raw else
        "## No knowledge base in this project — do NOT emit knowledge_base nodes",
        "",
        "## Output rules",
        "- Return ONE JSON object with ONLY these top-level keys: flowId,",
        "  description, aiDescription, mainLanguageCode, languageCodes,",
        "  slotTypes, contextVariables, nodes, metadata.",
        "- Do NOT include knowledge_bases / data_requests / application or any",
        "  other asset in this document — you are generating ONE flow.",
    ]
    return "\n".join(parts)


def extract_flow_json(text: str) -> Optional[dict]:
    """Extract the flow JSON from an LLM response (fenced block or bare)."""
    if isinstance(text, dict):
        doc = text
    else:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        candidate = match.group(1) if match else str(text).strip()
        try:
            doc = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(doc, dict):
        return None
    # Some responses wrap the flow, or bolt on sibling assets. Unwrap and
    # drop the strays instead of failing the whole attempt (live QA).
    if "flowId" not in doc:
        for key in ("flow", "flow_document", "acxd_flow"):
            inner = doc.get(key)
            if isinstance(inner, dict) and "flowId" in inner:
                doc = inner
                break
    for stray in ("knowledge_bases", "knowledgeBases", "data_requests",
                  "dataRequests", "slot_types", "slotTypesDefinitions",
                  "application", "guardrails", "contact_flows"):
        doc.pop(stray, None)
    return doc


def stub_bundle_for_validation(spec: dict, flow: dict) -> dict:
    """Single-flow pseudo-bundle so cross-reference checks can run.

    Slot types and data requests the interview declared (but other
    sub-agents have not generated yet) are stubbed with minimal valid
    documents; the KB gets a name-only stub so {KB:...} refs resolve.

    Custom slot types the flow PLANS reference are stubbed too: the
    interview often names an enum slot type (e.g. ReturnReason) without a
    separate save, and without this the generator deadlocks on
    SLOT_REF_UNDEFINED for all 3 attempts (observed live).
    """
    declared = {st["slotTypeId"] for st in spec.get("slot_types") or []
                if isinstance(st, dict) and st.get("slotTypeId")}
    planned: set[str] = set()
    for plan in spec.get("flows") or []:
        for slot in plan.get("slots") or []:
            st = (slot or {}).get("type")
            if st and st not in BUILTIN_SLOT_PRIMITIVES:
                planned.add(st)
    # ...and any custom type the generated flow itself attaches.
    for slot in flow.get("slotTypes") or []:
        st = (slot or {}).get("type")
        if st and st not in BUILTIN_SLOT_PRIMITIVES:
            planned.add(st)

    slot_types = [
        {"slotTypeId": st, "values": [{"value": "stub"}]}
        for st in sorted(declared | planned)
    ]
    data_requests = [
        {"dataRequestId": di["data_request_id"], "type": "object",
         "webhook": {"implementation": "inline-static", "code": "{}"}}
        for di in spec.get("data_integrations") or []
        if isinstance(di, dict) and di.get("data_request_id")
    ]
    kbs = []
    kb_name = (spec.get("knowledge_base") or {}).get("name")
    if kb_name:
        # Use the SANITIZED name — that is what the KB builder will emit, so
        # {KB:...} placeholders must resolve against it (live QA: a Korean KB
        # name made every attempt fail KB_REF_UNDEFINED + a name-charset
        # SCHEMA error).
        from tools.acxd_resource_builders import sanitize_kb_name
        kbs.append({"name": sanitize_kb_name(kb_name), "type": "articles"})
    return {
        "flows": [flow],
        "slot_types": slot_types,
        "data_requests": data_requests,
        "knowledge_bases": kbs,
    }


def normalize_generated_flow(flow: dict, spec: dict) -> dict:
    """Repair mechanical mismatches the model reliably makes.

    Currently: rewrite any `{KB:<name>}` placeholder to the SANITIZED KB
    name the KB builder will actually emit. A Korean KB name otherwise
    fails KB_REF_UNDEFINED on every attempt (live QA) even though the
    model's intent was correct.
    """
    kb_raw = ((spec.get("knowledge_base") or {}).get("name")) or ""
    if not kb_raw:
        return flow
    from tools.acxd_resource_builders import sanitize_kb_name
    clean = f"{{KB:{sanitize_kb_name(kb_raw)}}}"

    def walk(value):
        if isinstance(value, str):
            return clean if value.startswith("{KB:") and value.endswith("}") else value
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    return walk(flow)



# The service contract is derived from the SDK, not hand-maintained here.
# See tools/acxd_contract.py and scripts/extract_acxd_contract.py.
_TYPED_EDGE_LEFT_TYPES = TYPED_EDGE_LEFT_TYPES
_TERMINAL_NODE_TYPES = TERMINAL_NODE_TYPES
_UNARY_OPERATORS = UNARY_OPERATORS
_OPERATOR_ALIASES = OPERATOR_ALIASES

#: Allowed context-variable types, from the SDK (FlowContextVariableType).
_CONTEXT_VAR_TYPES = enum("FlowContextVariableType") or frozenset({"text", "number", "boolean"})

#: Canonical typed-edge condition sets by node type, from the contract. First
#: entry is the happy path, second is the failure path that gets the safety net.
_TYPED_EDGES_BY_NODE_TYPE = {
    node_type: tuple(edge["conditions"] for edge in edges)
    for node_type, edges in IMPLICIT_EDGES.items()
}


def _connect_dangling_edges(nodes: dict, flow_id: str) -> int:
    """Give every edge a target, adding a terminal node as the safety net.

    The console flags any targetless edge because those conversations leave the
    flow for the application Fallback. `end` is the only terminal type the API
    accepts (probed: exit / exit_application / disconnect / return are all
    rejected), so the safety net is an `end` node reached via an apology-free
    direct hop — callers keep whatever message the failure branch already had.
    """
    safety_net = next(
        (nid for nid, n in nodes.items()
         if isinstance(n, dict) and n.get("type") in _TERMINAL_NODE_TYPES),
        None,
    )

    def ensure_safety_net() -> str:
        nonlocal safety_net
        if safety_net is None:
            safety_net = "eeeeeeee-0000-4000-8000-000000000001"
            nodes[safety_net] = {"nodeId": safety_net, "type": "end"}
        return safety_net

    repairs = 0
    for nid, node in list(nodes.items()):
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype in _TERMINAL_NODE_TYPES:
            node.pop("childNodes", None)
            continue

        children = [c for c in (node.get("childNodes") or []) if isinstance(c, dict)]

        # 1. Typed-edge nodes must expose their full edge set, each targeted.
        typed = _TYPED_EDGES_BY_NODE_TYPE.get(ntype)
        if typed:
            happy_conds, fail_conds = typed

            def _matches(child, conds):
                got = child.get("conditions") or []
                return bool(got) and got[0].get("left", {}).get("type") == \
                    conds[0]["left"]["type"] and \
                    got[0].get("operator") == conds[0]["operator"] and \
                    (got[0].get("right") or {}).get("value") == \
                    (conds[0].get("right") or {}).get("value")

            happy = next((c for c in children if _matches(c, happy_conds)), None)
            fail = next((c for c in children if _matches(c, fail_conds)), None)
            # Untyped leftovers become the happy path if we have none.
            spare = next((c for c in children
                          if c is not happy and c is not fail and c.get("nodeId")), None)
            if happy is None:
                happy = {"conditions": list(happy_conds)}
                if spare is not None:
                    happy["nodeId"] = spare["nodeId"]
                repairs += 1
            if not happy.get("nodeId"):
                happy["nodeId"] = spare["nodeId"] if spare and spare.get("nodeId") \
                    else ensure_safety_net()
                repairs += 1
            happy["conditions"] = list(happy_conds)
            if fail is None:
                fail = {"conditions": list(fail_conds)}
                repairs += 1
            if not fail.get("nodeId"):
                fail["nodeId"] = ensure_safety_net()
                repairs += 1
            fail["conditions"] = list(fail_conds)
            node["childNodes"] = [happy, fail]
            continue

        # 2. Every other node: no targetless edge, and never zero edges.
        if not children:
            node["childNodes"] = [{"nodeId": ensure_safety_net(), "name": "next"}]
            repairs += 1
            continue
        for child in children:
            if not child.get("nodeId"):
                child["nodeId"] = ensure_safety_net()
                repairs += 1
        node["childNodes"] = children

    if repairs:
        logger.info("[ACXDFlowGen] %s: connected %d dangling edge(s)", flow_id, repairs)
    return repairs


def _canonical_locale(code: str) -> str:
    """Delegate to the contract's locale resolution (SDK LanguageCode list)."""
    return canonical_language(code)




#: Minimal, schema-valid bodies for a node type the plan confirmed but the model
#: did not emit. Anything needing configuration we cannot invent is absent, and
#: those stay reported rather than fabricated.
def _stub_node(node_type: str, node_id: str, description: str, spec: dict) -> Optional[dict]:
    text = (description or "").strip()[:200] or "확인 중입니다."
    node: dict = {"nodeId": node_id, "type": node_type}
    if node_type in ("basic", "user_input", "user_choice"):
        node["messages"] = [{"type": "text", "body": text}]
    elif node_type == "data_request":
        declared = [d.get("data_request_id") for d in spec.get("data_integrations") or []
                    if isinstance(d, dict) and d.get("data_request_id")]
        if not declared:
            return None
        node["dataRequests"] = [declared[0]]
    elif node_type in ("generative_text", "generative_task"):
        node["metadata"] = {
            "generativeText" if node_type == "generative_text" else "generativeTask":
                {"prompt": text}
        }
    elif node_type == "generative_journey":
        node["metadata"] = {"generativeJourney": {"prompt": text, "maxSteps": 8}}
    elif node_type == "knowledge_base":
        kb = (spec.get("knowledge_base") or {}).get("name")
        if not kb:
            return None
        node["metadata"] = {"knowledgeBase": {"knowledgeBaseId": f"{{KB:{kb}}}"}}
    elif node_type in ("escalate", "end", "note", "wait"):
        pass                       # no required configuration
    elif node_type in ("choice", "split"):
        pass                       # branches are added by the edge pass
    else:
        return None                # redirect/define/transform/loop/intent_capture
    return node


def _ensure_confirmed_node_types(nodes: dict, plan: dict, spec: dict) -> int:
    """Add a node for every confirmed step type the flow is missing.

    The determinism contract is verified by node TYPE presence, so a plan that
    confirmed a `user_input` step fails when the model folds that prompt into a
    neighbouring node. Previously only `escalate` was repaired; a live run failed
    on confirmed steps 3, 5, 8 and 9 of one flow and spent every retry there.

    New nodes are attached to a branch node when one exists (so a choice gains a
    real target) and otherwise chained off `start`; the edge pass that runs last
    gives them outgoing edges.
    """
    present = {n.get("type") for n in nodes.values() if isinstance(n, dict)}
    added = 0
    for step in plan.get("steps") or []:
        if not step.get("user_confirmed"):
            continue
        node_type = step.get("node_type")
        if not node_type or node_type in present:
            continue
        node_id = f"{added + 1:08d}-0000-4000-8000-{abs(hash(node_type)) % 10**12:012d}"
        stub = _stub_node(node_type, node_id, step.get("description") or "", spec)
        if stub is None:
            logger.info("[ACXDFlowGen] %s: cannot synthesise a %r node for "
                        "confirmed step %s — reporting instead",
                        plan.get("flow_id"), node_type, step.get("step"))
            continue
        nodes[node_id] = stub
        present.add(node_type)
        added += 1
        branch = next((n for n in nodes.values()
                       if isinstance(n, dict) and n.get("type") in ("choice", "split")), None)
        anchor = branch or next((n for n in nodes.values()
                                 if isinstance(n, dict) and n.get("type") == "start"), None)
        if anchor is not None:
            anchor.setdefault("childNodes", []).append(
                {"nodeId": node_id, "name": str(node_type)[:40]})
    if added:
        logger.info("[ACXDFlowGen] %s: added %d node(s) for confirmed steps",
                    plan.get("flow_id"), added)
    return added


def _ascii_fallback_description(plan: dict, flow: dict) -> str:
    """Deterministic ASCII descriptor when the authored text is non-ASCII.

    ACXD rejects non-ASCII `description` / `aiDescription` outright, and it has
    no training utterances — the AI Description is a flow's only routing
    descriptor. So when a Korean-language interview yields text that cannot
    survive transliteration, emit a usable English label rather than dropping
    the field (which would leave the flow unroutable and unpickable as a tool).

    Prefer any ASCII the interview already supplied.
    """
    for candidate in (plan.get("ai_description"), plan.get("purpose_en"),
                      plan.get("description_en")):
        text = str(candidate or "").strip()
        if text and re.fullmatch(r"[\x20-\x7E]+", text):
            return text

    flow_id = str(plan.get("flow_id") or flow.get("flowId") or "flow")
    # split camelCase / snake_case into words for a readable label
    words = re.sub(r"[^A-Za-z0-9]+", " ", flow_id)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", words).strip().lower()
    role = str(plan.get("role") or "").strip().lower()
    label = f"Handles the {words} conversation" if words else "Conversation flow"
    if role and role != "operation":
        label += f" ({role} flow)"
    return label


def _helper_flow_id_for(data_request_id: str) -> str:
    """Helper-flow id a journey mcpFlow tool must point at for this data request."""
    from tools.acxd_data_request_builder import helper_flow_id
    return helper_flow_id(data_request_id)

def repair_generated_flow(flow: dict, plan: dict, spec: dict) -> dict:
    """Deterministically repair the mechanical mistakes the model repeats.

    Philosophy: the LLM proposes the conversation design; CODE fixes the
    bookkeeping. Live QA showed generation burning all 3 attempts on
    mechanical issues (wrong flowId, invented data-request ids, an
    unauthorized generative node, a dangling child) while the actual flow
    shape was fine. Each repair is safe and preserves intent.
    """
    flow = dict(flow)
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict):
        return flow

    # LIVE-VERIFIED (2026-09-08) journey/tool repairs:
    #  - `intent_capture` is not a real node (palette has none, metadata is
    #    dropped, and a deployed flow using it failed on the first utterance).
    #    Convert it to `user_input`, which the palette does have.
    #  - a dataRequest tool sent only as `dataRequest.dataRequestId` comes back
    #    as `dataRequest:{}` — the id must ALSO ride in `payload`.
    #  - only generative nodes may carry `modelType`.
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        meta = node.setdefault("metadata", {}) if isinstance(node.get("metadata", {}), dict) else {}
        if node.get("type") == "intent_capture":
            node["type"] = "user_input"
            meta.pop("intentCapture", None)
        journey = meta.get("generativeJourney")
        if isinstance(journey, dict):
            rewritten = []
            for tool in journey.get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                if tool.get("type") != "dataRequest":
                    # mcpFlow saves and builds but fails on invocation.
                    if tool.get("type") == "mcpFlow" and tool.get("flowId"):
                        # mcpFlow fails on invocation: "Unknown tool type".
                        tool = {"type": "flow", "flowId": tool["flowId"]}
                    rewritten.append(tool)
                    continue
                dr = tool.get("dataRequest") if isinstance(tool.get("dataRequest"), dict) else {}
                drid = (dr.get("dataRequestId")
                        or dr.get("action")
                        or (tool.get("payload") or {}).get("dataRequestId"))
                if not drid:
                    rewritten.append(tool)
                    continue
                # A journey cannot call a data request directly.
                #
                # Verified by round-trip on a real workspace: the service DROPS
                # `dataRequest.dataRequestId`. Smuggling the id through
                # provider/action does make the FIELDS survive, but that shape
                # is not what the runtime resolves as a callable tool — and the
                # build succeeds either way, so a build is no evidence.
                #
                # The configuration that is actually deployed and passed a live
                # multi-turn test (SELC Assistant, build c4d9eeaf, the only
                # deployment on that app) binds each data request as an
                # **mcpFlow** pointing at a helper flow that wraps it. mcpFlow
                # is a first-class GenerativeJourneyToolType and keeps the agent
                # in control so it can summarize the result.
                rewritten.append({"type": "flow",
                                  "flowId": _helper_flow_id_for(drid)})
            if rewritten:
                journey["tools"] = rewritten
        gt = meta.get("generativeText")
        if isinstance(gt, dict):
            gt.pop("modelType", None)

    # A choice whose branches carry no conditions cannot route — and the
    # platform accepts and BUILDS it (verified live), so the caller silently
    # falls through the first branch. Derive conditions from the branch labels
    # against the flow's captured slot, which is what the model meant.
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("type") != "choice":
            continue
        children = [c for c in (node.get("childNodes") or []) if isinstance(c, dict)]
        if not children or (node.get("metadata") or {}).get("choice"):
            continue
        if any(c.get("conditions") or c.get("generativeCondition") for c in children):
            continue
        slot = None
        for other in nodes.values():
            if isinstance(other, dict) and other.get("type") in ("intent_capture", "user_input"):
                slot = ((other.get("metadata") or {}).get("intentCapture") or {}).get("slotName")
                if slot:
                    break
        slot = slot or "intentName"
        for c in children:
            label = str(c.get("name") or "").strip()
            if not label:
                continue
            c["conditions"] = [{
                "left": {"type": "slot", "name": slot},
                "operator": "eq",
                "right": {"type": "constant", "value": label},
            }]

    # LIVE-API constraint (found by a real deploy, 2026-09-08): every message
    # object requires `type` — "messages[0].type is required". The SDK schema
    # marks it optional; the live validator does not. Defaulting to "text" is
    # always safe (ssml is only ever produced deliberately).
    for node in nodes.values():
        if isinstance(node, dict):
            for msg in node.get("messages") or []:
                if isinstance(msg, dict) and "type" not in msg:
                    msg["type"] = "text"

    # 0. LIVE-API constraints (verified 2026-09-05 against a real workspace;
    #    the public docs are laxer than reality):
    #    - flowId must be ALPHABETIC only (digits rejected)
    #    - node ids must be UUIDs
    #    - description / aiDescription must be ASCII (Korean rejected) while
    #      customer-facing message bodies may be any language
    plan_id = re.sub(r"[^A-Za-z]", "", str(plan.get("flow_id") or ""))
    flow["flowId"] = (plan_id or re.sub(r"[^A-Za-z]", "",
                                        str(flow.get("flowId") or "")) or "acxdFlow")[:64]

    for field, limit in (("description", 200), ("aiDescription", 1000)):
        val = flow.get(field)
        if isinstance(val, str) and val:
            ascii_only = re.sub(r"[^\x20-\x7E]", " ", val)
            ascii_only = re.sub(r"\s+", " ", ascii_only).strip()
            if ascii_only:
                flow[field] = ascii_only[:limit]
            else:
                # Nothing survives transliteration (e.g. pure Korean).
                #
                # Do NOT drop it: ACXD has no training utterances, so the AI
                # Description is a flow's ONLY routing descriptor — dropping it
                # ships a flow the agent can neither match nor pick as a tool.
                # Fall back to a deterministic ASCII label instead.
                flow[field] = _ascii_fallback_description(plan, flow)[:limit]
        elif field == "aiDescription" and not val:
            # Missing entirely is the same problem: always give the router
            # something to match on.
            flow[field] = _ascii_fallback_description(plan, flow)[:limit]

    # Language codes must be full locales (live API: 'ko' is rejected with
    # "mainLanguageCode is not a supported value"; 'ko-KR' is accepted).
    for field in ("mainLanguageCode", "languageCode"):
        val = flow.get(field)
        if isinstance(val, str) and val:
            flow[field] = _canonical_locale(val)
    if isinstance(flow.get("languageCodes"), list):
        flow["languageCodes"] = [
            _canonical_locale(v) for v in flow["languageCodes"] if isinstance(v, str)
        ] or None
        if not flow["languageCodes"]:
            flow.pop("languageCodes")

    # Branch conditions: the API wants structured operands. Verified live —
    # the flat {variable, operator, value} form the LLM naturally emits fails
    # with "conditions[0].left must be an object". Accepted contract:
    #   {"left": {"type":"slot","name":"x"}, "operator":"eq",
    #    "right": {"type":"constant","value": ...}}
    # Typed edges (captured_flow / node_status) have their own shapes and MUST
    # be left alone — rewriting them to slot comparisons is what made the
    # service materialize its own empty edges.
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for child in node.get("childNodes") or []:
            if not isinstance(child, dict) or not child.get("conditions"):
                continue
            fixed_conds = []
            for cond in child["conditions"]:
                if not isinstance(cond, dict):
                    continue
                left, right = cond.get("left"), cond.get("right")
                if isinstance(left, dict) and left.get("type") in _TYPED_EDGE_LEFT_TYPES:
                    fixed_conds.append(cond)      # already canonical
                    continue
                if not isinstance(left, dict):
                    name = cond.get("variable") or cond.get("slot") or cond.get("name")
                    if not name:
                        continue  # unusable — drop rather than fail the deploy
                    left = {"type": "slot", "name": str(name)}
                if not isinstance(right, dict):
                    value = cond.get("value", right)
                    right = {"type": "constant", "value": value}
                # Keep the model's operator when it is one the service knows.
                # Forcing 'eq' here silently rewrote business rules — a plan
                # step like "refund over $500 needs approval" became
                # "refund equals $500". Operators come from the SDK's
                # ConditionOperator enum.
                op = canonical_operator(cond.get("operator"),
                                        right.get("value") is not None)
                built = {"left": left, "operator": op}
                # exists / not_exists are unary: a right operand is invalid.
                if op not in _UNARY_OPERATORS:
                    built["right"] = right
                fixed_conds.append(built)
            if fixed_conds:
                child["conditions"] = fixed_conds
            else:
                child.pop("conditions", None)

    # Context variables: the SDK models exactly {name, type} and the model keeps
    # inventing extras. Live runs lost the whole flow to
    #   $['contextVariables'][0]: 'type' is a required property
    #   $['contextVariables'][0]: Additional properties are not allowed
    #                             ('defaultValue' was unexpected)
    # and then — told to fix it — stopped returning JSON at all on every retry,
    # so attempt 1's mechanical slip burned all five attempts.
    raw_vars = flow.get("contextVariables")
    if isinstance(raw_vars, list):
        cleaned_vars = []
        for var in raw_vars:
            if not isinstance(var, dict):
                continue
            name = var.get("name") or var.get("variable") or var.get("id")
            if not name:
                continue
            vtype = str(var.get("type") or var.get("valueType") or "").strip().lower()
            if vtype not in _CONTEXT_VAR_TYPES:
                # Infer from a supplied default, else fall back to text.
                default = var.get("defaultValue", var.get("default"))
                if isinstance(default, bool):
                    vtype = "boolean"
                elif isinstance(default, (int, float)):
                    vtype = "number"
                else:
                    vtype = "text"
            cleaned_vars.append({"name": str(name), "type": vtype})
        if cleaned_vars:
            flow["contextVariables"] = cleaned_vars
        else:
            flow.pop("contextVariables", None)

    # Remap non-UUID node ids (and every reference to them) to stable UUIDs.
    # LIVE-VERIFIED (2026-09-10, CreateFlow): the service checks RFC-4122
    # v4 SHAPE — version nibble 4 and variant nibble [89ab] (a "1111-1111-8111" id was
    # accepted at CreateFlow but the stored flow had NO nodes and every update
    # failed; v4-shaped ids round-trip). A model-written
    # "22222222-2222-2222-2222-222222222222" (variant '2') is rejected with the
    # unhelpful "nodes is not in the expected format", while
    # "11111111-1111-1111-8111-111111111111" is accepted.
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    remap: dict[str, str] = {}
    for i, nid in enumerate(list(nodes.keys()), start=1):
        if not _UUID_RE.match(str(nid).lower()):
            remap[nid] = f"a0000000-0000-4000-8000-{i:012d}"
        elif str(nid) != str(nid).lower():
            remap[nid] = str(nid).lower()
    if remap:
        nodes = {remap.get(k, k): v for k, v in nodes.items()}
        flow["nodes"] = nodes
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            node["nodeId"] = remap.get(node.get("nodeId"), node.get("nodeId"))
            for child in node.get("childNodes") or []:
                if isinstance(child, dict) and child.get("nodeId") in remap:
                    child["nodeId"] = remap[child["nodeId"]]
            redirect_meta = (node.get("metadata") or {}).get("redirect") if isinstance(node.get("metadata"), dict) else None
            if isinstance(redirect_meta, dict) and redirect_meta.get("nodeId") in remap:
                redirect_meta["nodeId"] = remap[redirect_meta["nodeId"]]
        if flow.get("startNodeId") in remap:
            flow["startNodeId"] = remap[flow["startNodeId"]]
        logger.info("[ACXDFlowGen] repaired %s: remapped %d non-UUID node id(s)",
                    flow.get("flowId"), len(remap))

    # LIVE-VERIFIED (2026-09-10, CreateFlow) node metadata contracts, from the
    # SDK types (FlowNodeMetadata / RedirectConfig / DefineConfig / Operand):
    #  - redirect: `metadata.redirect.type` is REQUIRED ('flow' | 'page' |
    #    'parent_application'); the model writes only {flowId}.
    #  - define:   `metadata.define.value` must be an Operand OBJECT
    #    ({type:'constant', value:...}); the model writes a bare scalar.
    #  - escalate: FlowNodeMetadata has NO `escalate` config. The queue is
    #    chosen by the Contact Flow's Escalation branch, not here; any message
    #    belongs in node-level `messages`, and the node is terminal.
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        node["metadata"] = meta
        ntype = node.get("type")
        if ntype == "redirect":
            rd = meta.get("redirect") if isinstance(meta.get("redirect"), dict) else {}
            meta["redirect"] = rd
            if not rd.get("type"):
                rd["type"] = "page" if rd.get("pageName") else ("parent_application" if rd.get("parentApplication") else "flow")
            if rd["type"] == "flow" and not rd.get("flowId") and meta.get("flowId"):
                rd["flowId"] = meta["flowId"]
        elif ntype == "define":
            df = meta.get("define") if isinstance(meta.get("define"), dict) else {}
            meta["define"] = df
            val = df.get("value")
            if val is not None and not (isinstance(val, dict) and "type" in val):
                df["value"] = {"type": "constant", "value": val}
        elif ntype == "escalate":
            esc = meta.pop("escalate", None)
            if isinstance(esc, dict):
                msgs = esc.get("messages")
                if isinstance(msgs, list) and msgs and not node.get("messages"):
                    node["messages"] = [
                        {"type": m.get("type", "text"), "body": m.get("body", "")}
                        for m in msgs if isinstance(m, dict) and m.get("body")
                    ]
            # escalation hands control back to the Contact Flow — nothing follows it
            node["childNodes"] = []

    # 2. Map map-key/nodeId disagreements onto the map key (the key wins).
    for nid, node in list(nodes.items()):
        if isinstance(node, dict) and node.get("nodeId") != nid:
            node["nodeId"] = nid

    # 3. Data request ids → only ones the interview declared.
    declared_drs = [d.get("data_request_id") for d in spec.get("data_integrations") or []
                    if isinstance(d, dict) and d.get("data_request_id")]
    # Ids the plan attached to a data_request step, in step order, so a flow with
    # several lookups gets them assigned rather than all pointing at the first.
    planned_drs = [
        step.get("data_request_id") for step in (plan.get("steps") or [])
        if isinstance(step, dict) and step.get("node_type") == "data_request"
        and step.get("data_request_id")
    ]
    planned_queue = [d for d in planned_drs if d in declared_drs] or list(declared_drs)

    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        # The model serialises the singular key about as often as the plural
        # one ("노드 속성을 dataRequest(단수)로 직렬화 → 스키마가 요구하는
        # dataRequests 위반"). Accept both spellings and normalise.
        if not node.get("dataRequests"):
            singular = node.pop("dataRequest", None) or node.pop("dataRequestId", None)
            if singular:
                node["dataRequests"] = singular if isinstance(singular, list) else [singular]
        is_dr_node = node.get("type") == "data_request"
        if not node.get("dataRequests"):
            if not is_dr_node:
                continue
            # The field is REQUIRED on a data_request node and the model leaves
            # it out often enough that a live run stalled on
            # "data_request 노드(step 4)에 dataRequests 필드가 누락됨". Fill it
            # from the plan rather than bouncing the whole flow back to the LLM.
            picked = planned_queue.pop(0) if planned_queue else None
            if picked:
                node["dataRequests"] = [picked]
                logger.info("[ACXDFlowGen] repaired %s: filled missing dataRequests "
                            "on %s with %s", plan.get("flow_id"),
                            node.get("nodeId"), picked)
            else:
                # No integrations exist at all — degrade to a message node so
                # the graph stays valid instead of failing the flow.
                node["type"] = "basic"
                node.setdefault("messages", [{"type": "text", "body": "확인 중입니다."}])
                continue
        fixed_refs = []
        for ref in node["dataRequests"]:
            rid = ref.get("dataRequestId") if isinstance(ref, dict) else ref
            if rid in declared_drs:
                fixed_refs.append(rid)
                continue
            # nearest declared id by case-insensitive containment, else first
            lowered = str(rid or "").lower()
            match = next((d for d in declared_drs
                          if d.lower() in lowered or lowered in d.lower()), None)
            fixed_refs.append(match or (declared_drs[0] if declared_drs else None))
        node["dataRequests"] = [r for r in fixed_refs if r]
        if not node["dataRequests"] and node.get("type") == "data_request":
            # No integrations at all — degrade to a plain message node so the
            # graph stays valid instead of failing the whole flow.
            node["type"] = "basic"
            node.setdefault("messages", [{"type": "text", "body": "확인 중입니다."}])
            node.pop("dataRequests", None)

    # 4. Drop dangling childNodes references.
    for node in nodes.values():
        if isinstance(node, dict) and node.get("childNodes"):
            node["childNodes"] = [c for c in node["childNodes"]
                                  if isinstance(c, dict) and c.get("nodeId") in nodes]

    # 5. Generative nodes the user never confirmed → deterministic message.
    confirmed_gen = {s.get("node_type") for s in plan.get("steps") or []
                     if s.get("user_confirmed") and s.get("determinism") == "generative"}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("type") in GENERATIVE_NODE_TYPES and node["type"] not in confirmed_gen:
            node["type"] = "basic"
            node.setdefault("messages", [{"type": "text", "body": "안내드리겠습니다."}])
            node.pop("metadata", None)

    # 6. Every confirmed step's node type must exist in the flow — the
    #    determinism contract is checked that way (DETERMINISM_MISSING_NODE).
    #    Only `escalate` used to be repaired, so a plan confirming user_input /
    #    choice / data_request steps that the model then merged away failed
    #    validation and burned the retry budget: a live run reported missing
    #    nodes for confirmed steps 3, 5, 8 and 9 of one flow.
    _ensure_confirmed_node_types(nodes, plan, spec)

    # 7. Exactly one `start` node is required and the model sometimes omits it,
    #    which failed the flow outright (FLOW_NO_START) even though the entry
    #    point is unambiguous: the node nothing else points at.
    if not any(isinstance(n, dict) and n.get("type") == "start" for n in nodes.values()):
        referenced = {
            child.get("nodeId")
            for n in nodes.values() if isinstance(n, dict)
            for child in (n.get("childNodes") or []) if isinstance(child, dict)
        }
        entry = next((nid for nid in nodes if nid not in referenced), None)
        if entry is not None:
            start_id = "57a27000-0000-4000-8000-000000000001"
            nodes[start_id] = {"nodeId": start_id, "type": "start",
                               "childNodes": [{"nodeId": entry, "name": "start"}]}
            logger.info("[ACXDFlowGen] %s: added the missing start node -> %s",
                        plan.get("flow_id"), entry)

    # Last: connect every edge. Runs after the passes above so it also covers
    # nodes they introduced (e.g. the escalate node, which the LLM leaves with
    # no outgoing edge). A targetless edge is not a validation error — the API
    # accepts it — it silently reroutes live conversations to the application
    # Fallback flow, which is why this is enforced here rather than reported.
    _connect_dangling_edges(nodes, str(flow.get("flowId")))

    return flow


def validate_generated_flow(flow: dict, plan: dict, spec: dict) -> list[str]:
    """Full deterministic validation of one generated flow."""
    problems: list[str] = []
    if not isinstance(flow, dict):
        return ["response did not contain a JSON object"]
    if flow.get("flowId") != plan.get("flow_id"):
        problems.append(
            f"flowId {flow.get('flowId')!r} must equal the plan's flow_id "
            f"{plan.get('flow_id')!r}"
        )
    bundle = stub_bundle_for_validation(spec, flow)
    spec_view = {"flows": [plan]}
    problems += [str(v) for v in validate_acxd_consistency(bundle, spec=spec_view)]
    return problems


def _acxd_progress(status: str, message: str = "", flow_id: str = "") -> None:
    """Emit `subagent_progress` so the UI shows movement during generation.

    Classic generators do this and ACXD did not, which is why a run that was
    working through four flows at ~5s per attempt looked frozen: nothing reached
    the browser between the tool call and its result, and a flow that exhausts
    its five attempts occupies ~30s on its own.
    """
    try:
        from tools.session_context import current_callback_handler

        handler = current_callback_handler.get()
        if handler is None or not hasattr(handler, "add_ws_event"):
            return
        event = {"type": "subagent_progress", "subagent": "acxd_flow_generator",
                 "status": status}
        if message:
            event["message"] = message
        if flow_id:
            event["operation_id"] = flow_id
        handler.add_ws_event(event)
    except Exception:  # progress must never break generation
        pass


def _describe_response(text: str) -> str:
    """Name the likely reason a response carried no usable JSON.

    Turns one opaque message into a class we can act on: empty replies point at
    the model call, unterminated JSON at a token limit, prose at prompt
    adherence, and an error echo at a swallowed exception.
    """
    if not text:
        return "EMPTY — the model returned nothing; suspect the model call itself"
    lowered = text.lower()
    for marker in ("validationexception", "throttling", "accessdenied",
                   "an error occurred", "traceback"):
        if marker in lowered:
            return f"ERROR-ECHO — response contains {marker!r}; an exception " \
                   "reached the text instead of being raised"
    opens = text.count("{")
    closes = text.count("}")
    fenced = "```" in text
    if opens and opens != closes:
        return (f"TRUNCATED-JSON — {opens} '{{' vs {closes} '}}'"
                f"{' inside a fence' if fenced else ''}; suspect max_tokens")
    if fenced and opens == 0:
        return "FENCED-NON-JSON — a code fence with no object in it"
    if opens == 0:
        return "PROSE — no JSON object at all; the model explained instead of emitting"
    return "UNPARSEABLE — braces balance but json.loads failed; likely a syntax slip"


def run_flow_generation(
    plan: dict,
    spec: dict,
    invoke: Callable[[str], str],
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[Optional[dict], list[str], list[dict]]:
    """Self-correction loop: generate → validate → feed violations back.

    Args:
        invoke: LLM call — takes a prompt string, returns response text.

    Returns:
        (flow or None, final problems, attempt log)
    """
    attempts: list[dict] = []
    feedback: Optional[list[str]] = None
    problems: list[str] = ["not attempted"]
    flow_id = str(plan.get("flow_id") or "")
    for attempt in range(1, max_attempts + 1):
        _acxd_progress(
            "running",
            f"{flow_id}: generating (attempt {attempt}/{max_attempts})"
            + (f" — fixing {len(feedback)} issue(s)" if feedback else ""),
            flow_id,
        )
        prompt = build_generation_prompt(plan, spec, feedback)
        response = invoke(prompt)
        flow = extract_flow_json(response)
        if flow is None:
            # This is the single most common generation failure (91 occurrences
            # in one measured window) and it was undiagnosable: the message said
            # only "no valid JSON" while the response itself was never recorded,
            # so there was no way to tell an empty reply from prose, a truncated
            # document, or a swallowed API error. Log the shape — never the whole
            # body, which can be tens of KB of customer content.
            problems = ["response did not contain valid JSON"]
            text = response if isinstance(response, str) else str(response)
            stripped = text.strip()
            # Excerpts are deliberately short: the classifier carries the
            # diagnosis, and a response can be full of customer PII.
            logger.warning(
                "[ACXDFlowGen] %s attempt %d: no JSON in a %d-char response "
                "(prompt %d chars) | starts=%r | %s",
                flow_id, attempt, len(text), len(prompt),
                stripped[:60], _describe_response(stripped),
            )
        else:
            flow = normalize_generated_flow(flow, spec)
            flow = repair_generated_flow(flow, plan, spec)
            # Extra keys the model invents are the second most common failure
            # and carry no contract meaning, so drop them instead of spending an
            # attempt on them.
            flow = prune_to_schema(flow, "flow")
            problems = validate_generated_flow(flow, plan, spec)
        attempts.append({"attempt": attempt, "problems": list(problems)})
        if not problems:
            _acxd_progress("completed", f"{flow_id}: generated on attempt {attempt}",
                           flow_id)
            return flow, [], attempts
        feedback = problems
        # Log the ACTUAL violations, not just the count — without this a
        # 3-strike failure is undiagnosable from CloudWatch (hit live).
        logger.warning(
            "[ACXDFlowGen] %s attempt %d failed: %s",
            plan.get("flow_id"), attempt, " | ".join(problems[:5]),
        )
    return None, problems, attempts


# ---------------------------------------------------------------------------
# strands wrapper (orchestrator-facing tool)
# ---------------------------------------------------------------------------

def _make_llm_invoke():
    """Production ``invoke``: one agent instance per call.

    NOT ``get_agent()``. That returns a process-wide singleton, and one ECS task
    serves many sessions, so two sessions generating at the same time shared the
    instance: their messages clobbered each other and strands raised
    ConcurrencyException ("Direct tool call cannot be made while the agent is in
    the middle of an invocation"), from which the session never recovered.
    Reproduced by running five live conversations at once.

    ``get_agent_with_tools`` builds a fresh Agent while still reusing the cached
    BedrockModel, so isolation costs almost nothing. This generator needs no
    tools — it returns a JSON document.

    ``tools=None``, NOT ``tools=[]``. The shared model is built with
    ``cache_tools="default"``, and an empty tool list made Bedrock reject the
    Converse call ("ValidationException ... ConverseStream"), which surfaced to
    the operator as the generator "returning no valid JSON" on every attempt.
    """
    from agents.agent_pool import get_agent_with_tools

    def invoke(prompt: str) -> str:
        from tools.session_context import current_callback_handler

        agent = get_agent_with_tools(
            "acxd_flow_generator",
            tools=None,
            # Without this the sub-agent produced no visible output at all.
            callback_handler=current_callback_handler.get(),
        )
        result = agent(prompt)
        return str(result)

    return invoke


def _store_flow(session_id: str, flow: dict) -> None:
    from tools.s3_asset_storage import save_asset_to_s3
    from tools.streaming_callback import stream_asset

    flow_id = flow["flowId"]
    content = json.dumps(flow, indent=2, ensure_ascii=False)
    s3_key = save_asset_to_s3(session_id, "acxd_flow", f"{flow_id}.json", content)
    stream_asset(
        "acxd_flow",
        f"{flow_id}.json",
        content,
        operation_id=flow_id,
        is_complete=True,
        s3_key=s3_key,
    )


@tool
def generate_acxd_flows(flow_ids: list = None) -> dict:
    """Generate ACXD flow documents from the interview's confirmed flow plans.

    Call in interview phase ⑥ (review/generation) after every flow plan is
    confirmed. Generates, validates (schema + graph + cross-refs +
    determinism contract), self-corrects, and stores each JSON flow asset.

    Args:
        flow_ids: optional subset of plan flow_ids (default: all confirmed)
    """
    session_id = current_session_id.get() or "default"
    spec = get_acxd_spec().model_dump()
    plans = [p for p in spec.get("flows") or []
             if (flow_ids is None or p.get("flow_id") in flow_ids)]
    if not plans:
        return {"status": "error",
                "message": "no flow plans in the ACXD spec — run the interview first"}

    unconfirmed = [p["flow_id"] for p in plans
                   if any(not s.get("user_confirmed") for s in p.get("steps") or [])]
    if unconfirmed:
        return {"status": "error",
                "message": f"flows have unconfirmed determinism decisions: {unconfirmed}. "
                           "Confirm every step with the user first (D6)."}

    invoke = _make_llm_invoke()
    results = []

    # SlotType documents are derived from ACXDFlowSpec slots plus FieldSpec
    # constraints by tools.acxd_generation_context before this generator runs.
    for plan in plans:
        flow, problems, attempts = run_flow_generation(plan, spec, invoke)
        if flow is None:
            results.append({"flow_id": plan["flow_id"], "status": "failed",
                            "problems": problems, "attempts": len(attempts)})
            continue
        _store_flow(session_id, flow)
        results.append({"flow_id": plan["flow_id"], "status": "generated",
                        "nodes": len(flow.get("nodes") or {}),
                        "attempts": len(attempts)})

    failed = [r for r in results if r["status"] == "failed"]
    out = {
        "status": "error" if failed else "success",
        "generated": [r for r in results if r["status"] == "generated"],
        "failed": failed,
    }
    if failed:
        # Make the cause actionable instead of "some flows failed".
        out["message"] = (
            "Flow generation failed after 3 self-correction attempts. "
            "Fix the reported violations (usually a missing slot type or an "
            "unconfirmed/absent step) and retry ONLY the failed flows: "
            + "; ".join(f"{f['flow_id']}: {' | '.join(f['problems'][:3])}"
                        for f in failed)
        )
    return out


def _persist_planned_slot_types(spec: dict) -> None:
    """Deprecated compatibility hook.

    ``tools.acxd_generation_context`` now derives slot types from the canonical
    ACXD flow plan and Classic field constraints before flow generation.  There
    is no second mutable ACXD spec to persist.
    """
    return None
