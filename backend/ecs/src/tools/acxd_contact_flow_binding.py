"""Normalize a generated Amazon Connect flow for the ACXD runtime target.

The Agentic CX Flow-Language action type is not publicly documented, so the
normalizer preserves an importable ``MessageParticipant`` placeholder while
recording the exact manual-wiring contract in ``Metadata.acxdBinding``.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from tools.acxd_flow_spec import get_acxd_flow_spec

#: Flow-Language action type of the Agentic CX block — taken from a Connect
#: console export (2026-09-10) and verified to re-import via CreateContactFlow.
#: Reference: knowledge-base-docs/contact-flow/_reference-console-export-agentic-cx-block.json
AGENTIC_CX_ACTION_TYPE: str = "ConnectParticipantWithAgenticCX"

#: Identifier of the generated block (kept for bundles that still carry the
#: pre-2026-09-10 MessageParticipant placeholder under this id).
AGENTIC_CX_PLACEHOLDER_ID = "AgenticCXPlaceholder"
AGENTIC_CX_ALIAS_PLACEHOLDER = "{ACXD_ALIAS_ID}"
MAX_CONTEXT_VARIABLES = 10

_LEX_SESSION_ATTRIBUTE = re.compile(r"\$\.Lex\.SessionAttributes\.([A-Za-z0-9_]+)")
_ATTRIBUTE_REFERENCE = re.compile(r"\$\.Attributes\.([A-Za-z0-9_]+)")


def _model_dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value or {}) if isinstance(value, dict) else {}


def _action_id(action: dict) -> str:
    return str(action.get("Identifier") or action.get("identifier") or "")


def _action_type(action: dict) -> str:
    return str(action.get("Type") or action.get("type") or "")


def _set_action_id(action: dict, identifier: str) -> None:
    if "identifier" in action and "Identifier" not in action:
        action["identifier"] = identifier
    else:
        action["Identifier"] = identifier


def _transitions(action: dict) -> dict:
    transitions = action.get("Transitions") or action.get("transitions")
    if not isinstance(transitions, dict):
        transitions = {}
    action["Transitions"] = transitions
    action.pop("transitions", None)
    return transitions


def _replace_exact(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_exact(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_exact(item, old, new) for item in value]
    return new if value == old else value


def _rewrite_context_references(value: Any, context_names: set[str], *, rewrite_attributes: bool) -> Any:
    if isinstance(value, dict):
        return {
            key: _rewrite_context_references(item, context_names, rewrite_attributes=rewrite_attributes)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite_context_references(item, context_names, rewrite_attributes=rewrite_attributes)
            for item in value
        ]
    if not isinstance(value, str):
        return value

    result = _LEX_SESSION_ATTRIBUTE.sub(
        lambda match: f"$.AgenticCX.ContextVariables.{match.group(1)}", value
    )
    if rewrite_attributes:
        result = _ATTRIBUTE_REFERENCE.sub(
            lambda match: (
                f"$.AgenticCX.ContextVariables.{match.group(1)}"
                if match.group(1) in context_names else match.group(0)
            ),
            result,
        )
    return result


def _remove_actions(document: dict, action_types: set[str]) -> None:
    """Splice every action of the given types out of the flow, re-pointing all
    transitions (StartAction, NextAction, Errors, Conditions) at the removed
    action's own successor, so the graph stays connected."""
    actions = [item for item in document.get("Actions") or [] if isinstance(item, dict)]
    doomed = [a for a in actions if _action_type(a) in action_types and _action_id(a)]
    for action in doomed:
        old = _action_id(action)
        transitions = _transitions(action)
        successor = transitions.get("NextAction") or next(
            (item.get("NextAction") for item in transitions.get("Errors") or []
             if isinstance(item, dict) and item.get("NextAction")), None)
        actions = [a for a in actions if _action_id(a) != old]
        if successor and successor != old:
            if document.get("StartAction") == old:
                document["StartAction"] = successor
            for other in actions:
                trans = _transitions(other)
                if trans.get("NextAction") == old:
                    trans["NextAction"] = successor
                for item in list(trans.get("Errors") or []) + list(trans.get("Conditions") or []):
                    if isinstance(item, dict) and item.get("NextAction") == old:
                        item["NextAction"] = successor
        action_metadata = (document.get("Metadata") or {}).get("ActionMetadata")
        if isinstance(action_metadata, dict):
            action_metadata.pop(old, None)
    document["Actions"] = actions


_FALLBACK_TEXT_BY_LANGUAGE = {
    "ko": "죄송합니다. 지금은 상담 시스템에 연결할 수 없습니다. 잠시 후 다시 전화해 주세요.",
    "ja": "申し訳ありません。現在アシスタントに接続できません。しばらくしてからおかけ直しください。",
    "en": "We are sorry, but the assistant is unavailable. Please call again later.",
}


def _application_language(application: dict) -> str:
    locales = application.get("locales") or []
    primary = application.get("primary_locale") or (locales[0] if locales else "") or application.get("language") or ""
    return str(primary).split("-")[0].lower() or "en"


def _fallback_text(application: dict) -> str:
    return _FALLBACK_TEXT_BY_LANGUAGE.get(_application_language(application), _FALLBACK_TEXT_BY_LANGUAGE["en"])


def _english_default_fallback(action: dict) -> bool:
    text = str((action.get("Parameters") or {}).get("Text") or "")
    return text.startswith("We are sorry, but the assistant is unavailable")


_NOTICE_HINTS = ("record", "notice", "consent", "legal", "녹음", "고지")


def _is_recording_notice(action: dict, plan_data: dict) -> bool:
    """A pre-block announcement the spec asked for (recording/legal notice)."""
    ident = _action_id(action).lower()
    text = str((action.get("Parameters") or {}).get("Text") or "")
    if any(hint in ident for hint in _NOTICE_HINTS) or any(hint in text for hint in ("녹음", "recorded", "録音")):
        return True
    for notice in _spec_notice_texts(plan_data):
        if notice and notice in text:
            return True
    return False


def _spec_notice_texts(plan_data: dict) -> list[str]:
    """Recording/legal notice texts the specs asked the Contact Flow to play."""
    behaviors = list((plan_data.get("contact_flow") or {}).get("behaviors") or [])
    try:
        from tools.spec_manager import get_contact_flow_spec
        cf_spec = get_contact_flow_spec()
        if cf_spec is not None:
            behaviors += list(_model_dump(cf_spec).get("behaviors") or [])
    except Exception:
        pass
    out: list[str] = []
    for behavior in behaviors:
        if not isinstance(behavior, dict) or str(behavior.get("behavior", "")).lower() != "recording":
            continue
        params = behavior.get("parameters") or {}
        for key in ("message", "notice", "text", "recording_message"):
            value = str(params.get(key) or "").strip()
            if value:
                out.append(value)
    return out


def _strip_owned_speech(document: dict, plan_data: dict) -> list[str]:
    """Remove MessageParticipant actions the application owns: everything spoken
    before the Agentic CX block except a recording notice, and everything on the
    Default (conversation finished) path — the app already said goodbye.
    Returns the removed identifiers."""
    actions = [a for a in document.get("Actions") or [] if isinstance(a, dict)]
    by_id = {_action_id(a): a for a in actions if _action_id(a)}
    pre_block = _actions_before_block(document)
    block = next((a for a in actions if _action_type(a) == AGENTIC_CX_ACTION_TYPE), None)
    default_path: set[str] = set()
    if block is not None:
        ident = (_transitions(block)).get("NextAction")
        while ident in by_id and ident not in default_path:
            default_path.add(ident)
            ident = _transitions(by_id[ident]).get("NextAction")
    doomed: list[str] = []
    for ident in list(pre_block) + sorted(default_path):
        action = by_id.get(ident)
        if action is None or _action_type(action) != "MessageParticipant":
            continue
        if ident in pre_block and _is_recording_notice(action, plan_data):
            continue
        if _action_id(action) == "AgenticCXFallbackMessage":
            continue
        doomed.append(ident)
    for ident in doomed:
        _remove_action_by_id(document, ident)
    return doomed


def _remove_action_by_id(document: dict, old: str) -> None:
    """Splice one action out of the graph, re-pointing every transition at its
    own successor (NextAction first, else its first error target)."""
    actions = [a for a in document.get("Actions") or [] if isinstance(a, dict)]
    action = next((a for a in actions if _action_id(a) == old), None)
    if action is None:
        return
    transitions = _transitions(action)
    successor = transitions.get("NextAction") or next(
        (item.get("NextAction") for item in transitions.get("Errors") or []
         if isinstance(item, dict) and item.get("NextAction")), None)
    actions = [a for a in actions if _action_id(a) != old]
    if successor and successor != old:
        if document.get("StartAction") == old:
            document["StartAction"] = successor
        for other in actions:
            trans = _transitions(other)
            if trans.get("NextAction") == old:
                trans["NextAction"] = successor
            for item in list(trans.get("Errors") or []) + list(trans.get("Conditions") or []):
                if isinstance(item, dict) and item.get("NextAction") == old:
                    item["NextAction"] = successor
    action_metadata = (document.get("Metadata") or {}).get("ActionMetadata")
    if isinstance(action_metadata, dict):
        action_metadata.pop(old, None)
    document["Actions"] = actions


def _authored_branches(candidate: Optional[dict], actions: list) -> dict[str, str]:
    """Branch targets the generator already wired on the block being replaced,
    when each names an existing action (other than the block itself)."""
    if not isinstance(candidate, dict):
        return {}
    ids = {_action_id(a) for a in actions if isinstance(a, dict)}
    own = _action_id(candidate)
    transitions = candidate.get("Transitions") or {}

    def _ok(target: Any) -> Optional[str]:
        return str(target) if target and str(target) in ids and str(target) != own else None

    out: dict[str, str] = {}
    if _ok(transitions.get("NextAction")):
        out["Default"] = _ok(transitions.get("NextAction"))
    for condition in transitions.get("Conditions") or []:
        if not isinstance(condition, dict):
            continue
        operands = [str(o) for o in ((condition.get("Condition") or {}).get("Operands") or [])]
        if any(o.lower() in {"escalation", "escalate"} for o in operands) and _ok(condition.get("NextAction")):
            out["Escalation"] = _ok(condition.get("NextAction"))
    for error in transitions.get("Errors") or []:
        if not isinstance(error, dict):
            continue
        if error.get("ErrorType") == "NoMatchingError" and _ok(error.get("NextAction")):
            out["Error"] = _ok(error.get("NextAction"))
        if error.get("ErrorType") == "InputTimeLimitExceeded" and _ok(error.get("NextAction")):
            out["IdleChatTimeout"] = _ok(error.get("NextAction"))
    return out


def _actions_before_block(document: dict) -> set[str]:
    """Identifiers reachable from StartAction without passing the Agentic CX
    block (the block itself excluded)."""
    by_id = {_action_id(a): a for a in document.get("Actions") or [] if isinstance(a, dict) and _action_id(a)}
    start = document.get("StartAction")
    before: set[str] = set()
    queue = [start] if start in by_id else []
    while queue:
        ident = queue.pop(0)
        if ident in before or ident not in by_id:
            continue
        action = by_id[ident]
        if _action_type(action) == AGENTIC_CX_ACTION_TYPE or str(ident).startswith("AgenticCX"):
            continue
        before.add(ident)
        transitions = action.get("Transitions") or {}
        queue.append(transitions.get("NextAction"))
        queue.extend(c.get("NextAction") for c in transitions.get("Conditions") or [] if isinstance(c, dict))
        queue.extend(e.get("NextAction") for e in transitions.get("Errors") or [] if isinstance(e, dict))
    return before


def _reachable_action_ids(document: dict) -> set[str]:
    by_id = {_action_id(action): action for action in document.get("Actions") or [] if _action_id(action)}
    reachable: set[str] = set()
    todo = [document.get("StartAction")]
    while todo:
        identifier = todo.pop()
        if not identifier or identifier in reachable or identifier not in by_id:
            continue
        reachable.add(identifier)
        transitions = _transitions(by_id[identifier])
        targets = [transitions.get("NextAction")]
        targets.extend(item.get("NextAction") for item in transitions.get("Errors") or [] if isinstance(item, dict))
        targets.extend(item.get("NextAction") for item in transitions.get("Conditions") or [] if isinstance(item, dict))
        todo.extend(target for target in targets if target)
    return reachable


def _context_variables(application: dict) -> list[dict]:
    variables: list[dict] = []
    for raw in (application.get("context_variables") or [])[:MAX_CONTEXT_VARIABLES]:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        variables.append({
            "name": raw["name"],
            "type": raw.get("type") or "string",
            "description": raw.get("description") or "",
            "fromContactAttribute": raw.get("from_contact_attribute"),
        })
    return variables


def _find_or_add_action(actions: list[dict], action_type: str, identifier: str, action: dict) -> str:
    existing = next((item for item in actions if _action_type(item) == action_type), None)
    if existing:
        return _action_id(existing)
    action["Identifier"] = identifier
    actions.append(action)
    return identifier


def _build_binding(application: dict, branches: dict[str, str]) -> dict:
    context_variables = _context_variables(application)
    return {
        "workspaceId": "{ACXD_WORKSPACE_ID}",
        "applicationId": "{ACXD_APPLICATION_ID}",
        "aliasId": "{ACXD_ALIAS_ID}",
        "speechEngine": application.get("speech_engine") or "agentic_voice",
        "contextVariables": context_variables,
        "branches": branches,
    }


def normalize_acxd_contact_flow(
    flow: dict,
    flow_spec: Optional[Any] = None,
    *,
    rewrite_attribute_context: bool = False,
) -> dict:
    """Replace the Lex block with the Agentic CX placeholder and binding.

    The returned flow has four resolvable branch targets: Default and idle-chat
    timeout disconnect, Error reaches a fallback message, and Escalation reaches
    a queue-transfer action.  It is safe to call twice.
    """
    document = copy.deepcopy(flow)
    if not isinstance(document, dict):
        raise TypeError("contact flow must be a JSON object")

    plan = flow_spec if flow_spec is not None else get_acxd_flow_spec()
    plan_data = _model_dump(plan)
    application = plan_data.get("application") or {}
    actions = [item for item in document.get("Actions") or [] if isinstance(item, dict)]
    document["Actions"] = actions

    # ACXD answers FAQ from its own knowledge base, so the Classic Q in Connect
    # session block has nothing to bind to — deploy.sh skips the assistant for
    # this target and the unresolved {{WISDOM_ASSISTANT_ARN}} fails the import
    # ("Invalid Action property value ... WisdomAssistantArn", live 2026-09-10).
    # Splice such actions out, re-pointing every transition at their successor.
    _remove_actions(document, {"CreateWisdomSession", "UpdateWisdomSession"})
    actions = document["Actions"]

    disconnect_id = _find_or_add_action(
        actions,
        "DisconnectParticipant",
        "AgenticCXDisconnect",
        {"Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
    )
    fallback_id = "AgenticCXFallbackMessage"
    fallback_text = _fallback_text(application)
    existing_fallback = next((item for item in actions if _action_id(item) == fallback_id), None)
    if existing_fallback is None:
        actions.append({
            "Identifier": fallback_id,
            "Type": "MessageParticipant",
            "Parameters": {"Text": fallback_text},
            "Transitions": {"NextAction": disconnect_id},
        })
    elif _english_default_fallback(existing_fallback) and not fallback_text.startswith("We are sorry"):
        # an earlier binding pass wrote the English default into a non-English flow
        existing_fallback.setdefault("Parameters", {})["Text"] = fallback_text

    queue_id = _find_or_add_action(
        actions,
        "TransferContactToQueue",
        "AgenticCXQueueTransfer",
        {
            "Type": "TransferContactToQueue",
            "Parameters": {},
            "Transitions": {"Errors": [{"ErrorType": "NoMatchingError", "NextAction": fallback_id}]},
        },
    )

    candidate_index = next(
        (
            index for index, action in enumerate(actions)
            if _action_id(action) in {AGENTIC_CX_PLACEHOLDER_ID, "AgenticCX"}
            or _action_type(action) == AGENTIC_CX_ACTION_TYPE
        ),
        None,
    )
    if candidate_index is None:
        candidate_index = next(
            (index for index, action in enumerate(actions)
             if _action_type(action) in {"ConnectParticipantWithLexBot", "GetParticipantInput"}),
            None,
        )

    # The Agentic CX block, as exported from the Connect console (2026-09-10)
    # and re-imported through CreateContactFlow: Type
    # ConnectParticipantWithAgenticCX, AgentConfiguration {WorkspaceId,
    # ApplicationId, Alias, ContextVariables}, speech engine + audio filler,
    # Default on NextAction, Escalation as a Conditions entry, Error as
    # NoMatchingError, idle chat timeout as InputTimeLimitExceeded. Connect does
    # not validate the ids at import, so the runner substitutes the workspace and
    # application ids it deployed; the alias (an opaque ACXD id the SDK does not
    # expose) comes from ACXD_ALIAS_ID or stays a visible placeholder to pick in
    # the block's dropdown.
    context_variable_map = {
        item["name"]: item.get("fromContactAttribute") or f"$.Attributes.{item['name']}"
        for item in _context_variables(application)
    }
    speech_engine = str(application.get("speech_engine") or "agentic_voice").lower()
    agent_parameters: dict = {
        "AgentConfiguration": {
            "WorkspaceId": "{ACXD_WORKSPACE_ID}",
            "ApplicationId": "{ACXD_APPLICATION_ID}",
            "Alias": "{ACXD_ALIAS_ID}",
            "ContextVariables": context_variable_map,
        },
        "AudioFillerConfiguration": {
            "Enabled": True,
            "AudioType": "MELODY_CHIPPER_CHIME",
            "StartDelayInMilliseconds": 2500,
            "MinimumPlayDurationInMilliseconds": 3000,
            "ResponseDeliveryDelayInMilliseconds": 500,
        },
    }
    if speech_engine in {"agentic_voice", "agentic", "amazon_agentic_voice"}:
        agent_parameters["SpeechRecognitionConfiguration"] = {"SpeechRecognitionEngine": "AMAZON_AGENTIC_VOICE"}
    # The generator often wires the branches on purpose — Default → a
    # call-outcome logger, Escalation → business-hours check → set queue →
    # transfer. Keep such a target when it names an existing action and only
    # fill the branches that are missing (live: the fixed disconnect/queue
    # targets bypassed the logger and the hours check, leaving them
    # unreachable — the review's "escalation unreachable" finding).
    # Only an Agentic CX block the generator authored itself carries intended
    # branches; a Lex block being converted trails the legacy Lex-result Compare,
    # which the block's own Escalation branch makes redundant.
    candidate = actions[candidate_index] if candidate_index is not None else None
    authored_block = isinstance(candidate, dict) and (
        _action_type(candidate) == AGENTIC_CX_ACTION_TYPE
        or _action_id(candidate) in {AGENTIC_CX_PLACEHOLDER_ID, "AgenticCX"})
    authored = _authored_branches(candidate, actions) if authored_block else {}
    default_target = authored.get("Default") or disconnect_id
    escalation_target = authored.get("Escalation") or queue_id
    error_target = authored.get("Error") or fallback_id
    idle_target = authored.get("IdleChatTimeout") or disconnect_id
    agentic_action = {
        "Identifier": AGENTIC_CX_PLACEHOLDER_ID,
        "Type": AGENTIC_CX_ACTION_TYPE,
        "Parameters": agent_parameters,
        "Transitions": {
            "NextAction": default_target,
            "Errors": [
                {"ErrorType": "NoMatchingError", "NextAction": error_target},
                {"ErrorType": "NoMatchingCondition", "NextAction": default_target},
                {"ErrorType": "InputTimeLimitExceeded", "NextAction": idle_target},
            ],
            "Conditions": [
                {
                    "NextAction": escalation_target,
                    "Condition": {"Operator": "Equals", "Operands": ["Escalation"]},
                },
            ],
        },
    }
    # Earlier bundles carried a Compare block for the branches; it is redundant now.
    actions[:] = [item for item in actions if _action_id(item) != "AgenticCXBranch"]
    if candidate_index is not None:
        old_identifier = _action_id(actions[candidate_index])
        actions[candidate_index] = agentic_action
        if old_identifier and old_identifier != AGENTIC_CX_PLACEHOLDER_ID:
            document = _replace_exact(document, old_identifier, AGENTIC_CX_PLACEHOLDER_ID)
            actions = document["Actions"]
    else:
        actions.append(agentic_action)
        start_id = document.get("StartAction")
        start_action = next((item for item in actions if _action_id(item) == start_id), None)
        if start_action and _action_id(start_action) != AGENTIC_CX_PLACEHOLDER_ID:
            _transitions(start_action)["NextAction"] = AGENTIC_CX_PLACEHOLDER_ID
        else:
            document["StartAction"] = AGENTIC_CX_PLACEHOLDER_ID

    branches = {
        "Default": default_target,
        "Escalation": escalation_target,
        "Error": error_target,
        "IdleChatTimeout": idle_target,
    }
    metadata = document.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["acxdBinding"] = _build_binding(application, branches)
    document["Metadata"] = metadata

    context_names = {item["name"] for item in metadata["acxdBinding"]["contextVariables"]}
    # `$.AgenticCX.ContextVariables.<name>` only exists once the block has
    # RETURNED. Rewrite `$.Attributes.<name>` reads in the actions that run after
    # it; an action reachable from StartAction without passing the block (the
    # customer-lookup Compare, the pre-block greeting) keeps reading the contact
    # attribute — live, the whole-document rewrite left two such actions reading
    # a value that is empty at that point. Metadata is descriptive and is left as is.
    pre_block = _actions_before_block(document)
    rewritten_actions = []
    for action in document.get("Actions") or []:
        if _action_id(action) in pre_block:
            rewritten_actions.append(_rewrite_context_references(action, context_names, rewrite_attributes=False))
        else:
            rewritten_actions.append(_rewrite_context_references(
                action, context_names, rewrite_attributes=rewrite_attribute_context))
    document["Actions"] = rewritten_actions
    document["StartAction"] = _rewrite_context_references(
        document.get("StartAction"), context_names, rewrite_attributes=False)
    # The block's ContextVariables map is INPUT to the agent: its values are the
    # contact-side sources and must survive the rewrite above (which turns
    # `$.Attributes.<name>` reads elsewhere into `$.AgenticCX.ContextVariables.<name>`).
    for action in document.get("Actions") or []:
        if _action_type(action) == AGENTIC_CX_ACTION_TYPE:
            params = action.setdefault("Parameters", {})
            agent_cfg = params.setdefault("AgentConfiguration", {})
            agent_cfg["ContextVariables"] = {
                name: (source if not str(source).startswith("$.AgenticCX.") else f"$.Attributes.{name}")
                for name, source in context_variable_map.items()
            }

    # Speech ownership (ACXD target): the application speaks to the caller —
    # greeting, closing, "connecting you to an agent". The Contact Flow only
    # announces telephony states it alone knows (outside hours, queue full,
    # transfer error, app unavailable) and a recording/legal notice the spec
    # asked for. Live: the flow greeted before the block and the app's
    # WelcomeFlow greeted again with the same sentence.
    _strip_owned_speech(document, plan_data)

    # The former Lex Compare path is no longer part of the ACXD target.  Drop
    # unreachable fragments so the existing Connect linter sees a clean graph.
    reachable = _reachable_action_ids(document)
    document["Actions"] = [
        action for action in document.get("Actions") or []
        if _action_id(action) in reachable
    ]
    action_metadata = (document.get("Metadata") or {}).get("ActionMetadata")
    if isinstance(action_metadata, dict):
        live_ids = {_action_id(action) for action in document["Actions"]}
        for identifier in list(action_metadata):
            if identifier not in live_ids:
                action_metadata.pop(identifier, None)
    return document
