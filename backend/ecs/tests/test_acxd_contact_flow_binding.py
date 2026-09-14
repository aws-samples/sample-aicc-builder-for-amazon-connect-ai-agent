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
    # The application owns the greeting: the Contact Flow's pre-block welcome
    # message is stripped and the caller reaches the block directly.
    assert "Greeting" not in actions
    # Live (2026-09-14): a flow without a language block in front of the Agentic CX
    # block fails every contact ("NLX Chat Streaming Failed"); the binding inserts one.
    assert normalized["StartAction"] == "AgenticCXSetLanguage"
    language = next(a for a in normalized["Actions"] if a["Identifier"] == "AgenticCXSetLanguage")
    assert language["Type"] == "UpdateContactData" and language["Parameters"]["LanguageCode"]
    assert language["Transitions"]["NextAction"] == AGENTIC_CX_PLACEHOLDER_ID


def test_normalizer_rewrites_known_attribute_after_connect_lint_pass():
    flow = {
        "Version": "2019-10-30",
        "StartAction": "Entry",
        "Actions": [{
            # a NON-speech pre-block action (speech before the block is stripped)
            "Identifier": "Entry",
            "Type": "UpdateContactAttributes",
            "Parameters": {"Attributes": {"copy": "$.Attributes.customerId"}},
            "Transitions": {"NextAction": "Disconnect"},
        }, {
            "Identifier": "Disconnect",
            "Type": "DisconnectParticipant",
            "Parameters": {},
            "Transitions": {},
        }],
    }
    spec = _Model({"application": {"context_variables": [{"name": "customerId"}]}})
    # a read AFTER the block: the block's Default branch → this message → Disconnect
    flow["Actions"][0]["Transitions"]["NextAction"] = AGENTIC_CX_PLACEHOLDER_ID
    flow["Actions"].insert(1, {
        "Identifier": AGENTIC_CX_PLACEHOLDER_ID, "Type": AGENTIC_CX_ACTION_TYPE,
        "Parameters": {"AgentConfiguration": {}},
        "Transitions": {"NextAction": "AfterBlock", "Conditions": [], "Errors": []},
    })
    flow["Actions"].insert(2, {
        # a post-block NON-speech read (a message on the Default path would be
        # stripped: the app already said goodbye)
        "Identifier": "AfterBlock", "Type": "UpdateContactAttributes",
        "Parameters": {"Attributes": {"seen": "$.Attributes.customerId"}},
        "Transitions": {"NextAction": "Disconnect"},
    })

    normalized = normalize_acxd_contact_flow(flow, spec, rewrite_attribute_context=True)
    by_id = {a["Identifier"]: a for a in normalized["Actions"]}
    # $.AgenticCX.* exists only once the block has returned: the pre-block read
    # keeps the contact attribute (live: a Compare + greeting before the block
    # read an empty value), the post-block read is rewritten.
    assert by_id["Entry"]["Parameters"]["Attributes"]["copy"] == "$.Attributes.customerId"
    assert by_id["AfterBlock"]["Parameters"]["Attributes"]["seen"] == "$.AgenticCX.ContextVariables.customerId"


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
    assert actions["Logging"]["Transitions"]["NextAction"] == "AgenticCXSetLanguage"
    assert actions["AgenticCXSetLanguage"]["Transitions"]["NextAction"] == AGENTIC_CX_PLACEHOLDER_ID
    block = actions[AGENTIC_CX_PLACEHOLDER_ID]
    assert block["Type"] == "ConnectParticipantWithAgenticCX"
    assert "AgenticCXBranch" not in actions
    assert [c["Condition"]["Operands"] for c in block["Transitions"]["Conditions"]] == [["Escalation"]]
    assert {e["ErrorType"] for e in block["Transitions"]["Errors"]} == {"NoMatchingError", "NoMatchingCondition", "InputTimeLimitExceeded"}
    # no speech engine declared → no SpeechRecognitionConfiguration is forced
    assert "SpeechRecognitionConfiguration" not in block["Parameters"] or block["Parameters"]["SpeechRecognitionConfiguration"]
    assert not any("WisdomAssistantArn" in json.dumps(a) for a in normalized["Actions"])


def test_authored_agentic_cx_branches_are_preserved_not_bypassed():
    """Live (SELC, 2026-09-11): the generator wired Default → call-outcome logger
    and Escalation → business-hours check → set queue → transfer; the binder
    replaced both with fixed disconnect / queue targets, so the logger and the
    hours check became unreachable (review: 'escalation unreachable')."""
    flow = {
        "Version": "2019-10-30",
        "StartAction": "Entry",
        "Actions": [
            {"Identifier": "Entry", "Type": "MessageParticipant", "Parameters": {"Text": "Hi"},
             "Transitions": {"NextAction": AGENTIC_CX_PLACEHOLDER_ID}},
            {"Identifier": AGENTIC_CX_PLACEHOLDER_ID, "Type": AGENTIC_CX_ACTION_TYPE,
             "Parameters": {"AgentConfiguration": {}},
             "Transitions": {
                 "NextAction": "log-completed",
                 "Conditions": [{"NextAction": "check-hours",
                                 "Condition": {"Operator": "Equals", "Operands": ["Escalation"]}}],
                 "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "AgenticCXFallbackMessage"}],
             }},
            {"Identifier": "log-completed", "Type": "InvokeLambdaFunction",
             "Parameters": {"LambdaFunctionARN": "{{LOG}}", "LambdaInvocationAttributes": {"intent": "$.Attributes.intent"}},
             "Transitions": {"NextAction": "disconnect"}},
            {"Identifier": "check-hours", "Type": "CheckHoursOfOperation", "Parameters": {},
             "Transitions": {"NextAction": "set-queue", "Conditions": [], "Errors": []}},
            {"Identifier": "set-queue", "Type": "UpdateContactTargetQueue", "Parameters": {"QueueId": "{{QUEUE_ARN}}"},
             "Transitions": {"NextAction": "transfer-queue"}},
            {"Identifier": "transfer-queue", "Type": "TransferContactToQueue", "Parameters": {},
             "Transitions": {"Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}},
            {"Identifier": "disconnect", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        ],
    }
    spec = _Model({"application": {"context_variables": [{"name": "intent"}]}})
    normalized = normalize_acxd_contact_flow(flow, spec, rewrite_attribute_context=True)
    by_id = {a["Identifier"]: a for a in normalized["Actions"]}
    block = by_id[AGENTIC_CX_PLACEHOLDER_ID]
    assert block["Transitions"]["NextAction"] == "log-completed"
    assert block["Transitions"]["Conditions"][0]["NextAction"] == "check-hours"
    assert normalized["Metadata"]["acxdBinding"]["branches"]["Default"] == "log-completed"
    assert normalized["Metadata"]["acxdBinding"]["branches"]["Escalation"] == "check-hours"
    # the chain stays reachable and the post-block read is rewritten
    assert {"check-hours", "set-queue", "transfer-queue", "log-completed"} <= set(by_id)
    assert by_id["log-completed"]["Parameters"]["LambdaInvocationAttributes"]["intent"] == "$.AgenticCX.ContextVariables.intent"


def test_speech_ownership_contact_flow_stays_silent_except_telephony_states():
    """Live (SELC): the Contact Flow greeted before the block and the app's
    WelcomeFlow greeted again with the same sentence; the fallback was English in
    a Korean flow. The app owns greeting/closing; the flow keeps a recording
    notice, the escalation-path announcements and the (localised) fallback."""
    flow = {
        "Version": "2019-10-30",
        "StartAction": "recording-notice",
        "Actions": [
            {"Identifier": "recording-notice", "Type": "MessageParticipant",
             "Parameters": {"Text": "서비스 품질 향상을 위해 통화가 녹음됩니다."},
             "Transitions": {"NextAction": "welcome"}},
            {"Identifier": "welcome", "Type": "MessageParticipant",
             "Parameters": {"Text": "안녕하세요, 삼성전자로지텍입니다. 무엇을 도와드릴까요?"},
             "Transitions": {"NextAction": AGENTIC_CX_PLACEHOLDER_ID}},
            {"Identifier": AGENTIC_CX_PLACEHOLDER_ID, "Type": AGENTIC_CX_ACTION_TYPE,
             "Parameters": {"AgentConfiguration": {}},
             "Transitions": {
                 "NextAction": "goodbye",
                 "Conditions": [{"NextAction": "check-hours",
                                 "Condition": {"Operator": "Equals", "Operands": ["Escalation"]}}],
                 "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "AgenticCXFallbackMessage"}],
             }},
            {"Identifier": "goodbye", "Type": "MessageParticipant",
             "Parameters": {"Text": "이용해 주셔서 감사합니다."}, "Transitions": {"NextAction": "log"}},
            {"Identifier": "log", "Type": "InvokeLambdaFunction", "Parameters": {"LambdaFunctionARN": "{{LOG}}"},
             "Transitions": {"NextAction": "disconnect"}},
            {"Identifier": "check-hours", "Type": "CheckHoursOfOperation", "Parameters": {},
             "Transitions": {"NextAction": "transfer-queue",
                             "Conditions": [{"NextAction": "after-hours", "Condition": {"Operator": "Equals", "Operands": ["False"]}}]}},
            {"Identifier": "after-hours", "Type": "MessageParticipant",
             "Parameters": {"Text": "지금은 상담 시간이 아닙니다."}, "Transitions": {"NextAction": "disconnect"}},
            {"Identifier": "transfer-queue", "Type": "TransferContactToQueue", "Parameters": {},
             "Transitions": {"Errors": [{"ErrorType": "NoMatchingError", "NextAction": "disconnect"}]}},
            {"Identifier": "disconnect", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        ],
    }
    spec = _Model({"application": {"locales": ["ko-KR"], "context_variables": []}})
    normalized = normalize_acxd_contact_flow(flow, spec)
    by_id = {a["Identifier"]: a for a in normalized["Actions"]}
    assert "welcome" not in by_id                       # greeting: the app's
    assert "goodbye" not in by_id                       # closing: the app's
    assert "recording-notice" in by_id                  # legal notice: the flow's
    assert normalized["StartAction"] == "recording-notice"
    assert by_id["recording-notice"]["Transitions"]["NextAction"] == "AgenticCXSetLanguage"
    assert by_id["AgenticCXSetLanguage"]["Transitions"]["NextAction"] == AGENTIC_CX_PLACEHOLDER_ID
    assert by_id[AGENTIC_CX_PLACEHOLDER_ID]["Transitions"]["NextAction"] == "log"   # Default → logger, silent
    assert "after-hours" in by_id                       # telephony state: the flow's
    assert by_id["AgenticCXFallbackMessage"]["Parameters"]["Text"].startswith("죄송합니다")


def test_language_block_is_kept_when_the_flow_already_sets_one():
    from tools.acxd_contact_flow_binding import _ensure_language_before_block, _application_locale
    doc = {"StartAction": "lang", "Actions": [
        {"Identifier": "lang", "Type": "UpdateContactData", "Parameters": {"LanguageCode": "ko-KR"},
         "Transitions": {"NextAction": "acx"}},
        {"Identifier": "acx", "Type": "ConnectParticipantWithAgenticCX", "Parameters": {}, "Transitions": {}},
    ]}
    _ensure_language_before_block(doc, {"locales": ["ko-KR"]})
    assert [a["Identifier"] for a in doc["Actions"]] == ["lang", "acx"]
    assert _application_locale({"locales": ["ko-KR"]}) == "ko-KR"
    assert _application_locale({"language": "ja"}) == "ja-JP"
    assert _application_locale({}) == "en-US"
