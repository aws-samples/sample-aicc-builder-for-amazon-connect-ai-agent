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
    "end": "terminal. Exits the application and returns control to the Amazon "
           "Connect contact flow, which ENDS the conversation — only use it "
           "after a goodbye, never after answering a question",
    "basic": "say something and move on (fixed wording)",
    "user_input": "listen for what the customer wants and let the application "
                  "recognize which attached flow matches. Pair it with a "
                  "`redirect` to `{System.capturedFlow:NLX.System}` on the "
                  "`captured_flow exists` edge — that IS intent routing",
    "user_choice": "ask for ONE value and capture it into an attached slot "
                   "(order number, a category, yes/no)",
    "choice": "branch on data already captured. Every branch MUST carry "
              "`conditions` — the platform accepts and BUILDS a conditionless "
              "choice (verified live) and then cannot route",
    "split": "branch by percentage, for A/B experiments",
    "data_request": "call a configured API/webhook and branch on success/error",
    "knowledge_base": "answer from indexed documents (grounded, cites sources)",
    "generative_text": "one LLM-written reply, still inside a fixed structure",
    "generative_task": "LLM completes a bounded task, then returns to the flow",
    "generative_journey": "an LLM-run conversation loop with tool access "
                          "(knowledge bases, data requests, other flows). Use it "
                          "only for a confirmed stretch of conversation that "
                          "cannot be drawn in advance — NEVER for intent routing",
    "escalate": "hand off to a human agent. TERMINAL: it must have no outgoing "
                "edge (an `end` after it made Connect report Success instead of "
                "Escalation, so the caller was never transferred)",
    "redirect": "jump to another flow",
    "wait": "pause for a set duration",
    "note": "designer annotation, no runtime effect",
    "define": "set a context variable",
    "transform": "compute a new value from existing ones",
    # DO NOT USE. Verified live 2026-09-08: the canvas palette has no
    # intent-capture node and the server silently DROPS metadata.intentCapture
    # (there is no config object), so a deployed flow using
    # intent_capture + choice failed on the FIRST customer utterance with
    # "We encountered an issue. Please try again soon.". Intent routing is
    # `user_input` + `redirect` (verified live 2026-09-12).
    "intent_capture": "DO NOT USE — not a real node; route intents with "
                      "`user_input` + a `redirect` to "
                      "`{System.capturedFlow:NLX.System}` instead",
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
        "- `tools`: use ONLY these shapes.\n"
        "  * `{\"type\": \"dataRequest\", \"dataRequest\": {\"dataRequestId\": \"<id>\", "
        "\"payload\": {\"<field>\": \"{<slot>:NLX.Slot}\", ...}}}` for a Data Request the "
        "journey calls itself (live 2026-09-21: stored as sent, built, and invoked "
        "at runtime — the runtime composes the call's arguments from the "
        "conversation against the request schema, so the payload only documents "
        "the slot mapping and may be `{}`). Add `\"interimMessages\": [{\"text\": "
        "\"...\", \"delay\": 2}]` for a call that takes a moment.\n"
        "  * `{\"type\": \"knowledgeBase\", \"knowledgeBaseId\": \"{KB:<name>}\", "
        "\"scopeTags\": []}` for knowledge lookups.\n"
        "  * `{\"type\": \"flow\", \"flowId\": \"<flow>\"}` only when the plan says so.\n"
        "  NEVER emit an `mcpFlow` tool (runtime: \"Unknown tool type\"). Every id "
        "MUST be one this bundle actually creates.\n"
        "- `exitConditions`: named prompts describing when the journey hands the "
        "conversation back. An exit-condition edge carries "
        "`System.gjConditionIndex eq <i>` (i = the condition's position); timeout "
        "and failure edges carry `node_status`. The sentence a journey composes on "
        "the turn it exits is NOT delivered (live 2026-09-17), so the node an exit "
        "edge leads to must speak or redirect to the follow-up flow.\n"
        "- A journey WITH a dataRequest tool carries the whole operation: it "
        "collects every value (strict-format values included — it reads them "
        "back and re-asks on a wrong shape), calls the request, and announces "
        "the result itself while it still holds the turn (live 2026-09-21). Give "
        "it `exitConditions` `done` (index 0: the request is complete and the "
        "customer needs nothing else → the follow-up redirect) plus the hand-offs "
        "the plan names; set `dataCapture.exitEnabled: false` and every capture "
        "`required: false`; put NO data_request node, NO read-back `basic` and NO "
        "result `generative_text` after it — the contract removes duplicates and "
        "refuses a second call of the same request.\n"
        "- A journey WITHOUT a dataRequest tool only collects: `dataCapture` "
        "`{\"data\": [{\"name\": \"<slot>\", \"type\": \"slot\", \"required\": true, "
        "\"schema\": {...}}], \"exitEnabled\": true}` makes it fill the flow's "
        "attached slots as it talks; when every required value is captured the "
        "journey ENDS and evaluates its edges — neither `System.gjConditionIndex` "
        "nor `node_status eq success` is set for that exit (live 2026-09-17), so "
        "the captured edge tests the slots themselves: ONE edge, FIRST in "
        "`childNodes`, with `slot <name> exists` for each captured slot. Without "
        "it the runtime logs Error NoMessages and the caller lands in the fallback "
        "flow. The extractor that fills `dataCapture` is a separate model call and "
        "has stored a wrong phone number and empty results (live 2026-09-21): a "
        "value the backend will store belongs in a journey tool call, not in a "
        "captured slot.\n"
        "- `maxSteps` bounds the loop: 5-10 for a collecting journey, 12-16 for one "
        "that calls tools.\n"
        "A JOURNEY DOES NOT ROUTE INTENTS. Intent routing is `user_input` + a "
        "`redirect` to `{System.capturedFlow:NLX.System}`; a welcome flow that "
        "classified intent with a journey recognized nothing and the application "
        "never routed a single customer utterance (live, 2026-09-12). Use a "
        "journey ONLY inside an operation flow, and give its prompt the NAME of "
        "each attached tool and when to use it — attaching a tool is necessary "
        "but not sufficient.\n"
        "Do not add `modelType` to any node other than a generative one; "
        "`generative_text` has no `modelType` field."
    )


def runtime_contract_rules() -> str:
    """The live-verified runtime contract, in the order a flow is authored.

    Every line here was measured against a deployed application over Connect
    chat (a sandbox Connect Customer account, 2026-09-12), not read from documentation: the
    platform BUILDS the wrong shapes without complaint and then answers nothing,
    so these are not style preferences.
    """
    return (
        "### 1. Routing descriptor (how a customer reaches this flow)\n"
        "An operation flow is `untrained: false` and its `aiDescription` is the "
        "ONLY thing the application matches an utterance against — there are no "
        "training utterances. Write it as \"Use this flow when the user wants "
        "to <do the thing>...\", ASCII, from the CUSTOMER's point of view, "
        "listing the words a caller would actually say. It must be clearly "
        "distinct from every other flow's, and must NOT describe mechanics "
        "(nodes, data requests, slots) — a mechanical description matches "
        "nothing. System flows are `untrained: true` and say they are not "
        "routing targets.\n"
        "\n"
        "### 2. Attached slots\n"
        "`slotTypes: [{\"name\": \"orderNumber\", \"type\": \"<slot type>\", "
        "\"sensitive\": false, \"regex\": \"...\"}]`. `type` is EITHER a custom "
        "slot type id this bundle creates OR an `NLX.` built-in "
        "(`NLX.AlphaNumeric`, `NLX.Number`, `NLX.PhoneNumber`, `NLX.Text`, "
        "`NLX.Date`, `NLX.Time`, `NLX.Email`, `NLX.Name`, `NLX.Url`, "
        "`NLX.Ordinal`, `NLX.Duration`). NEVER `text` / `number` / `boolean`: "
        "those silently disable flow recognition for the WHOLE application, so "
        "one wrong slot stops every flow from being matched. A value set "
        "(product categories, service types) is a custom slot type; an open "
        "value (order number, phone number, free text) is a built-in plus "
        "`regex` — a custom slot type built from ONE sample value deploys as a "
        "one-item menu and is auto-selected without asking the customer. There "
        "is no boolean built-in: yes/no uses the bundled `yesNo` slot type.\n"
        "\n"
        "### 3. Capturing a value\n"
        "`user_choice` with `metadata.choice = {\"source\": \"slotType\", "
        "\"slotTypeId\": \"<the ATTACHED SLOT'S NAME>\"}`. The value is stored "
        "verbatim as the internal slot id, so it must be the slot NAME "
        "(`moreHelp`), not the slot type id (`yesNo`). Its edges are "
        "`slot <name> exists` / `not_exists`, never `captured_flow`.\n"
        "\n"
        "### 4. Retry after a no-match\n"
        "Point the `not_exists` edge at a short recovery `basic` node that "
        "carries `metadata.stateModifications: [{\"type\": \"slot\", \"name\": "
        "\"<slot>\", \"modification\": \"clear\"}]` and loops BACK to the same "
        "`user_choice`. Slot values persist for the session, so revisiting the "
        "capture node without clearing falls through to Fallback, and a SECOND "
        "capture node for the same slot fails immediately with slot_no_match.\n"
        "\n"
        "### 5. Data requests\n"
        "`node.dataRequests: [{\"dataRequestId\": \"getOrder\", \"payload\": "
        "{\"orderNumber\": \"{orderNumber:NLX.Slot}\"}}]` — without `payload` "
        "the webhook receives no fields at all. Map every request field from a "
        "slot (`{name:NLX.Slot}`) or a context variable (`{name:NLX.Context}`). "
        "Its edges are `node_status eq success` | `failure` | `timeout` — "
        "`error` is invalid and produces an unroutable NoMessages branch.\n"
        "\n"
        "### 6. Answering with the result\n"
        "The answer is a deterministic `basic` message with placeholders: "
        "`{<dataRequestId>.<field>:NLX.Variable}`. Every placeholder field MUST "
        "exist in that data request's `responseSchema` with EXACTLY that name "
        "(`price` and `unitPrice` are different fields; a placeholder that does "
        "not resolve is read out literally). Do NOT use `generative_text` to "
        "state a looked-up value: it emits no message at runtime, so the caller "
        "hears nothing, and a generated number is not the number the backend "
        "returned.\n"
        "\n"
        "### 7. Where a flow ENDS\n"
        "A successful operation redirects to `FollowUpFlow` "
        "(`metadata.redirect = {\"type\": \"flow\", \"flowId\": "
        "\"FollowUpFlow\"}`), which asks \"anything else?\" and keeps the "
        "session alive. `end` EXITS the application and ends the customer's "
        "conversation — using it after an answer is what made a live assistant "
        "hang up after one question. A flow that gives up hands over with a "
        "redirect to `EscalationFlow`. Clear the slots you captured on the "
        "node that LEAVES the flow, not at its start (a start-of-flow clear "
        "erases the value the routing utterance already filled).\n"
        "\n"
        "### 8. Escalation\n"
        "An `escalate` node is TERMINAL: no `childNodes`. Give it its "
        "\"connecting you now\" `messages` and, when the contact flow needs the "
        "reason, `metadata.stateModifications` setting a context variable. An "
        "`end` after `escalate` made Connect take the Success branch and the "
        "caller was never transferred.\n"
    )
