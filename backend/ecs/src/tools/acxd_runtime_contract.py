"""ACXD *runtime* contract: normalize a flow onto what the service executes.

Why this exists (live validation, 2026-09-13, Amazon Connect Customer /
Agentic CX Designer, ap-northeast-2): a bundle can pass every schema check,
build without a single issue, deploy — and then answer nothing. The service
accepts encodings it cannot execute and fails silently:

  * one attached slot typed ``text`` instead of ``NLX.Text`` disabled flow
    recognition for the WHOLE application (S1);
  * ``metadata.choice.slotTypeId`` is stored verbatim as the internal slot id,
    so a *type* there points at a slot that never fills (S2);
  * a custom slot type with a single sample value is a one-item menu that
    auto-selects without asking the caller anything (S5);
  * a ``data_request`` node with no ``payload`` posts an empty body (D3), and
    an edge on ``node_status eq 'error'`` matches nothing, so the turn ends
    with "the bot must respond with at least one message" → Fallback (D4);
  * ``generative_text`` stores a variable and sends NO message (M2);
  * slot values persist for the whole session, so a re-entered flow sees the
    old value, ``slot exists`` passes and the question is skipped (S6);
  * an ``escalate`` with a child made Connect report Success, not Escalation
    (R7).

:mod:`tools.acxd_flow_canonicalizer` decides *representation* (which key holds
what, so the SDK serializer does not drop it). This module decides *runtime
behaviour* on top of that: it is the deterministic port of the live findings.
Every rewrite is mechanical and recorded; anything that cannot be repaired
without guessing is reported as a violation instead.

Two entry points, one engine:

    flow, notes = apply_runtime_contract(flow, role="operation", ...)
    problems    = runtime_contract_violations(flow, ...)      # no mutation

``runtime_contract_violations`` returns exactly the residue
``apply_runtime_contract`` could not fix, so a normalized flow gates clean.
``scope`` splits the rules by the context they need: ``"flow"`` for the
checks that read only the flow document (wired into
``validate_acxd_flow.validate_acxd_asset``) and ``"cross"`` for the ones that
need slot type documents, data requests, flow ids and context variables
(wired into the D9 gate in ``validate_acxd_consistency``).

Contract source: the live validation notes in docs/acxd-live-validation.md and
the flow documents that held a conversation on the live service.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import re
import uuid
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

#: The complete set of NLX built-in slot types (S1). This is a CLOSED set:
#: anything else under the ``NLX.`` namespace is not a built-in, and one
#: invalid entry disables recognition for the whole application.
NLX_BUILTIN_SLOT_TYPES = frozenset({
    "NLX.Text", "NLX.Number", "NLX.Date", "NLX.Time", "NLX.Email", "NLX.Name",
    "NLX.PhoneNumber", "NLX.Url", "NLX.Ordinal", "NLX.AlphaNumeric",
    "NLX.Duration",
})

#: There is no boolean built-in (S4): yes/no is a custom slot type.
YES_NO_SLOT_TYPE = "yesNo"

#: Spellings a model uses for the two yes/no outcomes in edge conditions (S8).
_AFFIRMATIVE_CONSTANTS = frozenset({
    "yes", "y", "true", "1", "agree", "agreed", "ok", "okay", "affirmative",
    "예", "네", "응", "그래", "동의", "はい", "ええ", "うん",
})
_NEGATIVE_CONSTANTS = frozenset({
    "no", "n", "false", "0", "disagree", "declined", "decline", "negative",
    "아니요", "아니오", "아뇨", "아니", "いいえ", "いや",
})

#: Generator spellings → the built-in the service actually knows (S1).
SLOT_TYPE_ALIASES = {
    "text": "NLX.Text",
    "string": "NLX.Text",
    "freetext": "NLX.Text",
    "number": "NLX.Number",
    "integer": "NLX.Number",
    "int": "NLX.Number",
    "float": "NLX.Number",
    "quantity": "NLX.Number",
    "date": "NLX.Date",
    "datetime": "NLX.Date",
    "time": "NLX.Time",
    "email": "NLX.Email",
    "name": "NLX.Name",
    "phone": "NLX.PhoneNumber",
    "phonenumber": "NLX.PhoneNumber",
    "phone_number": "NLX.PhoneNumber",
    "mobile": "NLX.PhoneNumber",
    "url": "NLX.Url",
    "ordinal": "NLX.Ordinal",
    "alphanumeric": "NLX.AlphaNumeric",
    "duration": "NLX.Duration",
}

#: Spellings that mean "yes or no" and therefore need the yesNo slot type.
BOOLEAN_ALIASES = frozenset({"boolean", "bool", "yes_no", "yesno", "yn"})

#: Redirect targets that are runtime placeholders rather than flow ids (RX).
SYSTEM_FLOW_PLACEHOLDERS = frozenset({
    "{System.capturedFlow:NLX.System}", "{System.lastFlow:NLX.System}",
})

#: Flow ids the contract fixes by name (R1-R4): the application's system
#: entry points. They are workspace default-behaviour flows, so a bundle under
#: construction can legitimately redirect to one it does not yet carry — a
#: default flow that is missing altogether is reported by the application
#: checks, not by RX, whose job is catching a near-miss on an operation flow id.
SYSTEM_FLOW_IDS = frozenset({
    "WelcomeFlow", "FallbackFlow", "FollowUpFlow", "EscalationFlow",
    "UnknownFlow", "RequestAgentFlow",
})

#: Recovery wording used when the flow has none of its own (R6).
DEFAULT_RETRY_MESSAGE_KO = "죄송합니다, 확인하지 못했습니다. 다시 한 번 말씀해 주세요."
DEFAULT_RETRY_MESSAGE_EN = "Sorry, I could not catch that. Please say it again."

#: The only ``node_status`` values a data_request edge may compare (D4).
NODE_STATUSES = ("success", "failure", "timeout")

#: Edge names the console/generator use for a capture Match branch (S3).
MATCH_EDGE_NAMES = frozenset({"captured", "match", "matched", "flowrecognized"})
NO_MATCH_EDGE_NAMES = frozenset({
    "notcaptured", "nomatch", "no_match", "no match", "nocapture",
    "noflowrecognized", "notrecognized",
})

#: Which gate reports which rule. These are assigned deliberately:
#: ``flow`` is wired into ``validate_acxd_flow.validate_acxd_asset`` (needs the
#: flow document only), ``cross`` into the D9 gate in
#: ``validate_acxd_consistency`` (needs slot type docs, data requests, flow ids
#: and context variables). ``normalizer`` rules are repaired by
#: :func:`apply_runtime_contract` and their residue is reported only to a
#: caller that asks for every scope — the generator's repair loop — so a
#: minimal unit fixture is not failed by a rule about conversation shape.
FLOW_SCOPE_RULES = frozenset({"S1", "S2", "S3", "S8", "M3", "R6", "R7", "D4", "J", "J2", "J3", "J4", "J5", "A2"})
CROSS_SCOPE_RULES = frozenset({"S1", "S5", "D3", "D5", "M1", "RX"})
NORMALIZER_SCOPE_RULES = frozenset({"M2", "R3", "S6"})
ALL_SCOPES = ("flow", "cross", "normalizer")

_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_PLACEHOLDER = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_.]*):NLX\.(Slot|Variable|Context|System|Secret)\}")
_DIGITS_ONLY_REGEX = re.compile(
    r"(?:(?:\[0-9\]|\\d)(?:\{\d+(?:,\d*)?\}|\+|\*)?)+\Z")
_DIGITS = re.compile(r"^[0-9]+$")
#: Korean particles that cling to a label lifted out of a prompt (M2).
_KO_PARTICLE = re.compile(r"(?:은|는|이|가|을|를|의|와|과|도|로|으로|에|에서|에게|께|한테)$")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _derived_id(prefix: str, seed: str) -> str:
    """Deterministic node id that satisfies the service's v4 UUID regex."""
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{prefix}-0000-4000-8000-{digest[:12]}"


def _label(node_id: str, node: dict) -> str:
    return f"{node.get('type', '?')}[{str(node_id)[:8]}]"


def _meta(node: dict) -> dict:
    meta = node.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        node["metadata"] = meta
    return meta


def _edges(node: Any) -> list[dict]:
    children = node.get("childNodes") if isinstance(node, dict) else None
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _edge_name(edge: dict) -> str:
    return str(edge.get("name") or "").strip().lower()


def _normalize_alias(value: Any) -> Optional[str]:
    """Map a generator spelling onto a built-in / yesNo; None when unknown."""
    if not isinstance(value, str):
        return None
    key = value.strip()
    if key in NLX_BUILTIN_SLOT_TYPES:
        return key
    lowered = key.lower()
    if lowered in BOOLEAN_ALIASES:
        return YES_NO_SLOT_TYPE
    return SLOT_TYPE_ALIASES.get(lowered)


def _is_digits_only_regex(pattern: Any) -> bool:
    if not isinstance(pattern, str) or not pattern.strip():
        return False
    core = pattern.strip().lstrip("^").rstrip("$")
    return bool(core) and bool(_DIGITS_ONLY_REGEX.match(core))


def _reachable(nodes: dict, start_id: Optional[str]) -> set[str]:
    if start_id is None or start_id not in nodes:
        return set()
    seen: set[str] = set()
    stack = [start_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes:
            continue
        seen.add(node_id)
        for edge in _edges(nodes[node_id]):
            target = edge.get("nodeId")
            if isinstance(target, str):
                stack.append(target)
    return seen


def _status_condition(status: str) -> dict:
    return {"left": {"type": "node_status"}, "operator": "eq",
            "right": {"type": "constant", "value": status}}


_PLACEHOLDER_SLOT = re.compile(r"\{([A-Za-z_][\w-]*):NLX\.Slot\}")


def _captures_slot(edge: dict, slot: str) -> bool:
    """An edge whose conditions include ``slot <slot> exists`` — the capture edge."""
    for condition in edge.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        left = condition.get("left") or {}
        if left.get("type") == "slot" and left.get("name") == slot and condition.get("operator") == "exists":
            return True
    return False


def _slot_condition(slot: str, operator: str) -> dict:
    return {"left": {"type": "slot", "name": slot}, "operator": operator}


#: F1 — the context counter a flow keeps of wrong-format answers, and how many
#: in a row send the caller down the node's own "not captured" path instead of
#: asking a fourth time.
FORMAT_RETRIES_VAR = "formatRetries"
MAX_FORMAT_RETRIES = 2
#: E2 — words in a raw utterance that mean "connect me to a human", per
#: language. Matched with ``contains`` on ``{System.utterance}`` only on a
#: capture node's 'not captured' path, so a value that happens to contain one
#: of these is unaffected as long as the slot recognised it.
AGENT_REQUEST_WORDS = {
    "ko": ["상담원", "상담사", "직원 연결", "담당자 연결", "사람과", "사람이랑", "사람하고", "사람 연결"],
    "en": ["agent", "Agent", "representative", "Representative", "human", "Human", "operator", "Operator",
           "real person", "speak to someone", "talk to someone"],
    "ja": ["オペレーター", "担当者", "人と話", "係の人", "有人"],
}
_OPTIONAL_SEPARATOR = "[-. /:]?"


def runtime_regex(pattern: Any) -> Optional[str]:
    """The regex a ``matches_regex`` condition must use for a captured slot value.

    The runtime delivers built-in slot values without their separators
    (``GC-20260902`` → ``GC20260902``) — but not always (a phone number arrived
    with dashes in one deployment and without in another) — so every separator
    of a fixed-shape pattern becomes optional and literal letters match either
    case. A pattern with no fixed shape is used as written when it compiles.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    try:
        from tools.acxd_lambda_adapter import skeleton
        parts = skeleton(pattern)
    except Exception:  # pragma: no cover - adapter unavailable in a stub
        parts = None
    if parts is None:
        try:
            re.compile(pattern)
        except re.error:
            return None
        return pattern
    out = ["^"]
    for kind, text in parts:
        if kind == "sep":
            out.append(_OPTIONAL_SEPARATOR)
        elif kind == "lit":
            out.append("".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c) for c in text))
        else:
            out.append(("[0-9]" if text[0] == "d" else "[A-Za-z0-9]") + "{" + str(len(text)) + "}")
    out.append("$")
    return "".join(out)


def _has_status(edge: dict, status: str) -> bool:
    for condition in edge.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        left = condition.get("left") or {}
        right = condition.get("right") or {}
        if left.get("type") == "node_status" and right.get("value") == status:
            return True
    return False


def _clear_modification(slot: str) -> dict:
    return {"type": "slot", "name": slot, "modification": "clear"}


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

class _RuntimeContract:
    """One pass of the runtime contract over one flow document."""

    def __init__(
        self,
        flow: dict,
        *,
        role: Optional[str],
        slot_type_ids: Optional[Iterable[str]],
        slot_type_docs: Optional[dict],
        data_requests: Optional[dict],
        flow_ids: Optional[Iterable[str]],
        context_variables: Optional[Iterable[str]],
        follow_up_flow_id: str,
        escalation_flow_id: str,
        slot_plans: Optional[dict] = None,
        field_labels: Optional[dict] = None,
        field_enums: Optional[dict] = None,
        journey_steps: Optional[list] = None,
        kb_name: Optional[str] = None,
    ) -> None:
        self.flow = flow
        self.role = (role or "").strip().lower() or None
        self.slot_type_ids = set(slot_type_ids) if slot_type_ids is not None else None
        self.slot_type_docs = dict(slot_type_docs or {})
        self.data_requests = dict(data_requests or {})
        self.flow_ids = set(flow_ids) if flow_ids is not None else None
        self.context_variables = set(context_variables or ())
        self.follow_up_flow_id = follow_up_flow_id
        self.escalation_flow_id = escalation_flow_id
        #: the interview's generative_journey steps of this flow, in plan order
        #: ({captures: [slot names], journey_tools: [...], description}); the
        #: n-th journey node in the document realises the n-th step (J2-J5).
        self.journey_steps = [s for s in (journey_steps or []) if isinstance(s, dict)]
        #: the bundle's FAQ knowledge base name, for a journey's knowledgeBase tool
        self.kb_name = str(kb_name).strip() if kb_name else None
        #: slot name -> the interview's slot plan ({type, regex, examples, …}).
        #: The plan carries the FieldSpec constraints (a 10-digit order number)
        #: that the attached slot needs as an NLX built-in + regex (S1/S5).
        self.slot_plans = {
            str(k): v for k, v in (slot_plans or {}).items() if isinstance(v, dict)}
        #: data request id -> {field -> customer-facing label} from the interview's
        #: OperationSpec (the deployed document's descriptions are ASCII-only).
        self.field_labels = {
            str(k): {str(f): str(l) for f, l in v.items() if isinstance(l, str) and l.strip()}
            for k, v in (field_labels or {}).items() if isinstance(v, dict)}
        # {data request id: {field: [allowed values]}} from the interview's
        # OperationSpec — the reply schema no longer carries enums (they made
        # not-found replies fail), so D5 reads the allowed values from here.
        self.field_enums = {
            str(k): {str(f): [str(x) for x in vals] for f, vals in v.items()
                     if isinstance(vals, (list, tuple)) and vals}
            for k, v in (field_enums or {}).items() if isinstance(v, dict)}

        self.changes: list[str] = []
        #: (scope, rule, message)
        self.violations: list[tuple[str, str, str]] = []
        #: attached slot type before S1/S5 rewrote it — S2 still resolves it
        self.original_slot_types: dict[str, str] = {}

    # -- reporting ---------------------------------------------------------

    def change(self, message: str) -> None:
        self.changes.append(message)

    def violation(self, rule: str, scope: str, message: str) -> None:
        self.violations.append((scope, rule, f"{rule}: {message}"))

    # -- accessors ---------------------------------------------------------

    @property
    def flow_id(self) -> str:
        return str(self.flow.get("flowId") or "<unnamed flow>")

    @property
    def nodes(self) -> dict:
        nodes = self.flow.get("nodes")
        return nodes if isinstance(nodes, dict) else {}

    @property
    def attached(self) -> list[dict]:
        slots = self.flow.get("slotTypes")
        return [s for s in slots if isinstance(s, dict) and s.get("name")] \
            if isinstance(slots, list) else []

    @property
    def slot_names(self) -> list[str]:
        return [str(s["name"]) for s in self.attached]

    def start_id(self) -> Optional[str]:
        for node_id, node in self.nodes.items():
            if isinstance(node, dict) and node.get("type") == "start":
                return node_id
        return None

    def nodes_of_type(self, *types: str) -> list[tuple[str, dict]]:
        return [(nid, n) for nid, n in self.nodes.items()
                if isinstance(n, dict) and n.get("type") in types]

    def message_bodies(self) -> list[str]:
        bodies = []
        for node in self.nodes.values():
            if not isinstance(node, dict):
                continue
            for message in node.get("messages") or []:
                if isinstance(message, dict) and isinstance(message.get("body"), str):
                    bodies.append(message["body"])
        return bodies

    def is_korean(self) -> bool:
        if any(_HANGUL.search(body) for body in self.message_bodies()):
            return True
        language = str(self.flow.get("mainLanguageCode") or "")
        return language.lower().startswith("ko")

    def retry_message(self) -> str:
        return DEFAULT_RETRY_MESSAGE_KO if self.is_korean() else DEFAULT_RETRY_MESSAGE_EN

    def _silent_handoff(self, node: dict) -> bool:
        """True for a node that ends the turn without telling the caller anything:
        the end node, or a redirect to any flow but the escalation."""
        if not isinstance(node, dict):
            return False
        if node.get("type") == "end":
            return True
        if node.get("type") != "redirect":
            return False
        redirect = (node.get("metadata") or {}).get("redirect") or {}
        return redirect.get("flowId") != self.escalation_flow_id

    def escalation_target(self) -> Optional[str]:
        """The node that hands this flow's caller to a human, if it has one."""
        for node_id, node in self.nodes.items():
            if not isinstance(node, dict) or node.get("type") != "redirect":
                continue
            redirect = (node.get("metadata") or {}).get("redirect") or {}
            if redirect.get("flowId") == self.escalation_flow_id:
                return node_id
        for node_id, node in self.nodes.items():
            if isinstance(node, dict) and node.get("type") == "escalate":
                return node_id
        return None

    def choice_slot(self, node: dict) -> Optional[str]:
        choice = (node.get("metadata") or {}).get("choice")
        if isinstance(choice, dict):
            slot = choice.get("slotTypeId")
            if isinstance(slot, str) and slot in self.slot_names:
                return slot
        return None

    # ==================================================================
    # S1 — attached slot vocabulary
    # ==================================================================

    def rule_s1(self) -> None:
        for slot in self.attached:
            name = str(slot["name"])
            declared = slot.get("type")
            if isinstance(declared, str):
                self.original_slot_types[name] = declared.strip()

            if isinstance(declared, str) and declared.strip() in NLX_BUILTIN_SLOT_TYPES:
                continue
            if isinstance(declared, str) and declared.strip().startswith("NLX."):
                self.violation(
                    "S1", "flow",
                    f"attached slot {name!r} type {declared!r} is not an NLX "
                    f"built-in; use one of {sorted(NLX_BUILTIN_SLOT_TYPES)} or a "
                    f"custom slot type id")
                continue

            mapped = _normalize_alias(declared)
            if mapped is not None:
                if mapped != declared:
                    slot["type"] = mapped
                    self.change(f"slot {name!r}: type {declared!r} → {mapped!r} (S1)")
                if mapped == YES_NO_SLOT_TYPE:
                    self._require_yes_no(name)
                else:
                    self._apply_plan_constraints(slot, name)
                continue

            if not isinstance(declared, str) or not declared.strip():
                self.violation("S1", "flow",
                               f"attached slot {name!r} has no slot type")
                continue
            if self.slot_type_ids is not None and declared not in self.slot_type_ids:
                if self._resolve_unknown_custom_type(slot, name, declared):
                    continue
                self.violation(
                    "S1", "cross",
                    f"attached slot {name!r} type {declared!r} is neither an NLX "
                    f"built-in nor a bundled custom slot type id")

        self._rule_s1_yes_no_by_value()

    def _plan_constraints(self, name: str) -> tuple[Any, Any, Any]:
        """(regex, min_length, max_length) the interview recorded for a slot."""
        plan = self.slot_plans.get(name) or {}
        constraints = plan.get("constraints") if isinstance(plan.get("constraints"), dict) else {}
        regex = plan.get("regex") or plan.get("pattern") or constraints.get("regex") \
            or constraints.get("pattern")
        min_length = plan.get("min_length", constraints.get("min_length"))
        max_length = plan.get("max_length", constraints.get("max_length"))
        exact = plan.get("exact_length", constraints.get("exact_length"))
        if exact and not (min_length or max_length):
            min_length = max_length = exact
        return regex, min_length, max_length

    def _apply_plan_constraints(self, slot: dict, name: str) -> None:
        """A text-ish built-in whose plan carries a regex is an open value: the
        live bundle needed ``NLX.AlphaNumeric`` + ``regex`` for a 10-digit order
        number, and a phone-shaped slot the ``NLX.PhoneNumber`` built-in."""
        if slot.get("type") not in ("NLX.Text", "NLX.AlphaNumeric", "NLX.Number") or slot.get("regex"):
            return
        regex, min_length, max_length = self._plan_constraints(name)
        if not (regex or min_length or max_length):
            return
        if slot.get("type") == "NLX.Number" and not regex:
            return  # a quantity with a length bound stays a number
        builtin, new_regex = self._open_value_builtin(
            slot, name, str(slot.get("type")), {}, regex, min_length, max_length)
        previous = slot.get("type")
        if builtin != previous or new_regex:
            slot["type"] = builtin
            if new_regex:
                slot["regex"] = new_regex
            self.change(
                f"slot {name!r}: {previous} → {builtin}"
                + (f" (regex {new_regex})" if new_regex else "")
                + " from the interview's slot constraints (S1)")

    def _resolve_unknown_custom_type(self, slot: dict, name: str, declared: str) -> bool:
        """A slot type id the bundle does not define — typically the open-value
        custom type an earlier generator emitted for an order/phone number. When
        the interview's slot plan knows the value's shape, resolve it to the
        built-in the live contract wants instead of failing the flow."""
        plan = self.slot_plans.get(name)
        if not isinstance(plan, dict):
            return False
        regex, min_length, max_length = self._plan_constraints(name)
        planned = _normalize_alias(plan.get("type"))
        if planned == YES_NO_SLOT_TYPE:
            slot["type"] = YES_NO_SLOT_TYPE
            self._require_yes_no(name)
            self.change(f"slot {name!r}: unknown slot type {declared!r} → "
                        f"{YES_NO_SLOT_TYPE!r} from the interview's slot plan (S1)")
            return True
        if planned in NLX_BUILTIN_SLOT_TYPES and planned not in ("NLX.Text", "NLX.AlphaNumeric") \
                and not (planned == "NLX.Number" and regex):
            slot["type"] = planned
            self.change(f"slot {name!r}: unknown slot type {declared!r} → {planned!r} "
                        f"from the interview's slot plan (S1)")
            return True
        if not planned and not (regex or min_length or max_length or slot.get("examples")):
            return False
        builtin, new_regex = self._open_value_builtin(
            slot, name, declared, {}, regex, min_length, max_length)
        slot["type"] = builtin
        if new_regex:
            slot["regex"] = new_regex
        self.change(
            f"slot {name!r}: unknown slot type {declared!r} → {builtin}"
            + (f" (regex {new_regex})" if new_regex else "")
            + " from the interview's slot plan (S1/S5)")
        return True

    def _require_yes_no(self, slot_name: str) -> None:
        if self.slot_type_ids is not None and YES_NO_SLOT_TYPE not in self.slot_type_ids:
            self.violation(
                "S1", "cross",
                f"attached slot {slot_name!r} needs the {YES_NO_SLOT_TYPE!r} custom "
                f"slot type (there is no boolean built-in) but the bundle does not "
                f"define it")

    def _yes_no_vocabulary(self) -> set[str]:
        document = self.slot_type_docs.get(YES_NO_SLOT_TYPE) or {}
        vocabulary: set[str] = set()
        for value in document.get("values") or []:
            if isinstance(value, dict):
                if isinstance(value.get("value"), str):
                    vocabulary.add(value["value"].strip())
                for synonym in value.get("synonyms") or []:
                    if isinstance(synonym, str):
                        vocabulary.add(synonym.strip())
            elif isinstance(value, str):
                vocabulary.add(value.strip())
        return {word for word in vocabulary if word}

    def _rule_s1_yes_no_by_value(self) -> None:
        """A text slot compared only against yes/no values IS a yesNo slot.

        Extension of S1 (documented, not silent): the live GAON bundle needed
        ``privacyConsent`` moved from ``text`` to ``yesNo``, and the only
        deterministic evidence in the document is that every constant the flow
        compares the slot against is a yesNo value or synonym. Gated on the
        bundle actually defining ``yesNo`` so it can never invent a type.
        """
        vocabulary = self._yes_no_vocabulary()
        if not vocabulary or (
                self.slot_type_ids is not None and YES_NO_SLOT_TYPE not in self.slot_type_ids):
            return
        compared: dict[str, list[Any]] = {}
        for node in self.nodes.values():
            if not isinstance(node, dict):
                continue
            for edge in _edges(node):
                for condition in edge.get("conditions") or []:
                    if not isinstance(condition, dict):
                        continue
                    left = condition.get("left") or {}
                    right = condition.get("right") or {}
                    if left.get("type") == "slot" and right.get("type") == "constant":
                        compared.setdefault(str(left.get("name")), []).append(right.get("value"))
        for slot in self.attached:
            name = str(slot["name"])
            values = compared.get(name) or []
            if not values or slot.get("type") == YES_NO_SLOT_TYPE:
                continue
            if slot.get("type") not in ("NLX.Text", "NLX.Name"):
                continue
            if all(isinstance(v, str) and v.strip() in vocabulary for v in values):
                previous = slot.get("type")
                slot["type"] = YES_NO_SLOT_TYPE
                self.change(
                    f"slot {name!r}: type {previous!r} → {YES_NO_SLOT_TYPE!r} "
                    f"(compared only against yes/no values; S1/S4)")

    # ==================================================================
    # S5 — a one-item custom menu is not a question
    # ==================================================================

    def rule_s5(self) -> None:
        if not self.slot_type_docs:
            return
        for slot in self.attached:
            name = str(slot["name"])
            slot_type = slot.get("type")
            if not isinstance(slot_type, str) or slot_type in NLX_BUILTIN_SLOT_TYPES:
                continue
            document = self.slot_type_docs.get(slot_type)
            if not isinstance(document, dict):
                continue
            values = [v for v in (document.get("values") or [])]
            constraints = (document.get("metadata") or {}).get("constraints") or {}
            regex = constraints.get("regex") if isinstance(constraints, dict) else None
            min_length = constraints.get("min_length") if isinstance(constraints, dict) else None
            max_length = constraints.get("max_length") if isinstance(constraints, dict) else None
            if len(values) > 1 and not (regex or min_length or max_length):
                continue  # a real enumerated menu: leave it alone

            builtin, new_regex = self._open_value_builtin(
                slot, name, slot_type, document, regex, min_length, max_length)
            slot["type"] = builtin
            if new_regex:
                slot["regex"] = new_regex
            elif builtin == "NLX.PhoneNumber":
                slot.pop("regex", None)
            self.change(
                f"slot {name!r}: open-value custom slot type {slot_type!r} → "
                f"{builtin}" + (f" (regex {new_regex})" if new_regex else "") +
                " — a one-item slot type auto-selects without asking (S5)")

    def _open_value_builtin(
        self, slot: dict, name: str, slot_type: str, document: dict,
        regex: Any, min_length: Any, max_length: Any,
    ) -> tuple[str, Optional[str]]:
        haystack = " ".join(str(x).lower() for x in (name, slot_type, regex or ""))
        if "phone" in haystack or "mobile" in haystack or "010" in haystack:
            return "NLX.PhoneNumber", None
        if _is_digits_only_regex(regex):
            return "NLX.AlphaNumeric", str(regex)
        sample = self._sample_value(slot, document)
        if not regex and (min_length or max_length) and sample and _DIGITS.match(sample):
            low = int(min_length) if isinstance(min_length, int) else None
            high = int(max_length) if isinstance(max_length, int) else None
            if low is not None and high is not None and low == high:
                built = f"^[0-9]{{{low}}}$"
            elif low is not None and high is not None:
                built = f"^[0-9]{{{low},{high}}}$"
            elif high is not None:
                built = f"^[0-9]{{1,{high}}}$"
            else:
                built = f"^[0-9]{{{low},}}$"
            return "NLX.AlphaNumeric", built
        return "NLX.Text", (str(regex) if isinstance(regex, str) and regex else None)

    @staticmethod
    def _sample_value(slot: dict, document: dict) -> Optional[str]:
        for value in document.get("values") or []:
            if isinstance(value, dict) and isinstance(value.get("value"), str):
                return value["value"].strip()
            if isinstance(value, str):
                return value.strip()
        for example in slot.get("examples") or []:
            if isinstance(example, str):
                return example.strip()
        return None

    # ==================================================================
    # S2 — metadata.choice.slotTypeId is the attached slot NAME
    # ==================================================================

    # ==================================================================
    # S9 — a date or time shaped slot is a date or time slot
    # ==================================================================

    _DATE_REGEX = re.compile(
        r"^\^?(?:\\d|\[0-9\])\{4\}[-./](?:\\d|\[0-9\])\{2\}[-./](?:\\d|\[0-9\])\{2\}\$?$")
    _TIME_REGEX = re.compile(
        r"^\^?(?:\\d|\[0-9\])\{1,2\}:(?:\\d|\[0-9\])\{2\}\$?$|^\^?(?:\\d|\[0-9\])\{2\}:(?:\\d|\[0-9\])\{2\}\$?$")

    #: The compact form a typed HH:MM reaches the slot in ("10:00" → "1000",
    #: "9:30" → "930"); the Lambda adapter restores the colon.
    COMPACT_TIME_REGEX = "^[0-9]{3,4}$"

    def rule_s9(self) -> None:
        """A built-in text slot whose regex is a date (YYYY-MM-DD) skeleton becomes
        ``NLX.Date`` without the regex; a time (HH:MM) skeleton keeps the text type
        with the compact-digits regex. Built-in values arrive without separators
        (live), so a regex with '-' or ':' never matches: the appointment date
        '2026-09-18' was rejected, the flow's retry edge led to the fallback, and
        the third miss escalated. ``NLX.Date`` recognised the same input and
        delivers the ISO date; ``NLX.Time`` is NOT used because it delivers an
        instant in an assumed timezone ("10:00" → "2026-09-14T14:00:00.000Z",
        live) that no backend can map back to what the caller said."""
        for slot in self.attached:
            slot_type = slot.get("type")
            regex = slot.get("regex")
            if slot_type not in ("NLX.AlphaNumeric", "NLX.Text", "NLX.Time") or not isinstance(regex, str):
                if slot_type == "NLX.Time":
                    slot["type"] = "NLX.AlphaNumeric"
                    slot["regex"] = self.COMPACT_TIME_REGEX
                    self.change(f"slot {slot.get('name')!r}: NLX.Time → NLX.AlphaNumeric with regex "
                                f"{self.COMPACT_TIME_REGEX!r} (NLX.Time delivers a timezone-shifted instant; S9)")
                continue
            stripped = regex.strip()
            if self._DATE_REGEX.match(stripped):
                slot["type"] = "NLX.Date"
                slot.pop("regex", None)
                self.change(
                    f"slot {slot.get('name')!r}: {slot_type} with regex {regex!r} → NLX.Date "
                    f"(built-in values carry no separators, so the regex never matched; S9)")
            elif self._TIME_REGEX.match(stripped) or slot_type == "NLX.Time":
                slot["type"] = "NLX.AlphaNumeric"
                slot["regex"] = self.COMPACT_TIME_REGEX
                self.change(
                    f"slot {slot.get('name')!r}: {slot_type} with regex {regex!r} → NLX.AlphaNumeric with "
                    f"regex {self.COMPACT_TIME_REGEX!r} (the typed HH:MM reaches the slot without the "
                    f"colon; the Lambda adapter restores it; S9)")

    def rule_s2(self) -> None:
        names = self.slot_names
        if not names and not self.nodes_of_type("user_choice"):
            return

        pools: dict[str, list[str]] = {}
        for slot in self.attached:
            name = str(slot["name"])
            for key in {slot.get("type"), self.original_slot_types.get(name)}:
                if isinstance(key, str) and key:
                    pools.setdefault(key, []).append(name)
        used: set[str] = set()

        for node_id, node in self.nodes_of_type("user_choice"):
            meta = _meta(node)
            choice = meta.get("choice")
            if not isinstance(choice, dict):
                choice = {}
                meta["choice"] = choice
            reference = choice.get("slotTypeId")

            if isinstance(reference, str) and reference in names:
                used.add(reference)
                if choice.get("source") != "slotType":
                    choice["source"] = "slotType"
                    self.change(f"{_label(node_id, node)}: choice.source → 'slotType' (S2)")
                continue

            resolved = self._resolve_choice_slot(reference, pools, used)
            if resolved is None:
                self.violation(
                    "S2", "flow",
                    f"{_label(node_id, node)} metadata.choice.slotTypeId "
                    f"{reference!r} names no attached slot and no attached slot has "
                    f"that type — the service stores it verbatim as the slot id, so "
                    f"the slot would never fill")
                continue
            choice["source"] = "slotType"
            choice["slotTypeId"] = resolved
            used.add(resolved)
            self.change(
                f"{_label(node_id, node)}: choice.slotTypeId {reference!r} → "
                f"{resolved!r} (attached slot name; S2)")

    def _resolve_choice_slot(
        self, reference: Any, pools: dict[str, list[str]], used: set[str],
    ) -> Optional[str]:
        candidates: list[str] = []
        for key in (reference, _normalize_alias(reference)):
            if isinstance(key, str) and key in pools:
                candidates = pools[key]
                break
        if not candidates:
            # A single attached slot is unambiguous even when the reference is junk.
            names = self.slot_names
            if len(names) == 1:
                return names[0]
            return None
        for name in candidates:
            if name not in used:
                return name
        return candidates[-1]  # retry-shaped duplicate: same slot, asked again

    # ==================================================================
    # S3 — capture edges
    # ==================================================================

    def rule_s3(self) -> None:
        for node_id, node in self.nodes_of_type("user_choice"):
            slot = self.choice_slot(node)
            if not slot:
                continue
            for edge in _edges(node):
                self._rewrite_capture_edge(node_id, node, edge, slot=slot)
        for node_id, node in self.nodes_of_type("user_input"):
            for edge in _edges(node):
                self._rewrite_capture_edge(node_id, node, edge, slot=None)

    # ==================================================================
    # S8 — a constant compared with a yes/no slot is one of ITS values
    # ==================================================================

    def rule_s8(self) -> None:
        """Live (GAON, 2026-09-13): the model wrote `privacyConsent eq "yes"`
        while the yesNo slot type's values are 예 / 아니요, so the customer's
        "네" matched 예, failed the comparison and was treated as a refusal
        (straight to escalation). Any yes/no-ish constant compared with a slot
        attached as yesNo is rewritten to the slot type's own value."""
        yes_no_slots = {
            str(slot.get("name")) for slot in self.attached
            if slot.get("type") == YES_NO_SLOT_TYPE and slot.get("name")}
        if not yes_no_slots:
            return
        affirmative, negative = self._yes_no_pair()
        if not affirmative or not negative:
            return
        for node_id, node in self.nodes.items():
            if not isinstance(node, dict):
                continue
            for edge in _edges(node):
                for condition in (edge.get("conditions") or []):
                    if not isinstance(condition, dict):
                        continue
                    left, right = condition.get("left") or {}, condition.get("right") or {}
                    if not (isinstance(left, dict) and left.get("type") == "slot"
                            and str(left.get("name")) in yes_no_slots
                            and condition.get("operator") in ("eq", "neq")
                            and isinstance(right, dict) and right.get("type") == "constant"
                            and isinstance(right.get("value"), str)):
                        continue
                    value = right["value"].strip()
                    if value in (affirmative, negative):
                        continue
                    key = value.lower()
                    target = (affirmative if key in _AFFIRMATIVE_CONSTANTS
                              else negative if key in _NEGATIVE_CONSTANTS else None)
                    if target is None:
                        self.violation(
                            "S8", "flow",
                            f"{_label(node_id, node)} edge {edge.get('name')!r} compares yes/no slot "
                            f"{left.get('name')!r} with {value!r}, which is not one of the yesNo slot "
                            f"type's values ({affirmative!r} / {negative!r})")
                        continue
                    right["value"] = target
                    self.change(
                        f"{_label(node_id, node)} edge {edge.get('name')!r}: yes/no constant "
                        f"{value!r} → {target!r} (the yesNo slot type's value) (S8)")

    def _yes_no_pair(self) -> tuple[Optional[str], Optional[str]]:
        document = self.slot_type_docs.get(YES_NO_SLOT_TYPE) or {}
        values = [v.get("value").strip() if isinstance(v, dict) and isinstance(v.get("value"), str)
                  else (v.strip() if isinstance(v, str) else None)
                  for v in (document.get("values") or [])]
        values = [v for v in values if v]
        if len(values) >= 2:
            return values[0], values[1]
        return None, None

    def _rewrite_capture_edge(
        self, node_id: str, node: dict, edge: dict, *, slot: Optional[str],
    ) -> None:
        """user_choice edges test the slot; user_input edges test captured_flow."""
        name = _edge_name(edge)
        conditions = edge.get("conditions")
        conditions = conditions if isinstance(conditions, list) else []

        def operator_from_name() -> Optional[str]:
            if name in MATCH_EDGE_NAMES:
                return "exists"
            if name in NO_MATCH_EDGE_NAMES:
                return "not_exists"
            return None

        if not conditions:
            operator = operator_from_name()
            if operator is None:
                return
            edge["conditions"] = [
                _slot_condition(slot, operator) if slot
                else {"left": {"type": "captured_flow"}, "operator": operator}]
            self.change(
                f"{_label(node_id, node)} edge {edge.get('name')!r}: added "
                f"{'slot ' + slot if slot else 'captured_flow'} {operator} (S3)")
            return

        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            left = condition.get("left") or {}
            operator = condition.get("operator")
            if operator not in ("exists", "not_exists"):
                continue
            if slot:
                if left.get("type") == "captured_flow":
                    condition["left"] = {"type": "slot", "name": slot}
                    condition.pop("right", None)
                    self.change(
                        f"{_label(node_id, node)} edge {edge.get('name')!r}: "
                        f"captured_flow {operator} → slot {slot!r} {operator} (S3)")
                elif left.get("type") == "slot" and left.get("name") != slot:
                    previous = left.get("name")
                    condition["left"] = {"type": "slot", "name": slot}
                    self.change(
                        f"{_label(node_id, node)} edge {edge.get('name')!r}: "
                        f"slot {previous!r} → {slot!r} (S3)")
            elif left.get("type") == "slot":
                condition["left"] = {"type": "captured_flow"}
                self.change(
                    f"{_label(node_id, node)} edge {edge.get('name')!r}: slot "
                    f"{left.get('name')!r} {operator} → captured_flow {operator} "
                    f"(user_input captures an intent, not a value; S3)")

    # ==================================================================
    # M2 — generative_text sends nothing
    # ==================================================================

    # ==================================================================
    # M3 — a message node is only a message
    # ==================================================================

    def rule_m3(self) -> None:
        """Live (GAON, 2026-09-13): the price announcement was a ``basic`` node
        that also carried ``metadata.redirect`` (the next node was the real
        redirect) and cleared the very slots its own message rendered. The
        runtime never showed the message — the contact fell into the fallback
        re-guide instead — while every plain ``basic`` announcement worked.
        Strip the stray redirect (the node's edge already leads to a redirect
        node) and drop a ``clear`` of a slot the node's own message references
        (the following redirect node clears it anyway)."""
        for node_id, node in self.nodes_of_type("basic"):
            metadata = node.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if "redirect" in metadata:
                target = (metadata.get("redirect") or {}).get("flowId") if isinstance(metadata.get("redirect"), dict) else None
                leads_to_redirect = any(
                    (self.nodes.get(edge.get("nodeId")) or {}).get("type") == "redirect"
                    for edge in _edges(node))
                if leads_to_redirect or not target:
                    metadata.pop("redirect")
                    self.change(f"{_label(node_id, node)}: removed metadata.redirect from a message node "
                                f"(redirecting is the next node's job) (M3)")
                else:
                    # No redirect node follows: turn the stray metadata into one.
                    redirect_id = f"{node_id}-redirect"
                    self.nodes[redirect_id] = {
                        "nodeId": redirect_id, "type": "redirect",
                        "metadata": {"redirect": metadata.pop("redirect")},
                        "childNodes": list(_edges(node)),
                    }
                    node["childNodes"] = [{"nodeId": redirect_id, "name": "next"}]
                    self.change(f"{_label(node_id, node)}: moved metadata.redirect into its own redirect node (M3)")
            referenced = set()
            for message in node.get("messages") or []:
                body = message.get("body") if isinstance(message, dict) else None
                if isinstance(body, str):
                    referenced.update(re.findall(r"\{([A-Za-z_][\w-]*):NLX\.Slot\}", body))
            mods = metadata.get("stateModifications")
            if referenced and isinstance(mods, list):
                kept = [m for m in mods if not (isinstance(m, dict) and m.get("type") == "slot"
                                                and m.get("modification") == "clear"
                                                and str(m.get("name")) in referenced)]
                if len(kept) != len(mods):
                    dropped = sorted({str(m.get('name')) for m in mods if m not in kept})
                    if kept:
                        metadata["stateModifications"] = kept
                    else:
                        metadata.pop("stateModifications")
                    self.change(f"{_label(node_id, node)}: dropped clear of slot(s) {dropped} that its own "
                                f"message renders (M3)")
            if not metadata:
                node.pop("metadata", None)

    def rule_m2(self) -> None:
        for node_id, node in self.nodes_of_type("generative_text"):
            if not self._is_last_customer_facing(node_id):
                continue
            prompt = ((node.get("metadata") or {}).get("generativeText") or {}).get("prompt")
            if self._result_already_announced(node_id):
                # Live (GAON): the flow's own basic node had already said
                # "예약이 접수되었습니다. 예약번호는 …" and a trailing generative
                # node then produced a second "조회 결과: …" built from raw field
                # descriptions. A node that would only repeat the answer is
                # made a silent pass-through instead.
                node["type"] = "basic"
                node.pop("messages", None)
                meta = node.get("metadata")
                if isinstance(meta, dict):
                    meta.pop("generativeText", None)
                    if not meta:
                        node.pop("metadata", None)
                self.change(
                    f"{_label(node_id, node)}: generative_text after the result was already "
                    f"announced → silent pass-through (M2)")
                continue
            body = self._template_from_prompt(prompt if isinstance(prompt, str) else "")
            if not body:
                # Live (GAON v3): "성공 시 배송상태와 예정일을 자연스럽게 안내한다" has no
                # placeholders and the node said nothing — the caller heard
                # "anything else?" right after giving the order number. Announce
                # the data request's own result fields instead.
                body = self._template_from_result(node_id)
            if not body:
                self.violation(
                    "M2", "normalizer",
                    f"{_label(node_id, node)} is the last customer-facing step but "
                    f"generative_text sends no message, and neither its prompt nor a "
                    f"preceding data request gives fields to build a sentence from")
                continue
            # Live (2026-09-17): in a workspace without a default generative model
            # the node logs Error IntegrationNotFound, sets node_status failure
            # and says nothing; a `failure` edge to a templated basic DID speak.
            # With a model the node speaks the LLM sentence. So the confirmed
            # generative_text stays — the user approved generative wording —
            # and the templated sentence rides along as its measured fallback.
            self._attach_generative_fallback(node_id, node, body)

    def _attach_generative_fallback(self, node_id: str, node: dict, body: str) -> None:
        edges = _edges(node)
        if any(_has_status(e, "failure") for e in edges):
            return
        continuation = [dict(e) for e in edges if not (_has_status(e, "timeout") or _has_status(e, "failure"))]
        fallback_id = _derived_id("4f1a00a2", f"{self.flow_id}#{node_id}#m2fallback")
        self.nodes[fallback_id] = {
            "nodeId": fallback_id, "type": "basic",
            "messages": [{"type": "text", "body": body}],
            **({"childNodes": continuation} if continuation else {}),
        }
        node.setdefault("childNodes", []).insert(
            0, {"nodeId": fallback_id, "name": "modelUnavailable", "conditions": [_status_condition("failure")]})
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        node["metadata"] = meta
        cfg = meta.get("generativeText") if isinstance(meta.get("generativeText"), dict) else {}
        meta["generativeText"] = cfg
        prompt = str(cfg.get("prompt") or "").strip()
        if not _PLACEHOLDER.search(prompt):
            # the model has to be told which values it may say; the template is
            # exactly that list, in the flow's language
            cfg["prompt"] = (prompt + ("\n" if prompt else "")
                             + ("다음 값만 사용해 한두 문장으로 자연스럽게 안내하세요 (값을 바꾸거나 새 사실을 덧붙이지 마세요): "
                                if self.is_korean else
                                "Use only these values, in one or two natural sentences (do not alter them or add facts): ")
                             + body)
        self.change(
            f"{_label(node_id, node)}: kept generative_text; failure (no workspace model) → "
            f"templated basic [{fallback_id[:8]}] (M2)")

    def _result_already_announced(self, node_id: str) -> bool:
        """True when, between the nearest upstream data request and this node, a
        message node already speaks (the answer has been given)."""
        parents: dict[str, list[str]] = {}
        for pid, pnode in self.nodes.items():
            for edge in _edges(pnode):
                target = edge.get("nodeId")
                if isinstance(target, str):
                    parents.setdefault(target, []).append(pid)
        seen = {node_id}
        frontier = list(parents.get(node_id, []))
        found_request = False
        spoke = False
        while frontier:
            pid = frontier.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            pnode = self.nodes.get(pid) or {}
            if pnode.get("type") == "data_request":
                found_request = True
                continue
            if pnode.get("type") in ("start", "user_input", "user_choice"):
                continue
            if any(isinstance(m, dict) and str(m.get("body") or "").strip()
                   for m in pnode.get("messages") or []):
                spoke = True
            frontier.extend(parents.get(pid, []))
        return found_request and spoke

    def _is_last_customer_facing(self, node_id: str) -> bool:
        """True when nothing between here and leaving the flow says anything."""
        nodes = self.nodes
        seen = {node_id}
        frontier = [edge.get("nodeId") for edge in _edges(nodes[node_id])]
        while frontier:
            target = frontier.pop()
            if not isinstance(target, str) or target in seen or target not in nodes:
                continue
            seen.add(target)
            node = nodes[target]
            has_message = any(
                isinstance(m, dict) and str(m.get("body") or "").strip()
                for m in node.get("messages") or [])
            if has_message:
                return False
            if node.get("type") in ("end", "escalate", "redirect"):
                continue
            frontier.extend(edge.get("nodeId") for edge in _edges(node))
        return True

    def _template_from_prompt(self, prompt: str) -> Optional[str]:
        placeholders: list[tuple[str, str]] = []
        seen: set[str] = set()
        for match in _PLACEHOLDER.finditer(prompt):
            token = match.group(0)
            if match.group(2) not in ("Slot", "Variable") or token in seen:
                continue
            seen.add(token)
            placeholders.append((self._prompt_label(prompt, match.start()), token))
        if not placeholders:
            return None
        parts = [f"{label} {token}".strip() if label else token
                 for label, token in placeholders]
        if self.is_korean():
            return "조회 결과: " + ", ".join(parts) + "입니다."
        return "Here is what I found: " + ", ".join(parts) + "."

    _ENVELOPE_FIELDS = frozenset({"success", "errorCode", "errorcode", "message"})

    def _template_from_result(self, node_id: str) -> Optional[str]:
        """A deterministic announcement of the fields the nearest upstream data
        request returns (envelope fields excluded), labelled from the interview's
        field descriptions when known, else the field name."""
        request_id = self._upstream_data_request(node_id)
        if not request_id:
            return None
        document = self.data_requests.get(request_id)
        properties = ((document or {}).get("responseSchema") or {}).get("properties") or {}
        if not isinstance(properties, dict):
            return None
        labels = self.field_labels.get(request_id, {})
        korean = self.is_korean()
        parts: list[str] = []
        for field, schema in properties.items():
            if field in self._ENVELOPE_FIELDS or not isinstance(schema, dict):
                continue
            if schema.get("type") in ("object", "array"):
                continue
            label = self._result_label(field, labels.get(field), korean)
            token = f"{{{request_id}.{field}:NLX.Variable}}"
            parts.append(f"{label} {token}" if label else token)
            if len(parts) >= 6:
                break
        if not parts:
            return None
        if self.is_korean():
            return "조회 결과: " + ", ".join(parts) + "입니다."
        return "Here is what I found: " + ", ".join(parts) + "."

    #: Korean labels for result fields the interview described in English (live:
    #: "Air conditioner product type {…}" read aloud to a Korean caller).
    _KO_FIELD_WORDS = {
        "price": "가격", "unitprice": "단가", "amount": "금액", "totalamount": "총 금액", "fee": "요금",
        "status": "상태", "deliverystatus": "배송 상태", "date": "날짜", "deliverydate": "배송일",
        "expecteddeliverydate": "예상 배송일", "reservationdate": "예약일", "appointmentdate": "예약 날짜",
        "time": "시간", "timeslot": "예약 시간", "name": "이름", "customername": "고객명",
        "patientname": "환자명", "phone": "전화번호", "phonenumber": "전화번호", "address": "주소",
        "ordernumber": "주문번호", "orderid": "주문번호", "reservationid": "예약번호",
        "appointmentid": "예약번호", "returnid": "반품번호", "trackingnumber": "운송장번호",
        "carrier": "택배사", "department": "진료과", "quantity": "수량", "producttype": "제품 유형",
        "servicetype": "서비스 유형", "planname": "요금제", "balance": "잔액", "points": "포인트",
        "claimnumber": "청구번호", "policynumber": "증권번호", "message": "안내",
        "orderdate": "주문일", "pickupdate": "수거 예정일", "refundamount": "환불 금액",
        "returnstatus": "반품 상태", "appointmenttime": "예약 시간", "doctorname": "담당 의사",
        "items": "주문 상품", "shippingfee": "배송비", "totalprice": "총 금액", "duedate": "예정일",
    }

    def _result_label(self, field: str, described: Optional[str], korean: bool) -> str:
        """The label spoken before a result value: the interview's description when
        it is in the caller's language, a common Korean word for the field, or
        nothing (the value alone) rather than an English fragment."""
        short = self._short_label(described)
        if not korean:
            return short or field
        if short and _HANGUL.search(short):
            return short
        return self._KO_FIELD_WORDS.get(re.sub(r"[^a-z]", "", field.lower()), "")

    @staticmethod
    def _short_label(described: Optional[str]) -> str:
        """A spoken label from an interview description: the text before the
        first parenthesis, sentence break or comma, at most 20 characters —
        never the whole explanation (live: "총 금액 (unitPrice × quantity)",
        "예약 상태. PoC에서는 PENDING으로 접수 후 확정 연락" read out to the caller)."""
        if not described:
            return ""
        head = re.split(r"[(\[（.。,，:：;/]", str(described), maxsplit=1)[0].strip(" -–—·")
        if len(head) > 20:
            return ""
        return head

    def _upstream_data_request(self, node_id: str) -> Optional[str]:
        """The data request id of the nearest data_request node that leads here
        (through choices and message nodes), or None."""
        parents: dict[str, list[str]] = {}
        for pid, pnode in self.nodes.items():
            for edge in _edges(pnode):
                target = edge.get("nodeId")
                if isinstance(target, str):
                    parents.setdefault(target, []).append(pid)
        seen = {node_id}
        frontier = list(parents.get(node_id, []))
        while frontier:
            pid = frontier.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            pnode = self.nodes.get(pid) or {}
            if pnode.get("type") == "data_request":
                requests = pnode.get("dataRequests") or []
                for entry in requests:
                    rid = entry.get("dataRequestId") if isinstance(entry, dict) else entry
                    if isinstance(rid, str) and rid:
                        return rid
                return None
            if pnode.get("type") in ("start", "user_input", "user_choice"):
                continue
            frontier.extend(parents.get(pid, []))
        return None

    @staticmethod
    def _prompt_label(prompt: str, position: int) -> str:
        """The one or two words the prompt itself puts in front of a placeholder."""
        head = prompt[:position]
        # Earlier placeholders contain ':' and '.', which the sentence split below
        # would cut through — live (GreenCart) that left a "Variable}" fragment in
        # the label ("…, Variable} 택배사 …"). Blank them out first.
        head = _PLACEHOLDER.sub(" ", head)
        head = re.split(r"[.!?:;\n]", head)[-1]
        tokens = [t for t in re.split(r"\s+", head.strip()) if t]
        tokens = [t.strip("'\"(),") for t in tokens[-2:] if t.strip("'\"(),")]
        cleaned: list[str] = []
        for index, token in enumerate(tokens):
            token = _KO_PARTICLE.sub("", token) if index == len(tokens) - 1 else token
            # A dative/locative token ("고객에게") introduces the sentence, not the value.
            if index < len(tokens) - 1 and re.search(r"(?:에게|에서|한테|께)$", token):
                continue
            if token:
                cleaned.append(token)
        return " ".join(cleaned)

    # ==================================================================
    # R7 — escalate is terminal
    # ==================================================================

    def rule_r7(self) -> None:
        for node_id, node in self.nodes_of_type("escalate"):
            if node.get("childNodes"):
                node.pop("childNodes", None)
                self.change(
                    f"{_label(node_id, node)}: dropped childNodes — escalate is "
                    f"terminal; a child made Connect report Success instead of "
                    f"Escalation (R7)")

    # ==================================================================
    # R6 — retry = recovery basic that CLEARS the slot, then re-asks
    # ==================================================================

    def rule_r6(self) -> None:
        nodes = self.nodes
        for node_id, node in self.nodes_of_type("user_choice"):
            slot = self.choice_slot(node)
            if not slot:
                continue
            edge = next((e for e in _edges(node)
                         if _edge_name(e) in NO_MATCH_EDGE_NAMES), None)
            if edge is None:
                continue
            target_id = edge.get("nodeId")
            target = nodes.get(target_id) if isinstance(target_id, str) else None

            if target_id == node_id:
                recovery_id = self._recovery_basic(node_id, slot, self.retry_message())
                edge["nodeId"] = recovery_id
                self.change(
                    f"{_label(node_id, node)} notCaptured looped onto itself → "
                    f"recovery basic [{recovery_id[:8]}] clearing slot {slot!r} "
                    f"then re-asking (R6)")
                continue

            if not isinstance(target, dict):
                continue

            loops_back = any(e.get("nodeId") == node_id for e in _edges(target))
            if target.get("type") == "basic" and loops_back:
                self._make_recovery(target_id, target, node_id, slot)
                continue

            if target.get("type") == "user_choice" and self.choice_slot(target) == slot:
                # A second capture node for the same slot is same-turn
                # slot_no_match: it never re-asks. Fold it into the pattern.
                text = self._first_body(target) or self.retry_message()
                self.nodes[target_id] = {
                    "nodeId": target_id, "type": "basic",
                    "messages": [{"type": "text", "body": text}],
                    "metadata": {"stateModifications": [_clear_modification(slot)]},
                    "childNodes": [{"nodeId": node_id, "name": "retry"}],
                }
                self.change(
                    f"user_choice[{str(target_id)[:8]}]: second capture node for slot "
                    f"{slot!r} → recovery basic clearing the slot and re-asking "
                    f"{_label(node_id, node)} (a same-turn revisit never re-asks; R6)")

    def _make_recovery(self, node_id: str, node: dict, ask_id: str, slot: str) -> None:
        meta = _meta(node)
        modifications = meta.get("stateModifications")
        if not isinstance(modifications, list):
            modifications = []
            meta["stateModifications"] = modifications
        clear = _clear_modification(slot)
        if clear not in modifications:
            modifications.append(clear)
            self.change(
                f"{_label(node_id, node)}: recovery basic clears slot {slot!r} "
                f"before re-asking (an uncleared revisit drops to Fallback; R6)")
        if not any(isinstance(m, dict) and str(m.get("body") or "").strip()
                   for m in node.get("messages") or []):
            node["messages"] = [{"type": "text", "body": self.retry_message()}]
            self.change(f"{_label(node_id, node)}: added recovery wording (R6)")
        if not any(edge.get("nodeId") == ask_id for edge in _edges(node)):
            node.setdefault("childNodes", []).append({"nodeId": ask_id, "name": "retry"})
            self.change(f"{_label(node_id, node)}: loops back to [{ask_id[:8]}] (R6)")

    def _recovery_basic(self, ask_id: str, slot: str, text: str) -> str:
        node_id = _derived_id("4e7a5000", f"{self.flow_id}#retry#{ask_id}#{slot}")
        existing = self.nodes.get(node_id)
        if isinstance(existing, dict):
            self._make_recovery(node_id, existing, ask_id, slot)
            return node_id
        self.nodes[node_id] = {
            "nodeId": node_id, "type": "basic",
            "messages": [{"type": "text", "body": text}],
            "metadata": {"stateModifications": [_clear_modification(slot)]},
            "childNodes": [{"nodeId": ask_id, "name": "retry"}],
        }
        return node_id

    @staticmethod
    def _first_body(node: dict) -> Optional[str]:
        for message in node.get("messages") or []:
            if isinstance(message, dict) and str(message.get("body") or "").strip():
                return message["body"]
        return None

    # ==================================================================
    # R8 — a "not captured" path must not skip a value a request needs
    # ==================================================================

    def _required_payload_slots(self, node: dict) -> set[str]:
        """Slot names a data request node sends in a payload field the request's
        schema lists as ``required``. A field the schema leaves optional (a return
        number OR an order number) may legitimately be absent on some path."""
        out: set[str] = set()
        entries = node.get("dataRequests") if isinstance(node.get("dataRequests"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            document = self.data_requests.get(str(entry.get("dataRequestId") or ""))
            schema = (document or {}).get("requestSchema") if isinstance(document, dict) else None
            required = schema.get("required") if isinstance(schema, dict) else None
            if not isinstance(required, list):
                continue
            required_fields = {str(r) for r in required}
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            for field, value in payload.items():
                match = _PLACEHOLDER_SLOT.fullmatch(str(value or ""))
                if match and str(field) in required_fields:
                    out.add(match.group(1))
            # a payload the generator left out is mapped by D3 field-name → slot-name
            for field in required_fields - {str(f) for f in payload}:
                if field in self.slot_names:
                    out.add(field)
        return out

    def _request_needing_slot_from(self, start_id: str, slot: str) -> Optional[str]:
        """The first data request reachable from ``start_id`` that requires
        ``slot`` on a path that never captures it again."""
        seen: set[str] = set()
        stack = [start_id]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            node = self.nodes[current]
            if node.get("type") == "data_request" and slot in self._required_payload_slots(node):
                return current
            for edge in _edges(node):
                if _captures_slot(edge, slot):
                    continue  # re-captured from here on
                target = edge.get("nodeId")
                if isinstance(target, str):
                    stack.append(target)
        return None

    def rule_r8(self) -> None:
        """Generated (2026-09-15): the order-number node's 'not captured' edge led
        to the *next* question, so an unrecognised answer skipped the order number
        and the intake request was later sent without it (P1 then trimmed the
        payload — a request the backend cannot serve). A capture whose value a
        later request needs is re-asked when it is missed, like R6; a path that
        genuinely does without the value (a return number OR an order number)
        is left alone."""
        if self.role != "operation":
            return
        for node_id, node in list(self.nodes_of_type("user_choice")):
            slot = self.choice_slot(node)
            if not slot:
                continue
            for edge in _edges(node):
                if _captures_slot(edge, slot):
                    continue
                conds = [c for c in (edge.get("conditions") or []) if isinstance(c, dict)]
                not_captured = any((c.get("left") or {}).get("type") == "slot"
                                   and (c.get("left") or {}).get("name") == slot
                                   and c.get("operator") == "not_exists" for c in conds)
                if conds and not not_captured:
                    continue
                target_id = edge.get("nodeId")
                if not isinstance(target_id, str) or target_id == node_id or target_id not in self.nodes:
                    continue
                target = self.nodes[target_id]
                if any(e.get("nodeId") == node_id for e in _edges(target)):
                    continue  # already a recovery loop (R6)
                request_id = self._request_needing_slot_from(target_id, slot)
                if request_id is None:
                    continue
                recovery_id = self._recovery_basic(node_id, slot, self.retry_message())
                edge["nodeId"] = recovery_id
                self.change(
                    f"{_label(node_id, node)} notCaptured went on to [{target_id[:8]}] although "
                    f"request [{request_id[:8]}] still needs {slot!r} → recovery basic "
                    f"[{recovery_id[:8]}] clearing the slot and re-asking (R8)")

    # ==================================================================
    # F1 — a captured value of the wrong shape is re-asked, not sent
    # ==================================================================

    def _attached_regexes(self) -> dict:
        """slot name -> regex, for attached built-in slots that carry one."""
        out: dict = {}
        for slot in self.attached:
            regex = slot.get("regex")
            if str(slot.get("type") or "").startswith("NLX.") and isinstance(regex, str) and regex.strip():
                out[str(slot["name"])] = regex
        return out

    def _no_match_target(self, node_id: str, node: dict, slot: str) -> Optional[str]:
        """Where the node's own 'not captured' edge goes — unless that is a
        recovery loop back to the same node, which would ask a fourth time."""
        for edge in _edges(node):
            if _captures_slot(edge, slot):
                continue
            conds = [c for c in (edge.get("conditions") or []) if isinstance(c, dict)]
            not_captured = any((c.get("left") or {}).get("type") == "slot"
                               and (c.get("left") or {}).get("name") == slot
                               and c.get("operator") == "not_exists" for c in conds)
            if conds and not not_captured:
                continue  # some other guarded edge (e.g. the E1 agent escape)
            target_id = edge.get("nodeId")
            if not isinstance(target_id, str) or target_id == node_id:
                return None
            target = self.nodes.get(target_id)
            if isinstance(target, dict) and any(e.get("nodeId") == node_id for e in _edges(target)):
                return None  # the R6 recovery loop
            return target_id
        return None

    def _fallback_flow_id(self) -> str:
        for flow_id in sorted(self.flow_ids or []):
            if str(flow_id).lower().startswith("fallback"):
                return str(flow_id)
        return "Fallback"

    def _fallback_redirect(self) -> str:
        """A redirect to the application's fallback flow (created once per flow)."""
        node_id = _derived_id("4f1a00ff", f"{self.flow_id}#formatGiveUp")
        if node_id not in self.nodes:
            end_id = next((nid for nid, _ in self.nodes_of_type("end")), None)
            self.nodes[node_id] = {
                "nodeId": node_id, "type": "redirect",
                "metadata": {"redirect": {"type": "flow", "flowId": self._fallback_flow_id()}},
                **({"childNodes": [{"nodeId": end_id, "name": "next"}]} if end_id else {}),
            }
        return node_id

    def _declare_context(self, name: str, var_type: str) -> None:
        declared = self.flow.get("contextVariables")
        if not isinstance(declared, list):
            declared = []
            self.flow["contextVariables"] = declared
        if not any(isinstance(v, dict) and v.get("name") == name for v in declared):
            declared.append({"name": name, "type": var_type})

    def rule_f1(self) -> None:
        """Live (2026-09-15): the regex attached to a built-in slot does NOT gate
        capture — an order number typed at the return-number prompt was stored in
        ``returnId`` and the lookup escalated the caller. Every capture of a
        pattern-bearing slot is followed by a ``matches_regex`` check: a value of
        the wrong shape clears the slot, tells the caller and re-asks; after
        MAX_FORMAT_RETRIES wrong answers the node's own 'not captured' path (or
        the fallback flow) takes over."""
        if self.role != "operation":
            return
        regexes = self._attached_regexes()
        if not regexes:
            return
        from tools.acxd_system_flows import format_retry_message
        language = "ko" if self.is_korean() else str(self.flow.get("mainLanguageCode") or "en")[:2]
        for node_id, node in list(self.nodes_of_type("user_choice")):
            slot = self.choice_slot(node)
            if not slot or slot not in regexes:
                continue
            pattern = runtime_regex(regexes[slot])
            if not pattern:
                continue
            capture_edge = next((e for e in _edges(node) if _captures_slot(e, slot)), None)
            if capture_edge is None:
                continue
            seed = f"{self.flow_id}#fmt#{node_id}#{slot}"
            guard_id = _derived_id("4f1a0000", seed)
            if capture_edge.get("nodeId") == guard_id or guard_id in self.nodes:
                continue  # already guarded (idempotent re-run)
            ok_id = _derived_id("4f1a0001", seed)
            retry_id = _derived_id("4f1a0002", seed)
            check_id = _derived_id("4f1a0003", seed)
            target_id = capture_edge.get("nodeId")
            give_up = self._no_match_target(node_id, node, slot) or self._fallback_redirect()
            # When the flow already has another question to fall back to (return
            # number → order number) one wrong-shaped answer is enough to move on;
            # only a node with no alternative re-asks before giving up.
            give_up_node = self.nodes.get(give_up) or {}
            threshold = 1 if give_up_node.get("type") == "user_choice" else MAX_FORMAT_RETRIES
            self.nodes[guard_id] = {
                "nodeId": guard_id, "type": "choice",
                "childNodes": [
                    {"nodeId": ok_id, "name": "formatValid", "conditions": [{
                        "left": {"type": "slot", "name": slot}, "operator": "matches_regex",
                        "right": {"type": "constant", "value": pattern}}]},
                    {"nodeId": retry_id, "name": "formatInvalid"},
                ]}
            self.nodes[ok_id] = {
                "nodeId": ok_id, "type": "basic",
                "metadata": {"stateModifications": [{
                    "type": "context", "name": FORMAT_RETRIES_VAR, "modification": "set",
                    "value": {"type": "constant", "value": 0}}]},
                "childNodes": [{"nodeId": target_id, "name": "next"}]}
            self.nodes[retry_id] = {
                "nodeId": retry_id, "type": "basic",
                "messages": [{"type": "text", "body": format_retry_message(language)}],
                "metadata": {"stateModifications": [
                    _clear_modification(slot),
                    {"type": "context", "name": FORMAT_RETRIES_VAR, "modification": "increment"}]},
                "childNodes": [{"nodeId": check_id, "name": "next"}]}
            self.nodes[check_id] = {
                "nodeId": check_id, "type": "choice",
                "childNodes": [
                    {"nodeId": give_up, "name": "formatGiveUp", "conditions": [{
                        "left": {"type": "context", "name": FORMAT_RETRIES_VAR}, "operator": "gte",
                        "right": {"type": "constant", "value": threshold}}]},
                    {"nodeId": node_id, "name": "askAgain"},
                ]}
            capture_edge["nodeId"] = guard_id
            self._declare_context(FORMAT_RETRIES_VAR, "number")
            self.change(
                f"{_label(node_id, node)}: captured {slot!r} is checked against {pattern} before use; "
                f"a wrong shape clears the slot and re-asks, {threshold} miss(es) → "
                f"[{str(give_up)[:8]}] (F1)")

    # ==================================================================
    # E2 — "connect me to a human" said while a value is being collected
    # ==================================================================

    def _agent_request_words(self) -> list[str]:
        code = str(self.flow.get("mainLanguageCode") or "").lower()
        if code.startswith("ja"):
            return list(AGENT_REQUEST_WORDS["ja"])
        if code.startswith("en"):
            return list(AGENT_REQUEST_WORDS["en"])
        return list(AGENT_REQUEST_WORDS["ko"])

    def _agent_request_redirect(self) -> str:
        """A redirect to the routable agent-request system flow (created once per
        flow); falls back to the escalation flow when the bundle has none."""
        node_id = _derived_id("4e2a0001", f"{self.flow_id}#agentRequest")
        if node_id not in self.nodes:
            target = next((fid for fid in sorted(self.flow_ids or [])
                           if str(fid).lower().startswith("requestagent")), None) or self.escalation_flow_id
            end_id = next((nid for nid, _ in self.nodes_of_type("end")), None)
            self.nodes[node_id] = {
                "nodeId": node_id, "type": "redirect",
                "metadata": {"redirect": {"type": "flow", "flowId": target}},
                **({"childNodes": [{"nodeId": end_id, "name": "next"}]} if end_id else {}),
            }
        return node_id

    def rule_e2(self) -> None:
        """Live (2026-09-15/16): '상담원 연결해 주세요' at a capture prompt was answered
        with 'I could not catch the order number' — a ``user_choice`` node only
        captures, the documented flow routing from a User choice node did not
        fire, ``associatedSlotTypeIds`` is discarded by the service, and a
        ``user_input`` listen in front of the capture does not fill the slot (so
        it would cost every caller a second question). What does work, verified
        live, is testing the raw utterance: the 'not captured' edge of every
        capture node first passes a choice whose edges check
        ``{System.utterance}`` (operand ``{"type": "system", "name":
        "System.utterance"}``) for an agent-request word and redirect to the
        agent-request flow; anything else continues to the node's own recovery."""
        if self.role != "operation":
            return
        words = self._agent_request_words()
        if not words:
            return
        for node_id, node in list(self.nodes_of_type("user_choice")):
            slot = self.choice_slot(node)
            if not slot:
                continue
            for edge in _edges(node):
                conds = [c for c in (edge.get("conditions") or []) if isinstance(c, dict)]
                not_captured = any((c.get("left") or {}).get("type") == "slot"
                                   and (c.get("left") or {}).get("name") == slot
                                   and c.get("operator") == "not_exists" for c in conds)
                if not not_captured:
                    continue
                target_id = edge.get("nodeId")
                if not isinstance(target_id, str) or target_id not in self.nodes:
                    continue
                if self.nodes[target_id].get("type") == "choice" and any(
                        str(e.get("name") or "").startswith("agentRequest:") for e in _edges(self.nodes[target_id])):
                    continue  # already gated
                gate_id = _derived_id("4e2a0000", f"{self.flow_id}#agentGate#{node_id}#{slot}")
                redirect_id = self._agent_request_redirect()
                self.nodes[gate_id] = {
                    "nodeId": gate_id, "type": "choice",
                    "childNodes": [
                        *[{"nodeId": redirect_id, "name": f"agentRequest:{word}",
                           "conditions": [{"left": {"type": "system", "name": "System.utterance"},
                                           "operator": "contains",
                                           "right": {"type": "constant", "value": word}}]} for word in words],
                        {"nodeId": target_id, "name": "notAgentRequest"},
                    ],
                }
                edge["nodeId"] = gate_id
                self.change(
                    f"{_label(node_id, node)}: an answer that is not a {slot!r} is first checked for an "
                    f"agent request ({len(words)} words in the utterance) → [{redirect_id[:8]}]; "
                    f"otherwise the node's own recovery [{target_id[:8]}] (E2)")

    # ==================================================================
    # R3 — operation flows hand back, they never end the session
    # ==================================================================

    def rule_r3(self) -> None:
        if self.role != "operation":
            return
        nodes = self.nodes
        end_ids = {nid for nid, n in self.nodes_of_type("end")}
        if not end_ids:
            return
        redirect_id: Optional[str] = None
        for node_id, node in self.nodes_of_type("redirect"):
            redirect = (node.get("metadata") or {}).get("redirect") or {}
            if redirect.get("flowId") == self.follow_up_flow_id:
                redirect_id = node_id
                break

        rewired: list[str] = []
        for node_id, node in list(nodes.items()):
            if not isinstance(node, dict) or node.get("type") in (
                    "start", "redirect", "escalate", "end"):
                continue
            for edge in _edges(node):
                if edge.get("nodeId") not in end_ids:
                    continue
                if redirect_id is None:
                    redirect_id = self._follow_up_redirect(edge["nodeId"])
                edge["nodeId"] = redirect_id
                rewired.append(_label(node_id, node))
        if rewired:
            self.change(
                f"success path{'s' if len(rewired) > 1 else ''} {', '.join(rewired)} "
                f"→ redirect [{str(redirect_id)[:8]}] to {self.follow_up_flow_id} "
                f"before end — an operation flow never ends the session (R3)")

    def _follow_up_redirect(self, end_id: str) -> str:
        node_id = _derived_id("f0110bac", f"{self.flow_id}#followup")
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "nodeId": node_id, "type": "redirect",
                "metadata": {"redirect": {"type": "flow",
                                          "flowId": self.follow_up_flow_id}},
                "childNodes": [{"nodeId": end_id, "name": "next"}],
            }
        return node_id

    # ==================================================================
    # S6 — clear the flow's slots on the way OUT
    # ==================================================================

    def rule_s6(self) -> None:
        self._drop_start_clears()
        slots = self.slot_names
        if not slots:
            return
        for node_id, node in self.nodes_of_type("redirect", "escalate"):
            meta = _meta(node)
            modifications = meta.get("stateModifications")
            if not isinstance(modifications, list):
                modifications = []
            added = []
            for slot in slots:
                clear = _clear_modification(slot)
                if clear not in modifications:
                    modifications.append(clear)
                    added.append(slot)
            if added:
                meta["stateModifications"] = modifications
                self.change(
                    f"{_label(node_id, node)}: clears {added} on flow exit — slot "
                    f"values persist for the session and a re-entered flow would "
                    f"skip the question (S6)")

    def _drop_start_clears(self) -> None:
        """An operation flow must not clear at start: it erases the zero-turn fill."""
        if self.role != "operation":
            return
        nodes = self.nodes
        start_id = self.start_id()
        if start_id is None:
            return
        start_edges = _edges(nodes[start_id])
        if len(start_edges) != 1:
            return
        first_id = start_edges[0].get("nodeId")
        first = nodes.get(first_id) if isinstance(first_id, str) else None
        if not isinstance(first, dict) or first.get("type") != "basic":
            return
        if any(isinstance(m, dict) and str(m.get("body") or "").strip()
               for m in first.get("messages") or []):
            return
        meta = first.get("metadata") or {}
        modifications = meta.get("stateModifications") or []
        if not modifications or set(meta) - {"stateModifications"}:
            return
        if not all(isinstance(m, dict) and m.get("type") == "slot"
                   and m.get("modification") == "clear" for m in modifications):
            return
        onward = _edges(first)
        if len(onward) != 1:
            return
        start_edges[0]["nodeId"] = onward[0].get("nodeId")
        del nodes[first_id]
        self.change(
            f"removed the slot-clear node [{str(first_id)[:8]}] after start — "
            f"recognition fills slots from the routing utterance and a clear at "
            f"start erases them (S6)")

    # ==================================================================
    # D3 / D4 — data requests
    # ==================================================================

    def rule_d3(self) -> None:
        if not self.data_requests:
            return
        slots = set(self.slot_names)
        contexts = self._flow_contexts()
        for node_id, node in self.nodes_of_type("data_request"):
            for entry in node.get("dataRequests") or []:
                if not isinstance(entry, dict):
                    continue
                request_id = entry.get("dataRequestId")
                document = self.data_requests.get(request_id)
                if not isinstance(document, dict):
                    continue
                request_schema = document.get("requestSchema") or {}
                properties = request_schema.get("properties") or {}
                if not isinstance(properties, dict) or not properties:
                    continue
                required = set(request_schema.get("required") or [])
                payload = entry.get("payload")
                payload = dict(payload) if isinstance(payload, dict) else {}
                added, missing = [], []
                for field in properties:
                    if str(payload.get(field) or "").strip():
                        continue
                    if field in slots:
                        payload[field] = f"{{{field}:NLX.Slot}}"
                        added.append(field)
                    elif field in contexts:
                        payload[field] = f"{{{field}:NLX.Context}}"
                        added.append(field)
                    else:
                        missing.append(field)
                if added or (payload and entry.get("payload") != payload):
                    entry["payload"] = payload
                if added:
                    self.change(
                        f"{_label(node_id, node)} {request_id}: payload maps {added} "
                        f"— a data_request without a payload posts an empty body (D3)")
                for field in missing:
                    text = (f"data request {request_id} requires field {field} that "
                            f"flow {self.flow_id} never collects")
                    if field in required:
                        self.violation("D3", "cross", text)
                    else:
                        self.change(
                            f"{_label(node_id, node)} {request_id}: optional field "
                            f"{field!r} left unmapped (no slot or context variable "
                            f"of that name; D3)")

    def _flow_contexts(self) -> set[str]:
        names = set(self.context_variables)
        declared = self.flow.get("contextVariables")
        if isinstance(declared, list):
            for entry in declared:
                if isinstance(entry, dict) and entry.get("name"):
                    names.add(str(entry["name"]))
                elif isinstance(entry, str):
                    names.add(entry)
        return names

    def rule_p1(self) -> None:
        """A data request whose payload references a slot that is empty on the
        path that reaches it fails before the HTTP call is made (live: a return
        lookup by return number OR order number sent ``{orderNumber:NLX.Slot}``
        with the order number never asked, the webhook was never invoked and
        the caller was escalated). For each incoming edge the slots guaranteed
        captured on every path from start are computed; when the payload names
        others, the request node is cloned for that edge with a payload reduced
        to the guaranteed slots."""
        for node_id, node in list(self.nodes_of_type("data_request")):
            entries = node.get("dataRequests") if isinstance(node.get("dataRequests"), list) else []
            slot_fields: dict[str, str] = {}  # slot name → payload field
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for field, value in (entry.get("payload") or {}).items():
                    match = _PLACEHOLDER_SLOT.fullmatch(str(value or ""))
                    if match:
                        slot_fields[match.group(1)] = str(field)
            if not slot_fields:
                continue
            parents = [(pid, edge) for pid, pnode in self.nodes.items()
                       for edge in _edges(pnode) if edge.get("nodeId") == node_id]
            if not parents:
                continue
            if len(parents) == 1:
                # One way in: a slot the path never captures is simply dropped from
                # the payload (live: the flow asked for a return number only but the
                # payload also sent the never-asked order number).
                pid, edge = parents[0]
                guaranteed = {slot for slot in slot_fields
                              if _captures_slot(edge, slot) or self._slot_guaranteed_before(pid, slot)}
                missing = set(slot_fields) - guaranteed
                if missing and guaranteed:
                    for entry in node.get("dataRequests") or []:
                        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
                            entry["payload"] = {
                                f: v for f, v in entry["payload"].items()
                                if not (_PLACEHOLDER_SLOT.fullmatch(str(v or ""))
                                        and _PLACEHOLDER_SLOT.fullmatch(str(v or "")).group(1) in missing)}
                    self.change(
                        f"{_label(node_id, node)}: payload dropped {sorted(missing)} — never captured on "
                        f"the path that reaches the request (an unfilled slot placeholder fails the request "
                        f"before the call; P1)")
                continue
            clones: dict[frozenset, str] = {}
            for pid, edge in parents:
                # the capture itself happens on the edge into the request
                # ("slot X exists"), then on every path before its source
                guaranteed = {slot for slot in slot_fields
                              if _captures_slot(edge, slot) or self._slot_guaranteed_before(pid, slot)}
                missing = set(slot_fields) - guaranteed
                if not missing:
                    continue
                key = frozenset(guaranteed)
                clone_id = clones.get(key)
                if clone_id is None:
                    clone = copy.deepcopy(node)
                    clone_id = str(uuid.uuid4())
                    clone["nodeId"] = clone_id
                    for entry in clone.get("dataRequests") or []:
                        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
                            entry["payload"] = {
                                f: v for f, v in entry["payload"].items()
                                if not (_PLACEHOLDER_SLOT.fullmatch(str(v or ""))
                                        and _PLACEHOLDER_SLOT.fullmatch(str(v or "")).group(1) in missing)}
                    self.nodes[clone_id] = clone
                    clones[key] = clone_id
                edge["nodeId"] = clone_id
                self.change(
                    f"{_label(node_id, node)}: path via [{pid[:8]}] never captures {sorted(missing)}; "
                    f"the request is cloned as [{clone_id[:8]}] sending only "
                    f"{sorted(guaranteed) or 'no slots'} (an unfilled slot placeholder fails the "
                    f"request before the call; P1)")

    def _slot_guaranteed_before(self, node_id: str, slot: str) -> bool:
        """True when every path from start to ``node_id`` passes a capture of
        ``slot`` (an edge conditioned on ``slot <slot> exists``)."""
        start = self.start_id()
        if start is None or start == node_id:
            return False
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            if current == node_id:
                return False  # reached without passing a capture of the slot
            for edge in _edges(self.nodes[current]):
                if _captures_slot(edge, slot):
                    continue  # this way is fine: the slot is filled from here on
                target = edge.get("nodeId")
                if isinstance(target, str):
                    stack.append(target)
        return True

    def rule_d4(self) -> None:
        for node_id, node in self.nodes_of_type("data_request"):
            for edge in _edges(node):
                for condition in edge.get("conditions") or []:
                    if not isinstance(condition, dict):
                        continue
                    left = condition.get("left") or {}
                    right = condition.get("right")
                    if left.get("type") != "node_status" or not isinstance(right, dict):
                        continue
                    value = right.get("value")
                    if isinstance(value, str) and value.strip().lower() not in NODE_STATUSES:
                        right["value"] = "failure"
                        self.change(
                            f"{_label(node_id, node)} edge {edge.get('name')!r}: "
                            f"node_status {value!r} → 'failure' — only "
                            f"{list(NODE_STATUSES)} are statuses, so the edge matched "
                            f"nothing and the turn fell through to Fallback (D4)")

            edges = _edges(node)
            if not any(_has_status(edge, status) for edge in edges
                       for status in ("success", "failure", "timeout")):
                continue  # no status routing at all: a different, earlier defect
            # A failure or timeout edge that lands on a silent hand-off — the
            # follow-up redirect or the end node, with no message in between —
            # makes a failed call look like a finished one (live: a schema
            # mismatch sent "더 도와드릴 일이 있을까요?" instead of any answer).
            # Send it where a failure belongs: the escalation.
            for edge in edges:
                if not (_has_status(edge, "failure") or _has_status(edge, "timeout")):
                    continue
                target_node = self.nodes.get(edge.get("nodeId")) or {}
                if not self._silent_handoff(target_node):
                    continue
                escalation = self.escalation_target()
                if escalation is None or escalation == edge.get("nodeId"):
                    continue
                previous = str(edge.get("nodeId") or "")[:8]
                edge["nodeId"] = escalation
                self.change(
                    f"{_label(node_id, node)} edge {edge.get('name') or 'failure'!r}: "
                    f"→ [{previous}] answered nothing on failure; now → escalation "
                    f"[{escalation[:8]}] (D4)")
            if any(_has_status(edge, "failure") for edge in edges):
                continue
            target = self.escalation_target()
            if target is None:
                self.violation(
                    "D4", "flow",
                    f"{_label(node_id, node)} has no node_status 'failure' edge and "
                    f"the flow has no escalation redirect or escalate node to send "
                    f"one to — a failed call would answer nothing")
                continue
            node.setdefault("childNodes", []).append(
                {"nodeId": target, "name": "failure",
                 "conditions": [_status_condition("failure")]})
            self.change(
                f"{_label(node_id, node)}: added node_status 'failure' edge → "
                f"[{target[:8]}] (D4)")

    # ==================================================================
    # M1 — message placeholders must resolve
    # ==================================================================

    # ==================================================================
    # D5 — a condition on a data-request result names a field it returns
    # ==================================================================

    _SUCCESS_LIKE = frozenset({
        "accepted", "ok", "succeeded", "isSuccess", "issuccess", "approved",
        "completed", "done", "valid", "result", "status_ok", "successful",
    })

    def rule_d5(self) -> None:
        """Live (GreenCart, 2026-09-14): a choice branched on
        ``requestReturn.accepted`` while the data request returns ``success``;
        the field never existed, so every accepted return took the "rejected"
        edge to escalation. A result field the schema does not have is
        rewritten to the one obviously meant (the shared ``success`` flag for a
        success-ish name, a close spelling otherwise) and reported when it
        cannot be resolved."""
        if not self.data_requests:
            return
        for node_id, node in self.nodes.items():
            if not isinstance(node, dict):
                continue
            for edge in _edges(node):
                for condition in (edge.get("conditions") or []):
                    if not isinstance(condition, dict):
                        continue
                    for side in ("left", "right"):
                        operand = condition.get(side)
                        if not (isinstance(operand, dict) and operand.get("type") == "variable"
                                and isinstance(operand.get("name"), str) and "." in operand["name"]):
                            continue
                        request_id, _, field = operand["name"].partition(".")
                        document = self.data_requests.get(request_id)
                        if not isinstance(document, dict):
                            continue
                        properties = (document.get("responseSchema") or {}).get("properties") or {}
                        if not isinstance(properties, dict) or not properties:
                            continue
                        if field in properties:
                            self._d5_enum_constant(node_id, node, edge, condition, side, request_id,
                                                   field, properties)
                            continue
                        replacement = None
                        if field in self._SUCCESS_LIKE or field.lower() in self._SUCCESS_LIKE:
                            replacement = next((n for n in properties if n.lower() == "success"), None)
                        if replacement is None:
                            replacement = self._close_field(field, properties)
                        if replacement is None:
                            self.violation(
                                "D5", "cross",
                                f"{_label(node_id, node)} edge {edge.get('name')!r} tests "
                                f"{request_id}.{field}, which {request_id} does not return "
                                f"({sorted(properties)}) — the branch can never be taken")
                            continue
                        operand["name"] = f"{request_id}.{replacement}"
                        self.change(
                            f"{_label(node_id, node)} edge {edge.get('name')!r}: condition field "
                            f"{request_id}.{field} → {request_id}.{replacement} (the field the data "
                            f"request actually returns) (D5)")

    def _d5_enum_constant(self, node_id: str, node: dict, edge: dict, condition: dict, side: str,
                          request_id: str, field: str, properties: dict) -> None:
        operand_name = f"{request_id}.{field}"
        """Live (Hanbit, 2026-09-14): a branch tested ``cancelAppointment.status
        neq "not_cancelable"`` while the API's ``status`` enum is 예/취소/완료 and
        a passed deadline is signalled by ``success=false`` + ``errorCode``; the
        impossible constant made every appointment "cancelable". A constant an
        enum field can never hold is a dead branch: when the response carries the
        shared ``success`` flag the pair is rewritten onto it (eq impossible →
        the failure branch, neq impossible → the success branch); otherwise it is
        reported."""
        other = "right" if side == "left" else "left"
        constant = condition.get(other)
        if not (isinstance(constant, dict) and constant.get("type") == "constant"
                and isinstance(constant.get("value"), str)
                and condition.get("operator") in ("eq", "neq")):
            return
        schema = properties.get(field) if isinstance(properties.get(field), dict) else {}
        enum = schema.get("enum") if isinstance(schema.get("enum"), list) else None
        if not enum:
            enum = self.field_enums.get(request_id, {}).get(field)
        if not enum or constant["value"] in enum:
            return
        success = next((n for n in properties if n.lower() == "success"), None)
        # Only a two-way choice (eq X / neq X on the same impossible constant) has
        # an unambiguous meaning — success vs failure. Live (Hanbit): a
        # three-way choice on three impossible constants collapsed into three
        # identical conditions when every branch was mapped onto the flag.
        edges = _edges(node)
        pair = len(edges) == 2 and all(
            any(isinstance(c, dict) and (c.get("left") or {}).get("name") == operand_name
                and (c.get("right") or {}).get("value") == constant["value"]
                and c.get("operator") in ("eq", "neq")
                for c in (e.get("conditions") or []))
            for e in edges) and {
            c.get("operator") for e in edges for c in (e.get("conditions") or [])
            if isinstance(c, dict) and (c.get("left") or {}).get("name") == operand_name} == {"eq", "neq"}
        if success is None or not pair:
            self.violation(
                "D5", "cross",
                f"{_label(node_id, node)} edge {edge.get('name')!r} compares {request_id}.{field} with "
                f"{constant['value']!r}, a value the API never returns (enum {enum}) — the branch is dead. "
                f"Branch on a value from the enum, or on {request_id}.success / {request_id}.errorCode")
            return
        # Rewrite BOTH edges of the pair now: once the first is on the flag the
        # second no longer looks like a pair.
        for pair_edge in edges:
            for pair_condition in (pair_edge.get("conditions") or []):
                if not (isinstance(pair_condition, dict)
                        and (pair_condition.get("left") or {}).get("name") == operand_name
                        and (pair_condition.get("right") or {}).get("value") == constant["value"]):
                    continue
                operator = "neq" if pair_condition.get("operator") == "eq" else "eq"
                pair_condition["left"] = {"type": "variable", "name": f"{request_id}.{success}"}
                pair_condition["right"] = {"type": "constant", "value": True}
                pair_condition["operator"] = operator
                self.change(
                    f"{_label(node_id, node)} edge {pair_edge.get('name')!r}: {request_id}.{field} vs "
                    f"{constant['value']!r} (not in enum {enum}) → {request_id}.{success} "
                    f"{operator} true (D5)")

    # ==================================================================
    # D6 — the "success" branch must be the one that announces the result
    # ==================================================================

    @staticmethod
    def _success_polarity(edge: dict, request_id: str) -> Optional[bool]:
        """True for ``<request>.success eq true`` (or ``neq false``), False for the
        negation, None when the edge is not a success test on that request."""
        conds = [c for c in (edge.get("conditions") or []) if isinstance(c, dict)]
        if len(conds) != 1:
            return None
        cond = conds[0]
        left = cond.get("left") or {}
        name = str(left.get("name") or "")
        match = _PLACEHOLDER.fullmatch(name)
        if match:
            name = match.group(1)
        if left.get("type") != "variable" or name != f"{request_id}.success":
            return None
        right = (cond.get("right") or {}).get("value")
        if isinstance(right, str):
            if right.lower() in ("true", "false"):
                right = right.lower() == "true"
            else:
                return None
        if not isinstance(right, bool):
            return None
        operator = str(cond.get("operator") or "")
        if operator == "eq":
            return right
        if operator in ("neq", "ne"):
            return not right
        return None

    def _announces_result(self, start_id: str, request_id: str) -> Optional[bool]:
        """Walk the branch from ``start_id`` up to its first customer-facing
        message: True when that message names a ``<request>.<field>`` result,
        False when it is a plain message or an escalation, None when the branch
        reaches no message at all."""
        seen: set[str] = set()
        stack = [start_id]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            node = self.nodes[current]
            bodies = [str(m.get("body") or "") for m in node.get("messages") or [] if isinstance(m, dict)]
            if any(b.strip() for b in bodies):
                return any(m.group(1).startswith(f"{request_id}.") for b in bodies for m in _PLACEHOLDER.finditer(b))
            redirect = (_meta(node).get("redirect") or {}) if node.get("type") == "redirect" else {}
            if str(redirect.get("flowId") or "") == self.escalation_flow_id or node.get("type") == "escalate":
                return False
            for edge in _edges(node):
                target = edge.get("nodeId")
                if isinstance(target, str):
                    stack.append(target)
        return None

    def rule_d6(self) -> None:
        """Generated (2026-09-16): the branch after the intake request had its
        conditions crossed — ``success eq true`` led to the failure message and
        the escalation, ``success neq true`` to the result announcement — so a
        return the backend accepted was reported to the caller as refused. The
        two conditions are swapped back when the branch that names the request's
        result fields is the one guarded by the negative test."""
        for node_id, node in list(self.nodes_of_type("choice")):
            edges = _edges(node)
            for request_id in self.data_requests or {}:
                positive = [e for e in edges if self._success_polarity(e, request_id) is True]
                negative = [e for e in edges if self._success_polarity(e, request_id) is False]
                if len(positive) != 1 or len(negative) != 1:
                    continue
                pos_edge, neg_edge = positive[0], negative[0]
                pos_target, neg_target = pos_edge.get("nodeId"), neg_edge.get("nodeId")
                if not isinstance(pos_target, str) or not isinstance(neg_target, str):
                    continue
                pos_announces = self._announces_result(pos_target, request_id)
                neg_announces = self._announces_result(neg_target, request_id)
                if pos_announces is False and neg_announces is True:
                    pos_edge["conditions"], neg_edge["conditions"] = neg_edge["conditions"], pos_edge["conditions"]
                    self.change(
                        f"{_label(node_id, node)}: '{request_id}.success' tests were crossed — the branch "
                        f"announcing the result [{neg_target[:8]}] sat behind the negative test and the "
                        f"failure branch [{pos_target[:8]}] behind the positive one; conditions swapped (D6)")

    def rule_m1(self) -> None:
        slots = set(self.slot_names)
        for node_id, node in self.nodes.items():
            if not isinstance(node, dict):
                continue
            for message in node.get("messages") or []:
                if not isinstance(message, dict) or not isinstance(message.get("body"), str):
                    continue
                body = message["body"]
                for match in list(_PLACEHOLDER.finditer(body)):
                    name, kind = match.group(1), match.group(2)
                    if kind == "Slot":
                        if slots and name not in slots:
                            self.violation(
                                "M1", "cross",
                                f"{_label(node_id, node)} message placeholder "
                                f"{match.group(0)} names no attached slot "
                                f"(attached: {sorted(slots)})")
                        continue
                    if kind != "Variable" or "." not in name:
                        continue
                    request_id, _, field = name.partition(".")
                    document = self.data_requests.get(request_id)
                    if not isinstance(document, dict):
                        continue
                    properties = (document.get("responseSchema") or {}).get("properties") or {}
                    if not isinstance(properties, dict) or not properties or field in properties:
                        continue
                    replacement = self._close_field(field, properties)
                    if replacement is None:
                        self.violation(
                            "M1", "cross",
                            f"{_label(node_id, node)} message placeholder "
                            f"{match.group(0)} is not a field of {request_id}'s "
                            f"responseSchema ({sorted(properties)}) — it resolves to "
                            f"an empty string at runtime")
                        continue
                    fixed = f"{{{request_id}.{replacement}:NLX.Variable}}"
                    message["body"] = message["body"].replace(match.group(0), fixed)
                    self.change(
                        f"{_label(node_id, node)}: placeholder {request_id}.{field} → "
                        f"{request_id}.{replacement} (responseSchema field; M1)")

    @staticmethod
    def _close_field(field: str, properties: dict) -> Optional[str]:
        """The one responseSchema field the placeholder obviously meant, or None.

        Three signals, cheapest first. Containment carries the live case
        (``price`` → ``unitPrice``, ratio 0.571) that difflib's default 0.6
        cutoff rejects; lowering the cutoff instead would start guessing.
        """
        names = list(properties)
        lowered = field.lower()
        exact = [n for n in names if n.lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        contained = [n for n in names
                     if lowered in n.lower() or n.lower() in lowered]
        if len(contained) == 1:
            return contained[0]
        candidates = difflib.get_close_matches(field, names, n=3, cutoff=0.6)
        if not candidates:
            return None
        if len(candidates) > 1:
            best = difflib.SequenceMatcher(None, field, candidates[0]).ratio()
            runner_up = difflib.SequenceMatcher(None, field, candidates[1]).ratio()
            if best - runner_up < 0.05:
                return None  # ambiguous: refuse to guess
        return candidates[0]

    # ==================================================================
    # RX — redirect targets
    # ==================================================================

    def rule_rx(self) -> None:
        if self.flow_ids is None:
            return
        known = set(self.flow_ids) | SYSTEM_FLOW_IDS | {
            self.follow_up_flow_id, self.escalation_flow_id}
        for node_id, node in self.nodes_of_type("redirect"):
            redirect = (node.get("metadata") or {}).get("redirect")
            if not isinstance(redirect, dict):
                continue
            target = redirect.get("flowId")
            if not isinstance(target, str) or not target.strip():
                continue
            if target in known or target in SYSTEM_FLOW_PLACEHOLDERS:
                continue
            replacement = difflib.get_close_matches(target, sorted(known), n=1, cutoff=0.8)
            if replacement:
                redirect["flowId"] = replacement[0]
                self.change(
                    f"{_label(node_id, node)}: redirect target {target!r} → "
                    f"{replacement[0]!r} (RX)")
                continue
            self.violation(
                "RX", "cross",
                f"{_label(node_id, node)} redirects to flow {target!r}, which is "
                f"neither a bundled flow nor "
                f"{sorted(SYSTEM_FLOW_PLACEHOLDERS)}")

    # ==================================================================
    # J — generative_journey exit edges need conditions
    # ==================================================================

    # ==================================================================
    # J2-J5 — a generative_journey that collects values (live, 2026-09-17)
    # ==================================================================
    #
    # Measured on a deployed application over Connect chat:
    #  * `metadata.generativeJourney.dataCapture.data[{name, type: slot,
    #    required, schema}]` fills the flow's attached slot (AgenticDataCapture
    #    {"reason": "상품불량"}) — the model classified a free description
    #    against the enum schema.
    #  * once every required value is captured the node ends with
    #    GenerativeJourneySucceeded and evaluates its edges: no
    #    `System.gjConditionIndex` matches (those are the exitConditions), so a
    #    journey without a `node_status eq success` edge logs Error NoMessages
    #    and the application falls to its default (Fallback) flow.
    #  * the edges are positional for exitConditions: index i ↔ exitConditions[i].
    #  * a node-level modelType made the journey speak in a workspace whose
    #    default model was never checked; the prompt is what the agent follows.

    JOURNEY_MAX_STEPS = 8
    JOURNEY_DEFAULT_MODEL = "anthropic.claude-haiku-4-5"
    AGENT_EXIT_NAME = "agentRequested"
    AGENT_EXIT_PROMPTS = {
        "ko": "고객이 상담원, 상담사 또는 사람과 직접 통화하기를 원한다",
        "en": "The customer asks to talk to a human agent or representative",
        "ja": "お客様がオペレーターや担当者との通話を希望している",
    }

    def _journey_config(self, node: dict) -> dict:
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        node["metadata"] = meta
        cfg = meta.get("generativeJourney") if isinstance(meta.get("generativeJourney"), dict) else {}
        meta["generativeJourney"] = cfg
        return cfg

    def _capture_schema(self, slot_name: str) -> dict:
        """JSON schema the journey classifies a captured value against."""
        attached = next((s for s in self.attached if s.get("name") == slot_name), None)
        slot_type = str((attached or {}).get("type") or "")
        doc = self.slot_type_docs.get(slot_type) if slot_type else None
        values = [str(v.get("value")) for v in ((doc or {}).get("values") or [])
                  if isinstance(v, dict) and v.get("value")]
        if values:
            return {"type": "string", "enum": values}
        regex = (attached or {}).get("regex") or self._plan_constraints(slot_name)[0]
        if regex:
            return {"type": "string", "pattern": str(regex)}
        if slot_type == "NLX.Number":
            return {"type": "number"}
        return {"type": "string"}

    def _attach_plan_slot(self, slot_name: str) -> bool:
        """Attach a slot the plan names but the document does not (S1 shape)."""
        plan = self.slot_plans.get(slot_name) or {}
        declared = str(plan.get("type") or "NLX.Text")
        if not declared.startswith("NLX.") and self.slot_type_ids is not None \
                and declared not in self.slot_type_ids and declared not in ("text", "string"):
            return False
        if declared in ("text", "string"):
            declared = "NLX.Text"
        entry = {"name": slot_name, "type": declared, "sensitive": bool(plan.get("sensitive"))}
        if plan.get("regex"):
            entry["regex"] = str(plan["regex"])
        if plan.get("description"):
            entry["aiDescription"] = str(plan["description"])[:200]
        slots = self.flow.get("slotTypes")
        if not isinstance(slots, list):
            slots = []
            self.flow["slotTypes"] = slots
        slots.append(entry)
        return True

    def _agent_exit_prompt(self) -> str:
        code = str(self.flow.get("mainLanguageCode") or "").lower()
        for prefix, prompt in self.AGENT_EXIT_PROMPTS.items():
            if code.startswith(prefix):
                return prompt
        return self.AGENT_EXIT_PROMPTS["ko"]

    def rule_j2(self) -> None:
        journeys = self.nodes_of_type("generative_journey")
        if not journeys:
            return
        for position, (node_id, node) in enumerate(journeys):
            step = self.journey_steps[position] if position < len(self.journey_steps) else {}
            cfg = self._journey_config(node)
            label = _label(node_id, node)

            # --- data capture from the plan -------------------------------
            captures = [str(c) for c in (step.get("captures") or []) if str(c).strip()]
            if captures:
                capture = cfg.get("dataCapture") if isinstance(cfg.get("dataCapture"), dict) else {}
                cfg["dataCapture"] = capture
                data = [d for d in (capture.get("data") or []) if isinstance(d, dict) and d.get("name")]
                present = {str(d["name"]) for d in data}
                for name in captures:
                    if name not in self.slot_names and not self._attach_plan_slot(name):
                        self.violation(
                            "J2", "flow",
                            f"{label} captures {name!r}, which is neither attached to the flow "
                            f"nor a slot the plan can attach")
                        continue
                    if name in present:
                        continue
                    data.append({"name": name, "type": "slot", "required": True,
                                 "schema": self._capture_schema(name)})
                    self.change(f"{label}: dataCapture collects slot {name!r} (J2)")
                for entry in data:
                    entry.setdefault("type", "slot")
                    entry.setdefault("required", True)
                    if not isinstance(entry.get("schema"), dict):
                        entry["schema"] = self._capture_schema(str(entry["name"]))
                capture["data"] = data
                if capture.get("exitEnabled") is not True:
                    capture["exitEnabled"] = True
                    self.change(f"{label}: dataCapture.exitEnabled (the journey ends when every "
                                f"required value is captured) (J2)")
            elif isinstance(cfg.get("dataCapture"), dict) and cfg["dataCapture"].get("data"):
                for entry in cfg["dataCapture"]["data"]:
                    if isinstance(entry, dict) and entry.get("name") and entry["name"] not in self.slot_names \
                            and not self._attach_plan_slot(str(entry["name"])):
                        self.violation("J2", "flow",
                                       f"{label} captures {entry['name']!r}, which is not a slot of this flow")

            # --- exits ----------------------------------------------------
            edges = _edges(node)
            captured_names = [str(d["name"]) for d in ((cfg.get("dataCapture") or {}).get("data") or [])
                              if isinstance(d, dict) and d.get("name")]
            has_captured_edge = any(
                all(_captures_slot(e, name) for name in captured_names) for e in edges
            ) if captured_names else True
            if not has_captured_edge:
                # the continuation the model meant: the exit-condition-0 edge,
                # else the first edge that is not a status/agent branch
                target = None
                for e in edges:
                    if self._journey_index(e) == 0:
                        target = e.get("nodeId")
                        break
                if target is None:
                    for e in edges:
                        if not (_has_status(e, "timeout") or _has_status(e, "failure")) \
                                and self._journey_index(e) is None:
                            target = e.get("nodeId")
                            break
                if target is None:
                    self.violation(
                        "J3", "flow",
                        f"{label} captures {captured_names} but has no edge to continue on once they "
                        f"are captured (an edge testing the captured slots, or an exit-condition edge to reuse)")
                else:
                    node.setdefault("childNodes", []).insert(0, {
                        "nodeId": target, "name": "captured",
                        "conditions": [_slot_condition(name, "exists") for name in captured_names]})
                    self.change(f"{label}: added captured branch ({' and '.join(captured_names)} exist) "
                                f"→ [{str(target)[:8]}] (J3)")

            conditions = [c for c in (cfg.get("exitConditions") or []) if isinstance(c, dict)]
            has_agent_exit = any(str(c.get("name") or "") == self.AGENT_EXIT_NAME for c in conditions)
            if not has_agent_exit and self.role == "operation":
                conditions.append({"name": self.AGENT_EXIT_NAME, "prompt": self._agent_exit_prompt()})
                cfg["exitConditions"] = conditions
                index = len(conditions) - 1
                node.setdefault("childNodes", []).append({
                    "nodeId": self._agent_request_redirect(), "name": self.AGENT_EXIT_NAME,
                    "conditions": [{
                        "left": {"type": "system", "name": "System.gjConditionIndex"},
                        "operator": "eq",
                        "right": {"type": "constant", "value": index}}]})
                self.change(f"{label}: exit condition {index} '{self.AGENT_EXIT_NAME}' → agent request (J4)")
            elif conditions and cfg.get("exitConditions") is not conditions:
                cfg["exitConditions"] = conditions

            # --- tools ------------------------------------------------------
            tools = [t for t in (cfg.get("tools") or []) if isinstance(t, dict)]
            kept = []
            for tool in tools:
                kind = tool.get("type")
                if kind in ("dataRequest", "mcpFlow"):
                    self.violation(
                        "J5", "flow",
                        f"{label} tool type {kind!r} is not deployable from a bundle (the service drops "
                        f"a dataRequest tool's id; mcpFlow fails on invocation) — the flow's data_request "
                        f"node calls the backend after the journey")
                    continue
                kept.append(tool)
            wants_kb = "knowledge_base" in [str(t) for t in (step.get("journey_tools") or [])]
            if wants_kb and self.kb_name and not any(t.get("type") == "knowledgeBase" for t in kept):
                kept.append({"type": "knowledgeBase", "knowledgeBaseId": f"{{KB:{self.kb_name}}}",
                             "scopeTags": []})
                self.change(f"{label}: knowledgeBase tool {self.kb_name!r} (J5)")
            if kept or tools:
                cfg["tools"] = kept

            # --- bounds & model -----------------------------------------------
            if not isinstance(cfg.get("maxSteps"), int) or cfg["maxSteps"] < 1:
                cfg["maxSteps"] = self.JOURNEY_MAX_STEPS
                self.change(f"{label}: maxSteps {self.JOURNEY_MAX_STEPS} (J5)")
            if not cfg.get("modelType"):
                cfg["modelType"] = self.JOURNEY_DEFAULT_MODEL
                self.change(f"{label}: modelType {self.JOURNEY_DEFAULT_MODEL} (a workspace without a "
                            f"default model runs a silent journey) (J5)")

    def rule_j(self) -> None:
        for node_id, node in self.nodes_of_type("generative_journey"):
            edges = _edges(node)
            if not edges:
                continue

            taken: set[int] = set()
            has_timeout = has_failure = False
            unconditioned: list[dict] = []
            for edge in edges:
                index = self._journey_index(edge)
                if index is not None:
                    taken.add(index)
                elif _has_status(edge, "timeout"):
                    has_timeout = True
                elif _has_status(edge, "failure"):
                    has_failure = True
                elif not (edge.get("conditions") or edge.get("generativeCondition")):
                    unconditioned.append(edge)

            # An unconditioned journey edge is simply disconnected at runtime.
            # Exit branches are positional, so they take the free indices in
            # edge order; timeout and failure are added as their own edges below.
            for edge in unconditioned:
                index = next(i for i in range(len(edges) + len(taken) + 1)
                             if i not in taken)
                taken.add(index)
                edge["conditions"] = [{
                    "left": {"type": "system", "name": "System.gjConditionIndex"},
                    "operator": "eq",
                    "right": {"type": "constant", "value": index}}]
                self.change(
                    f"{_label(node_id, node)} edge {edge.get('name')!r}: exit "
                    f"condition {index} (an unconditioned journey edge is "
                    f"disconnected; J)")

            for status, present in (("timeout", has_timeout), ("failure", has_failure)):
                if present:
                    continue
                target = self.escalation_target()
                if target is None:
                    self.violation(
                        "J", "flow",
                        f"{_label(node_id, node)} has no {status} branch and the flow "
                        f"has no escalation redirect or escalate node to add one to")
                    continue
                node.setdefault("childNodes", []).append(
                    {"nodeId": target, "name": status,
                     "conditions": [_status_condition(status)]})
                self.change(
                    f"{_label(node_id, node)}: added {status} branch → "
                    f"[{target[:8]}] (J)")

    @staticmethod
    def _journey_index(edge: dict) -> Optional[int]:
        for condition in edge.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            left = condition.get("left") or {}
            right = condition.get("right") or {}
            if left.get("name") == "System.gjConditionIndex" \
                    and isinstance(right.get("value"), int):
                return right["value"]
        return None

    # ==================================================================
    # A2 — routing metadata is ASCII
    # ==================================================================

    def rule_a2(self) -> None:
        for field in ("aiDescription", "description"):
            value = self.flow.get(field)
            if isinstance(value, str) and any(ord(c) > 0x7F for c in value):
                self.violation(
                    "A2", "flow",
                    f"flow {field} contains non-ASCII characters; ACXD routing "
                    f"metadata must be ASCII (do not translate the customer "
                    f"messages — rewrite this field in English)")
        for slot in self.attached:
            value = slot.get("aiDescription")
            if isinstance(value, str) and any(ord(c) > 0x7F for c in value):
                self.violation(
                    "A2", "flow",
                    f"attached slot {slot['name']!r} aiDescription contains "
                    f"non-ASCII characters; ACXD metadata must be ASCII")

    # ==================================================================
    # housekeeping
    # ==================================================================

    def prune_disconnected(self, before: set[str]) -> None:
        """Drop every node the start node cannot reach.

        Two sources: our own rewrites (R7's orphaned `end`, folded retries) and
        nodes the model left dangling in the first place. Either way the node is
        dead weight the runtime never executes and the flow gate would refuse
        (FLOW_UNREACHABLE_NODE), so removing it cannot change behaviour — while
        an edge that points at the wrong id still dangles and is still reported.
        Nothing is pruned when there is no start node to reach from."""
        nodes = self.nodes
        after = _reachable(nodes, self.start_id())
        if not after:
            return
        orphans = sorted(node_id for node_id in nodes if node_id not in after)
        if not orphans:
            return
        for node_id in orphans:
            node = nodes.pop(node_id, None)
            if node is not None:
                why = ("unreachable after the rewrites above (R7)" if node_id in before
                       else "never reachable from the start node (model left it dangling)")
                self.change(f"dropped {_label(node_id, node)} — {why}")
        for node in nodes.values():
            if not isinstance(node, dict) or not isinstance(node.get("childNodes"), list):
                continue
            kept = [e for e in node["childNodes"]
                    if not (isinstance(e, dict) and e.get("nodeId") in set(orphans))]
            if len(kept) != len(node["childNodes"]):
                node["childNodes"] = kept

    def run(self) -> None:
        before = _reachable(self.nodes, self.start_id())
        self.rule_s1()
        self.rule_s5()
        self.rule_s9()
        self.rule_s2()
        self.rule_s3()
        self.rule_s8()
        self.rule_m3()
        self.rule_m2()
        self.rule_r7()
        self.rule_r6()
        self.rule_d3()
        self.rule_r8()
        self.rule_f1()
        self.rule_e2()
        self.rule_r3()
        self.rule_s6()
        self.rule_p1()
        self.rule_d4()
        self.rule_d5()
        self.rule_d6()
        self.rule_m1()
        self.rule_rx()
        self.rule_j2()
        self.rule_j()
        self.rule_a2()
        self.prune_disconnected(before)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def field_enums_from_integrations(integrations) -> dict:
    """``{data request id: {field: [allowed values]}}`` from an ACXDFlowSpec's
    ``data_integrations``. The deployed reply schema carries types only, so this
    is the one place D5 can learn which constants a result field can hold — the
    generator passes it at build time and the D9 gate at review/download time,
    so an impossible constant is reported in both places, not just one."""
    enums: dict = {}
    for integration in integrations or []:
        if not isinstance(integration, dict) or not integration.get("data_request_id"):
            continue
        per_field = {}
        for field in integration.get("response_fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            values = field.get("enum_values") or field.get("enum")
            if isinstance(values, (list, tuple)) and values:
                per_field[str(field["name"])] = [str(v) for v in values]
        if per_field:
            enums[str(integration["data_request_id"])] = per_field
    return enums


def apply_runtime_contract(
    flow: dict,
    *,
    role: Optional[str] = None,
    slot_type_ids: Optional[Iterable[str]] = None,
    slot_type_docs: Optional[dict] = None,
    data_requests: Optional[dict] = None,
    flow_ids: Optional[Iterable[str]] = None,
    context_variables: Optional[Iterable[str]] = None,
    follow_up_flow_id: str = "FollowUpFlow",
    escalation_flow_id: str = "EscalationFlow",
    slot_plans: Optional[dict] = None,
    field_labels: Optional[dict] = None,
    field_enums: Optional[dict] = None,
    journey_steps: Optional[list] = None,
    kb_name: Optional[str] = None,
) -> tuple[dict, list[str]]:
    """Normalize ``flow`` onto the live-verified runtime contract.

    Args:
        flow: the flow document (canonicalized first — see
            :mod:`tools.acxd_flow_canonicalizer`).
        role: the flow's role in the application. ``"operation"`` enables the
            continuity rules (R3, and the ban on clearing slots at start);
            system roles (``welcome`` / ``fallback`` / ``follow_up`` /
            ``escalation``) are left to end the session themselves.
        slot_type_ids: every custom slotTypeId the bundle defines (S1).
        slot_type_docs: slotTypeId -> slot type document, for the open-value
            detection (S5).
        data_requests: dataRequestId -> data request document, for payload
            mapping and placeholder checks (D3, M1).
        flow_ids: every flowId in the bundle, for redirect targets (RX).
        context_variables: workspace-level context variable names (D3).
        slot_plans: slot name -> the interview's slot plan (``type``, ``regex``,
            length constraints, ``examples``). Lets S1 resolve an open-value slot
            the way the live bundle needed it (``NLX.AlphaNumeric`` + regex for a
            10-digit order number) and rescue an attached slot type id the bundle
            no longer defines.

    Returns:
        ``(normalized flow, change notes)``. The input is not mutated.
        Deterministic and idempotent: applying the result again yields the same
        document and no further notes.
    """
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), dict):
        return flow, []
    engine = _RuntimeContract(
        copy.deepcopy(flow), role=role, slot_type_ids=slot_type_ids,
        slot_type_docs=slot_type_docs, data_requests=data_requests,
        flow_ids=flow_ids, context_variables=context_variables,
        follow_up_flow_id=follow_up_flow_id, escalation_flow_id=escalation_flow_id,
        slot_plans=slot_plans, field_labels=field_labels, field_enums=field_enums,
        journey_steps=journey_steps, kb_name=kb_name)
    engine.run()
    return engine.flow, engine.changes


def runtime_contract_violations(
    flow: dict,
    *,
    role: Optional[str] = None,
    slot_type_ids: Optional[Iterable[str]] = None,
    slot_type_docs: Optional[dict] = None,
    data_requests: Optional[dict] = None,
    flow_ids: Optional[Iterable[str]] = None,
    context_variables: Optional[Iterable[str]] = None,
    follow_up_flow_id: str = "FollowUpFlow",
    escalation_flow_id: str = "EscalationFlow",
    scope: str = "all",
    slot_plans: Optional[dict] = None,
    field_labels: Optional[dict] = None,
    field_enums: Optional[dict] = None,
    journey_steps: Optional[list] = None,
    kb_name: Optional[str] = None,
) -> list[str]:
    """Report what the runtime contract cannot repair. Never mutates ``flow``.

    ``scope`` selects which rules to report so the two gates do not
    double-report the same finding:

    * ``"flow"`` — rules that read only the flow document (S1 vocabulary, S2,
      S3, R6/R7, D4, J, A2). Wired into
      :func:`tools.validate_acxd_flow.validate_acxd_asset`.
    * ``"cross"`` — rules that need the rest of the bundle (S1 membership, S5,
      D3, M1, RX). Wired into the D9 gate in
      :mod:`tools.validate_acxd_consistency`.
    * ``"normalizer"`` — rules about conversation shape that
      :func:`apply_runtime_contract` normally repairs outright (M2, R3, S6);
      their residue belongs to the generator's repair loop, not to a schema
      gate, so a minimal unit fixture is never failed by one.
    * ``"all"`` — every scope (direct callers, CLI, tests).

    A violation is exactly what :func:`apply_runtime_contract` refused to guess
    at, so a normalized flow returns ``[]``.
    """
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), dict):
        return []
    engine = _RuntimeContract(
        copy.deepcopy(flow), role=role, slot_type_ids=slot_type_ids,
        slot_type_docs=slot_type_docs, data_requests=data_requests,
        flow_ids=flow_ids, context_variables=context_variables,
        follow_up_flow_id=follow_up_flow_id, escalation_flow_id=escalation_flow_id,
        slot_plans=slot_plans, field_labels=field_labels, field_enums=field_enums,
        journey_steps=journey_steps, kb_name=kb_name)
    engine.run()
    wanted = set(ALL_SCOPES) if scope == "all" else {scope}
    return [message for item_scope, _rule, message in engine.violations
            if item_scope in wanted]
