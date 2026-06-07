"""Tests for the Contact Flow structural / import-safety linter.

Covers the hallucinated-block regressions that broke Amazon Connect import:
  - `Trigger` / `EntryPoint` entry wrappers (no such block exists)
  - `InvokeAgentAction` (→ ConnectParticipantWithLexBot, with param rewrite)
  - `CheckCondition` (→ Compare)
  - `RealTime` in Voice AnalyticsModes
and asserts valid flows are left untouched (no false positives).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.asset_linters import lint_contact_flow  # noqa: E402


def _valid_flow():
    return {
        "Version": "2019-10-30",
        "StartAction": "log",
        "Actions": [
            {"Identifier": "log", "Type": "UpdateFlowLoggingBehavior",
             "Parameters": {"FlowLoggingBehavior": "Enabled"},
             "Transitions": {"NextAction": "end"}},
            {"Identifier": "end", "Type": "DisconnectParticipant",
             "Parameters": {}, "Transitions": {}},
        ],
    }


def test_valid_flow_passes_untouched():
    r = lint_contact_flow(json.dumps(_valid_flow()))
    assert r["ok"] is True
    assert r["fixes_applied"] == []
    assert r["errors"] == []


def test_trigger_block_is_removed_and_startaction_repointed():
    flow = {
        "Version": "2019-10-30",
        "StartAction": "entry-point",
        "Actions": [
            {"Identifier": "entry-point", "Type": "Trigger", "Parameters": {},
             "Transitions": {"NextAction": "log"}},
            {"Identifier": "log", "Type": "UpdateFlowLoggingBehavior",
             "Parameters": {"FlowLoggingBehavior": "Enabled"},
             "Transitions": {"NextAction": "end"}},
            {"Identifier": "end", "Type": "DisconnectParticipant",
             "Parameters": {}, "Transitions": {}},
        ],
    }
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    assert fixed["StartAction"] == "log"
    assert all(a["Type"] != "Trigger" for a in fixed["Actions"])


def test_checkcondition_renamed_to_compare():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "branch", "Type": "CheckCondition",
        "Parameters": {"ComparisonValue": "$.Channel"},
        "Transitions": {"NextAction": "end",
                        "Conditions": [{"NextAction": "end",
                                        "Condition": {"Operator": "Equals", "Operands": ["VOICE"]}}],
                        "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "end"}]},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "branch"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    types = {a["Type"] for a in fixed["Actions"]}
    assert "CheckCondition" not in types
    assert "Compare" in types


def test_invoke_agent_action_rewritten_to_lex_bot():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "ai", "Type": "InvokeAgentAction",
        "Parameters": {"AgentAliasArn": "{{AgentAliasArn}}",
                       "IdleSessionTimeout": "300",
                       "EndConversationPhrase": "Goodbye"},
        "Transitions": {"NextAction": "end",
                        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "end"}]},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "ai"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    ai = next(a for a in fixed["Actions"] if a["Identifier"] == "ai")
    assert ai["Type"] == "ConnectParticipantWithLexBot"
    # invalid params dropped, valid Lex params present
    assert "AgentAliasArn" not in ai["Parameters"]
    assert "IdleSessionTimeout" not in ai["Parameters"]
    assert ai["Parameters"]["LexV2Bot"]["AliasArn"] == "{{AgentAliasArn}}"
    assert ai["Parameters"]["Text"] == "Goodbye"


def test_realtime_stripped_from_voice_analytics():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "rec", "Type": "UpdateContactRecordingBehavior",
        "Parameters": {"AnalyticsBehavior": {"ChannelConfiguration": {
            "Voice": {"AnalyticsModes": ["RealTime", "PostContact"]}}}},
        "Transitions": {"NextAction": "end"},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "rec"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    rec = next(a for a in fixed["Actions"] if a["Identifier"] == "rec")
    modes = rec["Parameters"]["AnalyticsBehavior"]["ChannelConfiguration"]["Voice"]["AnalyticsModes"]
    assert "RealTime" not in modes
    assert modes == ["PostContact"]


def test_truly_unknown_type_is_hard_error():
    flow = _valid_flow()
    flow["Actions"][0]["Type"] = "TotallyMadeUpBlock"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is False
    assert any("TotallyMadeUpBlock" in e for e in r["errors"])


# ---------------------------------------------------------------------------
# Parameter normalization — every rule below was derived from real
# InvalidContactFlowException `problems` returned by Connect's CreateContactFlow.
# ---------------------------------------------------------------------------

def _fixed_action(flow, identifier):
    r = lint_contact_flow(json.dumps(flow))
    d = json.loads(r["fixed_json"])
    return next(a for a in d["Actions"] if a["Identifier"] == identifier), r


def test_recording_behavior_agent_customer_rebuilt():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "rec", "Type": "UpdateContactRecordingBehavior",
                               "Parameters": {"Agent": "Enabled", "Customer": "Enabled"},
                               "Transitions": {"NextAction": "end"}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "rec"
    a, _ = _fixed_action(flow, "rec")
    assert "Agent" not in a["Parameters"] and "Customer" not in a["Parameters"]
    assert a["Parameters"]["RecordingBehavior"]["RecordedParticipants"] == ["Agent", "Customer"]


def test_tts_voice_params_renamed():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "v", "Type": "UpdateContactTextToSpeechVoice",
                               "Parameters": {"VoiceId": "Seoyeon", "Engine": "Generative", "LanguageCode": "ko-KR"},
                               "Transitions": {"NextAction": "end"}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "v"
    a, _ = _fixed_action(flow, "v")
    assert a["Parameters"]["TextToSpeechVoice"] == "Seoyeon"
    assert a["Parameters"]["TextToSpeechEngine"] == "Generative"
    assert "VoiceId" not in a["Parameters"] and "LanguageCode" not in a["Parameters"]


def test_logging_behavior_renamed():
    flow = _valid_flow()
    flow["Actions"][0]["Type"] = "UpdateFlowLoggingBehavior"
    flow["Actions"][0]["Parameters"] = {"LoggingBehavior": "Enabled"}
    a, _ = _fixed_action(flow, "log")
    assert a["Parameters"]["FlowLoggingBehavior"] == "Enabled"
    assert "LoggingBehavior" not in a["Parameters"]


def test_target_queue_flattened_and_renamed():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "q", "Type": "UpdateContactTargetQueue",
                               "Parameters": {"QueueId": {"QueueId": "arn:q"}},
                               "Transitions": {"NextAction": "end"}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "q"
    a, _ = _fixed_action(flow, "q")
    assert a["Parameters"]["QueueId"] == "arn:q"


def test_lambda_request_attributes_renamed():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "fn", "Type": "InvokeLambdaFunction",
                               "Parameters": {"LambdaFunctionARN": "arn:fn", "InvocationTimeLimitSeconds": "8",
                                              "RequestAttributes": {"phone": "$.x"}},
                               "Transitions": {"NextAction": "end"}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "fn"
    a, _ = _fixed_action(flow, "fn")
    assert "RequestAttributes" not in a["Parameters"]
    assert a["Parameters"]["LambdaInvocationAttributes"] == {"phone": "$.x"}


def test_terminal_block_strips_stray_conditions_errors():
    flow = _valid_flow()
    # DisconnectParticipant with stray top-level Conditions/Errors
    flow["Actions"][1]["Conditions"] = []
    flow["Actions"][1]["Errors"] = [{"ErrorType": "NoMatchingError", "NextAction": "log"}]
    a, _ = _fixed_action(flow, "end")
    assert "Conditions" not in a and "Errors" not in a and "Transitions" not in a


def test_check_hours_gets_both_true_false_branches():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "h", "Type": "CheckHoursOfOperation",
                               "Parameters": {"HoursOfOperationId": None},
                               "Transitions": {"NextAction": "end",
                                               "Conditions": [{"Condition": {"Operator": "Equals", "Operands": ["True"]},
                                                               "NextAction": "end"}]}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "h"
    a, _ = _fixed_action(flow, "h")
    operands = {c["Condition"]["Operands"][0] for c in a["Transitions"]["Conditions"]}
    assert operands == {"True", "False"}
    assert a["Parameters"]["HoursOfOperationId"]  # placeholder filled


def test_compare_invalid_jsonpath_root_rehomed():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "c", "Type": "Compare",
                               "Parameters": {"ComparisonValue": "$.Agent.ReturnControlEvent.Type"},
                               "Transitions": {"NextAction": "end",
                                               "Conditions": [{"Condition": {"Operator": "Equals", "Operands": ["X"]}, "NextAction": "end"}]}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "c"
    a, _ = _fixed_action(flow, "c")
    assert a["Parameters"]["ComparisonValue"] == "$.Attributes.Type"


def test_compare_invalid_error_type_stripped():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "c", "Type": "Compare",
                               "Parameters": {"ComparisonValue": "$.Attributes.x"},
                               "Transitions": {"NextAction": "end",
                                               "Conditions": [{"Condition": {"Operator": "Equals", "Operands": ["X"]}, "NextAction": "end"}],
                                               "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "end"}]}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "c"
    a, _ = _fixed_action(flow, "c")
    types = {e["ErrorType"] for e in a["Transitions"].get("Errors", [])}
    assert "NoMatchingError" not in types
    assert "NoMatchingCondition" in types


def test_tool_result_operands_normalized_to_canonical():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "c", "Type": "Compare",
                               "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.Tool"},
                               "Transitions": {"NextAction": "end",
                                               "Conditions": [
                                                   {"Condition": {"Operator": "Equals", "Operands": ["ESCALATE"]}, "NextAction": "end"},
                                                   {"Condition": {"Operator": "Equals", "Operands": ["END_CALL"]}, "NextAction": "end"}]}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "c"
    a, _ = _fixed_action(flow, "c")
    operands = {c["Condition"]["Operands"][0] for c in a["Transitions"]["Conditions"]}
    assert operands == {"Escalate", "Complete"}


def test_tool_result_spec_extension_preserved():
    # Spec-driven extensions like OutOfHoursComplete must NOT be remapped.
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "c", "Type": "Compare",
                               "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.Tool"},
                               "Transitions": {"NextAction": "end",
                                               "Conditions": [
                                                   {"Condition": {"Operator": "Equals", "Operands": ["Escalate"]}, "NextAction": "end"},
                                                   {"Condition": {"Operator": "Equals", "Operands": ["OutOfHoursComplete"]}, "NextAction": "end"}]}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "c"
    a, _ = _fixed_action(flow, "c")
    operands = {c["Condition"]["Operands"][0] for c in a["Transitions"]["Conditions"]}
    assert operands == {"Escalate", "OutOfHoursComplete"}


def test_lex_bot_v1_and_wrong_keys_normalized():
    flow = _valid_flow()
    flow["Actions"].insert(1, {"Identifier": "lex", "Type": "ConnectParticipantWithLexBot",
                               "Parameters": {"BotAliasArn": "arn:lex", "ParticipantRole": "X",
                                              "SessionAttributes": {"k": "v"}},
                               "Transitions": {"NextAction": "end"}})
    flow["Actions"][0]["Transitions"]["NextAction"] = "lex"
    a, _ = _fixed_action(flow, "lex")
    assert a["Parameters"]["LexV2Bot"]["AliasArn"] == "arn:lex"
    assert "BotAliasArn" not in a["Parameters"] and "ParticipantRole" not in a["Parameters"]
    assert any(k in a["Parameters"] for k in ("Text", "SSML", "PromptId", "Media"))
    # required errors injected
    types = {e["ErrorType"] for e in a["Transitions"]["Errors"]}
    assert {"NoMatchingError", "NoMatchingCondition"} <= types
