"""Prompt fragments generated from the SDK-derived ACXD contract.

Hand-written prompt catalogues rot: the model was previously told about 12 node
types and one comparison operator, so that is what it produced. Building these
strings from ``contract.json`` means the prompt, the validator and the service
always describe the same system.
"""

from __future__ import annotations

from tools.acxd_contract import (
    CONDITION_OPERATORS,
    IMPLICIT_EDGES,
    UNARY_OPERATORS,
    enum,
)
from tools.validate_acxd_flow import (
    GENERATIVE_NODE_TYPES,
    SUPPORTED_NODE_TYPES,
)

#: What each node type is for, in the words a designer would use. Only types in
#: SUPPORTED_NODE_TYPES are offered to the model; the rest are filtered out so
#: the prompt cannot advertise something the validator rejects.
_NODE_PURPOSE = {
    "start": "entry point — exactly one per flow",
    "end": "terminal. Returns control to the caller (the Amazon Connect "
           "contact flow), it does not hang up by itself",
    "basic": "say something and move on (fixed wording)",
    "user_input": "ask for a value and capture it into a slot",
    "user_choice": "offer a fixed set of options and capture the pick",
    "choice": "branch on data already captured. Every branch MUST carry "
              "`conditions` — the platform accepts and BUILDS a conditionless "
              "choice (verified live) and then cannot route",
    "split": "branch by percentage, for A/B experiments",
    "data_request": "call a configured API/webhook and branch on success/error",
    "knowledge_base": "answer from indexed documents (grounded, cites sources)",
    "generative_text": "one LLM-written reply, still inside a fixed structure",
    "generative_task": "LLM completes a bounded task, then returns to the flow",
    "generative_journey": "an LLM-run conversation loop with tool access "
                          "(knowledge bases, data requests, other flows). This "
                          "is the agentic node: use it when the path cannot be "
                          "drawn in advance",
    "escalate": "hand off to a human agent",
    "redirect": "jump to another flow",
    "wait": "pause for a set duration",
    "note": "designer annotation, no runtime effect",
    "define": "set a context variable",
    "transform": "compute a new value from existing ones",
    # DO NOT USE. Verified live 2026-09-08: the canvas palette has no
    # intent-capture node, the server silently DROPS metadata.intentCapture
    # (there is no config object), and a deployed flow using
    # intent_capture + choice failed on the FIRST customer utterance with
    # "We encountered an issue. Please try again soon.". Intent routing is the
    # generative_journey's job — that is the product's model.
    "intent_capture": "DO NOT USE — not a real node; route intents with "
                      "generative_journey instead",
    "loop": "repeat a section a bounded number of times (e.g. retry twice)",
}


def node_type_catalog() -> str:
    """Markdown list of node types this system can generate and validate."""
    lines = []
    for name in sorted(SUPPORTED_NODE_TYPES):
        purpose = _NODE_PURPOSE.get(name, "")
        kind = "generative" if name in GENERATIVE_NODE_TYPES else "deterministic"
        lines.append(f"- `{name}` ({kind}) — {purpose}")
    return "\n".join(lines)


def operator_catalog() -> str:
    """Markdown list of condition operators, flagging the unary ones."""
    ordered = sorted(CONDITION_OPERATORS)
    unary = ", ".join(f"`{o}`" for o in sorted(UNARY_OPERATORS))
    listed = ", ".join(f"`{o}`" for o in ordered)
    return (
        f"Operators ({len(ordered)}): {listed}.\n"
        f"{unary} are unary — they take `left` and no `right`. "
        "Every other operator needs both. Use the operator the rule actually "
        "means: a policy like \"over $500 needs approval\" is `gt`, not `eq`."
    )


def implicit_edge_catalog() -> str:
    """Explain the edges the service adds on its own."""
    if not IMPLICIT_EDGES:
        return ""
    lines = [
        "These node types always have the following edges, whether or not you "
        "write them. Every one needs a `nodeId`: an edge without a target "
        "sends the live conversation to the application's Fallback flow.",
        "",
    ]
    for node_type, edges in sorted(IMPLICIT_EDGES.items()):
        described = []
        for edge in edges:
            cond = (edge.get("conditions") or [{}])[0]
            left = (cond.get("left") or {}).get("type", "?")
            op = cond.get("operator", "?")
            right = (cond.get("right") or {}).get("value")
            described.append(f"`{left}` {op}" + (f" `{right}`" if right is not None else ""))
        lines.append(f"- `{node_type}`: {' | '.join(described)}")
    return "\n".join(lines)


def generative_journey_guidance() -> str:
    """How to configure the agentic node, with the real enum values."""
    # Nova Micro is a GUARDRAILS-only model: it must not be offered as a
    # generative node's modelType. Everything else in the enum is fair game.
    _GUARDRAIL_ONLY_MODELS = {"amazon-nova-micro", "amazon.nova-micro",
                              "amazon-nova-micro-1", "nova-micro"}
    _generative_models = [
        m for m in sorted(enum("GenerativeModelType"))
        if m.lower().replace("_", "-") not in _GUARDRAIL_ONLY_MODELS
    ]
    models = ", ".join(f"`{m}`" for m in _generative_models)
    tools = ", ".join(f"`{t}`" for t in sorted(enum("GenerativeJourneyToolType")))
    return (
        "`generative_journey` config goes under "
        "`metadata.generativeJourney`:\n"
        "- `prompt` (required): the agent's instructions for this stretch of "
        "the conversation. Write it in the project's language.\n"
        f"- `modelType`: one of {models}.\n"
        "- `tools`: use ONLY these two shapes.\n"
        "  * `{\"type\": \"flow\", \"flowId\": \"<helper flow>\"}` for every "
        "data request. A journey CANNOT call a data request directly: the "
        "service drops `dataRequest.dataRequestId` on save. Each data request "
        "is wrapped in a generated helper flow "
        "(`start -> data_request -> basic message -> end`, emitting exactly "
        "`{<dataRequestId>.toolResponse:NLX.Variable}`) and attached as an "
        "a `flow` tool. Do NOT use `mcpFlow`: it saves and builds cleanly but "
        "fails on invocation with {\"error\": \"Unknown tool type\"} "
        "(measured in the Canvas debugger).\n"
        "  * `{\"type\": \"knowledgeBase\", \"knowledgeBaseId\": \"{KB:<name>}\", "
        "\"scopeTags\": []}` for knowledge lookups.\n"
        "  NEVER emit a `dataRequest` tool (the service drops its id) and NEVER "
        "an `mcpFlow` tool (runtime: \"Unknown tool type\"). Every id MUST be "
        "one this bundle actually creates.\n"
        "- `exitConditions`: named prompts describing when the loop is done, "
        "so the conversation returns to the deterministic flow.\n"
        "- `maxSteps` bounds the loop. Keep it modest (5-10) for a PoC.\n"
        "ROUTING IS THE JOURNEY'S JOB. The product's model is one agentic "
        "node that decides which tool answers the customer — not a classifier "
        "node followed by branches. A normal operation flow is: `start` -> "
        "`basic` greeting -> `generative_journey` (tools attached, with a "
        "`humanHandoff` exit condition) -> `escalate` on that exit / `end` "
        "otherwise. Use deterministic nodes for the stretches where a rule "
        "must hold exactly (identity checks, consent, money gates), and give "
        "the journey a prompt that NAMES each attached tool and says when to "
        "use it — attaching a tool is necessary but not sufficient.\n"
        "Do not add `modelType` to any node other than a generative one; "
        "`generative_text` has no `modelType` field."
    )
