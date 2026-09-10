from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_contact_flow_binding import (
    AGENTIC_CX_ACTION_TYPE,
    AGENTIC_CX_PLACEHOLDER_ID,
    normalize_acxd_contact_flow,
)


class _Model:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self) -> dict:
        return self.payload


def test_normalizer_replaces_lex_with_agentic_cx_binding_and_real_branches():
    flow = {
        "Version": "2019-10-30",
        "StartAction": "Greeting",
        "Metadata": {"ActionMetadata": {"Lex": {"position": {"x": 1, "y": 1}}}},
        "Actions": [
            {
                "Identifier": "Greeting",
                "Type": "MessageParticipant",
                "Parameters": {"Text": "Welcome $.Lex.SessionAttributes.customerId"},
                "Transitions": {"NextAction": "Lex"},
            },
            {
                "Identifier": "Lex",
                "Type": "ConnectParticipantWithLexBot",
                "Parameters": {"Text": "How can I help?", "LexV2Bot": {"AliasArn": "{LEX_BOT_ALIAS_ARN}"}},
                "Transitions": {"NextAction": "LexResult"},
            },
            {
                "Identifier": "LexResult",
                "Type": "Compare",
                "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.customerId"},
                "Transitions": {"NextAction": "Disconnect"},
            },
            {"Identifier": "Disconnect", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        ],
    }
    variables = [
        {"name": "customerId", "type": "string", "from_contact_attribute": "$.Attributes.CustomerId"}
    ] + [
        {"name": f"context{index}", "type": "string"} for index in range(1, 12)
    ]
    flow_spec = _Model({
        "application": {
            "speech_engine": "agentic_voice",
            "context_variables": variables,
        }
    })

    normalized = normalize_acxd_contact_flow(flow, flow_spec)
    actions = {action["Identifier"]: action for action in normalized["Actions"]}
    binding = normalized["Metadata"]["acxdBinding"]

    assert AGENTIC_CX_ACTION_TYPE == "ConnectParticipantWithAgenticCX"
    assert AGENTIC_CX_PLACEHOLDER_ID in actions
    block = actions[AGENTIC_CX_PLACEHOLDER_ID]
    assert block["Type"] == "ConnectParticipantWithAgenticCX"
    agent = block["Parameters"]["AgentConfiguration"]
    assert agent["WorkspaceId"] == "{ACXD_WORKSPACE_ID}"
    assert agent["ApplicationId"] == "{ACXD_APPLICATION_ID}"
    assert agent["Alias"] == "{ACXD_ALIAS_ID}"
    assert agent["ContextVariables"]["customerId"] == "$.Attributes.CustomerId"
    assert len(agent["ContextVariables"]) == 10
    assert block["Parameters"]["SpeechRecognitionConfiguration"] == {"SpeechRecognitionEngine": "AMAZON_AGENTIC_VOICE"}
    errors = {e["ErrorType"]: e["NextAction"] for e in block["Transitions"]["Errors"]}
    assert set(errors) == {"NoMatchingError", "NoMatchingCondition", "InputTimeLimitExceeded"}
    assert errors["NoMatchingError"] == "AgenticCXFallbackMessage"
    escalation = block["Transitions"]["Conditions"][0]
    assert escalation["Condition"]["Operands"] == ["Escalation"]
    assert actions[escalation["NextAction"]]["Type"] == "TransferContactToQueue"
    assert actions[block["Transitions"]["NextAction"]]["Type"] == "DisconnectParticipant"
    assert "Lex" not in actions
    assert "LexResult" not in actions
    assert len(binding["contextVariables"]) == 10
    assert binding["workspaceId"] == "{ACXD_WORKSPACE_ID}"
    assert binding["applicationId"] == "{ACXD_APPLICATION_ID}"
    assert binding["aliasId"] == "{ACXD_ALIAS_ID}"

    for target in binding["branches"].values():
        assert target in actions
    assert actions[binding["branches"]["Default"]]["Type"] == "DisconnectParticipant"
    assert actions[binding["branches"]["IdleChatTimeout"]]["Type"] == "DisconnectParticipant"
    assert actions[binding["branches"]["Escalation"]]["Type"] == "TransferContactToQueue"
    assert actions[binding["branches"]["Error"]]["Type"] == "MessageParticipant"

    serialized = json.dumps(normalized)
    assert "$.Lex.SessionAttributes." not in serialized
    # Lex session-attribute reads become Agentic CX context-variable reads.
    assert "$.AgenticCX.ContextVariables.customerId" in serialized


def test_normalizer_rewrites_known_attribute_after_connect_lint_pass():
    flow = {
        "Version": "2019-10-30",
        "StartAction": "Entry",
        "Actions": [{
            "Identifier": "Entry",
            "Type": "MessageParticipant",
            "Parameters": {"Text": "Hello"},
            "Transitions": {"NextAction": "Disconnect"},
        }, {
            "Identifier": "Disconnect",
            "Type": "DisconnectParticipant",
            "Parameters": {},
            "Transitions": {},
        }],
    }
    spec = _Model({"application": {"context_variables": [{"name": "customerId"}]}})
    flow["Actions"][0]["Parameters"]["Text"] = "$.Attributes.customerId"

    normalized = normalize_acxd_contact_flow(flow, spec, rewrite_attribute_context=True)
    assert normalized["Actions"][0]["Parameters"]["Text"] == "$.AgenticCX.ContextVariables.customerId"


def test_real_agentic_cx_block_replaces_placeholder_and_wisdom_session_is_spliced_out():
    """Live CreateContactFlow (2026-09-10): the Q in Connect block's unresolved
    ARN failed the import; the block itself is the console-exported
    ConnectParticipantWithAgenticCX (Escalation via Conditions, idle timeout via
    InputTimeLimitExceeded), no Compare helper needed."""
    flow = {
        "Version": "2019-10-30",
        "StartAction": "Logging",
        "Metadata": {"ActionMetadata": {"Wisdom": {"position": {"x": 1, "y": 1}}}},
        "Actions": [
            {"Identifier": "Logging", "Type": "UpdateFlowLoggingBehavior",
             "Parameters": {"FlowLoggingBehavior": "Enabled"}, "Transitions": {"NextAction": "Wisdom"}},
            {"Identifier": "Wisdom", "Type": "CreateWisdomSession",
             "Parameters": {"WisdomAssistantArn": "{{WISDOM_ASSISTANT_ARN}}"},
             "Transitions": {"NextAction": "Lex", "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "Lex"}]}},
            {"Identifier": "Lex", "Type": "ConnectParticipantWithLexBot",
             "Parameters": {"Text": "Hi", "LexV2Bot": {"AliasArn": "{LEX_BOT_ALIAS_ARN}"}},
             "Transitions": {"NextAction": "Disconnect"}},
            {"Identifier": "Disconnect", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        ],
    }
    normalized = normalize_acxd_contact_flow(flow, _Model({"application": {}}))
    actions = {action["Identifier"]: action for action in normalized["Actions"]}
    assert "Wisdom" not in actions
    assert "Wisdom" not in normalized["Metadata"].get("ActionMetadata", {})
    assert actions["Logging"]["Transitions"]["NextAction"] == AGENTIC_CX_PLACEHOLDER_ID
    block = actions[AGENTIC_CX_PLACEHOLDER_ID]
    assert block["Type"] == "ConnectParticipantWithAgenticCX"
    assert "AgenticCXBranch" not in actions
    assert [c["Condition"]["Operands"] for c in block["Transitions"]["Conditions"]] == [["Escalation"]]
    assert {e["ErrorType"] for e in block["Transitions"]["Errors"]} == {"NoMatchingError", "NoMatchingCondition", "InputTimeLimitExceeded"}
    # no speech engine declared → no SpeechRecognitionConfiguration is forced
    assert "SpeechRecognitionConfiguration" not in block["Parameters"] or block["Parameters"]["SpeechRecognitionConfiguration"]
    assert not any("WisdomAssistantArn" in json.dumps(a) for a in normalized["Actions"])
