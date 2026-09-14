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
    "UnknownFlow",
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
FLOW_SCOPE_RULES = frozenset({"S1", "S2", "S3", "S8", "M3", "R6", "R7", "D4", "J", "A2"})
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


def _slot_condition(slot: str, operator: str) -> dict:
    return {"left": {"type": "slot", "name": slot}, "operator": operator}


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

        Extension of S1 (documented, not silent): the live SELC bundle needed
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

    def rule_s9(self) -> None:
        """A built-in text slot whose regex is a date (YYYY-MM-DD) or time (HH:MM)
        skeleton becomes ``NLX.Date`` / ``NLX.Time`` without the regex. Built-in
        values arrive without separators (live), so a regex with '-' or ':' can
        never match: the appointment date '2026-09-18' was rejected, the flow's
        retry edge led to the fallback, and the third miss escalated. The date
        type recognised the same input in another project."""
        for slot in self.attached:
            slot_type = slot.get("type")
            regex = slot.get("regex")
            if slot_type not in ("NLX.AlphaNumeric", "NLX.Text") or not isinstance(regex, str):
                continue
            target = None
            if self._DATE_REGEX.match(regex.strip()):
                target = "NLX.Date"
            elif self._TIME_REGEX.match(regex.strip()):
                target = "NLX.Time"
            if target is None:
                continue
            slot["type"] = target
            slot.pop("regex", None)
            self.change(
                f"slot {slot.get('name')!r}: {slot_type} with regex {regex!r} → {target} "
                f"(built-in values carry no separators, so the regex never matched; S9)")

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
        """Live (SELC, 2026-09-13): the model wrote `privacyConsent eq "yes"`
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
        """Live (SELC, 2026-09-13): the price announcement was a ``basic`` node
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
                # Live (SELC): the flow's own basic node had already said
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
                # Live (SELC v3): "성공 시 배송상태와 예정일을 자연스럽게 안내한다" has no
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
            node["type"] = "basic"
            node["messages"] = [{"type": "text", "body": body}]
            meta = node.get("metadata")
            if isinstance(meta, dict):
                meta.pop("generativeText", None)
                if not meta:
                    node.pop("metadata", None)
            self.change(
                f"{_label(node_id, node)}: generative_text → basic with a templated "
                f"message (generative_text sends nothing; M2)")

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
        self.rule_r3()
        self.rule_s6()
        self.rule_d3()
        self.rule_d4()
        self.rule_d5()
        self.rule_m1()
        self.rule_rx()
        self.rule_j()
        self.rule_a2()
        self.prune_disconnected(before)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

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
        slot_plans=slot_plans, field_labels=field_labels)
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
        slot_plans=slot_plans, field_labels=field_labels)
    engine.run()
    wanted = set(ALL_SCOPES) if scope == "all" else {scope}
    return [message for item_scope, _rule, message in engine.violations
            if item_scope in wanted]
