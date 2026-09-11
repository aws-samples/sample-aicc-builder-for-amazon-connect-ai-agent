"""Canonicalize an ACXD flow document onto the SDK/service contract.

Why this exists (live finding, 2026-09-11): the flow generator is an LLM and
each call chose its own encoding for the same thing — `metadata.userInput.
slotName`, a top-level `slot`, `metadata.define.assignments[]`, `{{var}}` vs
`{var:NLX.Variable}`, `found` typed text in one flow and boolean in the next.
The JSON schema let it through (node `metadata` was open), and the SDK's
schema-based serializer then DROPPED every key it does not know: a GetFlow of
a deployed Harbor Bank flow returned `user_input` nodes with `messages: []`
and `metadata: {}` and a `define` node without its `name`. The flows deployed,
and asked the caller nothing.

This module is the single place that decides representation. It is applied
before validation in the generator, after every patch, and whenever a bundle
is loaded (packaging, D9, runner), so flows generated before it exist are
repaired too. Every rewrite is mechanical and recorded; anything that would
change behaviour is reported as a problem instead of guessed.

Contract sources: amazon-connect-acxd-sdk 0.1.0 models (FlowNode,
FlowNodeMetadata, DefineConfig, ChoiceConfig, FlowStateModification), the
Amazon Connect Customer admin guide (User input = intent capture, User choice
= value/slot capture, messages live on the node, placeholders are `{…}`), and
a CreateFlow/GetFlow probe of what the service persists.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: FlowNode members (SDK). Anything else on a node is not sent to the service.
SDK_NODE_KEYS = frozenset({
    "nodeId", "type", "childNodes", "dataRequests", "messages", "modalities",
    "canvasMetadata", "metadata",
})

#: FlowNodeMetadata members (SDK). Anything else under `metadata` is dropped
#: by the SDK serializer, i.e. silently lost at deploy time.
SDK_METADATA_KEYS = frozenset({
    "payload", "nodePayload", "flowId", "code", "choice", "define",
    "generativeText", "generativeJourney", "agenticTask", "knowledgeBase",
    "loop", "multimodal", "note", "redirect", "transform", "tags", "name",
    "interimMessages", "voiceSettings", "stateModifications", "sendContext",
    "timeout",
})

#: Built-in slot types a flow may attach without a custom slot type asset.
BUILTIN_SLOT_TYPES = frozenset({
    "text", "string", "number", "integer", "int", "boolean", "bool",
    "date", "datetime", "time", "email", "phone",
})

#: Canonical placeholder syntax inside `messages[].body` (NLX runtime).
SLOT_REF = "{%s:NLX.Slot}"
VARIABLE_REF = "{%s:NLX.Variable}"

_DOUBLE_BRACE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_INCREMENT = re.compile(r"^\s*\{?\{?\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}?\}?\s*([+-])\s*1\s*$")
_SLOT_NAME = re.compile(r"^[A-Za-z]{3,30}$")
_SINGLE_BRACE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_.]*)\}(?!\})")
_BOOL_STRINGS = {"true": True, "false": False}


@dataclass
class Canonicalization:
    flow: dict
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _v4_like_id(seed: str) -> str:
    """Deterministic UUID that satisfies the service's v4 regex."""
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"


def _as_message(item: Any) -> Optional[dict]:
    if isinstance(item, str):
        body = item.strip()
        return {"type": "text", "body": body} if body else None
    if isinstance(item, dict):
        body = item.get("body") or item.get("text") or item.get("content")
        if not isinstance(body, str) or not body.strip():
            return None
        msg_type = item.get("type") if item.get("type") in ("text", "ssml") else "text"
        out = {"type": msg_type, "body": body}
        if item.get("messageId"):
            out["messageId"] = item["messageId"]
        if isinstance(item.get("skipTranslation"), bool):
            out["skipTranslation"] = item["skipTranslation"]
        return out
    return None


def _operand(value: Any) -> dict:
    """Coerce a raw value into an Operand; pass real operands through.

    `{"type": "expression", "value": "x + 1"}` is not an OperandType — the
    contract expresses that as the variable operand with an increment /
    decrement modification.
    """
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        if value["type"] == "expression" and isinstance(value.get("value"), str):
            match = _INCREMENT.match(value["value"])
            if match:
                return {"type": "variable", "name": match.group(1),
                        "modification": "increment" if match.group(2) == "+" else "decrement"}
        return value
    return {"type": "constant", "value": value}


def _slot_alias(name: str) -> str:
    """Attached slot names are alphabetic 3-30 chars; `cardLast4` attaches as `cardLast`."""
    letters = re.sub(r"[^A-Za-z]", "", name or "")
    if len(letters) < 3:
        letters = f"{letters}Value" if letters else "CustomValue"
    return letters[:30]


def _slot_from_node(node: dict, meta: dict) -> tuple[Optional[str], Optional[str], bool]:
    """(slot name, slot type id, had_options) from every encoding seen live."""
    name: Optional[str] = None
    type_id: Optional[str] = None
    had_options = False
    candidates: list[Any] = [node.get("slot")]
    slots = node.get("slots")
    if isinstance(slots, list) and slots:
        candidates.append(slots[0])
    for container_key in ("userInput", "userChoice", "intentCapture"):
        container = meta.get(container_key)
        if isinstance(container, dict):
            candidates.append(container.get("slotName") or container.get("slot"))
            if container.get("options") or container.get("choices"):
                had_options = True
    candidates.append(meta.get("slotName"))
    candidates.append(meta.get("slot"))
    if meta.get("choices"):
        had_options = True
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
        if isinstance(candidate, dict):
            name = candidate.get("name") or candidate.get("slotName")
            type_id = candidate.get("slotTypeId") or candidate.get("type") or candidate.get("slotType")
            if name:
                break
    return name, type_id, had_options


def canonicalize_flow(flow: Any) -> Canonicalization:
    """Return the flow rewritten onto the SDK contract, with a change log."""
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), dict):
        return Canonicalization(flow)
    flow = copy.deepcopy(flow)
    result = Canonicalization(flow)
    nodes: dict = flow["nodes"]

    attached: dict[str, dict] = {}
    slot_types = flow.get("slotTypes")
    if not isinstance(slot_types, list):
        slot_types = []
        flow["slotTypes"] = slot_types
    for entry in slot_types:
        if isinstance(entry, dict) and entry.get("name"):
            attached[str(entry["name"])] = entry

    variables: dict[str, dict] = {}
    context_vars = flow.get("contextVariables")
    if not isinstance(context_vars, list):
        context_vars = []
        flow["contextVariables"] = context_vars
    for entry in context_vars:
        if isinstance(entry, dict) and entry.get("name"):
            variables[str(entry["name"])] = entry

    extra_nodes: dict[str, dict] = {}
    for node_id, node in list(nodes.items()):
        if not isinstance(node, dict):
            continue
        label = f"{node.get('type', '?')}[{str(node_id)[:8]}]"
        meta = node.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        node["metadata"] = meta

        # 1. Messages live on the node, never inside metadata containers.
        messages: list[dict] = []
        seen_bodies: set[str] = set()

        def add_messages(items: Any) -> None:
            for item in items if isinstance(items, list) else [items]:
                msg = _as_message(item)
                if msg and msg["body"] not in seen_bodies:
                    seen_bodies.add(msg["body"])
                    messages.append(msg)

        add_messages(node.get("messages") or [])
        for key in ("userInput", "userChoice", "basic", "escalate", "end", "messages", "intentCapture"):
            container = meta.get(key)
            if isinstance(container, dict) and container.get("messages"):
                add_messages(container["messages"])
                result.changes.append(f"{label}: moved metadata.{key}.messages to messages[]")
            elif key == "messages" and isinstance(container, list):
                add_messages(container)
                result.changes.append(f"{label}: moved metadata.messages to messages[]")
        if messages or "messages" in node:
            node["messages"] = messages

        # 2. Slot capture is a user_choice with metadata.choice (slotType source).
        slot_name, slot_type_id, had_options = _slot_from_node(node, meta)
        if slot_name and node.get("type") in ("user_input", "user_choice"):
            if not _SLOT_NAME.match(slot_name):
                alias = _slot_alias(slot_name)
                result.changes.append(f"{label}: slot name {slot_name!r} → {alias!r} (alphabetic 3-30)")
                slot_name = alias
            type_ok = bool(slot_type_id) and (
                _SLOT_NAME.match(str(slot_type_id)) or str(slot_type_id).lower() in BUILTIN_SLOT_TYPES)
            if not type_ok:
                slot_type_id = str(attached.get(slot_name, {}).get("type") or slot_name)
            if node["type"] == "user_input":
                node["type"] = "user_choice"
                result.changes.append(
                    f"{label}: user_input capturing slot {slot_name!r} → user_choice "
                    "(user_input is intent capture; user_choice captures a value)")
            choice = meta.get("choice") if isinstance(meta.get("choice"), dict) else {}
            if not choice.get("source"):
                choice = {"source": "slotType", "slotTypeId": slot_type_id, **({"showChoices": True} if had_options else {})}
                meta["choice"] = choice
                result.changes.append(f"{label}: slot {slot_name!r} → metadata.choice(source=slotType, slotTypeId={slot_type_id!r})")
            if slot_name not in attached:
                entry = {"name": slot_name, "type": slot_type_id}
                slot_types.append(entry)
                attached[slot_name] = entry
                result.changes.append(f"{label}: attached slot {slot_name!r} ({slot_type_id}) to the flow")
        for key in ("userInput", "userChoice", "choices", "slotName", "slot", "intentCapture"):
            if key in meta:
                meta.pop(key, None)
        for key in ("slot", "slots"):
            if key in node:
                node.pop(key, None)
        if "maxRetries" in meta:
            meta.pop("maxRetries", None)
            result.problems.append(
                f"{label}: `maxRetries` is not a node setting — retries are a loop node or a "
                "define/choice counter; the limit was dropped")

        # 3. define: {name, value: Operand}; extra assignments become chained defines.
        define = meta.get("define")
        if node.get("type") == "define" and isinstance(define, dict):
            assignments: list[tuple[str, Any]] = []
            for item in define.get("assignments") or define.get("variables") or []:
                if isinstance(item, dict):
                    var = item.get("variable") or item.get("name") or item.get("variableName")
                    if var:
                        assignments.append((str(var), item.get("value")))
            single_name = define.get("name") or define.get("variableName") or define.get("variable")
            if single_name:
                assignments.insert(0, (str(single_name), define.get("value")))
            if assignments:
                first_name, first_value = assignments[0]
                new_define = {"name": first_name, "value": _operand(first_value)}
                if define.get("schema") is not None:
                    new_define["schema"] = define["schema"]
                if new_define != define:
                    result.changes.append(f"{label}: define → {{name: {first_name!r}, value: Operand}}")
                meta["define"] = new_define
                if len(assignments) > 1:
                    # Chain one define node per extra assignment, in order.
                    tail_children = node.get("childNodes") or []
                    previous = node
                    for index, (var, value) in enumerate(assignments[1:], start=1):
                        new_id = _v4_like_id(f"{node_id}#define#{index}")
                        extra_nodes[new_id] = {
                            "nodeId": new_id, "type": "define",
                            "metadata": {"define": {"name": var, "value": _operand(value)}},
                        }
                        previous["childNodes"] = [{"nodeId": new_id}]
                        previous = extra_nodes[new_id]
                    previous["childNodes"] = tail_children
                    result.changes.append(
                        f"{label}: {len(assignments) - 1} extra assignment(s) → chained define nodes")
            else:
                result.problems.append(f"{label}: define node sets no variable (no name/value)")

        # 4. escalate/end context updates → stateModifications (context, set).
        updates = meta.pop("contextUpdates", None) or meta.pop("contextVariables", None)
        if isinstance(updates, list) and updates:
            mods = meta.get("stateModifications") if isinstance(meta.get("stateModifications"), list) else []
            for item in updates:
                if isinstance(item, dict) and item.get("name"):
                    mods.append({"type": "context", "name": item["name"], "modification": "set",
                                 "value": _operand(item.get("value"))})
            meta["stateModifications"] = mods
            result.changes.append(f"{label}: contextUpdates → metadata.stateModifications (context/set)")

        # 5. data_request: the request id lives in node.dataRequests[].
        dr_meta = meta.pop("dataRequest", None)
        requests = node.get("dataRequests")
        if not isinstance(requests, list):
            requests = []
        canonical_requests: list[dict] = []
        seen_ids: set[str] = set()
        for item in requests + ([dr_meta] if isinstance(dr_meta, dict) else []):
            request_id = item if isinstance(item, str) else (item.get("dataRequestId") if isinstance(item, dict) else None)
            if request_id and request_id not in seen_ids:
                seen_ids.add(request_id)
                entry = {"dataRequestId": request_id}
                if isinstance(item, dict):
                    for key in ("urlParams", "headers", "payload", "alwaysRetrigger", "name"):
                        if item.get(key) is not None:
                            entry[key] = item[key]
                canonical_requests.append(entry)
        if node.get("type") == "data_request" or canonical_requests:
            if canonical_requests != requests:
                result.changes.append(f"{label}: dataRequests normalized to [{{dataRequestId}}]")
            node["dataRequests"] = canonical_requests

        # 6. `escalate` is not a metadata field (schema says so); messages already moved.
        meta.pop("escalate", None)

        # 7. Drop what the SDK would drop anyway — but say so.
        for key in [k for k in meta if k not in SDK_METADATA_KEYS]:
            meta.pop(key, None)
            result.changes.append(f"{label}: dropped non-SDK metadata.{key}")
        for key in [k for k in node if k not in SDK_NODE_KEYS]:
            node.pop(key, None)
            result.changes.append(f"{label}: dropped non-SDK node key {key!r}")
        if not meta:
            node.pop("metadata", None)

    nodes.update(extra_nodes)

    # 8. Placeholders: one syntax. Slots → {x:NLX.Slot}, variables → {x:NLX.Variable}.
    known_slots = set(attached)
    known_vars = set(variables)
    data_request_ids = {
        r.get("dataRequestId")
        for n in nodes.values() if isinstance(n, dict)
        for r in (n.get("dataRequests") or []) if isinstance(r, dict)
    }

    def resolve(name: str) -> Optional[str]:
        if name in known_slots:
            return SLOT_REF % name
        if name in known_vars or name.split(".")[0] in data_request_ids or name.split(".")[0] in known_vars:
            return VARIABLE_REF % name
        return None

    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for msg in node.get("messages") or []:
            body = msg.get("body")
            if not isinstance(body, str) or "{" not in body:
                continue
            new_body = body

            def repl(match: re.Match) -> str:
                name = match.group(1)
                canonical = resolve(name)
                return canonical if canonical else match.group(0)

            new_body = _DOUBLE_BRACE.sub(repl, new_body)
            new_body = _SINGLE_BRACE.sub(
                lambda m: repl(m) if not m.group(1).startswith(("KB:", "System.")) else m.group(0), new_body)
            if new_body != body:
                msg["body"] = new_body
                result.changes.append(f"{node.get('type')}[{str(node_id)[:8]}]: placeholder syntax → NLX")
            for leftover in _DOUBLE_BRACE.findall(new_body):
                result.problems.append(
                    f"{node.get('type')}[{str(node_id)[:8]}]: placeholder {{{{{leftover}}}}} names no "
                    "attached slot, context variable or data request output")

    # 9. Booleans: a variable compared only with "true"/"false" is a boolean.
    comparisons: dict[str, list[dict]] = {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for child in node.get("childNodes") or []:
            for cond in (child.get("conditions") or []) if isinstance(child, dict) else []:
                left, right = cond.get("left") or {}, cond.get("right") or {}
                if left.get("type") in ("variable", "context") and right.get("type") == "constant":
                    comparisons.setdefault(str(left.get("name")), []).append(right)
    for var_name, rights in comparisons.items():
        entry = variables.get(var_name)
        string_bools = [r for r in rights if isinstance(r.get("value"), str) and r["value"].strip().lower() in _BOOL_STRINGS]
        if not string_bools:
            continue
        declared_boolean = bool(entry) and entry.get("type") == "boolean"
        all_boolean_like = all(
            isinstance(r.get("value"), bool) or (isinstance(r.get("value"), str) and r["value"].strip().lower() in _BOOL_STRINGS)
            for r in rights)
        if declared_boolean or all_boolean_like:
            for r in string_bools:
                r["value"] = _BOOL_STRINGS[r["value"].strip().lower()]
            if entry and entry.get("type") != "boolean":
                entry["type"] = "boolean"
                result.changes.append(f"contextVariables[{var_name}]: text → boolean (compared only with true/false)")
            result.changes.append(f"conditions on {var_name!r}: \"true\"/\"false\" strings → booleans")
    for node in nodes.values():
        if isinstance(node, dict) and node.get("type") == "define":
            define = (node.get("metadata") or {}).get("define") or {}
            value = define.get("value") or {}
            target = variables.get(str(define.get("name")))
            if target and target.get("type") == "boolean" and isinstance(value.get("value"), str) \
                    and value["value"].strip().lower() in _BOOL_STRINGS:
                value["value"] = _BOOL_STRINGS[value["value"].strip().lower()]
                result.changes.append(f"define {define.get('name')!r}: boolean constant typed")

    return result


def canonicalize_flow_document(flow: Any) -> Any:
    """Convenience for callers that only want the rewritten document."""
    return canonicalize_flow(flow).flow
