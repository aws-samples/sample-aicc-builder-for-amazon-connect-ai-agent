"""ACXDFlowSpec — the ACXD sibling of ContactFlowSpec.

When the session's runtime target is ``acxd`` (Agentic CX Designer replaces
Lex + AI Prompt + AgentCore Gateway), the Classic interview additionally
captures, per business operation, the *flow design*: the ordered steps, the
ACXD node type of each step and — the one decision Lex never asked for —
whether that step is **deterministic** (fixed behaviour) or **generative**
(an LLM decides wording or routing at runtime). Every such decision must be
explicitly confirmed by the user before generation is allowed.

Everything else ACXD needs is already in the Classic contract:

* business operations, fields, constraints, data sources → ``OperationSpec``
* HTTP contract the Data Requests call                   → OpenAPI asset
* FAQ content that becomes Knowledge Base articles       → FAQ asset
* infrastructure (API Gateway, Lambda, DB)               → ``InfrastructureSpec``

So this spec holds only what has no Classic home: flow plans with
determinism decisions, system flows, guardrails, the KB topic list and the
ACXD application settings. It is persisted next to the other sibling specs
at ``sessions/{sid}/state/acxd_flow_spec.json``.

Hard rules enforced here (not in the prompt):

* ``upsert_acxd_flow_plan`` never stores ``user_confirmed=True``. A step is
  confirmed only through ``confirm_acxd_flow_steps``, so the LLM cannot
  propose and confirm in one call.
* Steps whose ``decision_category`` is money / refund / authorization /
  eligibility / compliance / identity are always deterministic. A generative
  label on such a step is coerced and reported; there is no "user insisted"
  exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field
from strands import tool

from tools.acxd_contract import LANGUAGE_CODES, canonical_language
from tools.validate_acxd_flow import GENERATIVE_NODE_TYPES, SUPPORTED_NODE_TYPES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime target
# ---------------------------------------------------------------------------

RUNTIME_TARGET_CLASSIC = "classic"
RUNTIME_TARGET_ACXD = "acxd"
RUNTIME_TARGETS = (RUNTIME_TARGET_CLASSIC, RUNTIME_TARGET_ACXD)

_RUNTIME_TARGET_FILE = "runtime_target.json"
_SPEC_FILE = "acxd_flow_spec.json"

#: Flow roles. Exactly one ``operation`` flow per OperationSpec, plus the
#: system-event flows ``Application.defaultFlows`` can reference and the two
#: system flows that are NOT default-behaviour targets: ``followup`` (the
#: "anything else?" flow every operation's success path redirects to, R3) and
#: ``agent_request`` (the one ROUTABLE system flow, so "connect me to a human"
#: has something to match — EscalationFlow is a default behaviour, not a
#: routing target, R2).
FLOW_ROLES = (
    "operation", "welcome", "fallback", "unknown", "escalation",
    "frustration", "help", "repeat", "resume", "followup", "agent_request",
)
SYSTEM_FLOW_ROLES = tuple(r for r in FLOW_ROLES if r != "operation")
#: System flows every ACXD application must have (validate_acxd_flow_spec).
#: ``followup`` / ``agent_request`` are deliberately NOT required of the
#: interview: the flow generator emits them deterministically whether or not
#: they were planned, so a plan that predates them still generates a working
#: application.
REQUIRED_SYSTEM_FLOW_ROLES = ("welcome", "fallback", "escalation")

DETERMINISM_LABELS = ("deterministic", "generative")

#: Decision categories whose steps are ALWAYS deterministic.
DETERMINISTIC_ONLY_CATEGORIES = frozenset({
    "money", "refund", "payment", "authorization", "eligibility",
    "compliance", "identity",
})
DECISION_CATEGORIES = tuple(sorted(DETERMINISTIC_ONLY_CATEGORIES)) + ("general",)

#: How the application talks. 'generative' is the default for an LLM-run agent:
#: a generative_journey carries each operation's conversation and the flow only
#: pins down what must be exact. 'scripted' is the customer's explicit choice of
#: a scenario-driven agent (every step a fixed node).
CONVERSATION_STYLES = ("generative", "scripted")

#: Slot kinds a generative_journey must NOT capture: a value whose exactness
#: the runtime checks (regex, phone, identifier). These stay on a user_choice
#: step so F1 can re-ask on a format miss; an LLM paraphrase of "GC-20260902"
#: is not a lookup key.
STRICT_FORMAT_BUILTIN_TYPES = frozenset({
    "NLX.PhoneNumber", "NLX.AlphaNumeric", "NLX.Email", "NLX.Url",
})

SPEECH_ENGINES = ("agentic_voice", "transcribe", "speech_to_speech")
CHANNELS = ("voice", "chat")
#: The Agentic CX contact-flow block accepts at most 10 context variables.
MAX_CONTEXT_VARIABLES = 10

_FLOW_ID_RE = re.compile(r"^[A-Za-z]{3,64}$")

#: Names an LLM tends to invent for ACXD node types → the real node type.
#: Anything not in SUPPORTED_NODE_TYPES after aliasing is rejected with the
#: valid list, so a wrong name never reaches the spec or the generator.
NODE_TYPE_ALIASES = {
    "message": "basic", "say": "basic", "speak": "basic", "prompt": "basic",
    "text": "basic", "static_message": "basic", "announcement": "basic",
    "generative_message": "generative_text", "generative_response": "generative_text",
    "generate_message": "generative_text", "llm_message": "generative_text",
    "ai_message": "generative_text", "generative": "generative_text",
    # Collecting a value is the SDK's user_choice node (slot / built-in type
    # capture); user_input is open-ended intent capture. The planner's older
    # words for "collect" therefore map to user_choice.
    "input": "user_choice", "collect": "user_choice", "slot": "user_choice",
    "collect_input": "user_choice", "ask": "user_choice", "dtmf": "user_choice",
    "menu": "user_choice", "options": "user_choice", "intent": "user_input",
    "branch": "choice", "condition": "choice", "if": "choice", "decision": "choice",
    "route": "choice", "router": "choice", "switch": "choice", "compare": "choice",
    "api_call": "data_request", "api": "data_request", "lambda": "data_request",
    "tool": "data_request", "lookup": "data_request", "query": "data_request",
    "db_lookup": "data_request", "data": "data_request", "http": "data_request",
    "kb": "knowledge_base", "faq": "knowledge_base", "knowledge": "knowledge_base",
    "rag": "knowledge_base", "search": "knowledge_base",
    "escalation": "escalate", "transfer": "escalate", "handoff": "escalate",
    "agent_transfer": "escalate", "human": "escalate", "queue_transfer": "escalate",
    "end_call": "end", "end_conversation": "end", "hangup": "end", "complete": "end",
    "finish": "end", "disconnect": "end", "terminate": "end", "goodbye": "end",
    # Intent routing is `user_input` + a `redirect` to
    # {System.capturedFlow:NLX.System} — proven live 2026-09-12. `intent_capture`
    # is in the SDK enum but is not a deployable node (the palette has none and
    # its metadata is dropped), so it maps to the node that DOES capture an
    # intent. It is NOT a generative journey: an LLM-classifier welcome flow
    # recognized nothing and the application never routed a single utterance.
    "intent_capture": "user_input",
    "intent_router": "user_input", "classify": "user_input",
    "intent_routing": "user_input", "nlu": "user_input",
    "journey": "generative_journey", "agent": "generative_journey", "task": "generative_task",
    "jump": "redirect", "goto": "redirect", "subflow": "redirect", "call_flow": "redirect",
    "set": "define", "assign": "define", "variable": "define",
    "delay": "wait", "pause": "wait", "sleep": "wait",
    "comment": "note", "map": "transform", "format": "transform", "repeat": "loop", "retry": "loop",
}


def canonical_node_type(raw: Optional[str]) -> Optional[str]:
    """Return the real ACXD node type for *raw*, or ``None`` when unknown."""
    if not raw:
        return None
    key = re.sub(r"[\s\-]+", "_", str(raw).strip().lower())
    key = re.sub(r"\(.*\)$", "", key).strip("_")     # 'escalation(native)' → 'escalation'
    key = key.removesuffix("_node")
    if key in NODE_TYPE_ALIASES:          # aliases win: intent_capture → generative_journey
        return NODE_TYPE_ALIASES[key]
    if key in SUPPORTED_NODE_TYPES:
        return key
    return None


def _state_dir(session_id: Optional[str]) -> Optional[Path]:
    mount = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    if not session_id or not os.path.isdir(mount):
        return None
    from tools.path_safety import path_under
    state = path_under(Path(mount) / "sessions", session_id, "state")
    if state is None:
        logger.warning("[ACXDFlowSpec] rejected a session id that is not a plain path segment")
    return state


def _current_session_id() -> Optional[str]:
    try:
        from tools.session_context import current_session_id
        return current_session_id.get()
    except Exception:  # pragma: no cover
        return None


def _read_json(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # A half-written NFS file (seen live when two tool threads raced on the
        # same tmp name) must not shadow the S3 copy: report None so callers
        # fall back to the workspace read.
        logger.warning("[ACXDFlowSpec] read failed for %s: %s", path, e)
        return None


def _write_json_atomic(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        logger.warning("[ACXDFlowSpec] write failed for %s: %s", path, e)
        return False


def _workspace(session_id: Optional[str]):
    """S3-backed workspace for *session_id* (stateless, safe to construct ad hoc).

    ``ensure_workspace()`` needs the contextvar-bound session; the runtime
    target is set from app.py before a turn is bound, so build one directly.
    """
    if not session_id:
        return None
    try:
        from tools.project_workspace import ProjectWorkspace, get_workspace_for
        return get_workspace_for(session_id) or ProjectWorkspace(session_id)
    except Exception as e:  # pragma: no cover - storage outage
        logger.debug("[ACXDFlowSpec] workspace unavailable: %s", e)
        return None


_runtime_target_cache: dict[str, str] = {}


def set_runtime_target(session_id: str, target: str) -> bool:
    """Persist the session's runtime target (chosen on the start screen).

    Written once at session start, before any spec exists. Copied into
    ``InfrastructureSpec.runtime_target`` when that spec is saved, so a
    downloaded bundle carries the decision even without the state dir.
    Stored in memory, on the NFS state dir when mounted, and in the S3
    workspace (``state/runtime_target.json``) — the ECS task does not
    always have the S3 Files mount, and local dev never does.
    """
    if target not in RUNTIME_TARGETS or not session_id:
        return False
    _runtime_target_cache[session_id] = target
    payload = {"runtime_target": target}
    ok = False
    state = _state_dir(session_id)
    if state is not None:
        ok = _write_json_atomic(state / _RUNTIME_TARGET_FILE, payload)
    ws = _workspace(session_id)
    if ws is not None:
        try:
            ok = bool(ws._save_json([_RUNTIME_TARGET_FILE], payload)) or ok
        except Exception as e:  # pragma: no cover
            logger.debug("[ACXDFlowSpec] workspace persist skipped: %s", e)
    return True  # the in-memory record alone is enough for this process


def _infra_runtime_target(sid: str) -> Optional[str]:
    """``InfrastructureSpec.runtime_target`` — but ONLY when the spec store's
    current session is the one asked about.

    ``get_infrastructure_spec()`` is scoped by the ``current_session_id``
    ContextVar, not by an argument. The WebSocket connect handler asks about a
    brand-new session while that ContextVar may still name the previous
    session of the same connection loop; answering with that session's spec
    echoed ``classic`` for a fresh session and the start screen's ACXD choice
    was overwritten before it was sent (live on dev, 2026-09-10).
    """
    if sid != _current_session_id():
        return None
    try:
        from tools.spec_manager import get_infrastructure_spec
        infra = get_infrastructure_spec()
        target = getattr(infra, "runtime_target", None) if infra else None
        return target if target in RUNTIME_TARGETS else None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[ACXDFlowSpec] infra spec lookup failed: %s", e)
        return None


def get_runtime_target(session_id: Optional[str] = None) -> str:
    """Return ``classic`` or ``acxd`` for the session.

    Precedence: saved ``InfrastructureSpec.runtime_target`` → in-memory
    seed → NFS seed → S3 workspace seed → ``classic`` (v2 sessions
    predate the field).
    """
    sid = session_id or _current_session_id()
    if not sid:
        return RUNTIME_TARGET_CLASSIC
    infra_target = _infra_runtime_target(sid)
    if infra_target:
        return infra_target
    cached = _runtime_target_cache.get(sid)
    if cached in RUNTIME_TARGETS:
        return cached
    state = _state_dir(sid)
    data = _read_json(state / _RUNTIME_TARGET_FILE) if state else None
    if not data:
        ws = _workspace(sid)
        if ws is not None:
            try:
                data = ws._load_json([_RUNTIME_TARGET_FILE])
            except Exception as e:  # pragma: no cover
                logger.debug("[ACXDFlowSpec] workspace read skipped: %s", e)
    target = (data or {}).get("runtime_target")
    if target in RUNTIME_TARGETS:
        _runtime_target_cache[sid] = target
        return target
    return RUNTIME_TARGET_CLASSIC


def get_runtime_target_if_set(session_id: Optional[str] = None) -> Optional[str]:
    """Like :func:`get_runtime_target` but ``None`` when nothing was ever persisted.

    Used for the ``connected`` echo: a fresh, unseeded session must not push
    ``classic`` at the client, or it overwrites a start-screen choice the user
    has made but not yet sent.
    """
    sid = session_id or _current_session_id()
    if not sid:
        return None
    infra_target = _infra_runtime_target(sid)
    if infra_target:
        return infra_target
    if _runtime_target_cache.get(sid) in RUNTIME_TARGETS:
        return _runtime_target_cache[sid]
    state = _state_dir(sid)
    data = _read_json(state / _RUNTIME_TARGET_FILE) if state else None
    if not data:
        ws = _workspace(sid)
        if ws is not None:
            try:
                data = ws._load_json([_RUNTIME_TARGET_FILE])
            except Exception:  # pragma: no cover
                data = None
    target = (data or {}).get("runtime_target")
    return target if target in RUNTIME_TARGETS else None


def clear_runtime_target(session_id: Optional[str]) -> None:
    """Drop the in-memory seed (session cleanup / tests)."""
    if session_id:
        _runtime_target_cache.pop(session_id, None)


def is_acxd_target(session_id: Optional[str] = None) -> bool:
    return get_runtime_target(session_id) == RUNTIME_TARGET_ACXD


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ACXDNodeStep(_Model):
    """One planned step of a flow, with its determinism decision."""

    step: int = Field(description="1-based order within the flow")
    description: str = Field(description="What happens at this step, in plain language")
    node_type: str = Field(description=f"ACXD node type; one of {sorted(SUPPORTED_NODE_TYPES)}")
    determinism: str = Field(default="deterministic",
                             description="'deterministic' (fixed) or 'generative' (LLM-driven)")
    determinism_rationale: Optional[str] = Field(
        default=None, description="Why this label was recommended, in plain language")
    decision_category: str = Field(
        default="general",
        description=f"What the step decides; one of {DECISION_CATEGORIES}. "
                    "Money/refund/payment/authorization/eligibility/compliance/identity "
                    "steps are always deterministic.")
    data_request_id: Optional[str] = Field(
        default=None,
        description="For data_request steps: the Data Request to call. Defaults to the "
                    "flow's operation_id (one Data Request per operation).")
    redirect_flow_id: Optional[str] = Field(
        default=None,
        description="For redirect steps: the flow_id this step hands the conversation to "
                    "(e.g. the customer-info search flow when the order number is not found). "
                    "The generated flow must redirect there; system hand-offs (follow-up, "
                    "escalation) need not be named.")
    captures: List[str] = Field(
        default_factory=list,
        description="generative_journey steps only: names of the flow's slots the journey "
                    "collects in free conversation (e.g. ['reason', 'preferredDate']). A value "
                    "with a strict format — a regex, a phone number, an identifier — is never "
                    "captured here: it gets its own user_choice step, which the runtime checks "
                    "character by character.")
    slot: Optional[str] = Field(
        default=None,
        description="user_choice steps: the flow slot this step collects (e.g. 'orderNumber'). "
                    "Required for a strict-format slot when the flow also has a generative_journey, "
                    "so the plan shows which values stay deterministic.")
    journey_tools: List[str] = Field(
        default_factory=list,
        description="generative_journey steps only: 'knowledge_base' lets the journey answer "
                    "side questions from the FAQ knowledge base while it collects values.")
    template: Optional[str] = Field(
        default=None,
        description="The exact sentence the customer approved for this step, with placeholders: "
                    "for a basic / generative_text result step the announcement (\"예약이 접수되었습니다. "
                    "예약번호는 {createReservation.reservationId:NLX.Variable}이며 …\" — for a "
                    "generative_text it is the deterministic fallback spoken when the workspace has "
                    "no model), for a generative_journey the read-back of the captured values. "
                    "Propose one in the customer's register, ask once whether to keep it or change "
                    "it, and store what they approved; the generator uses it verbatim.")
    user_confirmed: bool = Field(default=False)
    confirmation_pending_reason: Optional[str] = Field(
        default=None, description="Set when a re-upsert changed the decision and reset confirmation")


class ACXDSlotPlan(_Model):
    name: str
    type: str = "text"
    sensitive: bool = False
    examples: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    regex: Optional[str] = None
    field_name: Optional[str] = Field(
        default=None, description="OperationSpec input field this slot fills (constraints come from its FieldSpec)")


class ACXDFlowPlan(_Model):
    """Interview-level plan for one ACXD flow (before node-graph generation)."""

    flow_id: str = Field(description="Letters only, 3-64 chars (ACXD constraint)")
    purpose: str
    display_name: Optional[str] = Field(
        default=None,
        description="Short customer-facing name of the operation in the project language "
                    "(2-4 words, e.g. '배송 조회'); spoken verbatim when the assistant lists "
                    "what it can help with")
    role: str = Field(default="operation", description=f"One of {FLOW_ROLES}")
    customer_initiated: bool = Field(
        default=True,
        description="False for an operation the customer never asks for by itself — e.g. "
                    "recording the call result after a conversation — that other flows reach "
                    "by redirect. Such a flow is left out of the greeting/re-guide menu and is "
                    "not an intent-routing target")
    operation_id: Optional[str] = Field(
        default=None, description="OperationSpec this flow implements (required when role='operation')")
    steps: List[ACXDNodeStep] = Field(default_factory=list)
    slots: List[ACXDSlotPlan] = Field(default_factory=list)
    uses_knowledge_base: bool = False
    escalation_conditions: Optional[str] = None
    confirmed: bool = Field(default=False, description="User approved the whole flow plan")

    @property
    def unconfirmed_steps(self) -> List[int]:
        return [s.step for s in self.steps if not s.user_confirmed]


class ACXDGuardrailPlan(_Model):
    name: str
    trigger: str = Field(default="input", description="'input' or 'output'")
    policy: str = Field(description="The business policy, in plain language")
    detection_method: str = Field(default="auto", description="'regex', 'keyword', 'llmJudge' or 'auto'")
    action: str = Field(default="flag", description="'mask', 'modify', 'route' or 'flag'")
    route_flow_id: Optional[str] = None
    examples: List[str] = Field(default_factory=list)


class ACXDKnowledgeBasePlan(_Model):
    """Only the KB shell — articles are rendered from the FAQ asset."""
    name: Optional[str] = None
    topics: List[str] = Field(default_factory=list)


class ACXDContextVariable(_Model):
    name: str
    type: str = "string"
    description: Optional[str] = None
    from_contact_attribute: Optional[str] = Field(
        default=None, description="Connect attribute the contact flow passes in, e.g. '$.CustomerEndpoint.Address'")


class ACXDApplicationPlan(_Model):
    name: Optional[str] = None
    description: Optional[str] = None
    conversation_style: str = Field(
        default="generative",
        description=f"One of {CONVERSATION_STYLES}. 'generative' (default): each operation is "
                    "run by a generative_journey that talks freely, with deterministic islands "
                    "only where they are needed — mandatory wording, strict-format identifiers, "
                    "money/eligibility/compliance/identity decisions, backend calls, hand-off. "
                    "'scripted': every step is a fixed node; only when the customer explicitly "
                    "asks for a scenario-driven agent.")
    channels: List[str] = Field(default_factory=lambda: ["voice"], description=f"Subset of {CHANNELS}")
    locales: List[str] = Field(default_factory=list, description="Full locale codes, e.g. ['ko-KR','en-US']")
    primary_locale: Optional[str] = None
    speech_engine: str = Field(default="agentic_voice", description=f"One of {SPEECH_ENGINES}")
    idle_chat_timeout_seconds: Optional[int] = Field(default=None, description="Chat only")
    context_variables: List[ACXDContextVariable] = Field(default_factory=list)
    default_flows: dict = Field(default_factory=dict, description="role → flow_id; derived from system flows when empty")
    environment: str = "development"


class ACXDFlowSpec(_Model):
    flows: List[ACXDFlowPlan] = Field(default_factory=list)
    guardrails: List[ACXDGuardrailPlan] = Field(default_factory=list)
    knowledge_base: ACXDKnowledgeBasePlan = Field(default_factory=ACXDKnowledgeBasePlan)
    application: ACXDApplicationPlan = Field(default_factory=ACXDApplicationPlan)
    notes: Optional[str] = None

    def flow(self, flow_id: str) -> Optional[ACXDFlowPlan]:
        return next((f for f in self.flows if f.flow_id == flow_id), None)

    def operation_flows(self) -> List[ACXDFlowPlan]:
        return [f for f in self.flows if f.role == "operation"]

    def system_flows(self) -> dict:
        return {f.role: f.flow_id for f in self.flows if f.role != "operation"}


# ---------------------------------------------------------------------------
# Persistence (mirrors contact_flow_spec: NFS state dir, S3 workspace fallback)
# ---------------------------------------------------------------------------

def get_acxd_flow_spec(session_id: Optional[str] = None) -> Optional[ACXDFlowSpec]:
    sid = session_id or _current_session_id()
    if not sid:
        return None
    state = _state_dir(sid)
    data = _read_json(state / _SPEC_FILE) if state else None
    if data is None:
        ws = _workspace(sid)
        if ws is not None:
            try:
                data = ws._load_json([_SPEC_FILE])
            except Exception as e:
                logger.warning("[ACXDFlowSpec] workspace restore failed: %s", e)
            if data is None:
                # ProjectWorkspace reads NFS first and gives up on invalid JSON
                # there; the S3 copy is the durable one, so read it directly.
                try:
                    raw = ws._get_s3(ws._s3_key(_SPEC_FILE))
                    data = json.loads(raw) if raw else None
                except Exception as e:
                    logger.warning("[ACXDFlowSpec] S3 restore failed: %s", e)
    if not data:
        return None
    try:
        return ACXDFlowSpec.model_validate(data)
    except Exception as e:
        logger.warning("[ACXDFlowSpec] stored spec invalid, ignoring: %s", e)
        return None


def save_acxd_flow_spec(spec: ACXDFlowSpec, session_id: Optional[str] = None) -> bool:
    sid = session_id or _current_session_id()
    if not sid:
        return False
    payload = spec.model_dump()
    ok = False
    state = _state_dir(sid)
    if state is not None:
        ok = _write_json_atomic(state / _SPEC_FILE, payload)
    ws = _workspace(sid)
    if ws is not None:
        try:
            ok = bool(ws._save_json([_SPEC_FILE], payload)) or ok
        except Exception as e:
            logger.debug("[ACXDFlowSpec] workspace persist skipped: %s", e)
    return ok


def _load_or_new() -> ACXDFlowSpec:
    return get_acxd_flow_spec() or ACXDFlowSpec()


# The orchestrator runs independent tool calls of one turn in parallel, and
# every mutating tool below is a read-modify-write of the whole spec file.
# Live run 1 lost three guardrails and a confirmation to exactly that race, so
# mutations are serialized per session.
_spec_locks: dict[str, threading.RLock] = {}
_spec_locks_guard = threading.Lock()


def _spec_lock(session_id: Optional[str]) -> threading.RLock:
    key = session_id or "__no_session__"
    with _spec_locks_guard:
        lock = _spec_locks.get(key)
        if lock is None:
            lock = _spec_locks[key] = threading.RLock()
        return lock


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def enforce_determinism_policy(step: ACXDNodeStep) -> Optional[str]:
    """Coerce money/compliance-class steps to deterministic. Returns a note when coerced."""
    category = (step.decision_category or "general").lower()
    if category in DETERMINISTIC_ONLY_CATEGORIES:
        if step.determinism != "deterministic" or step.node_type in GENERATIVE_NODE_TYPES:
            step.determinism = "deterministic"
            if step.node_type in GENERATIVE_NODE_TYPES:
                step.node_type = "choice"
            return (f"step {step.step}: '{category}' decisions are always deterministic — "
                    "label coerced (no exceptions)")
    if step.node_type in GENERATIVE_NODE_TYPES and step.determinism != "generative":
        step.determinism = "generative"
        return f"step {step.step}: node type '{step.node_type}' is generative by nature — label corrected"
    return None


DATE_LIKE_TYPES = frozenset({"date", "datetime", "time", "nlx.date", "nlx.time"})


def is_strict_format_slot(slot: ACXDSlotPlan, field: Optional[dict] = None) -> bool:
    """A value the runtime must check exactly — never a journey's to paraphrase.

    ``field`` is the OperationSpec input field the slot fills, when known: a
    field with a ``date_format`` (or a date/datetime ``field_type``) is strict
    too. Live (2026-09-20): `preferredDate` (YYYY-MM-DD in the spec) was a
    journey capture; the journey stored "다음주" verbatim, the backend got it,
    and the confirmation and the result both repeated "다음주" instead of a date.
    A user_choice over that slot becomes ``NLX.Date`` (S9) and delivers an ISO date."""
    kind = str(slot.type or "").strip()
    if slot.regex:
        return True
    if kind in STRICT_FORMAT_BUILTIN_TYPES:
        return True
    lowered = kind.lower()
    if lowered in DATE_LIKE_TYPES:
        return True
    if isinstance(field, dict):
        if field.get("date_format") or str(field.get("field_type") or "").lower() in ("date", "datetime"):
            return True
        if field.get("pattern"):
            return True
    return lowered in {"phone", "phonenumber", "phone_number", "email", "alphanumeric", "identifier"}


def spec_input_fields(operation_id: Optional[str]) -> dict[str, dict]:
    """The OperationSpec's input fields by name (and by snake/camel spelling),
    as plain dicts; empty when the spec is unavailable."""
    if not operation_id:
        return {}
    try:
        from tools.spec_manager import get_all_specs
        spec = (get_all_specs() or {}).get(operation_id)
    except Exception:
        return {}
    if spec is None:
        return {}
    out: dict[str, dict] = {}
    for f in (getattr(spec, "input_fields", None) or []):
        d = f.model_dump() if hasattr(f, "model_dump") else (dict(f) if isinstance(f, dict) else {})
        name = str(d.get("name") or "")
        if not name:
            continue
        out[name] = d
        out[name.lower()] = d
        out[re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()] = d
    return out


def generative_style_notes(spec, role: Optional[str], steps: List["ACXDNodeStep"],
                           slots: List[ACXDSlotPlan]) -> List[str]:
    """What a plan under the generative style still gets wrong — returned to the
    interviewer as notes so it fixes the plan before the user sees it.

    Live (2026-09-17): the first plan of a real interview had no journey in the
    lookup flow and asked product/service type as menus; a person had to send
    the plan back. Both are the two mistakes this reports:
    - an operation flow without a generative_journey (nothing collects what the
      customer explains in their own words)
    - a user_choice over an enum slot whose values a person describes (product
      type, reason) — a menu where a journey capture belongs; strict-format and
      identity values stay user_choice
    """
    notes: List[str] = []
    style = getattr(getattr(spec, "application", None), "conversation_style", "generative")
    if style != "generative" or (role or "operation") != "operation" or not steps:
        return notes
    if not any(st.node_type == "generative_journey" for st in steps):
        notes.append(
            "generative style: this operation flow has no generative_journey step — values the "
            "customer explains in their own words (reason, product, preferences, name, address) "
            "belong to ONE journey with `captures`; keep user_choice only for strict-format values "
            "and identity, data_request/choice/basic for the exact parts. Re-plan unless the "
            "customer explicitly asked for a scripted flow.")
    by_name = {sl.name: sl for sl in slots}
    for st in steps:
        if st.node_type != "user_choice" or not st.slot:
            continue
        slot = by_name.get(st.slot)
        if slot is None or is_strict_format_slot(slot):
            continue
        # an enum slot in a plan is `type: custom` with its values as examples
        vals = list(slot.examples or []) if str(slot.type or "").lower() in ("custom", "customenum", "enum") else []
        if len(vals) >= 2:
            notes.append(
                f"step {st.step}: user_choice over enum slot '{st.slot}' ({len(vals)} values) is a menu; "
                "under the generative style a value the customer describes belongs to the journey's "
                "`captures` (the journey classifies the description onto the values). Keep user_choice "
                "only if the customer wants a fixed menu here.")
    return notes


def enforce_capture_policy(step: ACXDNodeStep, slots: List[ACXDSlotPlan],
                           spec_fields: Optional[dict] = None) -> List[str]:
    """Keep a generative_journey's ``captures`` to values a conversation may
    collect. Returns notes for every coercion; mutates ``step``.

    - a strict-format slot (regex / phone / identifier / date) is dropped from
      the journey: it needs its own user_choice step so F1 can re-ask on a miss
      (and a date slot becomes NLX.Date, which delivers an ISO date instead of
      the words the caller used)
    - a name that is not one of the flow's slots is dropped (nothing to fill)
    - a non-journey step carries no captures at all
    """
    notes: List[str] = []
    if step.node_type != "generative_journey":
        if step.captures or step.journey_tools:
            step.captures, step.journey_tools = [], []
            notes.append(f"step {step.step}: captures/journey_tools apply to generative_journey steps only — cleared")
        return notes
    by_name = {s.name: s for s in slots}
    fields = spec_fields or {}
    kept: List[str] = []
    for name in step.captures:
        name = str(name).strip()
        if not name or name in kept:
            continue
        slot = by_name.get(name)
        if slot is None:
            notes.append(f"step {step.step}: captures '{name}' is not one of the flow's slots — dropped")
            continue
        field = fields.get(slot.field_name or "") or fields.get(name) or fields.get(name.lower())
        if is_strict_format_slot(slot, field):
            date_like = bool(field and (field.get("date_format") or str(field.get("field_type") or "").lower()
                                        in ("date", "datetime"))) or str(slot.type or "").lower() in DATE_LIKE_TYPES
            why = (f"a date ({(field or {}).get('date_format') or 'YYYY-MM-DD'}) — a journey stores the words "
                   f"the caller used ('다음주', 'next week'), a user_choice slot becomes NLX.Date and delivers "
                   f"the ISO date" if date_like
                   else f"a strict format ({slot.type}{' + regex' if slot.regex else ''}) — a journey must not "
                        f"capture it")
            notes.append(f"step {step.step}: '{name}' has {why}; plan a user_choice step for it")
            continue
        kept.append(name)
    step.captures = kept
    tools = []
    for tool in step.journey_tools:
        tool = str(tool).strip()
        if tool == "knowledge_base":
            tools.append(tool)
        elif tool:
            notes.append(f"step {step.step}: journey tool '{tool}' is not supported — a journey collects values; "
                         "the flow's data_request step calls the backend")
    step.journey_tools = tools
    return notes


def validate_acxd_flow_spec(spec: ACXDFlowSpec, known_operation_ids: Optional[set] = None) -> List[str]:
    """Deterministic readiness check; a non-empty list blocks generation."""
    problems: List[str] = []
    seen: set[str] = set()
    for f in spec.flows:
        if not _FLOW_ID_RE.match(f.flow_id or ""):
            problems.append(f"flow '{f.flow_id}': flowId must be letters only, 3-64 chars")
        if f.flow_id in seen:
            problems.append(f"flow '{f.flow_id}': duplicate flow_id")
        seen.add(f.flow_id)
        if f.role not in FLOW_ROLES:
            problems.append(f"flow '{f.flow_id}': unknown role '{f.role}'")
        if f.role == "operation":
            if not f.operation_id:
                problems.append(f"flow '{f.flow_id}': operation flows must name their operation_id")
            elif known_operation_ids is not None and f.operation_id not in known_operation_ids:
                problems.append(f"flow '{f.flow_id}': operation_id '{f.operation_id}' has no OperationSpec")
        if not f.steps:
            problems.append(f"flow '{f.flow_id}': no steps planned")
        for s in f.steps:
            if s.node_type not in SUPPORTED_NODE_TYPES:
                problems.append(f"flow '{f.flow_id}' step {s.step}: unsupported node type '{s.node_type}'")
            if s.determinism not in DETERMINISM_LABELS:
                problems.append(f"flow '{f.flow_id}' step {s.step}: determinism must be one of {DETERMINISM_LABELS}")
            if (s.decision_category or "general").lower() in DETERMINISTIC_ONLY_CATEGORIES \
                    and s.determinism != "deterministic":
                problems.append(f"flow '{f.flow_id}' step {s.step}: '{s.decision_category}' step must be deterministic")
            if not s.user_confirmed:
                problems.append(f"flow '{f.flow_id}' step {s.step}: determinism decision not confirmed by the user")
        if f.role == "operation":
            journeys = [s for s in f.steps if s.node_type == "generative_journey"]
            slot_names = {sl.name for sl in f.slots}
            for s in journeys:
                if not s.captures:
                    problems.append(f"flow '{f.flow_id}' step {s.step}: a generative_journey must name the slots "
                                    f"it collects in 'captures' (one of {sorted(slot_names)}) — or be a "
                                    "generative_text step if it only talks")
                for name in s.captures:
                    if name not in slot_names:
                        problems.append(f"flow '{f.flow_id}' step {s.step}: captures '{name}' is not a slot of this flow")
            captured_by_choice = {s.slot for s in f.steps if s.node_type == "user_choice" and s.slot}
            if journeys:
                for sl in f.slots:
                    if is_strict_format_slot(sl) and sl.name not in captured_by_choice:
                        problems.append(f"flow '{f.flow_id}': slot '{sl.name}' has a strict format and no "
                                        f"user_choice step declares slot='{sl.name}' — collect it "
                                        "deterministically (the journey must not)")
        if not f.confirmed:
            problems.append(f"flow '{f.flow_id}': flow plan not approved by the user")
    roles = spec.system_flows()
    for role in REQUIRED_SYSTEM_FLOW_ROLES:
        if role not in roles:
            problems.append(f"missing system flow with role '{role}'")
    for role in SYSTEM_FLOW_ROLES:
        holders = [f.flow_id for f in spec.flows if f.role == role]
        if len(holders) > 1:
            problems.append(f"system role '{role}' is planned by {len(holders)} flows {holders}: "
                            f"keep one (remove_acxd_flow_plan) — duplicates ship as extra flows")
    if known_operation_ids is not None:
        covered = {f.operation_id for f in spec.operation_flows()}
        for op in sorted(known_operation_ids - covered):
            problems.append(f"operation '{op}' has no ACXD flow plan (one operation flow per OperationSpec)")
    for g in spec.guardrails:
        if g.action == "route" and (not g.route_flow_id or g.route_flow_id not in seen):
            problems.append(f"guardrail '{g.name}': route action needs an existing route_flow_id")
    app = spec.application
    if app.conversation_style not in CONVERSATION_STYLES:
        problems.append(f"application: conversation_style must be one of {CONVERSATION_STYLES}")
    if len(app.context_variables) > MAX_CONTEXT_VARIABLES:
        problems.append(f"application: at most {MAX_CONTEXT_VARIABLES} context variables (Agentic CX block limit)")
    for ch in app.channels:
        if ch not in CHANNELS:
            problems.append(f"application: unknown channel '{ch}'")
    if app.speech_engine not in SPEECH_ENGINES:
        problems.append(f"application: speech_engine must be one of {SPEECH_ENGINES}")
    for loc in app.locales:
        if loc not in LANGUAGE_CODES:
            problems.append(f"application: locale '{loc}' is not a supported full locale code")
    return problems


def acxd_flow_spec_ready() -> tuple[bool, List[str]]:
    """Used by interview completion when the runtime target is acxd."""
    spec = get_acxd_flow_spec()
    if spec is None or not spec.flows:
        return False, ["ACXD flow spec not saved yet"]
    known = None
    try:
        from tools.spec_manager import get_all_specs
        known = set(get_all_specs().keys())
    except Exception:  # pragma: no cover
        pass
    problems = validate_acxd_flow_spec(spec, known)
    return not problems, problems


# ---------------------------------------------------------------------------
# Interview tools
# ---------------------------------------------------------------------------

def _as_list(value, name: str) -> list:
    """Accept a real list or its JSON-string form (the model sometimes passes
    `steps="[...]"`; the Classic behaviors parameter has the same history)."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{name} must be a JSON list, got a string that is not valid JSON: {e}")
        if not isinstance(parsed, list):
            raise ValueError(f"{name} must be a list")
        return parsed
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(f"{name} must be a list")


def _decision_key(s: ACXDNodeStep) -> tuple:
    """What the user actually confirmed: the determinism class of the step and
    what it decides. A node-type *name* correction inside the same class
    (e.g. an invented 'generative_message' fixed to 'generative_text') does
    not re-open a confirmation; switching a step between deterministic and
    generative, or changing its decision category, does."""
    return (
        "generative" if s.node_type in GENERATIVE_NODE_TYPES else "deterministic",
        s.determinism,
        (s.decision_category or "general").lower(),
    )


@tool
def upsert_acxd_flow_plan(
    flow_id: str,
    purpose: str,
    role: str = "operation",
    operation_id: str = None,
    steps: Union[list[dict], str] = None,
    slots: Union[list[dict], str] = None,
    uses_knowledge_base: bool = False,
    escalation_conditions: str = None,
    display_name: str = None,
    customer_initiated: bool = True,
) -> dict:
    """
    Propose or update the plan for ONE ACXD flow (runtime target acxd only).

    Call this in interview Phase 3 for each business operation (role='operation',
    one flow per OperationSpec) and for the system flows (role welcome / fallback /
    escalation, optionally unknown / frustration / help / repeat / resume). The
    system flows are BUILT DETERMINISTICALLY from the live-verified routing
    contract, so their step list is a description for the user, not a design the
    generator follows; ``followup`` and ``agent_request`` are emitted whether or
    not they are planned.

    node_type MUST be one of the real ACXD node types (nothing else is accepted):
      deterministic: start, end, basic (fixed message), user_choice (collect ONE value
        into a slot — order number, name, yes/no, a menu pick; this is how ACXD captures
        values), user_input (open-ended "what do you need?" intent capture; pair it
        with a redirect to '{System.capturedFlow:NLX.System}' — that IS intent
        routing), choice (rule branch — NOT 'split'), split (percentage A/B),
        data_request (call a Data Request / backend API), escalate (hand off to a
        human queue), redirect (jump to another flow), wait, note, define, transform, loop
      generative: generative_text (LLM-worded message), generative_task,
        generative_journey (LLM agent for a stretch of conversation that cannot be
        drawn in advance — NEVER for intent routing), knowledge_base (answer from the KB)
    Common wrong names are auto-corrected (message→basic, generative_message→
    generative_text, escalation→escalate, end_call→end, branch→choice,
    intent_capture→user_input); anything else is rejected with this list.

    Each step carries the AI's recommendation: node_type, determinism
    ('deterministic' or 'generative') and a plain-language rationale. This tool
    NEVER records a confirmation — present the steps to the user, and only after
    they agree call confirm_acxd_flow_steps. Re-upserting a step whose decision
    changed (deterministic↔generative, or decision_category) resets its
    confirmation; a pure node-type name correction keeps it.

    Money, refund, payment, authorization, eligibility, compliance and identity
    decisions (decision_category) are always deterministic; a generative label on
    them is coerced and reported back.

    HOW MUCH IS GENERATIVE follows application.conversation_style (default
    'generative', saved with save_acxd_application_settings):
      generative — ONE `generative_journey` step carries the operation's conversation:
        it collects the values a person would explain in their own words (a reason, a
        preference, a description, a choice among options) and answers side questions
        from the FAQ. Name those slots in `captures` and pass
        journey_tools=['knowledge_base'] when the project has an FAQ. Everything else
        stays a fixed node ONLY because it must be exact: a `basic` for wording the
        requirements mandate (consent, legal notice), a `user_choice` (with `slot`) for
        every strict-format value — order number, phone, id, anything with a regex —
        and for identity checks, `data_request` for the backend call, `choice` for a
        money/eligibility/compliance rule, `escalate`/`redirect` for hand-off, and a
        `generative_text` (or a `basic` when the wording is mandated) to announce the
        result. A strict-format slot listed in `captures` is removed and reported.
      scripted — the customer explicitly asked for a scenario-driven agent: every step
        is a fixed node, one `user_choice` per value.

    Args:
        flow_id: Letters only, 3-64 chars (e.g. 'ProcessReturn').
        purpose: What the flow accomplishes.
        role: 'operation' or a system role (welcome, fallback, escalation, ...).
        operation_id: The OperationSpec this flow implements (required for role='operation').
        steps: [{"step":1,"description":"...","node_type":"user_input",
                 "determinism":"deterministic","determinism_rationale":"...",
                 "decision_category":"general|money|refund|...","data_request_id":"...",
                 "slot":"orderNumber" (user_choice), "captures":["reason"] and
                 "journey_tools":["knowledge_base"] (generative_journey),
                 "template":"예약이 접수되었습니다. 예약번호는 {createReservation.reservationId:NLX.Variable}…"
                 (the sentence the customer approved for a result step or a journey's read-back)}]
        slots: [{"name":"orderId","type":"text","field_name":"order_id","sensitive":false,
                 "examples":[...],"regex":"..."}]. Omitting `steps` on a re-upsert keeps
                 the planned steps; omitting `slots` keeps the slots.
        uses_knowledge_base: True when the flow answers from the FAQ knowledge base.
        escalation_conditions: When this flow hands off to a human, in plain language.
        customer_initiated: False when the customer never asks for this operation
            by itself (e.g. recording the call result after a conversation, an
            internal follow-up other flows redirect to). Such a flow is not offered
            in the greeting/re-guide menu and is not an intent-routing target.
        display_name: Short customer-facing name of the operation in the project
            language (2-4 words, e.g. '배송 조회', 'Order status'). The assistant
            says it verbatim when it lists what it can help with (fallback
            re-guidance), so give one for every operation flow.

    Returns:
        The saved plan summary, coerced/normalized steps, and the step numbers awaiting confirmation.
    """
    with _spec_lock(_current_session_id()):
        try:
            if not _FLOW_ID_RE.match(flow_id or ""):
                return {"success": False,
                        "error": f"flow_id '{flow_id}' must be letters only, 3-64 chars (ACXD constraint)"}
            if role not in FLOW_ROLES:
                return {"success": False, "error": f"role must be one of {FLOW_ROLES}"}
            if role == "operation" and not operation_id:
                return {"success": False, "error": "operation flows must name their operation_id"}

            spec = _load_or_new()
            existing = spec.flow(flow_id)
            # A system role belongs to exactly ONE flow. Live (Japanese run):
            # the interview planned WelcomeFlow/FallbackFlow/EscalationFlow,
            # then re-planned Welcome/Fallback/Escalation after a restart, and
            # the application shipped nine flows with duplicate system roles.
            if role != "operation":
                holder = next((f for f in spec.flows if f.role == role and f.flow_id != flow_id), None)
                if holder is not None:
                    return {"success": False,
                            "error": (f"role '{role}' is already planned as flow '{holder.flow_id}'. "
                                      f"Upsert flow_id '{holder.flow_id}' to change it, or call "
                                      f"remove_acxd_flow_plan('{holder.flow_id}') first — one flow per system role.")}
            prev_steps = {s.step: s for s in (existing.steps if existing else [])}

            notes: List[str] = []
            new_slots = [ACXDSlotPlan.model_validate(x) for x in _as_list(slots, 'slots')]
            if slots is None and existing is not None:
                new_slots = list(existing.slots)          # a steps-only update keeps the slots
            raw_steps = _as_list(steps, 'steps')
            if not raw_steps and existing is not None and existing.steps:
                # Live (2026-09-16): a slots-only re-upsert wiped the flow's
                # confirmed steps (step_count 0) and cost a restore turn. An
                # omitted step list means "keep them", never "delete them".
                raw_steps = [s.model_dump() for s in existing.steps]
                notes.append(f"steps not given — kept the {len(raw_steps)} planned steps "
                             "(call remove_acxd_flow_plan to drop a flow)")
            new_steps: List[ACXDNodeStep] = []
            for raw in raw_steps:
                raw = dict(raw or {})
                raw.pop("user_confirmed", None)          # never trust a confirmation from the proposer
                raw.pop("confirmation_pending_reason", None)
                requested_type = raw.get("node_type")
                canonical = canonical_node_type(requested_type)
                if canonical is None:
                    return {"success": False,
                            "error": f"step {raw.get('step')}: unknown node_type '{requested_type}'. "
                                     f"Use one of {sorted(SUPPORTED_NODE_TYPES)}"}
                if canonical != requested_type:
                    notes.append(f"step {raw.get('step')}: node_type '{requested_type}' normalized to '{canonical}'")
                raw["node_type"] = canonical
                if canonical == "data_request" and not raw.get("data_request_id"):
                    raw["data_request_id"] = operation_id
                if canonical == "redirect" and raw.get("redirect_flow_id"):
                    # Live (TableNow, 2026-09-20): the proposer wrote the ROLE
                    # ("followup") and the determinism gate later compared it
                    # literally with the generated redirect's "FollowUpFlow".
                    # Store the id the bundle will ship.
                    from tools.acxd_system_flows import resolve_flow_reference
                    known_flow_ids = [f.flow_id for f in spec.flows] + [flow_id]
                    resolved_target = resolve_flow_reference(
                        raw["redirect_flow_id"], known_flow_ids,
                        {"flows": [{"role": f.role, "flow_id": f.flow_id} for f in spec.flows]})
                    if resolved_target != raw["redirect_flow_id"]:
                        notes.append(f"step {raw.get('step')}: redirect_flow_id "
                                     f"'{raw['redirect_flow_id']}' resolved to '{resolved_target}'")
                        raw["redirect_flow_id"] = resolved_target
                s = ACXDNodeStep.model_validate(raw)
                note = enforce_determinism_policy(s)
                if note:
                    notes.append(note)
                notes.extend(enforce_capture_policy(s, new_slots, spec_input_fields(operation_id)))
                prev = prev_steps.get(s.step)
                if prev and prev.user_confirmed and _decision_key(prev) == _decision_key(s):
                    s.user_confirmed = True                # unchanged decision keeps its confirmation
                elif prev and prev.user_confirmed:
                    s.confirmation_pending_reason = "decision changed since confirmation"
                new_steps.append(s)
            new_steps.sort(key=lambda s: s.step)
            notes.extend(generative_style_notes(spec, role, new_steps, new_slots))

            all_confirmed = bool(new_steps) and all(s.user_confirmed for s in new_steps)
            plan = ACXDFlowPlan(
                flow_id=flow_id, purpose=purpose, role=role, operation_id=operation_id,
                display_name=(display_name or "").strip() or (existing.display_name if existing else None),
                customer_initiated=bool(customer_initiated),
                steps=new_steps,
                slots=new_slots,
                uses_knowledge_base=uses_knowledge_base,
                escalation_conditions=escalation_conditions,
                # A plan whose every decision the user already confirmed stays approved.
                confirmed=bool(existing and existing.confirmed and all_confirmed),
            )
            spec.flows = [f for f in spec.flows if f.flow_id != flow_id] + [plan]
            save_acxd_flow_spec(spec)
            return {
                "success": True,
                "flow_id": flow_id,
                "role": role,
                "step_count": len(new_steps),
                "coerced": notes,
                "awaiting_confirmation": plan.unconfirmed_steps,
                "flow_approved": plan.confirmed,
                "message": ("Plan saved. Show the steps and their determinism labels to the user; "
                            "call confirm_acxd_flow_steps only after they explicitly agree."
                            if plan.unconfirmed_steps else "Plan saved; all decisions remain confirmed."),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


@tool
def confirm_acxd_flow_steps(flow_id: str, step_numbers: Union[list[int], str] = None, approve_flow: bool = True) -> dict:
    """
    Record the user's EXPLICIT confirmation of determinism decisions for a flow.

    Call only after the user has agreed in the conversation. Omit step_numbers to
    confirm every step of the flow. approve_flow=True also marks the whole plan
    as approved (the flow preview the user saw).

    Args:
        flow_id: The flow whose steps the user confirmed.
        step_numbers: 1-based step numbers confirmed; None = all steps.
        approve_flow: Mark the flow plan itself as approved.

    Returns:
        Remaining unconfirmed steps for this flow.
    """
    with _spec_lock(_current_session_id()):
        spec = get_acxd_flow_spec()
        plan = spec.flow(flow_id) if spec else None
        if plan is None:
            return {"success": False, "error": f"no plan for flow '{flow_id}' — call upsert_acxd_flow_plan first"}
        numbers = [int(n) for n in _as_list(step_numbers, 'step_numbers')]
        wanted = set(numbers) if numbers else {s.step for s in plan.steps}
        for s in plan.steps:
            if s.step in wanted:
                s.user_confirmed = True
                s.confirmation_pending_reason = None
        if approve_flow and not plan.unconfirmed_steps:
            plan.confirmed = True
        save_acxd_flow_spec(spec)
        return {"success": True, "flow_id": flow_id, "confirmed": sorted(wanted),
                "awaiting_confirmation": plan.unconfirmed_steps, "flow_approved": plan.confirmed}


@tool
def save_acxd_policies(guardrails: Union[list[dict], str] = None, kb_name: str = None, kb_topics: Union[list[str], str] = None) -> dict:
    """
    Save guardrails and the knowledge-base shell (runtime target acxd only).

    Args:
        guardrails: [{"name":"PII Filter","trigger":"input|output","policy":"...",
                      "detection_method":"regex|keyword|llmJudge|auto","action":"mask|modify|route|flag",
                      "route_flow_id":"EscalationFlow","examples":[...]}]
        kb_name: Knowledge base name (articles come from the FAQ asset).
        kb_topics: FAQ topics the KB should cover.
    """
    with _spec_lock(_current_session_id()):
        try:
            spec = _load_or_new()
            if guardrails is not None:
                spec.guardrails = [ACXDGuardrailPlan.model_validate(g) for g in _as_list(guardrails, 'guardrails')]
            if kb_name is not None:
                spec.knowledge_base.name = kb_name
            if kb_topics is not None:
                spec.knowledge_base.topics = [str(t) for t in _as_list(kb_topics, 'kb_topics')]
            save_acxd_flow_spec(spec)
            return {"success": True, "guardrail_count": len(spec.guardrails),
                    "kb_topics": len(spec.knowledge_base.topics)}
        except Exception as e:
            return {"success": False, "error": str(e)}


@tool
def save_acxd_application_settings(
    name: str = None,
    description: str = None,
    channels: Union[list[str], str] = None,
    locales: Union[list[str], str] = None,
    primary_locale: str = None,
    speech_engine: str = None,
    idle_chat_timeout_seconds: int = None,
    context_variables: Union[list[dict], str] = None,
    environment: str = None,
    conversation_style: str = None,
) -> dict:
    """
    Save ACXD application settings (runtime target acxd only).

    Args:
        channels: subset of ['voice','chat'].
        locales: language codes; short codes are canonicalized ('ko' → 'ko-KR').
        speech_engine: 'agentic_voice' (recommended) | 'transcribe' | 'speech_to_speech'.
        idle_chat_timeout_seconds: chat-only idle timeout.
        context_variables: at most 10, [{"name":"customerPhone","type":"string",
                            "from_contact_attribute":"$.CustomerEndpoint.Address"}].
        environment: ACXD deployment environment (default 'development').
        conversation_style: 'generative' (default — a generative_journey runs each
            operation's conversation; fixed nodes only where exactness is required) or
            'scripted' (the customer explicitly wants a scenario-driven agent: every
            step a fixed node). Ask the customer once, early; keep 'generative' unless
            they clearly say otherwise.
    """
    with _spec_lock(_current_session_id()):
        try:
            spec = _load_or_new()
            app = spec.application
            if name is not None:
                app.name = name
            if description is not None:
                app.description = description
            if conversation_style is not None:
                style = str(conversation_style).strip().lower()
                aliases = {"generative": "generative", "journey": "generative", "llm": "generative",
                           "생성형": "generative", "scripted": "scripted", "scenario": "scripted",
                           "deterministic": "scripted", "시나리오": "scripted"}
                if style not in aliases:
                    return {"success": False,
                            "error": f"conversation_style must be one of {CONVERSATION_STYLES}"}
                app.conversation_style = aliases[style]
            if channels is not None:
                app.channels = [str(c) for c in _as_list(channels, 'channels')]
            if locales is not None:
                app.locales = [canonical_language(c) for c in _as_list(locales, 'locales')]
            if primary_locale is not None:
                app.primary_locale = canonical_language(primary_locale)
            if speech_engine is not None:
                app.speech_engine = speech_engine
            if idle_chat_timeout_seconds is not None:
                app.idle_chat_timeout_seconds = idle_chat_timeout_seconds
            if context_variables is not None:
                app.context_variables = [ACXDContextVariable.model_validate(v) for v in _as_list(context_variables, 'context_variables')]
            if environment is not None:
                app.environment = environment
            save_acxd_flow_spec(spec)
            problems = [p for p in validate_acxd_flow_spec(spec) if p.startswith("application:")]
            return {"success": not problems, "application": app.model_dump(), "problems": problems}
        except Exception as e:
            return {"success": False, "error": str(e)}


@tool
def get_acxd_flow_spec_tool() -> dict:
    """
    Retrieve the saved ACXD flow spec with its readiness problems.

    Returns:
        {"spec": ..., "ready": bool, "problems": [...]} or an error when nothing is saved.
    """
    spec = get_acxd_flow_spec()
    if spec is None:
        return {"success": False, "error": "ACXD flow spec not saved yet."}
    ready, problems = acxd_flow_spec_ready()
    return {"success": True, "spec": spec.model_dump(), "ready": ready, "problems": problems}


@tool
def remove_acxd_flow_plan(flow_id: str) -> dict:
    """
    Remove ONE planned ACXD flow from the spec (runtime target acxd only).

    Use it when a flow was planned under the wrong id or role — e.g. a system
    role already held by another flow — before upserting the replacement. The
    user must have agreed to drop the flow; this tool does not ask.

    Args:
        flow_id: The planned flow to remove.

    Returns:
        The remaining flow ids and the spec's readiness problems.
    """
    with _spec_lock(_current_session_id()):
        try:
            spec = _load_or_new()
            if spec.flow(flow_id) is None:
                return {"success": False, "error": f"no planned flow '{flow_id}'",
                        "flows": [f.flow_id for f in spec.flows]}
            spec.flows = [f for f in spec.flows if f.flow_id != flow_id]
            save_acxd_flow_spec(spec)
            ready, problems = acxd_flow_spec_ready()
            return {"success": True, "removed": flow_id,
                    "flows": [f.flow_id for f in spec.flows], "ready": ready, "problems": problems}
        except Exception as e:
            return {"success": False, "error": str(e)}


ACXD_INTERVIEW_TOOLS = [
    remove_acxd_flow_plan,
    upsert_acxd_flow_plan,
    confirm_acxd_flow_steps,
    save_acxd_policies,
    save_acxd_application_settings,
    get_acxd_flow_spec_tool,
]
