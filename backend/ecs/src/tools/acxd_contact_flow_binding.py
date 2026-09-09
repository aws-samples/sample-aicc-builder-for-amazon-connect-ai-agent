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

#: Set when AWS publishes the Flow-Language action type and parameter shape.
AGENTIC_CX_ACTION_TYPE: str | None = None

AGENTIC_CX_PLACEHOLDER_ID = "AgenticCXPlaceholder"
AGENTIC_CX_PLACEHOLDER_TEXT = (
    ">>> AGENTIC CX PLACEHOLDER <<< Replace this action in the Connect flow "
    "designer with the Agentic CX block and keep Metadata.acxdBinding branches."
)
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

    disconnect_id = _find_or_add_action(
        actions,
        "DisconnectParticipant",
        "AgenticCXDisconnect",
        {"Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
    )
    fallback_id = "AgenticCXFallbackMessage"
    if not any(_action_id(item) == fallback_id for item in actions):
        actions.append({
            "Identifier": fallback_id,
            "Type": "MessageParticipant",
            "Parameters": {"Text": "We are sorry, but the assistant is unavailable."},
            "Transitions": {"NextAction": disconnect_id},
        })

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
        ),
        None,
    )
    if candidate_index is None:
        candidate_index = next(
            (index for index, action in enumerate(actions)
             if _action_type(action) in {"ConnectParticipantWithLexBot", "GetParticipantInput"}),
            None,
        )

    agentic_action = {
        "Identifier": AGENTIC_CX_PLACEHOLDER_ID,
        "Type": AGENTIC_CX_ACTION_TYPE or "MessageParticipant",
        "Parameters": {"Text": AGENTIC_CX_PLACEHOLDER_TEXT},
        "Transitions": {
            "NextAction": disconnect_id,
            "Errors": [{"ErrorType": "NoMatchingError", "NextAction": fallback_id}],
            "Conditions": [
                {
                    "Condition": {
                        "Operator": "Equals",
                        "Operands": ["$.AgenticCX.Branch", "Escalation"],
                    },
                    "NextAction": queue_id,
                },
                {
                    "Condition": {
                        "Operator": "Equals",
                        "Operands": ["$.AgenticCX.Branch", "IdleChatTimeout"],
                    },
                    "NextAction": disconnect_id,
                },
            ],
        },
    }
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
        "Default": disconnect_id,
        "Escalation": queue_id,
        "Error": fallback_id,
        "IdleChatTimeout": disconnect_id,
    }
    metadata = document.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["acxdBinding"] = _build_binding(application, branches)
    document["Metadata"] = metadata

    context_names = {item["name"] for item in metadata["acxdBinding"]["contextVariables"]}
    document = _rewrite_context_references(
        document,
        context_names,
        rewrite_attributes=rewrite_attribute_context,
    )

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
