#!/usr/bin/env python3
"""Derive the ACXD contract from the official SDK's TypeScript definitions.

Why this exists
---------------
The first cut of ACXD support was reverse-engineered by probing the live API.
That produced something that deployed, but it was impoverished: 12 of the 21
real node types, and a single condition operator (``eq``) where the service
supports 14 — which silently rewrote business rules like "refund over $500"
into "refund equals $500".

The SDK ships the real model in ``dist-types/models/enums.d.ts``. Deriving the
contract from it means the generator, the validator, the JSON schemas and the
prompt all agree with the service, and a version bump is a re-run rather than
an archaeology project.

Usage
-----
    python backend/ecs/scripts/extract_acxd_contract.py \
        --sdk node_modules/amazon-connect-acxd-sdk \
        --out backend/ecs/src/schemas/acxd/contract.json

The output is committed so the backend has no Node dependency at runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Enums the flow/asset generators actually need. Everything else in the SDK
#: (roles, analytics, trails, permissions) is out of scope for asset authoring.
WANTED_ENUMS = (
    "FlowNodeType",
    "ConditionOperator",
    "OperandType",
    "FlowContextVariableType",
    "CapturedDataType",
    "MessageType",
    "ChoiceDisplayFormat",
    "ChoiceSource",
    "DataRequestFieldType",
    "WebhookMethod",
    "WebhookImplementation",
    "LanguageCode",
    "DeploymentEnvironment",
    "FallbackBehaviorType",
    "GenerativeModelType",
    "GenerativeJourneyToolType",
    "GenerativeJourneyModalityTriggerType",
    "GroundingType",
    "LoopType",
    "MultimodalType",
    "MultimodalActionType",
    "RedirectType",
    "KnowledgeBaseType",
    "GuardrailTrigger",
    "EnforcementAction",
    "DetectionMethod",
    "ContextVariableType",
)

#: Values the live service emits that this SDK version does not model yet.
#: Verified 2026-09-05: a `user_input` node's edges come back with
#: `left.type == "captured_flow"`, which is absent from OperandType. The
#: service is ahead of the SDK, so accept both rather than "correcting" it.
SERVICE_OBSERVED_EXTRAS = {
    "OperandType": ["captured_flow"],
}

#: Operators that take no right-hand operand.
UNARY_OPERATORS = ["exists", "not_exists"]

#: Node types that legitimately have no outgoing edge.
TERMINAL_NODE_TYPES = ["end"]

#: Typed edge sets the service materializes on its own. Any edge left without a
#: target routes live conversations to the application's Fallback flow, so the
#: generator must supply both halves. Shapes verified against the live API.
IMPLICIT_EDGES = {
    "user_input": [
        {"conditions": [{"left": {"type": "captured_flow"}, "operator": "exists"}]},
        {"conditions": [{"left": {"type": "captured_flow"}, "operator": "not_exists"}]},
    ],
    "data_request": [
        {"conditions": [{"left": {"type": "node_status"}, "operator": "eq",
                         "right": {"type": "constant", "value": "success"}}]},
        {"conditions": [{"left": {"type": "node_status"}, "operator": "eq",
                         "right": {"type": "constant", "value": "error"}}]},
    ],
}


def parse_enum(source: str, name: str) -> list[str]:
    """Pull the string members of `export declare const <name> = {...}`."""
    match = re.search(
        rf"^export declare const {re.escape(name)}\b.*?\{{(.*?)^\}};",
        source,
        re.S | re.M,
    )
    if not match:
        return []
    return sorted(set(re.findall(r'"([^"]+)"', match.group(1))))


def sync_flow_schema(schema_path: Path, enums: dict[str, list[str]]) -> list[str]:
    """Push contract enums into flow.schema.json so the two cannot drift.

    Returns a list of human-readable changes.
    """
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    defs = schema.setdefault("$defs", {})
    changes: list[str] = []

    node_type = defs.get("node", {}).get("properties", {}).get("type")
    if node_type is not None and enums.get("FlowNodeType"):
        before = node_type.get("enum") or []
        if sorted(before) != enums["FlowNodeType"]:
            node_type["enum"] = enums["FlowNodeType"]
            added = sorted(set(enums["FlowNodeType"]) - set(before))
            removed = sorted(set(before) - set(enums["FlowNodeType"]))
            changes.append(f"node.type enum: +{added} -{removed}")

    message_type = defs.get("message", {}).get("properties", {}).get("type")
    if message_type is not None and enums.get("MessageType"):
        if sorted(message_type.get("enum") or []) != enums["MessageType"]:
            message_type["enum"] = enums["MessageType"]
            changes.append("message.type enum synced")

    # Conditions were validated as bare objects, so a wrong operator or operand
    # type only surfaced as an API rejection at deploy time.
    if enums.get("ConditionOperator") and enums.get("OperandType"):
        operand = {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string", "enum": enums["OperandType"]},
                "name": {"type": "string"},
                "value": {},
                "id": {"type": "string"},
            },
        }
        defs["operand"] = operand
        defs["condition"] = {
            "type": "object",
            "required": ["left", "operator"],
            "properties": {
                "left": {"$ref": "#/$defs/operand"},
                "right": {"$ref": "#/$defs/operand"},
                "operator": {"type": "string", "enum": enums["ConditionOperator"]},
            },
        }
        child = defs.get("childNode")
        if child is not None:
            child.setdefault("properties", {})["conditions"] = {
                "type": "array", "items": {"$ref": "#/$defs/condition"},
            }
            # The SDK models an LLM-evaluated branch too; `additionalProperties:
            # false` was silently making it unrepresentable.
            child["properties"]["generativeCondition"] = {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
            }
            changes.append("childNode.conditions now structurally validated")

    # generative_journey is the node that makes the agent agentic: an LLM loop
    # with tool access. Its config is enum-driven, so generate it here too.
    if enums.get("GenerativeJourneyToolType") and enums.get("GenerativeModelType"):
        defs["generativeJourneyTool"] = {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string", "enum": enums["GenerativeJourneyToolType"]},
                "knowledgeBaseId": {"type": "string"},
                "flowId": {"type": "string"},
                "modalityId": {"type": "string"},
                "prompt": {"type": "string"},
                "scopeTags": {"type": "array", "items": {"type": "string"}},
                "dataRequest": {
                    "type": "object",
                    "properties": {
                        "dataRequestId": {"type": "string"},
                        "name": {"type": "string"},
                        "alwaysRetrigger": {"type": "boolean"},
                    },
                },
                "interimMessages": {"type": "array"},
            },
        }
        defs["generativeJourney"] = {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string", "minLength": 1},
                "modelType": {"type": "string", "enum": enums["GenerativeModelType"]},
                "tools": {"type": "array", "items": {"$ref": "#/$defs/generativeJourneyTool"}},
                "exitConditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"},
                                       "prompt": {"type": "string"}},
                    },
                },
                "enableZeroTurnMode": {"type": "boolean"},
                "maxSteps": {"type": "integer", "minimum": 1, "maximum": 50},
                "maxTokens": {"type": "integer", "minimum": 1},
                "temperature": {"type": "number", "minimum": 0, "maximum": 1},
                "timeout": {"type": "integer", "minimum": 1},
            },
        }
        changes.append("generativeJourney config schema generated")

    if changes:
        schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk", required=True, type=Path,
                    help="path to the installed amazon-connect-acxd-sdk package")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    enums_file = args.sdk / "dist-types" / "models" / "enums.d.ts"
    if not enums_file.is_file():
        print(f"error: {enums_file} not found — run `npm install "
              f"amazon-connect-acxd-sdk` first", file=sys.stderr)
        return 1
    source = enums_file.read_text(encoding="utf-8")

    pkg = json.loads((args.sdk / "package.json").read_text(encoding="utf-8"))

    enums: dict[str, list[str]] = {}
    missing: list[str] = []
    for name in WANTED_ENUMS:
        values = parse_enum(source, name)
        if values:
            enums[name] = values
        else:
            missing.append(name)

    for name, extras in SERVICE_OBSERVED_EXTRAS.items():
        if name in enums:
            enums[name] = sorted(set(enums[name]) | set(extras))

    contract = {
        "$comment": (
            "GENERATED by backend/ecs/scripts/extract_acxd_contract.py from the "
            "official SDK's TypeScript model. Do not hand-edit: re-run the "
            "script after bumping the SDK."
        ),
        "sdkPackage": pkg.get("name"),
        "sdkVersion": pkg.get("version"),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enums": enums,
        "serviceObservedExtras": SERVICE_OBSERVED_EXTRAS,
        "unaryOperators": UNARY_OPERATORS,
        "terminalNodeTypes": TERMINAL_NODE_TYPES,
        "implicitEdges": IMPLICIT_EDGES,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=False) + "\n",
                        encoding="utf-8")

    schema = args.out.parent / "flow.schema.json"
    if schema.is_file():
        for change in sync_flow_schema(schema, enums):
            print(f"  schema: {change}")

    print(f"wrote {args.out} from {pkg.get('name')}@{pkg.get('version')}")
    for name, values in enums.items():
        print(f"  {name:28} {len(values)} values")
    if missing:
        print(f"  WARNING: not found in this SDK version: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
